"""clang-derived C call truth."""

import io
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import c_call_truth  # noqa: E402
import ground_truth  # noqa: E402

MAIN = """#include <string.h>
#include "api.h"
#define CHECK(x) do { if (!(x)) return 1; } while (0)
#define CALL_HELPER(a) helper(a)
#define length strlen
static int helper(int a) { return a; }
int (*pointer)(int) = helper;
int main(void) {
  CHECK(helper(1));
  CALL_HELPER(2);
  pointer(3);
  (helper)(4);
  shared(5);
  twice(6);
  declared_only(7);
  return (int)length("x") + (int)strlen("y");
}
"""
API = """int shared(int);
int twice(int);
int declared_only(int);
"""
LIB = """#include "api.h"
static int helper(int a) { return a + 1; }
int shared(int a) { return helper(a); }
"""
COPY = """int twice(int a) { return a; }
"""


@unittest.skipUnless(shutil.which("clang"), "clang is required")
class CCallTruthTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        for name, text in {
            "main.c": MAIN,
            "api.h": API,
            "lib.c": LIB,
            "a/copy.c": COPY,
            "b/copy.c": COPY,
        }.items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(text)

    def test_calls_bind_by_linkage_and_skip_pointers_macros_and_copies(self):
        expected, indexed = c_call_truth.run(self.root)
        self.assertEqual(
            expected,
            {
                # Written in a macro's argument: recorded where it is written.
                ("main.c", 9, "helper"): {("main.c", 6, "callable")},
                ("main.c", 12, "helper"): {("main.c", 6, "callable")},
                ("main.c", 13, "shared"): {("lib.c", 3, "callable")},
                ("main.c", 16, "strlen"): {c_call_truth.EXTERNAL},
                # A static function binds in its own translation unit.
                ("lib.c", 3, "helper"): {("lib.c", 2, "callable")},
            },
        )
        # Left out: the call a macro body writes (line 10), the pointer call
        # (11), a function defined twice (14), one never defined (15) and a
        # macro that names the function (16, `length`).
        self.assertEqual(indexed, ["a/copy.c", "api.h", "b/copy.c", "lib.c", "main.c"])

    def test_units_with_errors_are_not_indexed(self):
        (self.root / "broken.c").write_text("int f(void) { return g(; }\n")
        expected, indexed = c_call_truth.run(self.root)
        self.assertNotIn("broken.c", indexed)

    def test_rows_read_back_as_call_truth(self):
        expected, indexed = c_call_truth.run(self.root)
        stream = io.StringIO()
        c_call_truth.write(expected, indexed, stream)
        path = self.root / "calls.tsv"
        path.write_text(stream.getvalue())
        read, read_indexed = ground_truth.read_truth(path)
        self.assertEqual(read, expected)
        self.assertEqual(read_indexed, set(indexed))


if __name__ == "__main__":
    unittest.main()
