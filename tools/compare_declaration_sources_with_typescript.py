"""Compare stored TypeScript declaration text with the TypeScript parser.

The oracle (tools/typescript_declaration_source_oracle.cjs) parses each analyzed
file with the TypeScript compiler and records every named node's text and its
name position in UTF-16 columns. A stored declaration matches when a node has
its name at the same UTF-16 position and the same text and first line.

Usage:
  python tools/compare_declaration_sources_with_typescript.py ROOT \
      --typescript node_modules/typescript [--node node]
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import tempfile

from deepthought import analyze_repository
from deepthought.analysis_tools.tree_sitter import _declaration_sources

ORACLE = Path(__file__).with_name("typescript_declaration_source_oracle.cjs")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--typescript", required=True, type=Path)
    parser.add_argument("--node", default="node")
    args = parser.parse_args()
    result = analyze_repository(args.root, repository_id="r", languages=("typescript",))
    sources, changed = _declaration_sources(args.root, result)
    files = sorted({d.file for d in result.definitions})
    with tempfile.TemporaryDirectory() as directory:
        listing = Path(directory) / "files.json"
        listing.write_text(json.dumps(files))
        report = Path(directory) / "oracle.json"
        subprocess.run(
            [args.node, ORACLE, args.root, args.typescript, listing, report],
            check=True,
        )
        oracle = {}
        for item in json.loads(report.read_text()):
            oracle.setdefault((item["file"], *item["name"]), []).append(item)
    counts, mismatches = Counter(), []
    for d in result.definitions:
        stored = sources[d.id]
        candidates = oracle.get((d.file, *stored["name_range_utf16"]), [])
        if not candidates:
            outcome = "no_counterpart"
        elif any(
            c["text"] == stored["source_code"]
            and c["start_line"] == stored["source_start_line"]
            for c in candidates
        ):
            outcome = "matched"
        elif any(
            c["text"] == stored["source_code"] + ";"
            and c["start_line"] == stored["source_start_line"]
            for c in candidates
        ):
            # Signatures without a body: the compiler's node includes the ';'.
            outcome = "matched_without_terminating_semicolon"
        else:
            outcome = "different"
        counts[outcome] += 1
        if not outcome.startswith("matched"):
            mismatches.append(
                [d.file, d.symbol, outcome, [c["kind"] for c in candidates]]
            )
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
