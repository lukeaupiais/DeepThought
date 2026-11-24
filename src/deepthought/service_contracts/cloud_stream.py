"""Spring Cloud Stream bindings and the destinations each service binds them to.

Code names a binding; configuration names its destination. Sites read in
Java:

- consume: a method carrying ``@StreamListener`` whose value or ``target``
  names a binding (``Sink.INPUT`` and ``Processor.INPUT`` are ``input``);
- publish: ``send(...)`` on ``x.output()`` where ``x`` is declared only as
  ``Source`` or ``Processor``, or on a name assigned only such a call, a
  ``@SendTo`` on a listener, and ``StreamBridge.send("binding", ...)``.

A binding is bound in each service that runs the code: the file's own build
module when it names a service, else every module of the repository whose
Maven ``pom.xml`` depends on that module and names a service. There its
destination is ``spring.cloud.stream.bindings.<binding>.destination`` from
the service's configuration, or the binding name when that key is not set.
The binder is Kafka or RabbitMQ by the ``spring-cloud-stream-binder-*``
dependency of the service module or of a repository module it depends on;
with neither or both the site is unread. A Kafka destination is a topic; a
RabbitMQ binder publishes to a topic exchange named by the destination and
binds each consumer group's queue with ``#``, so the destination is
``<destination>/#``.
"""

from dataclasses import dataclass
import re
import xml.etree.ElementTree as ElementTree

from .folding import text
from .http import PLACEHOLDER
from .messaging import MessageUnread, _typed_names, _walk
from .spring import _Imports, _annotations, _arguments, _Values

_FRAMEWORK_CONSTANTS = {
    "Sink.INPUT": "input",
    "Processor.INPUT": "input",
    "Source.OUTPUT": "output",
    "Processor.OUTPUT": "output",
}
_STREAM = ("org.springframework.cloud.stream.annotation",)
_MESSAGING = ("org.springframework.messaging.handler.annotation",)


@dataclass(frozen=True)
class StreamSite:
    file: str
    line: int
    column: int
    declaration_id: str | None
    role: str  # publish | consume
    binding: str


def _binding(values, node, content):
    if node is None:
        return PLACEHOLDER
    written = text(node, content)
    if written in _FRAMEWORK_CONSTANTS:
        return _FRAMEWORK_CONSTANTS[written]
    return values.evaluate(node)


def extract_stream(tree, content, file, definitions, constants=None):
    """Binding sites and unread ones of one Java file."""
    root = tree.root_node
    imports = _Imports(root, content)
    values = _Values(root, content, constants, imports)
    mine = [d for d in definitions if d.file == file]
    spans = {(d.line, d.column, d.end_line, d.end_column): d.id for d in mine}
    enclosing = sorted(
        (((d.line, d.column), (d.end_line, d.end_column), d.id) for d in mine),
        reverse=True,
    )

    def around(node):
        start = (node.start_point.row + 1, node.start_point.column)
        end = (node.end_point.row + 1, node.end_point.column)
        return next((i for s, e, i in enclosing if s <= start and end <= e), None)

    sites, unread = [], []

    def add(node, role, binding, declaration_id):
        line = node.start_point.row + 1
        if PLACEHOLDER in binding:
            unread.append(MessageUnread(file, line, role, "binding_not_literal"))
            return
        sites.append(
            StreamSite(
                file, line, node.start_point.column, declaration_id, role, binding
            )
        )

    channels = _typed_names(root, content, imports, {"Source", "Processor"})
    bridges = _typed_names(root, content, imports, {"StreamBridge"})

    def output_call(node):
        """``x.output()`` on a Source/Processor name."""
        return (
            node is not None
            and node.type == "method_invocation"
            and text(node.child_by_field_name("name"), content) == "output"
            and node.child_by_field_name("object") is not None
            and text(node.child_by_field_name("object"), content).removeprefix("this.")
            in channels
        )

    # Names whose every value is x.output(): field initializers and assignments.
    assigned = {}
    for node in _walk(root):
        if node.type == "variable_declarator" and node.child_by_field_name("value"):
            name = text(node.child_by_field_name("name"), content)
            assigned.setdefault(name, []).append(node.child_by_field_name("value"))
        elif node.type == "assignment_expression":
            left = text(node.child_by_field_name("left"), content).removeprefix("this.")
            assigned.setdefault(left, []).append(node.child_by_field_name("right"))
    output_names = {
        name for name, found in assigned.items() if all(output_call(v) for v in found)
    }

    for node in _walk(root):
        if node.type == "method_declaration":
            handler = spans.get(
                (
                    node.start_point.row + 1,
                    node.start_point.column,
                    node.end_point.row + 1,
                    node.end_point.column,
                )
            )
            annotations = _annotations(node, content)
            listens = False
            for written, annotation in annotations:
                if imports.spring_name(written, _STREAM) == "StreamListener":
                    arguments = _arguments(annotation)
                    target = arguments.get("target", arguments.get("value"))
                    if target is None:
                        # @StreamListener without a target binds by @Input
                        # parameters, which are not read.
                        unread.append(
                            MessageUnread(
                                file, node.start_point.row + 1, "consume", "no_target"
                            )
                        )
                        continue
                    listens = True
                    add(
                        annotation,
                        "consume",
                        _binding(values, target, content),
                        handler,
                    )
            for written, annotation in annotations:
                if listens and imports.spring_name(written, _MESSAGING) == "SendTo":
                    target = _arguments(annotation).get("value")
                    add(
                        annotation,
                        "publish",
                        _binding(values, target, content),
                        handler,
                    )
        elif node.type == "method_invocation":
            if text(node.child_by_field_name("name"), content) != "send":
                continue
            receiver = node.child_by_field_name("object")
            if receiver is None:
                continue
            owner = text(receiver, content).removeprefix("this.")
            arguments = [
                c
                for c in node.child_by_field_name("arguments").named_children
                if c.type not in ("line_comment", "block_comment")
            ]
            if output_call(receiver) or (
                receiver.type in ("identifier", "field_access")
                and owner in output_names
            ):
                add(node, "publish", "output", around(node))
            elif owner in bridges and len(arguments) >= 2:
                add(node, "publish", values.evaluate(arguments[0]), around(node))
    return sites, unread


def _local(tag):
    return tag.rsplit("}", 1)[-1]


class MavenModules:
    """Repository modules by Maven artifactId, their in-repository
    dependencies and the Spring Cloud Stream binders they declare."""

    def __init__(self, root):
        self.root = root
        self.artifacts = {}  # artifactId -> module directory
        self.dependencies = {}  # module directory -> artifactIds
        for path in sorted(root.rglob("pom.xml")):
            relative = path.relative_to(root)
            if any(
                p.startswith(".") or p in ("node_modules", "target")
                for p in relative.parts
            ):
                continue
            try:
                project = ElementTree.parse(path).getroot()
            except (ElementTree.ParseError, OSError):
                continue
            module = relative.parent.as_posix()
            artifact = next(
                (
                    c.text.strip()
                    for c in project
                    if _local(c.tag) == "artifactId" and c.text
                ),
                None,
            )
            dependencies = []
            for element in project.iter():
                if _local(element.tag) != "dependency":
                    continue
                for child in element:
                    if _local(child.tag) == "artifactId" and child.text:
                        dependencies.append(child.text.strip())
            if artifact is not None:
                self.artifacts.setdefault(artifact, []).append(module)
            self.dependencies[module] = dependencies

    def dependents(self, module):
        """Modules that declare a dependency on ``module``'s artifact."""
        artifacts = {a for a, modules in self.artifacts.items() if module in modules}
        return sorted(
            m for m, found in self.dependencies.items() if artifacts & set(found)
        )

    def binder(self, module):
        """kafka, amqp or None, from the module and its repository dependencies."""
        found = set()
        modules = [module] + [
            m
            for artifact in self.dependencies.get(module, [])
            for m in self.artifacts.get(artifact, [])
        ]
        for current in modules:
            for artifact in self.dependencies.get(current, []):
                if re.fullmatch(
                    r"spring-cloud-stream-binder-kafka(-streams)?", artifact
                ):
                    found.add("kafka")
                elif artifact == "spring-cloud-stream-binder-rabbit":
                    found.add("amqp")
                elif artifact == "spring-cloud-starter-stream-kafka":
                    found.add("kafka")
                elif artifact == "spring-cloud-starter-stream-rabbit":
                    found.add("amqp")
        return next(iter(found)) if len(found) == 1 else None
