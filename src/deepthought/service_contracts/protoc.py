"""Read .proto files with protoc, the Protocol Buffers compiler.

grpcio-tools ships protoc and the well-known types. Each file is compiled
to a FileDescriptorSet with source info, and its services, rpcs, streaming
kinds and the lines of the rpc names come from the descriptor, so the
compiler, not a pattern, decides what the file declares.

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


def read_protos(root, protos, texts):
    """{path: (package, [ProtoMethod], [HttpBinding])} of the .proto files
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
            read[path] = _methods(descriptor, path)
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
