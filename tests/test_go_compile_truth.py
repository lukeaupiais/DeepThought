import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import go_compile_truth  # noqa: E402


def module(files):
    root = Path(tempfile.mkdtemp())
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, "utf-8")
    return root


@unittest.skipUnless(shutil.which("go"), "needs the go toolchain")
class RenameAndCompileTests(unittest.TestCase):
    def setUp(self):
        self.root = module(
            {
                "go.mod": "module example.com/m\n\ngo 1.21\n",
                "r.go": (
                    "package m\n"
                    "\n"
                    "type Router interface {\n"
                    "\tMount(p string)\n"
                    "}\n"
                    "\n"
                    "type Mux struct{}\n"
                    "\n"
                    "func (m *Mux) Mount(p string) {}\n"
                    "\n"
                    "var _ Router = &Mux{}\n"
                    "\n"
                    "func helper() int { return 1 }\n"
                    "\n"
                    "func unused() {}\n"
                ),
                # A test file in the package that fails to build.
                "r_test.go": (
                    'package m\n\nfunc mounted() { m := &Mux{}; m.Mount("/") }\n'
                ),
                # Another package uses helper through an exported wrapper.
                "w.go": "package m\n\nfunc Wrap() int { return helper() }\n",
                "sub/s.go": (
                    "package sub\n"
                    "\n"
                    'import "example.com/m"\n'
                    "\n"
                    "func Use() int { return m.Wrap() }\n"
                ),
            }
        )
        self.addCleanup(shutil.rmtree, self.root)
        self.out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.out)
        self.pkgs = go_compile_truth.packages(self.root)

    def rows(self, file, line, name):
        return go_compile_truth.rename_and_compile(
            self.root, self.out, self.pkgs, file, line, name
        )

    def test_a_method_reports_test_uses_and_interface_assignments(self):
        rows = self.rows("r.go", 9, "Mount")
        self.assertIn(("r.go", 9, "Mount", "r_test.go", 3, "reference"), rows)
        self.assertIn(("r.go", 9, "Mount", "r.go", 11, "implements"), rows)
        # sub imports the package left failing, so it was never compiled.
        self.assertIn(("r.go", 9, "Mount", "sub/", 0, "blocked"), rows)

    def test_renamed_uses_let_importers_compile(self):
        rows = self.rows("w.go", 3, "Wrap")
        self.assertEqual(rows, [("w.go", 3, "Wrap", "sub/s.go", 5, "reference")])

    def test_an_unreferenced_function_compiles_cleanly(self):
        self.assertEqual(
            self.rows("r.go", 15, "unused"),
            [("r.go", 15, "unused", "-", 0, "reference")],
        )

    def test_the_tree_is_restored(self):
        before = (self.root / "r.go").read_text("utf-8")
        self.rows("r.go", 13, "helper")
        self.assertEqual((self.root / "r.go").read_text("utf-8"), before)


class CompareTests(unittest.TestCase):
    def test_calls_are_scored_against_references(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        compile_tsv = root / "compile.tsv"
        compile_tsv.write_text(
            go_compile_truth.HEADER
            + "\n"
            + "\n".join(
                "\t".join(map(str, r))
                for r in [
                    ("r.go", 9, "Mount", "a.go", 4, "reference"),
                    ("r.go", 9, "Mount", "r.go", 11, "implements"),
                    ("r.go", 9, "Mount", "sub/", 0, "blocked"),
                ]
            )
            + "\n",
            "utf-8",
        )
        calls_tsv = root / "calls.tsv"
        calls_tsv.write_text(
            "a.go\t4\tMount\tr.go\t9\tcallable\n"
            "b.go\t7\tMount\tr.go\t9\tcallable\n"
            "sub/c.go\t2\tMount\tr.go\t9\tcallable\n"
            "a.go\t5\tother\tz.go\t1\tcallable\n",
            "utf-8",
        )
        report = go_compile_truth.compare(compile_tsv, calls_tsv)
        self.assertEqual(
            {k: report[k] for k in ("sampled", "scip_calls", "confirmed", "blocked")},
            {"sampled": 1, "scip_calls": 2, "confirmed": 1, "blocked": 1},
        )
        self.assertEqual(report["not_a_reference"], [["r.go", 9, "b.go", 7]])
        self.assertEqual(report["references_without_scip_call"], 0)

    def test_blocked_trees_and_files_are_not_judged(self):
        blocked = {"sub/", "./", "e2e/**", "x/y.ts"}
        judged = {
            p: not go_compile_truth._blocked(p, blocked)
            for p in (
                "a.go",
                "sub/c.go",
                "sub/deep/c.go",
                "e2e/a/b.ts",
                "x/y.ts",
                "x/z.ts",
            )
        }
        self.assertEqual(
            judged,
            {
                "a.go": False,
                "sub/c.go": False,
                "sub/deep/c.go": True,
                "e2e/a/b.ts": False,
                "x/y.ts": False,
                "x/z.ts": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
