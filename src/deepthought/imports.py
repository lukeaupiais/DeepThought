"""Resolve static import facts to analyzed repository files, by declared rules.

Resolution reads no files: it matches specifiers against the coverage of one
AnalysisResult. Every link names the rule that produced it. A specifier that no
rule can tie to exactly one analyzed file stays unresolved or ambiguous, with a
reason, and produces no link.
"""

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


def resolve_imports(result: AnalysisResult) -> tuple[ImportResolution, ...]:
    """One resolution per import fact, in import identity order."""
    files = {item.file for item in result.coverage}
    languages = {item.file: item.language for item in result.coverage}
    roots = ("",) + (("src",) if any(f.startswith("src/") for f in files) else ())
    resolutions = []
    for item in sorted(result.imports, key=lambda value: value.id):
        language = languages[item.file]
        if language == "python":
            resolutions.append(_python(item, files, roots))
        elif language == "javascript":
            resolutions.append(_javascript(item, files))
        elif language == "go":
            # Go imports name packages by module path, which go.mod defines;
            # the analysis does not read it, so no file is claimed.
            resolutions.append(
                ImportResolution(item.id, "unresolved", reason="go_module_path")
            )
        else:
            resolutions.append(_typescript(item, files))
    return tuple(resolutions)
