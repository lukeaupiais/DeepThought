"""Resolve static import facts to analyzed repository files, by declared rules.

Resolution reads no files: it matches specifiers against the coverage of one
AnalysisResult. Every link names the rule that produced it. A specifier that no
rule can tie to exactly one analyzed file stays unresolved or ambiguous, with a
reason, and produces no link.
"""

from collections import defaultdict
from dataclasses import dataclass
import posixpath

from .models import AnalysisResult, Import

# Rule IDs; their evidence types are registered by the producing AnalysisTool.
PYTHON_RELATIVE_MODULE = "tree-sitter.python-relative-module"
PYTHON_ABSOLUTE_MODULE = "tree-sitter.python-absolute-module-path"
PYTHON_FROM_IMPORT_SUBMODULE = "tree-sitter.python-from-import-submodule"
TYPESCRIPT_RELATIVE_MODULE = "tree-sitter.typescript-relative-module"
JAVASCRIPT_RELATIVE_FILE = "tree-sitter.javascript-relative-file"
JAVASCRIPT_EXTENSIONLESS_MODULE = "tree-sitter.javascript-extensionless-module"
GO_MODULE_PACKAGE = "tree-sitter.go-module-package"
JAVA_PACKAGE_MEMBER = "tree-sitter.java-package-member"
JAVA_SAME_SOURCE_ROOT = "tree-sitter.java-same-source-root"
_JAVA_TYPES = frozenset({"Class", "Interface", "Enum", "Record", "Annotation"})

_TS_EXTENSIONLESS = (".ts", ".tsx", ".d.ts")
_TS_INDEX = ("/index.ts", "/index.tsx", "/index.d.ts")
_TS_JS_EXTENSIONS = {".js": (".ts", ".tsx", ".d.ts"), ".jsx": (".tsx",)}
# JavaScript sources a TypeScript import may reach (as with allowJs), tried
# after every TypeScript candidate.
_JS_EXTENSIONLESS = (".js", ".jsx")
_JS_INDEX = ("/index.js", "/index.jsx")
_JS_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs")


@dataclass(frozen=True)
class ImportLink:
    target_file: str
    rule_id: str


@dataclass(frozen=True)
class ImportResolution:
    import_id: str
    status: str  # linked | ambiguous | unresolved
    links: tuple[ImportLink, ...] = ()
    reason: str | None = None
    candidates: tuple[str, ...] = ()


def _python_module(path, files):
    """Python's finder prefers a package directory over a same-named module."""
    for candidate in (path + "/__init__.py", path + ".py"):
        if candidate in files:
            return candidate
    return None


def _submodules(package_dir, names, files):
    links = []
    for name in names:
        if name == "*":
            continue
        prefix = f"{package_dir}/{name}" if package_dir else name
        target = _python_module(prefix, files)
        if target is not None:
            links.append(ImportLink(target, PYTHON_FROM_IMPORT_SUBMODULE))
    return links


def _python(item: Import, files, roots):
    specifier = item.specifier
    if specifier.startswith("."):
        dots = len(specifier) - len(specifier.lstrip("."))
        rest = specifier[dots:]
        base = posixpath.dirname(item.file)
        for _ in range(dots - 1):
            if not base:
                return ImportResolution(
                    item.id, "unresolved", reason="beyond_repository_root"
                )
            base = posixpath.dirname(base)
        if dots > 1 and posixpath.join(base, "__init__.py") not in files:
            # Climbing out of the importing module's directory needs a package.
            return ImportResolution(item.id, "unresolved", reason="outside_package")
        module_dir = posixpath.join(base, *rest.split(".")) if rest else base
        links = []
        if rest:
            target = _python_module(module_dir, files)
            if target is not None:
                links.append(ImportLink(target, PYTHON_RELATIVE_MODULE))
        else:
            init = posixpath.join(base, "__init__.py")
            if init in files:
                links.append(ImportLink(init, PYTHON_RELATIVE_MODULE))
        if item.kind == "from_import":
            links.extend(_submodules(module_dir, item.names, files))
    else:
        matches = []
        for root in roots:
            path = posixpath.join(root, *specifier.split("."))
            target = _python_module(path, files)
            if target is not None:
                matches.append((target, path))
        if len(matches) > 1:
            return ImportResolution(
                item.id,
                "ambiguous",
                reason="multiple_source_roots",
                candidates=tuple(sorted(target for target, _ in matches)),
            )
        links = []
        if matches:
            target, module_dir = matches[0]
            links.append(ImportLink(target, PYTHON_ABSOLUTE_MODULE))
            if item.kind == "from_import":
                links.extend(_submodules(module_dir, item.names, files))
    links = [link for link in dict.fromkeys(links) if link.target_file != item.file]
    if not links:
        return ImportResolution(item.id, "unresolved", reason="no_analyzed_file")
    return ImportResolution(item.id, "linked", tuple(links))


def _relative_target(item: Import):
    """The repository path a relative specifier names, or an unresolved result."""
    specifier = item.specifier
    if not (
        specifier in {".", ".."}
        or specifier.startswith("./")
        or specifier.startswith("../")
    ):
        return None, ImportResolution(
            item.id, "unresolved", reason="non_relative_specifier"
        )
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(item.file), specifier))
    if joined == ".." or joined.startswith("../"):
        return None, ImportResolution(
            item.id, "unresolved", reason="beyond_repository_root"
        )
    return joined, None


def _first(item: Import, candidates, files, rule):
    target = next((c for c in candidates if c in files), None)
    if target is None or target == item.file:
        return ImportResolution(item.id, "unresolved", reason="no_analyzed_file")
    return ImportResolution(item.id, "linked", (ImportLink(target, rule),))


def _typescript(item: Import, files):
    joined, unresolved = _relative_target(item)
    if unresolved:
        return unresolved
    stem, extension = posixpath.splitext(joined)
    if extension in _TS_JS_EXTENSIONS:
        # A ".js" specifier names the TypeScript source first, then the file.
        candidates = [stem + suffix for suffix in _TS_JS_EXTENSIONS[extension]]
        candidates.append(joined)
    elif joined.endswith((".ts", ".tsx")):
        candidates = [joined]
    else:
        base = "" if joined == "." else joined
        candidates = [
            *(base + suffix for suffix in _TS_EXTENSIONLESS),
            *((base + suffix).lstrip("/") for suffix in _TS_INDEX),
            *(base + suffix for suffix in _JS_EXTENSIONLESS),
            *((base + suffix).lstrip("/") for suffix in _JS_INDEX),
        ]
    return _first(item, candidates, files, TYPESCRIPT_RELATIVE_MODULE)


def _javascript(item: Import, files):
    joined, unresolved = _relative_target(item)
    if unresolved:
        return unresolved
    if posixpath.splitext(joined)[1] in _JS_EXTENSIONS:
        return _first(item, [joined], files, JAVASCRIPT_RELATIVE_FILE)
    # Extensionless specifiers follow the bundler/CommonJS convention; Node ES
    # modules require the extension, so the link is a convention match.
    base = "" if joined == "." else joined
    candidates = [
        *(base + suffix for suffix in _JS_EXTENSIONS),
        *((base + suffix).lstrip("/") for suffix in _JS_INDEX),
    ]
    return _first(item, candidates, files, JAVASCRIPT_EXTENSIONLESS_MODULE)


def _go_modules(result: AnalysisResult):
    """(module directory, module path) per go.mod, deepest directory first."""
    modules = [
        (posixpath.dirname(item.file), item.name)
        for item in result.packages
        if item.kind == "module"
    ]
    return sorted(modules, key=lambda m: (-len(m[0].split("/")) if m[0] else 0, m))


def _within(directory, file):
    return not directory or file.startswith(directory + "/")


def _go_packages(result: AnalysisResult):
    """Importable files per directory: non-test Go files, minus ``main``.

    ``main`` packages cannot be imported, so a build-ignored ``package main``
    generator beside a library does not make the directory ambiguous, and a
    directory with only ``main`` maps to no package.
    """
    names = defaultdict(lambda: defaultdict(list))
    for item in result.packages:
        if (
            item.kind == "package"
            and item.file.endswith(".go")
            and not item.file.endswith("_test.go")
        ):
            names[posixpath.dirname(item.file)][item.name].append(item.file)
    packages = {}
    for directory, by_name in names.items():
        packages[directory] = {n: f for n, f in by_name.items() if n != "main"}
    return packages


def _go(item: Import, modules, packages):
    own = next(((d, p) for d, p in modules if _within(d, item.file)), None)
    if own is None:
        return ImportResolution(item.id, "unresolved", reason="go_module_path")
    directory, path = own
    specifier = item.specifier
    if specifier != path and not specifier.startswith(path + "/"):
        other = any(specifier == p or specifier.startswith(p + "/") for _, p in modules)
        return ImportResolution(
            item.id,
            "unresolved",
            reason="other_repository_module" if other else "outside_module",
        )
    target = posixpath.join(directory, specifier[len(path) :].lstrip("/"))
    target = posixpath.normpath(target) if target else ""
    target = "" if target == "." else target
    # A nested module owns its own tree; the import cannot reach into it.
    if any(
        d != directory and _within(directory, d + "/") and _within(d, target + "/")
        for d, _ in modules
    ):
        return ImportResolution(item.id, "unresolved", reason="other_repository_module")
    by_name = packages.get(target)
    if not by_name:
        reason = "no_analyzed_file" if by_name is None else "main_package"
        return ImportResolution(item.id, "unresolved", reason=reason)
    if len(by_name) > 1:
        return ImportResolution(
            item.id,
            "ambiguous",
            reason="multiple_packages",
            candidates=tuple(sorted(f for files in by_name.values() for f in files)),
        )
    files = sorted(next(iter(by_name.values())))
    return ImportResolution(
        item.id,
        "linked",
        tuple(ImportLink(file, GO_MODULE_PACKAGE) for file in files),
    )


def _java_index(result: AnalysisResult):
    """Java packages and importable types, each mapped to source root -> files.

    A file's source root is its directory above the path its package names
    (``src/main/java`` for ``src/main/java/a/b/C.java`` in package ``a.b``).
    Files outside such a path, or in the unnamed package, are not indexed:
    javac could not find them by name. A type is importable when every
    enclosing declaration is a type too, so local classes are not. A package
    maps to the files declaring a type in it.
    """
    java = {i.file for i in result.coverage if i.language == "java"}
    roots, packages, types = {}, defaultdict(dict), defaultdict(dict)
    for clause in result.packages:
        directory = posixpath.dirname(clause.file)
        path = clause.name.replace(".", "/")
        if clause.file not in java or not (
            directory == path or directory.endswith("/" + path)
        ):
            continue
        root = directory[: len(directory) - len(path)].rstrip("/")
        roots[clause.file] = root
    declared = defaultdict(dict)
    for d in result.definitions:
        if d.file in roots:
            declared[d.file].setdefault(d.symbol, set()).add(d.kind)
    for clause in result.packages:
        root = roots.get(clause.file)
        if root is None:
            continue
        symbols = declared[clause.file]
        for symbol, kinds in symbols.items():
            parts = symbol.split(".")
            if all(
                symbols.get(".".join(parts[:i]), set()) & _JAVA_TYPES
                for i in range(1, len(parts) + 1)
            ):
                files = types[clause.name + "." + symbol].setdefault(root, [])
                files.append(clause.file)
                # A package's files are those declaring a type in it;
                # package-info.java declares none.
                packages[clause.name].setdefault(root, []).append(clause.file)
    return roots, packages, types


def _java(item: Import, roots, packages, types):
    """Link a Java import to the files declaring the type or package it names.

    A wildcard names a package (every file of it) or, failing that, a type; a
    package and a type cannot share a name. Candidates in one source root
    resolve; across several roots, the importer's own root is preferred.
    """
    by_root = packages.get(item.specifier) if item.names == ("*",) else None
    package = bool(by_root)
    if not package:
        by_root = types.get(item.specifier)
    if not by_root:
        return ImportResolution(item.id, "unresolved", reason="not_in_repository")
    own = roots.get(item.file)
    if len(by_root) == 1:
        files, rule = next(iter(by_root.values())), JAVA_PACKAGE_MEMBER
    elif own in by_root:
        files, rule = by_root[own], JAVA_SAME_SOURCE_ROOT
    else:
        return ImportResolution(
            item.id,
            "ambiguous",
            reason="multiple_source_roots",
            candidates=tuple(sorted(f for fs in by_root.values() for f in fs)),
        )
    files = sorted(set(files))
    if not package and len(files) > 1:
        # One type declared by several files of the same source root.
        return ImportResolution(
            item.id,
            "ambiguous",
            reason="multiple_declarations",
            candidates=tuple(files),
        )
    return ImportResolution(
        item.id, "linked", tuple(ImportLink(file, rule) for file in files)
    )


def resolve_imports(result: AnalysisResult) -> tuple[ImportResolution, ...]:
    """One resolution per import fact, in import identity order."""
    files = {item.file for item in result.coverage}
    languages = {item.file: item.language for item in result.coverage}
    roots = ("",) + (("src",) if any(f.startswith("src/") for f in files) else ())
    modules, packages = _go_modules(result), _go_packages(result)
    java = _java_index(result)
    resolutions = []
    for item in sorted(result.imports, key=lambda value: value.id):
        language = languages[item.file]
        if language == "python":
            resolutions.append(_python(item, files, roots))
        elif language == "javascript":
            resolutions.append(_javascript(item, files))
        elif language == "go":
            resolutions.append(_go(item, modules, packages))
        elif language == "java":
            resolutions.append(_java(item, *java))
        else:
            resolutions.append(_typescript(item, files))
    return tuple(resolutions)
