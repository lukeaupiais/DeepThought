"""Go dynamic calls from the pinned x/tools call graph (VTA).

A Go call through an interface method or a function value runs whatever the
value holds at that point. ``golang.org/x/tools/cmd/callgraph -algo=vta``
answers that from the program's SSA form with variable type analysis: for
each such call site, the functions and methods that may reach it. Those
edges replace matching method sets by name (``go_interfaces``) wherever a
call is involved. The tool is pinned (``GO_CALLGRAPH_VERSION``) and required
where Go is analyzed: a missing or wrong-version binary is an error.

Each package is analyzed as its own program with its tests, offline and
from the module cache, as ``go_facts`` runs the go command.
"""

from collections import defaultdict
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess

from .go_facts import _environment

GO_CALLGRAPH_MODULE = "golang.org/x/tools"
GO_CALLGRAPH_VERSION = "v0.31.0"
GO_CALLGRAPH_TOOL = f"callgraph@{GO_CALLGRAPH_VERSION}"
GO_CALLGRAPH_ENVIRONMENT_VARIABLE = "DEEPTHOUGHT_GO_CALLGRAPH"
GO_CALL_VTA = "go.call-vta"
_FORMAT = (
    "{{.Filename}}\t{{.Line}}\t{{.Column}}\t{{.Dynamic}}\t"
    "{{(.Callee.Prog.Fset.Position .Callee.Pos).Filename}}\t"
    "{{(.Callee.Prog.Fset.Position .Callee.Pos).Line}}\t"
    "{{(.Callee.Prog.Fset.Position .Callee.Pos).Column}}\t{{.Callee.Name}}"
)


# callgraph -algo=rta on a program with no main package and no tests.
_NO_ROOTS = "no main packages"


class GoCallgraphUnavailable(RuntimeError):
    """The pinned call graph tool cannot be run."""


@dataclass(frozen=True)
class DynamicCall:
    """A dynamic call edge, in 1-based lines and 0-based UTF-8 columns: the
    call's opening parenthesis, and the start of the callee's name."""

    file: str
    line: int
    lparen: int
    callee_file: str
    callee_line: int
    callee_column: int
    callee_name: str


def locate_go_callgraph(environ=None, go="go"):
    """The ``callgraph`` binary: ``$DEEPTHOUGHT_GO_CALLGRAPH`` if set, else
    PATH, built from ``golang.org/x/tools`` at the pinned version (as ``go
    version -m`` reports). Raises ``GoCallgraphUnavailable`` otherwise."""
    environ = os.environ if environ is None else environ
    found = environ.get(GO_CALLGRAPH_ENVIRONMENT_VARIABLE) or shutil.which(
        "callgraph", path=environ.get("PATH")
    )
    install = f"go install {GO_CALLGRAPH_MODULE}/cmd/callgraph@{GO_CALLGRAPH_VERSION}"
    if not found or not Path(found).is_file():
        raise GoCallgraphUnavailable(
            f"the Go call graph tool is required to analyze Go: {install}, "
            f"or set ${GO_CALLGRAPH_ENVIRONMENT_VARIABLE}"
        )
    built = subprocess.run(
        [go, "version", "-m", found], capture_output=True, text=True, timeout=60
    ).stdout
    modules = {
        tuple(line.split()[1:3])
        for line in built.splitlines()
        if line.split()[:1] in (["mod"], ["dep"])
    }
    if (GO_CALLGRAPH_MODULE, GO_CALLGRAPH_VERSION) not in modules:
        raise GoCallgraphUnavailable(
            f"{found} is not built from {GO_CALLGRAPH_MODULE} "
            f"{GO_CALLGRAPH_VERSION}: {install}"
        )
    return Path(found)


def dynamic_calls(root, packages, binary, go="go", timeout=1200):
    """(dynamic call edges, failures) for ``packages``, {module directory
    relative to ``root`` ("" for the root): package directories relative to
    the module ("." for its own)}.

    Each package is its own program, loaded with its tests: a Go binary is
    one main package or one package's tests with what they import, so
    values of two programs never meet. Analyzing them together would let a
    type of one example's main package reach a call in another package's
    tests. The edges are those whose call site and callee are both in files
    under ``root``, over every program, deduplicated and sorted. Failures
    are {package directory relative to ``root``: the tool's error} for each
    package it could not load (a dependency missing from the module cache,
    a type error), which yields no edges."""
    from concurrent.futures import ThreadPoolExecutor

    root = Path(root).resolve()
    programs = [
        (module, package) for module in sorted(packages) for package in packages[module]
    ]

    def run(program, algorithm):
        module, package = program
        return subprocess.run(
            [str(binary), f"-algo={algorithm}", "-test", "-format", _FORMAT]
            + ["./" + package if package != "." else "."],
            cwd=root / module,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_environment(go),
        )

    def edges(completed):
        found = set()
        for line in completed.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 8 or fields[3] != "dynamic":
                continue
            site = _relative(root, fields[0])
            callee = _relative(root, fields[4])
            if site is None or callee is None:
                continue
            found.add(
                DynamicCall(
                    site,
                    int(fields[1]),
                    int(fields[2]) - 1,
                    callee,
                    int(fields[5]),
                    int(fields[6]) - 1,
                    fields[7],
                )
            )
        return found

    def analyze(program):
        vta = run(program, "vta")
        if vta.returncode != 0:
            return vta, None
        # VTA follows values through every function of the program, dead
        # code included; RTA keeps only functions reachable from its main
        # or its tests. An edge both report is one the program can take.
        return vta, run(program, "rta")

    found, failures = set(), {}
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 1) as pool:
        for (module, package), (vta, rta) in zip(programs, pool.map(analyze, programs)):
            name = Path(os.path.normpath(os.path.join(module or ".", package)))
            if vta.returncode != 0:
                failures[name.as_posix()] = vta.stderr.strip()[-2000:]
                continue
            if rta.returncode == 0:
                found |= edges(vta) & edges(rta)
            elif _NO_ROOTS in rta.stderr:
                # A library package without tests has no root to reach
                # from: every function may be called, as VTA assumes.
                found |= edges(vta)
            else:
                failures[name.as_posix()] = rta.stderr.strip()[-2000:]
    return sorted(found, key=lambda c: tuple(vars(c).values())), failures


def _relative(root, path):
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def bind_dynamic_calls(result, calls):
    """{call site id: sorted declaration ids} for the dynamic calls whose
    site is a parsed call site (its callee's last name ends at the opening
    parenthesis) and whose callee is a parsed function or method of that
    name starting there."""
    sites = {}
    for site in result.call_sites:
        if site.callee_at is None or not site.file.endswith(".go"):
            continue
        line, column = site.callee_at
        name = site.callee.rsplit(".", 1)[-1].encode("utf-8")
        sites[(site.file, line, column + len(name))] = site.id
    declared = {}
    for d in result.definitions:
        if d.kind in {"Function", "Method"} and d.file.endswith(".go"):
            declared.setdefault((d.file, d.symbol.rsplit(".", 1)[-1]), []).append(d)
    targets = defaultdict(set)
    for call in calls:
        site = sites.get((call.file, call.line, call.lparen))
        if site is None:
            continue
        for d in declared.get((call.callee_file, call.callee_name), ()):
            if (
                (d.line, d.column)
                <= (call.callee_line, call.callee_column)
                <= (
                    d.end_line,
                    d.end_column,
                )
            ):
                targets[site].add(d.id)
    return {site: tuple(sorted(ids)) for site, ids in sorted(targets.items())}
