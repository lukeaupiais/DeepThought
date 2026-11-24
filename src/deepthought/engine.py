"""Local full-snapshot extraction, independent of storage and host platforms."""

from hashlib import sha256
from fnmatch import fnmatchcase
from importlib.metadata import version
import json
import os
import posixpath
from pathlib import Path
import stat
from threading import Event

from dataclasses import dataclass

from .models import (
    AnalysisResult,
    CallSite,
    Definition,
    Diagnostic,
    FileCoverage,
    ImplBlock,
    Import,
    PackageClause,
    Supertype,
    is_relative_file,
)
from .syntax import (
    DEFAULT_LANGUAGES,
    LANGUAGES,
    extract,
    extract_c,
    extract_c_call_sites,
    extract_cpp,
    extract_csharp,
    extract_php,
    extract_ruby,
    extract_call_sites,
    extract_cargo_manifest,
    extract_go_module,
    extract_package_entries,
    extract_script_config,
    extract_impls,
    extract_imports,
    extract_packages,
    extract_supertypes,
    make_parser,
    parse_source,
    rust_macro_rules,
    RustExpander,
)
from ._version import __version__

# Diagnostics a parsed file can produce; any other diagnostic for a recorded
# file means the file was not fully parsed, so its facts are not reused.
_PARSE_DIAGNOSTICS = {"syntax_error"}
_SCRIPT_MANIFESTS = frozenset({"tsconfig.json", "jsconfig.json", "package.json"})
# Fact kinds this engine records; part of snapshot identity, and an earlier
# analysis is reused only if it recorded all of them.
CAPABILITIES = (
    "call_sites",
    "definitions",
    "impls",
    "imports",
    "packages",
    "supertypes",
)


@dataclass(frozen=True)
class _FileFacts:
    coverage: FileCoverage
    diagnostics: tuple[Diagnostic, ...]
    definitions: tuple[Definition, ...]
    call_sites: tuple[CallSite, ...]
    imports: tuple[Import, ...]
    supertypes: tuple[Supertype, ...]
    packages: tuple[PackageClause, ...]
    impls: tuple[ImplBlock, ...]
    versions: dict

    def matches(self, language, digest, versions):
        """Same bytes, language and grammar packages as the recorded parse."""
        return (
            self.coverage.language == language
            and self.coverage.digest == digest
            and all(
                versions.get(name) == value for name, value in self.versions.items()
            )
        )


# The analyzer input naming the Rust macros whose invocations are read as the
# items they are given (see syntax.RustExpander), comma-separated.
RUST_ITEM_MACROS = "rust-item-macros"


def _rust_item_macros(root, include, exclude, max_file_bytes):
    """Names of the macros every ``macro_rules!`` definition of which, in the
    selected Rust sources, passes items through unchanged."""
    found = {}
    parser = None
    for directory, dirs, names, directory_fd in os.fwalk(root, follow_symlinks=False):
        dirs[:] = sorted(
            d for d in dirs if d not in {".git", ".venv", "node_modules", "__pycache__"}
        )
        for name in sorted(names):
            relative = (Path(directory) / name).relative_to(root).as_posix()
            if (
                not name.endswith(".rs")
                or not any(fnmatchcase(relative, pattern) for pattern in include)
                or any(fnmatchcase(relative, pattern) for pattern in exclude)
            ):
                continue
            try:
                fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
                with os.fdopen(fd, "rb") as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        continue
                    content = source.read(max_file_bytes + 1)
            except OSError:
                continue
            if len(content) > max_file_bytes or b"macro_rules" not in content:
                continue
            parser = parser or make_parser("rust", ".rs")
            for macro, through in rust_macro_rules(parser.parse(content), content):
                found.setdefault(macro, set()).add(through)
    return ",".join(sorted(name for name, kinds in found.items() if kinds == {True}))


def reusable_files(previous, repository_id):
    """Per-file facts of ``previous`` that may stand in for parsing a file.

    Facts are reused only from a schema 3 tree-sitter analysis of the same
    repository identity by this engine version (identities include both), and
    only for files that were fully parsed. Returns (facts by file, reason the
    whole analysis was rejected or None).
    """
    if previous is None:
        return {}, None
    if not isinstance(previous, AnalysisResult):
        raise ValueError("previous must be an AnalysisResult")
    recorded = dict(previous.analyzer_versions)
    if previous.analyzer != "tree-sitter" or previous.schema_version != "3":
        return {}, "not_a_tree_sitter_schema_3_analysis"
    if previous.repository_id != repository_id:
        return {}, "different_repository_id"
    if recorded.get("deepthought-analysis-engine") != __version__:
        return {}, "different_engine_version"
    if previous.coordinate_encoding != "utf-8":
        return {}, "different_coordinate_encoding"
    if not set(CAPABILITIES) <= set(previous.capabilities):
        return {}, "missing_fact_kinds"
    grouped = {}
    for kind, items in (
        ("diagnostics", previous.diagnostics),
        ("definitions", previous.definitions),
        ("call_sites", previous.call_sites),
        ("imports", previous.imports),
        ("supertypes", previous.supertypes),
        ("packages", previous.packages),
        ("impls", previous.impls),
    ):
        for item in items:
            grouped.setdefault(item.file, {}).setdefault(kind, []).append(item)
    facts = {}
    for item in previous.coverage:
        own = grouped.get(item.file, {})
        diagnostics = tuple(own.get("diagnostics", ()))
        if item.status not in {"analyzed", "partial"} or item.digest is None:
            continue
        if any(d.code not in _PARSE_DIAGNOSTICS for d in diagnostics):
            continue
        language = LANGUAGES.get(item.language)
        if language is None:
            continue
        packages = ("tree-sitter", language[1].replace("_", "-"))
        if posixpath.basename(item.file) in _SCRIPT_MANIFESTS:
            packages = ()
        if item.language == "rust":
            # Rust facts depend on which macros pass items through.
            packages += (RUST_ITEM_MACROS,)
        if any(name not in recorded for name in packages):
            continue
        facts[item.file] = _FileFacts(
            item,
            diagnostics,
            tuple(own.get("definitions", ())),
            tuple(own.get("call_sites", ())),
            tuple(own.get("imports", ())),
            tuple(own.get("supertypes", ())),
            tuple(own.get("packages", ())),
            tuple(own.get("impls", ())),
            {name: recorded[name] for name in packages},
        )
    return facts, None


def analyze_repository(
    path: str | Path,
    *,
    repository_id: str,
    languages: tuple[str, ...] = DEFAULT_LANGUAGES,
    max_file_bytes: int = 2_000_000,
    cancel_event: Event | None = None,
    max_files: int = 10_000,
    max_total_bytes: int = 100_000_000,
    include: tuple[str, ...] = ("*",),
    exclude: tuple[str, ...] = (),
    previous: AnalysisResult | None = None,
    reuse: dict | None = None,
) -> AnalysisResult:
    """Extract named declarations; cancellation is cooperative between files.

    With ``previous``, a file whose bytes equal a file that analysis recorded
    keeps that analysis's facts instead of being parsed again. Every file is
    still read and checked against the limits, so the result equals a full run
    (see reusable_files). ``reuse``, when given, receives the counts of reused
    and parsed files and why ``previous`` was rejected, if it was.
    """
    root = Path(path).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Workspace must be a directory")
    if not repository_id.strip():
        raise ValueError("repository_id must be nonempty")
    if not languages or set(languages) - LANGUAGES.keys():
        raise ValueError(
            "Supported languages: c, cpp, go, java, javascript, php, python, ruby, "
            "rust, typescript"
        )
    if max_file_bytes < 1:
        raise ValueError("max_file_bytes must be positive")
    if max_files < 1 or max_total_bytes < 1:
        raise ValueError("File and total-byte limits must be positive")
    languages = tuple(sorted(set(languages)))
    definitions, imports, coverage, diagnostics = [], [], [], []
    call_sites = []
    supertypes = []
    packages = []
    impls = []
    parsers, versions = {}, {"deepthought-analysis-engine": __version__}
    if "rust" in languages:
        try:
            versions[RUST_ITEM_MACROS] = _rust_item_macros(
                root, include, exclude, max_file_bytes
            )
        except ImportError:
            pass  # The grammar is missing: each Rust file reports it.
    item_macros = [m for m in versions.get(RUST_ITEM_MACROS, "").split(",") if m]
    reusable, rejected = reusable_files(previous, repository_id)
    stats = {"reused": 0, "parsed": 0, "previous_rejected": rejected}
    cancelled = False
    stop = False
    processed = total_bytes = 0

    def directory_error(exc):
        diagnostics.append(
            Diagnostic(
                "directory_unreadable", "Could not enumerate a selected directory"
            )
        )

    for directory, dirs, names, directory_fd in os.fwalk(
        root, follow_symlinks=False, onerror=directory_error
    ):
        if stop:
            break
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        dirs[:] = sorted(
            d for d in dirs if d not in {".git", ".venv", "node_modules", "__pycache__"}
        )
        for name in sorted(names):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            relative = (Path(directory) / name).relative_to(root).as_posix()
            if not any(fnmatchcase(relative, pattern) for pattern in include) or any(
                fnmatchcase(relative, pattern) for pattern in exclude
            ):
                continue
            suffix = Path(name).suffix
            language = next(
                (lang for lang in languages if suffix in LANGUAGES[lang][0]), None
            )
            if suffix == ".h" and "cpp" in languages:
                # A header is read as C++ when C++ is selected: C++ code
                # includes it, and the C++ grammar reads C declarations too.
                language = "cpp"
            if name == "go.mod" and "go" in languages:
                # Go module files declare the import path prefix of their tree.
                language = "go"
            if name == "Cargo.toml" and "rust" in languages:
                # Cargo manifests declare the crates of their package.
                language = "rust"
            script = next(
                (
                    lang
                    for lang in (
                        ("typescript", "javascript")
                        if name == "tsconfig.json"
                        else ("javascript", "typescript")
                    )
                    if lang in languages
                ),
                None,
            )
            if name in _SCRIPT_MANIFESTS and script is not None:
                # TypeScript and jsconfig projects map bare specifiers by
                # their paths, and package.json names a directory's entry.
                language = script
            if language is None:
                continue
            if not is_relative_file(relative):
                diagnostics.append(
                    Diagnostic(
                        "invalid_source_path",
                        "Selected source path cannot be represented in a portable artifact",
                    )
                )
                continue
            if processed >= max_files:
                diagnostics.append(
                    Diagnostic(
                        "file_count_limit", "Analysis reached max_files", relative
                    )
                )
                stop = True
                break
            processed += 1
            digest = None
            try:
                fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
                with os.fdopen(fd, "rb") as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise ValueError("Source must be a regular file")
                    content = source.read(
                        min(max_file_bytes + 1, max_total_bytes - total_bytes + 1)
                    )
                digest = sha256(content).hexdigest()
                total_bytes += len(content)
                if total_bytes > max_total_bytes:
                    diagnostics.append(
                        Diagnostic(
                            "total_byte_limit",
                            "Analysis reached max_total_bytes",
                            relative,
                        )
                    )
                    coverage.append(FileCoverage(relative, language, "skipped"))
                    stop = True
                    break
                if len(content) > max_file_bytes:
                    coverage.append(FileCoverage(relative, language, "skipped"))
                    diagnostics.append(
                        Diagnostic(
                            "file_limit", "Source exceeds max_file_bytes", relative
                        )
                    )
                    continue
                content.decode("utf-8")
                if name == "go.mod":
                    module = extract_go_module(content, repository_id, relative)
                    coverage.append(
                        FileCoverage(
                            relative,
                            language,
                            "analyzed" if module else "partial",
                            digest,
                        )
                    )
                    if not module:
                        diagnostics.append(
                            Diagnostic(
                                "syntax_error",
                                "go.mod declares no module path",
                                relative,
                            )
                        )
                    packages.extend(module)
                    continue
                if name == "Cargo.toml":
                    crates = extract_cargo_manifest(content, repository_id, relative)
                    coverage.append(
                        FileCoverage(
                            relative,
                            language,
                            "analyzed" if crates is not None else "partial",
                            digest,
                        )
                    )
                    if crates is None:
                        diagnostics.append(
                            Diagnostic(
                                "syntax_error", "Cargo.toml is not valid TOML", relative
                            )
                        )
                    imports.extend(crates or ())
                    continue
                if name in _SCRIPT_MANIFESTS and language == script:
                    read_manifest = (
                        extract_package_entries
                        if name == "package.json"
                        else extract_script_config
                    )
                    previous_facts = reusable.get(relative)
                    if (
                        previous_facts is not None
                        and previous_facts.coverage.language == language
                        and previous_facts.coverage.digest == digest
                    ):
                        # Reading a manifest depends on no grammar package.
                        stats["reused"] += 1
                        coverage.append(previous_facts.coverage)
                        diagnostics.extend(previous_facts.diagnostics)
                        imports.extend(previous_facts.imports)
                        continue
                    stats["parsed"] += 1
                    facts, readable = read_manifest(content, repository_id, relative)
                    coverage.append(
                        FileCoverage(
                            relative,
                            language,
                            "analyzed" if readable else "partial",
                            digest,
                        )
                    )
                    if not readable:
                        diagnostics.append(
                            Diagnostic(
                                "syntax_error", f"{name} is not valid JSON", relative
                            )
                        )
                    imports.extend(facts)
                    continue
                key = (language, suffix)
                if key not in parsers:
                    parsers[key] = make_parser(language, suffix)
                    for package in (
                        "tree-sitter",
                        LANGUAGES[language][1].replace("_", "-"),
                    ):
                        versions[package] = version(package)
                facts = reusable.get(relative)
                if facts is not None and facts.matches(language, digest, versions):
                    stats["reused"] += 1
                    coverage.append(facts.coverage)
                    diagnostics.extend(facts.diagnostics)
                    definitions.extend(facts.definitions)
                    call_sites.extend(facts.call_sites)
                    imports.extend(facts.imports)
                    supertypes.extend(facts.supertypes)
                    packages.extend(facts.packages)
                    impls.extend(facts.impls)
                    continue
                stats["parsed"] += 1
                tree = parse_source(parsers[key], language, suffix, content)
                if tree.root_node.has_error:
                    coverage.append(
                        FileCoverage(
                            relative, language, "partial", sha256(content).hexdigest()
                        )
                    )
                    diagnostics.append(
                        Diagnostic(
                            "syntax_error", "Source contains syntax errors", relative
                        )
                    )
                else:
                    coverage.append(
                        FileCoverage(
                            relative, language, "analyzed", sha256(content).hexdigest()
                        )
                    )
                if language == "c":
                    # C: declarations, includes and calls.
                    declared = extract_c(tree, content, repository_id, relative)
                    definitions.extend(declared)
                    imports.extend(
                        extract_imports(tree, content, repository_id, relative, "c")
                    )
                    call_sites.extend(
                        extract_c_call_sites(
                            tree, content, repository_id, relative, declared
                        )
                    )
                    continue
                if language == "cpp":
                    # C++: declarations only so far.
                    definitions.extend(
                        extract_cpp(tree, content, repository_id, relative)
                    )
                    continue
                if language == "ruby":
                    # Ruby: declarations only so far.
                    definitions.extend(
                        extract_ruby(tree, content, repository_id, relative)
                    )
                    continue
                if language == "csharp":
                    # C#: declarations only so far.
                    definitions.extend(
                        extract_csharp(tree, content, repository_id, relative)
                    )
                    continue
                if language == "php":
                    # PHP: declarations and imports so far.
                    definitions.extend(
                        extract_php(tree, content, repository_id, relative)
                    )
                    imports.extend(
                        extract_imports(tree, content, repository_id, relative, "php")
                    )
                    continue
                expander = (
                    RustExpander(content, item_macros) if language == "rust" else None
                )
                declared = extract(
                    tree, content, repository_id, relative, expander=expander
                )
                definitions.extend(declared)
                imports.extend(
                    extract_imports(
                        tree,
                        content,
                        repository_id,
                        relative,
                        language,
                        expander=expander,
                    )
                )
                call_sites.extend(
                    extract_call_sites(
                        tree,
                        content,
                        repository_id,
                        relative,
                        declared,
                        expander=expander,
                    )
                )
                if language == "rust":
                    # Rust: a trait's supertraits are supertypes; impls are
                    # their own facts.
                    supertypes.extend(
                        extract_supertypes(
                            tree,
                            content,
                            repository_id,
                            relative,
                            declared,
                            expander=expander,
                        )
                    )
                    impls.extend(
                        extract_impls(
                            tree, content, repository_id, relative, expander=expander
                        )
                    )
                    continue
                supertypes.extend(
                    extract_supertypes(tree, content, repository_id, relative, declared)
                )
                if language in {"go", "java"}:
                    packages.extend(
                        extract_packages(tree, content, repository_id, relative)
                    )
            except ImportError:
                coverage.append(FileCoverage(relative, language, "unavailable", digest))
                diagnostics.append(
                    Diagnostic(
                        "grammar_unavailable",
                        f"Provision the {language} grammar before analysis",
                        relative,
                    )
                )
            except (OSError, UnicodeError, ValueError) as exc:
                coverage.append(FileCoverage(relative, language, "failed", digest))
                diagnostics.append(Diagnostic("source_unreadable", str(exc), relative))
    cancelled = cancelled or (cancel_event is not None and cancel_event.is_set())
    status = (
        "cancelled"
        if cancelled
        else (
            "succeeded" if not diagnostics else ("partial" if definitions else "failed")
        )
    )
    manifest = {
        "repository_id": repository_id,
        "status": status,
        "diagnostics": [vars(item) for item in diagnostics],
        "files": [vars(item) for item in coverage],
        "languages": languages,
        "versions": sorted(versions.items()),
        "analyzer_version": __version__,
        "capabilities": CAPABILITIES,
        "max_file_bytes": max_file_bytes,
        "max_files": max_files,
        "max_total_bytes": max_total_bytes,
        "include": include,
        "exclude": exclude,
    }
    snapshot = sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    if reuse is not None:
        reuse.update(stats)
    return AnalysisResult(
        repository_id,
        snapshot,
        status,
        tuple(definitions),
        tuple(coverage),
        tuple(diagnostics),
        "tree-sitter",
        tuple(sorted(versions.items())),
        capabilities=CAPABILITIES,
        schema_version="3",
        imports=tuple(imports),
        call_sites=tuple(call_sites),
        supertypes=tuple(supertypes),
        packages=tuple(packages),
        impls=tuple(impls),
    )
