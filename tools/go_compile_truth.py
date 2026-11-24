"""Go reference truth from the Go compiler, independent of SCIP.

For a seeded sample of a module's functions and methods, rename each
declaration in a scratch copy and compile the module and its tests
(``go build`` and ``go test -c`` with ``-gcflags=all=-e``, so no error limit).
The compiler (cmd/compile's types2, not go/types and not scip-go) then
reports every place that referred to the old name. Nothing is run: test
binaries are written to a scratch directory and never executed.

Rows (tab-separated): decl_file, decl_line, name, ref_file, ref_line, kind.
``kind`` is ``reference`` for a use of the name and ``implements`` for an
assignment that needed the method to satisfy an interface. A declaration
whose rename compiles cleanly has no references and gets one row with
``-`` as ref_file. A declaration is skipped when the original tree does not
compile, or when the renamed tree fails for a reason that names nothing.

Usage:
  python tools/go_compile_truth.py WORKSPACE SPANS_TSV [--sample 60]
      [--seed 1] > compile.tsv

SPANS_TSV is the output of ``go run tools/go_function_spans.go WORKSPACE``.

``compare`` scores SCIP-derived call truth against these rows: a SCIP call
row whose target was sampled must be one of the compiler's references.

  python tools/go_compile_truth.py compare compile.tsv calls.tsv
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
from pathlib import Path

HEADER = "# decl_file\tdecl_line\tname\tref_file\tref_line\tkind"
_ERROR = re.compile(r"^(?:\./)?([^\s:][^:]*\.go):(\d+):(\d+): (.*)$")
_FAILED = re.compile(r"^# (\S+)")
_FUNC = re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)")
_GONE = "__compile_truth_gone"
# Errors that a missing method causes at an interface conversion, not at a use.
_IMPLEMENTS = ("does not implement", "missing method", "impossible type assertion")


def compile_errors(root, out):
    """({(file, line, column, message)}, {failed import path}) from building
    the module and its tests."""
    env = {**os.environ, "GOWORK": "off", "GOFLAGS": "-mod=mod", "CGO_ENABLED": "0"}
    found, failed = set(), set()

    def run(argv):
        done = subprocess.run(
            argv, cwd=root, env=env, capture_output=True, text=True, check=False
        )
        for line in done.stderr.splitlines():
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
            elif _FAILED.match(line):
                failed.add(_FAILED.match(line).group(1))

    test = ["go", "test", "-c", "-gcflags=all=-e", "-o", str(out) + os.sep]
    run(["go", "build", "-gcflags=all=-e", "./..."])
    run(test + ["./..."])
    # With ./..., a package whose own build fails never gets its test
    # variant compiled, so its test files are never checked. Compile each
    # failing package's tests on their own.
    for path in sorted(failed):
        run(test + [path])
    return found, failed


def packages(root):
    """{import path: (directory, {import paths its build or tests need})}."""
    import json

    env = {**os.environ, "GOWORK": "off", "GOFLAGS": "-mod=mod"}
    done = subprocess.run(
        ["go", "list", "-e", "-json", "./..."],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    out, decoder, text = {}, json.JSONDecoder(), done.stdout.strip()
    while text:
        item, end = decoder.raw_decode(text)
        text = text[end:].strip()
        rel = os.path.relpath(item["Dir"], root).replace(os.sep, "/")
        needs = set(item.get("Deps", [])) | set(item.get("TestImports", []))
        needs |= set(item.get("XTestImports", []))
        out[item["ImportPath"]] = (rel, needs)
    # Tests need their imports' dependencies too.
    for path, (rel, needs) in out.items():
        for dep in list(needs):
            needs |= out.get(dep, ("", set()))[1]
    return out


def candidates(spans, root):
    """(file, line, name) of each declared function or method."""
    out = []
    with open(spans) as stream:
        for r in csv.reader(stream, delimiter="\t"):
            if not r or r[0].startswith("#"):
                continue
            file, line = r[0], int(r[1])
            if file.startswith(("_", ".")) or "/_" in file or "/." in file:
                continue
            text = (root / file).read_text("utf-8").splitlines()[line - 1]
            m = _FUNC.match(text)
            if not m or m.group(1) in ("main", "init", "_"):
                continue
            if file.endswith("_test.go") and m.group(1).startswith(
                ("Test", "Benchmark", "Example", "Fuzz")
            ):
                continue
            out.append((file, line, m.group(1)))
    return sorted(out)


def derive(workspace, spans, sample, seed):
    workspace = Path(workspace)
    picks = candidates(spans, workspace)
    picks = sorted(random.Random(seed).sample(picks, min(sample, len(picks))))
    scratch = Path(tempfile.mkdtemp())
    try:
        root, out = scratch / "src", scratch / "bin"
        shutil.copytree(workspace, root, ignore=shutil.ignore_patterns(".git"))
        out.mkdir()
        before, _ = compile_errors(root, out)
        if before:
            raise SystemExit(
                f"the original tree does not compile: {sorted(before)[:3]}"
            )
        pkgs = packages(root)
        rows = []
        for file, line, name in picks:
            rows += rename_and_compile(root, out, pkgs, file, line, name)
        return rows
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def rename_and_compile(root, out, pkgs, file, line, name):
    """Rename the declaration, then repeatedly rename each reported use too,
    so packages that import the declaring one compile and report theirs.
    A package left failing only for interface satisfaction blocks its
    importers; their files are listed as ``blocked``."""
    gone = name + _GONE
    originals = {}
    word = re.compile(r"\b" + re.escape(name) + r"\b")

    def edit(path, n, column=None):
        p = root / path
        originals.setdefault(path, p.read_text("utf-8"))
        lines = p.read_text("utf-8").split("\n")
        text = lines[n - 1]
        if column is None:
            # A function or method, or a method of an interface type.
            m = _FUNC.match(text) or re.match(
                r"\s*(" + re.escape(name) + r")\s*\(", text
            )
            start = m.start(1)
        else:
            # The column marks the start of the expression: the name itself
            # for "undefined: f", the receiver for "x.f undefined".
            begin = len(text.encode("utf-8")[: column - 1].decode("utf-8", "replace"))
            m = word.search(text, begin)
            if not m:
                return False
            start = m.start()
        lines[n - 1] = text[:start] + gone + text[start + len(name) :]
        p.write_text("\n".join(lines), "utf-8")
        return True

    refs, unexplained, failed = set(), False, set()
    try:
        edit(file, line)
        for _ in range(20):
            errors, failed = compile_errors(root, out)
            progress = False
            for f, n, col, message in sorted(errors):
                if any(k in message for k in _IMPLEMENTS):
                    refs.add((f, n, "implements"))
                elif word.search(message) and edit(f, n, col):
                    refs.add((f, n, "reference"))
                    progress = True
                elif not word.search(message):
                    unexplained = True
            if not progress:
                break
    finally:
        for path, text in originals.items():
            (root / path).write_text(text, "utf-8")
    if unexplained and not refs:
        print(f"{file}:{line} {name}: skipped, errors name nothing", file=sys.stderr)
        return []
    rows = [(file, line, name, f, n, k) for f, n, k in sorted(refs)]
    # Packages still failing (interface satisfaction) were compiled and
    # reported; packages that need them were not, so their files are blocked.
    blocked = {
        rel
        for path, (rel, needs) in pkgs.items()
        if path not in failed and needs & failed
    }
    rows += [(file, line, name, rel + "/", 0, "blocked") for rel in sorted(blocked)]
    if not rows:
        rows.append((file, line, name, "-", 0, "reference"))
    return rows


def read(path):
    with open(path) as stream:
        return [
            r
            for r in csv.reader(stream, delimiter="\t")
            if r and not r[0].startswith("#")
        ]


def _blocked(path, blocked):
    """Whether path is judged: not a blocked file, not in a blocked package
    directory (``dir/``, ``./`` for the root) and not under a blocked tree
    (``dir/**``)."""
    if path in blocked or (os.path.dirname(path) or ".") + "/" in blocked:
        return True
    return any(b.endswith("/**") and path.startswith(b[:-2]) for b in blocked)


def compare(compile_tsv, calls_tsv):
    """How SCIP call truth stands against the compiler's references."""
    refs, sampled, blocked = {}, set(), {}
    for r in read(compile_tsv):
        decl = (r[0], int(r[1]))
        sampled.add(decl)
        if r[5] == "blocked":
            blocked.setdefault(decl, set()).add(r[3])
        elif r[5] == "untyped":
            # A line the checker could not type: silence there says nothing.
            blocked.setdefault(decl, set()).add(f"{r[3]}:{r[4]}")
        elif r[5] == "reference" and r[3] != "-":
            refs.setdefault(decl, set()).add((r[3], int(r[4])))
    calls = {}
    for r in read(calls_tsv):
        if len(r) >= 6 and r[5] == "callable" and (r[3], int(r[4])) in sampled:
            calls.setdefault((r[3], int(r[4])), set()).add((r[0], int(r[1])))
    report = {
        "sampled": len(sampled),
        "scip_calls": 0,
        "confirmed": 0,
        "blocked": 0,
        "not_a_reference": [],
    }
    for decl, sites in calls.items():
        for site in sorted(sites):
            here = blocked.get(decl, set())
            if site not in refs.get(decl, set()) and (
                _blocked(site[0], here) or f"{site[0]}:{site[1]}" in here
            ):
                report["blocked"] += 1
                continue
            report["scip_calls"] += 1
            if site in refs.get(decl, set()):
                report["confirmed"] += 1
            else:
                report["not_a_reference"].append([*decl, *site])
    referenced = {(d, s) for d, sites in refs.items() for s in sites}
    called = {(d, s) for d, sites in calls.items() for s in sites}
    # Uses that are not calls (a function passed as a value) land here too.
    report["references_without_scip_call"] = len(referenced - called)
    return report


def main(argv=None):
    if argv and argv[0] == "compare" or (argv is None and sys.argv[1:2] == ["compare"]):
        args = (argv or sys.argv[1:])[1:]
        import json

        print(json.dumps(compare(*args), indent=2))
        return
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workspace")
    parser.add_argument("spans")
    parser.add_argument("--sample", type=int, default=60)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)
    print(HEADER)
    for row in derive(args.workspace, args.spans, args.sample, args.seed):
        print("\t".join(map(str, row)))


if __name__ == "__main__":
    main()
