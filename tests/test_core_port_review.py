"""Independent counterexamples for the migrated graph writer and projection."""

import asyncio
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from deepthought import analyze_repository
from deepthought.analysis_tools import (
    NodeData,
    RelationshipData,
    StandardizedOutput,
    ToolExecutionMetadata,
    TreeSitterTool,
)
from deepthought.storage.graph.write_coordinator import (
    WriteCoordinator,
    WriteCoordinatorConfig,
    _combine,
)
from deepthought.storage.graph.writer import _prepare


def contributions(repo):
    nodes = [
        NodeData(
            ["Symbol", "Function"],
            {
                "qualified_name": name,
                "repo_url": repo,
                "name": name,
                "kind": "Function",
                "file_path": "sample.py",
                "start_line": i,
                "end_line": i,
            },
        )
        for i, name in enumerate(["f", "g"], 1)
    ]

    def output(tool, source_match):
        return StandardizedOutput(
            deepcopy(nodes),
            [
                RelationshipData(
                    "CALLS",
                    ["Symbol"],
                    source_match,
                    ["Symbol"],
                    {"qualified_name": "g"},
                    {"line": 1},
                )
            ],
            ToolExecutionMetadata(
                tool,
                "1",
                0,
                custom_metrics={
                    "snapshot_id": tool + "-source",
                    "analysis_status": "succeeded",
                    "capabilities": ["definitions", "calls"],
                },
            ),
        )

    return output("primary", {"qualified_name": "f"}), output(
        "supplement", {"qualified_name": "f", "kind": "Function"}
    )


class CorePortIndependentReview(unittest.TestCase):
    def test_equivalent_cross_tool_edge_constraints_are_one_shared_fact(self):
        repo = "https://github.com/public/port-review"
        merged, revision = _combine(
            contributions(repo), repo, "combined", WriteCoordinatorConfig()
        )
        data, _ = _prepare(merged, repo, revision)
        self.assertEqual(len(data["nodes"]), 2)
        self.assertEqual(len(data["relationships"]), 1)

    def test_conflicting_cross_tool_endpoint_constraints_are_rejected(self):
        repo = "https://github.com/public/port-review"
        primary, supplement = contributions(repo)
        supplement.relationships[0].source_match["kind"] = "Class"
        with self.assertRaises(ValueError):
            merged, revision = _combine(
                [primary, supplement], repo, "combined", WriteCoordinatorConfig()
            )
            _prepare(merged, repo, revision)

    @unittest.skipUnless(
        importlib.util.find_spec("tree_sitter_python"),
        "optional Python grammar required",
    )
    def test_projection_preserves_exact_sdk_definitions_and_directional_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "sample.py").write_text(
                "class Thing:\n    def method(self):\n        def nested():\n            return 1\n        return nested()\n\ndef top():\n    return Thing()\n"
            )
            sdk = analyze_repository(
                path,
                repository_id="https://github.com/public/port-review",
                languages=("python",),
            )
            tool = TreeSitterTool(path)
            output = asyncio.run(
                tool.analyze("https://github.com/public/port-review", {"python"})
            )
            symbols = [node for node in output.nodes if "Symbol" in node.labels]
            self.assertEqual(
                {node.properties["definition_id"] for node in symbols},
                {definition.id for definition in sdk.definitions},
            )
            by_id = {definition.id: definition for definition in sdk.definitions}
            for node in symbols:
                definition = by_id[node.properties["definition_id"]]
                self.assertEqual(
                    (
                        node.properties["name"],
                        node.properties["kind"],
                        node.properties["file_path"],
                        node.properties["start_line"],
                        node.properties["end_line"],
                    ),
                    (
                        definition.symbol,
                        definition.kind,
                        definition.file,
                        definition.line,
                        definition.end_line,
                    ),
                )
            files = {
                node.properties["qualified_name"]: node
                for node in output.nodes
                if "File" in node.labels
            }
            repositories = {
                node.properties["qualified_name"]
                for node in output.nodes
                if "Repository" in node.labels
            }
            owns = {
                edge.target_match["qualified_name"]: edge.source_match["qualified_name"]
                for edge in output.relationships
                if edge.type == "DEFINES"
            }
            self.assertEqual(
                set(owns), {node.properties["qualified_name"] for node in symbols}
            )
            for node in symbols:
                file = files[owns[node.properties["qualified_name"]]]
                self.assertEqual(file.properties["path"], node.properties["file_path"])
            contains = [
                edge for edge in output.relationships if edge.type == "CONTAINS"
            ]
            self.assertEqual(
                {edge.source_match["qualified_name"] for edge in contains}, repositories
            )
            self.assertEqual(
                {edge.target_match["qualified_name"] for edge in contains}, set(files)
            )
            self.assertEqual(
                {edge.type for edge in output.relationships}, {"CONTAINS", "DEFINES"}
            )
            self.assertEqual(
                output.metadata.custom_metrics["coverage"], sdk.to_dict()["coverage"]
            )
            self.assertEqual(
                output.metadata.custom_metrics["snapshot_id"], sdk.snapshot_id
            )


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_NEO4J_URI") and importlib.util.find_spec("neo4j"),
    "explicit isolated Neo4j instance required",
)
class CorePortDatabaseReview(unittest.TestCase):
    def test_equivalent_edges_roundtrip_and_same_names_stay_in_their_repository(self):
        asyncio.run(self.exercise())

    async def exercise(self):
        from neo4j import AsyncGraphDatabase
        from deepthought.storage.graph import setup_schema
        from deepthought.storage.graph.encoding import decode_properties
        from deepthought.graph_queries import GraphQueryService

        driver = AsyncGraphDatabase.driver(
            os.environ["DEEPTHOUGHT_NEO4J_URI"],
            auth=(
                os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
                os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
            ),
        )
        database = os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j")
        first = "https://github.com/public/review-" + uuid4().hex
        second = first + "-other"
        coordinator = WriteCoordinator(driver, database=database)
        service = GraphQueryService(driver, database)
        try:
            await setup_schema(driver, database=database)
            primary, supplement = contributions(first)
            primary.nodes[0].properties["payload"] = {
                "nested": [1, True, None, {"x": "y"}]
            }
            primary.relationships[0].properties.update(
                payload={"nested": [None, 1]}, nullable=None
            )
            supplement.relationships[0].properties = deepcopy(
                primary.relationships[0].properties
            )
            written = await coordinator.write_outputs(
                [primary, supplement], first, snapshot_id="combined"
            )
            self.assertTrue(written.success, written.errors)
            foreign = contributions(second)[0]
            foreign.nodes[0].properties["name"] = "foreign"
            written = await coordinator.write_tool_output("primary", foreign, second)
            self.assertTrue(written.success, written.errors)
            local = await service.search_elements("", first, snapshot_id="combined")
            self.assertEqual(
                [row["element"]["name"] for row in local["items"]], ["f", "g"]
            )
            foreign_result = await service.search_elements(
                "", second, snapshot_id="primary-source"
            )
            self.assertEqual(
                [row["element"]["name"] for row in foreign_result["items"]],
                ["foreign", "g"],
            )
            async with driver.session(database=database) as session:
                result = await session.run(
                    'MATCH (n:GraphNode {repo_url:$repo,snapshot_id:$snapshot,qualified_name:"f"})-[r:CALLS]->() RETURN properties(n) AS node,properties(r) AS edge',
                    repo=first,
                    snapshot="combined",
                )
                rows = [record async for record in result]
                self.assertEqual(len(rows), 1)
                self.assertEqual(
                    decode_properties(rows[0]["node"])["payload"],
                    {"nested": [1, True, None, {"x": "y"}]},
                )
                self.assertEqual(
                    decode_properties(rows[0]["edge"])["payload"], {"nested": [None, 1]}
                )
                self.assertIsNone(decode_properties(rows[0]["edge"])["nullable"])
            partial = deepcopy(primary)
            partial.metadata.custom_metrics["analysis_status"] = "partial"
            failed = await coordinator.write_outputs(
                [partial, supplement], first, snapshot_id="rejected"
            )
            self.assertFalse(failed.success)
            async with driver.session(database=database) as session:
                result = await session.run(
                    'MATCH (n {repo_url:$repo,snapshot_id:"rejected"}) RETURN count(n) AS count',
                    repo=first,
                )
                self.assertEqual((await result.single())["count"], 0)
        finally:
            async with driver.session(database=database) as session:
                result = await session.run(
                    "MATCH (n) WHERE n.repo_url IN $repos DETACH DELETE n",
                    repos=[first, second],
                )
                await result.consume()
            await driver.close()
