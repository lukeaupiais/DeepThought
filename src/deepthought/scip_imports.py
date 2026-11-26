"""Python imports resolved by scip-python instead of path rules.

pyright resolves an import as Python would, from the interpreter's search
path: the workspace's source roots (each project's own directory and its
``src``, see ``deepthought.indexers.python_path``), its installed
dependencies, and the standard library. scip-python records that resolution
as a reference to the module's symbol (`` `a.b`/__init__: ``) at each module
name an import statement writes, including the submodules ``from a import b``
names. A module the index defines (a document of the workspace) links the
importing file to that file; any other module is external and names its
package and version (``python-stdlib 3.13``, ``pydantic 2.12.5``).

Files no index covers keep the path rules (``deepthought.imports``), and so
do relative imports everywhere: scip-python 0.6.8 names the module of
``from .models import X`` by the written remainder alone (`` `models`/__init__: ``),
which matches another project's top-level ``models`` or nothing, while the
relative path rule is the language's own definition. A from-import of an
in-repository package also keeps the path rule's links to the submodules it
names (``from flask import json``), which the index records as attributes of
the package. An absolute import in a covered file with no module reference
is unresolved (``scip_no_occurrence``): pyright found no module for it, for
example a dependency that is not installed.
"""

from collections import defaultdict
from dataclasses import dataclass

from .imports import PYTHON_FROM_IMPORT_SUBMODULE, ImportLink, ImportResolution
from .scip_calls import _IndexView, _key, _Lines, _to_utf8

SCIP_PYTHON_IMPORT_MODULE = "scip.python-import-module"
SCIP_PYTHON_IMPORT_PACKAGE = "scip.python-import-package"
EXTERNAL = "scip_external"
NO_OCCURRENCE = "scip_no_occurrence"


@dataclass(frozen=True)
class Package:
    """An external package an import resolves into, and the modules named."""

    name: str
    version: str
    modules: tuple[str, ...]


def _module(native):
    """(package, version, module) of a scip-python module symbol, else None."""
    parts = native.split(" ", 4)
    if len(parts) != 5 or parts[0] != "scip-python" or parts[1] != "python":
        return None
    descriptor = parts[4]
    if not descriptor.endswith("/__init__:"):
        return None
    module = descriptor[: -len("/__init__:")]
    if module.startswith("`") and module.endswith("`"):
        module = module[1:-1]
    return parts[2], parts[3], module


def bind_python_imports(result, indexes, root, paths=None):
    """``{import id: (ImportResolution, packages)}`` for the absolute Python
    imports in files ``indexes`` (pairs of an ingested index and its receipt
    digest) cover; ``packages`` are the external ``Package``s of each.
    ``paths`` are the path rules' resolutions by import id, whose submodule
    links an in-repository from-import keeps."""
    paths = paths or {}
    from pathlib import Path

    lines = _Lines(Path(root))
    covering, shared_by_indexer, by_line = {}, {}, defaultdict(list)
    for index, receipt in indexes:
        tool = next(
            (f"{n}@{v}" for n, v in index.analyzer_versions if n.startswith("scip-")),
            "scip",
        )
        if not tool.startswith("scip-python@"):
            continue
        view = _IndexView(index, tool, receipt)
        shared = shared_by_indexer.setdefault("scip-python", defaultdict(list))
        for key, found in view.definitions.items():
            shared[key].extend(found)
        view.definitions = shared
        for coverage in index.coverage:
            covering.setdefault(coverage.file, view)
        for reference in index.references:
            if covering.get(reference.file) is view and _module(reference.native_id):
                by_line[(reference.file, reference.line)].append((reference, view))
    languages = {c.file: c.language for c in result.coverage}
    bound = {}
    for item in result.imports:
        if (
            languages.get(item.file) != "python"
            or item.file not in covering
            or item.specifier.startswith(".")
        ):
            continue
        links, packages = {}, {}
        for line in range(item.line, item.end_line + 1):
            for reference, view in by_line.get((item.file, line), ()):
                text = lines.get(item.file, line)
                column = _to_utf8(text, reference.column, view.encoding(reference))
                if (line, column) < (item.line, item.column) or (line, column) >= (
                    item.end_line,
                    item.end_column,
                ):
                    continue
                package, version, module = _module(reference.native_id)
                declared = view.definitions.get(
                    _key(item.file, reference.native_id), ()
                )
                files = sorted({d.file for d in declared})
                if files:
                    for file in files:
                        if file != item.file:
                            links.setdefault(
                                file, ImportLink(file, SCIP_PYTHON_IMPORT_MODULE)
                            )
                else:
                    packages.setdefault((package, version), set()).add(module)
        found = tuple(
            Package(name, version, tuple(sorted(modules)))
            for (name, version), modules in sorted(packages.items())
        )
        if links:
            earlier = paths.get(item.id)
            for link in earlier.links if earlier is not None else ():
                if link.rule_id == PYTHON_FROM_IMPORT_SUBMODULE:
                    links.setdefault(link.target_file, link)
            resolution = ImportResolution(
                item.id, "linked", tuple(links[f] for f in sorted(links))
            )
        elif found:
            resolution = ImportResolution(item.id, "unresolved", reason=EXTERNAL)
        else:
            resolution = ImportResolution(item.id, "unresolved", reason=NO_OCCURRENCE)
        bound[item.id] = (resolution, found)
    return bound
