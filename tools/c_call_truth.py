"""Ground-truth C calls from clang's AST.

Runs ``clang -fsyntax-only -Xclang -ast-dump=json`` on each C translation
unit (every .c file under the workspace, or the files given) and records
every call whose callee clang binds to a function declaration by name, at
the line where that name is written, with the definition it reaches:

- A ``static`` function binds to its definition in the same translation unit.
- Any other function binds to the one definition the repository has. When the
  repository defines it more than once (per-platform or per-example copies),
  the call is left out, so it is unjudged.
- A function the repository neither defines nor declares, but a header
  outside the repository declares (the C library, the platform), is written
  with the
  target ``<external>``, so linking it to any repository declaration counts
  as contradicted. One declared only in the repository and never defined, or
  called without any declaration, is left out.

Calls through a function pointer or any other expression are left out, as
are calls whose callee name is written by a macro (the call in the body of a
function-like macro, or a macro that names a function). A call written in a
macro's argument is recorded where it is written. A translation unit that
clang reports errors for is skipped as a whole (it is listed on stderr), and
only the repository files read by translation units without errors are
listed in ``# indexed`` rows.

Output: the call truth format of tools/ground_truth.py (file, line, name,
target_file, target_line, kind), with kind ``callable`` for a repository
definition and ``external`` for the C library.

Usage:
  python tools/c_call_truth.py WORKSPACE [FILE ...] [--clang clang] \\
      [--flag=-Iinclude ...] [--std-c c11] > calls.tsv
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys

HEADER = "# file\tline\tname\ttarget_file\ttarget_line\tkind"
EXTERNAL = ("<external>", 0, "external")
WRAPPERS = {"ImplicitCastExpr", "ParenExpr"}


class _Locations:
    """clang's JSON omits a location's file and line when they repeat the
    previous location's, so they are tracked in dump order."""

    def __init__(self):
        self.file = None
        self.line = None

    def _bare(self, location):
        if "file" in location:
            self.file = location["file"]
        if "line" in location:
            self.line = location["line"]
        return self.file, self.line

    def point(self, location):
        """(file, line, written): the location as written in the source, and
        whether a macro wrote it. A macro argument is written where it is
        spelled; anything else a macro produces is at the expansion."""
        if not location:
            return None, None, True
        if "expansionLoc" in location:
            spelled = self._bare(location.get("spellingLoc", {}))
            expanded = self._bare(location["expansionLoc"])
            if location["expansionLoc"].get("isMacroArgExpansion"):
                return (*spelled, True)
            return (*expanded, False)
        return (*self._bare(location), True)


def _callee(call):
    """The DeclRefExpr naming a call's callee, or None."""
    inner = call.get("inner") or []
    node = inner[0] if inner else None
    while node is not None and node.get("kind") in WRAPPERS:
        node = (node.get("inner") or [None])[0]
    if node is not None and node.get("kind") == "DeclRefExpr":
        if (node.get("referencedDecl") or {}).get("kind") == "FunctionDecl":
            return node
    return None


def unit_facts(ast, root):
    """Calls, function declarations and repository files of one unit.

    Returns (calls, functions, files): calls are (file, line, name, decl_id)
    for calls written in repository files; functions map a declaration id to
    (file or None, line, name, static, defined, outside); files are the repository
    files the unit read.
    """
    locations = _Locations()
    root = Path(root).resolve()
    files, calls, functions = set(), [], {}
    callees = set()

    def relative(path):
        if path is None:
            return None
        resolved = (root / path).resolve()
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError:
            return None

    def visit(node):
        file, line, _ = locations.point(node.get("loc"))
        span = node.get("range") or {}
        begin_file, begin_line, written = locations.point(span.get("begin"))
        locations.point(span.get("end"))
        for path in (file, begin_file):
            if relative(path) is not None:
                files.add(relative(path))
        kind = node.get("kind")
        if kind == "FunctionDecl":
            functions[node["id"]] = (
                relative(file),
                line,
                node.get("name"),
                node.get("storageClass") == "static",
                any(c.get("kind") == "CompoundStmt" for c in node.get("inner", ())),
                # Declared by a header outside the repository (not implicitly
                # by a call to an undeclared name).
                not node.get("isImplicit")
                and file is not None
                and relative(file) is None,
            )
        elif kind == "CallExpr":
            callee = _callee(node)
            if callee is not None:
                callees.add(id(callee))
        elif kind == "DeclRefExpr" and id(node) in callees:
            here = relative(begin_file)
            if here is not None and written:
                referenced = node["referencedDecl"]
                calls.append((here, begin_line, referenced["name"], referenced["id"]))
        for child in node.get("inner", ()):
            visit(child)

    visit(ast)
    return calls, functions, files


def _errors(stderr):
    return [line for line in stderr.splitlines() if ": error:" in line]


def run(workspace, clang="clang", flags=(), std_c="c11", files=()):
    workspace = Path(workspace).resolve()
    units = (
        [workspace / f for f in files]
        if files
        else sorted(
            p for p in workspace.rglob("*.c") if p.is_file() and ".git" not in p.parts
        )
    )
    facts = []
    for unit in units:
        command = [
            clang,
            "-x",
            "c",
            f"-std={std_c}",
            "-fsyntax-only",
            "-ferror-limit=0",
            "-Xclang",
            "-ast-dump=json",
            *flags,
            str(unit),
        ]
        completed = subprocess.run(
            command, cwd=workspace, capture_output=True, text=True, check=False
        )
        problems = _errors(completed.stderr)
        if problems or not completed.stdout.strip():
            name = unit.relative_to(workspace).as_posix()
            print(f"{name}: skipped, {len(problems)} errors", file=sys.stderr)
            if problems:
                print("  " + problems[0][:200], file=sys.stderr)
            continue
        facts.append(unit_facts(json.loads(completed.stdout), workspace))
    # Every repository definition of a function with external linkage.
    external = {}
    for _, functions, _ in facts:
        for path, line, name, static, defined, _ in functions.values():
            if path is not None and defined and not static:
                external.setdefault(name, set()).add((path, line))
    expected, dropped, indexed = {}, set(), set()
    for calls, functions, unit_files in facts:
        indexed |= unit_files
        local = {
            name: (path, line)
            for path, line, name, static, defined, _ in functions.values()
            if path is not None and defined and static
        }
        for file, line, name, decl in calls:
            key = (file, line, name)
            static = functions[decl][3] if decl in functions else False
            declared = [
                f for f in functions.values() if f[2] == name and f[3] == static
            ]
            if static:
                target = (*local[name], "callable") if name in local else None
            elif len(external.get(name, ())) == 1:
                target = (*next(iter(external[name])), "callable")
            elif (
                name not in external
                and declared
                and all(f[0] is None for f in declared)
                and any(f[5] for f in declared)
            ):
                target = EXTERNAL
            else:
                target = None
            if target is None:
                dropped.add(key)
            else:
                expected.setdefault(key, set()).add(target)
    # A call written once can be read by several units (a header); one that
    # any unit leaves unjudged, or that two units bind differently, is left out.
    for key in list(expected):
        if key in dropped or len(expected[key]) > 1:
            del expected[key]
    return expected, sorted(indexed)


def write(expected, indexed, stream):
    stream.write(HEADER + "\n")
    for file in sorted(indexed):
        stream.write(f"# indexed\t{file}\n")
    for (file, line, name), targets in sorted(expected.items()):
        for target in sorted(targets):
            stream.write("\t".join(map(str, (file, line, name, *target))) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("files", nargs="*")
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--flag", action="append", default=[])
    parser.add_argument("--std-c", default="c11")
    args = parser.parse_args(argv)
    expected, indexed = run(
        args.workspace, args.clang, args.flag, args.std_c, args.files
    )
    write(expected, indexed, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
