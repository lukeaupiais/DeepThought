"""Imports resolved by the SCIP indexers instead of path rules.

Each indexer resolves an import as its language's own tooling does and
records that resolution as a reference to the module's symbol at the name the
statement writes. A module the index defines (a document of the workspace)
links the importing file to the files that define it; any other module is
external and names its package and version.

Python: pyright searches the interpreter's path, the workspace's source roots
(each project's own directory and its ``src``, see
``deepthought.indexers.python_path``), its installed dependencies and the
standard library. The module symbol is `` `a.b`/__init__: ``, recorded at each
module name, the submodules ``from a import b`` names included; external
modules name ``python-stdlib 3.13`` or ``pydantic 2.12.5``.

Go: the go command's package loading, through go.mod, replace directives and
the module cache. The package symbol is `` `example.com/m/pkg`/ ``, defined by
the package clause of each of the package's files; external packages name
their module and version (``github.com/go-chi/chi/v5 v5.2.1``; the standard
library is ``github.com/golang/go/src go1.24``).

TypeScript and JavaScript: the TypeScript compiler's module resolution
through tsconfig, package.json and node_modules, workspaces included. The
module symbol is a file's namespace (``src/`a.ts`/``), or an ambient module
a declaration file declares (`` `buffer.d.ts`/`"node:buffer"`/ ``); external
modules name their npm package and version (``@types/node 22.10.1``).

Files no index covers keep the path rules (``deepthought.imports``), and so
do Python relative imports everywhere: scip-python 0.6.8 names the module of
``from .models import X`` by the written remainder alone (`` `models`/__init__: ``),
which matches another project's top-level ``models`` or nothing, while the
relative path rule is the language's own definition. A Python from-import of
an in-repository package also keeps the path rule's links to the submodules
it names (``from flask import json``), which the index records as attributes
of the package. An import in a covered file with no module reference keeps a
path rule's links if it has them (a side-effect import of a stylesheet has no
symbol to reference); otherwise it is unresolved (``scip_no_occurrence``):
the tooling found no module for it, for example a dependency that is not
installed.
"""

import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .imports import PYTHON_FROM_IMPORT_SUBMODULE, ImportLink, ImportResolution
from .scip_calls import _IndexView, _key, _Lines, _to_utf8

SCIP_PYTHON_IMPORT_MODULE = "scip.python-import-module"
SCIP_PYTHON_IMPORT_PACKAGE = "scip.python-import-package"
SCIP_GO_IMPORT_MODULE = "scip.go-import-module"
SCIP_GO_IMPORT_PACKAGE = "scip.go-import-package"
SCIP_SCRIPT_IMPORT_MODULE = "scip.typescript-import-module"
SCIP_SCRIPT_IMPORT_PACKAGE = "scip.typescript-import-package"
EXTERNAL = "scip_external"
NO_OCCURRENCE = "scip_no_occurrence"

# The analyzed languages each indexer resolves imports for.
_LANGUAGES = {
    "scip-python": {"python"},
    "scip-go": {"go"},
    "scip-typescript": {"typescript", "tsx", "javascript"},
}
ECOSYSTEMS = {"scip-python": "python", "scip-go": "go", "scip-typescript": "npm"}
LINK_RULES = {
    "scip-python": SCIP_PYTHON_IMPORT_MODULE,
    "scip-go": SCIP_GO_IMPORT_MODULE,
    "scip-typescript": SCIP_SCRIPT_IMPORT_MODULE,
}
PACKAGE_RULES = {
    "scip-python": SCIP_PYTHON_IMPORT_PACKAGE,
    "scip-go": SCIP_GO_IMPORT_PACKAGE,
    "scip-typescript": SCIP_SCRIPT_IMPORT_PACKAGE,
}
# A TypeScript module namespace's last segment is a source file.
_SCRIPT_FILE = re.compile(r"\.(d\.)?[mc]?[jt]sx?$")


@dataclass(frozen=True)
class Package:
    """An external package an import resolves into, and the modules named."""

    name: str
    version: str
    modules: tuple[str, ...]
    ecosystem: str = "python"
    rule_id: str = SCIP_PYTHON_IMPORT_PACKAGE


def _segments(descriptor):
    """A SCIP descriptor's namespace segments, backticks removed, if it is
    namespaces only (``a/`b.c`/``); else None."""
    found, rest = [], descriptor
    while rest:
        if rest.startswith("`"):
            end = rest.find("`", 1)
            while end != -1 and rest[end + 1 : end + 2] == "`":
                end = rest.find("`", end + 2)
            if end == -1 or rest[end + 1 : end + 2] != "/":
                return None
            found.append(rest[1:end].replace("``", "`"))
            rest = rest[end + 2 :]
        else:
            end = rest.find("/")
            if end <= 0 or any(c in rest[:end] for c in "#.:()[]!"):
                return None
            found.append(rest[:end])
            rest = rest[end + 1 :]
    return found or None


def _module(native, indexer="scip-python"):
    """(package, version, module) of an indexer's module symbol, else None."""
    parts = (native or "").split(" ", 4)
    if len(parts) != 5 or parts[0] != indexer:
        return None
    descriptor = parts[4]
    if indexer == "scip-python":
        if parts[1] != "python" or not descriptor.endswith("/__init__:"):
            return None
        module = descriptor[: -len("/__init__:")]
        if module.startswith("`") and module.endswith("`"):
            module = module[1:-1]
        return parts[2], parts[3], module
    segments = _segments(descriptor)
    if segments is None:
        return None
    if indexer == "scip-go":
        if parts[1] != "gomod":
            return None
        return parts[2], parts[3], "/".join(segments)
    if parts[1] != "npm":
        return None
    last = segments[-1]
    if len(segments) > 1 and len(last) > 2 and last[0] == last[-1] == '"':
        # An ambient module a declaration file declares (`declare module
        # "node:buffer"`): the module is the name it declares.
        if _SCRIPT_FILE.search(segments[-2]):
            return parts[2], parts[3], last[1:-1]
        return None
    if not _SCRIPT_FILE.search(last):
        return None
    return parts[2], parts[3], "/".join(segments)


def _indexer(index):
    for name, version in index.analyzer_versions:
        if name in _LANGUAGES:
            return name, f"{name}@{version}"
    return None, None


class _GoModules:
    """What the go command says of each module of the workspace: the version
    go.mod requires each module at (``go mod edit -json``, replace directives
    applied), and the files each package compiles on the engine's platform
    (``go list``). scip-go names a module's version in its own form
    (``v9.31.0`` for ``v9.31.0+incompatible``, ``6ff867650f40`` for a
    pseudo-version), and defines a package in every file it indexed,
    including files built only for another platform."""

    def __init__(self, root):
        from .indexers import go_modules

        self.root = Path(root)
        self.modules = go_modules(self.root)
        self.cache = {}

    def module(self, file):
        from .indexers import go_module_of

        found = go_module_of(file, self.modules)
        if found is None:
            return {}, {}
        if found not in self.cache:
            self.cache[found] = self._read(found)
        return self.cache[found]

    def _read(self, module):
        from .indexers import _go_command

        directory = self.root if module == "." else self.root / module
        environ = {**os.environ, "GOPROXY": "off", "GOWORK": "off"}
        versions = {}
        edit = _go_command(["mod", "edit", "-json"], directory, environ)
        if edit:
            document = json.loads(edit)
            for required in document.get("Require") or ():
                versions[required["Path"]] = required["Version"]
            for replace in document.get("Replace") or ():
                if replace.get("New", {}).get("Version"):
                    versions[replace["Old"]["Path"]] = replace["New"]["Version"]
        files = {}
        listed = _go_command(
            ["list", "-e", "-json=Dir,GoFiles,CgoFiles", "./..."], directory, environ
        )
        decoder, at = json.JSONDecoder(), 0
        while listed and at < len(listed):
            while at < len(listed) and listed[at].isspace():
                at += 1
            if at >= len(listed):
                break
            package, at = decoder.raw_decode(listed, at)
            try:
                relative = (
                    Path(package["Dir"]).resolve().relative_to(self.root.resolve())
                )
            except (KeyError, ValueError):
                continue
            prefix = "" if str(relative) == "." else relative.as_posix() + "/"
            files[prefix.rstrip("/") or "."] = {
                prefix + name
                for name in (package.get("GoFiles") or [])
                + (package.get("CgoFiles") or [])
            }
        return versions, files


def bind_imports(result, indexes, root, paths=None):
    """``{import id: (ImportResolution, packages)}`` for the imports in files
    ``indexes`` (pairs of an ingested index and its receipt digest) cover;
    ``packages`` are the external ``Package``s of each. ``paths`` are the path
    rules' resolutions by import id, which an import keeps where the index
    has nothing to say, and whose submodule links a Python from-import of an
    in-repository package keeps."""
    paths = paths or {}
    lines = _Lines(Path(root))
    go = (
        _GoModules(root)
        if any(_indexer(index)[0] == "scip-go" for index, _ in indexes)
        else None
    )
    covering, shared_by_indexer, by_line = {}, {}, defaultdict(list)
    own_modules, indexer_of = {}, {}
    for index, receipt in indexes:
        indexer, tool = _indexer(index)
        if indexer is None:
            continue
        view = _IndexView(index, tool, receipt)
        indexer_of[view] = indexer
        # The modules this index's own documents define, by module name.
        own = defaultdict(set)
        for definition in index.definitions:
            found = _module(definition.native_id, indexer)
            if found is not None:
                own[found[2]].add(definition.file)
        own_modules[view] = own
        shared = shared_by_indexer.setdefault(indexer, defaultdict(list))
        for key, found in view.definitions.items():
            shared[key].extend(found)
        view.definitions = shared
        for coverage in index.coverage:
            covering.setdefault(coverage.file, view)
        for reference in index.references:
            if covering.get(reference.file) is view and _module(
                reference.native_id, indexer
            ):
                by_line[(reference.file, reference.line)].append((reference, view))
    languages = {c.file: c.language for c in result.coverage}
    bound = {}
    for item in result.imports:
        view = covering.get(item.file)
        if view is None:
            continue
        indexer = indexer_of[view]
        language = languages.get(item.file)
        if language not in _LANGUAGES[indexer]:
            continue
        if language == "python" and item.specifier.startswith("."):
            continue
        links, packages = {}, {}
        for line in range(item.line, item.end_line + 1):
            for reference, seen in by_line.get((item.file, line), ()):
                if seen is not view:
                    continue
                text = lines.get(item.file, line)
                column = _to_utf8(text, reference.column, view.encoding(reference))
                if (line, column) < (item.line, item.column) or (line, column) >= (
                    item.end_line,
                    item.end_column,
                ):
                    continue
                package, version, module = _module(reference.native_id, indexer)
                declared = view.definitions.get(
                    _key(item.file, reference.native_id), ()
                )
                files = {d.file for d in declared}
                if not files and module in own_modules[view]:
                    # scip-python 0.6.8 names a module's package from the
                    # installed distributions' file lists before pyright's
                    # resolution: a wheel shipping a top-level `tests` puts
                    # its name on the repository's own tests/__init__.py,
                    # which pyright resolved (local roots come first).
                    files = own_modules[view][module]
                if indexer == "scip-go":
                    # A package's own test files are not part of what an
                    # import of it loads, nor files built for another
                    # platform where the go command lists the package.
                    files = {f for f in files if not f.endswith("_test.go")}
                    if files:
                        directory = str(PurePosixPath(min(files)).parent)
                        compiled = go.module(min(files))[1].get(directory)
                        if compiled and files & compiled:
                            files &= compiled
                    else:
                        version = go.module(item.file)[0].get(package, version)
                if files:
                    # An index may cover files the analysis left out (its
                    # project spans more than the selected files); only
                    # analysed files are link targets.
                    for file in sorted(files):
                        if file != item.file and file in languages:
                            links.setdefault(
                                file, ImportLink(file, LINK_RULES[indexer])
                            )
                else:
                    packages.setdefault((package, version), set()).add(module)
        found = tuple(
            Package(
                name,
                version,
                tuple(sorted(modules)),
                ECOSYSTEMS[indexer],
                PACKAGE_RULES[indexer],
            )
            for (name, version), modules in sorted(packages.items())
        )
        earlier = paths.get(item.id)
        if links:
            for link in earlier.links if earlier is not None else ():
                if link.rule_id == PYTHON_FROM_IMPORT_SUBMODULE:
                    links.setdefault(link.target_file, link)
            resolution = ImportResolution(
                item.id, "linked", tuple(links[f] for f in sorted(links))
            )
        elif found:
            resolution = ImportResolution(item.id, "unresolved", reason=EXTERNAL)
        elif earlier is not None and earlier.status == "linked":
            continue
        else:
            resolution = ImportResolution(item.id, "unresolved", reason=NO_OCCURRENCE)
        bound[item.id] = (resolution, found)
    return bound
