"""Edits with a known effect change only the edges they should.

Runs tools/check_metamorphic.py's shift, rename and add transformations on a
polyglot workspace and checks that the checker itself rejects a wrong
prediction.
"""

import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
import check_metamorphic  # noqa: E402
from test_determinism import FIXTURES, GO, JAVA, RUST, write  # noqa: E402

GRAMMARS = ("python", "typescript", "javascript", "go", "java", "rust")
READY = importlib.util.find_spec("tree_sitter") and all(
    importlib.util.find_spec(f"tree_sitter_{g}") for g in GRAMMARS
)


def polyglot(directory):
    shutil.copytree(FIXTURES / "python", directory, dirs_exist_ok=True)
    for language in ("typescript", "javascript"):
        shutil.copytree(FIXTURES / language, Path(directory, language))
    write(Path(directory, "go"), GO)
    write(Path(directory, "java"), JAVA)
    write(Path(directory, "rust"), RUST)


@unittest.skipUnless(READY, "Every language grammar is required")
class MetamorphicTest(unittest.TestCase):
    def test_predicted_effects_hold_in_every_language(self):
        with tempfile.TemporaryDirectory() as directory:
            polyglot(directory)
            for seed in range(3):
                reports = check_metamorphic.run(
                    Path(directory), GRAMMARS, trials=6, seed=seed
                )
                self.assertEqual(
                    {r["transformation"].split()[0] for r in reports},
                    {"shift", "rename", "add"},
                )
                for report in reports:
                    self.assertGreater(report["edges"], 20)
                    self.assertEqual(
                        (report["missing"], report["extra"], report["changed"]),
                        (0, 0, 0),
                        report,
                    )

    def test_a_wrong_prediction_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            polyglot(directory)
            original = check_metamorphic.edges(
                check_metamorphic.analyze(directory, GRAMMARS)
            )
            path = Path(directory, "lib/helpers.py")
            path.write_text("\n" + path.read_text())
            shifted = check_metamorphic.edges(
                check_metamorphic.analyze(directory, GRAMMARS)
            )
        # Calls in main.py keep their positions but their target moved.
        self.assertNotEqual(shifted, original)
        self.assertEqual(
            shifted,
            check_metamorphic.predict(
                original,
                location=lambda at: (
                    (at[0], at[1], at[2] + 1, *at[3:])
                    if at[1] == "lib/helpers.py"
                    else at
                ),
            ),
        )
        renamed = check_metamorphic.predict(
            original, text=lambda s: s.replace("helper", "helpeq")
        )
        self.assertNotEqual(renamed, original)


if __name__ == "__main__":
    unittest.main()
