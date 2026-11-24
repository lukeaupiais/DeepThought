"""clang-derived C and C++ declaration truth and its scoring."""

import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import c_declaration_truth  # noqa: E402

from deepthought import analyze_repository  # noqa: E402

READY = (
    shutil.which("clang")
    and importlib.util.find_spec("tree_sitter_c")
    and importlib.util.find_spec("tree_sitter_cpp")
)
C_SOURCE = """struct S { int x; };
struct Forward;
typedef struct S T;
enum E { A };
static int g(int x) { return x; }
int f(void) { return g(1); }
int h(int);
"""
CPP_SOURCE = """namespace n {
class C {
 public:
  C();
  void m();
  int k() { return 1; }
};
void C::m() {
  struct Local {};
}
template <typename T>
T id(T x) { return x; }
template <typename T> class Box {
 public:
  Box() {}
};
}
int main() { n::Box<int> b; return n::id(0); }
"""
EXPECTED = {
    ("a.c", 1, "S", "type", 1),
    ("a.c", 3, "T", "alias", 3),
    ("a.c", 4, "E", "type", 4),
    ("a.c", 5, "g", "function", 5),
    ("a.c", 6, "f", "function", 6),
    ("a.c", 7, "h", "function", 7),
    ("b.cpp", 1, "n", "namespace", 1),
    ("b.cpp", 2, "C", "type", 2),
    ("b.cpp", 4, "C", "method", 4),
    ("b.cpp", 5, "m", "method", 5),
    ("b.cpp", 6, "k", "method", 6),
    ("b.cpp", 8, "m", "method", 8),
    ("b.cpp", 12, "id", "function", 11),
    ("b.cpp", 13, "Box", "type", 13),
    ("b.cpp", 15, "Box", "method", 15),
    ("b.cpp", 18, "main", "function", 18),
}


@unittest.skipUnless(READY, "clang and the C/C++ grammars are required")
class CDeclarationTruthTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "a.c").write_text(C_SOURCE)
        (self.root / "b.cpp").write_text(CPP_SOURCE)

    def test_named_declarations_without_locals_forwards_or_instances(self):
        rows, indexed = c_declaration_truth.run(self.root)
        self.assertEqual(indexed, ["a.c", "b.cpp"])
        self.assertEqual(set(rows), EXPECTED)

    def test_round_trip_and_score(self):
        rows, indexed = c_declaration_truth.run(self.root)
        path = self.root / "truth.tsv"
        with path.open("w") as stream:
            c_declaration_truth.write(rows, indexed, stream)
        truth = c_declaration_truth.read(path)
        self.assertEqual(truth, (set(rows), set(indexed)))
        path.unlink()
        result = analyze_repository(
            self.root, repository_id="t", languages=("c", "cpp")
        )
        report, _ = c_declaration_truth.score(result, truth)
        self.assertEqual(report["expected"], len(EXPECTED))
        self.assertEqual(
            report["found"] + report["kind_mismatch"] + report["missed"], len(EXPECTED)
        )
        self.assertGreaterEqual(report["found"], 12)


class ScoreTests(unittest.TestCase):
    def test_kind_mismatch_missed_and_extra(self):
        from types import SimpleNamespace as D

        result = D(
            definitions=[
                D(file="x.h", line=1, kind="Struct", symbol="S"),
                D(file="x.h", line=2, kind="Function", symbol="m"),
                D(file="x.h", line=9, kind="Function", symbol="stray"),
                D(file="other.h", line=1, kind="Function", symbol="ignored"),
            ]
        )
        truth = (
            {
                ("x.h", 1, "S", "type", 1),
                ("x.h", 2, "m", "method", 2),
                ("x.h", 3, "gone", "function", 3),
            },
            {"x.h"},
        )
        report, samples = c_declaration_truth.score(result, truth)
        self.assertEqual(
            {k: report[k] for k in ("found", "kind_mismatch", "missed", "extra")},
            {"found": 1, "kind_mismatch": 1, "missed": 1, "extra": 1},
        )
        self.assertEqual(samples["extra"], [("x.h", 9, "stray", "function")])


if __name__ == "__main__":
    unittest.main()
