"""Installed CLI journey using public calibration controls, not model outcomes."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "evals/scenarios/tree_sitter/declarations.json"
SOURCE = ROOT / "evals/fixtures/tree_sitter_declarations"


class EvalCLIIntegrationTests(unittest.TestCase):
    def command(self, *args, expected=0):
        run = subprocess.run(
            [str(Path(sys.executable).with_name("deepthought-eval")), *map(str, args)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(run.returncode, expected, run.stderr)
        return run

    def test_prepare_record_compare_and_refuse_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = root / "prepared"
            self.command(
                "prepare", PACK, SOURCE, prepared, "--interface", "raw-context"
            )
            oracle = json.loads((prepared / "prepared.private.json").read_text())
            for arm in ("baseline", "treatment"):
                actor = json.loads((prepared / (arm + ".actor.json")).read_text())
                self.assertNotIn("expected", actor)
                self.assertNotIn("scenario", actor)
            answer = root / "answer.json"
            answer.write_text(
                json.dumps(
                    {"claims": oracle["scenario"]["expected"], "relationships": []}
                )
            )
            records = []
            for arm in ("baseline", "treatment"):
                record = root / (arm + ".json")
                self.command(
                    "record", prepared / "prepared.private.json", arm, answer, record
                )
                records.append(record)
            comparison = root / "comparison.json"
            self.command("compare", *records, comparison)
            result = json.loads(comparison.read_text())
            self.assertEqual(result["status"], "scripted_control_only")
            self.assertFalse(result["benefit_demonstrated"])
            self.assertIsNone(result["token_reduction"])
            self.assertTrue(
                all(x["quality"]["quality_pass"] for x in result["arms"].values())
            )
            before = comparison.read_bytes()
            self.command("compare", *records, comparison, expected=2)
            self.assertEqual(comparison.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
