"""Message destinations that code publishes to and consumes from.

Kafka: Spring ``KafkaTemplate.send(topic, ...)`` and ``@KafkaListener(topics
= ...)``, and Python kafka-python ``KafkaProducer.send(topic)`` and
``KafkaConsumer(topic, ...)``. AMQP (RabbitMQ): Spring
``RabbitTemplate.convertAndSend`` and ``@RabbitListener(queues = ...)``,
Python pika ``basic_publish``/``queue_bind``/``basic_consume`` and Go
``amqp`` ``Publish``/``QueueBind``/``Consume``.

An AMQP destination is ``exchange/routing-key``. A consumer of queue ``q``
receives what is published to the default exchange with key ``q`` (``/q``),
as every broker binds each queue to it, and what is published to ``x`` with
key ``k`` for each ``QueueBind(q, k, x)`` in the same file. Only exact keys
are read: a binding key with ``*`` or ``#`` (a topic exchange pattern) is
recorded as unread. Destinations with unknown text are not read.

STOMP (WebSocket messaging): Spring ``@MessageMapping`` methods consume
``<application prefix><path>``, ``@SendTo``, ``@SubscribeMapping`` and
``SimpMessagingTemplate.convertAndSend`` publish, and JavaScript STOMP clients
(``Stomp.over``/``Stomp.client``, webstomp, ``@stomp/stompjs`` ``Client``,
AngularJS ngStomp ``$stomp``: ``send``/``publish`` and ``subscribe``;
``@stomp/rx-stomp`` ``RxStomp``: ``publish`` and ``watch``;
``ng2-stomp-service`` ``StompService``: ``send`` and ``subscribe``; see
``stomp_clients``). The broker is ``stomp``.

GraphQL subscription pubsub: ``publish(topic, ...)`` and
``asyncIterator(topic)`` (or ``asyncIterableIterator``, ``subscribe``) on a
name bound to a ``PubSub`` (graphql-subscriptions and the servers that
re-export it) or a ``RedisPubSub``. An in-memory PubSub's destination is
``file#name/topic``, as it delivers only within its own instance; a
RedisPubSub's is the Redis channel.
"""

from dataclasses import dataclass
import re

from ..syntax import identifier
from .folding import Folder, text
from .http import PLACEHOLDER
from .script_constants import fold_through_calls
from .stomp_clients import bound_kinds, value_kind
from .spring import _Imports, _annotations, _arguments, _Values
from .web import _js_modules, _js_object, _python_imports, _type_span


@dataclass(frozen=True)
class MessageEndpoint:
    """One publish or consume site and the destination it names.

    ``declaration_id`` is the handler (a listener method) or the innermost
    declaration around a call, None at file level. ``module`` and
    ``service`` name the build module and service that run the code when
    that is not the file's own module (library code a service depends on).
    """

    id: str
    file: str
    line: int
    column: int
    declaration_id: str | None
    role: str  # publish | consume
    broker: str  # kafka | amqp
    destination: str
    client: str
    module: str | None = None
    service: str | None = None
    # How the running service is named (SpringConfig.identity), and the
    # Compose services ("file:name") that deploy it, for Spring Cloud Stream.
    service_identity: str | None = None
    deployed_as: tuple = ()
    # The calls ("file:line") that pass the destination to the parameter of
    # the function around the site, when that is where it comes from.
    argument_from: tuple = ()
    # (direction, start byte, end byte, body key) of the types a handler writes for
    # the message ("request") and its reply ("response"), which the
    # TypeScript compiler resolves (CARRIES).
    payloads: tuple = ()


@dataclass(frozen=True)
class MessageUnread:
    file: str
    line: int
    role: str
    reason: str


class _Sites:
    def __init__(self, tree, content, repository_id, file, definitions):
        self.root = tree.root_node
        self.content = content
        self.repository_id = repository_id
        self.file = file
        mine = [d for d in definitions if d.file == file]
        self.spans = {(d.line, d.column, d.end_line, d.end_column): d.id for d in mine}
        self.enclosing = sorted(
            (((d.line, d.column), (d.end_line, d.end_column), d.id) for d in mine),
            reverse=True,
        )
        self.endpoints, self.unread = [], []
        self._ordinals = {}
        self.calls_of = None

    def declaration(self, node, exact=False):
        start = (node.start_point.row + 1, node.start_point.column)
        end = (node.end_point.row + 1, node.end_point.column)
        if exact:
            return self.spans.get((*start, *end))
        return next((i for s, e, i in self.enclosing if s <= start and end <= e), None)

    def add(
        self,
        node,
        role,
        broker,
        destination,
        client,
        declaration_id=None,
        argument_from=(),
        payloads=(),
    ):
        line = node.start_point.row + 1
        if PLACEHOLDER in destination:
            self.unread.append(
                MessageUnread(self.file, line, role, "destination_not_literal")
            )
            return
        # Kafka has no empty topic: the text is set elsewhere (a flag, say).
        if broker == "kafka" and not destination:
            self.unread.append(MessageUnread(self.file, line, role, "empty_topic"))
            return
        key = (role, broker, destination, line)
        ordinal = self._ordinals.get(key, 0)
        self._ordinals[key] = ordinal + 1
        self.endpoints.append(
            MessageEndpoint(
                id=identifier(
                    self.repository_id,
                    self.file,
                    f"{role} {broker} {destination} {line}",
                    "MessageEndpoint",
                    ordinal,
                ),
                file=self.file,
                line=line,
                column=node.start_point.column,
                declaration_id=(
                    declaration_id
                    if declaration_id is not None
                    else self.declaration(node)
                ),
                role=role,
                broker=broker,
                destination=destination,
                client=client,
                argument_from=tuple(argument_from),
                payloads=tuple(payloads),
            )
        )

    def add_folded(self, node, role, broker, compute, client, folder, payloads=()):
        """Add the sites of the destination ``compute(folder)`` gives; one
        built from parameters of the module-level function around the site
        takes, instead, each value that calls of the function give
        (``self.calls_of``)."""
        for destination, calls in fold_through_calls(folder, compute, self.calls_of):
            self.add(
                node,
                role,
                broker,
                destination,
                client,
                argument_from=calls,
                payloads=payloads,
            )

    def consume_queue(
        self, node, queue, bindings, client, declaration_id=None, identity=None
    ):
        """A consumer of ``queue``: the default exchange plus its bindings.
        A binding is (queue, key, exchange), optionally with the identity of
        the declaration its queue comes from: a queue whose name is not
        known (the broker names it) still has the bindings of the same
        declaration."""
        if PLACEHOLDER in queue:
            self.add(node, "consume", "amqp", queue, client, declaration_id)
        else:
            self.add(node, "consume", "amqp", "/" + queue, client, declaration_id)
        for binding in bindings:
            bound_queue, key, exchange = binding[:3]
            if PLACEHOLDER in queue:
                bound = binding[3] if len(binding) > 3 else None
                if identity is None or bound != identity:
                    continue
            elif bound_queue != queue:
                continue
            if "*" in key or "#" in key:
                self.unread.append(
                    MessageUnread(
                        self.file, node.start_point.row + 1, "consume", "key_pattern"
                    )
                )
                continue
            self.add(
                node, "consume", "amqp", f"{exchange}/{key}", client, declaration_id
            )


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


# -- Java -------------------------------------------------------------------


def _typed_names(root, content, imports, simple_types):
    """Names declared only with one of ``simple_types`` (generics ignored)
    imported from a Spring package."""
    declared = {}
    for node in _walk(root):
        if node.type not in (
            "field_declaration",
            "local_variable_declaration",
            "formal_parameter",
        ):
            continue
        type_node = node.child_by_field_name("type")
        written = re.sub(r"<.*", "", text(type_node, content)) if type_node else ""
        names = (
            [node.child_by_field_name("name")]
            if node.type == "formal_parameter"
            else [
                d.child_by_field_name("name")
                for d in node.children_by_field_name("declarator")
            ]
        )
        for name in names:
            if name is not None:
                declared.setdefault(text(name, content), set()).add(written)
    return {
        name
        for name, types in declared.items()
        if len(types) == 1
        and (t := next(iter(types))) in simple_types
        and imports.from_spring(t)
    }


_KAFKA_LISTENER = ("org.springframework.kafka.annotation",)
_RABBIT_LISTENER = ("org.springframework.amqp.rabbit.annotation",)


_STOMP_HANDLER = ("org.springframework.messaging.handler.annotation",)
_STOMP_SIMP = ("org.springframework.messaging.simp.annotation",)
_STOMP_CONFIG = ("org.springframework.messaging.simp.config",)
_STOMP_TEMPLATES = {
    "SimpMessagingTemplate",
    "SimpMessageSendingOperations",
    "SimpMessageOperations",
}


def stomp_prefixes(tree, content, constants=None):
    """The application destination prefixes a file sets: None without a
    ``setApplicationDestinationPrefixes`` call on a MessageBrokerRegistry
    parameter, a list of prefixes (PLACEHOLDER marks one not fixed) with it."""
    root = tree.root_node
    imports = _Imports(root, content)
    values = _Values(root, content, constants, imports)
    found = None
    for node in _walk(root):
        if node.type != "method_declaration":
            continue
        parameters = node.child_by_field_name("parameters")
        registries = {
            text(p.child_by_field_name("name"), content)
            for p in (parameters.named_children if parameters else ())
            if p.child_by_field_name("type") is not None
            and imports.spring_name(
                text(p.child_by_field_name("type"), content), _STOMP_CONFIG
            )
            == "MessageBrokerRegistry"
        }
        for call in _walk(node):
            if call.type != "method_invocation" or not registries:
                continue
            target = call.child_by_field_name("object")
            if (
                target is None
                or text(target, content) not in registries
                or text(call.child_by_field_name("name"), content)
                != "setApplicationDestinationPrefixes"
            ):
                continue
            found = (found or []) + [
                values.evaluate(a)
                for a in call.child_by_field_name("arguments").named_children
                if a.type not in ("line_comment", "block_comment")
            ]
    return found


def _stomp_path(prefix, path):
    """A mapping path joined to a type-level one, as AntPathMatcher.combine
    does for paths with slashes; None when it does not start with one."""
    joined = prefix.rstrip("/") + "/" + path.lstrip("/") if prefix else path
    return joined if joined.startswith("/") else None


def _extract_stomp(sites, imports, values, application_prefixes):
    """STOMP destinations of Spring message-handling methods and of
    SimpMessagingTemplate sends."""
    content, root = sites.content, sites.root

    def strings(annotation):
        found = _arguments(annotation)
        node = found.get("value", found.get("destination"))
        return values.strings(node) if node is not None else []

    for declaration in _walk(root):
        if declaration.type != "class_declaration":
            continue
        type_paths = [""]
        for written, annotation in _annotations(declaration, content):
            if imports.spring_name(written, _STOMP_HANDLER) == "MessageMapping":
                type_paths = strings(annotation) or [""]
        body = declaration.child_by_field_name("body")
        for node in body.named_children if body is not None else ():
            if node.type != "method_declaration":
                continue
            handler = sites.declaration(node, exact=True)
            line = node.start_point.row + 1
            incoming, outgoing = [], []
            for written, annotation in _annotations(node, content):
                name = imports.spring_name(written, _STOMP_HANDLER) or (
                    imports.spring_name(written, _STOMP_SIMP)
                )
                if name in ("MessageMapping", "SubscribeMapping"):
                    paths = strings(annotation)
                    if not paths:
                        sites.unread.append(
                            MessageUnread(sites.file, line, "consume", "no_destination")
                        )
                    incoming.extend((name, annotation, p) for p in paths)
                elif name == "SendTo":
                    outgoing.append((annotation, strings(annotation)))
                elif name == "SendToUser":
                    sites.unread.append(
                        MessageUnread(sites.file, line, "publish", "user_destination")
                    )
            if not incoming:
                continue
            if application_prefixes is None:
                sites.unread.append(
                    MessageUnread(sites.file, line, "consume", "application_prefix")
                )
                continue
            for name, annotation, path in incoming:
                for type_path in type_paths:
                    joined = _stomp_path(type_path, path)
                    if joined is None or PLACEHOLDER in type_path + path:
                        sites.unread.append(
                            MessageUnread(
                                sites.file, line, "consume", "destination_not_literal"
                            )
                        )
                        continue
                    for prefix in application_prefixes:
                        destination = prefix.rstrip("/") + joined
                        # A subscription is answered by the method's return.
                        role = "publish" if name == "SubscribeMapping" else "consume"
                        sites.add(
                            annotation,
                            role,
                            "stomp",
                            destination,
                            "spring-stomp",
                            handler,
                        )
            for annotation, destinations in outgoing:
                if not destinations:
                    sites.unread.append(
                        MessageUnread(sites.file, line, "publish", "default_send_to")
                    )
                for destination in destinations:
                    sites.add(
                        annotation,
                        "publish",
                        "stomp",
                        destination,
                        "spring-stomp",
                        handler,
                    )
    templates = _typed_names(root, content, imports, _STOMP_TEMPLATES)
    for node in _walk(root):
        if node.type != "method_invocation":
            continue
        receiver = node.child_by_field_name("object")
        if receiver is None or receiver.type not in ("identifier", "field_access"):
            continue
        if text(receiver, content).removeprefix("this.") not in templates:
            continue
        name = text(node.child_by_field_name("name"), content)
        arguments = [
            c
            for c in node.child_by_field_name("arguments").named_children
            if c.type not in ("line_comment", "block_comment")
        ]
        if name == "convertAndSendToUser":
            sites.unread.append(
                MessageUnread(
                    sites.file, node.start_point.row + 1, "publish", "user_destination"
                )
            )
        elif name in ("convertAndSend", "send") and len(arguments) >= 2:
            sites.add(
                node, "publish", "stomp", values.evaluate(arguments[0]), "spring-stomp"
            )


def extract_java(
    sites, constants=None, template_exchange=None, application_prefixes=None
):
    content, root = sites.content, sites.root
    imports = _Imports(root, content)
    values = _Values(root, content, constants, imports)
    kafka = _typed_names(root, content, imports, {"KafkaTemplate"})
    rabbit = _typed_names(
        root, content, imports, {"RabbitTemplate", "AmqpTemplate", "RabbitOperations"}
    )
    _extract_stomp(sites, imports, values, application_prefixes)
    for node in _walk(root):
        if node.type == "method_declaration":
            handler = sites.declaration(node, exact=True)
            for written, annotation in _annotations(node, content):
                arguments = _arguments(annotation)
                if imports.spring_name(written, _KAFKA_LISTENER) == "KafkaListener":
                    topics = arguments.get("topics", arguments.get("value"))
                    if topics is None:
                        sites.unread.append(
                            MessageUnread(
                                sites.file,
                                node.start_point.row + 1,
                                "consume",
                                "no_topics",
                            )
                        )
                    for topic in values.strings(topics):
                        sites.add(
                            annotation,
                            "consume",
                            "kafka",
                            topic,
                            "spring-kafka",
                            handler,
                        )
                elif imports.spring_name(written, _RABBIT_LISTENER) == "RabbitListener":
                    queues = arguments.get("queues", arguments.get("value"))
                    if queues is None:
                        sites.unread.append(
                            MessageUnread(
                                sites.file,
                                node.start_point.row + 1,
                                "consume",
                                "no_queues",
                            )
                        )
                    for queue in values.strings(queues):
                        sites.consume_queue(
                            annotation, queue, (), "spring-amqp", handler
                        )
        elif node.type == "method_invocation":
            receiver = node.child_by_field_name("object")
            name = text(node.child_by_field_name("name"), content)
            if receiver is None or receiver.type not in ("identifier", "field_access"):
                continue
            owner = text(receiver, content).removeprefix("this.")
            arguments = [
                c
                for c in node.child_by_field_name("arguments").named_children
                if c.type not in ("line_comment", "block_comment")
            ]
            if owner in kafka and name in ("send", "sendDefault") and arguments:
                if name == "sendDefault":
                    sites.unread.append(
                        MessageUnread(
                            sites.file,
                            node.start_point.row + 1,
                            "publish",
                            "default_topic",
                        )
                    )
                    continue
                first = arguments[0]
                if first.type == "object_creation_expression":
                    created = first.child_by_field_name("arguments").named_children
                    topic = values.evaluate(created[0]) if created else PLACEHOLDER
                elif len(arguments) >= 2:
                    topic = values.evaluate(first)
                else:
                    topic = PLACEHOLDER  # send(Message): topic in a header
                sites.add(node, "publish", "kafka", topic, "spring-kafka")
            elif (
                owner in rabbit
                and name in ("convertAndSend", "send")
                and len(arguments) >= 2
            ):
                if len(arguments) >= 3:
                    destination = (
                        values.evaluate(arguments[0])
                        + "/"
                        + values.evaluate(arguments[1])
                    )
                elif template_exchange is not None:
                    # (routing key, message): the template's own exchange.
                    destination = (
                        template_exchange + "/" + values.evaluate(arguments[0])
                    )
                else:
                    sites.unread.append(
                        MessageUnread(
                            sites.file,
                            node.start_point.row + 1,
                            "publish",
                            "template_exchange",
                        )
                    )
                    continue
                sites.add(node, "publish", "amqp", destination, "spring-amqp")


# -- Python -----------------------------------------------------------------


def _keywords(arguments, content):
    found, positional = {}, []
    for child in arguments.named_children:
        if child.type == "keyword_argument":
            found[text(child.child_by_field_name("name"), content)] = (
                child.child_by_field_name("value")
            )
        elif child.type != "comment":
            positional.append(child)
    return found, positional


def _argument(keywords, positional, name, index):
    if name in keywords:
        return keywords[name]
    return positional[index] if len(positional) > index else None


def extract_python(sites, imports):
    content, root = sites.content, sites.root
    folder = Folder("python", root, content)

    def value(node):
        return PLACEHOLDER if node is None else folder.evaluate(node)

    modules = {origin.split(".")[0] for origin in imports.values()}
    pika = "pika" in modules
    kafka = "kafka" in modules
    calls = [n for n in _walk(root) if n.type == "call"]
    bindings = []
    if pika:
        for node in calls:
            function = node.child_by_field_name("function")
            if (
                function.type == "attribute"
                and text(function.child_by_field_name("attribute"), content)
                == "queue_bind"
            ):
                keywords, positional = _keywords(
                    node.child_by_field_name("arguments"), content
                )
                queue = value(_argument(keywords, positional, "queue", 0))
                exchange = value(_argument(keywords, positional, "exchange", 1))
                key = _argument(keywords, positional, "routing_key", 2)
                # pika binds with the queue name when no routing key is given.
                key = queue if key is None else value(key)
                bindings.append((queue, key, exchange))
    for node in calls:
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if arguments is None or arguments.type != "argument_list":
            continue
        keywords, positional = _keywords(arguments, content)
        if function.type == "attribute":
            method = text(function.child_by_field_name("attribute"), content)
            owner = function.child_by_field_name("object")
            if pika and method == "basic_publish":
                exchange = value(_argument(keywords, positional, "exchange", 0))
                key = value(_argument(keywords, positional, "routing_key", 1))
                sites.add(node, "publish", "amqp", f"{exchange}/{key}", "pika")
            elif pika and method == "basic_consume":
                queue = value(_argument(keywords, positional, "queue", 0))
                sites.consume_queue(node, queue, bindings, "pika")
            elif kafka and method == "send" and owner.type == "identifier":
                found = folder.binding(owner)
                if found is None or found[1].type != "call":
                    continue
                if _origin(found[1], imports, content) != "kafka.KafkaProducer":
                    continue
                topic = value(_argument(keywords, positional, "topic", 0))
                sites.add(node, "publish", "kafka", topic, "kafka-python")
        elif kafka and _origin(node, imports, content) == "kafka.KafkaConsumer":
            if not positional:
                sites.unread.append(
                    MessageUnread(
                        sites.file, node.start_point.row + 1, "consume", "no_topics"
                    )
                )
            for topic in positional:
                sites.add(node, "consume", "kafka", value(topic), "kafka-python")


def _origin(call, imports, content):
    written = text(call.child_by_field_name("function"), content)
    head, _, rest = written.partition(".")
    origin = imports.get(head)
    if origin is None:
        return None
    return origin + ("." + rest if rest else "")


# -- Go -----------------------------------------------------------------------
#
# Go is read from the type checker's facts (``go_facts``): calls with the
# package declaring the method and the string values of their arguments, and
# keyed composite literals of struct types from other modules. Which package
# a call or a literal belongs to is go/types' answer, not an import pattern;
# constant folding and locals assigned once are gofacts'. The tables below
# say which methods and struct fields of each client library carry a
# destination: that is the libraries' API, not a reading of the code.

_GO_AMQP = ("github.com/streadway/amqp", "github.com/rabbitmq/amqp091-go")
# Channel methods that publish to (exchange, routing key), after a
# context.Context in the WithContext forms.
_GO_AMQP_PUBLISH = frozenset(
    {
        "Publish",
        "PublishWithContext",
        "PublishWithDeferredConfirm",
        "PublishWithDeferredConfirmWithContext",
    }
)
_KAFKA_GO = "github.com/segmentio/kafka-go"
_SARAMA = ("github.com/IBM/sarama", "github.com/Shopify/sarama")
_CONFLUENT = re.compile(r"github\.com/confluentinc/confluent-kafka-go(?:/v\d+)?/kafka")
# Import paths whose files go_facts must check for messaging to be read.
GO_BROKER_IMPORTS = (*_GO_AMQP, _KAFKA_GO, *_SARAMA, "confluent-kafka-go")


def _go_kafka_client(path):
    if path == _KAFKA_GO:
        return "kafka-go"
    if path in _SARAMA:
        return "sarama"
    if _CONFLUENT.fullmatch(path):
        return "confluent-kafka-go"
    return None


# (client, type) -> (role, field) of composite literals that name a topic.
_GO_KAFKA_LITERALS = {
    ("kafka-go", "Writer"): ("publish", "Topic"),
    ("kafka-go", "WriterConfig"): ("publish", "Topic"),
    ("kafka-go", "Message"): ("publish", "Topic"),
    ("kafka-go", "ReaderConfig"): ("consume", "Topic"),
    ("sarama", "ProducerMessage"): ("publish", "Topic"),
    ("confluent-kafka-go", "TopicPartition"): ("publish", "Topic"),
}


def go_broker_file(content):
    """Whether a Go source imports one of the broker clients read here."""
    return any(path.encode() in content for path in GO_BROKER_IMPORTS)


def extract_go(sites, facts):
    """AMQP and Kafka sites of one Go file from its ``go_facts``: (calls,
    literals, reason) where reason says why the file was not, or only partly,
    type-checked (None when it was)."""
    calls, literals, reason = facts
    if reason is not None:
        sites.unread.append(MessageUnread(sites.file, 1, "go_types", reason))
    nodes = {
        (node.start_point.row + 1, node.start_point.column): node
        for node in _walk(sites.root)
        if node.type in ("call_expression", "composite_literal")
    }
    at = {(c.line, c.column): c for c in calls}

    def value(item):
        if item is None:
            return PLACEHOLDER
        if item.text is not None:
            return item.text
        if item.result is not None:
            # q.Name after q, err := ch.QueueDeclare(name, ...): the name;
            # for "" the broker names the queue, so it is not known.
            line, column, index, field = item.result
            call = at.get((line, column))
            if (
                call is not None
                and call.declared in _GO_AMQP
                and call.method == "QueueDeclare"
                and (index, field) == (0, "Name")
                and call.values
                and call.values[0].text
            ):
                return call.values[0].text
        return PLACEHOLDER

    def declaration(item):
        """The QueueDeclare result field a queue argument reads, or None."""
        if item is None or item.text is not None or item.result is None:
            return None
        line, column, _, _ = item.result
        call = at.get((line, column))
        if call is None or call.declared not in _GO_AMQP:
            return None
        return item.result if call.method == "QueueDeclare" else None

    def argument(call, index):
        return call.values[index] if index < len(call.values) else None

    def strings(item):
        return [None] if item is None or item.strings is None else item.strings

    def text_of(item):
        return PLACEHOLDER if item is None else item

    sited = []
    for call in calls:
        node = nodes.get((call.line, call.column))
        if node is not None:
            sited.append((node, call))
    bindings = [
        (
            value(argument(call, 0)),
            value(argument(call, 1)),
            value(argument(call, 2)),
            declaration(argument(call, 0)),
        )
        for _, call in sited
        if call.declared in _GO_AMQP and call.method == "QueueBind"
    ]
    for node, call in sited:
        method = call.method
        if call.declared in _GO_AMQP:
            if method in _GO_AMQP_PUBLISH:
                offset = 1 if method.endswith("WithContext") else 0
                exchange = value(argument(call, offset))
                key = value(argument(call, offset + 1))
                sites.add(node, "publish", "amqp", f"{exchange}/{key}", "go-amqp")
            elif method in ("Consume", "ConsumeWithContext"):
                offset = 1 if method == "ConsumeWithContext" else 0
                queue = argument(call, offset)
                sites.consume_queue(
                    node,
                    value(queue),
                    bindings,
                    "go-amqp",
                    identity=declaration(queue),
                )
            continue
        client = _go_kafka_client(call.declared)
        if client == "sarama":
            if method == "ConsumePartition":
                topic = value(argument(call, 0))
                sites.add(node, "consume", "kafka", topic, "sarama")
            elif method == "Consume" and call.receiver is not None:
                if call.receiver[1] != "ConsumerGroup":
                    continue
                for item in strings(argument(call, 1)):
                    sites.add(node, "consume", "kafka", text_of(item), "sarama")
        elif client == "confluent-kafka-go" and method in (
            "Subscribe",
            "SubscribeTopics",
        ):
            names = (
                [value(argument(call, 0))]
                if method == "Subscribe"
                else [text_of(item) for item in strings(argument(call, 0))]
            )
            for name in names:
                # A leading ^ subscribes to a regular expression.
                if name.startswith("^"):
                    sites.unread.append(
                        MessageUnread(sites.file, call.line, "consume", "topic_pattern")
                    )
                    continue
                sites.add(node, "consume", "kafka", name, "confluent-kafka-go")
    for literal in literals:
        node = nodes.get((literal.line, literal.column))
        client = _go_kafka_client(literal.type[0])
        rule = _GO_KAFKA_LITERALS.get((client, literal.type[1]))
        if node is None or rule is None:
            continue
        role, field = rule
        elements = dict(literal.elements)
        if client == "kafka-go" and role == "consume" and "GroupTopics" in elements:
            for item in strings(elements["GroupTopics"]):
                sites.add(node, role, "kafka", text_of(item), client)
        # A kafka-go Message without Topic goes to its Writer's topic.
        if field in elements:
            sites.add(node, role, "kafka", value(elements[field]), client)


# -- JavaScript / TypeScript -------------------------------------------------


def _bound_kinds(root, content, classify):
    """{name: kind} for the names (``x`` or ``this.x``) that every assignment
    in the file binds to a value ``classify`` gives the same kind."""
    found = {}
    for node in _walk(root):
        if node.type == "variable_declarator":
            name, value = (
                node.child_by_field_name("name"),
                node.child_by_field_name("value"),
            )
            written = text(name, content) if name is not None else None
        elif node.type == "assignment_expression":
            name, value = (
                node.child_by_field_name("left"),
                node.child_by_field_name("right"),
            )
            written = text(name, content) if name is not None else None
        elif node.type in ("public_field_definition", "field_definition"):
            name = node.child_by_field_name("name") or node.child_by_field_name(
                "property"
            )
            value = node.child_by_field_name("value")
            written = "this." + text(name, content) if name is not None else None
        else:
            continue
        if written is None or value is None:
            continue
        kind = classify(value)
        found.setdefault(written, set()).add(kind)
    return {
        name: next(iter(kinds))
        for name, kinds in found.items()
        if len(kinds) == 1 and None not in kinds
    }


def _constructed(value, content, modules, exports):
    """Whether ``value`` is ``new C(...)`` of a class one of ``exports``
    ({module: class}) names, imported under any local name."""
    if value.type != "new_expression":
        return False
    constructor = text(value.child_by_field_name("constructor"), content)
    owner, _, member = constructor.rpartition(".")
    return any(
        modules.get(constructor) == f"{module}.{name}"
        or (member == name and modules.get(owner) == module)
        for module, name in exports
    )


_PUBSUB_CLASSES = (
    ("graphql-subscriptions", "PubSub"),
    ("apollo-server", "PubSub"),
    ("apollo-server-express", "PubSub"),
    ("graphql-yoga", "PubSub"),
    ("@nestjs/graphql", "PubSub"),
)
_REDIS_PUBSUB = (("graphql-redis-subscriptions", "RedisPubSub"),)


def _kafkajs_values(messages, content):
    """(direction, start, end, None) of what each kafkajs message's ``value``
    serializes: the argument of ``JSON.stringify(x)``, written as the value
    or inside ``Buffer.from(...)``. The TypeScript compiler gives the types
    of the value at that span (ts_facts)."""
    found = []
    items = (
        [] if messages is None or messages.type != "array" else messages.named_children
    )
    for item in items:
        value = _js_object(item, content).get("value")
        while value is not None and value.type == "call_expression":
            function = value.child_by_field_name("function")
            arguments = [
                a
                for a in value.child_by_field_name("arguments").named_children
                if a.type != "comment"
            ]
            called = text(function, content)
            if called == "JSON.stringify" and arguments:
                found.append(
                    ("request", arguments[0].start_byte, arguments[0].end_byte, None)
                )
                break
            if called == "Buffer.from" and arguments:
                value = arguments[0]
                continue
            break
    return tuple(found)


_KAFKAJS = (("kafkajs", "Kafka"),)
_AMQPLIB = ("amqplib", "amqplib/callback_api")
_CHANNEL_FACTORIES = ("createChannel", "createConfirmChannel")


def _awaited(node):
    """``node`` without ``await`` and parentheses."""
    while node is not None and node.type in (
        "await_expression",
        "parenthesized_expression",
    ):
        children = node.named_children
        node = children[-1] if children else None
    return node


def _member_call(node, content):
    """(receiver text, method) of ``a.b(...)``, else None."""
    node = _awaited(node)
    if node is None or node.type != "call_expression":
        return None
    function = node.child_by_field_name("function")
    if function is None or function.type != "member_expression":
        return None
    return (
        text(function.child_by_field_name("object"), content),
        text(function.child_by_field_name("property"), content),
    )


def _callback_parameters(node, content):
    """Parameter names of a function or arrow function, in order."""
    if node is None or node.type not in (
        "function_expression",
        "function",
        "arrow_function",
    ):
        return []
    single = node.child_by_field_name("parameter")
    if single is not None:
        return [text(single, content)]
    parameters = node.child_by_field_name("parameters")
    if parameters is None:
        return []
    names = []
    for child in parameters.named_children:
        pattern = child.child_by_field_name("pattern") or child
        names.append(text(pattern, content) if pattern.type == "identifier" else None)
    return names


def _amqplib_channels(root, content):
    """Names bound to an amqplib channel: assigned ``createChannel()`` (or
    ``createConfirmChannel``) everywhere in the file, or the channel parameter
    of its callback (callback API) or of its ``.then`` (promise API)."""

    def classify(value):
        found = _member_call(value, content)
        return "channel" if found and found[1] in _CHANNEL_FACTORIES else None

    names = set(_bound_kinds(root, content, classify))
    for node in _walk(root):
        if node.type != "call_expression":
            continue
        found = _member_call(node, content)
        if found is None:
            continue
        arguments = node.child_by_field_name("arguments")
        callbacks = [c for c in arguments.named_children if c.type != "comment"]
        if found[1] in _CHANNEL_FACTORIES and callbacks:
            parameters = _callback_parameters(callbacks[-1], content)
            index = 1  # (err, channel)
        elif found[1] == "then" and callbacks:
            receiver = node.child_by_field_name("function").child_by_field_name(
                "object"
            )
            inner = _member_call(receiver, content)
            if inner is None or inner[1] not in _CHANNEL_FACTORIES:
                continue
            parameters = _callback_parameters(callbacks[0], content)
            index = 0
        else:
            continue
        if len(parameters) > index and parameters[index]:
            names.add(parameters[index])
    return names


def _required_members(root, content):
    """{local name: "module.Export"} of ``const {Export} = require("module")``
    (and ``{Export: local}``)."""
    names = {}
    for node in _walk(root):
        if node.type != "variable_declarator":
            continue
        pattern, value = (
            node.child_by_field_name("name"),
            node.child_by_field_name("value"),
        )
        if pattern is None or pattern.type != "object_pattern" or value is None:
            continue
        if value.type != "call_expression" or (
            text(value.child_by_field_name("function"), content) != "require"
        ):
            continue
        arguments = value.child_by_field_name("arguments").named_children
        if not arguments or arguments[0].type != "string":
            continue
        module = text(arguments[0], content)[1:-1]
        for item in pattern.named_children:
            if item.type == "shorthand_property_identifier_pattern":
                export = local = text(item, content)
            elif item.type == "pair_pattern":
                export = text(item.child_by_field_name("key"), content)
                target = item.child_by_field_name("value")
                if target is None or target.type != "identifier":
                    continue
                local = text(target, content)
            else:
                continue
            names[local] = f"{module}.{export}"
    return names


def _evaluator(node):
    """A destination computed from one expression (None: unknown)."""
    return lambda folder: PLACEHOLDER if node is None else folder.evaluate(node)


def extract_javascript_brokers(sites, language):
    """kafkajs producers and consumers, and amqplib channels. A topic that
    is a parameter of the enclosing module-level function is read from the
    calls of that function (``sites.calls_of``)."""
    content, root = sites.content, sites.root
    modules = {**_required_members(root, content), **_js_modules(root, content)}
    kafkajs = "kafkajs" in {m.split(".")[0] for m in modules.values() if m}
    amqplib = any(m in _AMQPLIB for m in modules.values())
    if not kafkajs and not amqplib:
        return
    folder = Folder(language, root, content)
    clients = {}
    if kafkajs:
        # Names bound to, or typed as, a kafkajs Kafka.
        kafkas = set(
            bound_kinds(
                root,
                content,
                modules,
                lambda v: (
                    "kafka"
                    if _constructed(_awaited(v), content, modules, _KAFKAJS)
                    else None
                ),
                {("kafkajs", "Kafka"): "kafka"},
            )
        )

        def classify(value):
            value = _awaited(value)
            found = _member_call(value, content)
            if found is None or found[1] not in ("producer", "consumer"):
                return None
            receiver = value.child_by_field_name("function").child_by_field_name(
                "object"
            )
            if found[0] in kafkas or _constructed(
                _awaited(receiver), content, modules, _KAFKAJS
            ):
                return found[1]
            return None

        clients.update(_bound_kinds(root, content, classify))
    channels = _amqplib_channels(root, content) if amqplib else set()
    calls = []
    for node in _walk(root):
        if node.type != "call_expression":
            continue
        found = _member_call(node, content)
        if found is None:
            continue
        positional = [
            c
            for c in node.child_by_field_name("arguments").named_children
            if c.type != "comment"
        ]
        calls.append((node, found[0], found[1], positional))
    bindings = []
    for node, owner, verb, positional in calls:
        if owner in channels and verb == "bindQueue" and len(positional) >= 3:
            queue, exchange, key = (folder.evaluate(a) for a in positional[:3])
            bindings.append((queue, key, exchange))
    for node, owner, verb, positional in calls:
        kind = clients.get(owner)
        if kind == "producer" and verb in ("send", "sendBatch") and positional:
            options = _js_object(positional[0], content)
            if verb == "send":
                sent = [(options.get("topic"), options.get("messages"))]
            else:
                batch = options.get("topicMessages")
                if batch is None or batch.type != "array":
                    sent = [(None, None)]
                else:
                    sent = [
                        (
                            _js_object(item, content).get("topic"),
                            _js_object(item, content).get("messages"),
                        )
                        for item in batch.named_children
                        if item.type != "comment"
                    ]
            for topic, messages in sent:
                sites.add_folded(
                    node,
                    "publish",
                    "kafka",
                    _evaluator(topic),
                    "kafkajs",
                    folder,
                    payloads=_kafkajs_values(messages, content),
                )
        elif kind == "consumer" and verb == "subscribe" and positional:
            options = _js_object(positional[0], content)
            if "topic" in options:
                topics = [options["topic"]]
            elif "topics" in options and options["topics"].type == "array":
                topics = [
                    c for c in options["topics"].named_children if c.type != "comment"
                ]
            else:
                topics = [None]
            for topic in topics:
                if topic is not None and topic.type == "regex":
                    sites.unread.append(
                        MessageUnread(
                            sites.file,
                            node.start_point.row + 1,
                            "consume",
                            "topic_pattern",
                        )
                    )
                    continue
                sites.add_folded(
                    node, "consume", "kafka", _evaluator(topic), "kafkajs", folder
                )
        elif owner in channels:
            if verb == "publish" and len(positional) >= 2:
                exchange, key = positional[:2]
                sites.add_folded(
                    node,
                    "publish",
                    "amqp",
                    lambda f, e=exchange, k=key: f"{f.evaluate(e)}/{f.evaluate(k)}",
                    "amqplib",
                    folder,
                )
            elif verb == "sendToQueue" and positional:
                sites.add_folded(
                    node,
                    "publish",
                    "amqp",
                    lambda f, q=positional[0]: f"/{f.evaluate(q)}",
                    "amqplib",
                    folder,
                )
            elif verb == "consume" and positional:
                queue = folder.evaluate(positional[0])
                sites.consume_queue(node, queue, bindings, "amqplib")


_NEST_MICROSERVICES = "@nestjs/microservices"
_NEST_CLIENTS = {
    "ClientProxy",
    "ClientKafka",
    "ClientRMQ",
    "ClientNats",
    "ClientRedis",
    "ClientMqtt",
    "ClientTCP",
}


def _const_objects(root, content):
    """{name: object literal} of the names declared once in the file, by
    ``const``, with an object literal."""
    found = {}
    for node in _walk(root):
        if node.type != "variable_declarator":
            continue
        name = node.child_by_field_name("name")
        if name is None or name.type != "identifier":
            continue
        parent = node.parent
        constant = (
            parent is not None
            and parent.type == "lexical_declaration"
            and parent.children
            and text(parent.children[0], content) == "const"
        )
        # Any other declaration of the name (a let, a var) is ambiguous.
        value = node.child_by_field_name("value") if constant else None
        found.setdefault(text(name, content), []).append(value)
    return {
        name: values[0]
        for name, values in found.items()
        if len(values) == 1 and values[0] is not None and values[0].type == "object"
    }


def _nest_escape(value):
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _nest_keys_ordered(keys):
    """Whether sorting ``keys`` by code point gives the order Nest gives them
    (``localeCompare``). That holds for keys of ASCII letters and digits whose
    order does not depend on case; other keys are not read."""
    if not all(re.fullmatch(r"[A-Za-z0-9]+", k) for k in keys):
        return False
    return sorted(keys) == sorted(keys, key=lambda k: (k.lower(), k.swapcase()))


def _nest_route(node, folder, content, objects=None):
    """A NestJS pattern as the route Nest matches it by
    (``transformPatternToRoute``): a string or number as written, an object
    as ``{"key":value,...}`` with sorted keys and quoted string values.
    ``objects`` are the file's ``_const_objects``."""
    if node is None:
        return PLACEHOLDER
    if node.type == "identifier" and objects and text(node, content) in objects:
        node = objects[text(node, content)]
    if node.type == "number":
        written = text(node, content)
        return written if re.fullmatch(r"-?\d+", written) else PLACEHOLDER
    if node.type in ("true", "false", "null"):
        return node.type
    if node.type == "object":
        parts = []
        for pair in node.named_children:
            if pair.type == "comment":
                continue
            if pair.type != "pair":
                return PLACEHOLDER
            key = folder.evaluate(pair.child_by_field_name("key"))
            if pair.child_by_field_name("key").type == "property_identifier":
                key = text(pair.child_by_field_name("key"), content)
            value = pair.child_by_field_name("value")
            if value.type in ("string", "template_string"):
                route = f'"{_nest_escape(folder.evaluate(value))}"'
            else:
                route = _nest_route(value, folder, content, objects)
            parts.append((key, route))
        keys = [k for k, _ in parts]
        if len(set(keys)) != len(keys) or not _nest_keys_ordered(keys):
            return PLACEHOLDER
        order = sorted(keys)
        routes = dict(parts)
        return "{" + ",".join(f'"{_nest_escape(k)}":{routes[k]}' for k in order) + "}"
    return folder.evaluate(node)


def _imported_from(name, modules, module):
    return modules.get(name, "") == f"{module}.{name}" or modules.get(
        name, ""
    ).startswith(f"{module}.")


def _nest_clients(root, content, modules):
    """Names (``this.x`` or ``x``) declared in the file only with a NestJS
    client type (``ClientProxy`` and its transport subclasses)."""
    found = {}
    for node in _walk(root):
        if node.type in ("public_field_definition", "field_definition"):
            name = node.child_by_field_name("name")
            written = "this." + text(name, content) if name is not None else None
        elif node.type in ("required_parameter", "optional_parameter"):
            pattern = node.child_by_field_name("pattern")
            if pattern is None or pattern.type != "identifier":
                continue
            # A parameter property (private x: T) is also this.x.
            modifier = any(
                c.type in ("accessibility_modifier", "readonly")
                or text(c, content) == "readonly"
                for c in node.children
            )
            written = ("this." if modifier else "") + text(pattern, content)
        elif node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            written = text(name, content) if name is not None else None
        else:
            continue
        annotation = node.child_by_field_name("type")
        if written is None or annotation is None:
            continue
        kind = annotation.named_children[0] if annotation.named_children else None
        typed = text(kind, content) if kind is not None else ""
        found.setdefault(written, set()).add(
            typed in _NEST_CLIENTS
            and _imported_from(typed, modules, _NEST_MICROSERVICES)
        )
    return {name for name, kinds in found.items() if kinds == {True}}


def _nest_message_payloads(method, modules, content, kind):
    """(direction, start, end, None) of the types a Nest pattern handler writes
    for the message (its parameter decorated ``@Payload()``, else its first
    parameter when that has no decorator, as Nest passes the data first)
    and, for a ``@MessagePattern``, its reply (the return type)."""
    found = []
    parameters = method.child_by_field_name("parameters")
    written = [] if parameters is None else parameters.named_children

    def decorators(parameter):
        return [
            d.named_children[0].child_by_field_name("function")
            for d in parameter.named_children
            if d.type == "decorator"
            and d.named_children
            and d.named_children[0].type == "call_expression"
        ]

    data = next(
        (
            p
            for p in written
            if any(
                f is not None
                and f.type == "identifier"
                and text(f, content) == "Payload"
                and _imported_from("Payload", modules, _NEST_MICROSERVICES)
                for f in decorators(p)
            )
        ),
        None,
    )
    if (
        data is None
        and written
        and not any(d.type == "decorator" for d in written[0].named_children)
    ):
        data = written[0]
    span = None if data is None else _type_span(data.child_by_field_name("type"))
    if span is not None:
        found.append(("request", *span, None))
    if kind == "message":
        span = _type_span(method.child_by_field_name("return_type"))
        if span is not None:
            found.append(("response", *span, None))
    return tuple(found)


def extract_nest_microservices(sites, language):
    """NestJS microservice patterns: ``@MessagePattern``/``@EventPattern``
    handlers of controllers consume, and a client's ``send``/``emit``
    publish. A message and an event of the same pattern are different
    destinations, as Nest matches them separately."""
    content, root = sites.content, sites.root
    modules = _js_modules(root, content)
    if not any(m and m.startswith(_NEST_MICROSERVICES) for m in modules.values()):
        return
    folder = Folder(language, root, content)
    objects = _const_objects(root, content)

    def decorator_call(node):
        call = node.named_children[0] if node.named_children else None
        if call is None or call.type != "call_expression":
            return None, []
        function = call.child_by_field_name("function")
        if function is None or function.type != "identifier":
            return None, []
        arguments = call.child_by_field_name("arguments")
        found = [] if arguments is None else arguments.named_children
        return text(function, content), [c for c in found if c.type != "comment"]

    for node in _walk(root):
        if node.type != "class_declaration":
            continue
        decorators = [c for c in node.named_children if c.type == "decorator"]
        if node.parent is not None and node.parent.type == "export_statement":
            decorators += [
                c for c in node.parent.named_children if c.type == "decorator"
            ]
        if not any(
            decorator_call(d)[0] == "Controller"
            and _imported_from("Controller", modules, "@nestjs/common")
            for d in decorators
        ):
            continue
        body = node.child_by_field_name("body")
        pending = []
        for member in [] if body is None else body.named_children:
            if member.type == "decorator":
                pending.append(member)
                continue
            if member.type != "method_definition":
                pending = []
                continue
            decorators, pending = pending, []
            for item in decorators:
                name, arguments = decorator_call(item)
                if name not in ("MessagePattern", "EventPattern") or not (
                    _imported_from(name, modules, _NEST_MICROSERVICES)
                ):
                    continue
                kind = "message" if name == "MessagePattern" else "event"
                payloads = _nest_message_payloads(member, modules, content, kind)
                route = _nest_route(
                    arguments[0] if arguments else None, folder, content, objects
                )
                sites.add(
                    item,
                    "consume",
                    "nestjs",
                    f"{kind}:{route}",
                    "nestjs-microservices",
                    sites.declaration(member),
                    payloads=payloads,
                )
    clients = _nest_clients(root, content, modules)
    if not clients:
        return
    for node in _walk(root):
        if node.type != "call_expression":
            continue
        found = _member_call(node, content)
        if (
            found is None
            or found[0] not in clients
            or found[1]
            not in (
                "send",
                "emit",
            )
        ):
            continue
        arguments = [
            c
            for c in node.child_by_field_name("arguments").named_children
            if c.type != "comment"
        ]
        if not arguments:
            continue
        kind = "message" if found[1] == "send" else "event"
        # The data the client sends; its types come from the TypeScript
        # compiler, by the span (ts_facts).
        data = arguments[1] if len(arguments) > 1 else None
        sites.add_folded(
            node,
            "publish",
            "nestjs",
            lambda f, a=arguments[0], k=kind: (
                f"{k}:{_nest_route(a, f, content, objects)}"
            ),
            "nestjs-microservices",
            folder,
            payloads=(
                ()
                if data is None
                else (("request", data.start_byte, data.end_byte, None),)
            ),
        )


_STOMP_VERBS = {
    # kind: {verb: (role, destination argument is an options object)}
    "stompjs": {
        "publish": ("publish", True),
        "send": ("publish", False),
        "subscribe": ("consume", False),
    },
    "rx-stomp": {"publish": ("publish", True), "watch": ("consume", None)},
    "ng2-stomp-service": {"send": ("publish", False), "subscribe": ("consume", False)},
}


def extract_javascript(sites, language):
    content, root = sites.content, sites.root
    modules = _js_modules(root, content)

    def classify(value):
        stomp = value_kind(value, content, modules)
        if stomp is not None:
            return stomp
        if _constructed(value, content, modules, _PUBSUB_CLASSES):
            return "graphql-pubsub"
        if _constructed(value, content, modules, _REDIS_PUBSUB):
            return "redis"
        return None

    names = bound_kinds(root, content, modules, classify)
    # AngularJS ngStomp's injected service, read as $http is in web.py.
    if b"$stomp" in content:
        names["$stomp"] = "stompjs"
    if not names:
        return
    folder = Folder(language, root, content)
    for node in _walk(root):
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if function.type != "member_expression" or arguments is None:
            continue
        owner = text(function.child_by_field_name("object"), content)
        kind = names.get(owner)
        if kind is None:
            continue
        verb = text(function.child_by_field_name("property"), content)
        positional = [c for c in arguments.named_children if c.type != "comment"]
        if not positional:
            continue
        if kind in _STOMP_VERBS:
            if verb not in _STOMP_VERBS[kind]:
                continue
            role, options = _STOMP_VERBS[kind][verb]
            target = positional[0]
            if options or (options is None and target.type == "object"):
                found = _js_object(target, content)
                if "destination" not in found:
                    continue
                target = found["destination"]
            sites.add_folded(node, role, "stomp", _evaluator(target), kind, folder)
            continue
        if verb == "publish":
            role, topics = "publish", [positional[0]]
        elif verb in ("asyncIterator", "asyncIterableIterator", "subscribe"):
            first = positional[0]
            role = "consume"
            topics = (
                [c for c in first.named_children if c.type != "comment"]
                if first.type == "array"
                else [first]
            )
        else:
            continue
        for topic in topics:
            name = folder.evaluate(topic)
            if kind == "redis":
                sites.add(node, role, "redis", name, "graphql-redis-subscriptions")
            elif PLACEHOLDER in name:
                sites.add(node, role, kind, name, "graphql-subscriptions")
            else:
                # An in-memory PubSub delivers only to its own instance.
                sites.add(
                    node,
                    role,
                    kind,
                    f"{sites.file}#{owner}/{name}",
                    "graphql-subscriptions",
                )


def extract_messaging(
    tree,
    content,
    repository_id,
    file,
    definitions,
    language,
    *,
    constants=None,
    template_exchange=None,
    application_prefixes=None,
    calls_of=None,
    go_facts=None,
):
    """Publish and consume sites, and unread ones, of one source file.

    ``template_exchange`` is the exchange a Spring RabbitTemplate of the
    file's module sends to when given only a routing key, None if unknown.
    ``calls_of(name)`` gives the calls of the file's module-level function
    ``name`` and their arguments (JavaScript/TypeScript), for destinations
    built from its parameters. ``go_facts`` is (calls, literals, reason) of
    a Go file that imports a broker client (``extract_go``).
    """
    sites = _Sites(tree, content, repository_id, file, definitions)
    sites.calls_of = calls_of
    if language == "java":
        extract_java(sites, constants, template_exchange, application_prefixes)
    elif language == "python":
        extract_python(sites, _python_imports(sites.root, content))
    elif language == "go":
        if go_facts is not None:
            extract_go(sites, go_facts)
    elif language in ("javascript", "typescript"):
        extract_javascript(sites, language)
        extract_javascript_brokers(sites, language)
        extract_nest_microservices(sites, language)
    return sites.endpoints, sites.unread
