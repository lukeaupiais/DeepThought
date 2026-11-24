"""Ground-truth C and C++ declarations from clang's AST.

Runs ``clang -fsyntax-only -Xclang -ast-dump=json`` on each translation unit
(every .c, .cc, .cpp and .cxx file under the workspace, or the files given)
and records every named declaration that is written in a repository file:
functions, methods, constructors, destructors, conversion functions, records
(struct, union, class), enums, typedefs, type aliases and namespaces. Implicit
declarations, anonymous ones, forward declarations of records, declarations
local to a function body and declarations from outside the repository are
skipped; a header contributes what the translation units that include it
declare. Declarations written by a macro expansion are recorded at the
expansion.

Output rows (TSV): file, line, name, kind, begin_line. ``line`` is where the
name is written, ``begin_line`` where the declaration begins (a template
declaration begins at ``template``). Every repository file clang read is
listed in a ``# indexed`` row.

Usage:
  python tools/c_declaration_truth.py WORKSPACE [--clang clang] \\
      [--flag=-Iinclude ...] [--std-c c11] [--std-cxx c++20] > truth.tsv
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys

HEADER = "# file\tline\tname\tkind\tbegin_line"
SOURCES = {".c": "c", ".cc": "c++", ".cpp": "c++", ".cxx": "c++"}
KINDS = {
    "FunctionDecl": "function",
    "CXXMethodDecl": "method",
    "CXXConstructorDecl": "method",
    "CXXDestructorDecl": "method",
    "CXXConversionDecl": "method",
    "RecordDecl": "type",
    "CXXRecordDecl": "type",
    "EnumDecl": "type",
    "TypedefDecl": "alias",
    "TypeAliasDecl": "alias",
    "NamespaceDecl": "namespace",
    "ClassTemplateSpecializationDecl": "type",
    "ClassTemplatePartialSpecializationDecl": "type",
}
RECORDS = {
    "RecordDecl",
    "CXXRecordDecl",
    "ClassTemplateSpecializationDecl",
    "ClassTemplatePartialSpecializationDecl",
}
TEMPLATES = {"ClassTemplateDecl", "FunctionTemplateDecl", "TypeAliasTemplateDecl"}


class _Files:
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
        """(file, line) of a location, at the macro expansion if any."""
        if not location:
            return None, None
        if "expansionLoc" in location:
            self._bare(location.get("spellingLoc", {}))
            return self._bare(location["expansionLoc"])
        return self._bare(location)


def declarations(ast, root):
    """Yield (file, line, name, kind, begin_line) for repository declarations."""
    files = _Files()
    root = Path(root).resolve()
    indexed = set()

    def relative(path):
        if path is None:
            return None
        resolved = (root / path).resolve()
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError:
            return None

    def visit(node, template_begin=None, local=False):
        file, line = files.point(node.get("loc"))
        span = node.get("range") or {}
        begin_file, begin = files.point(span.get("begin"))
        files.point(span.get("end"))
        kind = KINDS.get(node.get("kind"))
        here = relative(file)
        if here is not None:
            indexed.add(here)
        found = []
        name = node.get("name") or ""
        if not name.startswith("operator"):
            # A constructor of a class template is named `C<T>` in the dump.
            name = name.split("<")[0]
        forward = node.get("kind") in RECORDS and not node.get("completeDefinition")
        if (
            kind
            and here is not None
            and name
            and not node.get("isImplicit")
            and not local
            and not forward
        ):
            found.append((here, line, name, kind, template_begin or begin or line))
        template = node.get("kind") in TEMPLATES
        # Declarations inside a function body or lambda are local.
        local = (
            local
            or kind in {"function", "method"}
            or node.get("kind") == ("LambdaExpr")
        )
        for child in node.get("inner", ()):
            if template and child.get("kind") == "ClassTemplateSpecializationDecl":
                # An instantiation, not written in the source.
                continue
            found += visit(child, begin if template else None, local)
        return found

    return visit(ast), indexed


def run(workspace, clang="clang", flags=(), std_c="c11", std_cxx="c++20", files=()):
    workspace = Path(workspace).resolve()
    units = (
        [workspace / f for f in files]
        if files
        else sorted(
            p
            for p in workspace.rglob("*")
            if p.suffix in SOURCES and p.is_file() and ".git" not in p.parts
        )
    )
    rows, indexed = set(), set()
    for unit in units:
        language = SOURCES[unit.suffix]
        command = [
            clang,
            "-x",
            language,
            f"-std={std_c if language == 'c' else std_cxx}",
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
        if not completed.stdout.strip():
            print(f"{unit}: no AST ({completed.stderr[:200]})", file=sys.stderr)
            continue
        found, seen = declarations(json.loads(completed.stdout), workspace)
        rows.update(found)
        indexed.update(seen)
    return sorted(rows), sorted(indexed)


def write(rows, indexed, stream):
    print(HEADER, file=stream)
    for file in indexed:
        print(f"# indexed\t{file}", file=stream)
    for row in rows:
        print("\t".join(map(str, row)), file=stream)


def read(path):
    rows, indexed = set(), set()
    for line in Path(path).read_text("utf-8").splitlines():
        if line.startswith("# indexed\t"):
            indexed.add(line.split("\t", 1)[1])
        elif line and not line.startswith("#"):
            file, number, name, kind, begin = line.split("\t")
            rows.add((file, int(number), name, kind, int(begin)))
    return rows, indexed


ENGINE_KINDS = {
    "Function": "function",
    "Method": "method",
    "Class": "type",
    "Struct": "type",
    "Union": "type",
    "Enum": "type",
    "TypeAlias": "alias",
    "Namespace": "namespace",
}


def score(result, truth, examples=10):
    """Engine declarations against the truth, in the files clang read.

    A truth declaration is *found* when the engine declares the same name in
    the same file at its name line or begin line; *kind_mismatch* when found
    only with another kind. An engine declaration of a listed kind matching
    no truth row is *extra*: clang did not see it (a branch the preprocessor
    dropped, a macro clang expanded otherwise) or the engine misread it.
    """
    rows, indexed = truth
    engine = {}
    for d in result.definitions:
        if d.file in indexed and d.kind in ENGINE_KINDS:
            name = d.symbol.replace("::", ".").split(".")[-1]
            engine.setdefault((d.file, name), []).append(
                (d.line, ENGINE_KINDS[d.kind], d)
            )
    found, mismatched, missed, used = [], [], [], set()
    for row in sorted(rows):
        file, line, name, kind, begin = row
        candidates = [
            c
            for c in engine.get((file, name), ())
            if c[0] in {line, begin} and id(c[2]) not in used
        ]
        exact = [c for c in candidates if c[1] == kind]
        if exact or candidates:
            used.add(id((exact or candidates)[0][2]))
            (found if exact else mismatched).append(row)
        else:
            missed.append(row)
    extra = sorted(
        (c[2].file, c[0], key[1], c[1])
        for key, items in engine.items()
        for c in items
        if id(c[2]) not in used
    )
    by_kind = {}
    for outcome, items in (("found", found), ("missed", missed)):
        for row in items:
            by_kind.setdefault(row[3], {"found": 0, "missed": 0})[outcome] += 1
    report = {
        "expected": len(rows),
        "found": len(found),
        "kind_mismatch": len(mismatched),
        "missed": len(missed),
        "extra": len(extra),
        "recall": round(len(found) / len(rows), 4) if rows else None,
        "by_kind": dict(sorted(by_kind.items())),
    }
    samples = {
        "kind_mismatch": mismatched[:examples],
        "missed": missed[:examples],
        "extra": extra[:examples],
    }
    return report, samples


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("files", nargs="*")
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--flag", action="append", default=[])
    parser.add_argument("--std-c", default="c11")
    parser.add_argument("--std-cxx", default="c++20")
    args = parser.parse_args(argv)
    rows, indexed = run(
        args.workspace, args.clang, args.flag, args.std_c, args.std_cxx, args.files
    )
    write(rows, indexed, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
