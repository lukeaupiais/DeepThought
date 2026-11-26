"""Compare Tree-sitter INHERITS_FROM links with compiler-backed SCIP references.

For each base a class, interface or Rust trait names, SCIP's resolved reference at the
base's last identifier gives the declaration the compiler binds it to.
Precision: linked bases whose target declaration starts at that SCIP
definition's line. Recall: bases with an in-repository SCIP target that
Tree-sitter linked correctly.

Usage:
  python tools/compare_supertypes_with_scip.py ROOT INDEX.scip --language python \\
      [--include 'src/*'] [--path-prefix src]
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import re

import deepthought
from deepthought.inheritance import resolve_supertypes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("index", type=Path)
    parser.add_argument(
        "--language",
        required=True,
        choices=("go", "javascript", "python", "rust", "typescript"),
    )
    parser.add_argument("--include", action="append")
    parser.add_argument("--path-prefix", default="")
    args = parser.parse_args()
    from deepthought.ts_modules import with_compiler_modules

    result = with_compiler_modules(
        deepthought.analyze_repository(
            args.root,
            repository_id="comparison",
            languages=(args.language,),
            include=tuple(args.include or ("*",)),
        ),
        args.root,
    )
    source = deepthought.ingest_scip(
        args.index, repository_id="comparison", path_prefix=args.path_prefix
    )
    graph = deepthought.compose_graph([source])
    scip_defs = {node.id: node.definition for node in graph.definitions}
    at = {}
    for ref in graph.references:
        if ref.resolution == "resolved":
            r = ref.reference
            at[(r.file, r.line, r.column)] = scip_defs[ref.target_ids[0]]
    definitions = {d.id: d for d in result.definitions}
    content = {}

    def expected(item):
        """SCIP definition referenced at the base's last identifier, if any."""
        if item.file not in content:
            content[item.file] = (args.root / item.file).read_bytes().split(b"\n")
        line = content[item.file][item.line - 1]
        last = re.split(r"\.|::", item.name)[-1]
        start = line.find(last.encode(), item.column)
        return None if start < 0 else at.get((item.file, item.line, start))

    counts = Counter()
    by_rule, unlinked, contradicted = {}, Counter(), []
    items = {item.id: item for item in result.supertypes}
    for resolution in resolve_supertypes(result):
        item = items[resolution.call_id]
        counts["bases"] += 1
        scip_def = expected(item)
        if scip_def is not None:
            counts["scip_in_repo_targets"] += 1
        if resolution.status != "linked":
            if scip_def is not None:
                unlinked[resolution.status + ":" + str(resolution.reason)] += 1
            continue
        target = definitions[resolution.target_id]
        rule = by_rule.setdefault(resolution.rule_id, {"linked": 0, "confirmed": 0})
        counts["linked"] += 1
        rule["linked"] += 1
        if scip_def is None:
            counts["linked_no_scip_target"] += 1
        elif scip_def.file == target.file and scip_def.line == target.line:
            counts["linked_confirmed"] += 1
            rule["confirmed"] += 1
        else:
            counts["linked_contradicted"] += 1
            contradicted.append(
                [item.file, item.line, item.name, target.file, target.symbol]
            )
    print(
        json.dumps(
            {
                **dict(sorted(counts.items())),
                "by_rule": by_rule,
                "scip_targets_not_linked": dict(sorted(unlinked.items())),
                "contradicted": contradicted[:20],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
