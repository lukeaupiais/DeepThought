"""Actual Neo4j acceptance; opt in with isolated DEEPTHOUGHT_NEO4J_* settings.

Every test uses fresh repository identities and removes only its own records.
No application source, services, default production database or credentials are
assumed. These tests never wipe a database or connect without explicit opt-in.
"""

import asyncio
import importlib.util
import os
import unittest
from copy import deepcopy
from uuid import uuid4

from deepthought.analysis_tools.contracts import (
    NodeData,
    RelationshipData,
    StandardizedOutput,
    ToolExecutionMetadata,
)
from deepthought.storage.graph import (
    Neo4jConnectionManager,
    WriteCoordinator,
    setup_schema,
)
from deepthought.storage.graph.encoding import decode_properties


def output(repo, snapshot="source-v1"):
    return StandardizedOutput(
        nodes=[
            NodeData(
                ["File"],
                {"qualified_name": "sample.py", "repo_url": repo, "path": "sample.py"},
            ),
            NodeData(
                ["Function", "Symbol"],
                {
                    "qualified_name": "sample.run",
                    "repo_url": repo,
                    "name": "run",
                    "display_name": "run",
                    "start_line": 3,
                    "end_line": 5,
                    "nested": {"a": [1, None]},
                    "nullable": None,
                    "big": 2**80,
                },
            ),
        ],
        relationships=[
            RelationshipData(
                "DEFINES",
                ["File"],
                {"qualified_name": "sample.py"},
                ["Symbol"],
                {"qualified_name": "sample.run"},
                {"evidence": {"line": 3}},
            )
        ],
        metadata=ToolExecutionMetadata(
            "fixture",
            "1",
            0,
            custom_metrics={
                "snapshot_id": snapshot,
                "analysis_status": "succeeded",
                "capabilities": ["definitions"],
            },
        ),
    )


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_NEO4J_URI") and importlib.util.find_spec("neo4j"),
    "Explicit isolated Neo4j test settings and neo4j extra required",
)
class StorageNeo4jIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repos = ["https://github.com/deepthought-integration/" + uuid4().hex]
        self.manager = Neo4jConnectionManager(
            os.environ["DEEPTHOUGHT_NEO4J_URI"],
            os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
            os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
            database=os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j"),
        )
        self.driver = await self.manager.connect()
        self.database = self.manager.database
        await setup_schema(self.driver, database=self.database)
        self.coordinator = WriteCoordinator(self.driver, database=self.database)

    async def asyncTearDown(self):
        try:
            await self.rows(
                "MATCH (n) WHERE n.repo_url IN $repos DETACH DELETE n", repos=self.repos
            )
        finally:
            await self.manager.close()

    async def rows(self, query, **params):
        async with self.driver.session(database=self.database) as session:
            result = await session.run(query, params)
            return await result.data()

    async def write(self, value):
        return await self.coordinator.write_tool_output("fixture", value, self.repos[0])

    async def test_persistence_roundtrip_complete_marker_and_idempotence(self):
        value = output(self.repos[0])
        result = await self.write(value)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(result.stats.nodes_created, 2)
        self.assertEqual(result.stats.relationships_created, 1)
        rows = await self.rows(
            "MATCH (s:AnalysisSnapshot {repo_url:$repo}) RETURN properties(s) AS p",
            repo=self.repos[0],
        )
        self.assertEqual(rows[0]["p"]["status"], "complete")
        props = (
            await self.rows(
                "MATCH (n:Symbol {repo_url:$repo}) RETURN properties(n) AS p",
                repo=self.repos[0],
            )
        )[0]["p"]
        decoded = decode_properties(props)
        self.assertEqual(decoded["nested"], {"a": [1, None]})
        self.assertIsNone(decoded["nullable"])
        self.assertEqual(decoded["big"], 2**80)
        again = await self.write(deepcopy(value))
        self.assertTrue(again.success, again.errors)
        self.assertTrue(again.stats.reused)
        self.assertEqual(again.stats.nodes_created, 0)
        counts = await self.rows(
            "MATCH (f:File {repo_url:$repo})-[r:DEFINES]->(s) RETURN count(r) AS n",
            repo=self.repos[0],
        )
        self.assertEqual(counts[0]["n"], 1)

    async def test_changed_content_same_identity_rejected_without_overwrite(self):
        value = output(self.repos[0])
        self.assertTrue((await self.write(value)).success)
        value.nodes[1].properties["start_line"] = 99
        result = await self.write(value)
        self.assertFalse(result.success)
        self.assertIn("different analysis facts", result.errors[0])
        rows = await self.rows(
            "MATCH (n:Symbol {repo_url:$repo}) RETURN n.start_line AS line",
            repo=self.repos[0],
        )
        self.assertEqual(rows[0]["line"], 3)

    async def test_changed_deleted_facts_in_new_snapshot_do_not_mix(self):
        old = output(self.repos[0])
        self.assertTrue((await self.write(old)).success)
        new = output(self.repos[0], "source-v2")
        new.nodes = new.nodes[:1]
        new.relationships = []
        self.assertTrue((await self.write(new)).success)
        rows = await self.rows(
            "MATCH (n:GraphNode {repo_url:$repo}) RETURN n.snapshot_id AS snapshot,count(n) AS n ORDER BY snapshot",
            repo=self.repos[0],
        )
        self.assertEqual(
            rows, [{"snapshot": "source-v1", "n": 2}, {"snapshot": "source-v2", "n": 1}]
        )

    async def test_same_names_and_snapshot_from_two_repositories_remain_distinct(self):
        first = output(self.repos[0])
        self.assertTrue((await self.write(first)).success)
        other = "https://github.com/deepthought-integration/" + uuid4().hex
        self.repos.append(other)
        second = output(other)
        result = await self.coordinator.write_tool_output("fixture", second, other)
        self.assertTrue(result.success, result.errors)
        rows = await self.rows(
            "MATCH (n:Symbol) WHERE n.repo_url IN $repos RETURN count(n) AS n",
            repos=self.repos,
        )
        self.assertEqual(rows[0]["n"], 2)

    async def test_database_rollback_after_node_batches_never_publishes_partial_snapshot(
        self,
    ):
        async def fail_after_nodes(*args):
            raise RuntimeError("injected relationship write failure")

        self.coordinator.graph_writer._write_relationships = fail_after_nodes
        result = await self.write(output(self.repos[0]))
        self.assertFalse(result.success)
        self.assertEqual(result.stats.nodes_created, 0)
        rows = await self.rows(
            "MATCH (n) WHERE n.repo_url=$repo RETURN count(n) AS n", repo=self.repos[0]
        )
        self.assertEqual(rows[0]["n"], 0)

    async def test_two_tools_merge_shared_symbol_and_cross_output_relationship(self):
        import json

        repo = self.repos[0]
        primary = output(repo)
        primary.nodes[1].properties.update(
            tags=["base"], docstring="Long primary documentation", type_annotation="int"
        )
        supplemental = StandardizedOutput(
            nodes=[
                NodeData(
                    ["Method", "Symbol"],
                    {
                        "qualified_name": "sample.run",
                        "repo_url": repo,
                        "name": "run",
                        "kind": "Method",
                        "tags": ["base", "semantic"],
                        "docstring": "short",
                        "type_annotation": None,
                        "return_type": "bool",
                        "semantic": True,
                    },
                ),
                NodeData(
                    ["Function", "Symbol"],
                    {
                        "qualified_name": "sample.other",
                        "repo_url": repo,
                        "name": "other",
                    },
                ),
            ],
            relationships=[
                RelationshipData(
                    "CALLS",
                    ["Symbol"],
                    {"qualified_name": "sample.run"},
                    ["Symbol"],
                    {"qualified_name": "sample.other"},
                )
            ],
            metadata=ToolExecutionMetadata(
                "supplement",
                "2",
                0,
                custom_metrics={
                    "snapshot_id": "semantic-input-v1",
                    "analysis_status": "succeeded",
                    "capabilities": ["calls"],
                },
            ),
        )
        result = await self.coordinator.write_outputs(
            [primary, supplemental], repo, snapshot_id="combined-v1"
        )
        self.assertTrue(result.success, result.errors)
        self.assertEqual(result.stats.nodes_created, 3)
        self.assertEqual(result.stats.relationships_created, 2)
        self.assertEqual(
            [m["tool_name"] for m in result.contributions], ["fixture", "supplement"]
        )
        row = (
            await self.rows(
                "MATCH (n:Symbol {repo_url:$repo,snapshot_id:'combined-v1',qualified_name:'sample.run'}) RETURN properties(n) AS p,labels(n) AS labels",
                repo=repo,
            )
        )[0]
        props = decode_properties(row["p"])
        self.assertTrue({"Function", "Method", "Symbol"} <= set(row["labels"]))
        self.assertEqual(props["tags"], ["base", "semantic"])
        self.assertEqual(props["docstring"], "Long primary documentation")
        self.assertEqual(props["type_annotation"], "int")
        self.assertEqual(props["return_type"], "bool")
        self.assertTrue(props["semantic"])
        marker = (
            await self.rows(
                "MATCH (s:AnalysisSnapshot {repo_url:$repo,snapshot_id:'combined-v1'}) RETURN s.metadata_json AS metadata",
                repo=repo,
            )
        )[0]
        metadata = json.loads(marker["metadata"])
        self.assertEqual(
            metadata["custom_metrics"]["capabilities"], ["calls", "definitions"]
        )
        self.assertEqual(
            [
                m["custom_metrics"]["snapshot_id"]
                for m in metadata["custom_metrics"]["tool_outputs"]
            ],
            ["source-v1", "semantic-input-v1"],
        )
        again = await self.coordinator.write_outputs(
            [primary, supplemental], repo, snapshot_id="combined-v1"
        )
        self.assertTrue(again.success, again.errors)
        self.assertTrue(again.stats.reused)
        self.assertEqual(
            (
                await self.rows(
                    "MATCH (:GraphNode {repo_url:$repo})-[r]->() RETURN count(r) AS n",
                    repo=repo,
                )
            )[0]["n"],
            2,
        )

    async def test_bad_supplement_or_late_write_failure_publishes_no_graph(self):
        repo = self.repos[0]
        primary = output(repo)
        bad = deepcopy(primary)
        bad.metadata.tool_name = "supplement"
        bad.metadata.custom_metrics["analysis_status"] = "partial"
        rejected = await self.coordinator.write_outputs(
            [primary, bad], repo, snapshot_id="bad-batch"
        )
        self.assertFalse(rejected.success)
        self.assertEqual(
            (
                await self.rows(
                    "MATCH (n) WHERE n.repo_url=$repo RETURN count(n) AS n", repo=repo
                )
            )[0]["n"],
            0,
        )
        bad.metadata.custom_metrics["analysis_status"] = "succeeded"

        async def fail_after_combined_nodes(*args):
            raise RuntimeError("late combined relationship failure")

        self.coordinator.graph_writer._write_relationships = fail_after_combined_nodes
        failed = await self.coordinator.write_outputs(
            [primary, bad], repo, snapshot_id="failed-batch"
        )
        self.assertFalse(failed.success)
        self.assertEqual(
            (
                await self.rows(
                    "MATCH (n) WHERE n.repo_url=$repo RETURN count(n) AS n", repo=repo
                )
            )[0]["n"],
            0,
        )

    async def test_concurrent_identical_imports_publish_once(self):
        value = output(self.repos[0])
        one, two = await asyncio.gather(self.write(value), self.write(deepcopy(value)))
        self.assertTrue(one.success, one.errors)
        self.assertTrue(two.success, two.errors)
        self.assertEqual(sorted([one.stats.reused, two.stats.reused]), [False, True])
        rows = await self.rows(
            "MATCH (n:GraphNode {repo_url:$repo}) RETURN count(n) AS n",
            repo=self.repos[0],
        )
        self.assertEqual(rows[0]["n"], 2)


if __name__ == "__main__":
    unittest.main()
