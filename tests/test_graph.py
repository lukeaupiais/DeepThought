"""Graph SDK acceptance using literal SCIP native symbols and located evidence."""

from pathlib import Path
import tempfile
import unittest
import json
import subprocess
import sys

from deepthought import AnalysisResult, GraphSnapshot, analyze_repository

import deepthought
from deepthought._scip import scip_pb2 as wire


FIRST = "scip-python python fixture 1 `lib.first`/helper()."
SECOND = "scip-python python fixture 1 `lib.second`/helper()."


def result_for(repository_id, documents):
    index = wire.Index()
    index.metadata.tool_info.name = "fixture-indexer"
    index.metadata.tool_info.version = "1"
    for file, definitions, references in documents:
        doc = index.documents.add(
            relative_path=file, position_encoding=wire.UTF8CodeUnitOffsetFromLineStart
        )
        for native, display in definitions:
            doc.symbols.add(
                symbol=native,
                display_name=display,
                kind=wire.SymbolInformation.Function,
            )
            doc.occurrences.add(
                symbol=native, symbol_roles=wire.Definition, range=[0, 4, 10]
            )
        for line, native, roles in references:
            doc.occurrences.add(symbol=native, symbol_roles=roles, range=[line, 0, 6])
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "index.scip"
        path.write_bytes(index.SerializeToString())
        return deepthought.ingest_scip(path, repository_id=repository_id)


class GraphTests(unittest.TestCase):
    def test_import_cannot_hide_incomplete_coverage_behind_success_status(self):
        source = result_for("fixture", [("lib.py", [(FIRST, "helper")], [])])
        for field in ("coverage", "diagnostics"):
            data = source.to_dict()
            if field == "coverage":
                data["coverage"][0]["status"] = "partial"
            else:
                data["diagnostics"] = [
                    {
                        "code": "incomplete",
                        "message": "Indexing failed",
                        "file": None,
                        "line": None,
                    }
                ]
            with self.subTest(field=field), self.assertRaises(ValueError):
                AnalysisResult.from_json(json.dumps(data))

    def test_saved_graph_uses_portable_composer_contract_instead_of_package_version(
        self,
    ):
        saved = Path(__file__).parent / "fixtures/graph/empty.json"
        graph = GraphSnapshot.from_json(saved.read_text())
        self.assertEqual(graph.to_dict(), json.loads(saved.read_text()))
        self.assertEqual(graph.to_dict()["composer_version"], "1")
        self.assertEqual(graph.status, "succeeded")

    def test_malformed_schema_values_raise_value_error_and_cli_invalid_input(self):
        for schema in ([], {}, None, 2):
            data = {"schema_version": schema}
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                AnalysisResult.from_json(json.dumps(data))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "invalid.json"
                path.write_text(json.dumps(data))
                run = subprocess.run(
                    [sys.executable, "-m", "deepthought", "compose", str(path)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(run.returncode, 2, run.stderr)
                self.assertNotIn("Traceback", run.stderr)

    def test_captured_indexer_aliases_target_the_correct_helper_without_call_inference(
        self,
    ):
        fixtures = Path(__file__).parent / "fixtures/scip"
        for language, extension, first_lines, second_lines in (
            ("python", "py", [2, 15], [3, 15]),
            ("typescript", "ts", [2, 14], [3, 3, 14]),
        ):
            with self.subTest(language=language):
                source = deepthought.ingest_scip(
                    fixtures / f"{language}.scip", repository_id=language
                )
                graph = deepthought.compose_graph([source])
                helpers = [
                    node
                    for node in graph.definitions
                    if node.definition.native_id.endswith("/helper().")
                ]
                for file, lines in (
                    (f"lib/helpers.{extension}", first_lines),
                    (f"other/helpers.{extension}", second_lines),
                ):
                    helper = next(
                        node for node in helpers if node.definition.file == file
                    )
                    refs = [
                        ref
                        for ref in graph.references_to(helper.id)
                        if ref.reference.file == f"main.{extension}"
                    ]
                    self.assertEqual(sorted(ref.reference.line for ref in refs), lines)
                    self.assertTrue(all(ref.resolution == "resolved" for ref in refs))
                widgets = [
                    node
                    for node in graph.definitions
                    if node.definition.native_id.endswith("/Widget#")
                ]
                self.assertEqual(len(widgets), 1)
                self.assertGreaterEqual(len(graph.references_to(widgets[0].id)), 5)
                self.assertEqual(
                    {edge.relation for edge in graph.edges}, {"references"}
                )
                self.assertEqual(
                    graph.status, "partial"
                )  # Captured indexers omit encoding.

    def test_cli_rejects_malformed_and_oversized_artifacts_and_reports_partial_graphs(
        self,
    ):
        source = result_for("fixture", [("main.py", [], [(0, FIRST, 0)])])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            for content, args, code in (
                (source.to_json(), [], 1),
                (source.to_json(), ["--max-artifact-bytes", "8"], 2),
                (source.to_json(), ["--max-total-bytes", "8"], 2),
                ("{}", [], 2),
            ):
                path.write_text(content)
                run = subprocess.run(
                    [sys.executable, "-m", "deepthought", "compose", str(path), *args],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(run.returncode, code, run.stderr)
                if code == 1:
                    self.assertEqual(json.loads(run.stdout)["status"], "partial")
                else:
                    self.assertFalse(run.stdout)

    def test_cli_composes_the_same_portable_graph_as_sdk(self):
        source = result_for(
            "fixture",
            [("lib.py", [(FIRST, "helper")], []), ("main.py", [], [(0, FIRST, 0)])],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            path.write_text(source.to_json())
            run = subprocess.run(
                [sys.executable, "-m", "deepthought", "compose", str(path)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            json.loads(run.stdout), deepthought.compose_graph([source]).to_dict()
        )

    def test_local_symbols_never_cross_document_or_repository_boundaries(self):
        first = result_for(
            "first",
            [
                ("one.py", [("local 0", "value")], [(1, "local 0", 0)]),
                ("two.py", [], [(0, "local 0", 0)]),
            ],
        )
        second = result_for("second", [("one.py", [], [(0, "local 0", 0)])])
        graph = deepthought.compose_graph([first, second])
        self.assertEqual(
            graph.resolution_counts, {"resolved": 1, "unresolved": 2, "ambiguous": 0}
        )
        self.assertEqual(graph.status, "partial")
        self.assertFalse(graph.edges)
        self.assertEqual(len(graph.references_to(graph.definitions[0].id)), 1)

    def test_global_symbols_resolve_across_repositories_but_ambiguous_targets_do_not_make_edges(
        self,
    ):
        client = result_for("client", [("main.py", [], [(0, FIRST, 0)])])
        provider = result_for("provider", [("helpers.py", [(FIRST, "helper")], [])])
        graph = deepthought.compose_graph([client, provider])
        self.assertEqual(graph.resolution_counts["resolved"], 1)
        self.assertEqual(graph.edges[0].target_repository_id, "provider")
        duplicate = result_for("duplicate", [("other.py", [(FIRST, "helper")], [])])
        graph = deepthought.compose_graph([client, provider, duplicate])
        self.assertEqual(graph.resolution_counts["ambiguous"], 1)
        self.assertEqual(len(graph.references[0].target_ids), 2)
        self.assertFalse(graph.edges)
        self.assertEqual(graph.status, "partial")

    def test_unresolved_type_reference_never_becomes_a_call_or_instantiation(self):
        source = result_for(
            "fixture",
            [
                (
                    "main.py",
                    [],
                    [(0, "scip-python python dependency 1 Widget#", wire.ReadAccess)],
                )
            ],
        )
        graph = deepthought.compose_graph([source])
        self.assertEqual(graph.references[0].resolution, "unresolved")
        self.assertFalse(graph.references[0].target_ids)
        self.assertFalse(graph.edges)

    def test_composition_is_order_independent_and_rejects_multiple_snapshots_per_repository(
        self,
    ):
        client = result_for("client", [("main.py", [], [(0, FIRST, 0)])])
        provider = result_for("provider", [("helpers.py", [(FIRST, "helper")], [])])
        first = deepthought.compose_graph([client, provider])
        second = deepthought.compose_graph([provider, client])
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(GraphSnapshot.from_json(first.to_json()), first)
        for sources in ([], [client, client]):
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                deepthought.compose_graph(sources)

    def test_graph_import_rejects_forged_edges_and_resolution(self):
        source = result_for("fixture", [("main.py", [], [(0, FIRST, 0)])])
        graph = deepthought.compose_graph([source])
        for field in ("resolution", "target_ids", "snapshot_id", "status", "edges"):
            data = graph.to_dict()
            if field == "resolution":
                data["references"][0][field] = "resolved"
            elif field == "target_ids":
                data["references"][0][field] = ["invented"]
            elif field == "snapshot_id":
                data[field] = "invented"
            elif field == "status":
                data[field] = "succeeded"
            else:
                data[field] = [{"relation": "calls"}]
            with self.subTest(field=field), self.assertRaises(ValueError):
                GraphSnapshot.from_json(json.dumps(data))

    def test_syntax_only_and_legacy_sources_do_not_claim_reference_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "source.py").write_text("def helper(): pass\n")
            source = analyze_repository(
                directory, repository_id="syntax", languages=("python",)
            )
        legacy = source.to_dict()
        legacy["schema_version"] = "1"
        del legacy["references"]
        restored = AnalysisResult.from_json(json.dumps(legacy))
        graph = deepthought.compose_graph([restored])
        self.assertEqual(len(graph.lookup_definitions("helper")), 1)
        self.assertEqual(graph.status, "partial")
        self.assertFalse(graph.references)
        self.assertEqual(graph.sources[0].capabilities, ("definitions",))

    def test_exact_native_symbols_resolve_same_named_definitions_with_witnesses(self):
        source = result_for(
            "fixture",
            [
                ("lib/first.py", [(FIRST, "helper")], []),
                ("lib/second.py", [(SECOND, "helper")], []),
                (
                    "main.py",
                    [],
                    [(0, SECOND, wire.Import), (1, SECOND, wire.ReadAccess)],
                ),
            ],
        )
        graph = deepthought.compose_graph([source])
        self.assertEqual(graph.status, "succeeded")
        self.assertEqual(len(graph.lookup_definitions("helper")), 2)
        target = graph.lookup_definitions(SECOND)[0]
        self.assertEqual(target.definition.file, "lib/second.py")
        refs = graph.references_to(target.id)
        self.assertEqual([r.resolution for r in refs], ["resolved", "resolved"])
        self.assertTrue(all(r.target_ids == (target.id,) for r in refs))
        edges = graph.dependencies("fixture", "main.py")
        self.assertEqual(len(edges), 1)
        self.assertEqual(
            (edges[0].target_repository_id, edges[0].target_file, edges[0].relation),
            ("fixture", "lib/second.py", "references"),
        )
        self.assertEqual(set(edges[0].witness_ids), {r.id for r in refs})
        self.assertEqual({r.source_snapshot_id for r in refs}, {source.snapshot_id})
