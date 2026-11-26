"""Facts about Go code from the Go type checker (go/types).

``gofacts/main.go`` type-checks the packages of each Go module from source
with the standard library's go/types and prints the facts the data-contracts
tool reads: method calls with the package declaring the method, the
packages of its result types and the string value of each of its first
arguments (constants, locals assigned once, ``+`` joins), and GORM
``TableName`` methods returning a constant. Name resolution, constant
folding and method selection are the compiler's, not rules of ours.

Before the run, the engine downloads the modules providing the packages each
module imports, with ``go list -deps -test`` (and, with GOTOOLCHAIN=auto, the
Go release a module asks for), into copies of its go.mod and go.sum,
so imports are typed from the module cache. The Go toolchain is required:
without it ``collect`` raises ``GoMissing``.

The program is built once per (source, toolchain) with the Go toolchain on
``PATH`` (or ``DEEPTHOUGHT_GO``) into a cache directory. It builds nothing of
the analyzed repository and runs none of its code; imports from outside a
module come from the module cache, read with ``GOPROXY=off`` and
``-mod=readonly``, so a dependency not downloaded leaves its types unknown
and the package is reported as partly checked. A module asking for a newer
Go than the one on hand is checked with that release when the module cache
holds it (``golang.org/toolchain``, where ``go`` with GOTOOLCHAIN=auto
puts it), since an older go/types does not know newer language features. Files the facts do not cover
are listed with a reason, for the tree-sitter rules to read.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from functools import cache
from hashlib import sha256
from importlib import resources
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import tempfile

from .component_dependencies import _go_directives

PLACEHOLDER = "\x00"  # gofacts' stand-in for text the code does not fix


class GoMissing(RuntimeError):
    """The Go toolchain, which types Go code for the engine, is not installed."""


class GoVersionUnreadable(RuntimeError):
    """``go env GOVERSION`` names no Go release the engine can compare."""


@dataclass(frozen=True)
class GoValue:
    """What gofacts knows of an argument or a keyed literal element."""

    text: str | None = None  # its string value, when only one reaches it
    strings: tuple[str | None, ...] | None = None  # a []string's elements
    # (line, column, result index, field) of the call whose result's field
    # it is: q.Name after q, err := ch.QueueDeclare(...).
    result: tuple[int, int, int, str] | None = None


@dataclass(frozen=True)
class GoLiteral:
    file: str
    line: int
    column: int  # 0-based
    type: tuple[str, str]  # (import path, type name)
    elements: tuple[tuple[str, GoValue], ...]


@dataclass(frozen=True)
class GoCall:
    file: str
    line: int
    column: int  # 0-based, as tree-sitter counts
    method: str
    declared: str  # import path of the package declaring the method
    results: tuple[str, ...]  # import paths of the named result types
    receiver: tuple[str, str] | None  # (import path, type name)
    # Each of the first arguments' string values, one per path that reaches
    # the call (None: not known).
    texts: tuple[tuple[str, ...] | None, ...]
    types: tuple[tuple[str, str] | None, ...]  # and its named type
    values: tuple[GoValue, ...] = ()
    # The named type of each of the callee's results (None: not named), and
    # where the method name is written (0-based column).
    result_types: tuple[tuple[str, str] | None, ...] = ()
    # The protobuf message types (a ProtoReflect method) its arguments and
    # results are built from, type arguments included.
    messages: tuple[tuple[str, str], ...] = ()
    name_line: int | None = None
    name_column: int | None = None


@dataclass(frozen=True)
class GoSignature:
    """The named types a function's parameters and results are built from
    (``namedIn`` in gofacts), at the function's name."""

    file: str
    line: int
    column: int  # 0-based
    types: tuple[tuple[str, str], ...]  # (import path, type name)


@dataclass(frozen=True)
class GoTableName:
    file: str
    line: int
    column: int
    package: str
    type_name: str
    type_file: str
    table: str


@dataclass(frozen=True)
class GopathPackage:
    """How a package outside every module was checked: as a module of its own
    whose imports resolved to their latest versions when it was analyzed
    (status ``gopath_latest``). ``modules`` are the (path, version) pairs the
    go.mod of that check required, direct and indirect, and
    ``go_sum_sha256`` the digest of the go.sum it produced (None without
    one), so that a result can be traced and reproduced."""

    status: str
    modules: tuple
    go_sum_sha256: str | None


@dataclass
class GoFacts:
    calls: list[GoCall] = field(default_factory=list)
    table_names: list[GoTableName] = field(default_factory=list)
    literals: list[GoLiteral] = field(default_factory=list)
    signatures: list[GoSignature] = field(default_factory=list)
    # Files the type checker read, and the reason for the others.
    checked: set[str] = field(default_factory=set)
    partial: dict[str, str] = field(default_factory=dict)  # file -> first error
    uncovered: dict[str, str] = field(default_factory=dict)  # file -> reason
    modules: dict[str, str] = field(default_factory=dict)  # directory -> status
    toolchains: dict[str, str] = field(default_factory=dict)  # directory -> Go
    downloads: dict[str, str] = field(default_factory=dict)  # directory -> status
    # Directories of packages outside every module, checked as modules of
    # their own (GOPATH-era code).
    gopath: dict[str, GopathPackage] = field(default_factory=dict)
    requires: dict[str, frozenset[str]] = field(default_factory=dict)

    def module_of(self, file):
        """The directory of the deepest module holding ``file``, or None."""
        return next(
            (
                d
                for d in sorted(self.requires, key=lambda d: (-len(d), d))
                if not d or file.startswith(d + "/")
            ),
            None,
        )

    def requires_module(self, file, prefixes):
        """Whether the go.mod of ``file``'s module requires a module path
        equal to or under one of ``prefixes``."""
        module = self.module_of(file)
        return module is not None and any(
            path == p or path.startswith(p + "/")
            for path in self.requires[module]
            for p in prefixes
        )


def toolchain():
    """The Go command, or None."""
    return os.environ.get("DEEPTHOUGHT_GO") or shutil.which("go")


def _version(name):
    """(1, 26, 10) for "go1.26.10", None for a name that is not a release."""
    match = re.fullmatch(r"go(\d+)\.(\d+)(?:\.(\d+))?", name or "")
    return tuple(int(part or 0) for part in match.groups()) if match else None


def installed_version(goversion):
    """The release of the installed Go command, from ``go env GOVERSION``:
    (1, 27, 4) for "go1.27.4", and for an experiment build such as
    "go1.27.4-X:nodwarf5" or "go1.27.4 X:nodwarf5", whose experiment
    suffix does not change the release. Raises GoVersionUnreadable for
    anything else."""
    version = _version(re.sub(r"[- ]X:\S*$", "", goversion or ""))
    if version is None:
        raise GoVersionUnreadable(
            f"cannot read the Go release from GOVERSION {goversion!r}; "
            "install a Go release (go1.N.M)"
        )
    return version


def wanted_toolchain(text):
    """The toolchain a go.mod asks for: its toolchain line, else the release
    its go line names (go 1.26.0 is go1.26.0; from Go 1.21, go 1.22 is
    go1.22.0), or None."""
    found = {verb: tokens[0] for verb, tokens in _go_directives(text) if tokens}
    if "toolchain" in found and _version(found["toolchain"]):
        return found["toolchain"]
    line = found.get("go", "")
    if re.fullmatch(r"\d+\.\d+", line) and _version("go" + line) >= (1, 21, 0):
        line += ".0"
    return "go" + line if _version("go" + line) else None


def module_toolchain(go, info, text):
    """The Go command for a module: ``go`` itself, unless the module asks
    for a newer release that the module cache already holds (as ``go``
    with GOTOOLCHAIN=auto would download it). Nothing is downloaded."""
    wanted = wanted_toolchain(text)
    if wanted is None or _version(wanted) <= installed_version(info["GOVERSION"]):
        return go
    cached = (
        Path(info["GOMODCACHE"])
        / "golang.org"
        / f"toolchain@v0.0.1-{wanted}.{info['GOOS']}-{info['GOARCH']}"
        / "bin"
        / "go"
    )
    return str(cached) if cached.is_file() else go


@cache
def _info(go):
    names = ("GOVERSION", "GOOS", "GOARCH", "GOROOT", "GOMODCACHE")
    values = subprocess.run(
        [go, "env", *names],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env=_environment(go),
    ).stdout.split("\n")
    return dict(zip(names, values))


def _environment(go, goroot=None, modfile=None):
    """The environment gofacts and the go command run in: offline, the
    module files read only, the toolchain on hand. A binary built with
    -trimpath knows no GOROOT, so the run is told the toolchain's."""
    environment = dict(os.environ)
    if goroot:
        environment["GOROOT"] = goroot
    environment.update(
        GOFLAGS="-mod=readonly" + (f" -modfile={modfile}" if modfile else ""),
        GOPROXY="off",
        GOTOOLCHAIN="local",
        GOWORK="off",
        # cgo files are type-checked as the build sees them (confluent-kafka-
        # go declares its types there): the source importer runs cgo, which
        # reads the C headers; nothing is linked or run.
        CGO_ENABLED="1",
        GOTELEMETRY="off",
    )
    return environment


def module_files(directory, scratch):
    """Copies of a module's go.mod and go.sum in ``scratch``, so that the go
    command records what it resolves there and never writes the analyzed
    repository; the path of the go.mod copy (for -modfile)."""
    copy = Path(scratch) / "go.mod"
    shutil.copyfile(Path(directory) / "go.mod", copy)
    if (Path(directory) / "go.sum").is_file():
        shutil.copyfile(Path(directory) / "go.sum", Path(scratch) / "go.sum")
    return str(copy)


def download(go, directory, modfile, timeout=900):
    """Download what type-checking a module needs, with ``modfile`` (a copy
    of its go.mod) taking the updates: ``go list -deps -test ./...`` fetches
    the modules providing every package its packages and tests import (and
    the Go release it asks for) into the module cache. "downloaded", or
    "download_failed: <first line of the error>"."""
    environment = dict(os.environ)
    environment.update(
        GOFLAGS=f"-mod=mod -modfile={modfile}",
        GOTOOLCHAIN="auto",
        GOWORK="off",
        GOTELEMETRY="off",
    )
    try:
        subprocess.run(
            # The modules of the packages the module's own packages and
            # tests import, and nothing else of the module graph.
            [go, "list", "-e", "-deps", "-test", "-f", "", "./..."],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
            env=environment,
        )
    except subprocess.CalledProcessError as error:
        first = (error.stderr or "").strip().splitlines()
        return "download_failed: " + (first[0] if first else str(error.returncode))
    except (OSError, subprocess.SubprocessError) as error:
        return f"download_failed: {type(error).__name__}"
    return "downloaded"


def _cache_directory():
    configured = os.environ.get("DEEPTHOUGHT_CACHE")
    if configured:
        return Path(configured)
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "deepthought"


def build(go, timeout=300, program="gofacts"):
    """(path of the ``program`` binary (a directory of this package holding
    main.go) for this source and toolchain, built into the cache directory
    when missing; the toolchain's GOROOT)."""
    source = resources.files(__package__).joinpath(f"{program}/main.go").read_bytes()
    environment = _environment(go)
    info = _info(go)
    version = [info["GOVERSION"], info["GOOS"], info["GOARCH"], info["GOROOT"]]
    key = sha256(source + "\0".join(version[:3]).encode()).hexdigest()[:16]
    binary = _cache_directory() / f"{program}-{key}"
    if binary.is_file():
        return binary, version[3]
    binary.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"deepthought-{program}-") as scratch:
        (Path(scratch) / "main.go").write_bytes(source)
        output = Path(scratch) / program
        subprocess.run(
            [go, "build", "-trimpath", "-o", str(output), "main.go"],
            cwd=scratch,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
            env=environment,
        )
        # Rename into place, so a concurrent build never leaves half a file.
        staged = binary.with_name(binary.name + f".{os.getpid()}")
        shutil.copy2(output, staged)
        os.replace(staged, binary)
    return binary, version[3]


def _type(value):
    return (value["package"], value["name"]) if value else None


def collect(root, files, module_directories, *, timeout=600):
    """GoFacts of the Go ``files`` (relative paths) of ``root``, one gofacts
    run per module directory (relative, "" for the root)."""
    facts = GoFacts()
    files = sorted(files)
    modules = sorted(module_directories, key=lambda d: (-len(d), d))

    def module_of(file):
        return next(
            (d for d in modules if not d or file == d or file.startswith(d + "/")),
            None,
        )

    root = Path(root)
    texts = {}
    for directory in modules:
        try:
            text = (root / directory / "go.mod").read_text("utf-8", "replace")
        except OSError:
            text = ""
        texts[directory] = text
        facts.requires[directory] = frozenset(
            tokens[0].strip('"')
            for verb, tokens in _go_directives(text)
            if verb == "require" and tokens
        )
    owned = defaultdict(list)
    for file in files:
        owned[module_of(file)].append(file)
    # Files outside every module are GOPATH-era packages: each directory is
    # checked on its own, as a module of its own whose imports resolve to
    # their latest versions (what `go get` gave such a package).
    packages = defaultdict(list)
    for file in owned.pop(None, ()):
        packages[posixpath.dirname(file)].append(file)
    go = toolchain()
    if go is None and (owned or packages):
        raise GoMissing("the Go toolchain is required to type Go code")
    built = {}
    for directory in sorted(packages):
        with tempfile.TemporaryDirectory(prefix="deepthought-gopath-") as scratch:
            copy = Path(scratch) / "package"
            target = copy / directory if directory else copy
            target.mkdir(parents=True)
            try:
                for source in (root / directory).iterdir():
                    if source.suffix == ".go" and source.is_file():
                        shutil.copyfile(source, target / source.name)
            except OSError:
                facts.uncovered.update(
                    dict.fromkeys(packages[directory], "no_go_module")
                )
                continue
            version = ".".join(_info(go)["GOVERSION"].removeprefix("go").split(".")[:2])
            text = f"module gopath.local/package\n\ngo {version}\n"
            (target / "go.mod").write_text(text, encoding="utf-8")
            with tempfile.TemporaryDirectory(
                prefix="deepthought-gomod-"
            ) as files_scratch:
                _check_module(
                    copy,
                    directory,
                    packages[directory],
                    files_scratch,
                    go,
                    {directory: text},
                    built,
                    facts,
                    timeout,
                    gopath=True,
                )
    for directory in sorted(owned):
        with tempfile.TemporaryDirectory(prefix="deepthought-gomod-") as scratch:
            _check_module(
                root,
                directory,
                owned[directory],
                scratch,
                go,
                texts,
                built,
                facts,
                timeout,
            )
    for file in files:
        if file not in facts.checked and file not in facts.uncovered:
            facts.uncovered[file] = "not_checked"  # build constraints, parse errors
    return facts


def _check_module(
    root, directory, members, scratch, go, texts, built, facts, timeout, gopath=False
):
    """Download one module's dependencies and run gofacts on it. For a
    GOPATH-era package (``gopath``), what the download resolved is recorded
    in ``facts.gopath``."""
    try:
        modfile = module_files(root / directory, scratch)
    except OSError:
        modfile = None
        facts.downloads[directory] = "download_failed: no go.mod"
    else:
        facts.downloads[directory] = download(go, root / directory, modfile)
        if gopath:
            facts.gopath[directory] = _resolved(modfile)
    try:
        command = module_toolchain(go, _info(go), texts[directory])
        if command not in built:
            built[command] = build(command)
        binary, goroot = built[command]
        facts.toolchains[directory] = _info(command)["GOVERSION"]
    except (OSError, subprocess.SubprocessError):
        facts.modules[directory] = "gofacts_build_failed"
        facts.uncovered.update(dict.fromkeys(members, "gofacts_build_failed"))
        return
    try:
        run = subprocess.run(
            [str(binary), str(root / directory)],
            cwd=root / directory,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
            env=_environment(command, goroot, modfile),
        )
    except subprocess.TimeoutExpired:
        status = "gofacts_timeout"
    except (OSError, subprocess.SubprocessError):
        status = "gofacts_failed"
    else:
        status = "checked"
    facts.modules[directory] = status
    if status != "checked":
        facts.uncovered.update(dict.fromkeys(members, status))
        return
    _read(run.stdout, directory, set(members), facts)


def _resolved(modfile):
    """The GopathPackage a check of a GOPATH-era package resolved: the
    requirements of its go.mod copy and the digest of its go.sum."""
    modules = sorted(
        (tokens[0].strip('"'), tokens[1])
        for verb, tokens in _go_directives(Path(modfile).read_text(encoding="utf-8"))
        if verb == "require" and len(tokens) > 1
    )
    go_sum = Path(modfile).with_name("go.sum")
    digest = sha256(go_sum.read_bytes()).hexdigest() if go_sum.is_file() else None
    return GopathPackage("gopath_latest", tuple(modules), digest)


def gopath_report(facts):
    """{directory: {status, modules, go_sum_sha256}} of the GOPATH-era
    packages ``facts`` checked, for run metadata."""
    return {
        directory: {
            "status": package.status,
            "modules": dict(package.modules),
            "go_sum_sha256": package.go_sum_sha256,
        }
        for directory, package in sorted(facts.gopath.items())
    }


def _single(texts):
    return texts[0] if texts and len(texts) == 1 else None


def _value(item):
    strings = item.get("strings")
    result = item.get("result")
    return GoValue(
        _single(item.get("texts")),
        None if strings is None else tuple(_single(s) for s in strings),
        None
        if result is None
        else (result["line"], result["column"] - 1, result["index"], result["field"]),
    )


def _read(text, directory, members, facts):
    def relative(path):
        return posixpath.join(directory, path) if directory and path else path

    errors = {}
    seen = set()
    for line in text.splitlines():
        item = json.loads(line)
        kind = item["kind"]
        if kind == "error":
            rel = item["directory"]
            errors[directory if rel == "." else relative(rel)] = item["message"]
        elif kind == "file":
            file = relative(item["file"])
            if file in members:
                facts.checked.add(file)
        elif kind == "call":
            file = relative(item["file"])
            arguments = item.get("arguments") or ()
            key = (file, item["line"], item["column"], item["method"])
            if file not in members or key in seen:
                continue  # a package checked with and without its tests
            seen.add(key)
            facts.calls.append(
                GoCall(
                    file,
                    item["line"],
                    item["column"] - 1,
                    item["method"],
                    item.get("declared", ""),
                    tuple(item.get("results") or ()),
                    _type(item.get("receiver")),
                    tuple(
                        tuple(a["texts"]) if a.get("texts") else None for a in arguments
                    ),
                    tuple(_type(a.get("type")) for a in arguments),
                    tuple(_value(a) for a in arguments),
                    tuple(_type(t) for t in item.get("result_types") or ()),
                    tuple(dict.fromkeys(_type(t) for t in item.get("messages") or ())),
                    item.get("name_line"),
                    item["name_column"] - 1 if item.get("name_column") else None,
                )
            )
        elif kind == "signature":
            file = relative(item["file"])
            key = (file, item["line"], item["column"], "signature")
            if file not in members or key in seen:
                continue
            seen.add(key)
            facts.signatures.append(
                GoSignature(
                    file,
                    item["line"],
                    item["column"] - 1,
                    tuple(dict.fromkeys(_type(t) for t in item["types"])),
                )
            )
        elif kind == "literal":
            file = relative(item["file"])
            key = (file, item["line"], item["column"], "literal")
            if file not in members or key in seen:
                continue
            seen.add(key)
            facts.literals.append(
                GoLiteral(
                    file,
                    item["line"],
                    item["column"] - 1,
                    _type(item["type"]),
                    tuple((e["key"], _value(e)) for e in item.get("elements") or ()),
                )
            )
        elif kind == "table_name":
            file = relative(item["file"])
            key = (file, item["line"], "TableName")
            if file not in members or key in seen:
                continue
            seen.add(key)
            facts.table_names.append(
                GoTableName(
                    file,
                    item["line"],
                    item["column"] - 1,
                    item["type"]["package"],
                    item["type"]["name"],
                    relative(item["type"].get("file", "")),
                    item["table"],
                )
            )
    for file in facts.checked & members:
        message = errors.get(posixpath.dirname(file))
        if message is not None:
            facts.partial[file] = message
