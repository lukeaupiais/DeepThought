import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import py_check_truth  # noqa: E402

MYPY = shutil.which("mypy")

FILES = {
    # A module __getattr__ makes a missing attribute an object, so the error
    # at `pkg.twice(1)` names no name.
    "src/pkg/__init__.py": (
        "from .util import twice as twice\n"
        "\n"
        "\n"
        "def __getattr__(name: str) -> object:\n"
        "    return object()\n"
    ),
    "src/pkg/util.py": (
        "import typing as t\n"
        "\n"
        "\n"
        "def twice(n: int) -> int:\n"
        "    return n * 2\n"
        "\n"
        "\n"
        "def unused() -> None:\n"
        "    pass\n"
        "\n"
        "\n"
        "@t.overload\n"
        "def pick(x: int) -> int: ...\n"
        "@t.overload\n"
        "def pick(x: str) -> str: ...\n"
        "def pick(x: t.Any) -> t.Any:\n"
        "    return x\n"
    ),
    "src/pkg/shapes.py": (
        "class Base:\n"
        "    def area(self) -> int:\n"
        "        return 0\n"
        "\n"
        "\n"
        "class Square(Base):\n"
        "    def area(self) -> int:\n"
        "        return 1\n"
        "\n"
        "    def side(self) -> int:\n"
        "        return 1\n"
        "\n"
        "    class Inner(Base):\n"
        "        pass\n"
    ),
    "tests/test_a.py": (
        "import pkg\n"
        "from pkg.util import (\n"
        "    twice as double,\n"
        "    pick,\n"
        ")\n"
        "from pkg.shapes import Square\n"
        "\n"
        "\n"
        "def test_it(untyped):\n"
        "    assert pkg.twice(1) == double(1)\n"
        "    assert pick(1) == 1\n"
        "    assert Square().side() == untyped.side()\n"
    ),
}


@unittest.skipUnless(MYPY, "needs mypy")
class RenameAndCheckTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        for name, text in FILES.items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(text, "utf-8")
        self.cache = self.root / ".cache"
        self.targets = ["src", "tests"]
        self.before = py_check_truth.type_errors(
            self.root, MYPY, self.targets, self.cache
        )

    def rows(self, file, line, name):
        return [
            r[3:]
            for r in py_check_truth.rename_and_check(
                self.root, MYPY, self.targets, self.cache, self.before, file, line, name
            )
        ]

    def test_uses_are_followed_through_re_exports_and_aliases(self):
        self.assertEqual(
            self.rows("src/pkg/util.py", 4, "twice"),
            [
                ("src/pkg/__init__.py", 1, "reference"),
                ("tests/test_a.py", 2, "reference"),
                ("tests/test_a.py", 10, "reference"),
            ],
        )

    def test_an_overload_is_renamed_with_its_siblings(self):
        self.assertEqual(
            self.rows("src/pkg/util.py", 13, "pick"),
            [("tests/test_a.py", 2, "reference"), ("tests/test_a.py", 11, "reference")],
        )

    def test_an_unreferenced_function_adds_no_error_and_the_tree_is_restored(self):
        before = {p: p.read_text("utf-8") for p in self.root.rglob("*.py")}
        self.assertEqual(
            self.rows("src/pkg/util.py", 8, "unused"), [("-", 0, "reference")]
        )
        self.rows("src/pkg/util.py", 4, "twice")
        self.assertEqual(
            {p: p.read_text("utf-8") for p in self.root.rglob("*.py")}, before
        )

    def test_an_override_is_told_apart_from_a_new_method(self):
        args = (self.root, MYPY, self.targets, self.cache, "src/pkg/shapes.py")
        self.assertTrue(py_check_truth.overrides(*args, 7))
        self.assertFalse(py_check_truth.overrides(*args, 10))
        self.assertFalse(py_check_truth.overrides(*args, 2))
        self.assertFalse(py_check_truth.overrides(*args, 13))

    def test_lines_through_untyped_values_are_imprecise(self):
        report = self.root / ".report"
        py_check_truth.type_errors(self.root, MYPY, self.targets, self.cache, report)
        lines = py_check_truth.imprecise_lines(report)
        self.assertIn(("tests/test_a.py", 12), lines)
        self.assertNotIn(("tests/test_a.py", 11), lines)


if __name__ == "__main__":
    unittest.main()
