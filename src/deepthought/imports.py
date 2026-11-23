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
RUST_MODULE_FILE = "tree-sitter.rust-module-file"
RUST_CRATE_ROOT = "tree-sitter.rust-crate-root"
RUST_USE_DECLARED = "tree-sitter.rust-use-declared"
RUST_USE_MODULE = "tree-sitter.rust-use-module"
RUST_EXTERN_CRATE = "tree-sitter.rust-extern-crate"
RUST_USE_REEXPORT = "tree-sitter.rust-use-reexport"
_RUST_MODULE_KINDS = frozenset({"module", "module_path"})
_RUST_CRATE_KINDS = frozenset({"crate_library", "crate_target"})
# Declarations a use path can go through: modules and types.
_RUST_TYPE_KINDS = frozenset(
    {"Module", "Struct", "Enum", "Union", "Trait", "TypeAlias"}
)
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


# Directories whose files can be crate roots by Cargo's defaults
# (src/bin/*.rs, and examples/*.rs, tests/*.rs, benches/*.rs in a package).
_RUST_TARGET_DIRS = {"bin", "examples", "tests", "benches"}
_RUST_PACKAGE_TARGETS = ("tests", "benches", "examples")


def _rust_candidates(item: Import, roots_of_crate: bool | None):
    """Files a Rust ``mod name;`` may name.

    A crate root or ``mod.rs`` declares its modules beside it; any other file
    ``a.rs`` declares them in ``a/``. ``roots_of_crate`` says whether the
    declaring file is a crate root; when None, a file directly in a Cargo
    target directory may be either. A module ``m`` is ``m.rs`` or
    ``m/mod.rs`` there; a ``#[path]`` names its file from the declaring
    file's directory (inside inline modules, from the module directory).
    """
    directory, base = posixpath.split(item.file)
    stem = base.removesuffix(".rs")
    nested = posixpath.join(directory, stem)
    if stem in {"lib", "main", "mod"} or base == "build.rs" or roots_of_crate:
        bases = [directory]
    elif posixpath.basename(directory) in _RUST_TARGET_DIRS:
        bases = {True: [directory], False: [nested]}.get(
            roots_of_crate, [directory, nested]
        )
    else:
        bases = [nested]
    if item.kind == "module_path":
        # Outside inline modules a path is read from the file's directory;
        # inside them, from the module directory and the inline modules.
        roots = (
            [posixpath.join(b, *item.names) for b in bases]
            if item.names
            else [directory]
        )
        return [posixpath.normpath(posixpath.join(r, item.specifier)) for r in roots]
    return [
        posixpath.join(r, item.specifier) + suffix
        for r in bases
        for suffix in (".rs", "/mod.rs")
    ]


def _rust_target_dirs(files):
    """Cargo's target directories: ``src/bin``, and ``tests``, ``benches`` and
    ``examples`` outside any ``src`` (a package keeps them beside ``src``)."""
    targets = set()
    for f in files:
        parts = f.split("/")[:-1]
        if parts[-2:] == ["src", "bin"] or (
            parts and parts[-1] in _RUST_PACKAGE_TARGETS and "src" not in parts
        ):
            targets.add("/".join(parts))
    return targets


def _rust_declared(items, files):
    """Files some ``mod`` declaration may name: these are modules, not roots."""
    return {c for item in items for c in _rust_candidates(item, None) if c in files}


def _rust_crate_root(item: Import):
    """The file a Cargo.toml target names, from the manifest's directory."""
    return posixpath.normpath(
        posixpath.join(posixpath.dirname(item.file), item.specifier)
    )


def _rust(item: Import, files, declared, targets, crate_roots):
    """A Rust ``mod name;``: the file Cargo's module layout gives it; a
    Cargo.toml target: its crate root file.

    A file a Cargo.toml target names is a crate root. A file in a target
    directory that no ``mod`` declaration names is one too; one that is named
    is read as the module it is declared as.
    """
    if item.kind in _RUST_CRATE_KINDS:
        path = _rust_crate_root(item)
        if path in files:
            return ImportResolution(
                item.id, "linked", (ImportLink(path, RUST_CRATE_ROOT),)
            )
        return ImportResolution(item.id, "unresolved", reason="crate_root_missing")
    root = item.file in crate_roots or (
        item.file not in declared and posixpath.dirname(item.file) in targets
    )
    candidates = _rust_candidates(item, root)
    found = sorted({c for c in candidates if c in files})
    if len(found) == 1:
        return ImportResolution(
            item.id, "linked", (ImportLink(found[0], RUST_MODULE_FILE),)
        )
    if found:
        return ImportResolution(
            item.id,
            "ambiguous",
            reason="module_file_ambiguous",
            candidates=tuple(found),
        )
    return ImportResolution(item.id, "unresolved", reason="module_file_missing")


class _RustIndex:
    """The module trees of a repository's Rust crates.

    Crate roots are the files Cargo.toml targets name, and ``lib.rs``,
    ``main.rs`` and ``build.rs`` files and files directly in a target
    directory that no ``mod`` declaration includes. Each file reached from a
    root through ``mod`` declarations has a module path in that crate; inline
    modules extend the path of their file. A file can be in several crates.
    """

    def __init__(self, result: AnalysisResult, files, languages):
        rust = [i for i in result.imports if languages[i.file] == "rust"]
        mods = [i for i in rust if i.kind in _RUST_MODULE_KINDS]
        crates = [i for i in rust if i.kind in _RUST_CRATE_KINDS]
        declared = _rust_declared(mods, files)
        sources = {f for f, lang in languages.items() if lang == "rust"}
        targets = _rust_target_dirs(sources)
        crate_roots = {_rust_crate_root(i) for i in crates}
        self.resolutions = {
            i.id: _rust(i, files, declared, targets, crate_roots) for i in mods + crates
        }
        # Library crate name -> root files.
        self.libraries = {}
        for i in crates:
            links = self.resolutions[i.id].links
            if i.kind == "crate_library" and links:
                self.libraries.setdefault(i.local_names[0], set()).add(
                    links[0].target_file
                )
        # Bodyless modules whose file is not known: (file, inline, name).
        # Bodyless modules: (file, inline modules and name).
        children, included, self.unlinked, bodyless = {}, set(), set(), set()
        for i in mods:
            links = self.resolutions[i.id].links
            inline = (
                i.names
                if i.kind == "module_path"
                else tuple(i.specifier.split("/")[:-1])
            )
            bodyless.add((i.file, inline + i.local_names))
            if not links:
                self.unlinked.add((i.file, inline, i.local_names[0]))
                continue
            child = links[0].target_file
            children.setdefault(i.file, []).append((inline + i.local_names, child))
            included.add(child)
        roots = crate_roots | {
            f
            for f in sources
            if f.endswith(".rs")
            and f not in included
            and (
                posixpath.basename(f) in {"lib.rs", "main.rs", "build.rs"}
                or posixpath.dirname(f) in targets
            )
        }
        # Names declared in each (file, inline modules) scope, those that can
        # start a path (modules and types), and the inline modules of each file.
        self.items, self.types, self.kinds, inline = {}, {}, {}, {}
        for d in result.definitions:
            if languages.get(d.file) != "rust":
                continue
            parts = tuple(d.symbol.split("."))
            self.items.setdefault((d.file, parts[:-1]), set()).add(parts[-1])
            if d.kind != "Module":
                names = self.kinds.setdefault((d.file, parts[:-1]), {})
                names.setdefault(parts[-1], set()).add(d.kind)
            if d.kind in _RUST_TYPE_KINDS:
                self.types.setdefault((d.file, parts[:-1]), set()).add(parts[-1])
            if d.kind == "Module" and (d.file, parts) not in bodyless:
                inline.setdefault(d.file, set()).add(parts)
        # Names other uses bind in each (file, inline modules) scope, and the
        # globs there.
        self.bindings, self.globs, self.externs = {}, {}, {}
        for i in rust:
            if i.kind == "extern_crate":
                scope = self.externs.setdefault((i.file, i.names), {})
                scope.setdefault(i.local_names[0], []).append(i)
            elif i.kind == "use":
                scope = self.bindings.setdefault((i.file, i.names), {})
                scope.setdefault(i.local_names[0], []).append(i)
            elif i.kind == "use_glob":
                self.globs.setdefault((i.file, i.names), []).append(i)
        # (crate root, module path) -> {(file, inline modules)}, and
        # file -> {(crate root, module path of the file)}.
        self.modules, self.contexts = {}, {}
        for root in sorted(roots):
            stack, seen = [(root, ())], set()
            while stack:
                file, path = stack.pop()
                if file in seen:
                    continue
                seen.add(file)
                self.contexts.setdefault(file, set()).add((root, path))
                self.modules.setdefault((root, path), set()).add((file, ()))
                for scope in inline.get(file, ()):
                    # A module inside a function is never reached: a path
                    # goes through modules one at a time.
                    self.modules.setdefault((root, path + scope), set()).add(
                        (file, scope)
                    )
                for relative, child in children.get(file, ()):
                    stack.append((child, path + relative))

    def resolve(self, item: Import):
        if item.kind not in {"use", "use_glob", "extern_crate"}:
            return self.resolutions[item.id]
        contexts = self.contexts.get(item.file)
        if not contexts:
            return ImportResolution(
                item.id, "unresolved", reason="module_context_unknown"
            )
        outcomes = {self._resolve(item, root, path) for root, path in contexts}
        if len(outcomes) == 1:
            status, reason, links, candidates = outcomes.pop()
            return ImportResolution(item.id, status, links, reason, candidates)
        found = {
            f
            for _, _, links, candidates in outcomes
            for f in (*(link.target_file for link in links), *candidates)
        }
        return ImportResolution(
            item.id,
            "ambiguous",
            reason="use_context_ambiguous",
            candidates=tuple(sorted(found)),
        )

    def _library(self, name):
        """The root of the one workspace library named ``name``, or the
        unresolved outcome."""
        roots = self.libraries.get(name, set())
        if len(roots) == 1:
            return next(iter(roots)), None
        if roots:
            return None, ("ambiguous", "crate_ambiguous", (), tuple(sorted(roots)))
        return None, ("unresolved", "external_crate", (), ())

    def _walk(self, item: Import, root, path):
        """Where a use path leads in one crate's module: the failure outcome,
        or (crate root, module path, remaining names)."""
        module = path + item.names
        segments = item.specifier.split("::")
        first = segments[0]
        if first == "":
            # `::name`: a crate.
            library, failed = self._library(segments[1])
            if failed:
                return failed
            root, module, rest = library, (), segments[2:]
        elif first == "crate":
            module, rest = (), segments[1:]
        elif first in {"self", "super"}:
            rest = segments[1:] if first == "self" else segments
            while rest and rest[0] == "super":
                if not module:
                    return "unresolved", "super_outside_crate", (), ()
                module, rest = module[:-1], rest[1:]
        elif (root, module + (first,)) in self.modules or any(
            first in self.types.get(scope, ())
            for scope in self.modules.get((root, module), ())
        ):
            rest = segments
        else:
            # A crate: an `extern crate` in the crate root names it there.
            renamed = self.externs.get((root, ()), {}).get(first, ())
            name = renamed[0].specifier if renamed else first
            library, failed = self._library(name)
            if failed:
                return failed
            root, module, rest = library, (), segments[1:]
        return root, module, rest

    def _follow(self, root, module, rest, visiting):
        """Every meaning of a path from a module, in every configuration.

        A name in a module can be a child module, a declared item, a name a
        use there binds (followed), or, when none of these, a name a glob
        use supplies. Declarations behind different ``#[cfg]``s can give a
        name several meanings; all are kept. Returns (meanings, failures):
        a meaning is (what, file, root, module, followed) where what is
        "module" (the path names the module in file), "item" (file declares
        the name) or "undeclared" (file's module neither declares nor binds
        it); followed says whether a use was followed on the way.
        """
        meanings, failures = set(), []
        # Each state has fewer names left than the one it came from.
        states = [(root, module, tuple(rest), False)]
        while states:
            root, module, rest, followed = states.pop()
            scopes = sorted(self.modules.get((root, module), ()))
            if not scopes:
                # A use in a module inside a function body.
                failures.append(("unresolved", "module_missing", (), ()))
                continue
            if not rest:
                meanings.update(
                    ("module", file, root, module, followed) for file, _ in scopes
                )
                continue
            name, last = rest[0], len(rest) == 1
            child = (root, module + (name,))
            for where in scopes:
                found = False
                if child in self.modules:
                    states.append((*child, rest[1:], followed))
                    found = True
                elif (*where, name) in self.unlinked:
                    failures.append(("unresolved", "module_missing", (), ()))
                    found = True
                kinds = self.kinds.get(where, {}).get(name, set())
                if kinds and (last or kinds & _RUST_TYPE_KINDS):
                    # A name inside an item (a variant, an associated item)
                    # is declared with it.
                    meanings.add(("item", where[0], root, module, followed))
                    found = True
                for meaning in self._bound(root, module, where, name, visiting):
                    found = True
                    if meaning[0] == "module":
                        states.append((meaning[2], meaning[3], rest[1:], True))
                    else:
                        meanings.add(meaning[:4] + (True,))
                if not found:
                    meanings.add(("undeclared", where[0], root, module, followed))
        return meanings, failures

    def _bound(self, root, module, where, name, visiting):
        """The meanings uses in a module bind ``name`` to: ``extern crate``
        and explicit uses, or, when there are none, globs of modules."""
        key = (root, module, name)
        if key in visiting:
            return set()
        visiting = visiting | {key}
        file, scope = where
        path = module[: len(module) - len(scope)]
        found = set()
        for item in self.externs.get(where, {}).get(name, ()):
            library, failed = self._library(item.specifier)
            if library:
                found.add(("module", library, library, (), True))
        for item in self.bindings.get(where, {}).get(name, ()):
            walked = self._walk(item, root, path)
            if len(walked) == 3:
                found |= self._follow(*walked, visiting)[0]
        if found:
            return found
        for item in self.globs.get(where, ()):
            walked = self._walk(item, root, path)
            if len(walked) != 3:
                continue
            for glob in self._follow(*walked, visiting)[0]:
                # Only a glob of a module: one of an enum's variants names
                # no declaration here.
                if glob[0] == "module":
                    supplied = self._follow(glob[2], glob[3], [name], visiting)[0]
                    found |= {m for m in supplied if m[0] != "undeclared"}
        return found

    def _resolve(self, item: Import, root, path):
        """(status, reason, links, candidates) of one use in one crate's
        module: a link to each file the path can mean."""
        if item.kind == "extern_crate":
            library, failed = self._library(item.specifier)
            if failed:
                return failed
            return "linked", None, (ImportLink(library, RUST_EXTERN_CRATE),), ()
        walked = self._walk(item, root, path)
        if len(walked) != 3:
            return walked
        meanings, failures = self._follow(*walked, frozenset())
        strength = {RUST_USE_DECLARED: 0, RUST_USE_REEXPORT: 1, RUST_USE_MODULE: 2}
        rules = {}
        for what, file, _, _, followed in meanings:
            rule = (
                RUST_USE_MODULE
                if what == "undeclared"
                else RUST_USE_REEXPORT
                if followed
                else RUST_USE_DECLARED
            )
            if file not in rules or strength[rule] < strength[rules[file]]:
                rules[file] = rule
        if not rules:
            return min(failures)
        links = tuple(ImportLink(file, rules[file]) for file in sorted(rules))
        return "linked", None, links, ()


def resolve_imports(result: AnalysisResult) -> tuple[ImportResolution, ...]:
    """One resolution per import fact, in import identity order."""
    files = {item.file for item in result.coverage}
    languages = {item.file: item.language for item in result.coverage}
    roots = ("",) + (("src",) if any(f.startswith("src/") for f in files) else ())
    modules, packages = _go_modules(result), _go_packages(result)
    java = _java_index(result)
    rust = _RustIndex(result, files, languages)
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
        elif language == "rust":
            resolutions.append(rust.resolve(item))
        else:
            resolutions.append(_typescript(item, files))
    return tuple(resolutions)
