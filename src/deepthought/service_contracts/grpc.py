"""gRPC contracts: services declared in .proto files, their servers and stubs.

A method's identity is its fully qualified protobuf name
(``package.Service/Method``), which is what travels on the wire, so copies of
one .proto file in several services declare one method. Code refers to a
service by its simple name (``NewCartServiceClient``, ``CartServiceStub``,
``CartServiceGrpc``); a simple name declared by more than one package is
ambiguous and links nothing.
"""

from dataclasses import dataclass
from fnmatch import fnmatchcase
from hashlib import sha256
import os
from pathlib import Path
import re

from ..syntax import identifier, make_parser

# Generated code declares every service's stub and base class; it is not a
# server or a client of its own.
GENERATED_SUFFIXES = (
    ".pb.go",
    "_grpc.pb.go",
    "_pb2.py",
    "_pb2_grpc.py",
    "_pb.js",
    "_grpc_pb.js",
    "_pb.d.ts",
    "_grpc_pb.d.ts",
)


@dataclass(frozen=True)
class ProtoMethod:
    """An ``rpc`` of a ``service`` in a .proto file."""

    service: str  # fully qualified: package.Service
    method: str
    file: str
    line: int
    # unary | client | server | bidi, or None when the signature is not read.
    streaming: str | None = None

    @property
    def full_name(self):
        return f"{self.service}/{self.method}"


@dataclass(frozen=True)
class GrpcHandler:
    """A declaration that serves one rpc, and the registration that says so."""

    file: str
    line: int
    handler_id: str
    service: str  # simple service name as written
    method: str  # rpc name as written by the server
    rule: str


@dataclass(frozen=True)
class GrpcCall:
    """A call of an rpc on a stub, as written."""

    id: str
    file: str
    line: int
    column: int
    caller_id: str | None
    service: str  # simple service name of the stub
    method: str  # method name as written


_SIGNATURE = re.compile(
    r"\brpc\s+\w+\s*\(\s*(stream\s+)?\.?[\w.]+\s*\)"
    r"\s*returns\s*\(\s*(stream\s+)?\.?[\w.]+\s*\)"
)
_STREAMING = {
    (False, False): "unary",
    (True, False): "client",
    (False, True): "server",
    (True, True): "bidi",
}
_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_STRINGS = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'')


def read_proto(text, file):
    """(package, [ProtoMethod]) of a .proto file's text.

    Comments are removed (keeping line numbers); ``service`` blocks are read
    at the top level only; ``rpc Name (...) returns (...)`` statements inside
    them give methods, with ``stream`` on either side read as the streaming
    kind. Options, messages and imports are not needed.
    """

    def blank(match):
        return re.sub(r"[^\n]", " ", match.group(0))

    # Keep strings intact while removing comments, so "//" inside a string
    # is not a comment.
    cleaned, last = [], 0
    for match in re.finditer(
        _COMMENTS.pattern + "|" + _STRINGS.pattern, text, flags=re.S
    ):
        cleaned.append(text[last : match.start()])
        token = match.group(0)
        cleaned.append(token if token[0] in "\"'" else blank(match))
        last = match.end()
    cleaned.append(text[last:])
    text = "".join(cleaned)
    package = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", text)
    package = package.group(1) if package else ""
    methods = []
    for service in re.finditer(r"\bservice\s+(\w+)\s*\{", text):
        depth, index = 1, service.end()
        while index < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[index], 0)
            index += 1
        body_start = service.end()
        body = text[body_start : index - 1]
        name = f"{package}.{service.group(1)}" if package else service.group(1)
        for rpc in re.finditer(r"\brpc\s+(\w+)\s*\(", body):
            line = text.count("\n", 0, body_start + rpc.start(1)) + 1
            signature = _SIGNATURE.match(body, rpc.start())
            streaming = (
                _STREAMING[bool(signature.group(1)), bool(signature.group(2))]
                if signature
                else None
            )
            methods.append(ProtoMethod(name, rpc.group(1), file, line, streaming))
    return package, methods


def lower_camel(name):
    return name[:1].lower() + name[1:]


class ProtoIndex:
    """Proto services by simple name, and their rpcs."""

    def __init__(self, methods):
        self.methods = {}
        for method in sorted(methods, key=lambda m: (m.file, m.line)):
            self.methods.setdefault((method.service, method.method), []).append(method)
        self.by_simple = {}
        for service, _ in self.methods:
            self.by_simple.setdefault(service.rpartition(".")[2], set()).add(service)

    def service(self, simple):
        """(full name, None) or (None, reason)."""
        found = self.by_simple.get(simple)
        if not found:
            return None, "no_proto_service"
        if len(found) > 1:
            return None, "several_proto_packages"
        return next(iter(found)), None

    def method(self, service, written):
        """The rpc name a written method name denotes, or None.

        Go and Python stubs and servers use the rpc name; Java and
        JavaScript use it with a lowercase first letter.
        """
        names = {m for s, m in self.methods if s == service}
        if written in names:
            return written
        matches = [m for m in names if lower_camel(m) == written]
        return matches[0] if len(matches) == 1 else None


# --- language tables -------------------------------------------------------

_CALL = {
    "go": ("call_expression", "function", "selector_expression", "operand", "field"),
    "python": ("call", "function", "attribute", "object", "attribute"),
    "java": ("method_invocation", None, None, "object", "name"),
    "javascript": (
        "call_expression",
        "function",
        "member_expression",
        "object",
        "property",
    ),
    "typescript": (
        "call_expression",
        "function",
        "member_expression",
        "object",
        "property",
    ),
}
_LAST_NAME_TYPES = {
    "identifier",
    "field_identifier",
    "property_identifier",
}


def _text(node, content):
    return content[node.start_byte : node.end_byte].decode("utf-8")


def _method_call(node, language, content):
    """(receiver node, method name) of a method call node, or None."""
    call_type, function_field, member_type, object_field, name_field = _CALL[language]
    if node.type != call_type:
        return None
    if language == "java":
        receiver = node.child_by_field_name(object_field)
        name = node.child_by_field_name(name_field)
    else:
        function = node.child_by_field_name(function_field)
        if function is None or function.type != member_type:
            return None
        receiver = function.child_by_field_name(object_field)
        name = function.child_by_field_name(name_field)
    if receiver is None or name is None:
        return None
    return receiver, _text(name, content)


def _name_node(node, language):
    """The method name node of a method call node."""
    _, function_field, _, _, name_field = _CALL[language]
    if language == "java":
        return node.child_by_field_name(name_field)
    function = node.child_by_field_name(function_field)
    return None if function is None else function.child_by_field_name(name_field)


def _callee_name(node, language, content):
    """The last name of a plain or member call's callee (``pb.NewXClient``)."""
    call_type = _CALL[language][0]
    if node.type != call_type:
        return None
    if language == "java":
        name = node.child_by_field_name("name")
        return None if name is None else _text(name, content)
    function = node.child_by_field_name("function")
    if function is None:
        return None
    if function.type == "identifier":
        return _text(function, content)
    found = _method_call(node, language, content)
    return None if found is None else found[1]


def _stub_service(node, language, content):
    """The simple service name if ``node`` constructs a stub, else None."""
    if language in ("javascript", "typescript"):
        if node.type != "new_expression":
            return None
        constructor = node.child_by_field_name("constructor")
        if constructor is None or constructor.type != "member_expression":
            return None
        return _text(constructor.child_by_field_name("property"), content)
    name = _callee_name(node, language, content)
    if name is None:
        return None
    if language == "go":
        match = re.fullmatch(r"New(\w+)Client", name)
    elif language == "python":
        match = re.fullmatch(r"(\w+)Stub", name)
    else:  # java: XGrpc.newStub / newBlockingStub / newFutureStub
        target = node.child_by_field_name("object")
        if (
            name not in ("newStub", "newBlockingStub", "newFutureStub")
            or target is None
        ):
            return None
        match = re.fullmatch(r"(?:.*\.)?(\w+)Grpc", _text(target, content))
    return match.group(1) if match else None


def _last_name(node, content):
    """The last name of an identifier or member access (``fe.cartClient``)."""
    if node.type in _LAST_NAME_TYPES:
        return _text(node, content)
    for field in ("field", "attribute", "property", "name"):
        child = node.child_by_field_name(field)
        if child is not None and child.type in _LAST_NAME_TYPES:
            return _text(child, content)
    return None


def _bindings(root, language, content):
    """(name, value node) pairs of assignments and declarations in a file."""
    pairs = []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        kind = node.type
        if kind in ("short_var_declaration", "assignment_statement"):  # go
            left = node.child_by_field_name("left").named_children
            right = node.child_by_field_name("right").named_children
            if len(left) == len(right):
                pairs.extend(zip(left, right))
        elif kind == "var_spec":
            names = node.children_by_field_name("name")
            value = node.child_by_field_name("value")
            if value is not None and len(names) == len(value.named_children):
                pairs.extend(zip(names, value.named_children))
        elif kind in ("assignment", "assignment_expression"):  # python, java, js
            left, right = (
                node.child_by_field_name("left"),
                node.child_by_field_name("right"),
            )
            if left is not None and right is not None:
                pairs.append((left, right))
        elif kind == "variable_declarator":  # java, js
            name, value = (
                node.child_by_field_name("name"),
                node.child_by_field_name("value"),
            )
            if name is not None and value is not None:
                pairs.append((name, value))
    return [
        (name, value)
        for target, value in pairs
        if (name := _last_name(target, content)) is not None
    ]


def _unwrap(node):
    """Go's ``&T{}``; parentheses."""
    while node.type in ("unary_expression", "parenthesized_expression") and (
        node.named_children
    ):
        node = node.named_children[-1]
    return node


def _enclosing(definitions, file):
    spans = sorted(
        (
            ((d.line, d.column), (d.end_line, d.end_column), d.id)
            for d in definitions
            if d.file == file
        ),
        reverse=True,
    )

    def find(node):
        start = (node.start_point.row + 1, node.start_point.column)
        end = (node.end_point.row + 1, node.end_point.column)
        return next((i for s, e, i in spans if s <= start and end <= e), None)

    return find


def extract_calls(
    tree, content, repository_id, file, language, definitions, services=None
):
    """Stub calls of one file.

    Go, Python and Java stubs are named by their generated factories. A
    JavaScript stub is any ``new x.S(...)``, so there ``S`` must also be one
    of ``services`` (simple proto service names) when given.
    """
    root = tree.root_node

    def stub(node):
        service = _stub_service(_unwrap(node), language, content)
        if (
            service is not None
            and services is not None
            and language in ("javascript", "typescript")
            and service not in services
        ):
            return None
        return service

    bound = {}
    for name, value in _bindings(root, language, content):
        bound.setdefault(name, set()).add(stub(value))
    # A name bound to stubs of one service only, and to nothing else.
    stubs = {
        name: next(iter(services))
        for name, services in bound.items()
        if len(services) == 1 and None not in services
    }
    find = _enclosing(definitions, file)
    calls, ordinals = [], {}
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.named_children))
        found = _method_call(node, language, content)
        if found is None:
            continue
        receiver, method = found
        # A chained call (``NewXClient(c).\n Method(...)``) is placed at the
        # method's name.
        at = _name_node(node, language) or node
        service = stub(receiver)
        if service is None:
            name = _last_name(receiver, content)
            service = stubs.get(name) if name is not None else None
        if service is None:
            continue
        key = f"{service}.{method}"
        ordinal = ordinals.get(key, 0)
        ordinals[key] = ordinal + 1
        calls.append(
            GrpcCall(
                id=identifier(repository_id, file, key, "GrpcCall", ordinal),
                file=file,
                line=at.start_point.row + 1,
                column=at.start_point.column,
                caller_id=find(node),
                service=service,
                method=method,
            )
        )
    return calls


GO_REGISTER = "service-contracts.grpc-go-register"
GO_REGISTER_CONSTRUCTOR = "service-contracts.grpc-go-register-constructor"
PYTHON_SERVICER = "service-contracts.grpc-python-servicer"
JAVA_IMPL_BASE = "service-contracts.grpc-java-impl-base"
NODE_ADD_SERVICE = "service-contracts.grpc-node-add-service"
STUB_CALL = "service-contracts.grpc-stub-call"


def go_constructors(tree, content):
    """{function: T} of the top-level functions of a Go file whose only
    result is ``T`` or ``*T`` for a type ``T`` of the package."""
    found = {}
    for node in tree.root_node.named_children:
        if node.type != "function_declaration":
            continue
        result = node.child_by_field_name("result")
        if result is not None and result.type == "pointer_type":
            result = result.named_children[-1] if result.named_children else None
        if result is not None and result.type == "type_identifier":
            found[_text(node.child_by_field_name("name"), content)] = _text(
                result, content
            )
    return found


def go_struct_types(tree, content):
    """Names of the struct types a Go file declares at the top level."""
    names = set()
    for node in tree.root_node.named_children:
        if node.type != "type_declaration":
            continue
        for spec in node.named_children:
            value = spec.child_by_field_name("type")
            if spec.type == "type_spec" and value is not None:
                if value.type == "struct_type":
                    names.add(_text(spec.child_by_field_name("name"), content))
    return names


def go_registrations(tree, content, constructors=None):
    """(service, implementation type, rule, line) of
    ``pb.RegisterXServer(s, impl)``.

    ``constructors`` maps the functions of the package to the type they
    return (None when several files declare the name), so that
    ``RegisterXServer(s, newServer())`` names ``newServer``'s type.
    """
    constructors = constructors or {}
    root = tree.root_node
    values = {}
    for name, value in _bindings(root, "go", content):
        values.setdefault(name, []).append(_unwrap(value))
    found = []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        name = _callee_name(node, "go", content)
        match = re.fullmatch(r"Register(\w+)Server", name or "")
        if not match:
            continue
        arguments = node.child_by_field_name("arguments").named_children
        if len(arguments) != 2:
            continue
        implementation = _unwrap(arguments[1])
        if implementation.type == "identifier":
            assigned = values.get(_text(implementation, content), [])
            implementation = assigned[0] if len(assigned) == 1 else None
        type_name, rule = None, GO_REGISTER
        if implementation is not None and implementation.type == "composite_literal":
            type_name = _text(implementation.child_by_field_name("type"), content)
        elif implementation is not None and implementation.type == "call_expression":
            function = implementation.child_by_field_name("function")
            called = _text(function, content)
            if called == "new":
                args = implementation.child_by_field_name("arguments").named_children
                type_name = _text(args[0], content) if len(args) == 1 else None
            elif function.type == "identifier" and called in constructors:
                type_name, rule = constructors[called], GO_REGISTER_CONSTRUCTOR
        found.append((match.group(1), type_name, rule, node.start_point.row + 1))
    return found


def class_bases(tree, content, language):
    """{class name: (class node, [base names as written])} of a file."""
    classes = {}
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if language == "python" and node.type == "class_definition":
            bases = node.child_by_field_name("superclasses")
            written = (
                []
                if bases is None
                else [
                    _text(b, content)
                    for b in bases.named_children
                    if b.type in ("identifier", "attribute")
                ]
            )
        elif language == "java" and node.type == "class_declaration":
            superclass = node.child_by_field_name("superclass")
            written = (
                []
                if superclass is None
                else [_text(c, content) for c in superclass.named_children]
            )
        else:
            continue
        name = _text(node.child_by_field_name("name"), content)
        classes.setdefault(name, []).append((node, written))
    return {n: v[0] for n, v in classes.items() if len(v) == 1}


def node_add_service(tree, content, language):
    """(service, rpc key, handler symbol, line) of ``x.addService(S.service, {...})``."""
    found = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        call = _method_call(node, language, content)
        if call is None or call[1] != "addService":
            continue
        arguments = node.child_by_field_name("arguments").named_children
        if len(arguments) != 2 or arguments[1].type != "object":
            continue
        service_node = arguments[0]
        if (
            service_node.type != "member_expression"
            or _text(service_node.child_by_field_name("property"), content) != "service"
        ):
            continue
        owner = service_node.child_by_field_name("object")
        service = _last_name(owner, content)
        if service is None:
            continue
        for entry in arguments[1].named_children:
            if entry.type == "shorthand_property_identifier":
                key = symbol = _text(entry, content)
            elif entry.type == "pair":
                key_node = entry.child_by_field_name("key")
                key = _text(key_node, content).strip("'\"")
                value = entry.child_by_field_name("value")
                # H.C.bind(this) names H.C.
                if value.type == "call_expression":
                    call_value = _method_call(value, language, content)
                    if call_value is not None and call_value[1] == "bind":
                        value = call_value[0]
                if value.type == "identifier":
                    symbol = _text(value, content)
                elif value.type == "member_expression":
                    symbol = _text(value, content)
                    symbol = None if symbol.startswith("this.") else symbol
                else:
                    symbol = None  # an inline function has no declaration
            else:
                continue
            found.append((service, key, symbol, entry.start_point.row + 1))
    return found


# --- servers ----------------------------------------------------------------


def _class_definition(definitions, file, node):
    """The Class definition a class node declares, or None."""
    line = node.start_point.row + 1
    found = [
        d
        for d in definitions
        if d.file == file and d.kind == "Class" and d.line == line
    ]
    return found[0] if len(found) == 1 else None


def _servicer_families(classes, pattern):
    """{class name: service} of classes whose base matches ``pattern``, and of
    their subclasses in the same file named by a bare base name."""
    families = {}
    for name, (_, bases) in classes.items():
        services = {
            m.group(1) for b in bases if (m := re.fullmatch(pattern, b)) is not None
        }
        if len(services) == 1:
            families[name] = next(iter(services))
    changed = True
    while changed:
        changed = False
        for name, (_, bases) in classes.items():
            if name in families:
                continue
            inherited = {families[b] for b in bases if b in families}
            if len(inherited) == 1:
                families[name] = next(iter(inherited))
                changed = True
    return families


def server_handlers(
    tree, content, file, language, definitions, index, constructors=None
):
    """(handlers, unread) of one file.

    Handlers name the fully qualified service and the rpc; unread are
    (line, reason) of servers seen but not tied to a proto method.
    ``constructors`` are Go's, by package directory (see go_constructors).
    """
    handlers, unread = [], []

    def rpcs(simple, line):
        service, reason = index.service(simple)
        if service is None:
            unread.append((line, reason))
            return None, []
        return service, sorted({m for s, m in index.methods if s == service})

    if language == "go":
        directory = file.rpartition("/")[0]
        local = [
            d
            for d in definitions
            if d.kind == "Method"
            and d.file.rpartition("/")[0] == directory
            and not is_test(d.file)
        ]
        package = (constructors or {}).get(directory, {})
        for simple, type_name, rule, line in go_registrations(tree, content, package):
            if type_name is None:
                unread.append((line, "implementation_not_read"))
                continue
            service, names = rpcs(simple, line)
            for rpc in names:
                found = [d for d in local if d.symbol == f"{type_name}.{rpc}"]
                if len(found) == 1:
                    handlers.append(
                        GrpcHandler(
                            found[0].file,
                            found[0].line,
                            found[0].id,
                            service,
                            rpc,
                            rule,
                        )
                    )
    elif language in ("python", "java"):
        classes = class_bases(tree, content, language)
        if language == "python":
            families = _servicer_families(classes, r"(?:.*\.)?(\w+)Servicer")
            spelled, rule = str, PYTHON_SERVICER
        else:
            families = _servicer_families(classes, r"(?:.*\.)?(\w+)ImplBase")
            spelled, rule = lower_camel, JAVA_IMPL_BASE
        own = [d for d in definitions if d.file == file and d.kind == "Method"]
        for name, simple in sorted(families.items()):
            node = classes[name][0]
            owner = _class_definition(definitions, file, node)
            if owner is None:
                continue
            service, names = rpcs(simple, node.start_point.row + 1)
            for rpc in names:
                symbol = f"{owner.symbol}.{spelled(rpc)}"
                handlers.extend(
                    GrpcHandler(d.file, d.line, d.id, service, rpc, rule)
                    for d in own
                    if d.symbol == symbol
                )
    elif language in ("javascript", "typescript"):
        own = [d for d in definitions if d.file == file]
        for simple, key, symbol, line in node_add_service(tree, content, language):
            service, _ = rpcs(simple, line)
            if service is None:
                continue
            rpc = index.method(service, key)
            if rpc is None:
                unread.append((line, "no_proto_method"))
                continue
            found = [d for d in own if symbol is not None and d.symbol == symbol]
            if len(found) != 1:
                unread.append((line, "handler_not_declared"))
                continue
            handlers.append(
                GrpcHandler(
                    found[0].file,
                    found[0].line,
                    found[0].id,
                    service,
                    rpc,
                    NODE_ADD_SERVICE,
                )
            )
    return handlers, unread


# --- the whole repository ----------------------------------------------------


@dataclass(frozen=True)
class CallResolution:
    """What one stub call denotes: a proto method, or why none."""

    call_id: str
    status: str  # linked | unresolved
    reason: str | None = None
    service: str | None = None  # fully qualified
    method: str | None = None


@dataclass
class GrpcFacts:
    proto_files: dict  # path -> sha256
    index: ProtoIndex
    handlers: list
    calls: list
    resolutions: list
    unread: list  # (file, line, kind, reason)


_SKIPPED_DIRECTORIES = {".git", ".venv", "node_modules", "__pycache__", "vendor"}
_SUFFIXES = {
    "go": ".go",
    "python": ".py",
    "java": ".java",
    "javascript": ".js",
    "typescript": ".ts",
}


def proto_files(root, include=("*",), exclude=()):
    """Relative paths of the .proto files under ``root``, sorted."""
    found = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(
            d for d in dirs if d not in _SKIPPED_DIRECTORIES and not d.startswith(".")
        )
        for name in files:
            if not name.endswith(".proto"):
                continue
            relative = (Path(directory) / name).relative_to(root).as_posix()
            if any(fnmatchcase(relative, p) for p in include) and not any(
                fnmatchcase(relative, p) for p in exclude
            ):
                found.append(relative)
    return sorted(found)


def is_generated(path):
    return path.endswith(GENERATED_SUFFIXES)


def is_test(path):
    """A Go or Python test file (test source sets are skipped by the tool)."""
    name = path.rpartition("/")[2]
    return name.endswith("_test.go") or (
        name.startswith("test_") and name.endswith(".py")
    )


def resolve_call(call, index):
    service, reason = index.service(call.service)
    if service is None:
        return CallResolution(call.id, "unresolved", reason)
    method = index.method(service, call.method)
    if method is None:
        return CallResolution(call.id, "unresolved", "no_proto_method", service)
    return CallResolution(call.id, "linked", None, service, method)


def collect(root, sources, definitions, repository_id, protos):
    """gRPC facts of a repository.

    ``sources`` are (path, language, content) of analyzed files whose digest
    matched; ``protos`` are the .proto paths to read.
    """
    methods, digests = [], {}
    for path in protos:
        data = (root / path).read_bytes()
        digests[path] = sha256(data).hexdigest()
        methods.extend(read_proto(data.decode("utf-8", "replace"), path)[1])
    index = ProtoIndex(methods)
    handlers, calls, unread, parsers = set(), [], [], {}
    parsed = []
    for path, language, content in sources:
        if language not in _SUFFIXES or is_generated(path) or is_test(path):
            continue
        suffix = ".tsx" if path.endswith(".tsx") else _SUFFIXES[language]
        if suffix not in parsers:
            parsers[suffix] = make_parser(language, suffix)
        parsed.append((path, language, content, parsers[suffix].parse(content)))
    # Go constructors by package directory; a name that several files
    # declare (as build-tagged variants do) names no type.
    constructors, structs = {}, {}
    for path, language, content, tree in parsed:
        if language != "go":
            continue
        directory = path.rpartition("/")[0]
        structs.setdefault(directory, set()).update(go_struct_types(tree, content))
        package = constructors.setdefault(directory, {})
        for name, type_name in go_constructors(tree, content).items():
            package[name] = type_name if name not in package else None
    # Only a struct of the package serves; an interface's methods do not.
    for directory, package in constructors.items():
        for name, type_name in package.items():
            if type_name not in structs[directory]:
                package[name] = None
    for path, language, content, tree in parsed:
        found, skipped = server_handlers(
            tree, content, path, language, definitions, index, constructors
        )
        handlers.update(found)
        unread.extend((path, line, "server", reason) for line, reason in skipped)
        calls.extend(
            extract_calls(
                tree,
                content,
                repository_id,
                path,
                language,
                definitions,
                services=set(index.by_simple),
            )
        )
    calls.sort(key=lambda c: (c.file, c.line, c.column, c.id))
    return GrpcFacts(
        proto_files=digests,
        index=index,
        handlers=sorted(
            handlers, key=lambda h: (h.service, h.method, h.file, h.line, h.handler_id)
        ),
        calls=calls,
        resolutions=[resolve_call(c, index) for c in calls],
        unread=sorted(unread),
    )
