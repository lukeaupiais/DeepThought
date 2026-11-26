"""tools/ts_member_dispatch.cjs: calls through a member of a constant object
literal, listed as possible calls for the impact oracle."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from deepthought.indexers import INDEXERS, locate

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "ts_member_dispatch.cjs"


def typescript():
    """(node, lib/typescript.js) of the TypeScript the pinned scip-typescript
    installs, or (None, None)."""
    node, indexer = shutil.which("node"), locate(INDEXERS["scip-typescript"])
    if node is None or indexer is None:
        return None, None
    found = subprocess.run(
        [
            node,
            "-e",
            "process.stdout.write(require.resolve('typescript', "
            "{paths: [require('path').dirname(require('fs').realpathSync(process.argv[1]))]}))",
            indexer,
        ],
        capture_output=True,
        text=True,
    )
    return (node, found.stdout) if found.returncode == 0 else (None, None)


NODE, TYPESCRIPT = typescript()

SOURCES = {
    "detect.ts": """\
export const fromQuery = (q: string) => q
export function fromCookie(q: string) {
  return q
}
""",
    "main.ts": """\
import { fromQuery, fromCookie } from './detect'
export const detectors = {
  query: fromQuery,
  cookie: fromCookie,
} as const
let mutable = { query: fromQuery }
const spread = { ...detectors }

export function detect(name: keyof typeof detectors) {
  const detector = detectors[name]
  detector('a')
  detectors[name]('b')
  detectors['query']('c')
  mutable[name as 'query']('d')
  spread[name]('e')
}
""",
}


@unittest.skipUnless(NODE, "Provision the pinned scip-typescript")
class MemberDispatchTests(unittest.TestCase):
    def rows(self):
        with tempfile.TemporaryDirectory() as root:
            for name, text in SOURCES.items():
                Path(root, name).write_text(text)
            out = subprocess.run(
                [NODE, str(SCRIPT), TYPESCRIPT, root],
                input="detect.ts\nmain.ts\n",
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        return [line.split("\t") for line in out.splitlines()[1:]]

    def test_each_member_is_a_possible_target_of_a_dynamic_key(self):
        self.assertEqual(
            self.rows(),
            [
                # Through a const that holds the member, and directly.
                ["main.ts", "11", "detector", "detect.ts", "1", "callable"],
                ["main.ts", "11", "detector", "detect.ts", "2", "callable"],
                ["main.ts", "12", "detectors", "detect.ts", "1", "callable"],
                ["main.ts", "12", "detectors", "detect.ts", "2", "callable"],
                # A literal key (line 13), an object that is not a const
                # (14) and one built with a spread (15) give no row.
            ],
        )


if __name__ == "__main__":
    unittest.main()
