"""IMPLEMENTS edges from SCIP is_implementation relationships."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from uuid import uuid4

from deepthought.analysis_tools import ScipAnalysisTool
import test_scip_tool as fixtures

FIXTURE = Path(__file__).parent / "fixtures" / "scip_implementations"
# (source display name, line) -> (target display name, line), from a.ts.
EXPECTED = {
    (("Dog", 4), ("Animal", 1)),
    (("Dog", 4), ("Pet", 2)),
    (("Dog", 4), ("Base", 3)),
    (("name", 5), ("name", 1)),
    (("owner", 5), ("owner", 2)),
    (("sound", 6), ("sound", 1)),
    (("greet", 7), ("greet", 3)),
    (("Shadow", 9), ("Base", 3)),
    (("greet", 9), ("greet", 3)),
    (("Square", 11), ("Shape", 10)),
    (("area", 11), ("area", 10)),
}


def implements(output):
    nodes = {n.properties["qualified_name"]: n.properties for n in output.nodes}
    found = set()
    for r in output.relationships:
        if r.type == "IMPLEMENTS":
            a = nodes[r.source_match["qualified_name"]]
            b = nodes[r.target_match["qualified_name"]]
            found.add(
                (
                    (a["display_name"], a["start_line"]),
                    (b["display_name"], b["start_line"]),
                )
            )
            assert (r.properties["evidence_type"], r.properties["rule_id"]) == (
                "resolved",
                "scip.symbol-implementation",
            )
    return found


@unittest.skipUnless(fixtures.PROTOBUF, "Provision the scip extra")
class ScipImplementationTests(unittest.TestCase):
    def test_real_index_relationships_become_exact_edges(self):
        output = asyncio.run(ScipAnalysisTool(FIXTURE).analyze("r"))
        self.assertEqual(implements(output), EXPECTED)
        metrics = output.metadata.custom_metrics
        self.assertEqual(
            metrics["implementation_relationships"],
            {"linked": 11, "external_target": 0},
        )
        self.assertIn("implementations", metrics["capabilities"])

    def test_overload_sites_external_targets_and_local_scope(self):
        fixture = fixtures.ScipToolTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        wire = fixture.wire
        (fixture.root / "a.ts").write_text(
            "class Base { run(): void; run() {} }\n"
            "class Impl extends Base { run() {} }\n"
            "class Ext implements Iterable { }\n"
            "function f() { class L {} class M extends L {} }\n"
        )
        (fixture.root / "b.ts").write_text("function g() { class M {} }\n")
        index = wire.Index()
        index.metadata.tool_info.name = "scip-typescript"
        index.metadata.tool_info.version = "0.6.0"
        prefix = "scip-typescript npm demo 1 a.ts/"
        a = index.documents.add(relative_path="a.ts", language="typescript")
        b = index.documents.add(relative_path="b.ts", language="typescript")

        def define(document, symbol, ranges, kind, related=()):
            info = document.symbols.add(symbol=symbol, kind=kind)
            for target in related:
                info.relationships.add(symbol=target, is_implementation=True)
            for location in ranges:
                document.occurrences.add(
                    symbol=symbol, range=location, symbol_roles=wire.Definition
                )

        kinds = wire.SymbolInformation
        define(a, prefix + "Base#", [[0, 6, 10]], kinds.Class)
        define(a, prefix + "Base#run().", [[0, 13, 16], [0, 26, 29]], kinds.Method)
        define(a, prefix + "Impl#", [[1, 6, 10]], kinds.Class, [prefix + "Base#"])
        define(
            a,
            prefix + "Impl#run().",
            [[1, 26, 29]],
            kinds.Method,
            [prefix + "Base#run()."],
        )
        define(
            a,
            prefix + "Ext#",
            [[2, 6, 9]],
            kinds.Class,
            ["scip-typescript npm typescript 5 lib/Iterable#"],
        )
        define(a, "local 0", [[3, 21, 22]], kinds.Class)
        define(a, "local 1", [[3, 30, 31]], kinds.Class, ["local 0"])
        # b.ts's own `local 0` must not be taken for a.ts's.
        define(b, "local 0", [[0, 21, 22]], kinds.Class)
        fixture.index.write_bytes(index.SerializeToString())
        fixture.seal()
        output = asyncio.run(ScipAnalysisTool(fixture.root).analyze("r"))
        nodes = {n.properties["qualified_name"]: n.properties for n in output.nodes}
        edges = []
        for r in output.relationships:
            if r.type == "IMPLEMENTS":
                a = nodes[r.source_match["qualified_name"]]
                b = nodes[r.target_match["qualified_name"]]
                edges.append(
                    (
                        a["file_path"],
                        a["native_id"].removeprefix(prefix),
                        a["start_line"],
                        b["file_path"],
                        b["native_id"].removeprefix(prefix),
                        b["start_line"],
                    )
                )
        self.assertEqual(
            sorted(edges),
            [
                ("a.ts", "Impl#", 2, "a.ts", "Base#", 1),
                # One overridden method declared at two sites links to both.
                ("a.ts", "Impl#run().", 2, "a.ts", "Base#run().", 1),
                ("a.ts", "Impl#run().", 2, "a.ts", "Base#run().", 1),
                # Document-scoped locals stay in their own document.
                ("a.ts", "local 1", 4, "a.ts", "local 0", 4),
            ],
        )
        method_targets = {
            r.target_match["qualified_name"]
            for r in output.relationships
            if r.type == "IMPLEMENTS"
            and nodes[r.target_match["qualified_name"]]["native_id"]
            == prefix + "Base#run()."
        }
        self.assertEqual(len(method_targets), 2)
        self.assertEqual(
            output.metadata.custom_metrics["implementation_relationships"],
            {"linked": 3, "external_target": 1},
        )
        # Unselected sources are skipped; nothing links outside the selection.
        narrowed = asyncio.run(
            ScipAnalysisTool(fixture.root).analyze("r", skip_patterns=["a.ts"])
        )
        self.assertFalse([r for r in narrowed.relationships if r.type == "IMPLEMENTS"])
        self.assertEqual(
            narrowed.metadata.custom_metrics["implementation_relationships"],
            {"linked": 0, "external_target": 0},
        )


@unittest.skipUnless(
    fixtures.PROTOBUF and os.environ.get("DEEPTHOUGHT_NEO4J_URI"),
    "Provision isolated Neo4j and scip extra",
)
class ScipImplementationCommandTests(unittest.TestCase):
    def test_index_then_query_implementations_and_supertypes(self):
        from test_incremental import run_module
        from test_scip_graph import ScipCommandTests

        repo = "scip-impl-" + uuid4().hex
        self.addCleanup(lambda: asyncio.run(ScipCommandTests.cleanup(None, repo)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "work"
            shutil.copytree(FIXTURE, root)

            def command(*arguments, ok=True):
                result = run_module("deepthought.graph_cli", *arguments)
                if not ok:
                    self.assertNotEqual(result.returncode, 0)
                    return result.stderr
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            indexed = command(
                "index", str(root), "--tool", "scip", "--repository-id", repo
            )
            scope = ["--repository-id", repo, "--snapshot", indexed["snapshot_id"]]

            def native(name):
                found = command("search", *scope, "--query", name, "--limit", "20")
                return next(
                    item["element"]["native_id"]
                    for item in found["items"]
                    if item["element"]["display_name"] == name
                    and item["element"]["kind"] in {"Class", "Interface"}
                )

            def names(kind, symbol):
                rows = command(kind, *scope, "--symbol", symbol)["items"]
                self.assertTrue(
                    all(
                        (row["evidence_type"], row["rule_id"])
                        == ("resolved", "scip.symbol-implementation")
                        and row["edge_id"]
                        for row in rows
                    )
                )
                return [
                    (row["symbol"]["display_name"], row["symbol"]["start_line"])
                    for row in rows
                ]

            base, dog = native("Base"), native("Dog")
            # Indirect supertypes are recorded too: Animal through Pet.
            self.assertEqual(
                names("supertypes", dog), [("Animal", 1), ("Pet", 2), ("Base", 3)]
            )
            self.assertEqual(
                names("implementations", base), [("Dog", 4), ("Shadow", 9)]
            )
            self.assertEqual(
                names("implementations", base + "greet()."),
                [("greet", 7), ("greet", 9)],
            )
            self.assertEqual(names("implementations", dog), [])
            refused = command(
                "implementations", *scope, "--symbol", "local 0", ok=False
            )
            self.assertIn("file_path document scope", refused)
            tree = command(
                "index",
                str(root),
                "--allow-uncovered",
                "--tool",
                "tree-sitter",
                "--repository-id",
                repo,
            )
            refused = command(
                "implementations",
                "--repository-id",
                repo,
                "--snapshot",
                tree["snapshot_id"],
                "--symbol",
                base,
                ok=False,
            )
            self.assertIn("does not provide implementations", refused)
