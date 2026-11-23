"""Compare Tree-sitter Java declarations with javac's own parser.

tools/JavaDeclarationOracle.java lists every named type, method, constructor
and annotation element javac parses, with its lexically qualified name, kind
and character span. A Tree-sitter declaration matches when its file, symbol,
kind, start and end are the same. The stored name range must also cover the
declared name: the last part of the symbol.

Files javac rejects as syntax errors are reported, not compared.

Usage:
  python tools/compare_java_declarations_with_javac.py ROOT [--java JAVA]
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess

import deepthought
from deepthought.analysis_tools.tree_sitter import _declaration_sources

ORACLE = Path(__file__).with_name("JavaDeclarationOracle.java")


def _offsets(text):
    """(line, UTF-8 byte column) -> UTF-16 offset, as javac counts characters."""
    starts, total = [], 0
    for line in text.splitlines(keepends=True):
        starts.append((line, total))
        total += len(line.encode("utf-16-le")) // 2

    def offset(line, column):
        content, start = starts[line - 1]
        prefix = content.encode()[:column].decode()
        return start + len(prefix.encode("utf-16-le")) // 2

    return offset


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--java", default="java")
    args = parser.parse_args()
    root = args.root.resolve()
    oracle = subprocess.run(
        [args.java, str(ORACLE), str(root)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    javac = Counter(
        (row["file"], row["symbol"], row["kind"], row["start"], row["end"])
        for row in map(json.loads, oracle.splitlines())
    )
    result = deepthought.analyze_repository(
        root,
        repository_id="comparison",
        languages=("java",),
        max_files=1_000_000,
        max_total_bytes=10**10,
    )
    sources, _ = _declaration_sources(root, result)
    offsets, ours, names = {}, Counter(), Counter()
    for d in result.definitions:
        if d.file not in offsets:
            offsets[d.file] = _offsets((root / d.file).read_text())
        offset = offsets[d.file]
        ours[
            (
                d.file,
                d.symbol,
                d.kind,
                offset(d.line, d.column),
                offset(d.end_line, d.end_column),
            )
        ] += 1
        line, column, end_line, end_column = sources[d.id]["name_range_utf8"]
        text = (root / d.file).read_bytes().splitlines()[line - 1]
        named = line == end_line and (
            text[column:end_column].decode() == d.symbol.split(".")[-1]
        )
        names["name_range_covers_name" if named else "name_range_differs"] += 1
    coverage = Counter(item.status for item in result.coverage)
    print(
        json.dumps(
            {
                "files": dict(sorted(coverage.items())),
                "tree_sitter_declarations": sum(ours.values()),
                "javac_declarations": sum(javac.values()),
                "matched": sum((ours & javac).values()),
                "kinds": dict(sorted(Counter(k[2] for k in ours.elements()).items())),
                **dict(sorted(names.items())),
                "tree_sitter_only": sorted(map(list, (ours - javac).elements()))[:20],
                "javac_only": sorted(map(list, (javac - ours).elements()))[:20],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
