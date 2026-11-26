"""Indexer symbols bound to the parsed declarations they name."""

import asyncio
import importlib.util
import os
from pathlib import Path
import unittest
from uuid import uuid4

from deepthought.analysis_tools.contracts import NodeData
from deepthought.analysis_tools.declaration_binding import (
    BINDING_RULE,
    bind_declarations,
)
from deepthought.analysis_tools.evidence import registered_rules

READY = all(
    importlib.util.find_spec(m)
    for m in ("tree_sitter", "tree_sitter_typescript", "google.protobuf")
)
FIXTURE = Path(__file__).parent / "fixtures/scip_alias_reexport"
SYMBOL = "scip-typescript npm deepthought-scip-alias-control 0.0.0 `a.ts`/alpha()."


def file(path, digest):
    return NodeData(
        ["File"],
        {
            "qualified_name": path + "::file",
            "repo_url": "r",
            "path": path,
            "file_sha256": digest,
        },
    )


def declaration(name, path="a.ts", utf16=(1, 9, 1, 14)):
    return NodeData(
        ["Function", "Symbol"],
        {
            "qualified_name": "ts::" + name,
            "repo_url": "r",
            "file_path": path,
            "name_range_utf8": [utf16[0], utf16[1] + 1, utf16[2], utf16[3] + 1],
            "name_range_utf16": list(utf16),
            "name_range_utf32": list(utf16),
        },
    )


def symbol(name, digest="d1", encoding="utf-16", at=(1, 9, 1, 14), path="a.ts"):
    return NodeData(
        ["Symbol"],
        {
            "qualified_name": "scip::" + name,
            "repo_url": "r",
            "native_id": name,
            "file_path": path,
            "file_sha256": digest,
            "coordinate_encoding": encoding,
            "start_line": at[0],
            "column": at[1],
            "end_line": at[2],
            "end_column": at[3],
        },
    )


class BindingRuleTests(unittest.TestCase):
    def bind(self, nodes):
        edges, counts = bind_declarations(nodes, "r")
        return (
            sorted(
                (e.source_match["qualified_name"], e.target_match["qualified_name"])
                for e in edges
            ),
            counts,
        )

    def test_same_bytes_and_name_range_in_the_indexer_encoding_bind(self):
        nodes = [
            file("a.ts", "d1"),
            declaration("f"),
            symbol("exact"),
            # The same range counted in UTF-8 lands elsewhere in this line.
            symbol("utf8", encoding="utf-8", at=(1, 10, 1, 15)),
            symbol("other-bytes", digest="d2"),
            symbol("other-range", at=(1, 9, 1, 13)),
            symbol("no-encoding", encoding="unspecified"),
        ]
        edges, counts = self.bind(nodes)
        self.assertEqual(edges, [("scip::exact", "ts::f"), ("scip::utf8", "ts::f")])
        self.assertEqual(counts, {"bound": 2, "ambiguous": 0, "unmatched": 3})

    def test_ambiguous_and_undigested_declarations_do_not_bind(self):
        edges, counts = self.bind(
            [file("a.ts", "d1"), declaration("f"), declaration("g"), symbol("s")]
        )
        self.assertEqual((edges, counts["ambiguous"]), ([], 1))
        # Without the File's digest the declaration's bytes are unknown.
        edges, counts = self.bind([declaration("f"), symbol("s")])
        self.assertEqual((edges, counts["unmatched"]), ([], 1))

    def test_rule_is_registered_as_resolved(self):
        self.assertEqual(
            registered_rules()[BINDING_RULE.rule_id].evidence_type, "resolved"
        )


@unittest.skipUnless(READY, "Provision the typescript and scip extras")
class CombinedBindingTests(unittest.TestCase):
    def combine(self):
        from deepthought.analysis_tools import ScipAnalysisTool, TreeSitterTool
        from deepthought.storage.graph.write_coordinator import (
            WriteCoordinatorConfig,
            _combine,
        )

        tree = asyncio.run(
            TreeSitterTool(FIXTURE).analyze("r", languages={"typescript"})
        )
        scip = asyncio.run(ScipAnalysisTool(FIXTURE).analyze("r"))
        return (
            tree,
            scip,
            _combine([tree, scip], "r", "combined", WriteCoordinatorConfig())[0],
        )

    def test_functions_bind_and_parameters_and_modules_do_not(self):
        tree, scip, combined = self.combine()
        names = {n.properties["qualified_name"]: n.properties for n in combined.nodes}
        bound = sorted(
            (
                names[e.source_match["qualified_name"]]["native_id"].split(" ")[-1],
                names[e.target_match["qualified_name"]]["name"],
                names[e.target_match["qualified_name"]]["start_line"],
                e.properties["rule_id"],
                e.properties["encoding"],
            )
            for e in combined.relationships
            if e.type == "HAS_AST_DECLARATION"
        )
        # Both overload declarations of a.ts alpha are SCIP definitions.
        self.assertEqual(
            bound,
            [
                ("`a.ts`/alpha().", "alpha", 1, BINDING_RULE.rule_id, "utf-16"),
                ("`a.ts`/alpha().", "alpha", 2, BINDING_RULE.rule_id, "utf-16"),
                ("`b.ts`/alpha().", "alpha", 3, BINDING_RULE.rule_id, "utf-16"),
            ],
        )
        self.assertEqual(
            combined.metadata.custom_metrics["declaration_binding"],
            {"bound": 3, "ambiguous": 0, "unmatched": 6},
        )
        # A single tool's output is written as it is, without bindings.
        self.assertFalse(
            any(r.type == "HAS_AST_DECLARATION" for r in scip.relationships)
        )


@unittest.skipUnless(
    READY and os.environ.get("DEEPTHOUGHT_NEO4J_URI"),
    "Provision isolated Neo4j and the typescript and scip extras",
)
class BoundContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from deepthought.graph_queries import GraphQueryService
        from deepthought.storage.graph import (
            Neo4jConnectionManager,
            WriteCoordinator,
            setup_schema,
        )
        from deepthought.analysis_tools import ScipAnalysisTool, TreeSitterTool

        self.manager = Neo4jConnectionManager(
            os.environ["DEEPTHOUGHT_NEO4J_URI"],
            os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
            os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
            database=os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j"),
        )
        await self.manager.connect()
        self.addAsyncCleanup(self.manager.close)
        await setup_schema(self.manager.driver, database=self.manager.database)
        self.repo = "binding-" + uuid4().hex
        self.addAsyncCleanup(self.remove)
        tree = await TreeSitterTool(FIXTURE).analyze(
            self.repo, languages={"typescript"}
        )
        scip = await ScipAnalysisTool(FIXTURE).analyze(self.repo)
        self.snapshot = "combined-" + uuid4().hex
        written = await WriteCoordinator(
            self.manager.driver, database=self.manager.database
        ).write_outputs([tree, scip], self.repo, snapshot_id=self.snapshot)
        self.assertTrue(written.success, written.errors)
        self.service = GraphQueryService(
            self.manager.driver, database=self.manager.database
        )

    async def remove(self):
        async with self.manager.driver.session(
            database=self.manager.database
        ) as session:
            await (
                await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=self.repo
                )
            ).consume()

    async def test_a_place_names_the_parsed_declaration_not_indexer_nodes(self):
        # b.ts:3 declares alpha and its parameter; the index defines both
        # there too. The place names the parsed function alone.
        callers = await self.service.get_function_callers(
            "b.ts:3", self.repo, snapshot_id=self.snapshot
        )
        owner = await self.service.get_impact(
            "b.ts:3", self.repo, snapshot_id=self.snapshot
        )
        self.assertNotIn(
            "scip-definition",
            owner["items"][0]["element"]["qualified_name"] if owner["items"] else "",
        )
        self.assertEqual(
            [i["relationship"]["properties"]["line"] for i in callers["items"]], [5]
        )
        async with self.manager.driver.session(
            database=self.manager.database
        ) as session:
            on_line = await (
                await session.run(
                    "MATCH (d:Symbol {repo_url:$repo,snapshot_id:$snapshot,"
                    "file_path:'b.ts',start_line:3}) RETURN count(d) AS n",
                    repo=self.repo,
                    snapshot=self.snapshot,
                )
            ).single()
        # The fixture does put indexer definitions on that line.
        self.assertGreater(on_line["n"], 1)

    async def test_an_indexer_symbol_returns_its_declaration_source(self):
        context = await self.service.get_code_context(
            "a.ts",
            self.repo,
            snapshot_id=self.snapshot,
            native_symbol=SYMBOL,
            include_relationships=False,
        )
        self.assertEqual(
            [
                (row["element"]["source_code"], row["element"]["native_ids"])
                for row in context["items"]
            ],
            [
                ("export function alpha(value: string): string;", [SYMBOL]),
                (
                    "export function alpha(value: unknown): unknown { return value; }",
                    [SYMBOL],
                ),
            ],
        )
        # By line, a bound pair is one element, not one per tool.
        line = await self.service.get_code_context(
            "a.ts",
            self.repo,
            snapshot_id=self.snapshot,
            line_number=2,
            include_relationships=False,
        )
        elements = [row["element"] for row in line["items"]]
        declared = [e for e in elements if e["source_code"] is not None]
        self.assertEqual(len(declared), 1)
        self.assertEqual(declared[0]["native_ids"], [SYMBOL])
        # Unbound indexer symbols (parameters, the module) have no text.
        self.assertTrue(
            all(e["source_code"] is None for e in elements if e not in declared)
        )


if __name__ == "__main__":
    unittest.main()
