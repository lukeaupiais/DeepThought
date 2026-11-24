"""Spring Cloud Stream bindings and the destinations each service binds them to.

Code names a binding; configuration names its destination. Sites read in
Java:

- consume: a method carrying ``@StreamListener`` whose value or ``target``
  names a binding (``Sink.INPUT`` and ``Processor.INPUT`` are ``input``);
- publish: ``send(...)`` on ``x.output()`` where ``x`` is declared only as
  ``Source`` or ``Processor``, or on a name assigned only such a call, a
  ``@SendTo`` on a listener, and ``StreamBridge.send("binding", ...)``;
- functions: a ``@Bean`` method returning a ``java.util.function``
  ``Consumer`` or ``BiConsumer`` (consumes ``<bean>-in-0``, and
  ``<bean>-in-1``), ``Supplier`` (publishes ``<bean>-out-0``), or
  ``Function`` or ``BiFunction`` (both). A function returning a ``KStream[]``
  (Kafka Streams branches) or another function (currying) is unread. The bean is bound where the
  service's ``spring.cloud.function.definition`` lists it, or, without
  that key, when it is the service's only such bean; a binding renamed by
  ``spring.cloud.stream.function.bindings.<binding>`` takes that name.

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


# (inputs, outputs) of each functional type.
_FUNCTIONS = {
    "Consumer": (1, 0),
    "BiConsumer": (2, 0),
    "Supplier": (0, 1),
    "Function": (1, 1),
    "BiFunction": (2, 1),
}
_CONTEXT = ("org.springframework.context.annotation",)


@dataclass(frozen=True)
class StreamSite:
    file: str
    line: int
    column: int
    declaration_id: str | None
    role: str  # publish | consume
    binding: str
    # The bean of a functional binding (a Consumer, Function or Supplier
    # @Bean): bound only where the service's function definition names it.
    function: str | None = None


def _binding(values, node, content):
    if node is None:
        return PLACEHOLDER
    written = text(node, content)
    if written in _FRAMEWORK_CONSTANTS:
        return _FRAMEWORK_CONSTANTS[written]
    return values.evaluate(node)


def _function_type(node, content, imports):
    """(name, type arguments) when ``node`` is one of the java.util.function
    types of ``_FUNCTIONS`` with type arguments, else None."""
    if node is None or node.type != "generic_type":
        return None
    base = node.named_children[0] if node.named_children else None
    arguments = next(
        (c for c in node.named_children if c.type == "type_arguments"), None
    )
    if base is None or arguments is None:
        return None
    written = text(base, content)
    name = written.rpartition(".")[2]
    if name not in _FUNCTIONS:
        return None
    if imports.spring_name(written, ("java.util.function",)) != name:
        return None
    return name, [c for c in arguments.named_children if c.type != "block_comment"]


def _parameter_bindings(root, content, values, node, depth=0):
    """The literal values a parameter takes, when ``node`` names a parameter
    of a private method that never assigns it and that the file only calls
    directly (``m(...)`` or ``this.m(...)``, with as many arguments as it
    declares): the argument of each call, followed the same way through
    the caller's own parameters, at most three methods deep. None when any
    value is not literal or the method is used otherwise."""
    if node is None or node.type != "identifier" or depth > 2:
        return None
    name = text(node, content)
    method = node.parent
    while method is not None and method.type not in (
        "method_declaration",
        "constructor_declaration",
        "lambda_expression",
        "class_body",
    ):
        method = method.parent
    if method is None or method.type != "method_declaration":
        return None
    modifiers = next((c for c in method.children if c.type == "modifiers"), None)
    if modifiers is None or "private" not in text(modifiers, content).split():
        return None
    parameters = method.child_by_field_name("parameters")
    declared = [c for c in parameters.named_children if c.type == "formal_parameter"]
    if len(declared) != len(parameters.named_children):
        return None  # varargs or receiver parameters
    names = [text(p.child_by_field_name("name"), content) for p in declared]
    if names.count(name) != 1:
        return None
    index = names.index(name)
    body = method.child_by_field_name("body")
    for inner in _walk(body) if body is not None else []:
        if inner.type in ("assignment_expression", "update_expression"):
            target = inner.child_by_field_name("left") or inner.named_children[0]
            if text(target, content).removeprefix("this.") == name:
                return None
        if inner.type == "local_variable_declaration" and any(
            text(d.child_by_field_name("name"), content) == name
            for d in inner.children_by_field_name("declarator")
        ):
            return None
    method_name = text(method.child_by_field_name("name"), content)
    found = []
    for call in _walk(root):
        if call.type == "method_reference" and text(call, content).endswith(
            "::" + method_name
        ):
            return None
        if call.type != "method_invocation":
            continue
        if text(call.child_by_field_name("name"), content) != method_name:
            continue
        receiver = call.child_by_field_name("object")
        if receiver is not None and text(receiver, content) != "this":
            return None
        arguments = [
            c
            for c in call.child_by_field_name("arguments").named_children
            if c.type not in ("line_comment", "block_comment")
        ]
        if len(arguments) != len(names):
            return None  # an overload: which method is called is not read
        argument = arguments[index]
        written = values.alternatives(argument)
        if any(PLACEHOLDER in w for w in written):
            written = _parameter_bindings(root, content, values, argument, depth + 1)
            if written is None:
                return None
        found.extend(w for w in written if w not in found)
    return found or None


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
            for written, annotation in annotations:
                if imports.spring_name(written, _CONTEXT) != "Bean":
                    continue
                found = _function_type(
                    node.child_by_field_name("type"), content, imports
                )
                if found is None:
                    break
                kind, types = found
                inputs, outputs = _FUNCTIONS[kind]
                line = annotation.start_point.row + 1
                role = "consume" if inputs else "publish"
                if outputs and _function_type(types[-1], content, imports):
                    # A curried function: its inputs are not these alone.
                    unread.append(MessageUnread(file, line, role, "function_curried"))
                    break
                arguments = _arguments(annotation)
                named = arguments.get("name", arguments.get("value"))
                if named is None:
                    bean = text(node.child_by_field_name("name"), content)
                elif named.type == "string_literal":
                    bean = values.evaluate(named)
                else:
                    unread.append(MessageUnread(file, line, role, "bean_name"))
                    break
                bindings = [("consume", f"{bean}-in-{i}") for i in range(inputs)]
                if (
                    outputs
                    and types[-1].type == "array_type"
                    and text(types[-1], content).startswith("KStream")
                ):
                    # Kafka Streams branches: one output binding per element.
                    unread.append(
                        MessageUnread(file, line, "publish", "function_outputs")
                    )
                elif outputs:
                    bindings.append(("publish", f"{bean}-out-0"))
                for role, binding in bindings:
                    sites.append(
                        StreamSite(
                            file,
                            line,
                            annotation.start_point.column,
                            handler,
                            role,
                            binding,
                            bean,
                        )
                    )
                break
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
                binding = values.evaluate(arguments[0])
                through = (
                    _parameter_bindings(root, content, values, arguments[0])
                    if PLACEHOLDER in binding
                    else None
                )
                for written in through or [binding]:
                    add(node, "publish", written, around(node))
    return sites, unread


def _local(tag):
    return tag.rsplit("}", 1)[-1]


class MavenModules:
    """Repository modules by Maven artifactId, their in-repository
    dependencies and the Spring Cloud Stream binders they declare. Gradle
    modules (a ``build.gradle(.kts)`` without a ``pom.xml``) are read too:
    their quoted ``group:artifact`` coordinates and ``project(':path')``
    dependencies, resolved from the nearest ``settings.gradle``."""

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
        self.unresolved = set()  # Gradle modules with a project() not found
        for name in ("build.gradle", "build.gradle.kts"):
            for path in sorted(root.rglob(name)):
                relative = path.relative_to(root)
                module = relative.parent.as_posix()
                if module in self.dependencies or any(
                    p.startswith(".") or p in ("node_modules", "build")
                    for p in relative.parts
                ):
                    continue
                self._gradle(path, module)

    def _gradle(self, path, module):
        """A Gradle module's external artifacts and project() dependencies,
        named ``gradle:<module>``; a project() whose directory has no build
        file marks the module unresolved."""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        text = re.sub(r"/\*[\s\S]*?\*/|//[^\n]*", "", text)
        dependencies = [
            m.group(2)
            for m in re.finditer(
                r"""["']([\w.\-]+):([\w.\-]+)(?::[^"'\s]*)?["']""", text
            )
        ]
        dependencies += [
            m.group(1)
            for m in re.finditer(r"""\bname\s*[:=]\s*["']([\w.\-]+)["']""", text)
        ]
        settings = path.parent
        while not any(
            (settings / f).is_file() for f in ("settings.gradle", "settings.gradle.kts")
        ):
            if settings == self.root or settings == settings.parent:
                settings = None
                break
            settings = settings.parent
        for match in re.finditer(
            r"""project\s*\(\s*(?:path\s*[:=]\s*)?["'](:[\w.:\-]*)["']""", text
        ):
            base = settings if settings is not None else self.root
            target = base / match.group(1).strip(":").replace(":", "/")
            if not any(
                (target / f).is_file() for f in ("build.gradle", "build.gradle.kts")
            ):
                self.unresolved.add(module)
                continue
            dependencies.append(f"gradle:{target.relative_to(self.root).as_posix()}")
        self.artifacts.setdefault(f"gradle:{module}", []).append(module)
        self.dependencies[module] = dependencies

    def dependents(self, module):
        """Modules that declare a dependency on ``module``'s artifact."""
        artifacts = {a for a, modules in self.artifacts.items() if module in modules}
        return sorted(
            m for m, found in self.dependencies.items() if artifacts & set(found)
        )

    def binder(self, module):
        """kafka, amqp or None, from the module and its repository dependencies."""
        found = self.binders(module)
        return next(iter(found)) if len(found) == 1 else None

    def binders(self, module):
        """The binders (kafka, amqp) of the module and its dependencies."""
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
        return found
