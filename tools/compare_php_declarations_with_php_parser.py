"""Compare PHP declarations with nikic/PHP-Parser's.

Every ``.php`` file under ROOT is parsed by PHP-Parser
(tools/php_declarations_php_parser.php, run with ``php``), and the scoping
rules of deepthought.syntax.extract_php are applied to its tree: classes,
interfaces, traits, enums, functions and methods, with symbols joining the
namespace and the enclosing class-like declaration. A declaration matches
when PHP-Parser reports one of the same kind, name and symbol at the same
line and byte column of its name in the same file. A difference on a line
where PHP-Parser reported a syntax error, or after one in the same file, is
put down to it; every other difference is unexplained.

Usage:
  python tools/compare_php_declarations_with_php_parser.py PHP_PARSER_DIR ROOT

PHP_PARSER_DIR is a checkout of https://github.com/nikic/PHP-Parser (the
figures in docs/graph-port.md use v5.4.0).
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess

import deepthought
from deepthought.syntax import extract_php, make_parser

SCRIPT = Path(__file__).with_name("php_declarations_php_parser.php")


def parser_declarations(library, root, files):
    found, errors = set(), set()
    for start in range(0, len(files), 200):
        output = subprocess.run(
            ["php", str(SCRIPT), str(library), str(root), *files[start : start + 200]],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        for line in output.splitlines():
            item = json.loads(line)
            errors |= {(item["file"], n) for n in item["error_lines"]}
            for d in item["declarations"]:
                found.add(
                    (
                        d["file"],
                        d["line"],
                        d["column"],
                        d["name"],
                        d["kind"],
                        d["symbol"],
                    )
                )
    return found, errors


def _grammar_errors(root):
    """(first line, last line) of each part of the tree the grammar could
    not parse."""
    spans, stack = [], [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            spans.append((node.start_point.row + 1, node.end_point.row + 1))
        elif node.has_error:
            stack.extend(node.children)
    return spans


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("php_parser", type=Path)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    result = deepthought.analyze_repository(
        root, repository_id="comparison", languages=("php",)
    )
    files = sorted(c.file for c in result.coverage if c.language == "php")
    theirs, errors = parser_declarations(args.php_parser.resolve(), root, files)
    source_parser = make_parser("php", ".php")
    ours = set()
    grammar_errors = {}
    for file in files:
        content = (root / file).read_bytes()
        nodes = {}
        tree = source_parser.parse(content)
        grammar_errors[file] = _grammar_errors(tree.root_node)
        declared = extract_php(tree, content, "comparison", file, nodes=nodes)
        for definition in declared:
            _, name = nodes[definition.id]
            ours.add(
                (
                    file,
                    name.start_point.row + 1,
                    name.start_point.column,
                    content[name.start_byte : name.end_byte].decode("utf-8", "replace"),
                    definition.kind,
                    definition.symbol,
                )
            )
    counts, examples = Counter(), {}
    for side, items, other in (
        ("tree_sitter_only", ours - theirs, theirs),
        ("php_parser_only", theirs - ours, ours),
    ):
        for item in sorted(items):
            spans = grammar_errors[item[0]]
            if (item[0], item[1]) in errors:
                outcome = side + "_syntax_error"
            elif any(f == item[0] and n < item[1] for f, n in errors):
                # Parsers recover from an error differently.
                outcome = side + "_after_syntax_error"
            elif any(first <= item[1] <= last for first, last in spans):
                # Code PHP accepts and the grammar does not (a function
                # named after a keyword, `function readonly()`).
                outcome = side + "_grammar_error"
            elif any(first < item[1] for first, _ in spans):
                outcome = side + "_after_grammar_error"
            else:
                outcome = side + "_unexplained"
            counts[outcome] += 1
            examples.setdefault(outcome, []).append(list(item))
    counts["matched"] = len(ours & theirs)
    print(
        json.dumps(
            {
                "files": len(files),
                "files_with_syntax_errors": len({f for f, _ in errors}),
                "tree_sitter": len(ours),
                "php_parser": len(theirs),
                **dict(sorted(counts.items())),
                "examples": {k: v[:15] for k, v in sorted(examples.items())},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
