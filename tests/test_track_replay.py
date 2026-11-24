"""The replay history file is well formed and covers the pinned corpora."""

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import track_replay  # noqa: E402


class TrackReplayTests(unittest.TestCase):
    def test_summary_keeps_the_tracked_metrics(self):
        scores = {"recall": 0.5, "precision": 1.0, "mean_predicted_files": 2.0}
        report = {
            "cases": 3,
            "impact": {**scores, "hits": 1},
            "search": {**scores, "hits": 2},
            "per_case": [],
        }
        self.assertEqual(
            track_replay.summary(report),
            {"cases": 3, "impact": scores, "search": scores},
        )

    def test_every_entry_covers_every_corpus(self):
        history = json.loads(track_replay.RESULTS.read_text("utf-8"))
        manifest = json.loads((track_replay.CORPUS / "manifest.json").read_text())
        names = {c["name"] for c in manifest["corpora"]}
        self.assertTrue(history)
        for entry in history:
            self.assertTrue(entry["label"])
            self.assertRegex(entry["engine_commit"], r"^[0-9a-f]{40}$")
            self.assertEqual(set(entry["corpora"]), names)
            for scores in entry["corpora"].values():
                self.assertEqual(set(scores), {"cases", "impact", "search"})


if __name__ == "__main__":
    unittest.main()
