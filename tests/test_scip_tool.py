"""SCIP plugin conformance and source-binding regressions; no model calls."""

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deepthought.analysis_tools import ScipAnalysisTool, ToolCapability, tool_registry
from deepthought.scip import ingest_scip
from deepthought.scip_indexing import capture_inputs, index_typescript, validate_index
from deepthought.storage.graph.writer import _prepare

PROTOBUF = (
    importlib.util.find_spec("google") is not None
    and importlib.util.find_spec("google.protobuf") is not None
)


@unittest.skipUnless(PROTOBUF, "Provision the scip extra")
class ScipToolTests(unittest.TestCase):
    def test_real_indexer_alias_gap_remains_explicit_without_name_inference(self):
        root = Path(__file__).parent / "fixtures/scip_alias_reexport"
        output = asyncio.run(ScipAnalysisTool(root).analyze("owned-alias-control"))
        definitions = [
            node.properties
            for node in output.nodes
            if "Symbol" in node.labels
            and node.properties["file_path"] == "a.ts"
            and node.properties.get("display_name") == "alpha"
        ]
        self.assertEqual(len(definitions), 2)
        native = definitions[0]["native_id"]
        references = [
            node.properties for node in output.nodes if "Reference" in node.labels
        ]
        bound = [ref for ref in references if ref["native_id"] == native]
        self.assertEqual(len(bound), 7)
        self.assertTrue(all(ref["resolution"] == "resolved" for ref in bound))
        unresolved_import = next(
            ref
            for ref in references
            if ref["file_path"] == "c.ts"
            and ref["start_line"] == 1
            and ref["column"] == 9
        )
        self.assertEqual(unresolved_import["resolution"], "unresolved")
        self.assertEqual(unresolved_import["target_ids"], [])
        self.assertEqual(output.metadata.custom_metrics["graph_status"], "partial")
        self.assertNotIn("reference_verification", output.metadata.custom_metrics)

    def setUp(self):
        from deepthought._scip import scip_pb2

        self.wire = scip_pb2
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "a.ts").write_text(
            "export function alpha() {}\nalpha();\nlet same = 1; same;\n"
        )
        (self.root / "b.ts").write_text("alpha();\nlet same = 2; same;\nexternal();\n")
        self.index = self.root / "index.scip"
        index = scip_pb2.Index()
        index.metadata.tool_info.name = "scip-typescript"
        index.metadata.tool_info.version = "0.6.0"
        self.symbol = "scip-typescript npm demo 1 a.ts/alpha()."
        for file, definitions, references in [
            (
                "a.ts",
                [(self.symbol, [0, 16, 21]), ("local 0", [2, 4, 8])],
                [(self.symbol, [1, 0, 5]), ("local 0", [2, 14, 18])],
            ),
            (
                "b.ts",
                [("local 0", [1, 4, 8])],
                [
                    (self.symbol, [0, 0, 5]),
                    ("local 0", [1, 14, 18]),
                    ("scip-typescript npm outside 2 x.ts/external().", [2, 0, 8]),
                ],
            ),
        ]:
            document = index.documents.add(relative_path=file, language="typescript")
            for symbol, location in definitions:
                document.occurrences.add(
                    symbol=symbol, range=location, symbol_roles=scip_pb2.Definition
                )
                document.symbols.add(
                    symbol=symbol, kind=scip_pb2.SymbolInformation.Function
                )
            for symbol, location in references:
                document.occurrences.add(symbol=symbol, range=location)
        self.index.write_bytes(index.SerializeToString())
        self.seal()

    def seal(self, **extra):
        receipt = {
            "schema_version": 1,
            "profile": "scip-typescript-v1",
            "files": capture_inputs(self.root, self.index),
            "index_sha256": hashlib.sha256(self.index.read_bytes()).hexdigest(),
            "position_encoding": "utf-16",
            "producer": {"name": "scip-typescript", "version": "0.6.0"},
            **extra,
        }
        Path(str(self.index) + ".sources.json").write_text(json.dumps(receipt))
        return receipt

    def output(self, repo_url="public", **kwargs):
        tool = tool_registry.get_tool("scip", self.root)
        self.assertIsInstance(tool, ScipAnalysisTool)
        return asyncio.run(tool.analyze(repo_url, languages={"typescript"}, **kwargs))

    def test_registered_capabilities_and_sdk_identity_topology(self):
        output = self.output()
        self.assertEqual(output.metadata.custom_metrics["analysis_status"], "succeeded")
        self.assertEqual(
            output.metadata.custom_metrics["resolution_counts"],
            {"resolved": 4, "unresolved": 1, "ambiguous": 0},
        )
        self.assertEqual(output.metadata.custom_metrics["graph_status"], "partial")
        refs = [n for n in output.nodes if "Reference" in n.labels]
        self.assertEqual(len(refs), 5)
        self.assertTrue(
            all(n.properties["coordinate_encoding"] == "utf-16" for n in refs)
        )
        self.assertEqual(
            {r.type for r in output.relationships},
            {"CONTAINS", "DEFINES", "CONTAINS_REFERENCE", "REFERENCES", "DEPENDS_ON"},
        )
        _prepare(output, "public", output.metadata.custom_metrics["snapshot_id"])
        self.assertFalse(
            ScipAnalysisTool(self.root).has_capability(ToolCapability.CALL_GRAPH)
        )
        self.assertTrue(
            ScipAnalysisTool(self.root).has_capability(
                ToolCapability.REFERENCE_INDEXING
            )
        )
        dependency = next(r for r in output.relationships if r.type == "DEPENDS_ON")
        self.assertEqual(len(dependency.properties["witness_ids"]), 1)
        self.assertIn("b.ts", dependency.source_match["qualified_name"])
        self.assertIn("a.ts", dependency.target_match["qualified_name"])

    def test_local_symbols_are_scoped_and_external_refs_preserved(self):
        output = self.output()
        refs = [
            n
            for n in output.nodes
            if "Reference" in n.labels and n.properties["native_id"] == "local 0"
        ]
        self.assertEqual(len(refs), 2)
        self.assertNotEqual(
            refs[0].properties["target_ids"], refs[1].properties["target_ids"]
        )
        external = [
            n
            for n in output.nodes
            if "Reference" in n.labels and n.properties["resolution"] == "unresolved"
        ]
        self.assertEqual(len(external), 1)
        self.assertEqual(external[0].properties["target_ids"], [])

    def test_source_edit_added_file_dependency_and_index_mutation_rejected(self):
        source = self.root / "a.ts"
        original = source.read_bytes()
        source.write_bytes(original + b"// edit\n")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.output()
        source.write_bytes(original)
        extra = self.root / "new.ts"
        extra.write_text("export const extra = 1;")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.output()
        extra.unlink()
        dependencies = self.root / "node_modules/pkg"
        dependencies.mkdir(parents=True)
        (dependencies / "index.d.ts").write_text("export const typed: number;")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.output()
        (dependencies / "index.d.ts").unlink()
        self.index.write_bytes(self.index.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "differs"):
            self.output()

    def test_index_digest_is_not_used_as_source_hash_and_strict_sdk_remains(self):
        strict = ingest_scip(self.index, repository_id="public")
        self.assertEqual(strict.status, "partial")
        self.assertTrue(
            any(d.code == "position_encoding_unspecified" for d in strict.diagnostics)
        )
        explicit = ingest_scip(
            self.index, repository_id="public", default_position_encoding="utf-16"
        )
        self.assertEqual(explicit.status, "succeeded")
        self.assertEqual(
            dict(explicit.analyzer_versions)["unspecified_position_encoding_fallback"],
            "utf-16",
        )
        with self.assertRaises(ValueError):
            ingest_scip(
                self.index, repository_id="public", default_position_encoding="guessed"
            )

    def test_filters_and_generation_timing_do_not_change_semantic_identity(self):
        first = self.output()
        self.seal(wall_seconds=123.5)
        second = self.output()
        self.assertEqual(
            first.metadata.custom_metrics["snapshot_id"],
            second.metadata.custom_metrics["snapshot_id"],
        )
        selected = self.output(file_patterns=["b.ts"])
        self.assertEqual(selected.metadata.files_processed, 1)
        self.assertNotEqual(
            first.metadata.custom_metrics["snapshot_id"],
            selected.metadata.custom_metrics["snapshot_id"],
        )
        self.assertEqual(
            selected.metadata.custom_metrics["resolution_counts"]["resolved"], 1
        )
        self.assertEqual(
            asyncio.run(ScipAnalysisTool(self.root).get_symbol_definitions()), {}
        )

    def test_explicit_encoding_keeps_non_bmp_source_name(self):
        original_lines = (self.root / "a.ts").read_text().splitlines()
        (self.root / "a.ts").write_text(
            'const text = "😀"; alpha();\n' + "\n".join(original_lines[1:]) + "\n"
        )
        parsed = self.wire.Index.FromString(self.index.read_bytes())
        parsed.documents[0].occurrences[0].range[:] = [0, 19, 24]
        # UTF-16 unit offsets count the emoji as two units. Validate exact token.
        line = (self.root / "a.ts").read_text().splitlines()[0]
        start = len(line[: line.index("alpha")].encode("utf-16-le")) // 2
        parsed.documents[0].occurrences[0].range[:] = [0, start, start + 5]
        self.index.write_bytes(parsed.SerializeToString())
        self.seal()
        output = self.output()
        definition = next(
            n
            for n in output.nodes
            if n.properties.get("native_id") == self.symbol and "Symbol" in n.labels
        )
        self.assertEqual(definition.properties["display_name"], "alpha")

    def test_unavailable_dependency_and_unsupported_languages(self):
        tool = ScipAnalysisTool(self.root, index_path=self.root / "missing.scip")
        self.assertFalse(asyncio.run(tool.check_availability()))
        with self.assertRaisesRegex(ValueError, "TypeScript"):
            asyncio.run(tool.analyze("public", languages={"go"}))
        with self.assertRaisesRegex(ValueError, "no indexed"):
            self.output(file_patterns=["missing.ts"])

    def test_indexer_failure_preserves_existing_artifact(self):
        before = self.index.read_bytes()
        with patch("deepthought.scip_indexing.subprocess.run") as run:
            run.return_value.returncode = 1
            run.return_value.stdout = "index failed"
            run.return_value.stderr = ""
            with self.assertRaisesRegex(ValueError, "indexer failed"):
                index_typescript(self.root, node=__file__, indexer=__file__)
        self.assertEqual(self.index.read_bytes(), before)
        validate_index(self.root, self.index)


if __name__ == "__main__":
    unittest.main()
