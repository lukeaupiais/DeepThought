"""Facts about Go code from the Go type checker (go/types).

``gofacts/main.go`` type-checks the packages of each Go module from source
with the standard library's go/types and prints the facts the data-contracts
tool reads: method calls with the package declaring the method, the
packages of its result types and the string value of each of its first
arguments (constants, locals assigned once, ``+`` joins), and GORM
``TableName`` methods returning a constant. Name resolution, constant
folding and method selection are the compiler's, not rules of ours.

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


@dataclass(frozen=True)
class GoCall:
    file: str
    line: int
    column: int  # 0-based, as tree-sitter counts
    method: str
    declared: str  # import path of the package declaring the method
    results: tuple[str, ...]  # import paths of the named result types
    receiver: tuple[str, str] | None  # (import path, type name)
    texts: tuple[str | None, ...]  # each of the first arguments' string value
    types: tuple[tuple[str, str] | None, ...]  # and its named type


@dataclass(frozen=True)
class GoTableName:
    file: str
    line: int
    column: int
    package: str
    type_name: str
    type_file: str
    table: str


@dataclass
class GoFacts:
    calls: list[GoCall] = field(default_factory=list)
    table_names: list[GoTableName] = field(default_factory=list)
    # Files the type checker read, and the reason for the others.
    checked: set[str] = field(default_factory=set)
    partial: dict[str, str] = field(default_factory=dict)  # file -> first error
    uncovered: dict[str, str] = field(default_factory=dict)  # file -> reason
    modules: dict[str, str] = field(default_factory=dict)  # directory -> status
    toolchains: dict[str, str] = field(default_factory=dict)  # directory -> Go
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
    if wanted is None or _version(wanted) <= _version(info["GOVERSION"]):
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


def _environment(go, goroot=None):
    """The environment gofacts and the go command run in: offline, the
    module files read only, the toolchain on hand. A binary built with
    -trimpath knows no GOROOT, so the run is told the toolchain's."""
    environment = dict(os.environ)
    if goroot:
        environment["GOROOT"] = goroot
    environment.update(
        GOFLAGS="-mod=readonly",
        GOPROXY="off",
        GOTOOLCHAIN="local",
        GOWORK="off",
        CGO_ENABLED="0",
        GOTELEMETRY="off",
    )
    return environment


def _cache_directory():
    configured = os.environ.get("DEEPTHOUGHT_CACHE")
    if configured:
        return Path(configured)
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "deepthought"


def build(go, timeout=300):
    """(path of the gofacts binary for this source and toolchain, built into
    the cache directory when missing; the toolchain's GOROOT)."""
    source = resources.files(__package__).joinpath("gofacts/main.go").read_bytes()
    environment = _environment(go)
    info = _info(go)
    version = [info["GOVERSION"], info["GOOS"], info["GOARCH"], info["GOROOT"]]
    key = sha256(source + "\0".join(version[:3]).encode()).hexdigest()[:16]
    binary = _cache_directory() / f"gofacts-{key}"
    if binary.is_file():
        return binary, version[3]
    binary.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="deepthought-gofacts-") as scratch:
        (Path(scratch) / "main.go").write_bytes(source)
        output = Path(scratch) / "gofacts"
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
    for file in owned.pop(None, ()):
        facts.uncovered[file] = "no_go_module"
    go = toolchain()
    if go is None:
        for directory, members in owned.items():
            facts.modules[directory] = "go_not_installed"
            facts.uncovered.update(dict.fromkeys(members, "go_not_installed"))
        return facts
    built = {}
    for directory in sorted(owned):
        members = owned[directory]
        try:
            command = module_toolchain(go, _info(go), texts[directory])
            if command not in built:
                built[command] = build(command)
            binary, goroot = built[command]
            facts.toolchains[directory] = _info(command)["GOVERSION"]
        except (OSError, subprocess.SubprocessError):
            facts.modules[directory] = "gofacts_build_failed"
            facts.uncovered.update(dict.fromkeys(members, "gofacts_build_failed"))
            continue
        try:
            run = subprocess.run(
                [str(binary), str(root / directory)],
                cwd=root / directory,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
                env=_environment(command, goroot),
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
            continue
        _read(run.stdout, directory, set(members), facts)
    for file in files:
        if file not in facts.checked and file not in facts.uncovered:
            facts.uncovered[file] = "not_checked"  # build constraints, parse errors
    return facts


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
                    tuple(a.get("text") for a in arguments),
                    tuple(_type(a.get("type")) for a in arguments),
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
