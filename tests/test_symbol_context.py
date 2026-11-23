"""Source-authored ownership/body controls and actual shared-writer queries."""

import asyncio
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from deepthought import analyze_repository
from deepthought.analysis_tools import TreeSitterTool, ScipAnalysisTool
from deepthought.storage.graph.write_coordinator import _combine, WriteCoordinatorConfig

GRAMMARS = all(
    importlib.util.find_spec(m)
    for m in ("tree_sitter", "tree_sitter_python", "tree_sitter_typescript")
)
SCIP = (
    importlib.util.find_spec("google") is not None
    and importlib.util.find_spec("google.protobuf") is not None
)
FIXTURE = Path(__file__).parent / "fixtures/scip_alias_reexport"


@unittest.skipUnless(GRAMMARS, "Provision Python and TypeScript grammars")
class SymbolContextTests(unittest.TestCase):
    def test_exact_source_parent_ids_unicode_and_unchanged_sdk(self):
        source = """export class Outer {
  run() {
    const marker = "😀"; const nested = () => "é";
    function duplicate() { return marker; }
    return nested();
  }
}
function duplicate() { return "outside"; }
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.ts").write_text(source)
            before = analyze_repository(
                root, repository_id="owned", languages=("typescript",)
            )
            context = {}
            augmented = analyze_repository(
                root,
                repository_id="owned",
                languages=("typescript",),
                _declaration_context=context,
            )
            self.assertEqual(before.to_json(), augmented.to_json())
            tool = TreeSitterTool(root)
            output = asyncio.run(tool.analyze("owned", {"typescript"}))
            nodes = {
                n.properties["name"]: n.properties
                for n in output.nodes
                if "Symbol" in n.labels
            }
            self.assertEqual(
                nodes["Outer"]["source_code"],
                source[: source.index("\nfunction duplicate")],
            )
            self.assertEqual(
                nodes["Outer.run.nested"]["source_code"], 'nested = () => "é"'
            )
            self.assertEqual(
                nodes["Outer.run"]["parent_definition_id"],
                nodes["Outer"]["definition_id"],
            )
            self.assertEqual(
                nodes["Outer.run.nested"]["parent_definition_id"],
                nodes["Outer.run"]["definition_id"],
            )
            self.assertEqual(nodes["Outer.run.nested"]["lod"], 2)
            self.assertIsNone(nodes["duplicate"]["parent_definition_id"])
            ranges = nodes["Outer.run.nested"]["name_ranges"]
            prefix = source.splitlines()[2].split("nested")[0]
            self.assertEqual(ranges["utf-8"][1], len(prefix.encode()))
            self.assertEqual(ranges["utf-16"][1], len(prefix.encode("utf-16-le")) // 2)
            self.assertEqual(ranges["utf-32"][1], len(prefix))
            for name, p in nodes.items():
                self.assertEqual(
                    p["source_sha256"],
                    sha256(p["source_code"].encode()).hexdigest(),
                    name,
                )
            from deepthought.storage.graph.writer import _prepare

            repeat = asyncio.run(tool.analyze("owned", {"typescript"}))
            snapshot = output.metadata.custom_metrics["snapshot_id"]
            self.assertEqual(
                _prepare(output, "owned", snapshot)[1],
                _prepare(repeat, "owned", snapshot)[1],
            )

    def test_decorated_python_bodies_and_lexical_levels(self):
        source = 'class Outer:\n    @staticmethod\n    def run():\n        def inner():\n            return "é"\n        return inner()\n'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.py").write_text(source)
            output = asyncio.run(TreeSitterTool(root).analyze("owned", {"python"}))
            nodes = {
                n.properties["name"]: n.properties
                for n in output.nodes
                if "Symbol" in n.labels
            }
            self.assertTrue(
                nodes["Outer.run"]["source_code"].startswith(
                    "@staticmethod\n    def run"
                )
            )
            self.assertEqual(nodes["Outer.run"]["source_start_line"], 2)
            self.assertEqual(nodes["Outer.run.inner"]["lod"], 2)
            self.assertEqual(
                nodes["Outer.run.inner"]["parent_definition_id"],
                nodes["Outer.run"]["definition_id"],
            )

    @unittest.skipUnless(SCIP, "Provision SCIP protocol")
    def test_exact_scip_ast_binding_and_ambiguous_or_mismatched_source(self):
        tree = asyncio.run(TreeSitterTool(FIXTURE).analyze("owned", {"typescript"}))
        scip = asyncio.run(ScipAnalysisTool(FIXTURE).analyze("owned"))
        combined, _ = _combine(
            [tree, scip], "owned", "context-test", WriteCoordinatorConfig()
        )
        semantic = [
            n
            for n in combined.nodes
            if n.properties.get("native_id", "").endswith("/alpha().")
            and n.properties.get("file_path") == "a.ts"
        ]
        self.assertEqual(len(semantic), 2)
        self.assertTrue(
            all(n.properties["ast_binding_status"] == "resolved" for n in semantic)
        )
        links = [r for r in combined.relationships if r.type == "HAS_AST_DECLARATION"]
        self.assertTrue(links)
        from deepthought.analysis_tools import NodeData

        external = deepcopy(scip)
        external.nodes.append(
            NodeData(
                ["Symbol"],
                {
                    "qualified_name": "external",
                    "repo_url": "owned",
                    "native_id": "external-symbol",
                },
            )
        )
        external_output, _ = _combine(
            [tree, external], "owned", "external", WriteCoordinatorConfig()
        )
        external_node = next(
            n
            for n in external_output.nodes
            if n.properties["qualified_name"] == "external"
        )
        self.assertEqual(external_node.properties["ast_binding_status"], "unmatched")
        altered = deepcopy(scip)
        for n in altered.nodes:
            if n.properties.get("native_id"):
                n.properties["file_sha256"] = "different-source"
        mismatch, _ = _combine(
            [tree, altered], "owned", "mismatch", WriteCoordinatorConfig()
        )
        self.assertFalse(
            [r for r in mismatch.relationships if r.type == "HAS_AST_DECLARATION"]
        )
        ambiguous = deepcopy(tree)
        duplicate = deepcopy(
            next(
                n
                for n in tree.nodes
                if n.properties.get("name") == "alpha"
                and n.properties.get("file_path") == "a.ts"
            )
        )
        duplicate.properties["qualified_name"] += "::duplicate"
        duplicate.properties["definition_id"] = "duplicate"
        ambiguous.nodes.append(duplicate)
        result, _ = _combine(
            [ambiguous, scip], "owned", "ambiguous", WriteCoordinatorConfig()
        )
        self.assertTrue(
            any(
                n.properties.get("ast_binding_status") == "ambiguous"
                for n in result.nodes
            )
        )


@unittest.skipUnless(
    GRAMMARS and SCIP and os.environ.get("DEEPTHOUGHT_NEO4J_URI"),
    "Provision isolated Neo4j and analyzers",
)
class SymbolContextGraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_bounded_unicode_source_pages_ownership_semantic_lookup_and_receipts(
        self,
    ):
        from deepthought.storage.graph import (
            Neo4jConnectionManager,
            WriteCoordinator,
            setup_schema,
        )
        from deepthought.graph_queries import GraphQueryService

        repo = "context-" + uuid4().hex
        manager = Neo4jConnectionManager(
            os.environ["DEEPTHOUGHT_NEO4J_URI"],
            os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
            os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
        )
        driver = await manager.connect()
        try:
            await setup_schema(driver)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = (
                    'class Outer:\n    def run(self):\n        return "😀é'
                    + ("x" * 18000)
                    + '"\n'
                )
                (root / "a.py").write_text(source)
                tree = await TreeSitterTool(root).analyze(repo, {"python"})
                written = await WriteCoordinator(driver).write_tool_output(
                    "tree-sitter", tree, repo
                )
                self.assertTrue(written.success, written.errors)
                events = []
                api = GraphQueryService(driver, observer=events.append)
                kwargs = {
                    "snapshot_id": written.snapshot_id,
                    "line_number": 3,
                    "limit": 1,
                    "max_source_characters": 17,
                }
                first = await api.get_code_context("a.py", repo, **kwargs)
                self.assertEqual(
                    events[-1]["response_sha256"],
                    sha256(
                        json.dumps(first, sort_keys=True, ensure_ascii=False).encode()
                    ).hexdigest(),
                )
                body = first["items"][0]["element"]
                self.assertEqual(body["name"], "Outer.run")
                self.assertTrue(body["source_truncated"])
                self.assertTrue(body["parent_definition_id"])
                self.assertEqual(first["source_scope"], "stored_snapshot")
                self.assertTrue(first["relationships"])
                for relationship in first["relationships"]:
                    self.assertIn("properties", relationship["source"])
                    self.assertIn("labels", relationship["target"])
                    self.assertIsNone(
                        relationship["source"]["properties"].get("source_code")
                    )
                self.assertEqual(first["source_verification"]["status"], "unverified")
                code = body["source_code"]
                offset = body["source_next_offset"]
                while offset is not None:
                    page = await api.get_code_context(
                        "a.py",
                        repo,
                        snapshot_id=written.snapshot_id,
                        definition_id=body["definition_id"],
                        source_offset=offset,
                        max_source_characters=8192,
                        limit=1,
                        max_output_bytes=65536,
                    )
                    part = page["items"][0]["element"]
                    code += part["source_code"]
                    offset = part["source_next_offset"]
                expected = 'def run(self):\n        return "😀é' + ("x" * 18000) + '"'
                self.assertEqual(code, expected)
                self.assertEqual(
                    sha256(code.encode()).hexdigest(), body["source_sha256"]
                )
                (root / "a.py").write_text("def changed(): pass\n")
                historical = await api.get_code_context("a.py", repo, **kwargs)
                self.assertEqual(
                    historical["items"][0]["element"]["source_code"],
                    body["source_code"],
                )
                self.assertEqual(
                    historical["source_verification"]["status"], "unverified"
                )
            tree = await TreeSitterTool(FIXTURE).analyze(repo, {"typescript"})
            scip = await ScipAnalysisTool(FIXTURE).analyze(repo)
            written = await WriteCoordinator(driver).write_outputs(
                [tree, scip], repo, snapshot_id="bound-alias"
            )
            self.assertTrue(written.success, written.errors)
            native = next(
                n.properties["native_id"]
                for n in scip.nodes
                if n.properties.get("file_path") == "a.ts"
                and n.properties.get("native_id", "").endswith("/alpha().")
            )
            context = await api.get_code_context(
                "a.ts", repo, snapshot_id="bound-alias", native_symbol=native
            )
            self.assertEqual(context["returned"], 2)
            self.assertTrue(
                all(i["element"]["source_available"] for i in context["items"])
            )
            self.assertTrue(
                any(
                    r["relationship"]["type"] == "HAS_AST_DECLARATION"
                    for r in context["relationships"]
                )
            )
        finally:
            async with driver.session() as session:
                result = await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=repo
                )
                await result.consume()
            await manager.close()
