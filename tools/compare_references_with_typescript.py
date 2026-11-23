"""Compare stored SCIP REFERENCES links with the TypeScript compiler's symbols.

The oracle (tools/typescript_reference_oracle.cjs) runs the TypeScript checker
on the same workspace and never reads SCIP. For each identifier whose symbol,
after following import aliases, is declared in the project, it gives the
declaration name ranges. This script runs the SCIP AnalysisTool and compares,
at each oracle identifier, the definitions its REFERENCES edges point to.

Usage:
  node tools/typescript_reference_oracle.cjs WORKSPACE TYPESCRIPT_DIR oracle.json
  python tools/compare_references_with_typescript.py WORKSPACE oracle.json \\
      [--index WORKSPACE/index.scip]
"""

import argparse
import asyncio
from collections import Counter, defaultdict
import json
from pathlib import Path

from deepthought.analysis_tools import ScipAnalysisTool


def location(properties):
    return (
        properties["file_path"],
        properties["start_line"],
        properties["column"],
        properties["end_line"],
        properties["end_column"],
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("oracle", type=Path)
    parser.add_argument("--index", type=Path)
    args = parser.parse_args()
    output = asyncio.run(
        ScipAnalysisTool(args.workspace, index_path=args.index).analyze("comparison")
    )
    nodes = {n.properties["qualified_name"]: n for n in output.nodes}
    references = {
        location(n.properties): n.properties["qualified_name"]
        for n in output.nodes
        if "Reference" in n.labels
    }
    targets = defaultdict(set)
    for r in output.relationships:
        if r.type == "REFERENCES":
            definition = nodes[r.target_match["qualified_name"]].properties
            targets[r.source_match["qualified_name"]].add(location(definition))
    oracle = json.loads(args.oracle.read_text())
    outcome, samples = Counter(), defaultdict(list)
    seen = set()
    for item in oracle["occurrences"]:
        where = tuple(item["range"])
        expected = {tuple(d) for d in item["declarations"]}
        name = references.get(where)
        seen.add(where)
        if name is None:
            kind = "no_scip_occurrence"
        elif not targets[name]:
            kind = "scip_unresolved"
        elif targets[name] == expected:
            kind = "same_declarations"
        elif targets[name] & expected:
            kind = "overlapping_declarations"
        else:
            kind = "different_declarations"
        outcome[kind] += 1
        if kind != "same_declarations" and len(samples[kind]) < 5:
            samples[kind].append(
                {
                    "range": list(where),
                    "typescript": sorted(map(list, expected)),
                    "scip": sorted(map(list, targets.get(name, ()))),
                }
            )
    linked_elsewhere = [
        where
        for where, name in references.items()
        if targets[name] and where not in seen
    ]
    print(
        json.dumps(
            {
                "compiler": oracle["compiler"],
                "files": oracle["files"],
                "oracle_occurrences": len(oracle["occurrences"]),
                "scip_linked_references": sum(
                    1 for n in references.values() if targets[n]
                ),
                "by_outcome": dict(sorted(outcome.items())),
                "scip_linked_without_oracle_occurrence": len(linked_elsewhere),
                "samples": dict(sorted(samples.items())),
                "linked_without_oracle_samples": [
                    list(w) for w in sorted(linked_elsewhere)[:5]
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
