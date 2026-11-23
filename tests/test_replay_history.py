"""Historical replay scores impact and text search against real co-edits."""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

READY = (
    shutil.which("git")
    and importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_python")
)

BEFORE = {
    "pkg/core.py": "def helper(x):\n    return x + 1\n\n\ndef unused():\n    return 0\n",
    "pkg/use.py": (
        "from pkg.core import helper\n\n\ndef caller(y):\n    return helper(y)\n"
    ),
    "pkg/other.py": "def helper_note():\n    return 'helpers'\n",
}
# The commit changes helper's signature and its one caller.
AFTER = {
    "pkg/core.py": "def helper(x, step=1):\n    return x + step\n\n\ndef unused():\n    return 0\n",
    "pkg/use.py": (
        "from pkg.core import helper\n\n\ndef caller(y):\n    return helper(y, 2)\n"
    ),
}


@unittest.skipUnless(READY, "git and the Python grammar are required")
class ReplayHistoryTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }

        def git(*args):
            subprocess.run(
                ["git", "-C", str(self.repo), *args],
                check=True,
                capture_output=True,
                env=env,
            )

        git("init", "-q")
        for files, message in ((BEFORE, "before"), (AFTER, "after")):
            for name, text in files.items():
                path = self.repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, "utf-8")
            git("add", "-A")
            git("commit", "-q", "-m", message)

    def test_touched_lines_are_parent_lines(self):
        import replay_history

        lines = replay_history.touched_lines(self.repo, "HEAD^", "HEAD", "pkg/core.py")
        self.assertEqual(lines, {1, 2})

    def test_scores_impact_and_search(self):
        import replay_history

        report = replay_history.replay(self.repo, "python", count=5)
        self.assertEqual(report["cases"], 2)
        cases = {c["file"]: c for c in report["per_case"]}
        # helper's dependent caller is in use.py, which the commit edited.
        self.assertEqual(cases["pkg/core.py"]["impact"], {"predicted": 1, "hits": 1})
        # Search for "helper" also finds use.py; other.py has helper only
        # inside a longer word, which is not a whole-word match.
        self.assertEqual(cases["pkg/core.py"]["search"], {"predicted": 1, "hits": 1})
        # Nothing depends on caller; nothing else names it.
        self.assertEqual(cases["pkg/use.py"]["impact"], {"predicted": 0, "hits": 0})
        self.assertEqual(report["impact"]["recall"], 0.5)
        self.assertEqual(report["impact"]["precision"], 1.0)


if __name__ == "__main__":
    unittest.main()
