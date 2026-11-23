"""Compare SCIP IMPLEMENTS edges with the TypeScript compiler's supertypes.

The oracle (tools/typescript_supertype_oracle.cjs) runs the TypeScript checker
on the same workspace and never reads SCIP. It lists, by declaration-name
position, each class or interface's transitive extends/implements supertypes
and the same-named members of those supertypes, and whether the source type
(or the member's owner) is an interface. This script runs the SCIP
AnalysisTool and compares its IMPLEMENTS edges with those pairs.

Usage:
  node tools/typescript_supertype_oracle.cjs WORKSPACE TYPESCRIPT_DIR oracle.json
  python tools/compare_implementations_with_typescript.py WORKSPACE oracle.json \\
      [--index WORKSPACE/index.scip] [--examples N]
"""

import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path

from deepthought.analysis_tools import ScipAnalysisTool


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("oracle", type=Path)
    parser.add_argument("--index", type=Path)
    parser.add_argument("--examples", type=int, default=3)
    args = parser.parse_args()
    output = asyncio.run(
        ScipAnalysisTool(args.workspace, index_path=args.index).analyze("comparison")
    )
    nodes = {n.properties["qualified_name"]: n.properties for n in output.nodes}

    def at(qualified_name):
        p = nodes[qualified_name]
        return (p["file_path"], p["start_line"], p["column"])

    edges = {
        (at(r.source_match["qualified_name"]), at(r.target_match["qualified_name"]))
        for r in output.relationships
        if r.type == "IMPLEMENTS"
    }
    oracle = json.loads(args.oracle.read_text())
    expected = {(tuple(p["source"]), tuple(p["target"])): p for p in oracle["pairs"]}
    counts, examples = Counter(), {}

    def note(category, pair):
        counts[category] += 1
        examples.setdefault(category, [])
        if len(examples[category]) < args.examples:
            examples[category].append(pair)

    for pair, item in expected.items():
        if pair in edges:
            note(f"same {item['kind']}", pair)
        elif item["source_interface"]:
            note(f"missing {item['kind']} (source is an interface)", pair)
        else:
            note(f"missing {item['kind']}", pair)
    for pair in edges - expected.keys():
        note("extra", pair)
    print(
        json.dumps(
            {
                "compiler": oracle["compiler"],
                "files": oracle["files"],
                "types": oracle["types"],
                "edges": len(edges),
                "oracle_pairs": len(expected),
                "counts": dict(sorted(counts.items())),
                "metrics": output.metadata.custom_metrics[
                    "implementation_relationships"
                ],
                "examples": {
                    k: v
                    for k, v in sorted(examples.items())
                    if not k.startswith("same")
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
