"""tools/ts_callback_regions.cjs: anonymous callbacks outside every named
callable, listed as callers for the impact oracle."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_ts_member_dispatch import NODE, TYPESCRIPT

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "ts_callback_regions.cjs"
sys.path.insert(0, str(ROOT / "tools"))
import impact_truth  # noqa: E402

SOURCE = """\
import { add } from './add'
describe('add', () => {
  it('sums', () => {
    expect(add(1, 2)).toBe(3)
    act(() => add(0, 0))
  })
  it(`twice`, function () { add(add(1, 1), 1) })
})
export function helper(xs: number[]) {
  return xs.map((x) => add(x, 1))
}
setup(() => {})
"""


@unittest.skipUnless(NODE, "Provision the pinned scip-typescript")
class CallbackRegionTests(unittest.TestCase):
    def rows(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "add.test.ts").write_text(SOURCE)
            out = subprocess.run(
                [NODE, str(SCRIPT), TYPESCRIPT, root],
                input="add.test.ts\n",
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        return [line.split("\t") for line in out.splitlines()[1:]]

    def test_callbacks_outside_named_callables_are_regions(self):
        self.assertEqual(
            self.rows(),
            [
                ["add.test.ts", "2", "2", "8", 'describe("add")'],
                ["add.test.ts", "3", "3", "6", 'it("sums")'],
                ["add.test.ts", "5", "5", "5", "act(…)"],
                ["add.test.ts", "7", "7", "7", 'it("twice")'],
                # The map callback in helper (line 10) belongs to helper.
                ["add.test.ts", "12", "12", "12", "setup(…)"],
            ],
        )


class EnclosingRegionTests(unittest.TestCase):
    def test_a_callback_is_a_possible_callee_of_the_one_around_it(self):
        regions = [
            ("t.ts", 2, 2, 8),
            ("t.ts", 3, 3, 6),
            ("t.ts", 5, 5, 5),
            ("t.ts", 7, 7, 7),
            ("u.ts", 5, 5, 5),
        ]
        self.assertEqual(
            impact_truth.enclosing_regions(regions),
            {
                ("t.ts", 3): {("t.ts", 2)},
                ("t.ts", 5): {("t.ts", 3)},
                ("t.ts", 7): {("t.ts", 2)},
            },
        )

    def test_the_innermost_callback_holds_a_call(self):
        nodes = {("add.ts", 1): ("add", 1, 3)}
        regions = [("t.ts", 2, 2, 8), ("t.ts", 3, 3, 6), ("t.ts", 5, 5, 5)]
        calls = {
            ("t.ts", 4, "add"): {("add.ts", 1, "callable")},
            ("t.ts", 5, "add"): {("add.ts", 1, "callable")},
        }
        incoming = impact_truth.call_graph(nodes, calls, None, True, regions)
        self.assertEqual(incoming[("add.ts", 1)], {("t.ts", 3), ("t.ts", 5)})


if __name__ == "__main__":
    unittest.main()
