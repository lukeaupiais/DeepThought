"""The tests that reach an edit's changed declarations (checkEdit's tests)."""

import asyncio
import importlib.util
import os
import unittest
from uuid import uuid4

from deepthought.analysis_tools.contracts import (
    NodeData,
    RelationshipData,
    StandardizedOutput,
    ToolExecutionMetadata,
)
from deepthought.edit_check import parse_diff
from deepthought.graph_queries import GraphQueryService, _test_commands

from evidence_fixtures import FIXTURE_EVIDENCE


class ChangedLinesTests(unittest.TestCase):
    def test_removed_lines_are_ranges_and_insertions_sit_between_two_lines(self):
        diff = (
            "--- a/lib.py\n+++ b/lib.py\n"
            "@@ -1,6 +1,6 @@\n"
            " def a():\n"
            "-    return 1\n"
            "-    # old\n"
            "+    return 2\n"
            " \n"
            " def b():\n"
            "+    x = 1\n"
            "     return 3\n"
            "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x = 1\n"
        )
        old, new = parse_diff(diff)
        self.assertEqual(
            old.changed_lines(),
            [
                {"start": 2, "end": 3, "inside": False},
                {"start": 5, "end": 6, "inside": True},
            ],
        )
        self.assertIsNone(new.old_path)

    def test_commands_select_tests_by_their_runner(self):
        rows = [
            {"file": "a_test.go", "test": "TestA"},
            {"file": "pkg/b_test.go", "test": "TestB"},
            {"file": "pkg/b_test.go", "test": "TestC"},
            {"file": "pkg/b_test.go", "test": "helper"},
            {"file": "tests/test_x.py", "test": "TestK.test_y"},
            {"file": "src/a.test.ts", "test": 'it("adds")'},
        ]
        self.assertEqual(
            _test_commands(rows),
            [
                "go test ./ -run '^(TestA)$'",
                "go test ./pkg -run '^(TestB|TestC)$'",
                "python -m pytest tests/test_x.py::TestK::test_y",
                "test files: src/a.test.ts",
            ],
        )
        many = [{"file": "x_test.go", "test": f"Test{i}"} for i in range(31)]
        self.assertEqual(_test_commands(many), ["go test ./"])


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_NEO4J_URI") and importlib.util.find_spec("neo4j"),
    "explicit isolated Neo4j test instance required",
)
class TestsReachingIntegrationTests(unittest.TestCase):
    def test_tests_reach_the_changed_declarations_over_calls_and_values(self):
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
        repo = "https://github.com/public/tests-reaching-" + uuid4().hex
        snapshot = "snapshot-" + uuid4().hex

        def node(qname, labels, **props):
            return NodeData(
                labels, {"qualified_name": qname, "repo_url": repo, **props}
            )

        def symbol(qname, name, path, start, end):
            return node(
                qname,
                ["Function", "Symbol"],
                name=name,
                kind="Function",
                file_path=path,
                start_line=start,
                end_line=end,
            )

        nodes = [
            node("repo", ["Repository"], url=repo),
            node("lib", ["File"], path="src/lib.py", language="python"),
            node("tests", ["File"], path="tests/test_lib.py", language="python"),
            symbol("helper", "helper", "src/lib.py", 1, 3),
            symbol("caller", "caller", "src/lib.py", 5, 8),
            symbol("registry", "registry", "src/lib.py", 10, 12),
            symbol("other", "other", "src/lib.py", 14, 16),
            symbol("test_caller", "test_caller", "tests/test_lib.py", 1, 3),
            symbol("test_registry", "test_registry", "tests/test_lib.py", 5, 7),
            symbol("test_other", "test_other", "tests/test_lib.py", 9, 11),
        ]
        byname = {n.properties["qualified_name"]: n for n in nodes}

        def edge(source, target, kind, **props):
            return RelationshipData(
                kind,
                byname[source].labels,
                {"qualified_name": source},
                byname[target].labels,
                {"qualified_name": target},
                {**FIXTURE_EVIDENCE, **props},
            )

        rels = [
            edge("repo", "lib", "CONTAINS"),
            edge("repo", "tests", "CONTAINS"),
            *(
                edge("lib", s, "DEFINES")
                for s in ("helper", "caller", "registry", "other")
            ),
            *(
                edge("tests", s, "DEFINES")
                for s in ("test_caller", "test_registry", "test_other")
            ),
            edge("caller", "helper", "CALLS", line=6),
            edge("test_caller", "caller", "CALLS", line=2),
            # helper is passed as a value; the test runs what holds it.
            edge("registry", "helper", "REFERENCES_FUNCTION", line=11),
            edge("test_registry", "registry", "CALLS", line=6),
            edge("test_other", "other", "CALLS", line=10),
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
        service = GraphQueryService(driver, database)
        try:
            await setup_schema(driver, database=database)
            written = await WriteCoordinator(
                driver, database=database
            ).write_tool_output("fixture", StandardizedOutput(nodes, rels, meta), repo)
            self.assertTrue(written.success, written.errors)
            # A changed line in helper reaches both tests that run it.
            found = await service.tests_reaching(
                {"src/lib.py": [{"start": 2, "end": 2, "inside": False}]}
            )
            self.assertEqual(
                found["changed"], [{"name": "helper", "file": "src/lib.py", "line": 1}]
            )
            self.assertEqual(
                [(t["test"], t["reaches"], t["links_away"]) for t in found["tests"]],
                [("test_caller", "helper", 2), ("test_registry", "helper", 2)],
            )
            self.assertEqual(
                found["run"],
                [
                    "python -m pytest tests/test_lib.py::test_caller"
                    " tests/test_lib.py::test_registry"
                ],
            )
            # An insertion between two declarations changes neither; one
            # inside a declaration changes it; a changed test is its own test.
            found = await service.tests_reaching(
                {
                    "src/lib.py": [
                        {"start": 8, "end": 9, "inside": True},
                        {"start": 14, "end": 15, "inside": True},
                    ],
                    "tests/test_lib.py": [{"start": 6, "end": 6, "inside": False}],
                }
            )
            self.assertEqual(
                [c["name"] for c in found["changed"]], ["other", "test_registry"]
            )
            self.assertEqual(
                [(t["test"], t["links_away"]) for t in found["tests"]],
                [("test_registry", 0), ("test_other", 1)],
            )
            self.assertEqual(
                (await service.tests_reaching({}))["status"], "no_changed_lines"
            )
        finally:
            async with driver.session(database=database) as session:
                result = await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=repo
                )
                await result.consume()
            await driver.close()


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_NEO4J_URI") and importlib.util.find_spec("neo4j"),
    "explicit isolated Neo4j test instance required",
)
class SiblingCallsIntegrationTests(unittest.TestCase):
    def test_unchanged_calls_of_a_changed_call_are_listed(self):
        asyncio.run(self.exercise())

    async def exercise(self):
        from neo4j import AsyncGraphDatabase
        from deepthought.graph_queries import sibling_sentences
        from deepthought.storage.graph import setup_schema, WriteCoordinator

        driver = AsyncGraphDatabase.driver(
            os.environ["DEEPTHOUGHT_NEO4J_URI"],
            auth=(
                os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
                os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
            ),
        )
        database = os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j")
        repo = "https://github.com/public/sibling-calls-" + uuid4().hex
        snapshot = "snapshot-" + uuid4().hex
        map_source = (
            "func mapDecoder() {\n"
            "\tScan(SplitEscaped(v, sep)...)\n"
            "\tother()\n"
            "\tScan(v)\n"
            "}"
        )
        slice_source = "func sliceDecoder() {\n\tScan(SplitEscaped(v, sep)...)\n}"

        def node(qname, labels, **props):
            return NodeData(
                labels, {"qualified_name": qname, "repo_url": repo, **props}
            )

        def symbol(qname, path, start, end, source=None):
            extra = {"source_code": source} if source else {}
            return node(
                qname,
                ["Function", "Symbol"],
                name=qname,
                kind="Function",
                file_path=path,
                start_line=start,
                end_line=end,
                **extra,
            )

        nodes = [
            node("repo", ["Repository"], url=repo),
            node("mapper", ["File"], path="mapper.go", language="go"),
            node("scanner", ["File"], path="scanner.go", language="go"),
            symbol("mapDecoder", "mapper.go", 1, 5, map_source),
            symbol("sliceDecoder", "mapper.go", 7, 9, slice_source),
            symbol("other", "mapper.go", 11, 12),
            symbol("Scan", "scanner.go", 1, 3),
            symbol("parse", "scanner.go", 5, 7, "func parse() {\n\tScan(x)\n}"),
        ]
        byname = {n.properties["qualified_name"]: n for n in nodes}

        def edge(source, target, kind, **props):
            return RelationshipData(
                kind,
                byname[source].labels,
                {"qualified_name": source},
                byname[target].labels,
                {"qualified_name": target},
                {**FIXTURE_EVIDENCE, **props},
            )

        rels = [
            edge("repo", "mapper", "CONTAINS"),
            edge("repo", "scanner", "CONTAINS"),
            *(
                edge("mapper", s, "DEFINES")
                for s in ("mapDecoder", "sliceDecoder", "other")
            ),
            edge("scanner", "Scan", "DEFINES"),
            edge("scanner", "parse", "DEFINES"),
            edge("mapDecoder", "Scan", "CALLS", line=2),
            edge("mapDecoder", "other", "CALLS", line=3),
            edge("mapDecoder", "Scan", "CALLS", line=4),
            edge("sliceDecoder", "Scan", "CALLS", line=8),
            edge("parse", "Scan", "CALLS", line=6),
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
        service = GraphQueryService(driver, database)
        try:
            await setup_schema(driver, database=database)
            written = await WriteCoordinator(
                driver, database=database
            ).write_tool_output("fixture", StandardizedOutput(nodes, rels, meta), repo)
            self.assertTrue(written.success, written.errors)
            # The edit replaces sliceDecoder's Scan line only.
            found = await service.sibling_calls(
                {"mapper.go": [{"start": 8, "end": 8, "inside": False}]},
                {"mapper.go": {"8": "\tScan(SplitEscaped(v, sep)...)"}},
            )
            self.assertEqual(found["status"], "succeeded")
            [scan] = found["calls"]
            self.assertEqual(
                (scan["callee"], scan["changed"], scan["total"]), ("Scan", 1, 4)
            )
            self.assertEqual(
                [
                    (s["caller"], s["line"], s["same_file"], s["identical"])
                    for s in scan["unchanged"]
                ],
                [
                    ("mapDecoder", 2, True, True),
                    ("mapDecoder", 4, True, False),
                    ("parse", 6, False, False),
                ],
            )
            [sentence] = sibling_sentences(found)
            self.assertEqual(
                sentence,
                "Same call elsewhere: the edit changes 1 of 4 calls to Scan"
                " (scanner.go:1); not changed: mapDecoder (mapper.go:1) at lines 2"
                " (same text as a line the edit changes), 4; parse (scanner.go:5)"
                " at line 6.",
            )
            # A read of a declaration's source numbers its lines and says
            # what links into it and what it links to, unasked.
            text = await service.query_cypher_text(
                "MATCH (s:Symbol {name: $name}) RETURN s.source_code AS source",
                {"name": "mapDecoder"},
                repo,
                snapshot_id=snapshot,
                allow_database_scope=True,
            )
            self.assertIn("1| func mapDecoder() {\n", text)
            self.assertIn("4| \tScan(v)", text)
            self.assertIn(
                "Links of mapDecoder (mapper.go:1): 3 CALLS links from it"
                " (it calls these): Scan (scanner.go:1) at line 2; other"
                " (mapper.go:11) at line 3; Scan (scanner.go:1) at line 4.",
                text,
            )
            # A result that already holds links gets no such line.
            text = await service.query_cypher_text(
                "MATCH (s:Symbol {name: 'parse'})-[c:CALLS]->(t) RETURN c",
                {},
                repo,
                snapshot_id=snapshot,
                allow_database_scope=True,
            )
            self.assertNotIn("Links of", text)
            # An insertion changes no call.
            found = await service.sibling_calls(
                {"mapper.go": [{"start": 8, "end": 9, "inside": True}]}
            )
            self.assertEqual(found["status"], "no_changed_lines")
        finally:
            async with driver.session(database=database) as session:
                result = await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=repo
                )
                await result.consume()
            await driver.close()
