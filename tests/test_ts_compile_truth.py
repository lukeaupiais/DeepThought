import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import ts_compile_truth  # noqa: E402

TSC = shutil.which("tsc")

FILES = {
    "tsconfig.json": (
        '{"compilerOptions": {"strict": true, "noEmit": true, "target": "es2022",'
        ' "module": "nodenext"}, "include": ["src"]}'
    ),
    "package.json": '{"type": "module"}',
    "src/delay.ts": "export default function delay(ms: number): number {\n\treturn ms;\n}\n",
    "src/util.ts": (
        "export function twice(n: number): number {\n"
        "\treturn n * 2;\n"
        "}\n"
        "\n"
        "export function unused(): void {}\n"
    ),
    # A re-export, then an aliased import of it.
    "src/index.ts": "export {twice} from './util.js';\n",
    "src/a.ts": (
        "import {twice as double} from './index.js';\n"
        "import wait from './delay.js';\n"
        "\n"
        "export const x = double(2) + wait(1);\n"
    ),
    "src/over.ts": (
        "export class Req {\n"
        "\tget(key: string): string\n"
        "\tget(): string[]\n"
        "\tget(key?: string) {\n"
        "\t\treturn key ? key : [];\n"
        "\t}\n"
        "}\n"
        "\n"
        "export const all = new Req().get();\n"
    ),
    "src/shape.ts": (
        "interface Shape {\n"
        "\tarea(): number;\n"
        "}\n"
        "\n"
        "export class Square implements Shape {\n"
        "\tarea(): number {\n"
        "\t\treturn 1;\n"
        "\t}\n"
        "}\n"
        "\n"
        "export const s = new Square().area();\n"
    ),
}


@unittest.skipUnless(TSC and shutil.which("node"), "needs node and tsc")
class RenameAndCheckTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        for name, text in FILES.items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(text, "utf-8")
        self.tsc = os.path.realpath(TSC)
        self.before = ts_compile_truth.type_errors(self.root, self.tsc, "tsconfig.json")

    def rows(self, file, line, name):
        return ts_compile_truth.rename_and_check(
            self.root, self.tsc, "tsconfig.json", self.before, file, line, name
        )

    def test_uses_are_followed_through_re_exports_and_aliases(self):
        rows = self.rows("src/util.ts", 1, "twice")
        self.assertEqual(
            [r[3:] for r in rows],
            [
                ("src/a.ts", 1, "reference"),
                ("src/a.ts", 4, "reference"),
                ("src/index.ts", 1, "reference"),
            ],
        )

    def test_default_imports_are_followed_under_their_local_name(self):
        rows = self.rows("src/delay.ts", 1, "delay")
        self.assertEqual(
            [r[3:] for r in rows],
            [("src/a.ts", 2, "reference"), ("src/a.ts", 4, "reference")],
        )

    def test_a_method_reports_its_call_and_the_interface_it_implements(self):
        rows = self.rows("src/shape.ts", 6, "area")
        self.assertEqual(
            [r[3:] for r in rows],
            [("src/shape.ts", 5, "implements"), ("src/shape.ts", 11, "reference")],
        )

    def test_an_overload_is_renamed_with_its_siblings(self):
        rows = self.rows("src/over.ts", 3, "get")
        self.assertEqual([r[3:] for r in rows], [("src/over.ts", 9, "reference")])

    def test_an_unreferenced_function_adds_no_error_and_the_tree_is_restored(self):
        before = {p: p.read_text("utf-8") for p in self.root.rglob("*.ts")}
        self.assertEqual(
            self.rows("src/util.ts", 5, "unused"),
            [("src/util.ts", 5, "unused", "-", 0, "reference")],
        )
        self.rows("src/util.ts", 1, "twice")
        self.assertEqual(
            {p: p.read_text("utf-8") for p in self.root.rglob("*.ts")}, before
        )


if __name__ == "__main__":
    unittest.main()
