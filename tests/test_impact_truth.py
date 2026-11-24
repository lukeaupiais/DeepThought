"""Impact ground truth: oracle callers of the declarations commits edited."""

import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import impact_truth  # noqa: E402

READY = (
    shutil.which("git")
    and importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_python")
)

A = (
    "def base(x):\n    return x\n\n\n"
    "def mid(x):\n    return base(x)\n\n\n"
    "def top(x):\n    return mid(x)\n"
)
B = "from pkg.a import top\n\n\ndef user():\n    return top(1)\n"
# Lines 1-based; ranges in the index are 0-based [line, start, end] and
# enclosing [start_line, start_col, end_line, end_col].
DEFINITIONS = {
    "pkg/a.py": [("base", 0, 1), ("mid", 4, 5), ("top", 8, 9)],
    "pkg/b.py": [("user", 3, 4)],
}
CALLS = (
    "# file\tline\tname\ttarget_file\ttarget_line\tkind\n"
    "# indexed\tpkg/a.py\n# indexed\tpkg/b.py\n"
    "pkg/a.py\t6\tbase\tpkg/a.py\t1\tcallable\n"
    "pkg/a.py\t10\tmid\tpkg/a.py\t5\tcallable\n"
    "pkg/b.py\t5\ttop\tpkg/a.py\t9\tcallable\n"
)


def index_bytes():
    from deepthought._scip import scip_pb2

    index = scip_pb2.Index()
    for path, definitions in DEFINITIONS.items():
        document = index.documents.add(relative_path=path)
        module = path[:-3].replace("/", ".")
        for name, first, last in definitions:
            document.occurrences.add(
                range=[first, 4, 4 + len(name)],
                symbol=f"scip-python python t 0 `{module}`/{name}().",
                symbol_roles=1,
                enclosing_range=[first, 0, last, 16],
            )
    return index.SerializeToString()


class BlockTests(unittest.TestCase):
    def test_python_block_spans_a_multiline_signature(self):
        lines = [
            "def f(",
            "    a,",
            ") -> int:",
            "    x = 1",
            "",
            "    # note",
            "    return x",
            "def g():",
        ]
        self.assertEqual(impact_truth._python_block(lines, 1), 7)


@unittest.skipUnless(READY, "git and the Python grammar are required")
class ImpactTruthTests(unittest.TestCase):
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
        # The second commit edits base's body and user's body.
        for a, b, message in (
            (A, B, "first"),
            (A.replace("return x\n", "return x + 0\n", 1), B.replace("1", "2"), "edit"),
        ):
            (self.repo / "pkg").mkdir(exist_ok=True)
            (self.repo / "pkg/a.py").write_text(a, "utf-8")
            (self.repo / "pkg/b.py").write_text(b, "utf-8")
            git("add", "-A")
            git("commit", "-q", "-m", message)
        self.index = self.repo.parent / f"{self.repo.name}.scip"
        self.index.write_bytes(index_bytes())
        self.addCleanup(self.index.unlink)
        self.calls = self.repo.parent / f"{self.repo.name}.tsv"
        self.calls.write_text(CALLS, "utf-8")
        self.addCleanup(self.calls.unlink)

    def derive(self):
        rows, aliases = impact_truth.derive(
            self.index, self.calls, self.repo, "python", "HEAD", commits=5
        )
        stream = io.StringIO()
        impact_truth.write(rows, stream, aliases)
        path = self.repo.parent / f"{self.repo.name}.impact.tsv"
        path.write_text(stream.getvalue(), "utf-8")
        self.addCleanup(path.unlink)
        return impact_truth.read(path)

    def test_seeds_are_edited_callables_and_dependents_their_callers(self):
        truth = self.derive()
        self.assertEqual(
            truth,
            {
                ("pkg/a.py", 1, "base"): {
                    ("pkg/a.py", 1, "base"): 0,
                    ("pkg/a.py", 5, "mid"): 1,
                    ("pkg/a.py", 9, "top"): 2,
                    ("pkg/b.py", 4, "user"): 3,
                },
                ("pkg/b.py", 4, "user"): {("pkg/b.py", 4, "user"): 0},
            },
        )

    def test_engine_reaches_every_oracle_caller(self):
        import deepthought

        result = deepthought.analyze_repository(
            self.repo, repository_id="t", languages=("python",)
        )
        report, _ = impact_truth.score(result, self.derive())
        self.assertEqual(
            (report["seeds"], report["expected"], report["confirmed"]), (2, 3, 3)
        )
        self.assertEqual((report["missed"], report["extra"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
