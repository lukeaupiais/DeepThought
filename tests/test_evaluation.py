"""Harness outcomes are validated with independently constructed adversarial records."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import subprocess
import tempfile
import unittest
from uuid import uuid4

from deepthought.analysis_tools import (
    AnalysisTool,
    ToolCapability,
    ToolMetadata,
    ToolRegistry,
    StandardizedOutput,
    ToolExecutionMetadata,
)
from deepthought.evaluation import (
    Grader,
    compare,
    finalize_record,
    grade,
    load_scenario,
    prepare,
    register_grader,
)


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "evals/scenarios/tree_sitter/declarations.json"
FIXTURE = ROOT / "evals/fixtures/tree_sitter_declarations"


class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scenario = load_scenario(PACK)
        cls.prepared = prepare(cls.scenario, FIXTURE)
        cls.correct = {"claims": cls.scenario["expected"], "relationships": []}

    def test_actor_contexts_do_not_expose_grading_labels(self):
        for context in self.prepared["contexts"].values():
            self.assertNotIn("expected", context)
            self.assertNotIn("scenario", context)
            self.assertEqual(context["task"], self.scenario["task"])
        self.assertNotIn("tool_output", self.prepared["contexts"]["baseline"])
        self.assertIn("tool_output", self.prepared["contexts"]["treatment"])

    def test_positive_and_false_confidence_negative_controls(self):
        self.assertTrue(grade(self.scenario, self.correct)["quality_pass"])
        for change in [
            "missing",
            "duplicate",
            "extra",
            "line",
            "relationship",
            "override",
        ]:
            answer = deepcopy(self.correct)
            if change == "missing":
                answer["claims"].pop()
            elif change == "duplicate":
                answer["claims"].append(deepcopy(answer["claims"][0]))
            elif change == "extra":
                extra = deepcopy(answer["claims"][0])
                extra["symbol"] = "NotInTheSource"
                answer["claims"].append(extra)
            elif change == "line":
                answer["claims"][0]["line"] += 1
            elif change == "relationship":
                answer["relationships"] = [{"type": "CALLS", "target": "imaginary"}]
            else:
                answer["quality_pass"] = True
            with self.subTest(change=change):
                self.assertFalse(grade(self.scenario, answer)["quality_pass"])

    def make_record(self, arm, **overrides):
        args = {
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
            },
            "agent": {"model": "example-model", "settings": {"effort": "high"}},
            "kind": "agent",
            "execution": {"status": "completed", "wall_seconds": 1},
        }
        args.update(overrides)
        return finalize_record(self.prepared, arm, self.correct, **args)

    def test_scripted_controls_never_demonstrate_agent_gain(self):
        b = self.make_record("baseline", kind="scripted_control")
        t = self.make_record("treatment", kind="scripted_control")
        result = compare(b, t)
        self.assertEqual(result["status"], "scripted_control_only")
        self.assertIsNone(result["token_reduction"])
        self.assertFalse(result["benefit_demonstrated"])

    def test_subsets_not_double_counted_and_external_usage_not_attested(self):
        b = self.make_record("baseline")
        t = self.make_record(
            "treatment", usage={"input_tokens": 50, "output_tokens": 10}
        )
        result = compare(b, t)
        self.assertEqual(result["arms"]["baseline"]["total_tokens"], 120)
        self.assertEqual(result["arms"]["treatment"]["total_tokens"], 60)
        self.assertEqual(result["token_reduction"], 0.5)
        self.assertEqual(result["observed_pair_tokens"], 180)
        self.assertFalse(result["benefit_demonstrated"])
        self.assertEqual(
            result["usage_provenance"], "caller_reported_not_authenticated"
        )
        self.assertIsNone(result["arms"]["treatment"]["usage"]["cached_input_tokens"])

    def test_unknown_and_incomplete_usage_is_not_free(self):
        b = self.make_record("baseline")
        t = self.make_record("treatment", usage=None)
        self.assertEqual(compare(b, t)["status"], "unknown_usage")
        t = self.make_record(
            "treatment", execution={"status": "timed_out", "wall_seconds": 3}
        )
        result = compare(b, t)
        self.assertEqual(result["status"], "incomplete_execution")
        self.assertIsNone(result["arms"]["treatment"]["total_tokens"])
        self.assertEqual(result["arms"]["treatment"]["reported_tokens"], 120)
        self.assertIsNone(result["observed_pair_tokens"])
        self.assertTrue(result["unknown_or_incomplete_usage"])

    def test_bad_usage_and_nonfinite_time_rejected(self):
        for usage in [
            {"input_tokens": True},
            {"input_tokens": -1},
            {"input_tokens": 5, "cached_input_tokens": 6},
            {"output_tokens": 2, "reasoning_output_tokens": 3},
        ]:
            with self.subTest(usage=usage), self.assertRaises(ValueError):
                self.make_record("baseline", usage=usage)
        with self.assertRaises(ValueError):
            self.make_record(
                "baseline",
                execution={"status": "completed", "wall_seconds": float("nan")},
            )

    def test_records_must_be_matched_and_quality_cannot_be_overridden(self):
        b = self.make_record("baseline")
        t = self.make_record("treatment", agent={"model": "other", "settings": {}})
        with self.assertRaises(ValueError):
            compare(b, t)
        t = self.make_record("treatment")
        t["quality"]["quality_pass"] = False
        with self.assertRaises(ValueError):
            compare(b, t)

    def test_source_symlinks_and_changed_sources_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "real.py"
            source.write_text("def public(): pass\n")
            (root / "link.py").symlink_to(source)
            scenario = deepcopy(self.scenario)
            scenario["source"] = {
                "kind": "synthetic",
                "files": {"link.py": sha256(source.read_bytes()).hexdigest()},
            }
            scenario["expected"] = [
                {"file": "link.py", "symbol": "public", "kind": "Function", "line": 1}
            ]
            with self.assertRaises(ValueError):
                prepare(scenario, root)
            scenario["source"]["files"] = {"real.py": "0" * 64}
            scenario["expected"][0]["file"] = "real.py"
            with self.assertRaises(ValueError):
                prepare(scenario, root)

    def test_public_manifest_cannot_relabel_modified_or_untracked_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(*args):
                return subprocess.check_output(
                    [
                        "git",
                        "-C",
                        str(root),
                        "-c",
                        "core.hooksPath=/dev/null",
                        "-c",
                        "commit.gpgsign=false",
                        "-c",
                        "user.name=Harness Control",
                        "-c",
                        "user.email=harness@example.invalid",
                        *args,
                    ],
                    text=True,
                ).strip()

            git("init", "--quiet")
            (root / "fixture.py").write_text("def original(): pass\n")
            git("add", "fixture.py")
            git("commit", "--quiet", "-m", "Public-provenance negative control")
            revision = git("rev-parse", "HEAD")
            for name in ("fixture.py", "untracked.py"):
                (root / name).write_text("def replacement(): pass\n")
                scenario = deepcopy(self.scenario)
                scenario["languages"] = ["python"]
                scenario["source"] = {
                    "kind": "public_repository",
                    "repository": "https://github.com/example/public-source",
                    "revision": revision,
                    "files": {name: sha256((root / name).read_bytes()).hexdigest()},
                }
                scenario["expected"] = [
                    {
                        "file": name,
                        "symbol": "replacement",
                        "kind": "Function",
                        "line": 1,
                    }
                ]
                with (
                    self.subTest(name=name),
                    self.assertRaisesRegex(ValueError, "pinned commit"),
                ):
                    prepare(scenario, root)

    def test_other_tool_and_grader_plug_in_without_harness_changes(self):
        name = "marker-" + uuid4().hex
        events = []

        class MarkerTool(AnalysisTool):
            def _get_metadata(self):
                return ToolMetadata(
                    name="marker",
                    version="1",
                    capabilities={ToolCapability.SEMANTIC_ANALYSIS},
                    supported_languages={"python"},
                    requires_binary=False,
                    binary_name=None,
                    performance_class="fast",
                )

            async def check_availability(self):
                events.append("available")
                return True

            async def analyze(
                self, repo_url, languages=None, file_patterns=None, skip_patterns=None
            ):
                events.append("analyze")
                return StandardizedOutput(
                    nodes=[],
                    relationships=[],
                    metadata=ToolExecutionMetadata(
                        tool_name="marker",
                        tool_version="1",
                        execution_time_seconds=0,
                        custom_metrics={
                            "analysis_status": "succeeded",
                            "marker": "different-fact-shape",
                        },
                    ),
                )

            async def get_symbol_definitions(self):
                return {}

            async def cleanup(self):
                events.append("cleanup")

        def validate_expected(scenario):
            if scenario["expected"] != [{"marker": "different-fact-shape"}]:
                raise ValueError("Wrong marker oracle")

        def score(scenario, answer):
            return {
                "quality_pass": answer == {"marker": scenario["expected"][0]["marker"]}
            }

        register_grader(
            name,
            Grader(
                validate_expected, score, "Return a marker object, not declarations."
            ),
        )
        registry = ToolRegistry()
        registry.register(MarkerTool)
        scenario = deepcopy(self.scenario)
        scenario.update(
            tool="marker",
            grader=name,
            languages=["python"],
            expected=[{"marker": "different-fact-shape"}],
        )
        prepared = prepare(scenario, FIXTURE, registry=registry)
        self.assertEqual(events, ["available", "analyze", "cleanup"])
        self.assertIn(
            "marker object", prepared["contexts"]["treatment"]["instructions"]
        )
        self.assertNotIn(
            "declarations only", prepared["contexts"]["baseline"]["instructions"]
        )
        self.assertTrue(
            grade(scenario, {"marker": "different-fact-shape"})["quality_pass"]
        )
        with self.assertRaises(ValueError):
            register_grader(name, Grader(validate_expected, score, "same"))


if __name__ == "__main__":
    unittest.main()
