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

from collections import defaultdict
from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import shutil
import subprocess

from .imports import _SCRIPT_KINDS, ImportLink, ImportResolution

TYPESCRIPT_VERSION = "6.0.3"
TYPESCRIPT_TOOL = f"typescript@{TYPESCRIPT_VERSION}"
TYPESCRIPT_COMPILER_MODULE = "typescript.compiler-module"
NEST_INJECTED_CALL = "typescript.call-nest-injected-provider"
NO_IMPORT = "compiler_no_import"
NOT_FOUND = "compiler_module_not_found"
EXTERNAL = "compiler_external_module"
NOT_ANALYZED = "compiler_target_not_analyzed"
# Every reason the compiler gives an import it leaves unlinked.
COMPILER_REASONS = frozenset({NO_IMPORT, NOT_FOUND, EXTERNAL, NOT_ANALYZED})
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


@dataclass(frozen=True)
class CompilerExport:
    module: str  # workspace-relative
    name: str  # the exported name; ``default`` for a default export
    declared: str | None  # the declaring file, relative to the workspace
    line: int | None  # 1-based
    character: int | None  # UTF-16, 0-based, where the declaration's name starts
    # Instead of a declaration: an export the compiler follows to a
    # declaration file with an implementation beside it is that
    # implementation's export of ``runtime_name``.
    runtime: str | None = None
    runtime_name: str | None = None


@dataclass(frozen=True)
class CompilerInjection:
    """A NestJS class provider binding the token of a constructor parameter
    property: ``property`` of the class declared at ``file``/``line``/
    ``character`` holds an instance of a class at one of ``provided``."""

    file: str  # workspace-relative
    line: int  # 1-based
    character: int  # UTF-16, 0-based, where the class's name starts
    property: str
    provided: tuple  # ((file, 1-based line, UTF-16 character), ...)


# A declaration file and the implementation beside it with the same stem,
# as the compiler pairs them.
_DECLARATION_SUFFIXES = {".d.ts": ".js", ".d.mts": ".mjs", ".d.cts": ".cjs"}


def is_declaration_file(path):
    return path.endswith(tuple(_DECLARATION_SUFFIXES))


def implementation_of(path, analyzed):
    """The analyzed file whose declarations the declaration file ``path``
    describes (``x.d.ts`` for ``x.js``), or None."""
    for suffix, runtime in _DECLARATION_SUFFIXES.items():
        if path.endswith(suffix):
            candidate = path[: -len(suffix)] + runtime
            return candidate if candidate in analyzed else None
    return None


@dataclass(frozen=True)
class CompilerModules:
    """What the compiler says about a workspace's JavaScript and TypeScript
    modules (``with_compiler_modules``).

    ``resolutions``: an ImportResolution for each JavaScript or TypeScript
    import. ``runtime``: for each import the compiler resolves to an analyzed
    file, the module a call through it runs: that file, or, for a
    declaration file, the analyzed implementation beside it. ``exports``:
    ``{module: {name: ((file, line, UTF-8 column), ...)}}``, the
    declarations each analyzed module exports under each name.
    ``injections``: ``{(class id, property): (provided class ids, ...)}``,
    what NestJS injects into each constructor parameter property whose
    token a class provider binds, between analyzed classes."""

    resolutions: tuple
    runtime: dict
    exports: dict
    injections: dict = field(default_factory=dict)


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
    return compiler_modules(root, files, environ, timeout)[0]


def compiler_modules(root, files, environ=None, timeout=1800):
    """(``compiler_imports``, every name each of ``files`` exports, with
    its declarations: CompilerExport rows, and the NestJS injections the
    compiler resolves: CompilerInjection rows)."""
    node, typescript = locate_typescript(environ)
    root = Path(root).resolve()
    completed = subprocess.run(
        [node, str(_HELPER), typescript],
        input=json.dumps(
            {
                "root": str(root),
                "files": sorted(files),
                "components": _components(root, files, environ),
            }
        ),
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=root,
    )
    if completed.returncode:
        raise RuntimeError("TypeScript module resolution failed: " + completed.stderr)
    imports, exports, injections = [], [], set()
    for row in map(json.loads, completed.stdout.splitlines()):
        if "injected" in row:
            # A class in several programs is written once for each.
            injections.add(
                CompilerInjection(
                    row["injected"],
                    row["line"] + 1,
                    row["character"],
                    row["property"],
                    tuple(
                        (p["file"], p["line"] + 1, p["character"])
                        for p in row["provided"]
                    ),
                )
            )
        elif "module" in row:
            exports.append(
                CompilerExport(
                    row["module"],
                    row["name"],
                    row.get("declared"),
                    row["line"] + 1 if "line" in row else None,
                    row.get("character"),
                    row.get("runtime"),
                    row.get("runtime_name"),
                )
            )
        else:
            imports.append(
                CompilerImport(
                    row["file"],
                    row["line"] + 1,
                    row["character"],
                    row["specifier"],
                    row["resolved"],
                    row["project"],
                    row["options_from"],
                )
            )
    return imports, exports, sorted(injections, key=repr)


def _components(root, files, environ):
    """{Vue component of ``files``: its script, as the compiler reads it}."""
    from . import vue_sfc

    components = sorted(f for f in files if vue_sfc.is_component(f))
    sources = [(root / f).read_bytes() for f in components]
    read = vue_sfc.read(sources, environ) if sources else []
    return {
        file: {
            "text": vue_sfc.script_source(source, component).decode("utf-8"),
            "jsx": component.jsx,
        }
        for file, source, component in zip(components, sources, read)
    }


def bind_imports(result, root, found):
    """The compiler's resolution of each JavaScript and TypeScript import of
    ``result``, in import identity order: the import at the same specifier
    in ``found`` (``compiler_imports``). Configuration facts are read by
    the compiler, not resolved, and get none."""
    return _bind(result, root, found)[0]


def _bind(result, root, found):
    """(``bind_imports``, {import id: workspace-relative file the compiler
    resolves it to, or None})."""
    from .scip_calls import _Lines, _to_utf8

    root = Path(root).resolve()
    languages = {c.file: c.language for c in result.coverage}
    by_file = {}
    for row in found:
        by_file.setdefault(row.file, []).append(row)
    lines = _Lines(root)
    bound, targets = [], {}
    for item in sorted(result.imports, key=lambda value: value.id):
        if languages[item.file] not in _LANGUAGES or item.kind in _SCRIPT_KINDS:
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
        targets[item.id] = target
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
    return tuple(bound), targets


def with_compiler_modules(result, root, environ=None):
    """``result`` carrying the compiler's view of its JavaScript and
    TypeScript modules (``CompilerModules``), which import resolution and
    call binding then use for those languages."""
    from .scip_calls import _Lines, _to_utf8

    languages = {c.file: c.language for c in result.coverage}
    scripts = sorted(f for f, language in languages.items() if language in _LANGUAGES)
    if not scripts or result.compiler_modules is not None:
        return result
    found, exported, injected = compiler_modules(root, scripts, environ)
    resolutions, targets = _bind(result, root, found)
    runtime = {}
    for import_id, target in targets.items():
        if target in languages:
            runtime[import_id] = (implementation_of(target, languages) or target,)
        elif target is not None and is_declaration_file(target):
            # A declaration file that is not analyzed describes the analyzed
            # implementation beside it.
            implementation = implementation_of(target, languages)
            if implementation is not None:
                runtime[import_id] = (implementation,)
    lines = _Lines(Path(root).resolve())
    rows = {}
    for row in exported:
        rows.setdefault((row.module, row.name), []).append(row)

    def declarations(module, name, seen):
        found = set()
        if (module, name) in seen:
            return found
        seen = seen | {(module, name)}
        for row in rows.get((module, name), ()):
            if row.runtime is not None:
                found |= declarations(row.runtime, row.runtime_name, seen)
            elif row.declared in languages:
                column = _to_utf8(
                    lines.get(row.declared, row.line), row.character, "utf-16"
                )
                found.add((row.declared, row.line, column))
        return found

    exports = {}
    for module, name in rows:
        if module in languages:
            found = declarations(module, name, frozenset())
            if found:
                exports.setdefault(module, {})[name] = tuple(sorted(found))
    classes = defaultdict(list)
    for d in result.definitions:
        if d.kind == "Class":
            classes[d.file].append(d)

    def class_at(file, line, character):
        # The innermost class declared around the name's position.
        if file not in languages:
            return None
        column = _to_utf8(lines.get(file, line), character, "utf-16")
        around = [
            d
            for d in classes.get(file, ())
            if (d.line, d.column) <= (line, column) <= (d.end_line, d.end_column)
        ]
        return max(around, key=lambda d: (d.line, d.column), default=None)

    injections = defaultdict(set)
    for row in injected:
        owner = class_at(row.file, row.line, row.character)
        provided = {
            found.id for at in row.provided if (found := class_at(*at)) is not None
        }
        if owner is not None and provided:
            injections[(owner.id, row.property)] |= provided
    return replace(
        result,
        compiler_modules=CompilerModules(
            resolutions,
            runtime,
            exports,
            {key: tuple(sorted(ids)) for key, ids in sorted(injections.items())},
        ),
    )


def injected_calls(result):
    """{call site id: (method ids, ...)}: each ``this.p.m()`` in a class
    whose constructor parameter property ``p`` NestJS injects
    (``CompilerModules.injections``) runs ``m`` of each provided class:
    the instance method that class declares, or else the one it inherits
    through ``extends``, class by class. A provided class that neither
    declares nor inherits ``m`` from a repository class adds nothing."""
    compiled = result.compiler_modules
    if compiled is None or not compiled.injections:
        return {}
    from .inheritance import resolve_supertypes

    by_id = {d.id: d for d in result.definitions}
    by_symbol = defaultdict(list)
    for d in result.definitions:
        by_symbol[(d.file, d.symbol)].append(d)
    facts = {item.id: item for item in result.supertypes}
    extends = {}
    for resolution in resolve_supertypes(result):
        item = facts[resolution.call_id]
        if item.kind == "extends" and resolution.target_id is not None:
            extends[item.owner_id] = resolution.target_id

    def method(cls, name):
        seen = set()
        while cls is not None and cls.id not in seen:
            seen.add(cls.id)
            own = [
                d
                for d in by_symbol.get((cls.file, f"{cls.symbol}.{name}"), ())
                if d.kind == "Method" and not d.static
            ]
            if own:
                return own
            cls = by_id.get(extends.get(cls.id))
        return []

    found = {}
    for site in result.call_sites:
        caller = by_id.get(site.caller_id)
        if (
            site.receiver_type != "this"
            or len(site.receiver_steps or ()) != 1
            or caller is None
        ):
            continue
        scope = caller.symbol.split(".")
        # The innermost class around the call: `this` is one of its
        # instances.
        owner = next(
            (
                classes[0]
                for depth in range(len(scope), 0, -1)
                if (
                    classes := [
                        d
                        for d in by_symbol.get((site.file, ".".join(scope[:depth])), ())
                        if d.kind == "Class"
                    ]
                )
            ),
            None,
        )
        provided = owner and compiled.injections.get((owner.id, site.receiver_steps[0]))
        name = site.callee.rsplit(".", 1)[-1]
        targets = sorted(
            {d.id for cls in provided or () for d in method(by_id[cls], name)}
        )
        if targets:
            found[site.id] = tuple(targets)
    return found


def _relative(root, path):
    if path is None:
        return None
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return None
