"""Pinned SCIP indexers: the engine's primary source of symbol facts.

Each supported language is indexed by one pinned SCIP indexer. Indexers are
required dependencies: one that is missing, the wrong version, or failing is
reported per file, so a file with no index is never mistaken for a file
without references. No installation or network operation happens here;
``deepthought indexers`` lists what is needed.

Every index is written with a source receipt (``<index>.sources.json``) that
binds it to the bytes it was built from, as ``scip_indexing`` does for the
TypeScript-only v1 profile.
"""

from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from .models import is_relative_file

PROFILE_VERSION = 2

# Directories never read as sources: VCS data, caches and dependency trees
# that belong to another project.
_SKIP_DIRECTORIES = {".git", ".hg", ".svn", "__pycache__", ".bin"}


@dataclass(frozen=True)
class IndexerSpec:
    name: str
    version: str
    languages: tuple
    environment_variable: str
    # Column unit the indexer writes when a document leaves it unspecified.
    position_encoding: str
    source_suffixes: frozenset
    configuration_names: frozenset = field(default_factory=frozenset)
    # Dependency directories this language reads as inputs (TypeScript reads
    # installed types from node_modules); others skip them.
    reads_node_modules: bool = False

    @property
    def profile(self):
        return f"{self.name}-v{PROFILE_VERSION}"


INDEXERS = {
    spec.name: spec
    for spec in (
        IndexerSpec(
            "scip-typescript",
            "0.6.2",
            ("typescript", "javascript"),
            "DEEPTHOUGHT_SCIP_TYPESCRIPT",
            "utf-16",
            frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"}),
            frozenset(
                {
                    "package.json",
                    "tsconfig.json",
                    "jsconfig.json",
                    "yarn.lock",
                    "pnpm-lock.yaml",
                    "pnpm-workspace.yaml",
                    "package-lock.json",
                }
            ),
            reads_node_modules=True,
        ),
        IndexerSpec(
            "scip-python",
            "0.6.8",
            ("python",),
            "DEEPTHOUGHT_SCIP_PYTHON",
            # Pyright positions count UTF-16 code units, as LSP does.
            "utf-16",
            frozenset({".py", ".pyi"}),
            frozenset(
                {"pyproject.toml", "setup.cfg", "setup.py", "pyrightconfig.json"}
            ),
        ),
        IndexerSpec(
            "scip-go",
            "0.1.26",
            ("go",),
            "DEEPTHOUGHT_SCIP_GO",
            # go/token columns are byte offsets.
            "utf-8",
            frozenset({".go"}),
            frozenset({"go.mod", "go.sum", "go.work", "go.work.sum"}),
        ),
    )
}

LANGUAGE_INDEXERS = {
    language: spec for spec in INDEXERS.values() for language in spec.languages
}

# Source suffix -> language, for coverage of files no indexer has seen.
LANGUAGE_SUFFIXES = {
    ".py": "python",
    ".pyi": "python",
    ".go": "go",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
}


def _hash(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("SCIP input must be a regular non-symlink file")
    return sha256(path.read_bytes()).hexdigest()


def _digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def locate(spec, environ=None):
    """The indexer executable: ``$DEEPTHOUGHT_SCIP_*`` if set, else PATH."""
    environ = os.environ if environ is None else environ
    configured = environ.get(spec.environment_variable)
    if configured:
        path = Path(configured)
        return path if path.is_file() and os.access(path, os.X_OK) else None
    found = shutil.which(spec.name, path=environ.get("PATH"))
    return Path(found) if found else None


def _virtualenv(directory):
    return (Path(directory) / "pyvenv.cfg").is_file()


def capture_inputs(workspace, spec, excluded=()):
    """sha256 of every source and configuration file the indexer may read."""
    root = Path(workspace).resolve(strict=True)
    excluded = {Path(p).absolute() for p in excluded}
    files = {}
    for directory, names, filenames in os.walk(root, followlinks=False):
        names[:] = sorted(
            name
            for name in names
            if name not in _SKIP_DIRECTORIES
            and not (Path(directory) / name).is_symlink()
            and not _virtualenv(Path(directory) / name)
            and (spec.reads_node_modules or name != "node_modules")
        )
        for name in sorted(filenames):
            path = Path(directory) / name
            if path in excluded or path.is_symlink():
                continue
            if (
                path.suffix in spec.source_suffixes
                or name in spec.configuration_names
                or (spec.reads_node_modules and path.suffix == ".json")
                or (spec.name == "scip-python" and _requirements(name))
            ):
                files[path.relative_to(root).as_posix()] = _hash(path)
    return files


def _requirements(name):
    return name.startswith("requirements") and name.endswith(".txt")


def source_files(workspace, languages):
    """Repository-relative source files of the given languages, outside
    dependency trees and virtual environments."""
    root = Path(workspace).resolve(strict=True)
    found = {}
    for directory, names, filenames in os.walk(root, followlinks=False):
        names[:] = sorted(
            name
            for name in names
            if name not in _SKIP_DIRECTORIES
            and name != "node_modules"
            and not (Path(directory) / name).is_symlink()
            and not _virtualenv(Path(directory) / name)
        )
        for name in sorted(filenames):
            language = LANGUAGE_SUFFIXES.get(Path(name).suffix)
            if language in languages:
                path = (Path(directory) / name).relative_to(root).as_posix()
                found[path] = language
    return found


def _arguments(spec, root, output, project_name):
    if spec.name == "scip-typescript":
        argv = ["index", "--cwd", str(root), "--output", str(output)]
        argv.append("--no-progress-bar")
        package = root / "package.json"
        if (root / "pnpm-workspace.yaml").is_file():
            argv.append("--pnpm-workspaces")
        elif package.is_file() and "workspaces" in _json(package):
            argv.append("--yarn-workspaces")
        elif not (root / "tsconfig.json").is_file():
            # JavaScript and projects without a root tsconfig.
            argv.append("--infer-tsconfig")
        return argv, root
    if spec.name == "scip-python":
        # Pyright resolves `import pkg` through the interpreter's sys.path, so a
        # src-layout package that is not installed would resolve nowhere and
        # every call into it would be lost. Source roots go on PYTHONPATH.
        # A fixed project version keeps symbols stable across commits; the
        # default is the current git revision.
        return [
            "index",
            "--cwd",
            str(root),
            "--output",
            str(output),
            "--project-name",
            project_name,
            "--project-version",
            "0",
            "--quiet",
        ], root
    if spec.name == "scip-go":
        return [
            "--output",
            str(output),
            "--module-version",
            "0",
            "--quiet",
        ], root
    raise ValueError("Unknown indexer " + spec.name)


def python_source_roots(root):
    """Workspace-relative import roots: the workspace, and ``src`` if present."""
    return ["."] + (["src"] if (Path(root) / "src").is_dir() else [])


def _environment(spec, root, environ, platform=None):
    env = dict(os.environ if environ is None else environ)
    if spec.name == "scip-python":
        env["PYTHONPATH"] = os.pathsep.join(
            str((root / r).resolve()) for r in python_source_roots(root)
        )
    if spec.name == "scip-go":
        env.update(_go_environment(platform or GO_PLATFORM))
    return env


# The platform a Go module is compiled for unless a file builds only elsewhere;
# pinned so that the files indexed do not depend on the machine. Without cgo,
# so that no C toolchain is needed and none can change the result.
GO_PLATFORM = "linux/amd64"


def _go_environment(platform):
    goos, goarch = platform.split("/")
    return {"GOOS": goos, "GOARCH": goarch, "CGO_ENABLED": "0", "GOFLAGS": ""}


def _go_command(arguments, cwd, environ, platform=GO_PLATFORM):
    """Output of the go command, or None where it is missing or fails."""
    env = dict(os.environ if environ is None else environ)
    go = shutil.which("go", path=env.get("PATH"))
    if go is None:
        return None
    env.update(_go_environment(platform))
    try:
        result = subprocess.run(
            [go, *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def go_platforms(environ=None):
    """Every platform the go toolchain builds for: first-class ports first,
    then the rest, each group sorted; GO_PLATFORM excluded."""
    listed = _go_command(["tool", "dist", "list", "-json"], None, environ)
    if listed is None:
        return []
    ports = json.loads(listed)
    return [
        f"{p['GOOS']}/{p['GOARCH']}"
        for p in sorted(
            ports, key=lambda p: (not p.get("FirstClass"), p["GOOS"], p["GOARCH"])
        )
        if f"{p['GOOS']}/{p['GOARCH']}" != GO_PLATFORM
    ]


def go_package_files(root, module, platform, environ=None):
    """Workspace-relative Go files the go command compiles in ``module`` for
    ``platform`` (sources and tests), and those it ignores there because of
    build constraints."""
    base = root if module == "." else root / module
    template = (
        '{{.Dir}}\t{{join .GoFiles " "}} {{join .TestGoFiles " "}} '
        '{{join .XTestGoFiles " "}}\t{{join .IgnoredGoFiles " "}}'
    )
    listed = _go_command(
        ["list", "-e", "-f", template, "./..."], base, environ, platform
    )
    built, ignored = set(), set()
    for line in (listed or "").splitlines():
        directory, compiled, skipped = (line.split("\t") + ["", ""])[:3]
        try:
            relative = Path(directory).resolve().relative_to(root)
        except ValueError:
            continue
        for names, into in ((compiled, built), (skipped, ignored)):
            into.update((relative / n).as_posix() for n in names.split())
    return built, ignored


def _json(path):
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _unavailable_reason(spec, root):
    if spec.name == "scip-go" and not (root / "go.mod").is_file():
        return "no_go_module"
    return None


# Directories the go command never treats as part of a module's packages.
_GO_IGNORED = {"vendor", "testdata", "node_modules"}


def go_modules(workspace):
    """Workspace-relative directories holding a go.mod (``.`` for the root),
    outside vendor, testdata and hidden directories."""
    root = Path(workspace).resolve(strict=True)
    found = []
    for directory, names, filenames in os.walk(root, followlinks=False):
        names[:] = sorted(
            name
            for name in names
            if name not in _SKIP_DIRECTORIES
            and name not in _GO_IGNORED
            and not name.startswith((".", "_"))
            and not (Path(directory) / name).is_symlink()
        )
        if "go.mod" in filenames:
            found.append(Path(directory).relative_to(root).as_posix())
    return sorted(found)


def go_module_of(path, modules):
    """The innermost module directory that holds ``path``, or None."""
    owners = [m for m in modules if m == "." or path == m or path.startswith(m + "/")]
    return max(owners, key=lambda m: 0 if m == "." else len(m), default=None)


@dataclass
class IndexRun:
    """One indexer's outcome: an index and receipt, or why there is none."""

    indexer: str
    version: str
    languages: tuple
    status: str  # indexed | unavailable | failed
    reason: str | None = None
    index: str | None = None
    receipt_sha256: str | None = None
    position_encoding: str | None = None
    documents: tuple = ()
    detail: str | None = None
    wall_seconds: float = 0.0
    module: str | None = None
    platform: str | None = None

    def to_dict(self):
        return {
            "indexer": self.indexer,
            "version": self.version,
            "languages": list(self.languages),
            "status": self.status,
            "reason": self.reason,
            "index": self.index,
            "receipt_sha256": self.receipt_sha256,
            "position_encoding": self.position_encoding,
            "documents": len(self.documents),
            "detail": self.detail,
            "wall_seconds": round(self.wall_seconds, 3),
            **({"module": self.module} if self.module is not None else {}),
            **({"platform": self.platform} if self.platform is not None else {}),
        }


def run_indexer(
    spec,
    workspace,
    output,
    *,
    project_name=None,
    timeout=600,
    environ=None,
    files=None,
    module=None,
    platform=None,
):
    """Run one pinned indexer; return an IndexRun, never raise for tool failures.

    ``files`` (scip-typescript only) indexes just those workspace files, as one
    project that extends the workspace's root tsconfig.json: the pass for
    files no tsconfig of the workspace includes.
    """
    if files is not None and spec.name not in {"scip-typescript", "scip-go"}:
        raise ValueError("Only scip-typescript and scip-go index chosen files")
    if platform is not None and spec.name != "scip-go":
        raise ValueError("Only scip-go indexes for another platform")
    if module is not None and spec.name != "scip-go":
        raise ValueError("Only scip-go indexes one module of a workspace")
    from ._scip import scip_pb2

    started = time.monotonic()
    root = Path(workspace).resolve(strict=True)
    output = Path(output).absolute()
    run = IndexRun(
        spec.name,
        spec.version,
        spec.languages,
        "failed",
        module=module,
        platform=(platform or GO_PLATFORM) if spec.name == "scip-go" else None,
    )
    # A nested Go module is indexed from its own directory; its document
    # paths are then made workspace-relative.
    base = root if module in (None, ".") else root / module
    reason = _unavailable_reason(spec, base)
    if reason:
        run.status, run.reason = "unavailable", reason
        return run
    executable = locate(spec, environ)
    if executable is None:
        run.status, run.reason = "unavailable", "indexer_not_installed"
        run.detail = (
            f"Install {spec.name} {spec.version} on PATH or set "
            f"{spec.environment_variable}"
        )
        return run
    receipt_path = Path(str(output) + ".sources.json")
    before = capture_inputs(root, spec, excluded=(output, receipt_path))
    with tempfile.TemporaryDirectory(prefix="deepthought-scip-") as scratch:
        temporary = Path(scratch) / "index.scip"
        argv, cwd = _arguments(spec, root, temporary, project_name or root.name)
        cwd = base
        project = None
        if files is None and "--infer-tsconfig" in argv:
            # --infer-tsconfig would write a tsconfig.json into the workspace,
            # which may be read-only: the same project, every TypeScript and
            # JavaScript file, is written to the scratch directory instead.
            files = sorted(source_files(root, set(spec.languages)))
        if files is not None and spec.name == "scip-typescript":
            project = _uncovered_project(root, files)
            config = Path(scratch) / "tsconfig.json"
            config.write_text(
                json.dumps(
                    {
                        **project,
                        "files": [str(root / f) for f in project["files"]],
                        **(
                            {"extends": str(root / project["extends"])}
                            if "extends" in project
                            else {}
                        ),
                    }
                )
            )
            argv = [a for a in argv[:5] if a != "--infer-tsconfig"]
            argv += ["--no-progress-bar", str(config)]
        try:
            result = subprocess.run(
                [str(executable), *argv],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=_environment(spec, root, environ, platform),
            )
        except subprocess.TimeoutExpired:
            run.reason = "indexer_timeout"
            run.wall_seconds = time.monotonic() - started
            return run
        log = (result.stdout + result.stderr)[-4000:]
        if result.returncode or not temporary.is_file():
            run.reason, run.detail = "indexer_failed", log
            run.wall_seconds = time.monotonic() - started
            return run
        if capture_inputs(root, spec, excluded=(output, receipt_path)) != before:
            run.reason = "inputs_changed_during_indexing"
            run.wall_seconds = time.monotonic() - started
            return run
        parsed = scip_pb2.Index()
        parsed.ParseFromString(temporary.read_bytes())
        # scip-go also indexes test mains it generates in the Go build cache;
        # those paths leave the workspace and name the machine, so drop them.
        outside = [d for d in parsed.documents if not is_relative_file(d.relative_path)]
        if base != root:
            for document in parsed.documents:
                if is_relative_file(document.relative_path):
                    document.relative_path = f"{module}/{document.relative_path}"
        # A pass over chosen files keeps only those: their imports belong to
        # the first pass.
        other = [
            d
            for d in parsed.documents
            if files is not None
            and is_relative_file(d.relative_path)
            and d.relative_path not in files
        ]
        for document in outside + other:
            parsed.documents.remove(document)
        data = canonical_bytes(parsed)
        produced = (parsed.metadata.tool_info.name, parsed.metadata.tool_info.version)
        if produced != (spec.name, spec.version):
            run.reason = "indexer_version_mismatch"
            run.detail = (
                f"Expected {spec.name} {spec.version}, got {produced[0]} {produced[1]}"
            )
            run.wall_seconds = time.monotonic() - started
            return run
        documents = tuple(
            sorted(
                d.relative_path
                for d in parsed.documents
                if is_relative_file(d.relative_path) and d.relative_path in before
            )
        )
        receipt = {
            "schema_version": PROFILE_VERSION,
            "profile": spec.profile,
            "index_sha256": sha256(data).hexdigest(),
            "files": before,
            "position_encoding": spec.position_encoding,
            "producer": {
                "name": spec.name,
                "version": spec.version,
                "executable_sha256": _hash(executable.resolve()),
                **(
                    {"python_path": python_source_roots(root)}
                    if spec.name == "scip-python"
                    else {}
                ),
                # Paths are left out so the receipt does not depend on where
                # the workspace or the scratch directory lives.
                "arguments": [
                    "." if a == str(root) else a
                    for a in argv
                    if not a.startswith(str(Path(scratch)))
                ],
                **({"project": project} if project is not None else {}),
                **({"module": module} if module is not None else {}),
                **(
                    {
                        "platform": platform or GO_PLATFORM,
                        "go_version": (
                            _go_command(["env", "GOVERSION"], base, environ) or ""
                        ).strip(),
                    }
                    if spec.name == "scip-go"
                    else {}
                ),
            },
            "documents": list(documents),
            "dropped_outside_workspace": len(outside),
        }
        _write_pair(output, data, receipt_path, receipt)
    run.status, run.index = "indexed", str(output)
    run.receipt_sha256 = _digest(receipt)
    run.position_encoding = spec.position_encoding
    run.documents = documents
    run.wall_seconds = time.monotonic() - started
    return run


def _uncovered_project(root, files):
    """The project a scip-typescript pass over ``files`` indexes, with paths
    relative to the workspace: those files, allowing JavaScript and imports
    by .ts names, with the compiler options of the root tsconfig.json if there
    is one."""
    # Files outside every tsconfig are often Deno or Bun scripts, which import
    # modules by their .ts names; the compiler accepts that only when told.
    options = {"allowJs": True, "allowImportingTsExtensions": True, "noEmit": True}
    project = {"compilerOptions": options, "files": sorted(files)}
    if (root / "tsconfig.json").is_file():
        project["extends"] = "tsconfig.json"
    return project


def canonical_bytes(index):
    """The index with documents, occurrences and symbols in a fixed order.

    Some indexers (scip-go) emit the same facts in a different order from run
    to run; sorting makes equal facts equal bytes, so receipts and snapshot
    identities are reproducible.
    """
    documents = sorted(index.documents, key=lambda d: d.relative_path)
    for document in documents:
        occurrences = sorted(
            document.occurrences,
            key=lambda o: (list(o.range), o.symbol, o.symbol_roles),
        )
        del document.occurrences[:]
        document.occurrences.extend(occurrences)
        for symbol in document.symbols:
            _sort_relationships(symbol)
        symbols = sorted(document.symbols, key=lambda s: s.symbol)
        del document.symbols[:]
        document.symbols.extend(symbols)
    del index.documents[:]
    index.documents.extend(documents)
    for symbol in index.external_symbols:
        _sort_relationships(symbol)
    external = sorted(index.external_symbols, key=lambda s: s.symbol)
    del index.external_symbols[:]
    index.external_symbols.extend(external)
    return index.SerializeToString(deterministic=True)


def _sort_relationships(symbol):
    relationships = sorted(
        symbol.relationships,
        key=lambda r: (
            r.symbol,
            r.is_reference,
            r.is_implementation,
            r.is_type_definition,
            r.is_definition,
        ),
    )
    del symbol.relationships[:]
    symbol.relationships.extend(relationships)


def _write_pair(output, data, receipt_path, receipt):
    # Same-directory temporary files: a crash between the two replacements
    # leaves a pair validate_receipt rejects, never a falsely fresh index.
    staged = []
    try:
        for destination, content in (
            (output, data),
            (receipt_path, json.dumps(receipt, sort_keys=True).encode()),
        ):
            with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as stream:
                staged.append(Path(stream.name))
                stream.write(content)
            os.replace(staged[-1], destination)
    finally:
        for path in staged:
            path.unlink(missing_ok=True)


def validate_receipt(workspace, index_path):
    """Check an index against its receipt and the current inputs; return the receipt."""
    path = Path(index_path).absolute()
    receipt_path = Path(str(path) + ".sources.json")
    if receipt_path.is_symlink() or receipt_path.stat().st_size > 64_000_000:
        raise ValueError("Invalid SCIP source receipt")
    receipt = json.loads(receipt_path.read_text())
    spec = next(
        (s for s in INDEXERS.values() if s.profile == receipt.get("profile")), None
    )
    if spec is None or receipt.get("schema_version") != PROFILE_VERSION:
        raise ValueError("Unsupported SCIP source receipt profile")
    producer = receipt.get("producer", {})
    if (producer.get("name"), producer.get("version")) != (spec.name, spec.version):
        raise ValueError(
            f"The {spec.profile} profile requires {spec.name} {spec.version}"
        )
    expected = receipt.get("files")
    if not isinstance(expected, dict) or any(
        not is_relative_file(name) for name in expected
    ):
        raise ValueError("Invalid SCIP input manifest")
    if _hash(path) != receipt.get("index_sha256"):
        raise ValueError("SCIP index differs from its source receipt")
    if capture_inputs(workspace, spec, excluded=(path, receipt_path)) != expected:
        raise ValueError("SCIP inputs changed; regenerate the index")
    return receipt


def index_workspace(
    workspace,
    output_directory,
    *,
    languages=("python", "typescript", "javascript", "go"),
    project_name=None,
    timeout=600,
    environ=None,
):
    """Run every pinned indexer the languages need; report coverage per file.

    Returns ``{"runs": [...], "coverage": {file: {...}}, "uncovered": n}``.
    A file is ``covered`` when its indexer's index has a document for it;
    otherwise ``reason`` says why (indexer not installed, failed, wrong
    version, or the indexer did not include the file). TypeScript and
    JavaScript files outside every tsconfig.json of the workspace are indexed
    by a second scip-typescript pass (``scip-typescript-uncovered.scip``,
    coverage ``pass: outside_tsconfig``).
    """
    root = Path(workspace).resolve(strict=True)
    directory = Path(output_directory).absolute()
    directory.mkdir(parents=True, exist_ok=True)
    unknown = set(languages) - set(LANGUAGE_INDEXERS)
    if unknown:
        raise ValueError("No SCIP indexer for " + ", ".join(sorted(unknown)))
    sources = source_files(root, set(languages))
    present = {language for language in sources.values()}
    runs = []
    modules = go_modules(root) if "go" in present else []
    for spec in INDEXERS.values():
        if not present & set(spec.languages):
            continue
        # scip-go indexes one module per run, so each go.mod gets its own.
        for module in (modules or ["."]) if spec.name == "scip-go" else [None]:
            name = spec.name
            if module not in (None, "."):
                name += "-" + module.replace("/", "__")
            runs.append(
                run_indexer(
                    spec,
                    root,
                    directory / f"{name}.scip",
                    project_name=project_name,
                    timeout=timeout,
                    environ=environ,
                    module=module,
                )
            )
    by_language = {
        language: run
        for run in runs
        if run.module in (None, ".")
        for language in run.languages
    }
    go_runs = {run.module: run for run in runs if run.indexer == "scip-go"}
    # Second passes, for files the first leaves out: path -> (run, fields).
    passes = {}
    # Files outside every tsconfig of the workspace get a scip-typescript
    # pass of their own, so they are compiled too.
    typescript = next((r for r in runs if r.indexer == "scip-typescript"), None)
    second = None
    if typescript is not None and typescript.status == "indexed":
        missing = sorted(
            path
            for path, language in sources.items()
            if language in typescript.languages and path not in typescript.documents
        )
        if missing:
            second = run_indexer(
                INDEXERS["scip-typescript"],
                root,
                directory / "scip-typescript-uncovered.scip",
                project_name=project_name,
                timeout=timeout,
                environ=environ,
                files=missing,
            )
            runs.append(second)
            for path in second.documents:
                passes[path] = (second, {"pass": "outside_tsconfig"})
    # Go files that build only on another platform (command_windows.go) get a
    # scip-go pass for the first platform, in the go command's own list, that
    # compiles them.
    constrained = set()
    platforms = None
    for module, run in sorted(go_runs.items()):
        if run.status != "indexed":
            continue
        missing = {
            path
            for path, language in sources.items()
            if language == "go"
            and go_module_of(path, list(go_runs)) == module
            and path not in run.documents
        }
        _, ignored = go_package_files(root, module, GO_PLATFORM, environ)
        constrained |= missing & ignored
        missing &= ignored
        if missing and platforms is None:
            platforms = go_platforms(environ)
        for platform in platforms or ():
            if not missing:
                break
            built, _ = go_package_files(root, module, platform, environ)
            chosen = sorted(missing & built)
            if not chosen:
                continue
            name = "scip-go" + (
                "" if module == "." else "-" + module.replace("/", "__")
            )
            extra = run_indexer(
                INDEXERS["scip-go"],
                root,
                directory / f"{name}-{platform.replace('/', '-')}.scip",
                project_name=project_name,
                timeout=timeout,
                environ=environ,
                files=chosen,
                module=module,
                platform=platform,
            )
            runs.append(extra)
            for path in extra.documents:
                passes[path] = (extra, {"pass": "platform", "platform": platform})
            missing -= set(extra.documents)
    coverage = {}
    for path, language in sorted(sources.items()):
        run = by_language.get(language)
        if language == "go":
            run = go_runs.get(go_module_of(path, list(go_runs)), run)
        if run is None:
            # A Go file under no go.mod, in a workspace with nested modules.
            coverage[path] = {
                "language": language,
                "status": "uncovered",
                "indexer": "scip-go",
                "reason": "no_go_module",
            }
            continue
        if run.status == "indexed" and path in run.documents:
            coverage[path] = {
                "language": language,
                "status": "covered",
                "indexer": run.indexer,
            }
        elif path in passes:
            coverage[path] = {
                "language": language,
                "status": "covered",
                "indexer": run.indexer,
                **passes[path][1],
            }
        else:
            reason = run.reason if run.status != "indexed" else "not_in_index"
            if path in constrained:
                # Excluded by build constraints on every platform tried, e.g.
                # //go:build ignore or a custom tag.
                reason = "build_constraints"
            coverage[path] = {
                "language": language,
                "status": "uncovered",
                "indexer": run.indexer,
                "reason": reason,
                **(
                    {"outside_tsconfig_pass": second.reason}
                    if second is not None
                    and second.status != "indexed"
                    and language in second.languages
                    else {}
                ),
            }
    return {
        "runs": [run.to_dict() for run in runs],
        "coverage": coverage,
        "uncovered": sum(1 for c in coverage.values() if c["status"] != "covered"),
    }


# Reasons a file has no index because an indexer did not do its job: the run
# must stop. Every other reason (no go.mod above the file, no platform builds
# it, the indexer ran and left it out) is a file the compilers cannot index,
# which the parser's rules cover.
TOOL_FAILURES = frozenset(
    {
        "indexer_not_installed",
        "indexer_failed",
        "indexer_timeout",
        "indexer_version_mismatch",
        "inputs_changed_during_indexing",
    }
)


def failed_files(report):
    """Files of an ``index_workspace`` report left uncovered by a failing,
    missing or wrong indexer, with the reason."""
    return {
        path: entry["reason"]
        for path, entry in report["coverage"].items()
        if entry["status"] != "covered" and entry["reason"] in TOOL_FAILURES
    }


def ingest_indexed(report, repository_id, workspace=None):
    """The indexes an ``index_workspace`` report produced, ingested and each
    paired with its receipt digest: the ``scip_indexes`` TreeSitterTool binds
    call sites with.

    These indexes were written by this engine and are bound to their receipts,
    so their size is not capped the way an index from elsewhere is: scip-python
    writes over 100 MB for a large monorepo. With ``workspace``, each index is
    first checked against its receipt."""
    from .scip import ingest_scip

    sources = []
    for run in report["runs"]:
        if run["status"] != "indexed":
            continue
        if workspace is not None:
            validate_receipt(workspace, run["index"])
        sources.append(
            (
                ingest_scip(
                    run["index"],
                    repository_id=repository_id,
                    default_position_encoding=run["position_encoding"],
                    max_index_bytes=max(1, Path(run["index"]).stat().st_size),
                ),
                run["receipt_sha256"],
            )
        )
    return sources


def requirements(environ=None):
    """Each pinned indexer, its version and whether it is installed here."""
    return [
        {
            "indexer": spec.name,
            "version": spec.version,
            "languages": list(spec.languages),
            "installed": str(path) if (path := locate(spec, environ)) else None,
            "override": spec.environment_variable,
        }
        for spec in INDEXERS.values()
    ]
