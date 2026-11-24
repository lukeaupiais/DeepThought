"""Compare Tree-sitter CALLS edges with compiler-backed SCIP references.

For each recorded call site, SCIP's resolved reference at the callee name (the
last identifier of the callee) gives the declaration a compiler links it to.
Precision: linked calls whose target contains that SCIP definition. Recall:
call sites with an in-repository SCIP target that Tree-sitter linked correctly.

Usage:
  python tools/compare_calls_with_scip.py ROOT INDEX.scip --language python \
      [--include 'src/*'] [--path-prefix src]
"""

import argparse
import json
import re
from collections import Counter
from pathlib import Path

import deepthought
from deepthought.calls import resolve_calls


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("index", type=Path)
    parser.add_argument(
        "--language",
        required=True,
        choices=("go", "javascript", "python", "typescript"),
    )
    parser.add_argument("--include", action="append")
    parser.add_argument("--path-prefix", default="")
    args = parser.parse_args()
    result = deepthought.analyze_repository(
        args.root,
        repository_id="comparison",
        languages=(args.language,),
        include=tuple(args.include or ("*",)),
    )
    source = deepthought.ingest_scip(
        args.index, repository_id="comparison", path_prefix=args.path_prefix
    )
    graph = deepthought.compose_graph([source])
    scip_defs = {node.id: node.definition for node in graph.definitions}
    # (file, line, column) of each resolved reference -> its definition.
    at = {}
    for ref in graph.references:
        if ref.resolution == "resolved":
            r = ref.reference
            at[(r.file, r.line, r.column)] = scip_defs[ref.target_ids[0]]
    definitions = {d.id: d for d in result.definitions}
    content = {}

    def expected(site):
        """SCIP definition referenced at the callee's last identifier, if any."""
        if site.file not in content:
            content[site.file] = (args.root / site.file).read_bytes().split(b"\n")
        line = content[site.file][site.line - 1]
        name = re.escape(site.callee.split(".")[-1].encode())
        # A whole identifier, so `collect` is not found inside `collector`.
        match = re.compile(rb"(?<![\w$])" + name + rb"(?![\w$])").search(
            line, site.column
        )
        return None if match is None else at.get((site.file, site.line, match.start()))

    def contains(target, scip_def):
        return (
            scip_def is not None
            and scip_def.file == target.file
            and target.line <= scip_def.line <= target.end_line
        )

    counts = {
        "call_sites": 0,
        "linked": 0,
        "linked_confirmed": 0,
        "linked_contradicted": 0,
        "linked_no_scip_target": 0,
        "scip_in_repo_targets": 0,
        "scip_targets_linked": 0,
    }
    by_rule, contradicted, unwitnessed = {}, [], []
    sites = {site.id: site for site in result.call_sites}
    for resolution in resolve_calls(result):
        site = sites[resolution.call_id]
        counts["call_sites"] += 1
        scip_def = expected(site)
        if scip_def is not None:
            counts["scip_in_repo_targets"] += 1
        if resolution.status != "linked":
            continue
        target = definitions[resolution.target_id]
        rule = by_rule.setdefault(resolution.rule_id, {"linked": 0, "confirmed": 0})
        counts["linked"] += 1
        rule["linked"] += 1
        if scip_def is None:
            counts["linked_no_scip_target"] += 1
            unwitnessed.append(site.file)
        elif contains(target, scip_def):
            counts["linked_confirmed"] += 1
            counts["scip_targets_linked"] += 1
            rule["confirmed"] += 1
        else:
            counts["linked_contradicted"] += 1
            contradicted.append(
                [site.file, site.line, site.callee, target.file, target.symbol]
            )
    print(
        json.dumps(
            {
                **counts,
                "by_rule": by_rule,
                "contradicted": contradicted[:20],
                "unwitnessed_by_directory": dict(
                    sorted(Counter(f.split("/")[0] for f in unwitnessed).items())
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
