"""TypeScript reference truth from tsc, independent of SCIP.

For a seeded sample of the declarations that a call truth file links to,
rename each declaration in a scratch copy and type-check the project with
``tsc --noEmit``. Every place that referred to the old name now fails, and
the compiler names it. Nothing is run and nothing is emitted.

Errors the tree already has are recorded first, and only errors the rename
adds count, so a project that does not type-check cleanly can still be used.

Rows (tab-separated): decl_file, decl_line, name, ref_file, ref_line, kind.
``kind`` is ``reference`` for a use of the name and ``implements`` for a
type that no longer matches an interface or annotation because the member is
gone. ``blocked`` rows name TypeScript the project does not check, a tree
(``dir/**``) or a file, so call sites there are not judged. A declaration
whose rename adds no error has one row with ``-`` as ref_file.

Usage:
  python tools/ts_compile_truth.py WORKSPACE CALLS_TSV --tsc PATH
      [--project tsconfig.json] [--sample 40] [--seed 1] > compile.tsv

``compare`` in tools/go_compile_truth.py scores call truth against these
rows the same way as against the Go compiler's.
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
_ERROR = re.compile(r"^(.+?\.[cm]?tsx?)\((\d+),(\d+)\): error (TS\d+): (.*)$")
_GONE = "__compile_truth_gone"


def type_errors(root, tsc, project):
    """{(file, line, column, code, message)} that tsc reports."""
    done = subprocess.run(
        ["node", tsc, "--noEmit", "--pretty", "false", "-p", project],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    found = []
    for line in done.stdout.splitlines():
        m = _ERROR.match(line.strip())
        if m:
            path = os.path.relpath(os.path.join(root, m.group(1)), root)
            found.append(
                [
                    path.replace(os.sep, "/"),
                    int(m.group(2)),
                    int(m.group(3)),
                    m.group(4),
                    m.group(5),
                ]
            )
        elif found and line[:1].isspace():
            # The detail of an error, such as which member is missing.
            found[-1][4] += " " + line.strip()
    return {tuple(e) for e in found}


def unchecked(root, tsc, project):
    """The workspace's TypeScript outside the project, as directories that
    hold no checked file (ending in ``/``) or as single files."""
    done = subprocess.run(
        ["node", tsc, "--listFilesOnly", "-p", project],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    checked = set()
    for line in done.stdout.splitlines():
        rel = os.path.relpath(line.strip(), root).replace(os.sep, "/")
        if not rel.startswith(".."):
            checked.add(rel)
    files = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.suffix in (".ts", ".tsx", ".mts", ".cts")
        and "node_modules" not in p.parts
        and p.is_file()
    }
    dirs_with_checked = {d for f in checked for d in _parents(f)}
    out = set()
    for f in files - checked:
        top = f
        for d in _parents(f):
            if d not in dirs_with_checked:
                top = d + "/**"
                break
        out.add(top)
    return sorted(out)


def _parents(path):
    """Directories above path, outermost first."""
    parts = path.split("/")[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def candidates(calls, root):
    """(file, line, name) of each callable target whose line names it."""
    out = set()
    with open(calls) as stream:
        for r in csv.reader(stream, delimiter="\t"):
            if len(r) < 6 or r[0].startswith("#") or r[5] != "callable":
                continue
            file, line, name = r[3], int(r[4]), r[2]
            if (
                not re.fullmatch(r"[A-Za-z_$][\w$]*", name)
                or not file.endswith((".ts", ".mts", ".cts"))
                or file.endswith(".d.ts")
            ):
                continue
            path = root / file
            if not path.is_file():
                continue
            text = path.read_text("utf-8").split("\n")[line - 1]
            if _word(name).search(text):
                out.add((file, line, name))
    return sorted(out)


def _word(name):
    return re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])")


def derive(workspace, calls, tsc, project, sample, seed):
    workspace = Path(workspace).resolve()
    picks = candidates(calls, workspace)
    picks = sorted(random.Random(seed).sample(picks, min(sample, len(picks))))
    scratch = Path(tempfile.mkdtemp())
    try:
        root = scratch / "src"
        shutil.copytree(
            workspace,
            root,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git", "node_modules"),
        )
        if (workspace / "node_modules").exists():
            (root / "node_modules").symlink_to(workspace / "node_modules")
        tsc = str(Path(tsc).resolve())
        before = type_errors(root, tsc, project)
        blocked = unchecked(root, tsc, project)
        rows = []
        for file, line, name in picks:
            found = rename_and_check(root, tsc, project, before, file, line, name)
            rows += found
            if found:
                rows += [(file, line, name, b, 0, "blocked") for b in blocked]
        return rows
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def rename_and_check(root, tsc, project, before, file, line, name):
    """Rename the declaration, then repeatedly rename each reported use too:
    an import of a missing name is the only error in the importing file, and
    renaming it there makes each use in that file fail in turn, as well as
    the files that import it again through a re-export."""
    originals = {}
    words = {name: _word(name)}

    def edit(f, n, start, old):
        p = root / f
        originals.setdefault(f, p.read_text("utf-8"))
        lines = p.read_text("utf-8").split("\n")
        text = lines[n - 1]
        m = words[old].search(text, start)
        if not m:
            return False
        text = text[: m.start()] + old + _GONE + text[m.end() :]
        # `import {name as alias}`: uses go by the alias, so follow it too.
        alias = re.match(
            r"\s+as\s+([A-Za-z_$][\w$]*)", text[m.start() + len(old + _GONE) :]
        )
        if alias and alias.group(1) not in words:
            words[alias.group(1)] = _word(alias.group(1))
            a = m.start() + len(old + _GONE) + alias.start(1)
            text = (
                text[:a]
                + alias.group(1)
                + _GONE
                + text[alias.end(1) + m.start() + len(old + _GONE) :]
            )
        lines[n - 1] = text
        p.write_text("\n".join(lines), "utf-8")
        return True

    def column(f, n, col):
        text = (root / f).read_text("utf-8").split("\n")[n - 1]
        return len(text.encode("utf-8")[: col - 1].decode("utf-8", "replace"))

    def unexport_default():
        # A default export is imported under any name, so renaming the
        # declaration breaks nothing. Make it a named export instead: each
        # default import then fails, naming the module, and its local
        # binding is followed like an alias.
        p = root / file
        text = p.read_text("utf-8").split("\n")
        if not re.match(r"\s*export\s+default\s+", text[line - 1]):
            return False
        text[line - 1] = re.sub(
            r"export\s+default\s+", "export ", text[line - 1], count=1
        )
        p.write_text("\n".join(text), "utf-8")
        return True

    module = re.compile(
        r"Module '\"[^\"]*?/"
        + re.escape(re.sub(r"\.[cm]?tsx?$", "", file))
        + r"(\.[cm]?[jt]sx?)?\"'"
    )

    def default_import(f, n):
        p = root / f
        originals.setdefault(f, p.read_text("utf-8"))
        lines = p.read_text("utf-8").split("\n")
        m = re.match(
            r"(\s*(?:import|export)\s+(?:type\s+)?)([A-Za-z_$][\w$]*)", lines[n - 1]
        )
        if not m or m.group(2).endswith(_GONE):
            return False
        local = m.group(2)
        words.setdefault(local, _word(local))
        lines[n - 1] = (
            lines[n - 1][: m.end(1)] + local + _GONE + lines[n - 1][m.end(2) :]
        )
        p.write_text("\n".join(lines), "utf-8")
        return True

    refs, unexplained = set(), False
    try:
        edit(file, line, 0, name)
        own = {(file, line)}
        # Overload signatures and the implementation share the name; with
        # one renamed, calls still resolve to the others.
        overload = re.compile(
            r"\s*(?:export\s+)?(?:declare\s+)?(?:function\s+)?"
            r"(?:(?:public|private|protected|static|async|abstract|override)\s+)*"
            + re.escape(name)
            + r"\s*[<(]"
        )
        text = (root / file).read_text("utf-8").split("\n")
        for step in (-1, 1):
            n = line + step
            while 1 <= n <= len(text) and overload.match(text[n - 1]):
                edit(file, n, 0, name)
                own.add((file, n))
                n += step
        unexport_default()
        for _ in range(20):
            errors = sorted(
                type_errors(root, tsc, project) - before,
                key=lambda e: (e[0], e[1], -e[2]),
            )
            progress, unexplained = False, False
            for f, n, col, code, message in errors:
                if module.search(message) and "default export" in message:
                    if default_import(f, n):
                        refs.add((f, n, "reference"))
                        progress = True
                    continue
                named = [w for w, rx in words.items() if rx.search(message)]
                if not named:
                    unexplained = True
                    continue
                if (f, n) in own:
                    continue
                if edit(f, n, column(f, n, col), named[0]):
                    refs.add((f, n, "reference"))
                    progress = True
                else:
                    refs.add((f, n, "implements"))
            if not progress:
                break
    finally:
        for f, text in originals.items():
            (root / f).write_text(text, "utf-8")
    if unexplained and not refs:
        print(f"{file}:{line} {name}: skipped, errors name nothing", file=sys.stderr)
        return []
    rows = [(file, line, name, f, n, k) for f, n, k in sorted(refs)]
    return rows or [(file, line, name, "-", 0, "reference")]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workspace")
    parser.add_argument("calls")
    parser.add_argument("--tsc", required=True, help="path to typescript/bin/tsc")
    parser.add_argument("--project", default="tsconfig.json")
    parser.add_argument("--sample", type=int, default=40)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)
    print(HEADER)
    for row in derive(
        args.workspace, args.calls, args.tsc, args.project, args.sample, args.seed
    ):
        print("\t".join(map(str, row)))


if __name__ == "__main__":
    main()
