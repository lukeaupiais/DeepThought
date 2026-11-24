"""The mutation operators of tools/mutate_resolution.py change one thing."""

import ast
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import mutate_resolution  # noqa: E402

SOURCE = """def f(a, b, xs):
    if a == b and not xs:
        return a in xs
    return a < b or b is None
"""


EXPECTED = [
    ("a == b and not xs", "a == b or not xs"),
    ("a == b", "a != b"),
    ("not xs", "xs"),
    ("a in xs", "a not in xs"),
    ("a < b or b is None", "a < b and b is None"),
    ("a < b", "a <= b"),
    ("b is None", "b is not None"),
]


def norm(expression):
    return ast.dump(ast.parse(expression, mode="eval"))


class MutationOperatorTest(unittest.TestCase):
    def test_each_mutant_changes_one_operator(self):
        sites = mutate_resolution.sites(ast.parse(SOURCE))
        mutants = [mutate_resolution.mutant(SOURCE, i) for i in range(len(sites))]
        # Compared as parsed expressions: unparse spacing varies by version.
        self.assertEqual(
            [(norm(m[1]["before"]), norm(m[1]["after"])) for m in mutants],
            [(norm(a), norm(b)) for a, b in EXPECTED],
        )
        original = ast.dump(ast.parse(SOURCE))
        for tree, description in mutants:
            self.assertNotEqual(ast.dump(tree), original)
            self.assertEqual(description["function"], "f")
            compile(tree, "<mutant>", "exec")

    def test_line_ranges(self):
        self.assertEqual(mutate_resolution.line_range("12-30"), (12, 30))
        self.assertEqual(mutate_resolution.line_range("7"), (7, 7))


if __name__ == "__main__":
    unittest.main()
