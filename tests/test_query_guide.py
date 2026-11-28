"""The queryGraph guide: its worked queries pass the read-only guard, use only
the parameters the guide names, and answer on a real graph."""

import asyncio
import importlib.util
import os
import re
import unittest
from uuid import uuid4

from deepthought.analysis_tools.contracts import (
    NodeData,
    RelationshipData,
    StandardizedOutput,
    ToolExecutionMetadata,
)
from deepthought.graph_queries import (
    BUILTIN_PARAMETERS,
    _check_cypher,
    _named_anchor_pairs,
    _with_builtin_parameters,
)
from deepthought.query_guide import GUIDE, RECIPES

from evidence_fixtures import FIXTURE_EVIDENCE

# The parameters a caller passes to each worked query.
PARAMETERS = {
    "users_of_a_type": {"file": "a.go", "line": 1, "top": 30},
    "callers": {"file": "a.go", "line": 1, "top": 30},
    "between": {"from_file": "a.go", "from_line": 1, "to_file": "b.go", "to_line": 1},
    "shared_data": {
        "from_file": "a.go",
        "from_line": 1,
        "to_file": "b.go",
        "to_line": 1,
    },
    "implementations": {"file": "a.go", "line": 1},
    "tests": {"file": "a.go", "line": 1, "top": 30},
    "uses": {"file": "a.go", "line": 1, "top": 30},
}


def used_parameters(query):
    return set(re.findall(r"\$(\w+)", query))


class GuideTests(unittest.TestCase):
    def test_every_worked_query_is_in_the_guide_and_passes_the_guard(self):
        self.assertEqual(set(RECIPES), set(PARAMETERS))
        for name, query in RECIPES.items():
            with self.subTest(name=name):
                self.assertIn(query, GUIDE)
                _check_cypher(query, PARAMETERS[name], True)
                self.assertEqual(
                    used_parameters(query) - set(BUILTIN_PARAMETERS),
                    set(PARAMETERS[name]),
                )

    def test_the_guide_sends_reading_to_the_shell(self):
        self.assertIn("Do not fetch source text through the graph", GUIDE)
        self.assertNotIn("RETURN s.source_code", GUIDE)
        self.assertLess(len(GUIDE.encode()), 12_000)

    def test_builtin_parameters_are_added_only_when_used_and_not_passed(self):
        query = "MATCH (n:File) WHERE n.path =~ $test_path_pattern RETURN n"
        self.assertEqual(
            _with_builtin_parameters(query, {}),
            {"test_path_pattern": BUILTIN_PARAMETERS["test_path_pattern"]},
        )
        self.assertEqual(
            _with_builtin_parameters(query, {"test_path_pattern": "x"}),
            {"test_path_pattern": "x"},
        )
        self.assertEqual(_with_builtin_parameters("RETURN 1", {"a": 1}), {"a": 1})
        # A name in a string is not a parameter.
        self.assertEqual(
            _with_builtin_parameters("RETURN '$test_path_pattern'", {}), {}
        )
        self.assertRegex("pkg/a_test.go", BUILTIN_PARAMETERS["test_path_pattern"])
        self.assertNotRegex("pkg/a.go", BUILTIN_PARAMETERS["test_path_pattern"])

    def test_anchor_parameters_pair_by_name(self):
        query = RECIPES["between"]
        self.assertEqual(
            _named_anchor_pairs(query, PARAMETERS["between"]),
            [("a.go", 1), ("b.go", 1)],
        )
        self.assertEqual(
            _named_anchor_pairs(RECIPES["callers"], {"file": "a.go", "line": 7}),
            [("a.go", 7)],
        )
        # Unpaired names leave pairing to the path-by-line fallback.
        self.assertEqual(
            _named_anchor_pairs(
                "MATCH (s {file_path: $path}) WHERE s.start_line = $n RETURN s",
                {"path": "a.go", "n": 3},
            ),
            [],
        )


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_NEO4J_URI") and importlib.util.find_spec("neo4j"),
    "explicit isolated Neo4j test instance required",
)
class GuideQueriesIntegrationTests(unittest.TestCase):
    def test_worked_queries_answer_on_a_graph(self):
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
        tag = uuid4().hex
        repo = "https://github.com/public/query-guide-" + tag
        snapshot = "snapshot-" + tag
        # Raw Cypher is database-scoped: unique paths keep this fixture apart.
        lib, app, test = f"g{tag}/lib.go", f"g{tag}/app.go", f"g{tag}/app_test.go"

        def node(qname, labels, **props):
            return NodeData(
                labels, {"qualified_name": qname, "repo_url": repo, **props}
            )

        def symbol(qname, name, path, start, end, kind="Function"):
            return node(
                qname,
                [kind, "Symbol"],
                name=name,
                kind=kind,
                file_path=path,
                start_line=start,
                end_line=end,
            )

        nodes = [
            node("repo", ["Repository"], url=repo),
            node("lib", ["File"], path=lib, language="go"),
            node("app", ["File"], path=app, language="go"),
            node("test", ["File"], path=test, language="go"),
            symbol("Rec", "Rec", lib, 1, 5, "Struct"),
            symbol("Rec.a", "Rec.a", lib, 2, 2, "Field"),
            symbol("Rec.b", "Rec.b", lib, 3, 3, "Field"),
            symbol("Rec.c", "Rec.c", lib, 4, 4, "Field"),
            symbol("Rec.Equal", "Rec.Equal", lib, 7, 9, "Method"),
            symbol("Runner.Run", "Runner.Run", lib, 11, 11, "Method"),
            symbol("real.Run", "real.Run", lib, 13, 15, "Method"),
            symbol("produce", "produce", lib, 17, 19),
            symbol("carry", "carry", app, 1, 4),
            symbol("consume", "consume", app, 6, 9),
            symbol("entry", "entry", app, 11, 14),
            symbol("readA", "readA", app, 16, 18),
            symbol("mock.Run", "mock.Run", test, 1, 3, "Method"),
            symbol("TestEntry", "TestEntry", test, 5, 9),
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
            *(edge("repo", f, "CONTAINS") for f in ("lib", "app", "test")),
            *(
                edge("lib", s, "DEFINES")
                for s in ("Rec", "Rec.Equal", "Runner.Run", "real.Run", "produce")
            ),
            *(
                edge("app", s, "DEFINES")
                for s in ("carry", "consume", "entry", "readA")
            ),
            *(edge("test", s, "DEFINES") for s in ("mock.Run", "TestEntry")),
            # Equal handles the whole value; readA reads one field.
            *(
                edge("Rec.Equal", f, "READS_FIELD", line=8)
                for f in ("Rec.a", "Rec.b", "Rec.c")
            ),
            edge("readA", "Rec.a", "READS_FIELD", line=17),
            edge("produce", "Rec.b", "WRITES_FIELD", line=18),
            edge("consume", "Rec.b", "READS_FIELD", line=8),
            # entry -> carry -> produce; entry -> consume; the test runs entry.
            edge("entry", "carry", "CALLS", line=12),
            edge("entry", "consume", "CALLS", line=13),
            edge("carry", "produce", "CALLS", line=2),
            edge("carry", "Runner.Run", "CALLS", line=3),
            edge("Runner.Run", "real.Run", "DISPATCHES_TO"),
            edge("Runner.Run", "mock.Run", "DISPATCHES_TO"),
            edge("TestEntry", "entry", "CALLS", line=6),
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

        async def rows(name, **parameters):
            # Run as queryGraph would, without its single-snapshot database
            # check: the fixture's unique paths keep the answers to it.
            query = RECIPES[name]
            _check_cypher(query, parameters, True)
            async with driver.session(
                database=database, default_access_mode="READ"
            ) as session:
                result = await session.run(
                    query, _with_builtin_parameters(query, parameters)
                )
                return [record async for record in result]

        def name(value):
            return value["name"]

        try:
            await setup_schema(driver, database=database)
            written = await WriteCoordinator(
                driver, database=database
            ).write_tool_output("fixture", StandardizedOutput(nodes, rels, meta), repo)
            self.assertTrue(written.success, written.errors)

            # Anchored on one field: that field's users.
            found = await rows("users_of_a_type", file=lib, line=2, top=1)
            self.assertEqual(found[0]["users_in_total"], 2)
            found = await rows("users_of_a_type", file=lib, line=1, top=2)
            self.assertEqual(found[0]["users_in_total"], 4)
            self.assertEqual(
                [(name(r["user"]), r["fields_touched"]) for r in found],
                [("Rec.Equal", 3), ("consume", 1)],
            )
            self.assertEqual(found[0]["reads"], ["a", "b", "c"])

            found = await rows("callers", file=lib, line=18, top=10)
            self.assertEqual(
                [(r["calls_away"], name(r["caller"])) for r in found],
                [(1, "carry"), (2, "entry"), (3, "TestEntry")],
            )
            self.assertEqual(found[0]["callers_in_total"], 3)

            found = await rows(
                "between", from_file=lib, from_line=18, to_file=app, to_line=12
            )
            self.assertEqual(
                [(r["step"], name(r["on_the_way"])) for r in found], [(1, "carry")]
            )

            found = await rows(
                "shared_data", from_file=lib, from_line=18, to_file=app, to_line=12
            )
            self.assertEqual(
                [
                    (
                        name(r["field"]),
                        [name(w) for w in r["written_by"]],
                        [name(x) for x in r["read_by"]],
                    )
                    for r in found
                ],
                [("Rec.b", ["produce"], ["consume"])],
            )

            # From the interface method, or from one implementation: all of them.
            async with driver.session(database=database) as session:
                result = await session.run(
                    "MATCH (n {repo_url: $repo}) RETURN elementId(n) AS id, n.name AS name",
                    repo=repo,
                )
                names = {r["id"]: r["name"] async for r in result}
            for line in (11, 14):
                found = await rows("implementations", file=lib, line=line)
                self.assertEqual(
                    sorted(
                        names[r["implementation"].end_node.element_id] for r in found
                    ),
                    ["mock.Run", "real.Run"],
                )

            found = await rows("tests", file=lib, line=18, top=10)
            self.assertEqual(
                [(r["calls_away"], name(r["test"])) for r in found],
                [(3, "TestEntry")],
            )

            found = await rows("uses", file=app, line=2, top=10)
            self.assertEqual(
                [
                    (r["how"], r["in_total"], sorted(name(w) for w in r["what"]))
                    for r in found
                ],
                [("CALLS", 2, ["Runner.Run", "produce"])],
            )
        finally:
            async with driver.session(database=database) as session:
                result = await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=repo
                )
                await result.consume()
            await driver.close()


if __name__ == "__main__":
    unittest.main()
