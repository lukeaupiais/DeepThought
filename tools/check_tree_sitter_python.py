"""Independent source declaration reference using CPython ast, never tree-sitter."""

import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys


class Declarations(ast.NodeVisitor):
    def __init__(self, file):
        self.file = file
        self.scope = []
        self.rows = []

    def declaration(self, node, is_class=False):
        kind = (
            "Class"
            if is_class
            else (
                "Method" if self.scope and self.scope[-1][1] == "Class" else "Function"
            )
        )
        name = ".".join([part[0] for part in self.scope] + [node.name])
        self.rows.append((self.file, name, kind, node.lineno))
        self.scope.append((node.name, "Class" if is_class else "Function"))
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node):
        self.declaration(node, is_class=True)

    def visit_FunctionDef(self, node):
        self.declaration(node)

    def visit_AsyncFunctionDef(self, node):
        self.declaration(node)


def check(prepared_file, output_file):
    prepared_file = Path(prepared_file)
    output_file = Path(output_file)
    prepared = json.loads(prepared_file.read_text())
    workspace = Path(prepared["provenance"]["source"]["workspace"]).resolve()
    expected = []
    files = []
    for name, fingerprint in sorted(prepared["scenario"]["source"]["files"].items()):
        path = (workspace / name).resolve(strict=True)
        if not path.is_relative_to(workspace):
            raise ValueError("Source escapes workspace: " + name)
        content = path.read_bytes()
        actual_fingerprint = hashlib.sha256(content).hexdigest()
        if actual_fingerprint != fingerprint:
            raise ValueError("Source fingerprint mismatch: " + name)
        visitor = Declarations(name)
        visitor.visit(ast.parse(content, filename=name))
        expected.extend(visitor.rows)
        files.append(
            {
                "file": name,
                "sha256": fingerprint,
                "declarations": len(visitor.rows),
                "lines": len(content.splitlines()),
            }
        )
    actual = []
    for node in prepared["private_tool_output"]["nodes"]:
        if "Symbol" in node["labels"]:
            p = node["properties"]
            actual.append((p["file_path"], p["name"], p["kind"], p["start_line"]))
    expected_counter = Counter(expected)
    actual_counter = Counter(actual)

    def rows(counter):
        return [
            dict(zip(("file", "symbol", "kind", "line"), row), occurrences=count)
            for row, count in sorted(counter.items())
        ]

    missing = rows(expected_counter - actual_counter)
    extra = rows(actual_counter - expected_counter)
    result = {
        "schema_version": 1,
        "method": "Independent CPython stdlib ast lexical declaration walk. Names join enclosing class/function scopes, direct class-scope functions are Methods, nested functions are Functions. Declaration line is class/def/async-def line, excluding decorators. Imports, annotations and anonymous lambdas are excluded. Counters preserve duplicated declarations.",
        "checker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "prepared_sha256": hashlib.sha256(prepared_file.read_bytes()).hexdigest(),
        "source": prepared["scenario"]["source"],
        "workspace": str(workspace),
        "input_id": prepared["input_id"],
        "expected_count": len(expected),
        "actual_count": len(actual),
        "matched_count": sum((expected_counter & actual_counter).values()),
        "missing": missing,
        "extra": extra,
        "exact_match": not missing and not extra,
        "files": files,
    }
    output_file.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "expected_count",
                    "actual_count",
                    "matched_count",
                    "exact_match",
                    "missing",
                    "extra",
                )
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(
            "Usage: check_tree_sitter_python.py prepared.private.json output.json"
        )
    check(sys.argv[1], sys.argv[2])
