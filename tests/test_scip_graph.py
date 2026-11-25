"""Real shared-writer and query conformance for registered SCIP records."""

import os
import unittest
from uuid import uuid4

from deepthought.analysis_tools import ScipAnalysisTool, TreeSitterTool
from deepthought.graph_queries import GraphQueryService
from deepthought.storage.graph import (
    Neo4jConnectionManager,
    WriteCoordinator,
    setup_schema,
)
import test_scip_tool as fixtures


@unittest.skipUnless(
    fixtures.PROTOBUF and os.environ.get("DEEPTHOUGHT_NEO4J_URI"),
    "Provision isolated Neo4j and scip extra",
)
class ScipGraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_overloads_resolve_to_every_declaration_and_page_completely(self):
        wire = self.fixture.wire
        source = "export function alpha(): void;\n" * 5 + "export function alpha() {}\n"
        (self.fixture.root / "a.ts").write_text(source)
        (self.fixture.root / "b.ts").write_text(
            'import { alpha } from "./a";\n' + "alpha();\n" * 26
        )
        index = wire.Index()
        index.metadata.tool_info.name = "scip-typescript"
        index.metadata.tool_info.version = "0.6.0"
        a = index.documents.add(relative_path="a.ts", language="typescript")
        a.symbols.add(symbol=self.fixture.symbol, kind=wire.SymbolInformation.Function)
        for line in range(6):
            a.occurrences.add(
                symbol=self.fixture.symbol,
                range=[line, 16, 21],
                symbol_roles=wire.Definition,
            )
        b = index.documents.add(relative_path="b.ts", language="typescript")
        b.occurrences.add(symbol=self.fixture.symbol, range=[0, 9, 14])
        for line in range(1, 27):
            b.occurrences.add(symbol=self.fixture.symbol, range=[line, 0, 5])
        self.fixture.index.write_bytes(index.SerializeToString())
        self.fixture.seal()
        output = await ScipAnalysisTool(self.fixture.root).analyze(
            self.repo, languages={"typescript"}
        )
        snapshot = output.metadata.custom_metrics["snapshot_id"]
        written = await self.writer.write_tool_output("scip", output, self.repo)
        self.assertTrue(written.success, written.errors)
        rows, cursor, pages = [], "", 0
        while True:
            page = await self.service.get_references(
                self.fixture.symbol,
                self.repo,
                snapshot_id=snapshot,
                limit=100,
                after_reference_id=cursor,
            )
            pages += 1
            self.assertLess(pages, 20, "the cursor must advance")
            rows += page["items"]
            if not page["truncated"]:
                break
            cursor = page["items"][-1]["reference"]["reference_id"]
        self.assertGreater(pages, 1)
        expected = {
            n.properties["reference_id"]
            for n in output.nodes
            if "Reference" in n.labels
        }
        self.assertEqual(
            [r["reference"]["reference_id"] for r in rows], sorted(expected)
        )
        declarations = {
            n.properties["definition_id"] for n in output.nodes if "Symbol" in n.labels
        }
        for row in rows:
            # Overloads are one symbol declared six times: resolved, not ambiguous.
            self.assertEqual(row["reference"]["resolution"], "resolved")
            self.assertEqual(row["reference"]["declaration_resolution"], "multiple")
            self.assertEqual({t["definition_id"] for t in row["targets"]}, declarations)
        dependencies = await self.service.get_file_dependencies(
            "b.ts", self.repo, snapshot_id=snapshot
        )
        self.assertEqual(dependencies["items"][0]["file"], "a.ts")
        self.assertEqual(dependencies["items"][0]["witness_count"], 27)
        self.assertEqual(len(dependencies["items"][0]["witness_ids"]), 20)
        empty = await self.service.get_references(
            "absent", self.repo, snapshot_id=snapshot
        )
        self.assertEqual((empty["returned"], empty["truncated"]), (0, False))

    async def asyncSetUp(self):
        self.fixture = fixtures.ScipToolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo = "scip-test-" + uuid4().hex
        self.manager = Neo4jConnectionManager(
            os.environ["DEEPTHOUGHT_NEO4J_URI"],
            os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
            os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
            database=os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j"),
        )
        await self.manager.connect()
        self.addAsyncCleanup(self.manager.close)
        await setup_schema(self.manager.driver, database=self.manager.database)
        self.service = GraphQueryService(
            self.manager.driver, database=self.manager.database
        )
        self.writer = WriteCoordinator(
            self.manager.driver, database=self.manager.database
        )
        from deepthought.analysis_tools import ScipAnalysisTool

        self.output = await ScipAnalysisTool(self.fixture.root).analyze(
            self.repo, languages={"typescript"}
        )
        self.snapshot = self.output.metadata.custom_metrics["snapshot_id"]
        result = await self.writer.write_tool_output("scip", self.output, self.repo)
        self.assertTrue(result.success, result.errors)
        self.addAsyncCleanup(self.remove_scope)

    async def remove_scope(self):
        async with self.manager.driver.session(
            database=self.manager.database
        ) as session:
            await session.run(
                "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=self.repo
            )

    async def test_reference_pagination_native_lookup_and_witnessed_dependency(self):
        definitions = await self.service.find_symbol_by_name(
            "alpha", self.repo, snapshot_id=self.snapshot
        )
        self.assertEqual(definitions["returned"], 1)
        self.assertEqual(
            definitions["items"][0]["element"]["properties"]["native_id"],
            self.fixture.symbol,
        )
        page = await self.service.get_references(
            self.fixture.symbol, self.repo, snapshot_id=self.snapshot, limit=1
        )
        self.assertTrue(page["truncated"])
        self.assertEqual(page["returned"], 1)
        cursor = page["items"][0]["reference"]["reference_id"]
        next_page = await self.service.get_references(
            self.fixture.symbol,
            self.repo,
            snapshot_id=self.snapshot,
            limit=1,
            after_reference_id=cursor,
        )
        self.assertFalse(next_page["truncated"])
        self.assertEqual(next_page["returned"], 1)
        rows = page["items"] + next_page["items"]
        expected = {
            n.properties["reference_id"]
            for n in self.output.nodes
            if "Reference" in n.labels
            and n.properties["native_id"] == self.fixture.symbol
        }
        self.assertEqual({r["reference"]["reference_id"] for r in rows}, expected)
        self.assertEqual({r["reference"]["file_path"] for r in rows}, {"a.ts", "b.ts"})
        target = rows[0]["targets"][0]
        self.assertEqual(target["file_path"], "a.ts")
        explained = await self.service.explain_edge(
            target["edge_id"], self.repo, snapshot_id=self.snapshot
        )
        evidence = explained["items"][0]["evidence"]
        self.assertEqual(
            (evidence["evidence_type"], evidence["rule_id"], evidence["rule_status"]),
            ("resolved", "scip.native-symbol-reference", "registered"),
        )
        dependencies = await self.service.get_file_dependencies(
            "b.ts", self.repo, snapshot_id=self.snapshot
        )
        self.assertEqual(dependencies["returned"], 1)
        item = dependencies["items"][0]
        self.assertEqual(item["file"], "a.ts")
        self.assertEqual(
            (item["evidence_type"], item["rule_id"]),
            ("resolved", "scip.reference-dependency"),
        )
        self.assertEqual(item["witness_count"], 1)
        self.assertLessEqual(set(item["witness_ids"]), expected)
        dependents = await self.service.get_file_dependents(
            "a.ts", self.repo, snapshot_id=self.snapshot
        )
        self.assertEqual([i["file"] for i in dependents["items"]], ["b.ts"])
        self.assertEqual(dependents["items"][0]["edge_id"], item["edge_id"])

    async def test_local_and_unresolved_queries_preserve_semantics(self):
        candidates = await self.service.find_symbol_by_name(
            "same", self.repo, snapshot_id=self.snapshot
        )
        self.assertEqual(candidates["returned"], 2)
        self.assertEqual(
            len(
                {
                    row["element"]["properties"]["file_path"]
                    for row in candidates["items"]
                }
            ),
            2,
        )
        with self.assertRaisesRegex(ValueError, "document scope"):
            await self.service.get_references(
                "local 0", self.repo, snapshot_id=self.snapshot
            )
        for file in ["a.ts", "b.ts"]:
            result = await self.service.get_references(
                "local 0", self.repo, snapshot_id=self.snapshot, file_path=file
            )
            self.assertEqual(result["returned"], 1)
            self.assertEqual(result["items"][0]["reference"]["file_path"], file)
            self.assertEqual(
                {t["file_path"] for t in result["items"][0]["targets"]}, {file}
            )
        external = await self.service.get_references(
            "scip-typescript npm outside 2 x.ts/external().",
            self.repo,
            snapshot_id=self.snapshot,
        )
        self.assertEqual(external["returned"], 1)
        self.assertEqual(external["items"][0]["reference"]["resolution"], "unresolved")
        self.assertEqual(external["items"][0]["targets"], [])

    async def test_tree_only_rejects_unavailable_references_and_combined_keeps_them(
        self,
    ):
        tree = await TreeSitterTool(self.fixture.root).analyze(
            self.repo, languages={"typescript"}
        )
        revision = tree.metadata.custom_metrics["snapshot_id"]
        imported = await self.writer.write_tool_output("tree-sitter", tree, self.repo)
        self.assertTrue(imported.success, imported.errors)
        with self.assertRaisesRegex(ValueError, "does not provide references"):
            await self.service.get_references(
                self.fixture.symbol, self.repo, snapshot_id=revision
            )
        with self.assertRaisesRegex(ValueError, "does not provide dependencies"):
            await self.service.get_file_dependencies(
                "b.ts", self.repo, snapshot_id=revision
            )
        combined = await self.writer.write_outputs(
            [tree, self.output], self.repo, snapshot_id="combined-" + self.snapshot
        )
        self.assertTrue(combined.success, combined.errors)
        refs = await self.service.get_references(
            self.fixture.symbol, self.repo, snapshot_id=combined.snapshot_id
        )
        self.assertEqual(refs["returned"], 2)
        async with self.manager.driver.session(
            database=self.manager.database
        ) as session:
            # Both tools' edges coexist, each with its own evidence.
            result = await session.run(
                "MATCH (n {repo_url:$repo,snapshot_id:$snapshot})-[r]->() "
                "RETURN type(r) AS type, r.evidence_type AS evidence, "
                "r.rule_id STARTS WITH 'scip.' AS scip, count(r) AS n",
                repo=self.repo,
                snapshot=combined.snapshot_id,
            )
            rows = {
                (row["type"], row["evidence"], row["scip"]): row["n"]
                async for row in result
            }
        self.assertTrue(all(evidence is not None for _, evidence, _ in rows))
        self.assertIn(("REFERENCES", "resolved", True), rows)
        self.assertIn(("DEPENDS_ON", "resolved", True), rows)
        self.assertTrue(all(not scip for (kind, _, scip) in rows if kind == "CALLS"))


@unittest.skipUnless(
    fixtures.PROTOBUF and os.environ.get("DEEPTHOUGHT_NEO4J_URI"),
    "Provision isolated Neo4j and scip extra",
)
class ScipCommandTests(unittest.TestCase):
    def test_index_real_index_then_query_references_and_dependents(self):
        import asyncio
        import json
        import shutil
        import tempfile
        from pathlib import Path

        from test_incremental import run_module

        repo = "scip-cli-" + uuid4().hex
        self.addCleanup(lambda: asyncio.run(self.cleanup(repo)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "work"
            shutil.copytree(
                Path(__file__).parent / "fixtures/scip_alias_reexport", root
            )

            def command(*arguments):
                result = run_module("deepthought.graph_cli", *arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            indexed = command(
                "index",
                str(root),
                "--tool",
                "scip",
                "--scip-index",
                str(root / "index.scip"),
                "--repository-id",
                repo,
            )
            scope = ["--repository-id", repo, "--snapshot", indexed["snapshot_id"]]
            alpha = command("search", *scope, "--query", "alpha", "--limit", "10")
            native = next(
                item["element"]["native_id"]
                for item in alpha["items"]
                if item["element"]["file_path"] == "a.ts"
                and item["element"]["display_name"] == "alpha"
            )
            references = command(
                "references", *scope, "--symbol", native, "--limit", "100"
            )
            self.assertEqual(references["returned"], 7)
            self.assertTrue(
                all(len(item["targets"]) == 2 for item in references["items"])
            )
            dependents = command("dependents", *scope, "--file", "a.ts")
            # c.ts's aliased consumer binding and call name alpha directly.
            self.assertEqual(
                [item["file"] for item in dependents["items"]], ["b.ts", "c.ts"]
            )
            outward = command(
                "references", *scope, "--symbol", "local 2", "--file", "c.ts"
            )
            # The renamed re-export stays unresolved; nothing is inferred by name.
            self.assertEqual([item["targets"] for item in outward["items"]], [[]])
            (root / "c.ts").write_text("export const changed = 1;\n")
            stale = run_module(
                "deepthought.graph_cli",
                "index",
                str(root),
                "--tool",
                "scip",
                "--scip-index",
                str(root / "index.scip"),
                "--repository-id",
                repo,
            )
            self.assertNotEqual(stale.returncode, 0)
            self.assertIn("regenerate the index", stale.stderr)

    async def cleanup(self, repo):
        manager = Neo4jConnectionManager(
            os.environ["DEEPTHOUGHT_NEO4J_URI"],
            os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
            os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
            database=os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j"),
        )
        await manager.connect()
        try:
            async with manager.driver.session(database=manager.database) as session:
                result = await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=repo
                )
                await result.consume()
        finally:
            await manager.close()
