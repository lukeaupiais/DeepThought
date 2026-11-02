"""Independent harness counterexamples, using synthetic receipts, never models."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from deepthought.analysis_tools import ToolRegistry, TreeSitterTool
from deepthought.evaluation import compare, finalize_record, load_scenario, prepare

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "evals/scenarios/tree_sitter/declarations.json"
FIXTURE = ROOT / "evals/fixtures/tree_sitter_declarations"


def digest(value):
    from hashlib import sha256

    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


class EvalIndependentReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scenario = load_scenario(PACK)
        cls.prepared = prepare(cls.scenario, FIXTURE)

    def answer(self):
        return {"claims": deepcopy(self.scenario["expected"]), "relationships": []}

    def test_both_arms_receive_the_same_explicit_declaration_label_rules(self):
        baseline = self.prepared["contexts"]["baseline"]
        treatment = self.prepared["contexts"]["treatment"]
        self.assertEqual(baseline["instructions"], treatment["instructions"])
        instructions = baseline["instructions"]
        for rule in (
            "exact case-sensitive kind labels",
            "Class, Function, Method, Interface, Enum, TypeAlias, Namespace",
            "directly in a class's lexical scope is Method",
            "nested inside another function or method is Function",
            "Async declarations use the same Function or Method labels",
            "excluding decorators",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, instructions)
        self.assertNotIn("tool_output", baseline)
        self.assertIn("tool_output", treatment)

    def record(self, arm, *, agent=None, answer=None, execution=None):
        return finalize_record(
            self.prepared,
            arm,
            self.answer() if answer is None else answer,
            agent=agent
            or {"model": "synthetic-review-model", "settings": {"effort": "fixed"}},
            usage={
                "input_tokens": 100 if arm == "baseline" else 10,
                "output_tokens": 5,
            },
            execution={"status": "completed", "wall_seconds": 1.0}
            if execution is None
            else execution,
            kind="agent",
        )

    def test_mutated_task_instructions_and_tool_facts_cannot_reuse_handoff_identity(
        self,
    ):
        for field in ("task", "instructions", "tool_output"):
            with self.subTest(field=field):
                prepared = deepcopy(self.prepared)
                context = prepared["contexts"]["treatment"]
                if field == "tool_output":
                    context[field]["nodes"] = []
                else:
                    context[field] += " different context"
                with self.assertRaises(ValueError):
                    finalize_record(prepared, "treatment", self.answer())

    def test_added_baseline_payload_cannot_reuse_handoff_identity(self):
        prepared = deepcopy(self.prepared)
        prepared["contexts"]["baseline"]["tool_output"] = prepared["contexts"][
            "treatment"
        ]["tool_output"]
        with self.assertRaises(ValueError):
            finalize_record(prepared, "baseline", self.answer())

    def test_resigned_record_cannot_override_independent_quality(self):
        answer = self.answer()
        answer["claims"].pop()
        tampered = self.record("treatment", answer=answer)
        tampered["quality"]["quality_pass"] = True
        tampered["quality_sha256"] = digest(tampered["quality"])
        tampered.pop("record_id")
        tampered["record_id"] = digest(tampered)
        with self.assertRaises(ValueError):
            compare(self.record("baseline"), tampered)

    def test_boolean_and_integer_agent_settings_are_not_an_exact_match(self):
        baseline = self.record(
            "baseline",
            agent={"model": "synthetic-review-model", "settings": {"flag": True}},
        )
        treatment = self.record(
            "treatment",
            agent={"model": "synthetic-review-model", "settings": {"flag": 1}},
        )
        with self.assertRaises(ValueError):
            compare(baseline, treatment)

    def test_missing_agent_completion_receipt_cannot_report_reduction(self):
        try:
            treatment = finalize_record(
                self.prepared,
                "treatment",
                self.answer(),
                agent={
                    "model": "synthetic-review-model",
                    "settings": {"effort": "fixed"},
                },
                usage={"input_tokens": 10, "output_tokens": 5},
                kind="agent",
            )
        except ValueError:
            return
        result = compare(self.record("baseline"), treatment)
        self.assertIsNone(result["token_reduction"])
        self.assertFalse(result["benefit_demonstrated"])

    def test_explicit_empty_execution_is_not_replaced_by_success(self):
        with self.assertRaises(ValueError):
            self.record("treatment", execution={})

    def test_nonfinite_and_boolean_token_values_are_rejected(self):
        for value in (float("nan"), float("inf"), True, 1.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                finalize_record(
                    self.prepared,
                    "baseline",
                    self.answer(),
                    usage={"input_tokens": value},
                )

    def test_unknown_upstream_tokens_remain_unknown(self):
        class UnmeteredTool(TreeSitterTool):
            def _get_metadata(self):
                metadata = super()._get_metadata()
                metadata.name = "unmetered-review"
                return metadata

            async def analyze(self, *args, **kwargs):
                result = await super().analyze(*args, **kwargs)
                result.metadata.custom_metrics.pop("model_tokens", None)
                return result

        registry = ToolRegistry()
        registry.register(UnmeteredTool)
        scenario = deepcopy(self.scenario)
        scenario["tool"] = "unmetered-review"
        prepared = prepare(scenario, FIXTURE, registry=registry)
        self.assertIsNone(prepared["preparation"]["model_tokens"])
        records = [
            finalize_record(
                prepared,
                arm,
                self.answer(),
                usage={"input_tokens": 20, "output_tokens": 5},
                agent={"model": "synthetic-review-model", "settings": {}},
                execution={"status": "completed", "wall_seconds": 1},
                kind="agent",
            )
            for arm in ("baseline", "treatment")
        ]
        result = compare(*records)
        self.assertIsNone(result["pipeline_tokens"]["treatment"])
        self.assertFalse(result["benefit_demonstrated"])

    def test_invalid_upstream_counts_are_rejected(self):
        for invalid in (True, -1, 1.0, float("inf")):
            with self.subTest(invalid=invalid):

                class InvalidMeterTool(TreeSitterTool):
                    def _get_metadata(self):
                        metadata = super()._get_metadata()
                        metadata.name = "invalid-meter-review"
                        return metadata

                    async def analyze(self, *args, **kwargs):
                        result = await super().analyze(*args, **kwargs)
                        result.metadata.custom_metrics["model_tokens"] = invalid
                        return result

                registry = ToolRegistry()
                registry.register(InvalidMeterTool)
                scenario = deepcopy(self.scenario)
                scenario["tool"] = "invalid-meter-review"
                with self.assertRaises(ValueError):
                    prepare(scenario, FIXTURE, registry=registry)
