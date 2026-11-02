"""Installed query contract: honest coverage, provenance and bounded output."""

from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from deepthought import (
    AnalysisResult,
    QueryRequest,
    analyze_repository,
    compose_graph,
    ingest_scip,
    query_graph,
)
from deepthought.models import Definition, Diagnostic, FileCoverage
from test_graph import FIRST, SECOND, result_for


class QueryTests(unittest.TestCase):
    def graph(self):
        return compose_graph(
            [
                result_for(
                    "repo",
                    [
                        ("one.py", [(FIRST, "helper")], []),
                        ("two.py", [(SECOND, "helper")], []),
                        ("main.py", [], [(1, FIRST, 0), (2, SECOND, 0)]),
                    ],
                )
            ]
        )

    def test_definitions_keep_graph_identity_native_symbol_and_source_ranges(self):
        graph = self.graph()
        result = query_graph(graph, QueryRequest("definitions", "repo", symbol=FIRST))
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.total_matches, 1)
        row = result.matches[0]
        node = next(
            node for node in graph.definitions if node.definition.native_id == FIRST
        )
        self.assertEqual(row["id"], node.id)
        self.assertEqual(row["source_snapshot_id"], graph.sources[0].snapshot_id)
        self.assertEqual(row["definition"]["file"], "one.py")
        self.assertEqual(row["definition"]["native_id"], FIRST)
        self.assertEqual(row["definition"]["coordinate_encoding"], "utf-8")
        self.assertEqual(result.to_dict()["artifact_type"], "query")
        self.assertEqual(result.to_dict()["schema_version"], "1")

    def test_references_preserve_exact_targets_and_dependencies_keep_witnesses(self):
        graph = self.graph()
        refs = query_graph(graph, QueryRequest("references", "repo", symbol=FIRST))
        self.assertEqual(refs.total_matches, 1)
        self.assertEqual(refs.matches[0]["reference"]["line"], 2)
        self.assertEqual(refs.matches[0]["resolution"], "resolved")
        target = next(
            node.id for node in graph.definitions if node.definition.native_id == FIRST
        )
        self.assertEqual(refs.matches[0]["target_ids"], (target,))
        deps = query_graph(graph, QueryRequest("dependencies", "repo", file="main.py"))
        self.assertEqual(deps.total_matches, 2)
        self.assertTrue(all(row["relation"] == "references" for row in deps.matches))
        self.assertTrue(all(row["witness_ids"] for row in deps.matches))
        self.assertEqual(deps.snapshot_id, graph.sources[0].snapshot_id)
        self.assertEqual(deps.graph_snapshot_id, graph.snapshot_id)

    def test_unresolved_native_symbol_is_visible_and_stays_partial(self):
        source = result_for("repo", [("main.py", [], [(1, FIRST, 0)])])
        result = query_graph(
            compose_graph([source]), QueryRequest("references", "repo", symbol=FIRST)
        )
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.matches[0]["resolution"], "unresolved")
        self.assertEqual(result.matches[0]["target_ids"], ())
        self.assertEqual(
            result.resolution_counts, {"resolved": 0, "unresolved": 1, "ambiguous": 0}
        )

    def test_ambiguous_targets_and_dependency_witnesses_have_explicit_detail_caps(self):
        source = result_for(
            "repo",
            [
                *[(f"lib{i}.py", [(FIRST, "helper")], []) for i in range(25)],
                ("main.py", [], [(0, FIRST, 0)]),
            ],
        )
        result = query_graph(
            compose_graph([source]), QueryRequest("references", "repo", symbol=FIRST)
        )
        row = result.matches[0]
        self.assertEqual((result.status, row["resolution"]), ("partial", "ambiguous"))
        self.assertEqual(
            (row["target_count"], len(row["target_ids"]), row["targets_truncated"]),
            (25, 20, True),
        )
        source = result_for(
            "repo",
            [
                ("lib.py", [(FIRST, "helper")], []),
                ("main.py", [], [(i, FIRST, 0) for i in range(25)]),
            ],
        )
        result = query_graph(
            compose_graph([source]),
            QueryRequest("dependencies", "repo", file="main.py"),
        )
        row = result.matches[0]
        self.assertEqual(
            (row["witness_count"], len(row["witness_ids"]), row["witnesses_truncated"]),
            (25, 20, True),
        )

    def test_result_limits_return_a_deterministic_prefix_and_exact_count(self):
        graph = self.graph()
        full = query_graph(graph, QueryRequest("definitions", "repo"))
        limited = query_graph(graph, QueryRequest("definitions", "repo", limit=1))
        self.assertEqual(limited.total_matches, 2)
        self.assertEqual(limited.matches, full.matches[:1])
        self.assertTrue(limited.truncated)
        self.assertFalse(full.truncated)
        self.assertEqual(
            limited.to_json(),
            query_graph(graph, QueryRequest("definitions", "repo", limit=1)).to_json(),
        )

    def test_repository_scope_filters_origins_without_losing_cross_repository_targets(
        self,
    ):
        a = result_for("a", [("lib.py", [(FIRST, "helper")], [])])
        b = result_for("b", [("main.py", [], [(1, FIRST, 0)])])
        graph = compose_graph([a, b])
        result = query_graph(graph, QueryRequest("references", "b", symbol=FIRST))
        self.assertEqual(result.total_matches, 1)
        self.assertEqual(result.matches[0]["repository_id"], "b")
        self.assertEqual(result.matches[0]["target_ids"], (graph.definitions[0].id,))
        self.assertEqual(
            query_graph(graph, QueryRequest("definitions", "b")).total_matches, 0
        )
        with self.assertRaises(ValueError):
            query_graph(graph, QueryRequest("definitions", "missing"))
        self.assertEqual(
            result.to_json(),
            query_graph(
                compose_graph([b, a]), QueryRequest("references", "b", symbol=FIRST)
            ).to_json(),
        )

    def test_syntax_definitions_succeed_without_fabricating_reference_availability(
        self,
    ):
        source = analyze_repository(
            Path(__file__).parent / "fixtures/python",
            repository_id="repo",
            languages=("python",),
        )
        graph = compose_graph([source])
        definitions = query_graph(graph, QueryRequest("definitions", "repo"))
        self.assertEqual(definitions.status, "succeeded")
        self.assertEqual(definitions.graph_status, "partial")
        self.assertTrue(definitions.matches)
        for request in [
            QueryRequest("references", "repo", symbol="helper"),
            QueryRequest("dependencies", "repo", file="main.py"),
        ]:
            result = query_graph(graph, request)
            self.assertEqual(result.status, "unavailable")
            self.assertEqual((result.matches, result.total_matches), ((), 0))

    def test_failed_cancelled_and_partial_analysis_never_become_success(self):
        source = self.graph().sources[0]
        for status in ["failed", "cancelled", "partial"]:
            changed = replace(source, status=status)
            result = query_graph(
                compose_graph([changed]), QueryRequest("definitions", "repo")
            )
            self.assertEqual((result.status, result.analysis_status), (status, status))
            self.assertTrue(result.matches)

    def sized_graph(self, second_symbol="small"):
        definitions = (
            Definition("a", "a.py", "small", "Function", 1, 0, 1, 5),
            Definition("b", "b.py", second_symbol, "Function", 1, 0, 1, 5),
        )
        return compose_graph(
            [
                AnalysisResult(
                    "repo",
                    "s" * 64,
                    "succeeded",
                    definitions,
                    (
                        FileCoverage("a.py", "python", "analyzed"),
                        FileCoverage("b.py", "python", "analyzed"),
                    ),
                    (),
                    "fixture",
                    (),
                )
            ]
        )

    def test_byte_budget_counts_utf8_and_keeps_a_prefix_without_losing_total(self):
        graph = self.sized_graph("界" * 1000)
        result = query_graph(
            graph, QueryRequest("definitions", "repo", max_output_bytes=2048)
        )
        self.assertEqual(result.total_matches, 2)
        self.assertEqual(len(result.matches), 1)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.to_json().encode("utf-8")) + 1, 2048)
        result.matches[0]["definition"]["symbol"] = "界" * 20_000
        with self.assertRaisesRegex(ValueError, "max_output_bytes"):
            result.to_json()

    def test_an_oversized_first_match_or_metadata_is_explicitly_rejected(self):
        graph = self.sized_graph()
        source = graph.sources[0]
        source = replace(
            source, definitions=(replace(source.definitions[0], symbol="界" * 2000),)
        )
        with self.assertRaisesRegex(ValueError, "First query match"):
            query_graph(
                compose_graph([source]),
                QueryRequest("definitions", "repo", max_output_bytes=2048),
            )
        source = replace(
            source, status="partial", diagnostics=(Diagnostic("fixture", "x" * 40_000),)
        )
        with self.assertRaisesRegex(ValueError, "response exceeds"):
            query_graph(compose_graph([source]), QueryRequest("definitions", "repo"))

    def test_diagnostic_limits_remain_visible(self):
        source = replace(
            self.graph().sources[0],
            status="partial",
            diagnostics=tuple(Diagnostic("fixture", str(i)) for i in range(12)),
        )
        result = query_graph(
            compose_graph([source]), QueryRequest("definitions", "repo")
        )
        self.assertEqual(result.diagnostic_count, 12)
        self.assertEqual(len(result.diagnostics), 10)
        self.assertTrue(result.diagnostics_truncated)
        self.assertEqual(result.status, "partial")

    def test_captured_scip_keeps_resolution_and_coordinate_qualification(self):
        source = ingest_scip(
            Path(__file__).parent / "fixtures/scip/typescript.scip",
            repository_id="repo",
        )
        graph = compose_graph([source])
        result = query_graph(
            graph,
            QueryRequest(
                "references",
                "repo",
                symbol="scip-typescript npm deepthought-typescript-fixture 0.0.0 other/`helpers.ts`/helper().",
            ),
        )
        self.assertEqual(result.total_matches, 3)
        self.assertTrue(
            all(
                row["reference"]["coordinate_encoding"] == "unspecified"
                for row in result.matches
            )
        )
        self.assertEqual(result.status, "partial")
        self.assertEqual(
            result.coverage,
            dict(Counter(c.status for c in source.coverage)),
        )
        self.assertEqual(result.resolution_counts, graph.resolution_counts)

    def test_invalid_requests_are_rejected_instead_of_coerced(self):
        cases = [
            dict(operation=[]),
            dict(operation="calls"),
            dict(repository_id=""),
            dict(limit=True),
            dict(limit=0),
            dict(limit=101),
            dict(max_output_bytes=False),
            dict(max_output_bytes=1023),
            dict(max_output_bytes=32769),
            dict(symbol=42),
            dict(file="main.py"),
            dict(operation="references"),
            dict(operation="dependencies", file="../outside"),
            dict(operation="dependencies", file="main.py", symbol="helper"),
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                QueryRequest(
                    **(dict(operation="definitions", repository_id="repo") | overrides)
                )

    def run_cli(self, path, *args):
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "deepthought",
                "query",
                str(path),
                "--repository-id",
                "repo",
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_installed_cli_and_sdk_emit_the_same_bounded_response(self):
        graph = self.graph()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "graph.json"
            path.write_text(graph.to_json())
            run = self.run_cli(
                path, "--operation", "references", "--symbol", FIRST, "--limit", "1"
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(
                run.stdout,
                query_graph(
                    graph, QueryRequest("references", "repo", symbol=FIRST, limit=1)
                ).to_json()
                + "\n",
            )

    def test_cli_partial_unavailable_and_invalid_inputs_have_honest_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "graph.json"
            source = analyze_repository(
                Path(__file__).parent / "fixtures/python",
                repository_id="repo",
                languages=("python",),
            )
            path.write_text(compose_graph([source]).to_json())
            run = self.run_cli(path, "--operation", "references", "--symbol", "helper")
            self.assertEqual(run.returncode, 1, run.stderr)
            self.assertEqual(json.loads(run.stdout)["status"], "unavailable")
            path.write_text(
                compose_graph(
                    [result_for("repo", [("main.py", [], [(1, FIRST, 0)])])]
                ).to_json()
            )
            run = self.run_cli(path, "--operation", "references", "--symbol", FIRST)
            self.assertEqual(run.returncode, 1, run.stderr)
            self.assertEqual(json.loads(run.stdout)["status"], "partial")
            for extra in [
                ("--limit", "0"),
                ("--max-artifact-bytes", "1"),
                ("--repository-id", "absent"),
                ("--file", "../outside"),
            ]:
                run = self.run_cli(path, "--operation", "definitions", *extra)
                self.assertEqual(run.returncode, 2, run.stderr)
                self.assertEqual(run.stdout, "")
                self.assertNotIn("Traceback", run.stderr)
            alias = Path(directory) / "alias.json"
            alias.symlink_to(path)
            run = self.run_cli(alias, "--operation", "definitions")
            self.assertEqual(run.returncode, 2)
            path.write_text(source.to_json())
            self.assertEqual(
                self.run_cli(path, "--operation", "definitions").returncode, 2
            )
            path.write_text("{")
            self.assertEqual(
                self.run_cli(path, "--operation", "definitions").returncode, 2
            )
