"""Deterministic impact queries over stored edges."""

import importlib.util
import json
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
from go_stubs import needs_go

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_python"
)
JAVA_READY = importlib.util.find_spec("tree_sitter_java")
GO_READY = importlib.util.find_spec("tree_sitter_go")
TS_READY = importlib.util.find_spec("tree_sitter_typescript")
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
# A gRPC client whose wrappers either pass the method's messages through
# their signatures (lookup, named's callee) or hide them (named, report).
# The generated code is cut down to what go/types needs: messages have
# protoc-gen-go's ProtoReflect method.
GRPC_CLIENT = {
    "go.mod": "module example.com/guide\n\ngo 1.21\n",
    "pb/guide.proto": 'syntax = "proto3";\n'
    "package guide;\n"
    "service Guide {\n"
    "  rpc GetFeature(Point) returns (Feature) {}\n"
    "}\n"
    "message Point { int32 x = 1; }\n"
    "message Feature { string name = 1; }\n",
    "pb/guide.pb.go": "package pb\n\n"
    "type Point struct{ X int32 }\n\n"
    "func (*Point) ProtoReflect() int { return 0 }\n\n"
    "type Feature struct{ Name string }\n\n"
    "func (*Feature) ProtoReflect() int { return 0 }\n",
    "pb/guide_grpc.pb.go": "package pb\n\n"
    'import "context"\n\n'
    "type GuideClient interface {\n"
    "\tGetFeature(ctx context.Context, in *Point) (*Feature, error)\n"
    "}\n\n"
    "type guideClient struct{}\n\n"
    "func NewGuideClient(cc any) GuideClient { return guideClient{} }\n\n"
    "func (guideClient) GetFeature(ctx context.Context, in *Point) (*Feature, error) {\n"
    "\treturn &Feature{}, nil\n"
    "}\n",
    "client/main.go": "package main\n\n"
    "import (\n"
    '\t"context"\n\n'
    '\tpb "example.com/guide/pb"\n'
    ")\n\n"
    "func lookup(client pb.GuideClient, point *pb.Point) *pb.Feature {\n"
    "\tfeature, _ := client.GetFeature(context.Background(), point)\n"
    "\treturn feature\n"
    "}\n\n"
    "func named(client pb.GuideClient, x int32) string {\n"
    "\treturn lookup(client, &pb.Point{X: x}).Name\n"
    "}\n\n"
    "func report(client pb.GuideClient) string {\n"
    "\treturn named(client, 1)\n"
    "}\n\n"
    "func main() {\n"
    "\tclient := pb.NewGuideClient(nil)\n"
    "\t_ = report(client)\n"
    "}\n",
}
# The same client in TypeScript, against grpc-tools' generated declarations:
# SCIP resolves the stub call and the types each signature names.
TS_GRPC_CLIENT = {
    "package.json": '{"name": "guide", "version": "1.0.0", "private": true}\n',
    "tsconfig.json": '{"compilerOptions": {"strict": true}, "include": ["**/*.ts"]}\n',
    "pb/guide.proto": GRPC_CLIENT["pb/guide.proto"],
    "pb/guide_pb.d.ts": "export class Point {\n  getX(): number;\n}\n"
    "export class Feature {\n  getName(): string;\n}\n",
    "pb/guide_grpc_pb.d.ts": 'import { Point, Feature } from "./guide_pb";\n'
    "export class GuideClient {\n"
    "  constructor(address: string, credentials: unknown);\n"
    "  getFeature(request: Point, callback: "
    "(error: Error | null, response: Feature) => void): void;\n"
    "}\n",
    "client.ts": 'import * as services from "./pb/guide_grpc_pb";\n'
    'import { Point, Feature } from "./pb/guide_pb";\n'
    "\n"
    'const client = new services.GuideClient("localhost:50051", null);\n'
    "\n"
    "export function lookup(point: Point, done: (f: Feature) => void): void {\n"
    "  client.getFeature(point, (error, feature) => done(feature));\n"
    "}\n"
    "\n"
    "export function named(x: number): void {\n"
    "  lookup(new Point(), (f) => console.log(f.getName(), x));\n"
    "}\n"
    "\n"
    "export function report(): void {\n"
    "  named(1);\n"
    "}\n",
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

    async def write(self, tool, files, name, **options):
        repo = "impact-" + uuid4().hex
        self.repos.append(repo)
        with tempfile.TemporaryDirectory() as directory:
            for path, content in files.items():
                (Path(directory) / path).parent.mkdir(parents=True, exist_ok=True)
                (Path(directory) / path).write_text(content)
            output = await tool(directory).analyze(repo, **options)
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
                ("use", 1, "resolved", ["CALLS"]),
                ("Base.run", 1, "resolved", ["CALLS"]),
                ("top", 2, "resolved", ["CALLS", "CALLS"]),
                ("app.py", 3, "resolved", ["CALLS", "CALLS", "CALLS"]),
            ],
        )
        for item in impact["items"]:
            self.assertTrue(all(len(e["edge_id"]) == 64 for e in item["edges"]))
            self.assertTrue(all(e["rule_id"] for e in item["edges"]))
        # Each dependent carries its line span, signature and the lines of
        # its own calls toward the change; each step of its path names the
        # declarations it joins and the line of the call.
        top = next(i for i in impact["items"] if i["element"]["name"] == "top")
        self.assertEqual(
            (
                top["element"]["start_line"],
                top["element"]["end_line"],
                top["element"]["signature"],
                top["element"]["top_level"],
                top["lines"],
            ),
            (12, 13, "def top():", False, [13]),
        )
        self.assertEqual(
            [
                (e["from"]["name"], e["line"], e["to"]["name"], e["to"]["start_line"])
                for e in top["edges"]
            ],
            [("top", 13, "use", 9), ("use", 10, "helper", 5)],
        )
        # A dependent calling the change several times lists each line once,
        # in order.
        repeated, snapshot2, names2 = await self.write(
            TreeSitterTool,
            {
                "lib.py": "def helper():\n    return 1\n",
                "use.py": "from lib import helper\n\ndef many():\n"
                + "".join("    helper()\n" for _ in range(6))
                + "    helper()\n    helper()\n",
            },
            "tree-sitter",
        )
        many = await self.service.get_impact(
            names2["helper"], repeated, snapshot_id=snapshot2
        )
        self.assertEqual(
            [(i["element"]["name"], i["lines"]) for i in many["items"]],
            [("many", list(range(4, 12)))],
        )
        module = next(i for i in impact["items"] if i["depth"] == 3)
        self.assertTrue(module["element"]["top_level"])
        self.assertEqual(module["lines"], [15])
        self.assertEqual(
            [(e["from"]["name"], e["from"]["top_level"]) for e in module["edges"]][0],
            ("(top level of file)", True),
        )
        shallow = await self.service.get_impact(
            names["helper"], repo, snapshot_id=snapshot, max_depth=1
        )
        self.assertEqual([row[0] for row in self.rows(shallow)], ["use", "Base.run"])
        # The calls bind through scip-python, so allowing only name-matched
        # evidence leaves nothing to traverse.
        strict = await self.service.get_impact(
            names["helper"], repo, snapshot_id=snapshot, evidence_types=["name_matched"]
        )
        self.assertEqual(strict["items"], [])
        self.assertEqual(strict["status"], "unsupported_or_unobserved")
        self.assertIn("Absence does not prove", strict["semantics"])

    async def test_tests_depend_on_code_their_fixtures_call(self):
        repo, snapshot, names = await self.write(
            TreeSitterTool,
            {
                "app.py": "def make():\n    return 1\n",
                "tests/conftest.py": "import pytest\n\nfrom app import make\n\n"
                "@pytest.fixture\ndef thing():\n    return make()\n",
                "tests/test_app.py": "def test_thing(thing):\n    assert thing\n",
            },
            "tree-sitter",
        )
        impact = await self.service.get_impact(
            names["make"], repo, snapshot_id=snapshot
        )
        self.assertEqual(
            self.rows(impact),
            [
                ("thing", 1, "resolved", ["CALLS"]),
                ("test_thing", 2, "pattern_matched", ["USES_FIXTURE", "CALLS"]),
            ],
        )
        self.assertEqual(impact["items"][1]["source_roles"], ["test"])

    async def test_endpoints_depend_on_the_fastapi_dependencies_they_inject(self):
        from test_fastapi_dependencies import OCCURRENCES, SOURCES, scip_index

        # A file that does not parse would leave the analysis incomplete.
        sources = {p: t for p, t in SOURCES.items() if p != "app/broken.py"}
        found = {p: o for p, o in OCCURRENCES.items() if p in sources}
        with tempfile.TemporaryDirectory() as scratch:
            indexes = (scip_index(sources, found, scratch),)
            repo, snapshot, names = await self.write(
                TreeSitterTool,
                sources,
                "tree-sitter",
                languages=("python",),
                scip_indexes=indexes,
            )
        impact = await self.service.get_impact(
            names["Params"], repo, snapshot_id=snapshot
        )
        # The module-level ``ParamsDep = Annotated[Params, ...]`` alias names
        # the type itself. Each endpoint both injects Params and names it as
        # a parameter type: both links are resolved and one hop, so which one
        # the row shows is the first by edge identity.
        rows = self.rows(impact)
        self.assertEqual(
            [row[:3] for row in rows],
            [
                ("app/aliases.py", 1, "resolved"),
                ("b", 1, "resolved"),
                ("c", 1, "resolved"),
                ("d", 1, "resolved"),
                ("f", 1, "resolved"),
            ],
        )
        self.assertEqual(rows[0][3], ["USES_TYPE"])
        for row in rows[1:]:
            self.assertIn(row[3], (["USES_DEPENDENCY"], ["USES_TYPE"]))

    async def test_direct_and_transitive_dependents_with_source_roles(self):
        files = dict(SOURCES)
        files["tests/test_base.py"] = (
            "from base import helper\n\ndef test_helper():\n    assert helper()\n"
        )
        files["examples/demo.py"] = "from app import top\n\ndef demo():\n    top()\n"
        repo, snapshot, names = await self.write(TreeSitterTool, files, "tree-sitter")
        impact = await self.service.get_impact(
            names["helper"], repo, snapshot_id=snapshot
        )

        def roles(response):
            return [
                (
                    item["element"]["name"] or item["element"]["path"],
                    item["impact"],
                    item["source_roles"],
                )
                for item in response["items"]
            ]

        self.assertNotIn("exclude_roles", impact)
        self.assertIn("transitive: re-test", impact["semantics"])
        rows = roles(impact)
        self.assertIn(("test_helper", "direct", ["test"]), rows)
        self.assertIn(("use", "direct", []), rows)
        self.assertIn(("top", "transitive", []), rows)
        self.assertIn(("demo", "transitive", ["example"]), rows)
        # Direct dependents come first, so a short page holds the edits.
        kinds = [row[1] for row in rows]
        self.assertEqual(kinds, sorted(kinds))
        kept = await self.service.get_impact(
            names["helper"],
            repo,
            snapshot_id=snapshot,
            exclude_roles=["test", "example"],
        )
        self.assertEqual(kept["exclude_roles"], ["example", "test"])
        self.assertEqual([row for row in rows if not row[2]], roles(kept))
        no_tests = await self.service.get_file_impact(
            "base.py", repo, snapshot_id=snapshot, exclude_roles=["test"]
        )
        self.assertEqual(
            [(i["element"]["path"], i["impact"]) for i in no_tests["items"]],
            [("app.py", "direct"), ("examples/demo.py", "transitive")],
        )
        no_generated = await self.service.get_impact(
            names["helper"],
            repo,
            snapshot_id=snapshot,
            exclude_roles=["generated", "test"],
        )
        self.assertEqual(no_generated["exclude_roles"], ["generated", "test"])
        self.assertTrue(
            all("test" not in i["source_roles"] for i in no_generated["items"])
        )
        for bad in ([], ["docs"], "test", ["generated", "docs"]):
            with self.assertRaises(ValueError):
                await self.service.get_impact(
                    names["helper"], repo, snapshot_id=snapshot, exclude_roles=bad
                )

    async def test_generated_dependents_have_the_generated_role(self):
        sources = {
            "base.py": "def helper():\n    return 1\n",
            "app.py": "from base import helper\n\n\ndef use():\n    return helper()\n",
            "demo_pb2.py": "# Generated by the protocol buffer compiler.  DO NOT EDIT!\n"
            "from base import helper\n\n\ndef built():\n    return helper()\n",
        }
        repo, snapshot, names = await self.write(TreeSitterTool, sources, "tree-sitter")
        impact = await self.service.get_impact(
            names["helper"], repo, snapshot_id=snapshot
        )
        roles = {i["element"]["name"]: i["source_roles"] for i in impact["items"]}
        self.assertEqual(roles["built"], ["generated"])
        self.assertEqual(roles["use"], [])
        kept = await self.service.get_impact(
            names["helper"], repo, snapshot_id=snapshot, exclude_roles=["generated"]
        )
        self.assertEqual(
            {i["element"]["name"] for i in kept["items"]}, set(roles) - {"built"}
        )

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
        # scip-python resolves app.py's absolute import of base.
        self.assertEqual(self.rows(files), [("app.py", 1, "resolved", ["IMPORTS"])])

    async def test_answers_say_which_named_files_changed_since(self):
        repo, snapshot, names = await self.write(TreeSitterTool, SOURCES, "tree-sitter")
        with tempfile.TemporaryDirectory() as directory:
            for path, content in SOURCES.items():
                (Path(directory) / path).parent.mkdir(parents=True, exist_ok=True)
                (Path(directory) / path).write_text(content)
            service = GraphQueryService(
                self.manager.driver, database=self.manager.database, workspace=directory
            )
            impact = await service.get_impact(
                names["helper"], repo, snapshot_id=snapshot
            )
            self.assertEqual(impact["freshness"]["status"], "current")
            self.assertGreater(impact["freshness"]["files_checked"], 0)
            edited, removed = sorted(SOURCES)[:2]
            (Path(directory) / edited).write_text(SOURCES[edited] + "\n# edited\n")
            (Path(directory) / removed).unlink()
            impact = await service.get_impact(
                names["helper"], repo, snapshot_id=snapshot
            )
            named = {
                item["element"]["file_path"] or item["element"]["path"]
                for item in impact["items"]
            }
            freshness = impact["freshness"]
            self.assertEqual(freshness["status"], "changed")
            self.assertEqual(set(freshness.get("changed_files", [])), {edited} & named)
            self.assertEqual(set(freshness.get("missing_files", [])), {removed} & named)
        # Without a workspace nothing is checked, and answers say nothing.
        plain = await self.service.get_impact(
            names["helper"], repo, snapshot_id=snapshot
        )
        self.assertNotIn("freshness", plain)

    async def test_bounds_and_budget(self):
        repo, snapshot, names = await self.write(TreeSitterTool, SOURCES, "tree-sitter")
        for depth in (0, 6, "3"):
            with self.assertRaises(ValueError):
                await self.service.get_impact(
                    names["helper"], repo, snapshot_id=snapshot, max_depth=depth
                )
        small = await self.service.get_impact(
            names["helper"], repo, snapshot_id=snapshot, max_output_bytes=1164
        )
        self.assertTrue(small["truncated"])
        self.assertLess(small["returned"], 4)
        # No item fits, so no page would make progress.
        if not small["returned"]:
            self.assertIsNone(small["next_offset"])

    async def test_pages_continue_where_a_truncated_answer_stops(self):
        repo, snapshot, names = await self.write(TreeSitterTool, SOURCES, "tree-sitter")
        whole = await self.service.get_impact(
            names["helper"], repo, snapshot_id=snapshot
        )
        self.assertEqual((whole["offset"], whole["next_offset"]), (0, None))
        pages, offset = [], 0
        while offset is not None:
            page = await self.service.get_impact(
                names["helper"], repo, snapshot_id=snapshot, limit=1, offset=offset
            )
            self.assertEqual(page["offset"], offset)
            pages.extend(page["items"])
            offset = page["next_offset"]
            self.assertEqual(page["truncated"], offset is not None)
        self.assertEqual(pages, whole["items"])
        # A byte budget that cuts the list leaves the rest to the next page.
        small = await self.service.get_impact(
            names["helper"], repo, snapshot_id=snapshot, max_output_bytes=4096
        )
        self.assertTrue(small["truncated"])
        self.assertTrue(small["items"])
        self.assertEqual(small["next_offset"], small["returned"])
        rest = await self.service.get_impact(
            names["helper"],
            repo,
            snapshot_id=snapshot,
            offset=small["next_offset"],
        )
        self.assertEqual(small["items"] + rest["items"], whole["items"])
        for offset in (-1, 100_001, "1"):
            with self.assertRaises(ValueError):
                await self.service.get_impact(
                    names["helper"], repo, snapshot_id=snapshot, offset=offset
                )

    async def test_strongest_path_wins_over_shortest(self):
        # c.py imports pkg.a directly by absolute name (name_matched) and
        # through b.py by relative imports (resolved). Without an index, the
        # absolute import keeps its path rule, which only matches the name.
        class PathRulesOnly(TreeSitterTool):
            async def analyze(self, repo_url, **kwargs):
                return await super().analyze(repo_url, scip_indexes=(), **kwargs)

        repo, snapshot, _ = await self.write(
            PathRulesOnly,
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
        # c.py also imports a.py in one hop, so it is a direct dependent
        # although its strongest path is two hops long.
        self.assertEqual([i["impact"] for i in impact["items"]], ["direct", "direct"])
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
    @unittest.skipUnless(GO_READY, "Provision the go grammar")
    async def test_go_implementations_reach_interface_callers(self):
        files = {
            "store.go": "package app\n\n"
            "type Store interface{ Get(k string) string }\n\n"
            "type Mem struct{}\n\n"
            "func (Mem) Get(k string) string { return k }\n\n"
            "type Disk struct{}\n\n"
            "func (Disk) Get(k string) string { return k }\n\n"
            'func Load(s Store) string { return s.Get("k") }\n',
        }

        class GoTool(TreeSitterTool):
            async def analyze(self, repo_url, **options):
                return await super().analyze(repo_url, languages=("go",))

        repo, snapshot, names = await self.write(GoTool, files, "tree-sitter")
        impact = await self.service.get_impact(
            names["Mem.Get"], repo, snapshot_id=snapshot
        )
        # The interface method dispatches to Mem.Get, so the interface's
        # callers are reached; the other implementation is not.
        self.assertEqual(
            [(row[0], row[1], row[3]) for row in self.rows(impact)],
            [
                ("Store.Get", 1, ["DISPATCHES_TO"]),
                ("Load", 2, ["CALLS", "DISPATCHES_TO"]),
            ],
        )
        interface = await self.service.get_impact(
            names["Store"], repo, snapshot_id=snapshot
        )
        self.assertEqual(
            sorted((row[0], row[3]) for row in self.rows(interface)),
            [
                ("Disk", ["IMPLEMENTS"]),
                ("Load", ["CALLS"]),
                ("Mem", ["IMPLEMENTS"]),
            ],
        )

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

    @unittest.skipUnless(JAVA_READY, "Provision the java grammar")
    async def test_contract_impact_reaches_providers_clients_and_callers(self):
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
        found = await self.service.search_contracts(
            "stats/LATEST", repo, snapshot_id=snapshot
        )
        self.assertEqual(
            [
                (i["kind"], i["name"], i["providers"], i["clients"])
                for i in found["items"]
            ],
            [("HttpRoute", "GET /stats/latest", 1, 1)],
        )
        self.assertEqual(
            (
                await self.service.search_contracts(
                    "stats", repo, snapshot_id=snapshot, kind="DbTable"
                )
            )["items"],
            [],
        )
        with self.assertRaises(ValueError):
            await self.service.search_contracts(
                "", repo, snapshot_id=snapshot, kind="Route"
            )
        route = found["items"][0]["qualified_name"]
        impact = await self.service.get_contract_impact(
            route, repo, snapshot_id=snapshot, max_depth=3
        )
        self.assertEqual(
            [(p["name"], p["relations"]) for p in impact["providers"]],
            [("StatsController.latest", ["HANDLES"])],
        )
        self.assertFalse(impact["providers_truncated"])
        # Providers are named as declarations, with the lines of their
        # edges to the contract; clients carry the lines of their requests.
        self.assertEqual(
            [
                (
                    p["kind"],
                    p["file_path"],
                    p["start_line"],
                    p["end_line"],
                    p["signature"],
                    p["top_level"],
                    p["lines"],
                    p["source_roles"],
                )
                for p in impact["providers"]
            ],
            [
                (
                    "Method",
                    "stats/src/main/java/s/StatsController.java",
                    6,
                    7,
                    '@GetMapping("/latest")\n  public String latest()',
                    False,
                    [6],
                    [],
                )
            ],
        )
        self.assertEqual(
            [
                (i["element"]["name"], i["element"]["signature"], i["lines"])
                for i in impact["items"]
            ],
            [
                ("StatsReader.read", "public String read()", [5]),
                ("StatsReader.twice", "public String twice()", [6]),
            ],
        )
        self.assertEqual(
            [
                (row[0], row[1], row[3], item["impact"])
                for row, item in zip(self.rows(impact), impact["items"])
                if not row[0].endswith(".java")
            ],
            [
                ("StatsReader.read", 1, ["REQUESTS"], "direct"),
                ("StatsReader.twice", 2, ["CALLS", "REQUESTS"], "transitive"),
            ],
        )
        # The smallest budget still answers, within it.
        small = await self.service.get_contract_impact(
            route, repo, snapshot_id=snapshot, max_output_bytes=2048
        )
        self.assertLessEqual(
            len(json.dumps(small, sort_keys=True, ensure_ascii=False).encode()), 2048
        )
        with self.assertRaises(ValueError):
            await self.service.get_contract_impact(
                route, repo, snapshot_id=snapshot, max_output_bytes=2047
            )

    async def test_impact_action_follows_the_change(self):
        repo, snapshot, names = await self.write(TreeSitterTool, SOURCES, "tree-sitter")
        signature = await self.service.get_impact(
            names["helper"], repo, snapshot_id=snapshot, max_depth=5
        )
        self.assertEqual(
            sorted(
                (item["element"]["name"], item["depth"], item["action"])
                for item in signature["items"]
                if item["element"]["name"]
            ),
            [
                ("Base.run", 1, "must_change"),
                ("app.py", 3, "retest"),
                ("top", 2, "retest"),
                ("use", 1, "must_change"),
            ],
        )
        behaviour = await self.service.get_impact(
            names["helper"], repo, snapshot_id=snapshot, change="behaviour"
        )
        self.assertEqual({i["action"] for i in behaviour["items"]}, {"retest"})
        with self.assertRaises(ValueError):
            await self.service.get_impact(
                names["helper"], repo, snapshot_id=snapshot, change="contract"
            )

    @unittest.skipUnless(GO_READY, "Provision the go grammar")
    @needs_go
    async def test_contract_impact_separates_message_carriers_from_wrappers(self):
        from deepthought.analysis_tools.service_contracts import ServiceContractTool

        repo = "impact-" + uuid4().hex
        self.repos.append(repo)
        with tempfile.TemporaryDirectory() as directory:
            for path, content in GRPC_CLIENT.items():
                (Path(directory) / path).parent.mkdir(parents=True, exist_ok=True)
                (Path(directory) / path).write_text(content)
            trees = await TreeSitterTool(directory).analyze(repo, languages={"go"})
            contracts = await ServiceContractTool(Path(directory)).analyze(repo)
        snapshot = trees.metadata.custom_metrics["snapshot_id"]
        written = await WriteCoordinator(
            self.manager.driver, database=self.manager.database
        ).write_outputs([trees, contracts], repo, snapshot_id=snapshot)
        self.assertTrue(written.success, written.errors)
        method = next(n.properties for n in contracts.nodes if "GrpcMethod" in n.labels)
        self.assertEqual(
            method["message_types"],
            ["example.com/guide/pb.Feature", "example.com/guide/pb.Point"],
        )
        carriers = sorted(
            (
                r.source_match["qualified_name"].split("::")[2],
                r.properties["message_types"],
            )
            for r in contracts.relationships
            if r.type == "CARRIES_MESSAGE"
        )
        self.assertIn(
            ("lookup", ["example.com/guide/pb.Feature", "example.com/guide/pb.Point"]),
            carriers,
        )
        self.assertNotIn("named", [c[0] for c in carriers])
        self.assertNotIn("report", [c[0] for c in carriers])
        impact = await self.service.get_contract_impact(
            method["qualified_name"], repo, snapshot_id=snapshot, max_depth=5
        )
        actions = {
            item["element"]["name"]: (item["depth"], item["action"])
            for item in impact["items"]
            if item["element"]["name"]
        }
        # lookup requests the method; named builds the Point lookup takes;
        # report and main only reach it through named, whose signature
        # hides the messages, so they re-test.
        self.assertEqual(actions["lookup"], (1, "must_change"))
        self.assertEqual(actions["named"], (2, "must_change"))
        self.assertEqual(actions["report"], (3, "retest"))
        self.assertEqual(actions["main"], (4, "retest"))

    @unittest.skipUnless(TS_READY, "Provision the typescript grammar")
    async def test_typescript_contract_messages_come_from_the_stub_signature(self):
        from deepthought.analysis_tools.service_contracts import ServiceContractTool

        repo = "impact-" + uuid4().hex
        self.repos.append(repo)
        with tempfile.TemporaryDirectory() as directory:
            for path, content in TS_GRPC_CLIENT.items():
                (Path(directory) / path).parent.mkdir(parents=True, exist_ok=True)
                (Path(directory) / path).write_text(content)
            trees = await TreeSitterTool(directory).analyze(
                repo, languages={"typescript"}
            )
            contracts = await ServiceContractTool(Path(directory)).analyze(repo)
        snapshot = trees.metadata.custom_metrics["snapshot_id"]
        written = await WriteCoordinator(
            self.manager.driver, database=self.manager.database
        ).write_outputs([trees, contracts], repo, snapshot_id=snapshot)
        self.assertTrue(written.success, written.errors)
        method = next(n.properties for n in contracts.nodes if "GrpcMethod" in n.labels)
        self.assertEqual(
            (method["request_message"], method["response_message"]),
            ("guide.Point", "guide.Feature"),
        )
        self.assertNotIn("message_types", method)
        impact = await self.service.get_contract_impact(
            method["qualified_name"], repo, snapshot_id=snapshot, max_depth=5
        )
        actions = {
            item["element"]["name"]: (item["depth"], item["action"])
            for item in impact["items"]
            if item["element"]["name"]
        }
        # named builds the Point lookup's signature takes; report only calls
        # named, whose signature names no message.
        self.assertEqual(actions["lookup"], (1, "must_change"))
        self.assertEqual(actions["named"], (2, "must_change"))
        self.assertEqual(actions["report"], (3, "retest"))

    async def test_contract_impact_action_is_unknown_without_message_types(self):
        repo = "impact-" + uuid4().hex
        self.repos.append(repo)
        snapshot = "s-" + uuid4().hex

        def node(labels, name, **extra):
            return NodeData(
                labels,
                {
                    "qualified_name": f"{repo}::{name}",
                    "repo_url": repo,
                    "name": name,
                    **extra,
                },
            )

        def edge(kind, source, target, **extra):
            return RelationshipData(
                kind,
                source.labels,
                {
                    "qualified_name": source.properties["qualified_name"],
                    "repo_url": repo,
                },
                target.labels,
                {
                    "qualified_name": target.properties["qualified_name"],
                    "repo_url": repo,
                },
                {**FIXTURE_EVIDENCE, **extra},
            )

        route = node(["HttpRoute", "Contract"], "GET /x")
        client = node(["Function", "Symbol"], "client", file_path="a.py")
        caller = node(["Function", "Symbol"], "caller", file_path="a.py")
        meta = ToolExecutionMetadata(
            "fixture",
            "1",
            0,
            custom_metrics={"snapshot_id": snapshot, "analysis_status": "succeeded"},
        )
        output = StandardizedOutput(
            [route, client, caller],
            [edge("REQUESTS", client, route), edge("CALLS", caller, client)],
            meta,
        )
        written = await WriteCoordinator(
            self.manager.driver, database=self.manager.database
        ).write_tool_output("fixture", output, repo)
        self.assertTrue(written.success, written.errors)
        impact = await self.service.get_contract_impact(
            route.properties["qualified_name"], repo, snapshot_id=snapshot
        )
        self.assertEqual(
            [(i["element"]["name"], i["action"]) for i in impact["items"]],
            [("client", "must_change"), ("caller", "unknown")],
        )

    async def test_contract_impact_follows_foreign_keys(self):
        from deepthought.analysis_tools.data_contracts import DataContractTool

        repo = "impact-" + uuid4().hex
        self.repos.append(repo)
        files = {
            "pyproject.toml": '[project]\nname = "shop"\n',
            "db/schema.sql": "CREATE TABLE users (id int);\n"
            "CREATE TABLE orders (id int, user_id int REFERENCES users (id));\n",
            "shop/store.py": "import sqlite3\n"
            "\n"
            "def totals(conn):\n"
            '    return conn.execute("SELECT id FROM orders")\n',
        }
        with tempfile.TemporaryDirectory() as directory:
            for path, content in files.items():
                (Path(directory) / path).parent.mkdir(parents=True, exist_ok=True)
                (Path(directory) / path).write_text(content)
            trees = await TreeSitterTool(directory).analyze(repo, languages={"python"})
            tables = await DataContractTool(Path(directory)).analyze(
                repo, languages=["python"]
            )
        snapshot = trees.metadata.custom_metrics["snapshot_id"]
        written = await WriteCoordinator(
            self.manager.driver, database=self.manager.database
        ).write_outputs([trees, tables], repo, snapshot_id=snapshot)
        self.assertTrue(written.success, written.errors)
        users = next(
            i["qualified_name"]
            for i in (
                await self.service.search_contracts(
                    "users", repo, snapshot_id=snapshot, kind="DbTable"
                )
            )["items"]
        )
        impact = await self.service.get_contract_impact(
            users, repo, snapshot_id=snapshot, max_depth=3
        )
        # orders references users, and totals reads orders.
        self.assertEqual(
            [(row[0], row[1], row[3]) for row in self.rows(impact)],
            [
                ("orders", 1, ["REFERENCES_TABLE"]),
                ("totals", 2, ["READS", "REFERENCES_TABLE"]),
            ],
        )

    @staticmethod
    def rows_edges(response):
        return [item["edges"] for item in response["items"]]


if __name__ == "__main__":
    unittest.main()
