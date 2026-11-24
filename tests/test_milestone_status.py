import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from milestone_status import evaluate  # noqa: E402

MILESTONE = {
    "impact": [{"corpus": "a", "min_recall": 0.9, "min_precision": 0.99}],
    "contracts": {
        "corpora": [
            {"corpus": "contracts-x", "min_pair_recall": 0.95, "max_false_pairs": 0}
        ]
    },
    "held_out": {"rule": "sealed"},
    "capabilities": ["graders exist"],
}


def baseline(recall, precision, pairs):
    labelled, predicted, true_positive = pairs
    return {
        "a": {"impact": {"recall": recall, "precision": precision}},
        "contracts": {
            "contracts-x": {
                "grpc": {
                    "service_pairs": {
                        "labelled_in_scope": labelled,
                        "predicted": predicted,
                        "true_positive": true_positive,
                    }
                },
                "http": {
                    "service_pairs": {
                        "labelled_in_scope": 1,
                        "predicted": 1,
                        "true_positive": 1,
                    }
                },
            }
        },
    }


class MilestoneStatusTest(unittest.TestCase):
    def test_passes_when_every_threshold_is_met(self):
        rows, manual = evaluate(MILESTONE, baseline(0.9, 1.0, (19, 19, 19)))
        self.assertTrue(all(row["passed"] for row in rows))
        self.assertIn("20/20", rows[1]["detail"])
        self.assertEqual(manual, ["held-out corpora: sealed", "graders exist"])

    def test_low_recall_or_precision_fails(self):
        rows, _ = evaluate(MILESTONE, baseline(0.89, 1.0, (19, 19, 19)))
        self.assertFalse(rows[0]["passed"])
        rows, _ = evaluate(MILESTONE, baseline(0.95, 0.98, (19, 19, 19)))
        self.assertFalse(rows[0]["passed"])

    def test_pairs_sum_over_kinds_and_false_pairs_fail(self):
        rows, _ = evaluate(MILESTONE, baseline(1.0, 1.0, (13, 12, 12)))
        self.assertFalse(rows[1]["passed"])  # 13/14 < 0.95
        rows, _ = evaluate(MILESTONE, baseline(1.0, 1.0, (19, 20, 19)))
        self.assertFalse(rows[1]["passed"])  # one false pair
        self.assertIn("1 false", rows[1]["detail"])

    def test_missing_scores_fail(self):
        rows, _ = evaluate(MILESTONE, {})
        self.assertEqual([row["passed"] for row in rows], [False, False])

    def test_committed_milestone_is_well_formed(self):
        path = ROOT / "evals/milestones/agent-ab-1.json"
        milestone = json.loads(path.read_text())
        rows, _ = evaluate(
            milestone,
            json.loads((ROOT / "evals/ground_truth/baseline.json").read_text()),
        )
        self.assertEqual(len(rows), 7)
        self.assertEqual(len(milestone["tasks"]), 12)


if __name__ == "__main__":
    unittest.main()
