"""On-demand query handoff controls; these do not establish agent benefit."""

import importlib.util
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from deepthought.evaluation import compare, finalize_record, prepare
from deepthought.evaluation.__main__ import main

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "evals/scenarios/tree_sitter/declarations.json"
SOURCE = ROOT / "evals/fixtures/tree_sitter_declarations"
GRAMMARS = all(
    importlib.util.find_spec(name) is not None
    for name in ("tree_sitter", "tree_sitter_python", "tree_sitter_typescript")
)


@unittest.skipUnless(GRAMMARS, "Provision optional Python/TypeScript grammars")
class QueryAccessEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.access = {
            "command": "/work/graph",
            "help": "Query indexed declarations on demand; graph search --query NAME",
        }
        cls.prepared = prepare(PACK, SOURCE, tool_access=cls.access)
        cls.answer = {
            "claims": cls.prepared["scenario"]["expected"],
            "relationships": [],
        }

    def test_query_access_replaces_injection_and_keeps_oracle_private(self):
        baseline, treatment = (
            self.prepared["contexts"][arm] for arm in ("baseline", "treatment")
        )
        self.assertEqual(baseline["instructions"], treatment["instructions"])
        self.assertNotIn("tool_access", baseline)
        self.assertNotIn("tool_output", baseline)
        self.assertEqual(treatment["tool_access"], self.access)
        self.assertNotIn("tool_output", treatment)
        for context in (baseline, treatment):
            self.assertNotIn("expected", context)
            self.assertNotIn("scenario", context)
            self.assertNotIn("private_tool_output", context)
        self.assertTrue(self.prepared["private_tool_output"]["nodes"])

    def test_default_injection_handoff_is_unchanged(self):
        prepared = prepare(PACK, SOURCE)
        self.assertIn("tool_output", prepared["contexts"]["treatment"])
        self.assertNotIn("tool_access", prepared["contexts"]["treatment"])
        self.assertNotIn("private_tool_output", prepared)
        self.assertNotIn("delivery_mode", prepared["provenance"])

    def test_both_query_access_records_grade_and_compare_without_claiming_benefit(self):
        records = [
            finalize_record(self.prepared, arm, self.answer)
            for arm in ("baseline", "treatment")
        ]
        result = compare(*records)
        self.assertEqual(result["status"], "scripted_control_only")
        self.assertFalse(result["benefit_demonstrated"])
        self.assertTrue(
            all(row["quality"]["quality_pass"] for row in result["arms"].values())
        )

    def test_changed_access_descriptor_is_rejected(self):
        prepared = deepcopy(self.prepared)
        prepared["contexts"]["treatment"]["tool_access"]["command"] = "/other/graph"
        with self.assertRaisesRegex(ValueError, "Actor context changed"):
            finalize_record(prepared, "treatment", self.answer)

    def test_changed_private_graph_is_rejected(self):
        prepared = deepcopy(self.prepared)
        prepared["private_tool_output"]["nodes"] = []
        with self.assertRaisesRegex(ValueError, "Tool output differs"):
            finalize_record(prepared, "treatment", self.answer)

    def test_invalid_descriptors_are_rejected(self):
        for value in (
            {},
            [],
            {"command": "", "help": "query help"},
            {"command": "/work/graph", "help": 12},
            {"command": "/work/graph", "help": "x" * 32_768},
            {"command": "/work/graph", "help": "query help", "bad": float("nan")},
        ):
            with (
                self.subTest(value_type=type(value).__name__),
                self.assertRaises(ValueError),
            ):
                prepare(PACK, SOURCE, tool_access=value)

    def test_cli_handoff_never_executes_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "must-not-exist"
            descriptor = root / "access.json"
            descriptor.write_text(
                json.dumps({"command": f"touch {marker}", "help": "Descriptive only"})
            )
            prepared_dir = root / "prepared"
            self.assertEqual(
                main(
                    [
                        "prepare",
                        str(PACK),
                        str(SOURCE),
                        str(prepared_dir),
                        "--tool-access",
                        str(descriptor),
                    ]
                ),
                0,
            )
            self.assertFalse(marker.exists())
            actor = json.loads((prepared_dir / "treatment.actor.json").read_text())
            self.assertNotIn("tool_output", actor)
            self.assertNotIn("expected", actor)
            prepared = json.loads((prepared_dir / "prepared.private.json").read_text())
            answer = root / "answer.json"
            answer.write_text(json.dumps(self.answer))
            records = []
            for arm in ("baseline", "treatment"):
                record = root / (arm + ".record.json")
                self.assertEqual(
                    main(
                        [
                            "record",
                            str(prepared_dir / "prepared.private.json"),
                            arm,
                            str(answer),
                            str(record),
                        ]
                    ),
                    0,
                )
                records.append(record)
            comparison = root / "comparison.json"
            self.assertEqual(main(["compare", *map(str, records), str(comparison)]), 0)
            self.assertEqual(prepared["provenance"]["delivery_mode"], "tool_access")
            self.assertEqual(
                json.loads(comparison.read_text())["status"], "scripted_control_only"
            )


if __name__ == "__main__":
    unittest.main()
