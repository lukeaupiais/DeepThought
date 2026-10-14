"""Reference facts through the public SCIP SDK; expected roles/ranges are literal."""

from pathlib import Path
import tempfile
import unittest
import json

from deepthought import AnalysisResult, ingest_scip
from deepthought._scip import scip_pb2 as wire


class ReferenceTests(unittest.TestCase):
    def test_tied_occurrences_keep_role_identity_when_index_order_changes(self):
        observations = []
        for roles in ((0, wire.WriteAccess), (wire.WriteAccess, 0)):
            index = wire.Index()
            index.metadata.tool_info.name = "fixture-indexer"
            doc = index.documents.add(
                relative_path="main.py",
                position_encoding=wire.UTF8CodeUnitOffsetFromLineStart,
            )
            for role in roles:
                doc.occurrences.add(
                    symbol="local 0", symbol_roles=role, range=[0, 0, 3]
                )
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "index.scip"
                path.write_bytes(index.SerializeToString())
                result = ingest_scip(path, repository_id="fixture")
            observations.append({r.symbol_roles: r.id for r in result.references})
        self.assertEqual(observations[0], observations[1])

    def test_reference_artifact_cannot_claim_facts_without_file_coverage(self):
        index = wire.Index()
        index.metadata.tool_info.name = "fixture-indexer"
        doc = index.documents.add(
            relative_path="main.py",
            position_encoding=wire.UTF8CodeUnitOffsetFromLineStart,
        )
        doc.occurrences.add(symbol="local 0", range=[0, 0, 3])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.scip"
            path.write_bytes(index.SerializeToString())
            data = ingest_scip(path, repository_id="fixture").to_dict()
        data["coverage"] = []
        with self.assertRaises(ValueError):
            AnalysisResult.from_json(json.dumps(data))

    def test_malformed_reference_is_disclosed_and_valid_facts_are_retained(self):
        index = wire.Index()
        index.metadata.tool_info.name = "fixture-indexer"
        doc = index.documents.add(
            relative_path="main.py",
            position_encoding=wire.UTF8CodeUnitOffsetFromLineStart,
        )
        doc.occurrences.add(symbol="local 0", range=[0, 0, 3])
        doc.occurrences.add(symbol="local 1", range=[1, 6, 2])
        doc.occurrences.add(symbol="", range=[2, 0, 2])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.scip"
            path.write_bytes(index.SerializeToString())
            result = ingest_scip(path, repository_id="fixture")
        self.assertEqual(len(result.references), 1)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.coverage[0].status, "partial")
        self.assertEqual(
            [d.code for d in result.diagnostics], ["invalid_reference_range"]
        )

    def test_references_retain_native_symbols_roles_and_encoding_without_call_inference(
        self,
    ):
        index = wire.Index()
        index.metadata.tool_info.name = "fixture-indexer"
        doc = index.documents.add(
            relative_path="main.ts",
            position_encoding=wire.UTF16CodeUnitOffsetFromLineStart,
        )
        doc.occurrences.add(
            symbol="scip-typescript npm fixture 1 Widget#",
            range=[2, 4, 10],
            symbol_roles=wire.ReadAccess | wire.Import,
        )
        doc.occurrences.add(
            symbol="local 0", range=[3, 1, 5], symbol_roles=wire.WriteAccess
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.scip"
            path.write_bytes(index.SerializeToString())
            result = ingest_scip(path, repository_id="fixture")
        self.assertEqual(result.status, "succeeded")
        self.assertFalse(result.definitions)
        self.assertEqual(result.capabilities, ("definitions", "references"))
        self.assertEqual(
            [
                (
                    r.native_id,
                    r.symbol_roles,
                    r.line,
                    r.column,
                    r.end_line,
                    r.end_column,
                    r.coordinate_encoding,
                )
                for r in result.references
            ],
            [
                ("scip-typescript npm fixture 1 Widget#", 10, 3, 4, 3, 10, "utf-16"),
                ("local 0", 4, 4, 1, 4, 5, "utf-16"),
            ],
        )
        self.assertEqual(AnalysisResult.from_json(result.to_json()), result)


if __name__ == "__main__":
    unittest.main()
