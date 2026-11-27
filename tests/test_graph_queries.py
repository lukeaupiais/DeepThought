"""Real Neo4j acceptance of the ported graph-query service (explicit opt-in)."""

import asyncio
from hashlib import sha256
import importlib.util
import json
import os
import unittest
from pathlib import Path
import tempfile
from uuid import uuid4

from deepthought.analysis_tools.contracts import (
    NodeData,
    RelationshipData,
    StandardizedOutput,
    ToolExecutionMetadata,
)
from deepthought.graph_queries import (
    GraphQueryService,
    _bounds,
    _cypher_code,
    _named_paths,
)

from evidence_fixtures import FIXTURE_EVIDENCE


class QueryInputTests(unittest.TestCase):
    def test_a_read_the_database_timed_out_is_tried_once_more(self):
        class TimedOut(Exception):
            def __init__(self, code):
                super().__init__(code)
                self.code = code

        def service(errors):
            calls = []

            async def once(*args, **kwargs):
                calls.append(args)
                if errors:
                    raise errors.pop(0)
                return {"status": "succeeded"}

            graph = GraphQueryService(None)
            graph._read_once = once
            return graph, calls

        timed_out = "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration"
        graph, calls = service([TimedOut(timed_out)])
        self.assertEqual(
            asyncio.run(graph._read("q", {}, "r", "s"))["status"], "succeeded"
        )
        self.assertEqual(len(calls), 2)
        # Only once, and only for a timeout.
        graph, calls = service([TimedOut(timed_out), TimedOut(timed_out)])
        with self.assertRaises(TimedOut):
            asyncio.run(graph._read("q", {}, "r", "s"))
        self.assertEqual(len(calls), 2)
        graph, calls = service([TimedOut("Neo.ClientError.Statement.SyntaxError")])
        with self.assertRaises(TimedOut):
            asyncio.run(graph._read("q", {}, "r", "s"))
        self.assertEqual(len(calls), 1)

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

    def test_answers_name_the_files_they_point_at(self):
        self.assertEqual(
            _named_paths(
                [
                    {"file": "a.go", "line": 3},
                    {"element": {"file_path": "b/c.ts", "path": None}},
                    {"p": {"nodes": [{"path": "d.py"}, {"path": "/abs.py"}]}},
                    {"file": "../up.go", "name": "e.go"},
                    # A raw query's unaliased columns.
                    {"f.path": "f.go", "s.file_path": "g.go", "f.paths": "h.go"},
                ]
            ),
            {"a.go", "b/c.ts", "d.py", "f.go", "g.go"},
        )

    def test_freshness_names_files_edited_or_gone_since_the_snapshot(self):
        from hashlib import sha256
        import tempfile
        from pathlib import Path

        class Records:
            def __init__(self, rows):
                self.rows = rows

            def __aiter__(self):
                async def rows():
                    for row in self.rows:
                        yield row

                return rows()

        class Transaction:
            def __init__(self, recorded):
                self.recorded = recorded
                self.asked = None

            async def run(self, query, **parameters):
                self.asked = parameters["paths"]
                return Records(
                    [
                        {"path": p, "digest": d}
                        for p, d in self.recorded.items()
                        if p in parameters["paths"]
                    ]
                )

        def digest(text):
            return sha256(text.encode()).hexdigest()

        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "same.go").write_text("package a\n")
            (Path(root) / "edited.go").write_text("package a // edited\n")
            recorded = {
                "same.go": digest("package a\n"),
                "edited.go": digest("package a\n"),
                "gone.go": digest("package a\n"),
            }
            service = GraphQueryService(None, workspace=root)
            tx = Transaction(recorded)
            items = [{"file": p} for p in ("same.go", "edited.go", "gone.go", "x.md")]
            freshness = asyncio.run(service._freshness(tx, "r", "s", items))
            self.assertEqual(
                freshness,
                {
                    "status": "changed",
                    "files_checked": 3,
                    "changed_files": ["edited.go"],
                    "missing_files": ["gone.go"],
                },
            )
            self.assertEqual(tx.asked, ["edited.go", "gone.go", "same.go", "x.md"])
            current = asyncio.run(
                service._freshness(tx, "r", "s", [{"file": "same.go"}])
            )
            self.assertEqual(current, {"status": "current", "files_checked": 1})
            # No analyzed file named, or no workspace given: nothing to vouch for.
            self.assertEqual(
                asyncio.run(service._freshness(tx, "r", "s", [{"n": 1}])),
                {"status": "unverified"},
            )
            self.assertEqual(
                asyncio.run(GraphQueryService(None)._freshness(tx, "r", "s", items)),
                {"status": "unverified"},
            )

        # A workspace symlink whose target is outside it is not read.
        with (
            tempfile.TemporaryDirectory() as root,
            tempfile.TemporaryDirectory() as out,
        ):
            (Path(out) / "secret.go").write_text("package a\n")
            (Path(root) / "link.go").symlink_to(Path(out) / "secret.go")
            (Path(root) / "same.go").write_text("package a\n")
            service = GraphQueryService(None, workspace=root)
            tx = Transaction(
                {"link.go": digest("package a\n"), "same.go": digest("package a\n")}
            )
            self.assertEqual(
                asyncio.run(service._freshness(tx, "r", "s", [{"file": "link.go"}])),
                {"status": "unverified"},
            )
            self.assertEqual(
                asyncio.run(
                    service._freshness(
                        tx, "r", "s", [{"file": "link.go"}, {"file": "same.go"}]
                    )
                ),
                {"status": "current", "files_checked": 1},
            )

    def test_cypher_guard_reads_only_the_query_code(self):
        reads = []

        class Recording(GraphQueryService):
            async def _read(self, query, parameters, *args, **kwargs):
                reads.append(query)
                return {"status": "succeeded"}

        service = Recording(None)

        def run(query):
            return asyncio.run(
                service.query_cypher(
                    query, {}, "repo", snapshot_id="s", allow_database_scope=True
                )
            )

        for query in [
            'MATCH (s:Symbol) WHERE s.name = "use" RETURN s',
            "MATCH (s:Symbol {name: 'Start'}) RETURN s",
            'MATCH (f:File) WHERE f.path ENDS WITH "delete.go" RETURN f',
            'MATCH (s) WHERE s.name CONTAINS "Set" RETURN s',
            'RETURN "a;b" AS text',
            'RETURN "say \\"call\\" twice" AS text',
            "MATCH (n:`CALL`) RETURN n",
            "MATCH (n) // set the scene\nRETURN n",
            "MATCH (n) /* DELETE */ RETURN n",
            # The query's own names are not clauses.
            "MATCH (s:Symbol) OPTIONAL MATCH (c)-[r:CALLS]->(s) "
            "RETURN s.source_code AS declaration, r AS call",
            "MATCH (start:File)-[:DEFINES]->(set) RETURN start.path, set.name",
            "MATCH (s) RETURN s {.name, use: s.start_line} AS show",
            "MATCH (s) WHERE s.load = $create RETURN s",
        ]:
            with self.subTest(query=query):
                self.assertEqual(run(query), {"status": "succeeded"})
        self.assertEqual(len(reads), 13)
        for query in [
            "MATCH (n) DELETE n",
            "MATCH (n) SET n.bad = true RETURN n",
            "RETURN 1; RETURN 2",
            "CALL db.labels()",
            'RETURN "unterminated',
            "MATCH (n) /* open comment RETURN n",
            'MATCH (n) WHERE n.name = "x" DELETE n',
            "MATCH (n) WITH n AS m DETACH DELETE m",
            "MATCH (n) CALL { WITH n SET n.x = 1 } RETURN n",
            "LOAD CSV FROM 'file:///x' AS row RETURN row",
        ]:
            with self.subTest(query=query), self.assertRaises(ValueError):
                run(query)
        self.assertEqual(len(reads), 13)
        self.assertEqual(_cypher_code("RETURN 'x' AS y"), "RETURN     AS y")


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
                docstring="Helps run things.",
                file_path="src/a.py",
                start_line=12,
                end_line=14,
            ),
            node(
                "imported", ["Import"], name="typing", nested={"a": [1, 2]}, empty=None
            ),
            *(
                node(
                    f"coverage-{family}-{language}",
                    ["EdgeCoverage"],
                    family=family,
                    language=language,
                    status=status,
                    resolver=resolver,
                )
                for family, language, status, resolver in (
                    ("IMPORTS", "python", "not_linked", ["tree-sitter@1"]),
                    ("CALLS", "python", "linked", ["scip-python@0.6.8"]),
                    ("CALLS", "go", "no_sites", []),
                )
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
                {**FIXTURE_EVIDENCE, **props},
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
            # A bare name finds the member it names, first; a dotted query
            # and a Lucene expression only use the index.
            member = await service.search_elements("run", repo, snapshot_id=snapshot)
            # helper's docstring says "run": an index hit, after the member.
            self.assertEqual(
                [(x["element"]["name"], x["name_match"]) for x in member["items"]],
                [("Child.run", True), ("helper", False)],
            )
            self.assertTrue(found["items"][0]["name_match"])
            exact = await service.search_elements(
                "helper", repo, snapshot_id=snapshot, file_path="other.py"
            )
            self.assertEqual(exact["items"], [])
            partial = await service.search_elements("un", repo, snapshot_id=snapshot)
            self.assertFalse(any(x["name_match"] for x in partial["items"]))
            dotted = await service.search_elements(
                "Child.run", repo, snapshot_id=snapshot
            )
            self.assertFalse(any(x["name_match"] for x in dotted["items"]))
            self.assertEqual(
                (
                    await service.search_elements(
                        "zzz_absent", repo, snapshot_id=snapshot
                    )
                )["items"],
                [],
            )
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
            self.assertEqual(stats["items"][0]["nodes"], 10)
            exists = await service.node_exists(
                "method", repo, snapshot_id=snapshot, node_type="Method"
            )
            self.assertTrue(exists["items"][0]["exists"])
            with self.assertRaises(ValueError):
                await service.search_elements("", repo, snapshot_id="missing")
            described = await service.describe_snapshot(repo, snapshot_id=snapshot)
            self.assertEqual(
                (
                    described["repo_url"],
                    described["snapshot_id"],
                    described["capabilities"],
                ),
                (repo, snapshot, ["definitions", "calls"]),
            )
            self.assertEqual(described["returned"], 1)
            # Coverage records by family and language, without storage keys.
            self.assertEqual(
                described["items"][0]["coverage"],
                [
                    {
                        "family": "CALLS",
                        "language": "go",
                        "resolver": [],
                        "status": "no_sites",
                    },
                    {
                        "family": "CALLS",
                        "language": "python",
                        "resolver": ["scip-python@0.6.8"],
                        "status": "linked",
                    },
                    {
                        "family": "IMPORTS",
                        "language": "python",
                        "resolver": ["tree-sitter@1"],
                        "status": "not_linked",
                    },
                ],
            )
            with self.assertRaises(ValueError):
                await service.describe_snapshot(repo, snapshot_id="missing")
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
                self.assertEqual(
                    raw,
                    {
                        "status": "succeeded",
                        "items": [{"n": 4}],
                        "returned": 1,
                        "truncated": False,
                    },
                )
                # A partial snapshot says so in every answer.
                async with driver.session(database=database) as session:
                    marked = await session.run(
                        "MATCH (s:AnalysisSnapshot {repo_url:$repo,snapshot_id:$id})"
                        " SET s.metadata_json=$metadata",
                        repo=repo,
                        id=snapshot,
                        metadata=json.dumps(
                            {"custom_metrics": {"analysis_status": "partial"}}
                        ),
                    )
                    await marked.consume()
                raw = await service.query_cypher(
                    "RETURN 1 AS n",
                    {},
                    repo,
                    snapshot_id=snapshot,
                    allow_database_scope=True,
                )
                self.assertEqual(
                    (sorted(raw), raw["analysis_status"]),
                    (
                        ["analysis_status", "items", "returned", "status", "truncated"],
                        "partial",
                    ),
                )
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
                # The storage label every node carries is left out.
                self.assertEqual(raw["items"][0]["f"]["labels"], ["File"])
                self.assertEqual(raw["items"][0]["p"]["nodes"][0]["labels"], ["File"])
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
                # The text form reads links as sentences, names link ends the
                # query did not return, and keeps its own byte budget.
                text = await service.query_cypher_text(
                    "MATCH ()-[r:CALLS]->() RETURN r",
                    {},
                    repo,
                    snapshot_id=snapshot,
                    allow_database_scope=True,
                )
                self.assertEqual(
                    text,
                    "1 result: every one this query matched.\n"
                    "- Child.run (src/a.py:5) calls helper (src/a.py:12) at line 6",
                )
                # With a workspace, freshness also covers the files of link
                # ends the text names but the query did not return.
                async with driver.session(database=database) as session:
                    result = await session.run(
                        "MATCH (f:File {repo_url:$repo}) SET f.file_sha256=$digest",
                        repo=repo,
                        digest=sha256(b"def helper(): pass\n").hexdigest(),
                    )
                    await result.consume()
                with tempfile.TemporaryDirectory() as root:
                    (Path(root) / "src").mkdir()
                    (Path(root) / "src/a.py").write_text("def helper(): return 1\n")
                    text = await GraphQueryService(
                        driver, database, workspace=root
                    ).query_cypher_text(
                        "MATCH ()-[r:CALLS]->() RETURN r",
                        {},
                        repo,
                        snapshot_id=snapshot,
                        allow_database_scope=True,
                    )
                self.assertEqual(
                    text.splitlines()[1],
                    "Changed since the graph was built: edited src/a.py. Lines in"
                    " these files may have moved: read them in source.",
                )
                text = await service.query_cypher_text(
                    "MATCH p=(f:File)-[:DEFINES]->(n:Symbol) RETURN p ORDER BY n.start_line",
                    {},
                    repo,
                    snapshot_id=snapshot,
                    allow_database_scope=True,
                )
                self.assertEqual(
                    text.splitlines()[:3],
                    [
                        "4 results: every one this query matched.",
                        'Each chain below starts at src/a.py (file) ("it"):',
                        "- it defines Base (class, src/a.py:1)",
                    ],
                )
                text = await service.query_cypher_text(
                    "MATCH (n:Symbol) RETURN n.name AS name, n.start_line AS line",
                    {},
                    repo,
                    snapshot_id=snapshot,
                    allow_database_scope=True,
                    limit=2,
                    max_output_bytes=1024,
                )
                self.assertTrue(text.startswith("2 results, cut at the limit"))
                self.assertLessEqual(len(text.encode()), 1024)
                # An empty answer says why, from the paths and names the query
                # looks for: an unanalyzed kind of file, a declared name (so
                # the other conditions matched nothing), an undeclared one.
                empty = (
                    "MATCH (s:Symbol)-[:CALLS]->(t:Symbol) WHERE t.name IN $names"
                    " AND s.file_path = 'web/App.vue' RETURN s"
                )
                arguments = ({"names": ["Child", "missing_fn"]}, repo)
                raw = await service.query_cypher(
                    empty,
                    *arguments,
                    snapshot_id=snapshot,
                    allow_database_scope=True,
                )
                self.assertEqual(raw["returned"], 0)
                self.assertEqual(
                    raw["why_empty"],
                    [
                        "No analyzed file path contains 'web/App.vue': no .vue file"
                        " is analyzed in this snapshot (analyzed: .py); search the"
                        " source for it.",
                        "'Child' is declared at src/a.py:4, with 0 calls to it.",
                        "Nothing named 'missing_fn' is declared in the analyzed files.",
                    ],
                )
                text = await service.query_cypher_text(
                    empty,
                    *arguments,
                    snapshot_id=snapshot,
                    allow_database_scope=True,
                )
                self.assertEqual(
                    text.splitlines()[:2], ["No results.", raw["why_empty"][0]]
                )
                # When everything the query looks for is in the graph, the
                # other conditions are what matched nothing. A plain word
                # ('nope') may be a property value: named only when declared.
                raw = await service.query_cypher(
                    "MATCH (s:Symbol {name: 'helper', file_path: 'src/a.py'})"
                    "<-[:CALLS]-(c {name: 'nope'}) RETURN c",
                    {},
                    repo,
                    snapshot_id=snapshot,
                    allow_database_scope=True,
                )
                self.assertEqual(
                    raw["why_empty"],
                    [
                        "'src/a.py' is analyzed (src/a.py).",
                        "'helper' is declared at src/a.py:12, with 1 call to it.",
                        "Everything above is in the graph, so the query's other"
                        " conditions (links, properties, line numbers) matched"
                        " nothing.",
                    ],
                )
                # A parameter the query never uses is not looked for, and an
                # unknown label or relationship type is named.
                raw = await service.query_cypher(
                    "MATCH (s:Nope)-[:CALLS]->(t:Symbol) RETURN s",
                    {"file": "src/a.py"},
                    repo,
                    snapshot_id=snapshot,
                    allow_database_scope=True,
                )
                self.assertEqual(len(raw["why_empty"]), 1)
                self.assertTrue(
                    raw["why_empty"][0].startswith("The graph has no label 'Nope' ("),
                    raw["why_empty"],
                )
                raw = await service.query_cypher(
                    "MATCH (s:Symbol) WHERE s.language = 'cobol' RETURN s",
                    {"file": "src/a.py"},
                    repo,
                    snapshot_id=snapshot,
                    allow_database_scope=True,
                )
                self.assertNotIn("why_empty", raw)
                # A UNION whose parts return different columns names the fix.
                with self.assertRaisesRegex(ValueError, "same aliases"):
                    await service.query_cypher(
                        "RETURN 1 AS a UNION RETURN 2 AS b",
                        {},
                        repo,
                        snapshot_id=snapshot,
                        allow_database_scope=True,
                    )
                with self.assertRaises(ValueError):
                    await service.query_cypher_text(
                        "MATCH (n) DETACH DELETE n",
                        {},
                        repo,
                        snapshot_id=snapshot,
                        allow_database_scope=True,
                    )
        finally:
            async with driver.session(database=database) as session:
                result = await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=repo
                )
                await result.consume()
            await driver.close()
