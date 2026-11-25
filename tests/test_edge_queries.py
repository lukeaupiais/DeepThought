"""Edge queries: evidence filters, importers and explain-edge over stored edges."""

import asyncio
from contextlib import redirect_stderr
import io
import importlib.util
import os
from pathlib import Path
import re
import unittest
from uuid import uuid4

from deepthought.analysis_tools.evidence import (
    EVIDENCE_MEANINGS,
    EVIDENCE_TYPES,
    registered_rules,
)
from deepthought.calls import CALL_IMPORTED_NAME
from deepthought.graph_cli import parser
from deepthought.graph_queries import GraphQueryService, _evidence_filter
from deepthought.imports import PYTHON_ABSOLUTE_MODULE
from deepthought.storage.graph.writer import _prepare

PYTHON_FIXTURE = Path(__file__).parent / "fixtures" / "python"
TREE_SITTER_READY = importlib.util.find_spec(
    "tree_sitter"
) and importlib.util.find_spec("tree_sitter_python")
EDGE_ID = re.compile(r"[0-9a-f]{64}")


def stored_row(rule_id, evidence_type):
    return {
        "source": {"qualified_name": "r::a.py::file", "name": "a.py", "path": "a.py"},
        "relationship": {
            "element_id": "5:x:1",
            "edge_id": "a" * 64,
            "type": "CALLS",
            "source": "s",
            "target": "t",
            "properties": {
                "evidence_type": evidence_type,
                "rule_id": rule_id,
                "line": 3,
                "column": 4,
            },
        },
        "target": {"qualified_name": "r::b.py::f::1", "name": "f"},
    }


class FakeReadService(GraphQueryService):
    """Exercise explain_edge's projection without a database read."""

    def __init__(self, rows):
        super().__init__(None)
        self.rows = rows

    async def _read(self, query, parameters, repo_url, snapshot_id, **bounds):
        return {
            "repo_url": repo_url,
            "snapshot_id": snapshot_id,
            "status": "succeeded",
            "items": list(self.rows),
            "returned": len(self.rows),
            "truncated": False,
        }


class EdgeQueryContractTests(unittest.TestCase):
    def test_evidence_filter_accepts_only_known_evidence_types(self):
        self.assertIsNone(_evidence_filter(None))
        self.assertEqual(
            _evidence_filter(("resolved", "name_matched", "resolved")),
            ["name_matched", "resolved"],
        )
        for value in ([], "resolved", ["likely"], [1], {"resolved": 1}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "evidence_types"):
                    _evidence_filter(value)

    def test_every_evidence_type_has_a_meaning(self):
        self.assertEqual(set(EVIDENCE_MEANINGS), EVIDENCE_TYPES)

    def test_explain_edge_rejects_malformed_edge_ids_before_reading(self):
        service = FakeReadService([])
        for edge_id in ("", "A" * 64, "a" * 63, "g" * 64, None):
            with self.subTest(edge_id=edge_id):
                with self.assertRaisesRegex(ValueError, "edge_id"):
                    asyncio.run(service.explain_edge(edge_id, "r", snapshot_id="s"))

    def test_explain_edge_reports_rule_status_against_this_version(self):
        rule = registered_rules()[CALL_IMPORTED_NAME]
        cases = (
            (CALL_IMPORTED_NAME, "name_matched", "registered", rule.description),
            ("other-tool.unknown-rule", "resolved", "unregistered", None),
            (CALL_IMPORTED_NAME, "resolved", "conflicts_with_registry", None),
        )
        for rule_id, evidence_type, status, description in cases:
            with self.subTest(status=status):
                service = FakeReadService([stored_row(rule_id, evidence_type)])
                item = asyncio.run(
                    service.explain_edge("a" * 64, "r", snapshot_id="s")
                )["items"][0]
                self.assertEqual(item["evidence"]["rule_status"], status)
                self.assertEqual(
                    item["evidence"]["meaning"], EVIDENCE_MEANINGS[evidence_type]
                )
                self.assertEqual(
                    item["location"], {"file": "a.py", "line": 3, "column": 4}
                )
                if description:
                    self.assertEqual(item["evidence"]["rule_description"], description)
                    self.assertIn(description, item["statement"])
                else:
                    # An unknown or conflicting rule is reported, never described.
                    self.assertNotIn(":", item["statement"].split("rule ")[-1])

    def test_explain_edge_reports_missing_edges(self):
        response = asyncio.run(
            FakeReadService([]).explain_edge("b" * 64, "r", snapshot_id="s")
        )
        self.assertEqual(response["status"], "not_found")
        self.assertEqual(response["items"], [])

    def test_cli_exposes_edge_commands(self):
        scope = ["--repository-id", "r", "--snapshot", "s"]
        args = parser().parse_args(
            ["importers", *scope, "--file", "a.py", "--evidence", "resolved"]
        )
        self.assertEqual((args.file, args.evidence), ("a.py", ["resolved"]))
        args = parser().parse_args(["explain-edge", *scope, "--edge-id", "a" * 64])
        self.assertFalse(hasattr(args, "limit"))
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parser().parse_args(
                ["callers", *scope, "--symbol", "x", "--evidence", "likely"]
            )


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_NEO4J_URI")
    and importlib.util.find_spec("neo4j")
    and TREE_SITTER_READY,
    "Explicit isolated Neo4j test settings, neo4j and python extras required",
)
class EdgeQueryNeo4jTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from deepthought.analysis_tools import TreeSitterTool
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
        # The parser's own rules, which bind calls in files no index covers.
        self.output = await TreeSitterTool(PYTHON_FIXTURE).analyze(
            self.repo, languages=["python"], scip_indexes=()
        )
        written = await WriteCoordinator(
            self.driver, database=self.database
        ).write_tool_output("tree-sitter", self.output, self.repo)
        self.assertTrue(written.success, written.errors)
        self.snapshot = written.snapshot_id
        self.service = GraphQueryService(self.driver, self.database)

    async def asyncTearDown(self):
        try:
            async with self.driver.session(database=self.database) as session:
                result = await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=self.repo
                )
                await result.consume()
        finally:
            await self.manager.close()

    def qualified(self, file, name):
        return next(
            n.properties["qualified_name"]
            for n in self.output.nodes
            if n.properties.get("file_path") == file
            and n.properties.get("name") == name
        )

    async def test_imports_and_importers_carry_evidence_and_stable_edge_ids(self):
        imports = await self.service.get_file_imports(
            "main.py", self.repo, snapshot_id=self.snapshot
        )
        self.assertEqual(
            sorted(row["element"]["properties"]["path"] for row in imports["items"]),
            ["lib/helpers.py", "models.py", "other/helpers.py"],
        )
        expected_ids = {
            rel["edge_id"]
            for rel in _prepare(self.output, self.repo, self.snapshot)[0][
                "relationships"
            ]
        }
        for row in imports["items"]:
            relationship = row["relationship"]
            self.assertRegex(relationship["edge_id"], EDGE_ID)
            # The stored identity is the content hash the writer computed.
            self.assertIn(relationship["edge_id"], expected_ids)
            self.assertEqual(
                relationship["properties"]["rule_id"], PYTHON_ABSOLUTE_MODULE
            )
        importers = await self.service.get_file_importers(
            "lib/helpers.py", self.repo, snapshot_id=self.snapshot
        )
        self.assertEqual(
            [row["element"]["properties"]["path"] for row in importers["items"]],
            ["main.py"],
        )
        self.assertIn("incoming IMPORTS", importers["semantics"])

    async def test_evidence_filter_keeps_only_selected_evidence(self):
        resolved_only = await self.service.get_file_imports(
            "main.py",
            self.repo,
            snapshot_id=self.snapshot,
            evidence_types=["resolved"],
        )
        # Absolute Python module paths are name matches, so nothing is resolved.
        self.assertEqual(resolved_only["items"], [])
        self.assertEqual(resolved_only["status"], "unsupported_or_unobserved")
        self.assertEqual(resolved_only["evidence_types"], ["resolved"])
        named = await self.service.get_file_imports(
            "main.py",
            self.repo,
            snapshot_id=self.snapshot,
            evidence_types=["name_matched"],
        )
        self.assertEqual(named["returned"], 3)
        callers = await self.service.get_function_callers(
            self.qualified("lib/helpers.py", "helper"),
            self.repo,
            snapshot_id=self.snapshot,
            evidence_types=["resolved"],
        )
        self.assertEqual(callers["items"], [])

    async def test_explain_edge_describes_a_stored_call(self):
        callers = await self.service.get_function_callers(
            self.qualified("lib/helpers.py", "helper"),
            self.repo,
            snapshot_id=self.snapshot,
        )
        self.assertEqual(len(callers["items"]), 1)
        edge_id = callers["items"][0]["relationship"]["edge_id"]
        first = await self.service.explain_edge(
            edge_id, self.repo, snapshot_id=self.snapshot
        )
        second = await self.service.explain_edge(
            edge_id, self.repo, snapshot_id=self.snapshot
        )
        self.assertEqual(first, second)
        item = first["items"][0]
        rule = registered_rules()[CALL_IMPORTED_NAME]
        self.assertEqual(item["type"], "CALLS")
        self.assertEqual(item["source"]["name"], "run")
        self.assertEqual(item["target"]["file_path"], "lib/helpers.py")
        self.assertEqual(
            item["location"], {"file": "main.py", "line": 15, "column": 11}
        )
        self.assertEqual(
            item["evidence"],
            {
                "evidence_type": "name_matched",
                "meaning": EVIDENCE_MEANINGS["name_matched"],
                "rule_id": CALL_IMPORTED_NAME,
                "rule_description": rule.description,
                "rule_status": "registered",
            },
        )
        self.assertEqual(
            item["statement"],
            "CALLS from run to helper at main.py:15:11. Evidence: name_matched, "
            f"by rule {CALL_IMPORTED_NAME}: {rule.description}.",
        )
        missing = await self.service.explain_edge(
            "0" * 64, self.repo, snapshot_id=self.snapshot
        )
        self.assertEqual(missing["status"], "not_found")

    async def test_explain_edge_is_scoped_to_the_selected_snapshot(self):
        imports = await self.service.get_file_imports(
            "main.py", self.repo, snapshot_id=self.snapshot
        )
        edge_id = imports["items"][0]["relationship"]["edge_id"]
        with self.assertRaisesRegex(ValueError, "missing or incomplete"):
            await self.service.explain_edge(
                edge_id, self.repo + "-other", snapshot_id=self.snapshot
            )


if __name__ == "__main__":
    unittest.main()
