"""Compare Ruby declarations with Prism's, Ruby's own parser.

Every ``.rb`` file under ROOT is parsed by Prism
(tools/ruby_declarations_prism.rb), and the scoping rules of
deepthought.syntax.extract_ruby are applied to Prism's tree: modules,
classes and methods, with symbols joining enclosing modules and classes.
A declaration matches when Prism reports one of the same kind, name and
symbol at the same line and column of its name in the same file. A
difference on a line where Prism reported a syntax error, or after one in
the same file, is put down to it; every other difference is unexplained.

Usage:
  python tools/compare_ruby_declarations_with_prism.py ROOT
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess

import deepthought
from deepthought.syntax import extract_ruby, make_parser

SCRIPT = Path(__file__).with_name("ruby_declarations_prism.rb")


def prism_declarations(root, files):
    found, errors = set(), set()
    for start in range(0, len(files), 200):
        output = subprocess.run(
            ["ruby", str(SCRIPT), str(root), *files[start : start + 200]],
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    result = deepthought.analyze_repository(
        root, repository_id="comparison", languages=("ruby",)
    )
    files = sorted(c.file for c in result.coverage if c.language == "ruby")
    prism, errors = prism_declarations(root, files)
    source_parser = make_parser("ruby", ".rb")
    ours = set()
    for file in files:
        content = (root / file).read_bytes()
        nodes = {}
        declared = extract_ruby(
            source_parser.parse(content), content, "comparison", file, nodes=nodes
        )
        for definition in declared:
            _, name = nodes[definition.id]
            line = content.split(b"\n")[name.start_point.row]
            column = len(line[: name.start_point.column].decode("utf-8"))
            ours.add(
                (
                    file,
                    name.start_point.row + 1,
                    column,
                    content[name.start_byte : name.end_byte].decode("utf-8"),
                    definition.kind,
                    definition.symbol,
                )
            )
    counts, examples = Counter(), {}
    for side, items, other in (
        ("tree_sitter_only", ours - prism, prism),
        ("prism_only", prism - ours, ours),
    ):
        for item in sorted(items):
            if (item[0], item[1]) in errors:
                outcome = side + "_syntax_error"
            elif any(f == item[0] and n < item[1] for f, n in errors):
                # Parsers recover from an error differently (an ERB
                # template read as Ruby).
                outcome = side + "_after_syntax_error"
            else:
                outcome = side + "_unexplained"
            counts[outcome] += 1
            examples.setdefault(outcome, []).append(list(item))
    counts["matched"] = len(ours & prism)
    print(
        json.dumps(
            {
                "files": len(files),
                "files_with_syntax_errors": len({f for f, _ in errors}),
                "tree_sitter": len(ours),
                "prism": len(prism),
                **dict(sorted(counts.items())),
                "examples": {k: v[:15] for k, v in sorted(examples.items())},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
