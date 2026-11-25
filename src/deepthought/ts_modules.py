"""Bind JavaScript and TypeScript imports to the files the TypeScript
compiler resolves them to.

``tsmodules/resolve.js`` builds a TypeScript program for each project
(the nearest ``tsconfig.json`` or ``jsconfig.json`` that includes a file,
references followed) and asks the compiler where each of its imports
resolves: ``paths``, ``baseUrl``, extensions, index files, ``package.json``
``types`` and ``exports``, and the resolution mode of each import are the
compiler's. A file no project includes (excluded from it, or left out for a
file of the same name with another extension) gets the options of the
nearest config above it, JavaScript allowed, so its ``paths`` and
``baseUrl`` apply; with no config above it, the options the language service
gives such a file. Nothing is type-checked or emitted.

The compiler is the ``typescript`` package the pinned scip-typescript
installs, so the engine needs no other install; a missing or different
version raises ``TypeScriptUnavailable``.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess

from .imports import RESOLUTION_CONFIGURATION, ImportLink, ImportResolution

TYPESCRIPT_VERSION = "6.0.3"
TYPESCRIPT_TOOL = f"typescript@{TYPESCRIPT_VERSION}"
TYPESCRIPT_COMPILER_MODULE = "typescript.compiler-module"
NO_IMPORT = "compiler_no_import"
NOT_FOUND = "compiler_module_not_found"
EXTERNAL = "compiler_external_module"
NOT_ANALYZED = "compiler_target_not_analyzed"
_LANGUAGES = {"javascript", "typescript"}
_HELPER = Path(__file__).with_name("tsmodules") / "resolve.js"


class TypeScriptUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class CompilerImport:
    file: str
    line: int  # 1-based
    character: int  # UTF-16, 0-based, where the specifier's literal starts
    specifier: str
    resolved: str | None  # absolute, symbolic links resolved
    project: str | None  # the config that includes the file
    # For a file no config includes: the nearest config, whose options
    # (JavaScript allowed) it was resolved with; None when there is none.
    options_from: str | None = None


def locate_typescript(environ=None):
    """(node, lib/typescript.js) of the TypeScript the pinned scip-typescript
    installs, checked to be ``TYPESCRIPT_VERSION``."""
    from .indexers import INDEXERS, locate

    environ = os.environ if environ is None else environ
    node = shutil.which("node", path=environ.get("PATH"))
    indexer = locate(INDEXERS["scip-typescript"], environ)
    if node is None or indexer is None:
        raise TypeScriptUnavailable(
            "resolving JavaScript and TypeScript imports needs node and the "
            "pinned scip-typescript (deepthought indexers lists it)"
        )
    package = Path(indexer).resolve()
    while not (package / "package.json").is_file() or _name(package) != (
        "@sourcegraph/scip-typescript"
    ):
        if package.parent == package:
            raise TypeScriptUnavailable(f"no scip-typescript package above {indexer}")
        package = package.parent
    found = subprocess.run(
        [
            node,
            "-e",
            "const p = require.resolve('typescript', {paths: [process.argv[1]]});"
            "process.stdout.write(JSON.stringify([p, require(p).version]))",
            str(package),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if found.returncode:
        raise TypeScriptUnavailable("scip-typescript's typescript package is missing")
    path, version = json.loads(found.stdout)
    if version != TYPESCRIPT_VERSION:
        raise TypeScriptUnavailable(
            f"typescript {version} found, {TYPESCRIPT_VERSION} required"
        )
    return node, path


def _name(package):
    try:
        return json.loads((package / "package.json").read_text("utf-8")).get("name")
    except (OSError, ValueError):
        return None


def compiler_imports(root, files, environ=None, timeout=1800):
    """Every import the compiler records in ``files`` (workspace-relative),
    with the file it resolves to."""
    node, typescript = locate_typescript(environ)
    root = Path(root).resolve()
    completed = subprocess.run(
        [node, str(_HELPER), typescript],
        input=json.dumps({"root": str(root), "files": sorted(files)}),
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=root,
    )
    if completed.returncode:
        raise RuntimeError("TypeScript module resolution failed: " + completed.stderr)
    return [
        CompilerImport(
            row["file"],
            row["line"] + 1,
            row["character"],
            row["specifier"],
            row["resolved"],
            row["project"],
            row["options_from"],
        )
        for row in map(json.loads, completed.stdout.splitlines())
    ]


def bind_imports(result, root, found, resolutions):
    """``resolutions`` with each JavaScript or TypeScript import replaced by
    the compiler's: the import at the same specifier in ``found``
    (``compiler_imports``)."""
    from .scip_calls import _Lines, _to_utf8

    root = Path(root).resolve()
    languages = {c.file: c.language for c in result.coverage}
    imports = {i.id: i for i in result.imports}
    by_file = {}
    for row in found:
        by_file.setdefault(row.file, []).append(row)
    lines = _Lines(root)
    bound = []
    for resolution in resolutions:
        item = imports.get(resolution.import_id)
        if (
            item is None
            or languages.get(item.file) not in _LANGUAGES
            or resolution.reason == RESOLUTION_CONFIGURATION
        ):
            bound.append(resolution)
            continue
        rows = [
            row
            for row in by_file.get(item.file, ())
            if row.specifier == item.specifier
            and (item.line, item.column)
            <= (
                row.line,
                _to_utf8(lines.get(item.file, row.line), row.character, "utf-16"),
            )
            <= (item.end_line, item.end_column)
        ]
        if not rows:
            bound.append(ImportResolution(item.id, "unresolved", reason=NO_IMPORT))
            continue
        row = rows[0]
        target = _relative(root, row.resolved)
        if row.resolved is None:
            bound.append(ImportResolution(item.id, "unresolved", reason=NOT_FOUND))
        elif target is None or "node_modules" in target.split("/"):
            # Outside the workspace, or an installed package. The compiler's
            # own flag is not read: it marks any lookup through
            # node_modules, workspace packages linked there included.
            bound.append(ImportResolution(item.id, "unresolved", reason=EXTERNAL))
        elif target not in languages:
            bound.append(ImportResolution(item.id, "unresolved", reason=NOT_ANALYZED))
        else:
            bound.append(
                ImportResolution(
                    item.id,
                    "linked",
                    (ImportLink(target, TYPESCRIPT_COMPILER_MODULE),),
                )
            )
    return tuple(bound)


def _relative(root, path):
    if path is None:
        return None
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return None
