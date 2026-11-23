"""Compare stored Python declaration text with CPython's own parser.

For every Python function and class the tree-sitter projection stores, the
expected text is cut from the file by the ``ast`` module's positions: from the
first decorator's ``@`` (or the ``def``/``class`` keyword) to the end of the
body. A declaration matches when its stored text, span and name position are
exactly what ``ast`` gives.

Usage:
  python tools/compare_declaration_sources_with_ast.py ROOT
"""

import argparse
import ast
from collections import Counter
import json
import re
from pathlib import Path

from deepthought import analyze_repository
from deepthought.analysis_tools.tree_sitter import _declaration_sources

_DECLARATIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_HEADER = re.compile(rb"(?:async\s+)?(?:def|class)\s+")


def _offset(lines, line, column):
    return sum(len(text) for text in lines[: line - 1]) + column


def expected(source: bytes):
    """(line, column) of each def/class keyword -> (text, span, name column)."""
    lines = source.splitlines(keepends=True)
    found = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, _DECLARATIONS):
            continue
        start = (node.lineno, node.col_offset)
        if node.decorator_list:
            first = node.decorator_list[0]
            # The '@' precedes the decorator expression on its line.
            at = lines[first.lineno - 1].rindex(b"@", 0, first.col_offset)
            start = (first.lineno, at)
        end = (node.end_lineno, node.end_col_offset)
        text = source[_offset(lines, *start) : _offset(lines, *end)].decode()
        header = _HEADER.match(lines[node.lineno - 1], node.col_offset)
        name = header.end()
        found[(node.lineno, node.col_offset)] = (text, (*start, *end), name)
    return found


def _only_comments(text):
    return all(
        not line.strip() or line.strip().startswith("#") for line in text.split("\n")
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    result = analyze_repository(args.root, repository_id="r", languages=("python",))
    sources, changed = _declaration_sources(args.root, result)
    counts, mismatches = Counter(), []
    by_file = {}
    for definition in result.definitions:
        by_file.setdefault(definition.file, []).append(definition)
    for file, definitions in sorted(by_file.items()):
        try:
            truth = expected((args.root / file).read_bytes())
        except SyntaxError:
            counts["file_not_parsed_by_ast"] += len(definitions)
            continue
        for d in definitions:
            stored = sources.get(d.id)
            want = truth.get((d.line, d.column))
            if stored is None or want is None:
                counts["no_counterpart"] += 1
                mismatches.append([file, d.symbol, "no_counterpart"])
                continue
            text, span, name = want
            got_span = (
                stored["source_start_line"],
                stored["source_start_column"],
                stored["source_end_line"],
                stored["source_end_column"],
            )
            if got_span == span and stored["source_code"] == text:
                outcome = "matched"
            elif stored["source_code"].startswith(text) and _only_comments(
                stored["source_code"][len(text) :]
            ):
                # tree-sitter keeps comments that end the body; ast does not.
                outcome = "matched_plus_trailing_comments"
            else:
                outcome = "different"
            if stored["name_range_utf8"][:2] != [d.line, name]:
                outcome = "different_name_position"
            counts[outcome] += 1
            if outcome.startswith("different"):
                mismatches.append([file, d.symbol, outcome])
    print(
        json.dumps(
            {
                "declarations": len(result.definitions),
                "files_changed_since_analysis": changed,
                **dict(sorted(counts.items())),
                "mismatches": mismatches[:20],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
