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

A file protoc cannot compile (an import that is not in the repository,
such as googleapis' ``google/api/annotations.proto`` when it is not
vendored, or a syntax error) has no descriptor here; the caller reads it
another way.
"""

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


def available():
    """The include directory of the well-known types when grpcio-tools and
    protobuf are installed, else None."""
    try:
        import grpc_tools
        from google.protobuf import descriptor_pb2  # noqa: F401
    except ImportError:
        return None
    return os.path.join(os.path.dirname(grpc_tools.__file__), "_proto")


def _roots(protos, texts):
    """{path: [include roots, its own first]} of the .proto paths."""
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
    return roots


def _compile(root, paths, includes, well_known):
    """{path: FileDescriptorProto} of ``paths``, compiled together with the
    same include roots; empty when protoc fails on any of them."""
    from google.protobuf import descriptor_pb2

    with tempfile.TemporaryDirectory() as directory:
        output = os.path.join(directory, "set.pb")
        arguments = [sys.executable, "-m", "grpc_tools.protoc"]
        arguments += [f"--proto_path={root / include}" for include in includes]
        arguments += [
            f"--proto_path={well_known}",
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


def _methods(descriptor, path):
    lines = {
        tuple(location.path): location.span[0] + 1
        for location in descriptor.source_code_info.location
        if len(location.path) == 5
        and location.path[0] == _SERVICE
        and location.path[2] == _METHOD
        and location.path[4] == _NAME
    }
    package = descriptor.package
    methods = []
    for s, service in enumerate(descriptor.service):
        name = f"{package}.{service.name}" if package else service.name
        for m, method in enumerate(service.method):
            methods.append(
                ProtoMethod(
                    name,
                    method.name,
                    path,
                    lines.get((_SERVICE, s, _METHOD, m, _NAME), 1),
                    _STREAMING[
                        bool(method.client_streaming), bool(method.server_streaming)
                    ],
                )
            )
    return package, methods


def read_protos(root, protos, texts):
    """{path: (package, [ProtoMethod])} of the .proto files protoc compiles;
    ``texts`` are their contents. Empty when grpcio-tools is not
    installed."""
    well_known = available()
    if well_known is None:
        return {}
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
