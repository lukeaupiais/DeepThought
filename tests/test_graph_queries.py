"""Real Neo4j acceptance of the ported graph-query service (explicit opt-in)."""

import asyncio
import importlib.util
import json
import os
import unittest
from uuid import uuid4

from deepthought.analysis_tools.contracts import (
    NodeData,
    RelationshipData,
    StandardizedOutput,
    ToolExecutionMetadata,
)
from deepthought.graph_queries import GraphQueryService, _bounds


class QueryInputTests(unittest.TestCase):
    def test_bounds_reject_ambiguous_or_unbounded_requests(self):
        for args in [
            (True, 16000, 5),
            (101, 16000, 5),
            (20, 1023, 5),
            (20, 16000, float("inf")),
            (20, 16000, True),
        ]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                _bounds(*args)


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_NEO4J_URI") and importlib.util.find_spec("neo4j"),
    "explicit isolated Neo4j test instance required",
)
class GraphQueryIntegrationTests(unittest.TestCase):
    def test_actual_snapshot_queries_and_cypher_guardrails(self):
        asyncio.run(self.exercise())

    async def exercise(self):
        from neo4j import AsyncGraphDatabase
        from deepthought.storage.graph import setup_schema, WriteCoordinator

        driver = AsyncGraphDatabase.driver(
            os.environ["DEEPTHOUGHT_NEO4J_URI"],
            auth=(
                os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
                os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
            ),
        )
        database = os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j")
        repo = "https://github.com/public/query-test-" + uuid4().hex
        snapshot = "snapshot-" + uuid4().hex

        def node(qname, labels, **props):
            return NodeData(
                labels, {"qualified_name": qname, "repo_url": repo, **props}
            )

        nodes = [
            node("repo", ["Repository"], url=repo),
            node("file", ["File"], path="src/a.py", language="python"),
            node(
                "base",
                ["Class", "Symbol"],
                name="Base",
                kind="Class",
                file_path="src/a.py",
                start_line=1,
                end_line=3,
            ),
            node(
                "child",
                ["Class", "Symbol"],
                name="Child",
                kind="Class",
                file_path="src/a.py",
                start_line=4,
                end_line=10,
            ),
            node(
                "method",
                ["Method", "Symbol"],
                name="Child.run",
                kind="Method",
                file_path="src/a.py",
                start_line=5,
                end_line=8,
            ),
            node(
                "helper",
                ["Function", "Symbol"],
                name="helper",
                kind="Function",
                file_path="src/a.py",
                start_line=12,
                end_line=14,
            ),
            node(
                "imported", ["Import"], name="typing", nested={"a": [1, 2]}, empty=None
            ),
        ]
        byname = {n.properties["qualified_name"]: n for n in nodes}

        def edge(source, target, kind, **props):
            return RelationshipData(
                kind,
                byname[source].labels,
                {"qualified_name": source},
                byname[target].labels,
                {"qualified_name": target},
                props,
            )

        rels = [
            edge("repo", "file", "CONTAINS"),
            edge("file", "base", "DEFINES"),
            edge("file", "child", "DEFINES"),
            edge("file", "method", "DEFINES"),
            edge("file", "helper", "DEFINES"),
            edge("child", "base", "INHERITS_FROM"),
            edge("method", "helper", "CALLS", line=6),
            edge("method", "helper", "UNIFIED_EDGE", dimension="CALL", line=6),
            edge("file", "imported", "IMPORTS"),
        ]
        meta = ToolExecutionMetadata(
            "fixture",
            "1",
            0,
            custom_metrics={
                "snapshot_id": snapshot,
                "analysis_status": "succeeded",
                "capabilities": ["definitions", "calls"],
            },
        )
        output = StandardizedOutput(nodes, rels, meta)
        service = GraphQueryService(driver, database)
        try:
            await setup_schema(driver, database=database)
            written = await WriteCoordinator(
                driver, database=database
            ).write_tool_output("fixture", output, repo)
            self.assertTrue(written.success, written.errors)
            all_elements = await service.search_elements(
                "", repo, snapshot_id=snapshot, limit=100
            )
            self.assertEqual(
                [x["element"]["name"] for x in all_elements["items"]],
                ["Base", "Child", "Child.run", "helper"],
            )
            bounded = await service.search_elements(
                "", repo, snapshot_id=snapshot, limit=1
            )
            self.assertTrue(bounded["truncated"])
            self.assertEqual(bounded["returned"], 1)
            await driver.verify_connectivity()
            # Fulltext index is eventually populated; db.awaitIndexes is an administrative test setup call.
            async with driver.session(database=database) as session:
                result = await session.run("CALL db.awaitIndexes(30)")
                await result.consume()
            found = await service.search_elements("helper", repo, snapshot_id=snapshot)
            self.assertEqual(found["items"][0]["element"]["name"], "helper")
            context = await service.get_code_context(
                "src/a.py", repo, snapshot_id=snapshot, line_number=6
            )
            self.assertEqual(context["items"][0]["element"]["name"], "Child.run")
            self.assertTrue(
                any(
                    row["relationship"]["type"] == "CALLS"
                    for row in context["relationships"]
                )
            )
            self.assertTrue(
                any(
                    row["relationship"]["type"] == "IMPORTS"
                    for row in context["relationships"]
                )
            )
            structure = await service.get_file_structure(
                "src", repo, snapshot_id=snapshot
            )
            self.assertEqual(structure["items"][0]["declarations"], 4)
            calls = await service.get_function_calls(
                "method", repo, snapshot_id=snapshot
            )
            self.assertEqual(calls["items"][0]["element"]["qualified_name"], "helper")
            callers = await service.get_function_callers(
                "helper", repo, snapshot_id=snapshot
            )
            self.assertEqual(callers["items"][0]["element"]["qualified_name"], "method")
            imports = await service.get_file_imports(
                "src/a.py", repo, snapshot_id=snapshot
            )
            self.assertEqual(
                imports["items"][0]["element"]["properties"]["nested"], {"a": [1, 2]}
            )
            self.assertIsNone(imports["items"][0]["element"]["properties"]["empty"])
            ancestors = await service.get_class_hierarchy(
                "child", repo, snapshot_id=snapshot
            )
            self.assertEqual(ancestors["items"][0]["direction"], "ancestor")
            self.assertEqual(
                ancestors["items"][0]["element"]["properties"]["name"], "Base"
            )
            path = await service.trace_execution_paths(
                "method", "helper", repo, snapshot_id=snapshot
            )
            self.assertEqual(path["items"][0]["depth"], 1)
            stats = await service.get_repository_stats(repo, snapshot_id=snapshot)
            self.assertEqual(stats["items"][0]["nodes"], 7)
            exists = await service.node_exists(
                "method", repo, snapshot_id=snapshot, node_type="Method"
            )
            self.assertTrue(exists["items"][0]["exists"])
            with self.assertRaises(ValueError):
                await service.search_elements("", repo, snapshot_id="missing")
            # Raw Cypher tests require an exclusively owned single-snapshot database.
            raw_enabled = (
                os.environ.get("DEEPTHOUGHT_NEO4J_SINGLE_SNAPSHOT_TEST") == "1"
            )
            with self.assertRaises(ValueError):
                await service.query_cypher(
                    "RETURN 1 AS n", {}, repo, snapshot_id=snapshot
                )
            for query in [
                "CREATE (n:Unsafe)",
                "MATCH (n) DELETE n",
                "CALL db.labels()",
                "RETURN 1; RETURN 2",
                "MATCH (n) SET n.bad=true RETURN n",
            ]:
                with self.subTest(query=query), self.assertRaises(ValueError):
                    await service.query_cypher(
                        query, {}, repo, snapshot_id=snapshot, allow_database_scope=True
                    )
            if raw_enabled:
                raw = await service.query_cypher(
                    "MATCH (n:Symbol) RETURN count(n) AS n",
                    {},
                    repo,
                    snapshot_id=snapshot,
                    allow_database_scope=True,
                )
                self.assertEqual(raw["items"], [{"n": 4}])
                raw = await service.query_cypher(
                    "RETURN $value AS value",
                    {"value": {"nested": [1, 2]}},
                    repo,
                    snapshot_id=snapshot,
                    allow_database_scope=True,
                )
                self.assertEqual(raw["items"][0]["value"], {"nested": [1, 2]})
                raw = await service.query_cypher(
                    "MATCH p=(f:File)-[r:DEFINES]->(n:Symbol) RETURN f,r,n,p ORDER BY n.qualified_name",
                    {},
                    repo,
                    snapshot_id=snapshot,
                    allow_database_scope=True,
                    limit=1,
                )
                self.assertTrue(raw["truncated"])
                self.assertEqual(raw["items"][0]["r"]["type"], "DEFINES")
                self.assertEqual(len(raw["items"][0]["p"]["nodes"]), 2)
                raw = await service.query_cypher(
                    "RETURN 'x' + 'y' AS value",
                    {},
                    repo,
                    snapshot_id=snapshot,
                    allow_database_scope=True,
                    max_output_bytes=1024,
                )
                self.assertEqual(raw["items"][0]["value"], "xy")
                raw = await service.query_cypher(
                    "RETURN $value AS value",
                    {"value": "x" * 2000},
                    repo,
                    snapshot_id=snapshot,
                    allow_database_scope=True,
                    max_output_bytes=1024,
                )
                self.assertTrue(raw["truncated"])
                self.assertEqual(raw["returned"], 0)
                self.assertLessEqual(
                    len(json.dumps(raw, sort_keys=True).encode()), 1024
                )
        finally:
            async with driver.session(database=database) as session:
                result = await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=repo
                )
                await result.consume()
            await driver.close()
