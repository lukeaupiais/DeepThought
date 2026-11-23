"""Deterministic impact queries over stored edges."""

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from deepthought.analysis_tools import ScipAnalysisTool, TreeSitterTool
from deepthought.analysis_tools.contracts import (
    NodeData,
    RelationshipData,
    StandardizedOutput,
    ToolExecutionMetadata,
)
from deepthought.graph_queries import GraphQueryService
from deepthought.storage.graph import (
    Neo4jConnectionManager,
    WriteCoordinator,
    setup_schema,
)

from evidence_fixtures import FIXTURE_EVIDENCE

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_python"
)
JAVA_READY = importlib.util.find_spec("tree_sitter_java")
SCIP_READY = importlib.util.find_spec("google") and importlib.util.find_spec(
    "google.protobuf"
)
SOURCES = {
    "base.py": "class Base:\n"
    "    def run(self):\n"
    "        return helper()\n"
    "\n"
    "def helper():\n"
    "    return 1\n",
    "app.py": "from base import Base, helper\n"
    "\n"
    "class Child(Base):\n"
    "    def go(self):\n"
    "        return self.go2()\n"
    "    def go2(self):\n"
    "        return 2\n"
    "\n"
    "def use():\n"
    "    return helper()\n"
    "\n"
    "def top():\n"
    "    return use()\n"
    "\n"
    "top()\n",
}
# Two services joined by one HTTP contract: StatsReader.read requests the
# route StatsController.latest handles.
SERVICES = {
    "stats/pom.xml": "<project/>",
    "stats/src/main/resources/application.properties": "spring.application.name=stats\n",
    "stats/src/main/java/s/StatsController.java": "package s;\n"
    "import org.springframework.web.bind.annotation.*;\n"
    "@RestController\n"
    '@RequestMapping("/stats")\n'
    "public class StatsController {\n"
    '  @GetMapping("/latest")\n'
    "  public String latest() { return compute(); }\n"
    '  String compute() { return ""; }\n'
    "}\n",
    "web/pom.xml": "<project/>",
    "web/src/main/java/w/StatsReader.java": "package w;\n"
    "import org.springframework.web.client.RestTemplate;\n"
    "public class StatsReader {\n"
    "  RestTemplate rest;\n"
    '  public String read() { return rest.getForObject("http://stats/stats/latest", String.class); }\n'
    "  public String twice() { return read() + read(); }\n"
    "}\n",
}


@unittest.skipUnless(
    READY and os.environ.get("DEEPTHOUGHT_NEO4J_URI"),
    "Provision isolated Neo4j and the python extra",
)
class ImpactTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = Neo4jConnectionManager(
            os.environ["DEEPTHOUGHT_NEO4J_URI"],
            os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
            os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
            database=os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j"),
        )
        await self.manager.connect()
        self.addAsyncCleanup(self.manager.close)
        await setup_schema(self.manager.driver, database=self.manager.database)
        self.repos = []
        self.addAsyncCleanup(self.remove)
        self.service = GraphQueryService(
            self.manager.driver, database=self.manager.database
        )

    async def remove(self):
        async with self.manager.driver.session(
            database=self.manager.database
        ) as session:
            for repo in self.repos:
                await (
                    await session.run(
                        "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=repo
                    )
                ).consume()

    async def write(self, tool, files, name):
        repo = "impact-" + uuid4().hex
        self.repos.append(repo)
        with tempfile.TemporaryDirectory() as directory:
            for path, content in files.items():
                (Path(directory) / path).parent.mkdir(parents=True, exist_ok=True)
                (Path(directory) / path).write_text(content)
            output = await tool(directory).analyze(repo)
        written = await WriteCoordinator(
            self.manager.driver, database=self.manager.database
        ).write_tool_output(name, output, repo)
        self.assertTrue(written.success, written.errors)
        names = {
            n.properties["name"]: n.properties["qualified_name"]
            for n in output.nodes
            if "Symbol" in n.labels
        }
        return repo, output.metadata.custom_metrics["snapshot_id"], names

    @staticmethod
    def rows(response):
        return [
            (
                item["element"]["name"] or item["element"]["path"],
                item["depth"],
                item["evidence_type"],
                [edge["type"] for edge in item["edges"]],
            )
            for item in response["items"]
        ]

    async def test_transitive_dependents_with_paths_and_evidence(self):
        repo, snapshot, names = await self.write(TreeSitterTool, SOURCES, "tree-sitter")
        impact = await self.service.get_impact(
            names["helper"], repo, snapshot_id=snapshot
        )
        self.assertEqual(
            self.rows(impact),
            [
                ("use", 1, "name_matched", ["CALLS"]),
                ("Base.run", 1, "name_matched", ["CALLS"]),
                ("top", 2, "name_matched", ["CALLS", "CALLS"]),
                ("app.py", 3, "name_matched", ["CALLS", "CALLS", "CALLS"]),
            ],
        )
        for item in impact["items"]:
            self.assertTrue(all(len(e["edge_id"]) == 64 for e in item["edges"]))
            self.assertTrue(all(e["rule_id"] for e in item["edges"]))
        shallow = await self.service.get_impact(
            names["helper"], repo, snapshot_id=snapshot, max_depth=1
        )
        self.assertEqual([row[0] for row in self.rows(shallow)], ["use", "Base.run"])
        # Name-matched edges are not traversed when only resolved evidence is allowed.
        strict = await self.service.get_impact(
            names["helper"], repo, snapshot_id=snapshot, evidence_types=["resolved"]
        )
        self.assertEqual(strict["items"], [])
        self.assertEqual(strict["status"], "unsupported_or_unobserved")
        self.assertIn("Absence does not prove", strict["semantics"])

    async def test_change_set_includes_members_and_subtypes_count(self):
        repo, snapshot, names = await self.write(TreeSitterTool, SOURCES, "tree-sitter")
        impact = await self.service.get_impact(
            names["Base"], repo, snapshot_id=snapshot
        )
        # Base.run belongs to the change set, so its callers do not appear as
        # dependents of it; Child depends on Base through INHERITS_FROM.
        self.assertEqual(
            self.rows(impact), [("Child", 1, "name_matched", ["INHERITS_FROM"])]
        )
        self.assertEqual(impact["items"][0]["changed"], names["Base"])
        files = await self.service.get_file_impact(
            "base.py", repo, snapshot_id=snapshot
        )
        self.assertEqual(self.rows(files), [("app.py", 1, "name_matched", ["IMPORTS"])])

    async def test_bounds_and_budget(self):
        repo, snapshot, names = await self.write(TreeSitterTool, SOURCES, "tree-sitter")
        for depth in (0, 6, "3"):
            with self.assertRaises(ValueError):
                await self.service.get_impact(
                    names["helper"], repo, snapshot_id=snapshot, max_depth=depth
                )
        small = await self.service.get_impact(
            names["helper"], repo, snapshot_id=snapshot, max_output_bytes=1024
        )
        self.assertTrue(small["truncated"])
        self.assertLess(small["returned"], 4)

    async def test_strongest_path_wins_over_shortest(self):
        # c.py imports pkg.a directly by absolute name (name_matched) and
        # through b.py by relative imports (resolved).
        repo, snapshot, _ = await self.write(
            TreeSitterTool,
            {
                "pkg/__init__.py": "",
                "pkg/a.py": "X = 1\n",
                "pkg/b.py": "from .a import X\n",
                "pkg/c.py": "from .b import X\nimport pkg.a\n",
            },
            "tree-sitter",
        )
        impact = await self.service.get_file_impact(
            "pkg/a.py", repo, snapshot_id=snapshot
        )
        self.assertEqual(
            [
                (i["element"]["path"], i["depth"], i["evidence_type"])
                for i in impact["items"]
            ],
            [("pkg/b.py", 1, "resolved"), ("pkg/c.py", 2, "resolved")],
        )
        direct = await self.service.get_file_impact(
            "pkg/a.py", repo, snapshot_id=snapshot, max_depth=1
        )
        self.assertEqual(
            [(i["element"]["path"], i["evidence_type"]) for i in direct["items"]],
            [("pkg/b.py", "resolved"), ("pkg/c.py", "name_matched")],
        )

    async def test_members_of_the_changed_declaration_are_changed_too(self):
        repo = "impact-" + uuid4().hex
        self.repos.append(repo)

        def node(name, kind):
            return NodeData(
                [kind, "Symbol"],
                {"qualified_name": name, "repo_url": repo, "name": name, "kind": kind},
            )

        def edge(kind, source, target):
            return RelationshipData(
                kind,
                ["Symbol"],
                {"qualified_name": source, "repo_url": repo},
                ["Symbol"],
                {"qualified_name": target, "repo_url": repo},
                dict(FIXTURE_EVIDENCE),
            )

        nodes = [
            node("R", "Class"),
            node("R.m", "Method"),
            node("R.m.inner", "Function"),
            node("X", "Function"),
            node("Y", "Function"),
        ]
        rels = [
            edge("CONTAINS", "R", "R.m"),
            edge("CONTAINS", "R.m", "R.m.inner"),
            edge("CALLS", "X", "R.m.inner"),
            edge("CALLS", "R.m", "Y"),
            # A dependency inside the change set is not a dependent.
            edge("CALLS", "R.m", "R.m.inner"),
        ]
        snapshot = "snapshot-" + uuid4().hex
        meta = ToolExecutionMetadata(
            "fixture",
            "1",
            0,
            custom_metrics={"snapshot_id": snapshot, "analysis_status": "succeeded"},
        )
        written = await WriteCoordinator(
            self.manager.driver, database=self.manager.database
        ).write_tool_output("fixture", StandardizedOutput(nodes, rels, meta), repo)
        self.assertTrue(written.success, written.errors)
        impact = await self.service.get_impact("R", repo, snapshot_id=snapshot)
        # X calls a nested member of R; R's own calls out (to Y) are not impact.
        self.assertEqual(
            [(i["element"]["name"], i["depth"], i["changed"]) for i in impact["items"]],
            [("X", 1, "R.m.inner")],
        )

    @unittest.skipUnless(SCIP_READY, "Provision the scip extra")
    async def test_scip_file_impact_is_resolved(self):
        fixture = Path(__file__).parent / "fixtures/scip_alias_reexport"
        repo = "impact-" + uuid4().hex
        self.repos.append(repo)
        output = await ScipAnalysisTool(fixture).analyze(repo)
        written = await WriteCoordinator(
            self.manager.driver, database=self.manager.database
        ).write_tool_output("scip", output, repo)
        self.assertTrue(written.success, written.errors)
        impact = await self.service.get_file_impact(
            "a.ts", repo, snapshot_id=output.metadata.custom_metrics["snapshot_id"]
        )
        self.assertEqual(
            [
                (i["element"]["path"], i["depth"], i["evidence_type"])
                for i in impact["items"]
            ],
            [("b.ts", 1, "resolved"), ("c.ts", 1, "resolved")],
        )

    @unittest.skipUnless(SCIP_READY, "Provision the scip extra")
    async def test_implementations_are_resolved_dependents(self):
        fixture = Path(__file__).parent / "fixtures/scip_implementations"
        repo = "impact-" + uuid4().hex
        self.repos.append(repo)
        output = await ScipAnalysisTool(fixture).analyze(repo)
        written = await WriteCoordinator(
            self.manager.driver, database=self.manager.database
        ).write_tool_output("scip", output, repo)
        self.assertTrue(written.success, written.errors)
        base = next(
            n.properties["qualified_name"]
            for n in output.nodes
            if n.properties.get("display_name") == "Base"
        )
        impact = await self.service.get_impact(
            base, repo, snapshot_id=output.metadata.custom_metrics["snapshot_id"]
        )
        self.assertEqual(
            sorted(
                (
                    i["element"]["display_name"],
                    i["evidence_type"],
                    i["edges"][0]["type"],
                )
                for i in impact["items"]
            ),
            [("Dog", "resolved", "IMPLEMENTS"), ("Shadow", "resolved", "IMPLEMENTS")],
        )

    @unittest.skipUnless(JAVA_READY, "Provision the java grammar")
    async def test_impact_crosses_http_contracts(self):
        from deepthought.analysis_tools.service_contracts import ServiceContractTool

        repo = "impact-" + uuid4().hex
        self.repos.append(repo)
        with tempfile.TemporaryDirectory() as directory:
            for path, content in SERVICES.items():
                (Path(directory) / path).parent.mkdir(parents=True, exist_ok=True)
                (Path(directory) / path).write_text(content)
            trees = await TreeSitterTool(directory).analyze(repo, languages={"java"})
            contracts = await ServiceContractTool(Path(directory)).analyze(repo)
        snapshot = trees.metadata.custom_metrics["snapshot_id"]
        written = await WriteCoordinator(
            self.manager.driver, database=self.manager.database
        ).write_outputs([trees, contracts], repo, snapshot_id=snapshot)
        self.assertTrue(written.success, written.errors)
        names = {
            n.properties["name"]: n.properties["qualified_name"]
            for n in trees.nodes
            if "Symbol" in n.labels
        }
        impact = await self.service.get_impact(
            names["StatsController.compute"], repo, snapshot_id=snapshot, max_depth=5
        )
        rows = self.rows(impact)
        self.assertEqual(
            [row for row in rows if not row[0].endswith(".java")],
            [
                ("StatsController.latest", 1, "name_matched", ["CALLS"]),
                ("GET /stats/latest", 2, "name_matched", ["HANDLES", "CALLS"]),
                (
                    "StatsReader.read",
                    3,
                    "name_matched",
                    ["REQUESTS", "HANDLES", "CALLS"],
                ),
                (
                    "StatsReader.twice",
                    4,
                    "name_matched",
                    ["CALLS", "REQUESTS", "HANDLES", "CALLS"],
                ),
            ],
        )
        self.assertEqual(
            impact["forward_relations"], ["HANDLES", "PUBLISHES", "WRITES"]
        )
        self.assertIn("forwards over HANDLES", impact["semantics"])
        # Edges are listed from the dependent towards the change.
        # From the handler, the contract hops are pattern matched.
        handler = await self.service.get_impact(
            names["StatsController.latest"], repo, snapshot_id=snapshot
        )
        self.assertEqual(
            self.rows(handler)[:2],
            [
                ("GET /stats/latest", 1, "pattern_matched", ["HANDLES"]),
                ("StatsReader.read", 2, "pattern_matched", ["REQUESTS", "HANDLES"]),
            ],
        )
        self.assertEqual(
            [e["rule_id"] for e in self.rows_edges(handler)[1]],
            [
                "service-contracts.http-service-path",
                "service-contracts.spring-route-mapping",
            ],
        )
        # A requester depends on the route; the route does not depend on it,
        # so neither the route nor its handler is affected by the requester.
        reader = await self.service.get_impact(
            names["StatsReader.read"], repo, snapshot_id=snapshot, max_depth=5
        )
        self.assertNotIn("GET /stats/latest", [row[0] for row in self.rows(reader)])
        self.assertNotIn(
            "StatsController.latest", [row[0] for row in self.rows(reader)]
        )

    @staticmethod
    def rows_edges(response):
        return [item["edges"] for item in response["items"]]


if __name__ == "__main__":
    unittest.main()
