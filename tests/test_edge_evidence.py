"""Edge evidence contract: evidence type and rule ID on every stored relationship."""

import asyncio
from collections import Counter
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import unittest
from uuid import uuid4

from deepthought.analysis_tools import (
    NodeData,
    RelationshipData,
    StandardizedOutput,
    ToolExecutionMetadata,
    TreeSitterTool,
)
from deepthought.analysis_tools.evidence import (
    EVIDENCE_TYPES,
    EvidenceRule,
    get_rule,
    register_rule,
    registered_rules,
)
from deepthought.storage.graph.writer import _prepare

from evidence_fixtures import FIXTURE_EVIDENCE

PYTHON_FIXTURE = Path(__file__).parent / "fixtures" / "python"
TREE_SITTER_READY = importlib.util.find_spec(
    "tree_sitter"
) and importlib.util.find_spec("tree_sitter_python")

NAME_RULE = register_rule(
    EvidenceRule(
        "test.name-match", "name_matched", "Test edge linked by equal names only."
    )
)


def edge(properties):
    return RelationshipData(
        "CALLS",
        ["Symbol"],
        {"qualified_name": "a"},
        ["Symbol"],
        {"qualified_name": "b"},
        properties,
    )


def output_with(relationship):
    nodes = [
        NodeData(["Symbol"], {"qualified_name": q}, match_keys=["qualified_name"])
        for q in ("a", "b")
    ]
    return StandardizedOutput(
        nodes,
        [relationship],
        ToolExecutionMetadata(
            "fixture",
            "1",
            0,
            custom_metrics={"snapshot_id": "s1", "analysis_status": "succeeded"},
        ),
    )


async def tree_sitter_output(repo_url):
    return await TreeSitterTool(PYTHON_FIXTURE).analyze(repo_url, languages=["python"])


class EvidenceContractTests(unittest.TestCase):
    def test_every_relationship_must_name_a_registered_rule(self):
        self.assertEqual(
            EVIDENCE_TYPES, {"resolved", "pattern_matched", "name_matched"}
        )
        for properties, message in (
            ({}, "evidence_type and rule_id"),
            ({"evidence_type": "resolved"}, "evidence_type and rule_id"),
            ({"rule_id": FIXTURE_EVIDENCE["rule_id"]}, "evidence_type and rule_id"),
            ({"evidence_type": "resolved", "rule_id": "test.unknown"}, "Unknown"),
            (
                {"evidence_type": "resolved", "rule_id": ["test.fixture-edge"]},
                "Unknown",
            ),
        ):
            with self.subTest(properties=properties):
                with self.assertRaisesRegex(ValueError, message):
                    edge(properties)
        self.assertEqual(edge(dict(FIXTURE_EVIDENCE)).properties, FIXTURE_EVIDENCE)

    def test_rule_fixes_evidence_type_so_name_matches_cannot_claim_resolved(self):
        edge({"evidence_type": "name_matched", "rule_id": NAME_RULE.rule_id})
        with self.assertRaisesRegex(ValueError, "declares name_matched"):
            edge({"evidence_type": "resolved", "rule_id": NAME_RULE.rule_id})
        with self.assertRaisesRegex(ValueError, "declares resolved"):
            edge({**FIXTURE_EVIDENCE, "evidence_type": "pattern_matched"})

    def test_confidence_and_probability_scores_are_rejected(self):
        for key in ("confidence", "probability"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "not scores"):
                    edge({**FIXTURE_EVIDENCE, key: 1.0})

    def test_rule_definitions_are_validated_and_cannot_be_redefined(self):
        for rule_id in ("", "nodot", "Upper.case", "a..b", "a.b-", ".a", "a b.c"):
            with self.subTest(rule_id=rule_id):
                with self.assertRaisesRegex(ValueError, "rule_id"):
                    EvidenceRule(rule_id, "resolved", "x")
        with self.assertRaisesRegex(ValueError, "evidence_type"):
            EvidenceRule("test.bad-type", "likely", "x")
        with self.assertRaisesRegex(ValueError, "description"):
            EvidenceRule("test.no-description", "resolved", " ")
        self.assertIs(register_rule(NAME_RULE), NAME_RULE)
        self.assertEqual(
            register_rule(
                EvidenceRule(
                    NAME_RULE.rule_id, NAME_RULE.evidence_type, NAME_RULE.description
                )
            ),
            NAME_RULE,
        )
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            register_rule(EvidenceRule(NAME_RULE.rule_id, "resolved", "changed"))
        self.assertEqual(get_rule(NAME_RULE.rule_id), NAME_RULE)

    def test_storage_boundary_rechecks_records_mutated_after_construction(self):
        repo = "https://github.com/example/public"
        for change, message in (
            ({"evidence_type": "resolved", "rule_id": NAME_RULE.rule_id}, "declares"),
            ({"confidence": 0.8}, "not scores"),
        ):
            with self.subTest(change=change):
                relationship = edge(
                    {"evidence_type": "name_matched", "rule_id": NAME_RULE.rule_id}
                )
                value = output_with(relationship)
                relationship.properties.update(change)
                with self.assertRaisesRegex(ValueError, message):
                    _prepare(value, repo, "s1")
        value = output_with(edge(dict(FIXTURE_EVIDENCE)))
        del value.relationships[0].properties["rule_id"]
        with self.assertRaisesRegex(ValueError, "rule_id"):
            _prepare(value, repo, "s1")


@unittest.skipUnless(TREE_SITTER_READY, "Provision the python extra")
class TreeSitterEvidenceTests(unittest.TestCase):
    def test_every_tree_sitter_edge_carries_its_declared_rule(self):
        output = asyncio.run(tree_sitter_output("https://github.com/example/public"))
        self.assertTrue(output.relationships)
        rules = registered_rules()
        by_type = Counter()
        for relationship in output.relationships:
            rule = rules[relationship.properties["rule_id"]]
            self.assertEqual(
                relationship.properties["evidence_type"], rule.evidence_type
            )
            by_type[(relationship.type, rule.rule_id)] += 1
        files = sum("File" in node.labels for node in output.nodes)
        symbols = sum("Symbol" in node.labels for node in output.nodes)
        self.assertEqual(
            by_type,
            {
                ("CONTAINS", "tree-sitter.repository-contains-file"): files,
                ("DEFINES", "tree-sitter.file-defines-declaration"): symbols,
                # scip-python resolves the absolute imports as Python would.
                ("IMPORTS", "scip.python-import-module"): 3,
                ("CALLS", "scip.call-occurrence"): 3,
                ("SIGNATURE_USES", "scip.signature-type-reference"): 2,
                # `missing_function()` is bound by nothing.
                ("HAS_UNRESOLVED_CALL", "tree-sitter.call-site-unlinked"): 1,
            },
        )

    def test_same_source_gives_identical_edges_and_fingerprint(self):
        repo = "https://github.com/example/public"
        first = asyncio.run(tree_sitter_output(repo))
        second = asyncio.run(tree_sitter_output(repo))
        self.assertEqual(
            first.to_dict()["relationships"], second.to_dict()["relationships"]
        )
        snapshot = first.metadata.custom_metrics["snapshot_id"]
        self.assertEqual(
            _prepare(first, repo, snapshot)[1], _prepare(second, repo, snapshot)[1]
        )


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_NEO4J_URI")
    and importlib.util.find_spec("neo4j")
    and TREE_SITTER_READY,
    "Explicit isolated Neo4j test settings, neo4j and python extras required",
)
class EvidenceNeo4jIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from deepthought.storage.graph import (
            Neo4jConnectionManager,
            WriteCoordinator,
            setup_schema,
        )

        self.repo = "https://github.com/deepthought-integration/" + uuid4().hex
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
                "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=self.repo
            )
        finally:
            await self.manager.close()

    async def rows(self, query, **params):
        async with self.driver.session(database=self.database) as session:
            result = await session.run(query, params)
            return await result.data()

    async def test_stored_edges_keep_evidence_and_reimport_is_idempotent(self):
        output = await tree_sitter_output(self.repo)
        expected = Counter(
            (r.type, r.properties["evidence_type"], r.properties["rule_id"])
            for r in output.relationships
        )
        first = await self.coordinator.write_tool_output(
            "tree-sitter", output, self.repo
        )
        self.assertTrue(first.success, first.errors)
        stored = await self.rows(
            "MATCH (:GraphNode {repo_url:$repo})-[r]->(:GraphNode {repo_url:$repo}) "
            "RETURN type(r) AS type, r.evidence_type AS evidence, r.rule_id AS rule",
            repo=self.repo,
        )
        self.assertEqual(
            Counter((row["type"], row["evidence"], row["rule"]) for row in stored),
            expected,
        )
        second = await self.coordinator.write_tool_output(
            "tree-sitter", deepcopy(await tree_sitter_output(self.repo)), self.repo
        )
        self.assertTrue(second.success, second.errors)
        self.assertTrue(second.stats.reused)
        again = await self.rows(
            "MATCH (:GraphNode {repo_url:$repo})-[r]->() RETURN count(r) AS n",
            repo=self.repo,
        )
        self.assertEqual(again[0]["n"], sum(expected.values()))


if __name__ == "__main__":
    unittest.main()
