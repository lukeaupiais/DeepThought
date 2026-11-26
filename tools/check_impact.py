"""Check Neo4j impact queries against an in-memory recomputation.

Indexes a workspace with the tree-sitter tool, writes it to the configured
Neo4j database, and for a deterministic sample of declarations (every Nth by
qualified name) compares get_impact with a breadth-first search over the
tool's own relationships: same dependents, depths and path evidence. The
recomputation never reads Neo4j. The repository is deleted afterwards.

Usage (DEEPTHOUGHT_NEO4J_URI/_USER/_PASSWORD set):
  python tools/check_impact.py WORKSPACE --language python [--include 'pkg/*'] \\
      [--every 50] [--max-depth 3]
"""

import argparse
import asyncio
from collections import defaultdict, deque
import json
import os
from uuid import uuid4

from deepthought.analysis_tools import TreeSitterTool
from deepthought.graph_queries import GraphQueryService
from deepthought.storage.graph import (
    Neo4jConnectionManager,
    WriteCoordinator,
    setup_schema,
)

WEAKNESS = {"resolved": 0, "pattern_matched": 1, "name_matched": 2}
EVIDENCE = ["resolved", "pattern_matched", "name_matched"]
# Code relations; get_impact also follows a test's USES_FIXTURE edges and an
# endpoint's USES_DEPENDENCY edges.
CODE_RELATIONS = {"CALLS", "INHERITS_FROM", "IMPLEMENTS", "DISPATCHES_TO"}
RELATIONS = CODE_RELATIONS | {"USES_FIXTURE", "USES_DEPENDENCY"}


def expected(output, root, max_depth, relations=RELATIONS):
    """{dependent qualified_name: (depth, evidence)} by reverse BFS over
    ``relations``."""
    incoming = defaultdict(list)
    children = defaultdict(list)
    for r in output.relationships:
        source = r.source_match["qualified_name"]
        target = r.target_match["qualified_name"]
        if r.type in relations:
            incoming[target].append((source, WEAKNESS[r.properties["evidence_type"]]))
        elif r.type == "CONTAINS" and "Symbol" in r.source_labels:
            children[source].append(target)
    changed, stack = {root}, [root]
    while stack:
        for child in children[stack.pop()]:
            if child not in changed:
                changed.add(child)
                stack.append(child)
    found = {}
    for limit in range(3):
        depth = {node: 0 for node in changed}
        queue = deque(changed)
        while queue:
            node = queue.popleft()
            if depth[node] == max_depth:
                continue
            for source, weakness in incoming[node]:
                if weakness <= limit and source not in depth:
                    depth[source] = depth[node] + 1
                    queue.append(source)
        for node, d in depth.items():
            if node not in changed and node not in found:
                found[node] = (d, EVIDENCE[limit])
    return found


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace")
    parser.add_argument("--language", required=True)
    parser.add_argument("--include", action="append")
    parser.add_argument("--every", type=int, default=50)
    parser.add_argument("--max-depth", type=int, default=3)
    args = parser.parse_args()
    repo = "impact-check-" + uuid4().hex
    output = await TreeSitterTool(args.workspace).analyze(
        repo, languages=(args.language,), file_patterns=args.include
    )
    manager = Neo4jConnectionManager(
        os.environ["DEEPTHOUGHT_NEO4J_URI"],
        os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
        os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
        database=os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j"),
    )
    await manager.connect()
    try:
        await setup_schema(manager.driver, database=manager.database)
        written = await WriteCoordinator(
            manager.driver, database=manager.database
        ).write_tool_output("tree-sitter", output, repo)
        if not written.success:
            raise SystemExit(written.errors)
        snapshot = output.metadata.custom_metrics["snapshot_id"]
        service = GraphQueryService(manager.driver, database=manager.database)
        symbols = sorted(
            n.properties["qualified_name"] for n in output.nodes if "Symbol" in n.labels
        )
        counts = {"seeds": 0, "dependents": 0, "matching": 0, "mismatches": []}
        for qname in symbols[:: args.every]:
            want = expected(output, qname, args.max_depth)
            got = await service.get_impact(
                qname,
                repo,
                snapshot_id=snapshot,
                max_depth=args.max_depth,
                limit=100,
                max_output_bytes=65536,
                timeout=30,
            )
            if got["truncated"]:
                counts.setdefault("truncated_seeds", 0)
                counts["truncated_seeds"] += 1
                continue
            have = {
                item["element"]["qualified_name"]: (
                    item["depth"],
                    item["evidence_type"],
                )
                for item in got["items"]
            }
            counts["seeds"] += 1
            counts["dependents"] += len(want)
            if have == want:
                counts["matching"] += 1
            else:
                counts["mismatches"].append(qname)
        print(json.dumps(counts, indent=2))
    finally:
        async with manager.driver.session(database=manager.database) as session:
            await (
                await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=repo
                )
            ).consume()
        await manager.close()


if __name__ == "__main__":
    asyncio.run(main())
