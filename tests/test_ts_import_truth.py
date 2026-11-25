import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import ts_import_truth  # noqa: E402


def _node_ready():
    node = shutil.which("node")
    if node is None:
        return False
    probe = subprocess.run(
        [node, "-e", "require('enhanced-resolve'); require('@babel/parser')"],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


class TruthShapeTests(unittest.TestCase):
    def test_external_only_when_nothing_resolves(self):
        expected, errors = ts_import_truth.truth(
            [
                {"file": "a.ts", "line": 1, "spec": "./b", "targets": ["b.ts"]},
                {"file": "a.ts", "line": 2, "spec": "x", "targets": []},
                {"file": "a.ts", "line": 3, "spec": "./c", "targets": ["c.ts"]},
                {"file": "a.ts", "line": 3, "spec": "y", "targets": []},
                {"file": "bad.ts", "error": "Unexpected token"},
            ]
        )
        self.assertEqual(expected[("a.ts", 1, "import")], {("b.ts", 0, "module")})
        self.assertEqual(expected[("a.ts", 2, "import")], {("-", 0, "external")})
        self.assertEqual(expected[("a.ts", 3, "import")], {("c.ts", 0, "module")})
        self.assertEqual(errors, ["bad.ts"])


@unittest.skipUnless(_node_ready(), "node with enhanced-resolve and @babel/parser")
class OracleTests(unittest.TestCase):
    def test_typescript_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {
                "tsconfig.json": (
                    "{\n  // comments are allowed\n"
                    '  "compilerOptions": {"baseUrl": ".", "paths": {"@lib/*": ["lib/*"]},},\n}\n'
                ),
                "src/a.ts": (
                    "import { b } from './b.js'\n"
                    "import { c } from '@lib/c'\n"
                    "export * from 'pkg'\n"
                    "const d = await import('./d')\n"
                    "import e = require('./e')\n"
                ),
                "src/b.ts": "export const b = 1\n",
                "lib/c.ts": "export const c = 1\n",
                "src/d.d.ts": "export {}\n",
                "src/d.js": "export {}\n",
                "src/e.tsx": "export = 1\n",
            }
            for name, text in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text)
            rows = ts_import_truth.resolve(root, ["src/a.ts"])
        got = {(r["line"], r["spec"]): r["targets"] for r in rows}
        self.assertEqual(
            got,
            {
                (1, "./b.js"): ["src/b.ts"],
                (2, "@lib/c"): ["lib/c.ts"],
                (3, "pkg"): [],
                (4, "./d"): ["src/d.d.ts", "src/d.js"],
                (5, "./e"): ["src/e.tsx"],
            },
        )


if __name__ == "__main__":
    unittest.main()
