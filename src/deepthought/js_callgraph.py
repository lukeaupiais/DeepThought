"""JavaScript and TypeScript calls through function values, from the pinned
Jelly call graph.

A call through a value (a callback parameter, a function taken from an
array, a map or an object of plugins, a hook registered and run later) runs
whatever function flows to it. Jelly (``@cs-au-dk/jelly``), a static
analyser for JavaScript and TypeScript, answers that with a points-to
analysis over the program: for each call site, the functions that may be
called there. The engine reads its call graph (``--callgraph-json``) for
the analyzed files and keeps, for each call the parser and the indexes do
not link, the functions Jelly finds that are parsed declarations.

Installed packages are not analyzed (``--ignore-dependencies``): the result
depends on the repository's own files only, whether or not node_modules is
present, so a value that flows only through a dependency's code is not
followed. The tool is pinned (``JELLY_VERSION``) and required where
JavaScript or TypeScript is analyzed: a missing or wrong-version tool is an
error.
"""

from collections import defaultdict
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

JELLY_PACKAGE = "@cs-au-dk/jelly"
JELLY_VERSION = "0.13.0"
JELLY_TOOL = f"jelly@{JELLY_VERSION}"
JELLY_ENVIRONMENT_VARIABLE = "DEEPTHOUGHT_JELLY"
JS_CALL_POINTS_TO = "jelly.call-points-to"
# Call resolution reasons that say what a call's name binds: the TypeScript
# compiler finds that the imported name is not exported, so the call runs
# nothing a points-to analysis of the files could add.
DECIDED_REASONS = frozenset({"imported_name_not_declared"})
# Seconds Jelly may analyze before it stops; a stopped analysis is a failure.
JELLY_TIMEOUT = 900
_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
_DECLARATIONS = (".d.ts", ".d.mts", ".d.cts")


class JellyUnavailable(RuntimeError):
    """The pinned Jelly cannot be run."""


@dataclass(frozen=True)
class PointsToCall:
    """A call edge, in 1-based lines and 0-based UTF-16 columns: where the
    call expression starts and ends, and where the called function's
    expression or declaration starts and ends."""

    file: str
    line: int
    column: int
    end_line: int
    end_column: int
    callee_file: str
    callee_line: int
    callee_column: int
    callee_end_line: int
    callee_end_column: int


def locate_jelly(environ=None):
    """The ``jelly`` executable: ``$DEEPTHOUGHT_JELLY`` if set, else PATH, at
    the pinned version (as ``jelly --version`` reports). Raises
    ``JellyUnavailable`` otherwise."""
    environ = os.environ if environ is None else environ
    found = environ.get(JELLY_ENVIRONMENT_VARIABLE) or shutil.which(
        "jelly", path=environ.get("PATH")
    )
    install = f"npm install -g {JELLY_PACKAGE}@{JELLY_VERSION}"
    if not found or not Path(found).is_file():
        raise JellyUnavailable(
            "the Jelly call graph is required to analyze JavaScript and "
            f"TypeScript: {install}, or set ${JELLY_ENVIRONMENT_VARIABLE}"
        )
    try:
        version = subprocess.run(
            [found, "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            env={**environ},
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise JellyUnavailable(f"{found} cannot be run: {error}") from None
    if version != JELLY_VERSION:
        raise JellyUnavailable(
            f"{found} is Jelly {version or 'of unknown version'}, "
            f"{JELLY_VERSION} is required: {install}"
        )
    return Path(found)


def analyzed_scripts(result):
    """The analyzed JavaScript and TypeScript sources Jelly reads (declaration
    files left out: they hold no code)."""
    return sorted(
        c.file
        for c in result.coverage
        if c.language in {"javascript", "typescript"}
        and c.status in {"analyzed", "partial"}
        and c.file.endswith(_SUFFIXES)
        and not c.file.endswith(_DECLARATIONS)
    )


def _location(text):
    # Jelly writes "file:line:column:end line:end column", columns 1-based.
    file, line, column, end_line, end_column = (int(part) for part in text.split(":"))
    return file, line, column - 1, end_line, end_column - 1


def points_to_calls(root, files, binary, environ=None, timeout=JELLY_TIMEOUT):
    """Every call edge Jelly finds among ``files`` (workspace-relative)."""
    if not files:
        return []
    root = Path(root).resolve()
    with tempfile.TemporaryDirectory() as scratch:
        output = Path(scratch) / "callgraph.json"
        completed = subprocess.run(
            [
                str(binary),
                "--callgraph-json",
                str(output),
                "--ignore-dependencies",
                # Only calls whose callee value the analysis finds: no edges
                # for callbacks assumed of unanalyzed or native code, for
                # getters and setters, or for module loading.
                "--no-callgraph-external",
                "--no-callgraph-native",
                "--no-callgraph-implicit",
                "--no-callgraph-require",
                "--no-tty",
                "--no-print-progress",
                "--timeout",
                str(timeout),
                "--logfile",
                str(Path(scratch) / "jelly.log"),
                "--basedir",
                str(root),
                *files,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout + 300,
            env={**(os.environ if environ is None else environ)},
        )
        log = (
            (Path(scratch) / "jelly.log").read_text("utf-8", "replace")
            if (Path(scratch) / "jelly.log").is_file()
            else ""
        )
        if completed.returncode or not output.is_file():
            raise RuntimeError(
                "Jelly failed: " + (completed.stderr or log or completed.stdout)
            )
        if "Time limit reached" in log:
            raise RuntimeError(f"Jelly stopped at its {timeout} s limit")
        graph = json.loads(output.read_text("utf-8"))
    names = graph["files"]
    calls, functions = graph["calls"], graph["functions"]
    edges = []
    for call, function in graph.get("call2fun", ()):
        site = _location(calls[str(call)])
        callee = _location(functions[str(function)])
        edges.append(
            PointsToCall(names[site[0]], *site[1:], names[callee[0]], *callee[1:])
        )
    return sorted(set(edges), key=lambda e: tuple(map(str, vars(e).values())))


def bind_points_to_calls(result, calls, root, linked=frozenset()):
    """{call site id: sorted declaration ids} for the edges whose call is a
    parsed call site starting and ending where Jelly's call expression does,
    not in ``linked``, and whose callee is a parsed function or method whose
    declaration ends where Jelly's function does and holds its start (a
    function written as a variable's value, ``const f = () => {}``,
    included). A function Jelly finds that no declaration spans (an
    anonymous callback, a module's top level) adds nothing."""
    from .scip_calls import _Lines, _to_utf8

    lines = _Lines(Path(root).resolve())

    def utf8(file, line, column):
        return _to_utf8(lines.get(file, line), column, "utf-16")

    sites = {}
    for site in result.call_sites:
        if site.id in linked or not site.file.endswith(_SUFFIXES):
            continue
        sites[(site.file, site.line, site.column, site.end_line, site.end_column)] = (
            site.id
        )
    declared = defaultdict(list)
    for d in result.definitions:
        if d.kind in {"Function", "Method"}:
            declared[(d.file, d.end_line, d.end_column)].append(d)
    targets = defaultdict(set)
    for call in calls:
        site = sites.get(
            (
                call.file,
                call.line,
                utf8(call.file, call.line, call.column),
                call.end_line,
                utf8(call.file, call.end_line, call.end_column),
            )
        )
        if site is None:
            continue
        start = (
            call.callee_line,
            utf8(call.callee_file, call.callee_line, call.callee_column),
        )
        end = utf8(call.callee_file, call.callee_end_line, call.callee_end_column)
        for d in declared.get((call.callee_file, call.callee_end_line, end), ()):
            if (d.line, d.column) <= start:
                targets[site].add(d.id)
    return {site: tuple(sorted(ids)) for site, ids in sorted(targets.items())}
