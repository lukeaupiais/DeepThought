"""Read .proto files with protoc, the Protocol Buffers compiler.

grpcio-tools ships protoc and the well-known types. Each file is compiled
to a FileDescriptorSet with source info, and its services, rpcs, streaming
kinds and the lines of the rpc names come from the descriptor, so the
compiler, not a pattern, decides what the file declares. So do its message
and enum types: each message's fields (number, scalar or the message or
enum type it holds, repeated/optional, map key, oneof, JSON name) and each
enum's values, nested ones included.

protoc needs the files a .proto imports. The import paths are relative to
an include root, which the repository does not name; a root is found by
matching each import against the repository's .proto paths (the root is
what is left of the path in front of the import). A file whose own path
some other file imports takes that importer's root, else its directory.

googleapis-common-protos, when installed, supplies
``google/api/annotations.proto`` and ``google/api/http.proto`` to files that
import them without vendoring them. An rpc's ``google.api.http`` option (the
HTTP mapping that grpc-gateway and Kratos serve) is read from the
descriptor as ``HttpBinding`` records, with its ``additional_bindings``.

A file protoc cannot compile (an import that is not in the repository or
the include directories, or a syntax error) has no descriptor here; the
caller counts it as unread. Nothing else reads .proto files.
"""

from dataclasses import dataclass
import os
from pathlib import PurePosixPath
import re
import subprocess
import sys
import tempfile

from .grpc import ProtoMethod

# FileDescriptorProto.service, ServiceDescriptorProto.method,
# MethodDescriptorProto.name: the source-info path of an rpc's name.
_SERVICE, _METHOD, _NAME = 6, 2, 1
_STREAMING = {
    (False, False): "unary",
    (True, False): "client",
    (False, True): "server",
    (True, True): "bidi",
}
# FileDescriptorProto.message_type / enum_type, DescriptorProto.field /
# nested_type / enum_type, EnumDescriptorProto.value: source-info path
# components.
_MESSAGE, _ENUM = 4, 5
_FIELD, _NESTED, _NESTED_ENUM, _VALUE = 2, 3, 4, 2
# FieldDescriptorProto.Type numbers to their .proto names.
_SCALARS = {
    1: "double",
    2: "float",
    3: "int64",
    4: "uint64",
    5: "int32",
    6: "fixed64",
    7: "fixed32",
    8: "bool",
    9: "string",
    12: "bytes",
    13: "uint32",
    15: "sfixed32",
    16: "sfixed64",
    17: "sint32",
    18: "sint64",
}
_GROUP, _MESSAGE_TYPE, _ENUM_TYPE = 10, 11, 14
_IMPORT = re.compile(r'(?m)^\s*import\s+(?:public\s+|weak\s+)?"([^"]+)"\s*;')
_TIMEOUT = 60
_VERBS = ("get", "put", "post", "delete", "patch")


@dataclass(frozen=True)
class HttpBinding:
    """An HTTP mapping of an rpc, from its ``google.api.http`` option."""

    service: str  # fully qualified: package.Service
    method: str
    verb: str  # GET | PUT | POST | DELETE | PATCH, or the custom kind
    path: str  # the template as written: /v1/{name=shelves/*}:get
    file: str
    line: int


@dataclass(frozen=True)
class ProtoField:
    """A field of a message, as protoc compiled it."""

    name: str
    number: int
    line: int
    # A scalar's .proto name (string, int64), or the fully qualified name of
    # the message or enum it holds (pkg.Point).
    type: str
    kind: str  # scalar | message | enum
    label: str | None  # repeated | optional | required, or None
    json_name: str
    # map<key, value>: the key's scalar name; type and kind are the value's.
    map_key: str | None = None
    oneof: str | None = None
    deprecated: bool = False


@dataclass(frozen=True)
class ProtoMessage:
    """A message type of a compiled .proto file; nested ones too."""

    full_name: str  # pkg.Outer.Inner
    package: str
    file: str
    line: int
    fields: tuple  # ProtoField
    parent: str | None = None  # the enclosing message's full name


@dataclass(frozen=True)
class ProtoEnum:
    full_name: str
    package: str
    file: str
    line: int
    values: tuple  # (name, number, line)
    parent: str | None = None


class ProtocMissing(RuntimeError):
    """protoc (grpcio-tools) or protobuf is not installed."""


def available():
    """The include directories (the well-known types, then googleapis'
    common protos when installed) when grpcio-tools and protobuf are
    installed, else None."""
    try:
        import grpc_tools
        from google.protobuf import descriptor_pb2  # noqa: F401
    except ImportError:
        return None
    includes = [os.path.join(os.path.dirname(grpc_tools.__file__), "_proto")]
    try:
        from google.api import http_pb2
    except ImportError:
        return includes
    # site-packages/google/api/http_pb2.py: the root holds google/api/*.proto.
    includes.append(
        os.path.dirname(os.path.dirname(os.path.dirname(http_pb2.__file__)))
    )
    return includes


def _roots(protos, texts):
    """{path: [include roots, its own first]} of the .proto paths: the roots
    under which its imports, and theirs, are found."""
    imports = {path: _IMPORT.findall(texts[path]) for path in protos}

    def providers(imported):
        return [
            path for path in protos if path == imported or path.endswith("/" + imported)
        ]

    own = {}
    for path in protos:
        for imported in imports[path]:
            for provider in providers(imported):
                root = provider[: len(provider) - len(imported)].rstrip("/")
                own.setdefault(provider, set()).add(root)
    roots = {}
    for path in protos:
        found = own.get(path)
        first = (
            min(found)
            if found and len(found) == 1
            else PurePosixPath(path).parent.as_posix()
        )
        first = "" if first == "." else first
        listed = [first]
        for imported in imports[path]:
            # The closest provider first: the longest shared directory.
            for provider in sorted(
                providers(imported),
                key=lambda p: -len(os.path.commonpath([p, path])),
            ):
                root = provider[: len(provider) - len(imported)].rstrip("/")
                if root not in listed:
                    listed.append(root)
        roots[path] = listed
    # protoc also opens the imports of imports: add the roots those need,
    # nearest first.
    closed = {}
    for path in protos:
        listed, seen, queue = list(roots[path]), {path}, [path]
        while queue:
            current = queue.pop(0)
            for imported in imports[current]:
                for provider in sorted(
                    providers(imported),
                    key=lambda p: -len(os.path.commonpath([p, current])),
                ):
                    if provider in seen:
                        continue
                    seen.add(provider)
                    queue.append(provider)
                    listed += [r for r in roots[provider] if r not in listed]
        closed[path] = listed
    return closed


def _compile(root, paths, includes, well_known):
    """{path: FileDescriptorProto} of ``paths``, compiled together with the
    same include roots; empty when protoc fails on any of them."""
    from google.protobuf import descriptor_pb2

    # The google.api.http extension parses into MethodOptions only once its
    # module is loaded.
    _http_rule_extension()
    with tempfile.TemporaryDirectory() as directory:
        output = os.path.join(directory, "set.pb")
        arguments = [sys.executable, "-m", "grpc_tools.protoc"]
        arguments += [f"--proto_path={root / include}" for include in includes]
        arguments += [
            *(f"--proto_path={include}" for include in well_known),
            "--include_source_info",
            f"--descriptor_set_out={output}",
        ]
        arguments += [str(root / path) for path in paths]
        try:
            completed = subprocess.run(
                arguments, capture_output=True, timeout=_TIMEOUT, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}
        if completed.returncode != 0 or not os.path.exists(output):
            return {}
        descriptors = descriptor_pb2.FileDescriptorSet()
        with open(output, "rb") as handle:
            descriptors.ParseFromString(handle.read())
    # A descriptor's name is its path under the include root protoc
    # matched; inputs are found under their own (first) root.
    by_name = {}
    for path in paths:
        prefix = includes[0] + "/" if includes[0] else ""
        if path.startswith(prefix):
            by_name[path[len(prefix) :]] = path
    found = {}
    for descriptor in descriptors.file:
        if descriptor.name in by_name:
            found[by_name[descriptor.name]] = descriptor
    return found if len(found) == len(paths) else {}


def _split(root, paths, includes, roots, well_known):
    """``_compile`` of ``paths``; when the run fails, each half is compiled
    on its own, down to single files with their own roots, so a broken
    file costs only itself."""
    found = _compile(root, paths, includes, well_known)
    if found or not paths:
        return found
    if len(paths) == 1:
        own = roots[paths[0]]
        return _compile(root, paths, own, well_known) if own != includes else {}
    middle = len(paths) // 2
    found = _split(root, paths[:middle], includes, roots, well_known)
    found.update(_split(root, paths[middle:], includes, roots, well_known))
    return found


def _http_rule_extension():
    """The ``google.api.http`` extension of MethodOptions, when
    googleapis-common-protos is installed."""
    try:
        from google.api import annotations_pb2
    except ImportError:
        return None
    return annotations_pb2.http


def _bindings(rule, service, method, path, line):
    found = []
    for current in (rule, *rule.additional_bindings):
        kind = current.WhichOneof("pattern")
        if kind in _VERBS:
            verb, template = kind.upper(), getattr(current, kind)
        elif kind == "custom":
            verb, template = current.custom.kind.upper(), current.custom.path
        else:
            continue
        if template:
            found.append(HttpBinding(service, method, verb, template, path, line))
    return found


def _methods(descriptor, path):
    """(package, [ProtoMethod], [HttpBinding]) of a compiled file."""
    lines = {
        tuple(location.path): location.span[0] + 1
        for location in descriptor.source_code_info.location
        if len(location.path) == 5
        and location.path[0] == _SERVICE
        and location.path[2] == _METHOD
        and location.path[4] == _NAME
    }
    extension = _http_rule_extension()
    package = descriptor.package
    methods, bindings = [], []
    for s, service in enumerate(descriptor.service):
        name = f"{package}.{service.name}" if package else service.name
        for m, method in enumerate(service.method):
            line = lines.get((_SERVICE, s, _METHOD, m, _NAME), 1)
            methods.append(
                ProtoMethod(
                    name,
                    method.name,
                    path,
                    line,
                    _STREAMING[
                        bool(method.client_streaming), bool(method.server_streaming)
                    ],
                    method.input_type.lstrip("."),
                    method.output_type.lstrip("."),
                )
            )
            if extension is not None and method.options.HasExtension(extension):
                bindings += _bindings(
                    method.options.Extensions[extension], name, method.name, path, line
                )
    return package, methods, bindings


def _types(descriptor, path):
    """[ProtoMessage | ProtoEnum] a compiled file declares, nested ones
    included (map entries are a field's map, not a message)."""
    lines = {
        tuple(location.path): location.span[0] + 1
        for location in descriptor.source_code_info.location
    }
    package = descriptor.package
    found = []

    def named(prefix, name):
        return f"{prefix}.{name}" if prefix else name

    def enum(item, location, prefix, parent):
        full = named(prefix, item.name)
        found.append(
            ProtoEnum(
                full,
                package,
                path,
                lines.get(location, 1),
                tuple(
                    (value.name, value.number, lines.get((*location, _VALUE, v), 1))
                    for v, value in enumerate(item.value)
                ),
                parent,
            )
        )

    def message(item, location, prefix, parent):
        full = named(prefix, item.name)
        entries = {
            f".{full}.{nested.name}": nested
            for nested in item.nested_type
            if nested.options.map_entry
        }
        fields = []
        for f, field in enumerate(item.field):
            label = {1: None, 2: "required", 3: "repeated"}.get(field.label)
            if field.proto3_optional or (
                field.label == 1 and descriptor.syntax in ("", "proto2")
            ):
                label = "optional"
            oneof = None
            if field.HasField("oneof_index") and not field.proto3_optional:
                oneof = item.oneof_decl[field.oneof_index].name
            map_key, value = None, field
            entry = (
                entries.get(field.type_name) if field.type == _MESSAGE_TYPE else None
            )
            if entry is not None:
                key, value = entry.field[0], entry.field[1]
                map_key, label = _SCALARS.get(key.type, str(key.type)), None
            if value.type in (_MESSAGE_TYPE, _GROUP):
                kind, type_name = "message", value.type_name.lstrip(".")
            elif value.type == _ENUM_TYPE:
                kind, type_name = "enum", value.type_name.lstrip(".")
            else:
                kind, type_name = "scalar", _SCALARS.get(value.type, str(value.type))
            fields.append(
                ProtoField(
                    field.name,
                    field.number,
                    lines.get((*location, _FIELD, f), 1),
                    type_name,
                    kind,
                    label,
                    field.json_name,
                    map_key,
                    oneof,
                    bool(field.options.deprecated),
                )
            )
        found.append(
            ProtoMessage(
                full, package, path, lines.get(location, 1), tuple(fields), parent
            )
        )
        for n, nested in enumerate(item.nested_type):
            if not nested.options.map_entry:
                message(nested, (*location, _NESTED, n), full, full)
        for e, nested in enumerate(item.enum_type):
            enum(nested, (*location, _NESTED_ENUM, e), full, full)

    for m, item in enumerate(descriptor.message_type):
        message(item, (_MESSAGE, m), package, None)
    for e, item in enumerate(descriptor.enum_type):
        enum(item, (_ENUM, e), package, None)
    return found


def read_protos(root, protos, texts):
    """{path: (package, [ProtoMethod], [HttpBinding], [ProtoMessage |
    ProtoEnum])} of the .proto files
    protoc compiles; ``texts`` are their contents. grpcio-tools and
    protobuf are required: without them this raises ``ProtocMissing``
    rather than reading nothing."""
    if not protos:
        return {}
    well_known = available()
    if well_known is None:
        raise ProtocMissing(
            "grpcio-tools and protobuf are required to read .proto files; "
            "install deepthought with its dependencies"
        )
    roots = _roots(protos, texts)
    # Files under the same own root compile in one protoc run, with every
    # root their imports need (``_split`` isolates the files that fail).
    groups = {}
    for path in protos:
        groups.setdefault(roots[path][0], []).append(path)
    read = {}
    for own, paths in groups.items():
        includes = [own]
        for path in paths:
            includes += [r for r in roots[path] if r not in includes]
        found = _split(root, paths, includes, roots, well_known)
        for path, descriptor in found.items():
            read[path] = (*_methods(descriptor, path), _types(descriptor, path))
    return read


_VARIABLE = re.compile(r"\{([^{}=]+)(?:=([^{}]*))?\}")


def route_path(template):
    """A google.api.http path template as a route path: ``{name}`` and
    ``{name=*}`` are variables, ``{name=**}`` the rest of the path, and a
    pattern ``{name=shelves/*}`` its own segments with each ``*`` a
    variable. A ``:verb`` suffix stays literal text of the last segment."""

    def expand(match):
        name, pattern = match.group(1).strip(), match.group(2)
        if pattern is None or pattern == "*":
            return "{" + name + "}"
        parts = []
        for part in pattern.split("/"):
            parts.append(
                "{*" + name + "}"
                if part == "**"
                else "{" + name + "}"
                if part == "*"
                else part
            )
        return "/".join(parts)

    return _VARIABLE.sub(expand, template)


def _flattened(serialized):
    """(enums, messages): the full names of the enum and message types a
    serialized FileDescriptorProto declares, each in protobuf-go's
    flattened ordering (top-level declarations first, then, for each
    message in depth-first order, the ones nested directly in it); map
    entry messages included. None when it does not parse."""
    from google.protobuf import descriptor_pb2
    from google.protobuf.message import DecodeError

    descriptor = descriptor_pb2.FileDescriptorProto()
    try:
        descriptor.ParseFromString(serialized)
    except DecodeError:
        return None
    prefix = descriptor.package

    def named(parent, name):
        return f"{parent}.{name}" if parent else name

    walk = []

    def visit(items, parent):
        for item in items:
            full = named(parent, item.name)
            walk.append((item, full))
            visit(item.nested_type, full)

    visit(descriptor.message_type, prefix)
    enums = [named(prefix, e.name) for e in descriptor.enum_type]
    messages = [named(prefix, m.name) for m in descriptor.message_type]
    for item, full in walk:
        enums.extend(named(full, e.name) for e in item.enum_type)
        messages.extend(named(full, m.name) for m in item.nested_type)
    return enums, messages


def flattened_types(serialized, enums, messages):
    """The fully qualified names of the enum and message types a serialized
    FileDescriptorProto declares, in the order protobuf-go's TypeBuilder
    lists their Go types (GoTypes): the file's enums, then its messages, in
    protobuf-go's flattened ordering. Map entry messages are listed too;
    their Go type is nil. None when the descriptor does not parse or does
    not declare ``enums`` enums and ``messages`` messages."""
    found = _flattened(serialized)
    if found is None or len(found[0]) != enums or len(found[1]) != messages:
        return None
    return found[0] + found[1]


def declared_types(serialized):
    """[(kind, full name)] of the enums and messages (map entries too) a
    serialized FileDescriptorProto declares; [] when it does not parse."""
    found = _flattened(serialized)
    if found is None:
        return []
    return [("enum", n) for n in found[0]] + [("message", n) for n in found[1]]


def python_descriptors(source):
    """The serialized FileDescriptorProtos a Python module generated by
    protoc's Python generator registers, read with Python's ast: the bytes
    literal passed to ``AddSerializedFile`` of a descriptor pool, or as
    ``serialized_pb`` to ``FileDescriptor``, on a name the module imports
    from ``google.protobuf`` (descriptor_pool or descriptor)."""
    import ast

    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    imported = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "google.protobuf":
            for alias in node.names:
                imported[alias.asname or alias.name] = alias.name
    found = []

    def root(expression):
        while isinstance(expression, (ast.Attribute, ast.Call)):
            expression = (
                expression.value
                if isinstance(expression, ast.Attribute)
                else expression.func
            )
        return expression.id if isinstance(expression, ast.Name) else None

    def literal(expression):
        if isinstance(expression, ast.Constant) and isinstance(expression.value, bytes):
            return expression.value
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        module = imported.get(root(node.func.value))
        if node.func.attr == "AddSerializedFile" and module == "descriptor_pool":
            data = literal(node.args[0]) if node.args else None
        elif node.func.attr == "FileDescriptor" and module == "descriptor":
            data = next(
                (literal(k.value) for k in node.keywords if k.arg == "serialized_pb"),
                None,
            )
        else:
            continue
        if data is not None:
            found.append(data)
    return found


def indexed_type(serialized, kind, path):
    """The full name of the message or enum protobuf-es's messageDesc /
    enumDesc indexes in a serialized FileDescriptorProto: the first index
    picks a top-level message (or, for an enum given one index, a top-level
    enum), each next one a message nested in it, and an enum's last index
    an enum of that message. None when it does not parse or an index is out
    of range."""
    from google.protobuf import descriptor_pb2
    from google.protobuf.message import DecodeError

    descriptor = descriptor_pb2.FileDescriptorProto()
    try:
        descriptor.ParseFromString(serialized)
    except DecodeError:
        return None
    if not path:
        return None
    names = [descriptor.package] if descriptor.package else []
    if kind == "enum" and len(path) == 1:
        items = descriptor.enum_type
        if not 0 <= path[0] < len(items):
            return None
        return ".".join([*names, items[path[0]].name])
    messages = path[:-1] if kind == "enum" else path
    items, current = descriptor.message_type, None
    for index in messages:
        if not 0 <= index < len(items):
            return None
        current = items[index]
        names.append(current.name)
        items = current.nested_type
    if kind == "enum":
        if current is None or not 0 <= path[-1] < len(current.enum_type):
            return None
        names.append(current.enum_type[path[-1]].name)
    return ".".join(names)
