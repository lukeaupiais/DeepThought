"""Python reference truth from mypy, independent of SCIP.

For a seeded sample of the declarations that a call truth file links to,
rename each declaration in a scratch copy and type-check the given targets
with mypy (``--check-untyped-defs``, no configuration file, so no plugin of
the repository is loaded; nothing is imported or run). Every place that
referred to the old name and that mypy can type now fails, and mypy names it.

Errors the tree already has are recorded first, and only errors the rename
adds count. An import of the missing name is the only error in the importing
file, so it is renamed there in turn (following ``as`` aliases), which makes
each use in that file, and in files importing it again, fail too. Overloads
are renamed together.

mypy only sees what it can type: a call through an untyped value is not
reported, so this checks that call rows are references, and finds only some
of the references that have no call row. A call row that mypy does not
report on a line its precision report marks as holding Any is ``untyped``
and not judged. A method that overrides a base method is skipped: its uses
resolve to the base, so its rename breaks nothing. mypy decides that too,
by rejecting ``@typing.override`` on a method that overrides nothing.

Rows (tab-separated): decl_file, decl_line, name, ref_file, ref_line, kind.
``kind`` is ``reference`` for a use of the name and ``implements`` for an
error that names it where the line does not (an override or protocol
mismatch). ``blocked`` rows name trees outside the targets, and ``untyped``
rows the call rows on imprecise lines. A declaration
whose rename adds no error has one row with ``-`` as ref_file.

Usage:
  python tools/py_check_truth.py WORKSPACE CALLS_TSV --targets src tests
      [--mypy mypy] [--sample 40] [--seed 1] > compile.tsv

``compare`` in tools/go_compile_truth.py scores call truth against the rows.
"""

import argparse
import csv
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from xml.etree import ElementTree

HEADER = "# decl_file\tdecl_line\tname\tref_file\tref_line\tkind"
_ERROR = re.compile(r"^(.+?\.pyi?):(\d+):(\d+): error: (.*?)(?:  \[[\w-]+\])?$")
_GONE = "__compile_truth_gone"
_DEF = re.compile(r"^(\s*)(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")


_clock = [int(time.time())]


def _write(path, text):
    """Write a file with a modification time no earlier write had. mypy
    trusts its cache for a file whose size and whole-second time are
    unchanged, and two renames in the same file within a second can leave
    both equal."""
    path.write_text(text, "utf-8")
    _clock[0] += 1
    os.utime(path, (_clock[0], _clock[0]))


def _word(name):
    return re.compile(r"(?<![\w])" + re.escape(name) + r"(?!\w)")


def type_errors(root, mypy, targets, cache, report=None):
    """{(file, line, column, message)} that mypy reports. With report, a
    directory, mypy also writes its per-line XML precision report there."""
    extra = ["--xml-report", str(report)] if report else []
    done = subprocess.run(
        [
            mypy,
            "--config-file=",
            "--check-untyped-defs",
            "--show-column-numbers",
            "--no-error-summary",
            "--ignore-missing-imports",
            "--no-pretty",
            "--cache-dir",
            str(cache),
            *extra,
            *targets,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "MYPYPATH": ""},
    )
    found = set()
    for line in done.stdout.splitlines():
        m = _ERROR.match(line.strip())
        if m:
            path = os.path.relpath(os.path.join(root, m.group(1)), root)
            found.add(
                (
                    path.replace(os.sep, "/"),
                    int(m.group(2)),
                    int(m.group(3)),
                    m.group(4),
                )
            )
    return found


def imprecise_lines(report):
    """{(file, line)} where mypy's types hold Any or were not analyzed: a
    use of a renamed name there can go unreported."""
    out = set()
    for path in Path(report, "xml").rglob("*.xml"):
        tree = ElementTree.parse(path).getroot()
        name = tree.get("name", "").replace(os.sep, "/")
        for line in tree.iter("line"):
            # A line in an unannotated function is "any" even when nothing
            # on it is; its note says whether a value there is Any.
            if line.get("precision") == "unanalyzed" or line.get(
                "any_info", ""
            ).startswith("Any Types"):
                out.add((name, int(line.get("number"))))
    return out


def call_sites(calls):
    """{(decl_file, decl_line): {(file, line)}} of the call truth rows."""
    out = {}
    with open(calls) as stream:
        for r in csv.reader(stream, delimiter="\t"):
            if len(r) >= 6 and not r[0].startswith("#") and r[5] == "callable":
                out.setdefault((r[3], int(r[4])), set()).add((r[0], int(r[1])))
    return out


def overrides(root, mypy, targets, cache, file, line):
    """Whether the method at file:line overrides a base class method. Its
    rename then breaks nothing, since uses resolve to the base, so it is not
    judged. mypy says so: it rejects ``@typing.override`` on a method that
    overrides nothing."""
    p = root / file
    original = p.read_text("utf-8")
    lines = original.split("\n")
    indent = _DEF.match(lines[line - 1]).group(1)
    if not indent or not re.match(r"\s*(?:async\s+)?def\b", lines[line - 1]):
        return False
    top = line - 1
    while top > 0 and lines[top - 1].startswith(indent + "@"):
        top -= 1
    lines[line - 1 : line - 1] = [indent + "@_ct_typing.override"]
    lines[top:top] = [indent + "import typing as _ct_typing"]
    try:
        _write(p, "\n".join(lines))
        errors = type_errors(root, mypy, targets, cache)
    finally:
        _write(p, original)
    return not any(
        f == file and "marked as an override, but no base method" in m
        for f, _, _, m in errors
    )


def candidates(calls, root):
    """(file, line, name) of each callable target declared on its line."""
    out = set()
    with open(calls) as stream:
        for r in csv.reader(stream, delimiter="\t"):
            if len(r) < 6 or r[0].startswith("#") or r[5] != "callable":
                continue
            file, line, name = r[3], int(r[4]), r[2]
            if not file.endswith(".py") or not (root / file).is_file():
                continue
            text = (root / file).read_text("utf-8").split("\n")[line - 1]
            m = _DEF.match(text)
            if m and m.group(2) == name and not name.startswith("__"):
                out.add((file, line, name))
    return sorted(out)


def derive(workspace, calls, mypy, targets, sample, seed):
    workspace = Path(workspace).resolve()
    picks = candidates(calls, workspace)
    picks = sorted(random.Random(seed).sample(picks, min(sample, len(picks))))
    scratch = Path(tempfile.mkdtemp())
    try:
        root, cache = scratch / "src", scratch / "cache"
        shutil.copytree(workspace, root, ignore=shutil.ignore_patterns(".git"))
        before = type_errors(root, mypy, targets, cache, scratch / "report")
        untyped = imprecise_lines(scratch / "report")
        sites = call_sites(calls)
        outside = sorted(
            p.relative_to(root).as_posix() + "/**"
            for p in root.iterdir()
            if p.is_dir() and p.name not in targets and not p.name.startswith(".")
        )
        rows = []
        for file, line, name in picks:
            if overrides(root, mypy, targets, cache, file, line):
                print(
                    f"{file}:{line} {name}: skipped, may override a base method",
                    file=sys.stderr,
                )
                continue
            found = rename_and_check(
                root, mypy, targets, cache, before, file, line, name
            )
            rows += found
            if found:
                rows += [(file, line, name, b, 0, "blocked") for b in outside]
                seen = {(r[3], r[4]) for r in found}
                rows += [
                    (file, line, name, f, n, "untyped")
                    for f, n in sorted(sites.get((file, line), set()) & untyped - seen)
                ]
        return rows
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def rename_and_check(root, mypy, targets, cache, before, file, line, name):
    originals = {}
    words = {name: _word(name)}

    def edit(f, n, start, old):
        p = root / f
        originals.setdefault(f, p.read_text("utf-8"))
        lines = p.read_text("utf-8").split("\n")
        text = lines[n - 1]
        m = words[old].search(text, start)
        if not m and re.search(r"\bimport\s*\(", text):
            # mypy reports a parenthesized import at its first line; the
            # name is on one of the lines up to the closing parenthesis.
            while not m and ")" not in text and n < len(lines):
                n += 1
                text = lines[n - 1]
                m = words[old].search(text)
        if not m:
            return False
        text = text[: m.start()] + old + _GONE + text[m.end() :]
        # `from m import name as alias`: uses go by the alias, so follow it.
        tail = text[m.start() + len(old + _GONE) :]
        alias = re.match(r"\s+as\s+([A-Za-z_]\w*)", tail)
        # `import name as name` re-exports it, and is renamed the same way.
        if alias and not alias.group(1).endswith(_GONE):
            words.setdefault(alias.group(1), _word(alias.group(1)))
            a = m.start() + len(old + _GONE)
            text = (
                text[:a]
                + tail[: alias.start(1)]
                + alias.group(1)
                + _GONE
                + tail[alias.end(1) :]
            )
        lines[n - 1] = text
        _write(p, "\n".join(lines))
        return True

    def column(f, n, col):
        return max(0, col - 1)

    own = {(file, line)}
    text = (root / file).read_text("utf-8").split("\n")
    indent = _DEF.match(text[line - 1]).group(1)
    # An @overload group: every same-level def of the name is one declaration.
    group = [
        n
        for n, t in enumerate(text, 1)
        if (m := _DEF.match(t)) and m.group(2) == name and m.group(1) == indent
    ]
    if any(re.match(r"\s*@(?:\w+\.)?overload\b", text[n - 2]) for n in group if n > 1):
        own.update((file, n) for n in group)
    refs, unexplained = set(), False
    try:
        for _, n in sorted(own):
            edit(file, n, 0, name)
        for _ in range(20):
            errors = sorted(
                type_errors(root, mypy, targets, cache) - before,
                key=lambda e: (e[0], e[1], -e[2]),
            )
            progress, unexplained = False, False
            for f, n, col, message in errors:
                named = [w for w, rx in words.items() if rx.search(message)]
                if not named:
                    # A module `__getattr__` turns a missing attribute into
                    # some other type ("object" not callable): the new error
                    # is still on a line that uses the old name.
                    line_text = (root / f).read_text("utf-8").split("\n")[n - 1]
                    named = [
                        w
                        for w, rx in words.items()
                        if rx.search(line_text, max(0, col - 1))
                    ]
                if not named:
                    unexplained = True
                    continue
                if (f, n) in own:
                    continue
                if edit(f, n, column(f, n, col), named[0]):
                    refs.add((f, n, "reference"))
                    progress = True
                elif (f, n, "reference") not in refs:
                    refs.add((f, n, "implements"))
            if not progress:
                break
    finally:
        for f, original in originals.items():
            _write(root / f, original)
    if unexplained and not refs:
        print(f"{file}:{line} {name}: skipped, errors name nothing", file=sys.stderr)
        return []
    rows = [(file, line, name, f, n, k) for f, n, k in sorted(refs)]
    return rows or [(file, line, name, "-", 0, "reference")]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workspace")
    parser.add_argument("calls")
    parser.add_argument("--targets", nargs="+", required=True)
    parser.add_argument("--mypy", default="mypy")
    parser.add_argument("--sample", type=int, default=40)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)
    print(HEADER)
    for row in derive(
        args.workspace, args.calls, args.mypy, args.targets, args.sample, args.seed
    ):
        print("\t".join(map(str, row)))


if __name__ == "__main__":
    main()
