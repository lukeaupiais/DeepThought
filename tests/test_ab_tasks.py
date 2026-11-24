"""The committed agent-ab-1 scenarios are valid, and their truth grades as expected."""

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import ab_graders  # noqa: E402,F401  (registers the graders)
from deepthought.evaluation import grade, load_scenario  # noqa: E402

TASKS = ROOT / "evals" / "milestones" / "agent-ab-1"
IDS = ["U1", "U2", "U3", "P1", "P2", "P3", "C1", "C2", "C3", "K1", "K2", "K3"]


def scenarios():
    return {tid: load_scenario(TASKS / "tasks" / f"{tid}.scenario.json") for tid in IDS}


def locations(scenario, role):
    return [
        {"file": r["file"], "line": r["line"]}
        for r in scenario["expected"]
        if r["role"] == role
    ]


def perfect(scenario):
    grader = scenario["grader"]
    if grader == "affected-set-v1":
        return {"items": locations(scenario, "affected")}
    if grader == "change-plan-v1":
        return {
            "must_edit": locations(scenario, "must_edit"),
            "retest": locations(scenario, "retest"),
        }
    if grader == "change-check-v1":
        return {"complete": False, "missed": locations(scenario, "omission")}
    return {"facts": {r["key"]: r["value"] for r in scenario["expected"]}}


class TaskScenarioTests(unittest.TestCase):
    def test_every_task_is_present_and_valid(self):
        loaded = scenarios()
        self.assertEqual(sorted(loaded), sorted(IDS))
        for tid, scenario in loaded.items():
            self.assertEqual(scenario["id"], f"agent-ab-1-{tid}")
            self.assertEqual(scenario["source"]["kind"], "public_repository")

    def test_truth_rows_name_files_at_the_pin(self):
        for tid, scenario in scenarios().items():
            files = scenario["source"]["files"]
            for row in scenario["expected"]:
                if "file" in row and row.get("key") is None:
                    self.assertIn(row["file"], files, tid)

    def test_exact_truth_passes_and_an_empty_answer_fails(self):
        for tid, scenario in scenarios().items():
            with self.subTest(tid):
                self.assertTrue(grade(scenario, perfect(scenario))["quality_pass"])
                empty = {
                    "affected-set-v1": {"items": []},
                    "change-plan-v1": {"must_edit": [], "retest": []},
                    "change-check-v1": {"complete": True, "missed": []},
                    "exact-fact-v1": {"facts": {}},
                }[scenario["grader"]]
                self.assertFalse(grade(scenario, empty)["quality_pass"])

    def test_check_patches_match_and_name_their_omissions(self):
        for tid in ("C1", "C2", "C3"):
            scenario = load_scenario(TASKS / "tasks" / f"{tid}.scenario.json")
            patch = (TASKS / scenario["metadata"]["patch"]).read_text()
            self.assertIn(patch, scenario["task"])
            omitted = {
                f"{r['file']}:{r['line']}"
                for r in scenario["expected"]
                if r["role"] == "omission"
            }
            self.assertEqual(omitted, set(scenario["metadata"]["omitted"]))
            # The patch leaves every omitted line untouched.
            for site in omitted:
                file, line = site.rsplit(":", 1)
                self.assertNotIn(f"+++ b/{file}\n@@ -{line},", patch)

    def test_milestone_counts(self):
        counts = {
            tid: {
                role: sum(r["role"] == role for r in s["expected"])
                for role in ("affected", "must_edit", "retest", "omission")
            }
            for tid, s in scenarios().items()
        }
        self.assertEqual(counts["U3"]["affected"], 34)
        self.assertEqual((counts["P1"]["must_edit"], counts["P1"]["retest"]), (11, 20))
        self.assertEqual((counts["P2"]["must_edit"], counts["P2"]["retest"]), (6, 28))
        self.assertEqual([counts[t]["omission"] for t in ("C1", "C2", "C3")], [2, 1, 2])
        self.assertEqual(json.loads(json.dumps(counts["U1"]))["affected"], 3)


if __name__ == "__main__":
    unittest.main()
