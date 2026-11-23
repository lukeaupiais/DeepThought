"""Real shared-writer and query conformance for registered SCIP records."""

import os
import json
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
    async def test_compact_overload_pages_preserve_every_reference_and_witness(self):
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
        raw = await self.service.get_references(
            self.fixture.symbol, self.repo, snapshot_id=snapshot, limit=100
        )
        self.assertTrue(raw["truncated"])
        compact = await self.service.get_references(
            self.fixture.symbol,
            self.repo,
            snapshot_id=snapshot,
            limit=100,
            compact=True,
        )
        self.assertEqual(compact["returned"], 27)
        self.assertFalse(compact["truncated"])
        self.assertIsNone(compact["next_cursor"])
        self.assertLessEqual(
            len(json.dumps(compact, sort_keys=True, ensure_ascii=False).encode()), 16000
        )
        expected = {
            n.properties["reference_id"]: n.properties
            for n in output.nodes
            if "Reference" in n.labels
        }
        for row in compact["items"]:
            ref = {**compact["reference_common"], **row["reference"]}
            original = expected[ref["reference_id"]]
            self.assertEqual(ref, {key: original[key] for key in ref})
            self.assertEqual(ref["resolution"], "resolved")
            self.assertEqual(len(ref["target_ids"]), 6)
        first = await self.service.get_references(
            self.fixture.symbol, self.repo, snapshot_id=snapshot, limit=10, compact=True
        )
        self.assertEqual(
            first["next_cursor"], first["items"][-1]["reference"]["reference_id"]
        )
        second = await self.service.get_references(
            self.fixture.symbol,
            self.repo,
            snapshot_id=snapshot,
            limit=100,
            compact=True,
            after_reference_id=first["next_cursor"],
        )
        self.assertEqual(first["returned"] + second["returned"], 27)
        definitions = await self.service.find_symbol_by_name(
            "alpha", self.repo, snapshot_id=snapshot, compact=True
        )
        self.assertEqual(definitions["returned"], 1)
        self.assertEqual(len(definitions["items"][0]["declarations"]), 6)
        dependencies = await self.service.get_file_dependencies(
            "b.ts", self.repo, snapshot_id=snapshot
        )
        self.assertEqual(dependencies["items"][0]["witness_count"], 27)
        with self.assertRaisesRegex(ValueError, "First result exceeds"):
            await self.service.get_references(
                self.fixture.symbol,
                self.repo,
                snapshot_id=snapshot,
                compact=True,
                max_output_bytes=1024,
            )
        empty = await self.service.get_references(
            "absent",
            self.repo,
            snapshot_id=snapshot,
            compact=True,
        )
        self.assertEqual(empty["reference_common"], {})
        self.assertFalse(empty["truncated"])
        self.assertIsNone(empty["next_cursor"])

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
        dependencies = await self.service.get_file_dependencies(
            "b.ts", self.repo, snapshot_id=self.snapshot
        )
        self.assertEqual(dependencies["returned"], 1)
        self.assertEqual(dependencies["items"][0]["target_file"], "a.ts")
        self.assertEqual(dependencies["items"][0]["witness_count"], 1)
        self.assertLessEqual(set(dependencies["items"][0]["witness_ids"]), expected)

    async def test_local_and_unresolved_queries_preserve_semantics(self):
        candidates = await self.service.find_symbol_by_name(
            "same",
            self.repo,
            snapshot_id=self.snapshot,
            compact=True,
        )
        self.assertEqual(candidates["returned"], 2)
        self.assertEqual(len({row["symbol_id"] for row in candidates["items"]}), 2)
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
        external = await self.service.get_references(
            "scip-typescript npm outside 2 x.ts/external().",
            self.repo,
            snapshot_id=self.snapshot,
        )
        self.assertEqual(external["returned"], 1)
        self.assertEqual(external["items"][0]["reference"]["resolution"], "unresolved")
        self.assertEqual(external["items"][0]["reference"]["target_ids"], [])

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
            result = await session.run(
                "MATCH (n {repo_url:$repo,snapshot_id:$snapshot})-[r:CALLS]->() RETURN count(r) AS n",
                repo=self.repo,
                snapshot=combined.snapshot_id,
            )
            self.assertEqual((await result.single())["n"], 0)
