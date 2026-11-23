"""The attribution check flags AI identities and trailers, not other text."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import check_attribution  # noqa: E402

HUMAN = ("Dana", "dana@example.invalid")


@unittest.skipUnless(shutil.which("git"), "git is required")
class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo)
        self.git("init", "-q")
        self.commit("base")

    def git(self, *args, who=HUMAN):
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": who[0],
            "GIT_AUTHOR_EMAIL": who[1],
            "GIT_COMMITTER_NAME": who[0],
            "GIT_COMMITTER_EMAIL": who[1],
        }
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        ).stdout.strip()

    def commit(self, message, who=HUMAN):
        self.git("commit", "-q", "--allow-empty", "-m", message, who=who)
        return self.git("rev-parse", "HEAD")

    def test_flags_identity_and_trailers(self):
        cases = {
            "author": self.commit("fix", who=("Claude", "noreply@anthropic.com")),
            "co-author": self.commit(
                "fix\n\nCo-Authored-By: Claude <noreply@anthropic.com>"
            ),
            "session": self.commit("fix\n\nClaude-Session: https://example.invalid"),
            "generated": self.commit("fix\n\nGenerated with Claude Code"),
        }
        failures = check_attribution.check(self.repo, ["HEAD~4..HEAD"])
        self.assertEqual(set(failures), set(cases.values()))

    def test_allows_other_mentions(self):
        self.commit("feat(mcp): expose impact queries to Claude clients")
        self.commit("fix\n\nReviewed-by: Dana <dana@example.invalid>")
        self.assertEqual(check_attribution.check(self.repo, ["HEAD~2..HEAD"]), {})
        self.assertEqual(
            check_attribution.main(["HEAD~2..HEAD", "--repo", str(self.repo)]), 0
        )


if __name__ == "__main__":
    unittest.main()
