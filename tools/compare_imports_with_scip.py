"""Compare Tree-sitter IMPORTS edges with file dependencies witnessed by SCIP.

SCIP dependencies come from compiler-backed references, so they are an
independent check of which file pairs are linked. They are not the same
relation: SCIP also links files through references that need no import (for
example members of inferred types), and an import of a package links to its
__init__.py or index file rather than to where a name is defined.

Usage:
  python tools/compare_imports_with_scip.py ROOT INDEX.scip --language python \
      [--include 'src/*'] [--path-prefix src]
"""

import argparse
import asyncio
import json
from pathlib import Path

import deepthought
from deepthought.analysis_tools import TreeSitterTool


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("index", type=Path)
    parser.add_argument(
        "--language", required=True, choices=("javascript", "python", "typescript")
    )
    parser.add_argument("--include", action="append")
    parser.add_argument("--path-prefix", default="")
    args = parser.parse_args()
    output = asyncio.run(
        TreeSitterTool(args.root).analyze(
            "comparison", languages=[args.language], file_patterns=args.include
        )
    )
    files = {node.properties["path"] for node in output.nodes if "File" in node.labels}
    edges = {}
    for relationship in output.relationships:
        if relationship.type == "IMPORTS":
            pair = tuple(
                match["qualified_name"].split("::")[1]
                for match in (relationship.source_match, relationship.target_match)
            )
            edges.setdefault(pair, set()).add(relationship.properties["rule_id"])
    source = deepthought.ingest_scip(
        args.index, repository_id="comparison", path_prefix=args.path_prefix
    )
    scip = {
        (edge.source_file, edge.target_file)
        for edge in deepthought.compose_graph([source]).edges
        if edge.source_file in files and edge.target_file in files
    }
    rules = {}
    for pair, rule_ids in edges.items():
        for rule_id in rule_ids:
            counts = rules.setdefault(rule_id, {"edges": 0, "in_scip": 0})
            counts["edges"] += 1
            counts["in_scip"] += pair in scip
    print(
        json.dumps(
            {
                "files": len(files),
                "import_resolution": output.metadata.custom_metrics[
                    "import_resolution"
                ],
                "tree_sitter_file_pairs": len(edges),
                "scip_file_pairs": len(scip),
                "shared_file_pairs": len(edges.keys() & scip),
                "by_rule": dict(sorted(rules.items())),
                "only_tree_sitter": sorted(map(list, edges.keys() - scip)),
                "only_scip": sorted(map(list, scip - edges.keys())),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
