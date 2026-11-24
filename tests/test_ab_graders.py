"""Positive and negative controls for the agent-ab-1 graders (tools/ab_graders.py)."""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import ab_graders  # noqa: E402,F401  (registers the graders)
from deepthought.evaluation import grade  # noqa: E402

SOURCE = {"kind": "synthetic", "files": {"a.go": "0" * 64}}


def scenario(grader, expected, **metadata):
    value = {
        "schema_version": 1,
        "id": f"t-{grader}",
        "tool": "agent-ab-1",
        "task": "test",
        "languages": ["go"],
        "source": SOURCE,
        "expected": expected,
        "grader": grader,
    }
    if metadata:
        value["metadata"] = metadata
    return value


def loc(file, line, role=None, span=None):
    row = {"file": file, "line": line}
    if role:
        row["role"] = role
    if span:
        row["span"] = span
    return row


AFFECTED = scenario(
    "affected-set-v1",
    [
        loc("a.go", 10, "affected", [10, 20]),
        loc("b.go", 5, "affected"),
        loc("c.go", 7, "unjudged"),
    ],
)


class AffectedSetTests(unittest.TestCase):
    def test_exact_answer_passes(self):
        result = grade(AFFECTED, {"items": [loc("a.go", 10), loc("b.go", 5)]})
        self.assertTrue(result["quality_pass"])
        self.assertEqual((result["f1"], result["wrong_claims"]), (1.0, 0))

    def test_line_in_span_or_within_tolerance_matches(self):
        result = grade(AFFECTED, {"items": [loc("a.go", 15), loc("b.go", 8)]})
        self.assertEqual(result["f1"], 1.0)
        result = grade(AFFECTED, {"items": [loc("a.go", 10), loc("b.go", 9)]})
        self.assertEqual((result["recall"], result["wrong_claims"]), (0.5, 1))

    def test_unjudged_items_are_neither_right_nor_wrong(self):
        result = grade(
            AFFECTED, {"items": [loc("a.go", 10), loc("b.go", 5), loc("c.go", 7)]}
        )
        self.assertTrue(result["quality_pass"])
        self.assertEqual(result["unjudged_count"], 1)

    def test_negative_controls(self):
        empty = grade(AFFECTED, {"items": []})
        self.assertEqual((empty["f1"], empty["quality_pass"]), (0.0, False))
        wrong = grade(AFFECTED, {"items": [loc("z.go", 1), loc("a.go", 99)]})
        self.assertEqual((wrong["precision"], wrong["wrong_claims"]), (0.0, 2))
        # One truth row cannot be claimed twice.
        twice = grade(AFFECTED, {"items": [loc("b.go", 5), loc("b.go", 6)]})
        self.assertEqual((twice["correct_count"], twice["wrong_claims"]), (1, 1))
        broken = grade(AFFECTED, {"items": [{"file": "/etc/passwd", "line": 0}]})
        self.assertFalse(broken["quality_pass"])
        self.assertTrue(broken["invalid"])
        self.assertFalse(grade(AFFECTED, {"answer": []})["quality_pass"])

    def test_truth_must_have_a_scored_row(self):
        with self.assertRaises(ValueError):
            grade(scenario("affected-set-v1", [loc("c.go", 7, "unjudged")]), {})


PLAN = scenario(
    "change-plan-v1",
    [
        loc("m.go", 1, "must_edit"),
        loc("m.go", 20, "must_edit"),
        loc("r.go", 1, "retest"),
        loc("r.go", 30, "retest"),
    ],
    tolerance=0,
)


class ChangePlanTests(unittest.TestCase):
    def test_exact_plan_passes(self):
        result = grade(
            PLAN,
            {
                "must_edit": [loc("m.go", 1), loc("m.go", 20)],
                "retest": [loc("r.go", 1), loc("r.go", 30)],
            },
        )
        self.assertTrue(result["quality_pass"])
        self.assertEqual((result["f1"], result["retest_recall"]), (1.0, 1.0))

    def test_retest_item_listed_as_edit_counts_for_the_union_only(self):
        result = grade(
            PLAN,
            {
                "must_edit": [loc("m.go", 1), loc("m.go", 20), loc("r.go", 1)],
                "retest": [loc("r.go", 30)],
            },
        )
        self.assertEqual(result["f1"], 1.0)
        self.assertEqual(result["must_edit"]["wrong"], [])
        self.assertEqual(result["must_edit"]["precision"], 1.0)
        self.assertEqual(result["retest_recall"], 0.5)

    def test_negative_controls(self):
        empty = grade(PLAN, {"must_edit": [], "retest": []})
        self.assertEqual((empty["f1"], empty["quality_pass"]), (0.0, False))
        wrong = grade(PLAN, {"must_edit": [loc("x.go", 1)], "retest": []})
        self.assertEqual((wrong["precision"], wrong["wrong_claims"]), (0.0, 1))
        missing_key = grade(PLAN, {"must_edit": [loc("m.go", 1)]})
        self.assertFalse(missing_key["quality_pass"])


CHECK = scenario(
    "change-check-v1",
    [
        loc("c.go", 12, "omission"),
        loc("d.go", 40, "omission"),
        loc("c.go", 30, "covered"),
    ],
    tolerance=0,
)


class ChangeCheckTests(unittest.TestCase):
    def test_finding_every_omission_passes(self):
        result = grade(
            CHECK, {"complete": False, "missed": [loc("c.go", 12), loc("d.go", 40)]}
        )
        self.assertTrue(result["quality_pass"])

    def test_negative_controls(self):
        says_complete = grade(CHECK, {"complete": True, "missed": []})
        self.assertEqual(
            (says_complete["recall"], says_complete["quality_pass"]), (0.0, False)
        )
        # A site the patch already updates is a false alarm.
        alarm = grade(
            CHECK,
            {
                "complete": False,
                "missed": [loc("c.go", 12), loc("d.go", 40), loc("c.go", 30)],
            },
        )
        self.assertEqual((alarm["false_alarms"], alarm["quality_pass"]), (1, False))
        contradiction = grade(CHECK, {"complete": True, "missed": [loc("c.go", 12)]})
        self.assertFalse(contradiction["quality_pass"])
        self.assertFalse(grade(CHECK, {"missed": []})["quality_pass"])


FACTS = scenario(
    "exact-fact-v1",
    [
        {"role": "fact", "key": "file", "value": "command.go"},
        {"role": "fact", "key": "line", "value": 1068},
        {"role": "fact", "key": "returns", "value": ["*Command", "error"]},
    ],
)


class ExactFactTests(unittest.TestCase):
    def test_exact_facts_pass_up_to_case_and_spacing(self):
        result = grade(
            FACTS,
            {
                "facts": {
                    "file": "command.go",
                    "line": 1068,
                    "returns": ["error", "*command"],
                }
            },
        )
        self.assertTrue(result["quality_pass"])
        self.assertEqual(result["f1"], 1.0)

    def test_negative_controls(self):
        off_by_one = grade(
            FACTS,
            {"facts": {"file": "command.go", "line": 1069, "returns": ["error"]}},
        )
        self.assertFalse(off_by_one["quality_pass"])
        self.assertEqual(off_by_one["wrong"], ["line", "returns"])
        self.assertEqual(off_by_one["correct_count"], 2)
        self.assertEqual(off_by_one["wrong_claims"], 1)
        self.assertFalse(grade(FACTS, {"facts": {}})["quality_pass"])
        self.assertFalse(grade(FACTS, {"facts": "command.go:1068"})["quality_pass"])
        # A number given as a string is not the number.
        self.assertIn(
            "line",
            grade(
                FACTS,
                {
                    "facts": {
                        "file": "command.go",
                        "line": "1068",
                        "returns": ["*Command", "error"],
                    }
                },
            )["wrong"],
        )


if __name__ == "__main__":
    unittest.main()
