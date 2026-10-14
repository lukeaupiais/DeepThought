"""Public ingestion behavior over supplied SCIP bytes, without running indexers.

Captured fixtures use our owned Python/TypeScript sources, indexed by
scip-python 0.6.6 and scip-typescript 0.4.0. Only metadata project_root and
tool arguments were sanitized; document symbols and occurrences are unchanged.
"""

from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

from deepthought import ingest_scip
from deepthought._scip import scip_pb2 as wire


class ScipTests(unittest.TestCase):
    def test_native_symbol_identity_survives_source_line_insertion(self):
        index = wire.Index()
        index.metadata.tool_info.name = "fixture-indexer"
        doc = index.documents.add(
            relative_path="main.py",
            position_encoding=wire.UTF8CodeUnitOffsetFromLineStart,
        )
        doc.symbols.add(
            symbol="scip-python python fixture 1 `main`/helper().",
            display_name="helper",
            kind=wire.SymbolInformation.Function,
        )
        occurrence = doc.occurrences.add(
            symbol=doc.symbols[0].symbol, symbol_roles=wire.Definition, range=[0, 4, 10]
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.scip"
            path.write_bytes(index.SerializeToString())
            before = ingest_scip(path, repository_id="fixture")
            occurrence.range[:] = [1, 4, 10]
            path.write_bytes(index.SerializeToString())
            after = ingest_scip(path, repository_id="fixture")
        self.assertEqual(before.definitions[0].id, after.definitions[0].id)
        self.assertEqual(after.definitions[0].line, 2)
        self.assertNotEqual(before.snapshot_id, after.snapshot_id)

    def test_rejected_document_changes_snapshot_completeness(self):
        index = wire.Index()
        index.metadata.tool_info.name = "fixture-indexer"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.scip"
            path.write_bytes(index.SerializeToString())
            complete = ingest_scip(path, repository_id="fixture")
            index.documents.add(relative_path="../outside.py")
            path.write_bytes(index.SerializeToString())
            rejected = ingest_scip(path, repository_id="fixture")
        self.assertEqual(complete.status, "succeeded")
        self.assertEqual(rejected.status, "failed")
        self.assertNotEqual(complete.snapshot_id, rejected.snapshot_id)

    def test_invalid_path_prefix_is_rejected_before_reading_an_artifact(self):
        for prefix in (
            "/absolute",
            "../outside",
            "src/../outside",
            "src/",
            ".",
            "src\\package",
        ):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                ingest_scip("missing.scip", repository_id="fixture", path_prefix=prefix)

    def test_captured_indexer_artifacts_preserve_helpers_and_disclose_legacy_encoding(
        self,
    ):
        fixtures = Path(__file__).parent / "fixtures/scip"
        for language, extension, column in [
            ("python", "py", 4),
            ("typescript", "ts", 16),
        ]:
            with self.subTest(language=language):
                result = ingest_scip(
                    fixtures / f"{language}.scip", repository_id="fixture"
                )
                helpers = [
                    d for d in result.definitions if d.native_id.endswith("/helper().")
                ]
                self.assertEqual(
                    {(d.file, d.line, d.column) for d in helpers},
                    {
                        (f"lib/helpers.{extension}", 1, column),
                        (f"other/helpers.{extension}", 1, column),
                    },
                )
                self.assertEqual(len({d.id for d in helpers}), 2)
                self.assertEqual(result.status, "partial")
                self.assertEqual(
                    {d.code for d in result.diagnostics},
                    {"position_encoding_unspecified"},
                )

    def test_target_directory_can_be_mapped_without_reading_index_metadata_paths(self):
        index = wire.Index()
        index.metadata.tool_info.name = "fixture-indexer"
        index.metadata.project_root = "file:///untrusted/private/root"
        doc = index.documents.add(
            relative_path="helpers.py",
            position_encoding=wire.UTF8CodeUnitOffsetFromLineStart,
        )
        doc.symbols.add(
            symbol="local 1",
            display_name="helper",
            kind=wire.SymbolInformation.Function,
        )
        doc.occurrences.add(
            symbol="local 1", symbol_roles=wire.Definition, range=[0, 4, 10]
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.scip"
            path.write_bytes(index.SerializeToString())
            result = ingest_scip(
                path, repository_id="fixture", path_prefix="src/package"
            )
        self.assertEqual(result.definitions[0].file, "src/package/helpers.py")
        self.assertEqual(result.coverage[0].file, "src/package/helpers.py")
        self.assertNotIn("untrusted", result.to_json())

    def test_duplicate_basenames_preserve_native_identities_and_locations(self):
        index = wire.Index()
        index.metadata.tool_info.name = "fixture-indexer"
        index.metadata.tool_info.version = "1"
        for file, native in [
            ("lib/helpers.py", "scip-python python fixture 1 `lib.helpers`/helper()."),
            (
                "other/helpers.py",
                "scip-python python fixture 1 `other.helpers`/helper().",
            ),
        ]:
            doc = index.documents.add(
                relative_path=file,
                language="python",
                position_encoding=wire.UTF8CodeUnitOffsetFromLineStart,
            )
            doc.symbols.add(
                symbol=native,
                display_name="helper",
                kind=wire.SymbolInformation.Function,
            )
            doc.occurrences.add(
                symbol=native, symbol_roles=wire.Definition, range=[0, 4, 10]
            )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.scip"
            path.write_bytes(index.SerializeToString())
            result = ingest_scip(path, repository_id="fixture")
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(
            {d.file for d in result.definitions}, {"lib/helpers.py", "other/helpers.py"}
        )
        self.assertEqual(len({d.id for d in result.definitions}), 2)
        self.assertTrue(
            all(
                (d.line, d.column, d.kind) == (1, 4, "Function")
                for d in result.definitions
            )
        )
        self.assertEqual(
            {d.native_id for d in result.definitions},
            {doc.symbols[0].symbol for doc in index.documents},
        )

    def test_cli_ingests_without_running_indexers(self):
        index = wire.Index()
        index.metadata.tool_info.name = "fixture-indexer"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "empty.scip"
            path.write_bytes(index.SerializeToString())
            run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "deepthought",
                    "ingest-scip",
                    str(path),
                    "--repository-id",
                    "fixture",
                ],
                capture_output=True,
                text=True,
            )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["analyzer"], "scip")

    def test_reference_to_class_is_not_a_definition_or_instantiation(self):
        index = wire.Index()
        index.metadata.tool_info.name = "fixture-indexer"
        doc = index.documents.add(
            relative_path="main.py",
            position_encoding=wire.UTF8CodeUnitOffsetFromLineStart,
        )
        doc.occurrences.add(
            symbol="scip-python python fixture 1 `models`/Widget#", range=[0, 18, 24]
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.scip"
            path.write_bytes(index.SerializeToString())
            result = ingest_scip(path, repository_id="fixture")
        self.assertFalse(result.definitions)
        self.assertEqual(result.capabilities, ("definitions",))

    def test_malformed_artifact_is_failed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "broken.scip"
            path.write_bytes(b"not a protobuf")
            result = ingest_scip(path, repository_id="fixture")
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.diagnostics)

    def test_escaping_document_is_rejected(self):
        index = wire.Index()
        index.metadata.tool_info.name = "fixture-indexer"
        doc = index.documents.add(relative_path="../outside.py")
        doc.occurrences.add(
            symbol="local 1", symbol_roles=wire.Definition, range=[0, 0, 4]
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "escape.scip"
            path.write_bytes(index.SerializeToString())
            result = ingest_scip(path, repository_id="fixture")
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.definitions)
        self.assertIn("invalid_document_path", {d.code for d in result.diagnostics})

    def test_native_coordinate_encoding_is_preserved(self):
        index = wire.Index()
        index.metadata.tool_info.name = "fixture-indexer"
        doc = index.documents.add(
            relative_path="unicode.ts",
            position_encoding=wire.UTF16CodeUnitOffsetFromLineStart,
        )
        doc.symbols.add(
            symbol="local 1",
            display_name="afterEmoji",
            kind=wire.SymbolInformation.Function,
        )
        doc.occurrences.add(
            symbol="local 1", symbol_roles=wire.Definition, range=[0, 4, 14]
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.scip"
            path.write_bytes(index.SerializeToString())
            result = ingest_scip(path, repository_id="fixture")
        self.assertEqual(
            (result.definitions[0].coordinate_encoding, result.definitions[0].column),
            ("utf-16", 4),
        )

    def test_invalid_or_unspecified_ranges_never_report_complete_success(self):
        for location, encoding in [
            ([0, 9, 4], wire.UTF8CodeUnitOffsetFromLineStart),
            ([0, 0, 4], 0),
        ]:
            with (
                self.subTest(location=location, encoding=encoding),
                tempfile.TemporaryDirectory() as folder,
            ):
                index = wire.Index()
                index.metadata.tool_info.name = "fixture-indexer"
                doc = index.documents.add(
                    relative_path="main.py", position_encoding=encoding
                )
                doc.occurrences.add(
                    symbol="local 1", symbol_roles=wire.Definition, range=location
                )
                path = Path(folder) / "fixture.scip"
                path.write_bytes(index.SerializeToString())
                self.assertNotEqual(
                    ingest_scip(path, repository_id="fixture").status, "succeeded"
                )

    def test_ingestion_coexists_with_another_scip_binding(self):
        index = wire.Index()
        index.metadata.tool_info.name = "fixture-indexer"
        script = """
import sys
from google.protobuf import descriptor_pb2, descriptor_pool
file=descriptor_pb2.FileDescriptorProto(name='other-scip.proto', package='scip', syntax='proto3')
file.message_type.add(name='Index')
descriptor_pool.Default().AddSerializedFile(file.SerializeToString())
from deepthought import ingest_scip
result=ingest_scip(sys.argv[1], repository_id='fixture')
assert result.status == 'succeeded', result.diagnostics
"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "empty.scip"
            path.write_bytes(index.SerializeToString())
            run = subprocess.run(
                [sys.executable, "-c", script, str(path)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(run.returncode, 0, run.stderr)
