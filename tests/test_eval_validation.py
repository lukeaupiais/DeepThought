"""Independent public scenario and grader falsification controls.

Positive controls exercise an oracle answer, not an AI agent; they can validate
harness decisions but can never establish agent-performance improvement.
"""

import copy
import os
import tempfile
import unittest
from pathlib import Path

from deepthought.analysis_tools import (
    AnalysisTool,
    NodeData,
    StandardizedOutput,
    ToolCapability,
    ToolExecutionMetadata,
    ToolMetadata,
    ToolRegistry,
)
from deepthought.evaluation import (
    compare,
    finalize_record,
    grade,
    load_scenario,
    prepare,
)

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "evals/scenarios/tree_sitter/declarations.json"
FIXTURE = ROOT / "evals/fixtures/tree_sitter_declarations"
PUBLIC_PACK = ROOT / "evals/scenarios/tree_sitter/deepagents_backends.json"


class IndependentEvalValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scenario = load_scenario(PACK)

    def answer(self):
        return {"claims": copy.deepcopy(self.scenario["expected"]), "relationships": []}

    def test_complete_oracle_and_different_order_are_accepted(self):
        answer = self.answer()
        self.assertTrue(grade(self.scenario, answer)["quality_pass"])
        answer["claims"].reverse()
        self.assertTrue(grade(self.scenario, answer)["quality_pass"])

    def test_missing_nested_definition_is_rejected(self):
        answer = self.answer()
        answer["claims"] = [
            row for row in answer["claims"] if row["symbol"] != "build.parse"
        ]
        result = grade(self.scenario, answer)
        self.assertFalse(result["quality_pass"])
        self.assertTrue(result["missing"])

    def test_same_name_wrong_lexical_scope_is_rejected(self):
        answer = self.answer()
        next(row for row in answer["claims"] if row["symbol"] == "Writer.parse")[
            "symbol"
        ] = "Reader.parse"
        self.assertFalse(grade(self.scenario, answer)["quality_pass"])

    def test_comment_string_lookalike_is_not_a_declaration(self):
        answer = self.answer()
        answer["claims"].append(
            {"file": "cases.py", "symbol": "imaginary", "kind": "Function", "line": 24}
        )
        self.assertFalse(grade(self.scenario, answer)["quality_pass"])

    def test_duplicate_claim_is_rejected_instead_of_double_counted(self):
        answer = self.answer()
        answer["claims"].append(copy.deepcopy(answer["claims"][0]))
        self.assertFalse(grade(self.scenario, answer)["quality_pass"])

    def test_wrong_line_and_wrong_file_citations_are_rejected(self):
        for field, value in [("line", 1), ("file", "cases.ts")]:
            with self.subTest(field=field):
                answer = self.answer()
                answer["claims"][0][field] = value
                self.assertFalse(grade(self.scenario, answer)["quality_pass"])

    def test_path_escape_and_boolean_line_are_rejected(self):
        for field, value in [
            ("file", "../cases.py"),
            ("file", "/cases.py"),
            ("line", True),
        ]:
            with self.subTest(field=field, value=value):
                answer = self.answer()
                answer["claims"][0][field] = value
                self.assertFalse(grade(self.scenario, answer)["quality_pass"])

    def test_correct_declarations_do_not_justify_an_invented_call(self):
        answer = self.answer()
        answer["relationships"] = [
            {"type": "CALLS", "source": "build", "target": "Reader.parse"}
        ]
        result = grade(self.scenario, answer)
        self.assertFalse(result["quality_pass"])
        self.assertTrue(result["unsupported_relationships"])

    def test_malformed_answer_cannot_pass(self):
        for answer in [{}, {"claims": "correct"}, {"claims": [None]}]:
            with self.subTest(answer=answer):
                self.assertFalse(grade(self.scenario, answer)["quality_pass"])

    def test_private_expected_answers_are_not_forwarded_to_actor_contexts(self):
        prepared = prepare(self.scenario, FIXTURE)
        for context in prepared["contexts"].values():
            self.assertNotIn("expected", context)
            self.assertNotIn("scenario", context)
        self.assertNotIn("tool_output", prepared["contexts"]["baseline"])
        self.assertIn("tool_output", prepared["contexts"]["treatment"])

    def test_changed_source_fails_before_an_actor_context_is_issued(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # This is an authored harness-failure input, not a copied repository.
            (root / "cases.py").write_text("def other(): pass\n")
            (root / "cases.ts").write_text("export class Other {}\n")
            with self.assertRaises(ValueError):
                prepare(self.scenario, root)

    def test_missing_usage_remains_unknown_and_has_no_gain(self):
        prepared = prepare(self.scenario, FIXTURE)
        agent = {"model": "test-model", "settings": {"effort": "same"}}
        baseline = finalize_record(
            prepared,
            "baseline",
            self.answer(),
            agent=agent,
            execution={"status": "completed", "wall_seconds": 1.0},
            kind="agent",
        )
        treatment = finalize_record(
            prepared,
            "treatment",
            self.answer(),
            agent=agent,
            execution={"status": "completed", "wall_seconds": 1.0},
            kind="agent",
        )
        result = compare(baseline, treatment)
        self.assertIsNone(baseline["usage"]["input_tokens"])
        self.assertIsNone(treatment["usage"]["output_tokens"])
        self.assertIsNone(result["arms"]["baseline"]["total_tokens"])
        self.assertIsNone(result["observed_pair_tokens"])
        self.assertEqual(result["status"], "unknown_usage")
        self.assertNotEqual(result.get("status"), "observed_reduction")
        self.assertFalse(result.get("benefit_demonstrated", False))

    def test_faster_cheaper_scripted_control_never_becomes_agent_benefit(self):
        prepared = prepare(self.scenario, FIXTURE)
        agent = {"model": "test-model", "settings": {"effort": "same"}}
        execution = {"status": "completed", "wall_seconds": 1.0}
        baseline = finalize_record(
            prepared,
            "baseline",
            self.answer(),
            usage={"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 50},
            agent=agent,
            execution=execution,
            kind="scripted_control",
        )
        treatment = finalize_record(
            prepared,
            "treatment",
            self.answer(),
            usage={"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 5},
            agent=agent,
            execution=execution,
            kind="scripted_control",
        )
        result = compare(baseline, treatment)
        self.assertFalse(result.get("benefit_demonstrated", False))
        self.assertNotEqual(result.get("status"), "observed_reduction")

    def agent_record(self, prepared, arm, answer=None, **kwargs):
        kwargs.setdefault("execution", {"status": "completed", "wall_seconds": 1.0})
        return finalize_record(
            prepared,
            arm,
            self.answer() if answer is None else answer,
            agent={"model": "test-model", "settings": {"effort": "same"}},
            usage={"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 50},
            kind="agent",
            **kwargs,
        )

    def test_missing_agent_execution_receipt_cannot_imply_completion(self):
        prepared = prepare(self.scenario, FIXTURE)
        baseline = self.agent_record(prepared, "baseline")
        treatment = finalize_record(
            prepared,
            "treatment",
            self.answer(),
            agent={"model": "test-model", "settings": {"effort": "same"}},
            usage={"input_tokens": 1, "output_tokens": 1},
            kind="agent",
        )
        result = compare(baseline, treatment)
        self.assertEqual(treatment["execution"]["status"], "unknown")
        self.assertEqual(result["status"], "incomplete_execution")
        self.assertIsNone(result["token_reduction"])
        self.assertIsNone(result["arms"]["treatment"]["total_tokens"])

    def test_cheaper_incomplete_run_is_not_an_efficiency_win(self):
        prepared = prepare(self.scenario, FIXTURE)
        baseline = self.agent_record(prepared, "baseline")
        treatment = self.agent_record(
            prepared,
            "treatment",
            execution={"status": "timed_out", "wall_seconds": 0.1},
        )
        result = compare(baseline, treatment)
        self.assertEqual(result["status"], "incomplete_execution")
        self.assertIsNone(result["token_reduction"])

    def test_incorrect_answer_is_not_an_efficiency_win(self):
        prepared = prepare(self.scenario, FIXTURE)
        answer = self.answer()
        answer["claims"].pop()
        result = compare(
            self.agent_record(prepared, "baseline"),
            self.agent_record(prepared, "treatment", answer),
        )
        self.assertEqual(result["status"], "quality_or_source_failure")
        self.assertIsNone(result["token_reduction"])

    def test_invalid_token_subsets_are_rejected(self):
        prepared = prepare(self.scenario, FIXTURE)
        for usage in [
            {"input_tokens": 10, "output_tokens": 1, "cached_input_tokens": 11},
            {"input_tokens": 10, "output_tokens": 1, "reasoning_output_tokens": 2},
            {"input_tokens": True, "output_tokens": 1},
            {"input_tokens": -1, "output_tokens": 1},
        ]:
            with self.subTest(usage=usage), self.assertRaises(ValueError):
                finalize_record(prepared, "baseline", self.answer(), usage=usage)

    def test_subset_tokens_are_not_added_twice(self):
        prepared = prepare(self.scenario, FIXTURE)
        baseline = self.agent_record(prepared, "baseline")
        treatment = self.agent_record(prepared, "treatment")
        result = compare(baseline, treatment)
        self.assertEqual(result["arms"]["baseline"]["total_tokens"], 120)
        self.assertEqual(result["observed_pair_tokens"], 240)
        self.assertEqual(result["token_reduction"], 0.0)
        self.assertFalse(result["benefit_demonstrated"])

    def test_actual_synthetic_tool_facts_match_independent_labels(self):
        prepared = prepare(self.scenario, FIXTURE)
        nodes = prepared["contexts"]["treatment"]["tool_output"]["nodes"]
        claims = [
            {
                "file": row["properties"]["file_path"],
                "symbol": row["properties"]["name"],
                "kind": row["properties"]["kind"],
                "line": row["properties"]["start_line"],
            }
            for row in nodes
        ]
        self.assertTrue(grade(self.scenario, {"claims": claims})["quality_pass"])

    @unittest.skipUnless(
        os.environ.get("DEEPTHOUGHT_PUBLIC_EVAL_WORKSPACE"),
        "Opt-in public checkout is supplied externally; tests never download it",
    )
    def test_actual_public_tool_facts_match_independent_labels(self):
        scenario = load_scenario(PUBLIC_PACK)
        workspace = Path(os.environ["DEEPTHOUGHT_PUBLIC_EVAL_WORKSPACE"])
        prepared = prepare(scenario, workspace)
        nodes = prepared["contexts"]["treatment"]["tool_output"]["nodes"]
        claims = [
            {
                "file": row["properties"]["file_path"],
                "symbol": row["properties"]["name"],
                "kind": row["properties"]["kind"],
                "line": row["properties"]["start_line"],
            }
            for row in nodes
        ]
        self.assertEqual(len(claims), 78)
        self.assertTrue(grade(scenario, {"claims": claims})["quality_pass"])

    def test_public_pack_is_explicitly_calibration_and_has_no_vendored_source(self):
        scenario = load_scenario(PUBLIC_PACK)
        self.assertEqual(scenario["source"]["kind"], "public_repository")
        self.assertEqual(len(scenario["source"]["revision"]), 40)
        self.assertEqual(scenario["metadata"]["classification"], "public_calibration")
        self.assertEqual(len(scenario["expected"]), 78)
        self.assertNotIn("workspace", scenario["source"])

    def test_second_tool_registers_without_harness_changes(self):
        class IndependentFixtureTool(AnalysisTool):
            calls = 0

            def _get_metadata(self):
                return ToolMetadata(
                    name="independent-fixture",
                    version="1",
                    capabilities={ToolCapability.SYMBOL_INDEXING},
                    supported_languages={"python"},
                    requires_binary=False,
                    binary_name=None,
                    performance_class="unmeasured",
                )

            async def check_availability(self):
                return True

            async def analyze(self, repo_url, **kwargs):
                type(self).calls += 1
                facts = [
                    ("build", "Function", 4),
                    ("build.parse", "Function", 5),
                    ("Reader", "Class", 10),
                    ("Reader.parse", "Method", 11),
                    ("Writer", "Class", 15),
                    ("Writer.parse", "Method", 16),
                    ("consume", "Function", 20),
                ]
                return StandardizedOutput(
                    nodes=[
                        NodeData(
                            labels=[kind],
                            properties={
                                "repo_url": repo_url,
                                "qualified_name": f"{repo_url}::cases.py::{symbol}",
                                "file_path": "cases.py",
                                "name": symbol,
                                "kind": kind,
                                "start_line": line,
                            },
                        )
                        for symbol, kind, line in facts
                    ],
                    relationships=[],
                    metadata=ToolExecutionMetadata(
                        "independent-fixture",
                        "1",
                        0.0,
                        languages_processed=["python"],
                        files_processed=1,
                        custom_metrics={"analysis_status": "succeeded"},
                    ),
                )

            async def get_symbol_definitions(self):
                return {}

        registry = ToolRegistry()
        registry.register(IndependentFixtureTool)
        scenario = copy.deepcopy(self.scenario)
        scenario["tool"] = "independent-fixture"
        scenario["languages"] = ["python"]
        scenario["source"]["files"] = {
            "cases.py": scenario["source"]["files"]["cases.py"]
        }
        scenario["expected"] = [
            row for row in scenario["expected"] if row["file"] == "cases.py"
        ]
        prepared = prepare(scenario, FIXTURE, registry=registry)
        self.assertEqual(IndependentFixtureTool.calls, 1)
        self.assertIn("tool_output", prepared["contexts"]["treatment"])


if __name__ == "__main__":
    unittest.main()
