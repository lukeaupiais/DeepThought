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
AngularJS ngStomp ``$stomp``)
``send``/``publish`` and ``subscribe``. The broker is ``stomp``.

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
from .spring import _Imports, _annotations, _arguments, _Values
from .web import _go_imports, _js_modules, _js_object, _python_imports


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

    def declaration(self, node, exact=False):
        start = (node.start_point.row + 1, node.start_point.column)
        end = (node.end_point.row + 1, node.end_point.column)
        if exact:
            return self.spans.get((*start, *end))
        return next((i for s, e, i in self.enclosing if s <= start and end <= e), None)

    def add(self, node, role, broker, destination, client, declaration_id=None):
        line = node.start_point.row + 1
        if PLACEHOLDER in destination:
            self.unread.append(
                MessageUnread(self.file, line, role, "destination_not_literal")
            )
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
            )
        )

    def consume_queue(self, node, queue, bindings, client, declaration_id=None):
        """A consumer of ``queue``: the default exchange plus its bindings."""
        if PLACEHOLDER in queue:
            self.add(node, "consume", "amqp", queue, client, declaration_id)
            return
        self.add(node, "consume", "amqp", "/" + queue, client, declaration_id)
        for bound_queue, key, exchange in bindings:
            if bound_queue != queue:
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

_GO_AMQP = ("github.com/streadway/amqp", "github.com/rabbitmq/amqp091-go")


def extract_go(sites, imports):
    content, root = sites.content, sites.root
    if not any(path in _GO_AMQP for path in imports.values()):
        return
    folder = Folder("go", root, content)
    declared_queues = {}  # name -> queue name, from q, err := ch.QueueDeclare("n", ...)
    for node in _walk(root):
        if node.type not in ("short_var_declaration", "assignment_statement"):
            continue
        left = node.child_by_field_name("left").named_children
        right = node.child_by_field_name("right").named_children
        if len(right) == 1 and right[0].type == "call_expression" and left:
            function = right[0].child_by_field_name("function")
            if (
                function.type == "selector_expression"
                and text(function.child_by_field_name("field"), content)
                == "QueueDeclare"
            ):
                arguments = right[0].child_by_field_name("arguments").named_children
                name = text(left[0], content)
                queue = folder.evaluate(arguments[0]) if arguments else PLACEHOLDER
                declared_queues.setdefault(name, []).append(queue)

    def queue_name(node):
        if (
            node.type == "selector_expression"
            and text(node.child_by_field_name("field"), content) == "Name"
        ):
            found = declared_queues.get(
                text(node.child_by_field_name("operand"), content)
            )
            return found[0] if found and len(found) == 1 else PLACEHOLDER
        return folder.evaluate(node)

    calls = []
    bindings = []
    for node in _walk(root):
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        if function.type != "selector_expression":
            continue
        method = text(function.child_by_field_name("field"), content)
        arguments = node.child_by_field_name("arguments").named_children
        calls.append((node, method, arguments))
        if method == "QueueBind" and len(arguments) >= 3:
            bindings.append(
                (
                    queue_name(arguments[0]),
                    folder.evaluate(arguments[1]),
                    folder.evaluate(arguments[2]),
                )
            )
    for node, method, arguments in calls:
        if method in ("Publish", "PublishWithContext"):
            offset = 1 if method == "PublishWithContext" else 0
            if len(arguments) < offset + 2:
                continue
            exchange = folder.evaluate(arguments[offset])
            key = folder.evaluate(arguments[offset + 1])
            sites.add(node, "publish", "amqp", f"{exchange}/{key}", "go-amqp")
        elif method in ("Consume", "ConsumeWithContext") and arguments:
            offset = 1 if method == "ConsumeWithContext" else 0
            if len(arguments) <= offset:
                continue
            sites.consume_queue(
                node, queue_name(arguments[offset]), bindings, "go-amqp"
            )


# -- JavaScript / TypeScript -------------------------------------------------

_STOMP_FACTORIES = {"Stomp.over", "Stomp.client", "webstomp.over", "webstomp.client"}


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


def extract_javascript(sites, language):
    content, root = sites.content, sites.root
    modules = _js_modules(root, content)

    def classify(value):
        if value.type == "call_expression":
            called = text(value.child_by_field_name("function"), content)
            return "stomp" if called in _STOMP_FACTORIES else None
        if _constructed(value, content, modules, (("@stomp/stompjs", "Client"),)):
            return "stomp"
        if _constructed(value, content, modules, _PUBSUB_CLASSES):
            return "graphql-pubsub"
        if _constructed(value, content, modules, _REDIS_PUBSUB):
            return "redis"
        return None

    names = _bound_kinds(root, content, classify)
    # AngularJS ngStomp's injected service, read as $http is in web.py.
    if b"$stomp" in content:
        names["$stomp"] = "stomp"
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
        if kind == "stomp":
            if verb == "publish":
                options = _js_object(positional[0], content)
                if "destination" not in options:
                    continue
                destination = folder.evaluate(options["destination"])
            elif verb in ("send", "subscribe"):
                destination = folder.evaluate(positional[0])
            else:
                continue
            role = "consume" if verb == "subscribe" else "publish"
            sites.add(node, role, "stomp", destination, "stompjs")
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
):
    """Publish and consume sites, and unread ones, of one source file.

    ``template_exchange`` is the exchange a Spring RabbitTemplate of the
    file's module sends to when given only a routing key, None if unknown.
    """
    sites = _Sites(tree, content, repository_id, file, definitions)
    if language == "java":
        extract_java(sites, constants, template_exchange, application_prefixes)
    elif language == "python":
        extract_python(sites, _python_imports(sites.root, content))
    elif language == "go":
        extract_go(sites, _go_imports(sites.root, content))
    elif language in ("javascript", "typescript"):
        extract_javascript(sites, language)
    return sites.endpoints, sites.unread
