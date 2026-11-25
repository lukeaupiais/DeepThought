import difflib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from deepthought.edit_check import TOP_LEVEL, check_edit, parse_diff


def _diff(path, before, after):
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            f"a/{path}",
            f"b/{path}",
        )
    )


def _write(root, files):
    for path, text in files.items():
        target = Path(root) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def _typescript():
    configured = os.environ.get("DEEPTHOUGHT_TYPESCRIPT")
    if configured:
        return configured
    npm = shutil.which("npm")
    if npm is None:
        return None
    root = subprocess.run(
        [npm, "root", "-g"], capture_output=True, text=True, check=False
    ).stdout.strip()
    for candidate in (
        Path(root) / "typescript",
        Path(root) / "@sourcegraph" / "scip-typescript" / "node_modules" / "typescript",
    ):
        if (candidate / "lib" / "tsc.js").is_file():
            return str(candidate)
    return None


class ParseDiffTest(unittest.TestCase):
    def test_maps_edited_lines_to_unedited_lines(self):
        before = "".join(f"line {n}\n" for n in range(1, 21))
        lines = before.splitlines(keepends=True)
        lines[4] = "changed 5\n"  # replace line 5
        lines.insert(10, "new\n")  # insert before line 11
        del lines[15]  # remove line 15 (index shifted by the insert)
        edit = parse_diff(_diff("f.txt", before, "".join(lines)))[0]
        self.assertEqual(edit.path, "f.txt")
        self.assertEqual(edit.unedited_line(1), (1, False))
        self.assertEqual(edit.unedited_line(5), (None, True))
        self.assertEqual(edit.unedited_line(10), (10, False))
        self.assertEqual(edit.unedited_line(11), (None, True))
        self.assertEqual(edit.unedited_line(12), (11, False))
        self.assertEqual(edit.unedited_line(15), (14, False))
        self.assertEqual(edit.unedited_line(16), (16, False))
        self.assertEqual(edit.unedited_line(20), (20, False))

    def test_new_file_lines_are_written_by_the_edit(self):
        edit = parse_diff("--- /dev/null\n+++ b/new.go\n@@ -0,0 +1,2 @@\n+a\n+b\n")[0]
        self.assertIsNone(edit.old_path)
        self.assertEqual(edit.unedited_line(2), (None, True))

    def test_rejects_paths_outside_the_workspace(self):
        with self.assertRaises(ValueError):
            parse_diff("--- a/../x.go\n+++ b/../x.go\n@@ -1 +1 @@\n-a\n+b\n")
        with self.assertRaises(ValueError):
            parse_diff("not a diff")


@unittest.skipUnless(shutil.which("go"), "needs the go toolchain")
class GoEditTest(unittest.TestCase):
    def test_reports_callers_a_signature_change_breaks(self):
        lib = "package calc\n\nfunc Add(a, b int) int {\n\treturn a + b\n}\n"
        files = {
            "go.mod": "module example.com/calc\n\ngo 1.21\n",
            "calc.go": lib,
            "use.go": (
                "package calc\n\n"
                "func Total(xs []int) int {\n"
                "\tt := 0\n"
                "\tfor _, x := range xs {\n"
                "\t\tt = Add(t, x)\n"
                "\t}\n"
                "\treturn t\n"
                "}\n\n"
                "var seed = Add(1, 2)\n"
            ),
            "calc_test.go": (
                'package calc\n\nimport "testing"\n\n'
                "func TestAdd(t *testing.T) {\n"
                "\tif Add(1, 2) != 3 {\n"
                '\t\tt.Fatal("add")\n'
                "\t}\n"
                "}\n"
            ),
        }
        edited = lib.replace("Add(a, b int) int", "Add(a, b int) (int, error)").replace(
            "return a + b", "return a + b, nil"
        )
        with tempfile.TemporaryDirectory() as root:
            _write(root, files)
            result = check_edit(root, _diff("calc.go", lib, edited))
        self.assertTrue(result["complete"], result["checks"])
        self.assertEqual(result["edit_diagnostics"], [])
        found = {(b["file"], b["line"], b["declaration"]) for b in result["breaks"]}
        self.assertEqual(
            found,
            {
                ("use.go", 6, "Total"),
                ("use.go", 11, TOP_LEVEL),
                ("calc_test.go", 6, "TestAdd"),
            },
        )

    def test_cli_exit_code_says_whether_the_edit_breaks(self):
        import contextlib
        import io
        import json

        from deepthought.cli import main

        lib = "package calc\n\nfunc Add(a, b int) int {\n\treturn a + b\n}\n"
        use = "package calc\n\nfunc Twice(a int) int {\n\treturn Add(a, a)\n}\n"
        with tempfile.TemporaryDirectory() as root:
            _write(
                root,
                {
                    "go.mod": "module example.com/calc\n\ngo 1.21\n",
                    "calc.go": lib,
                    "use.go": use,
                },
            )
            breaking = Path(root) / "breaking.diff"
            breaking.write_text(
                _diff("calc.go", lib, lib.replace("a, b int", "a, b, c int"))
            )
            harmless = Path(root) / "harmless.diff"
            harmless.write_text(_diff("calc.go", lib, "// Package calc adds.\n" + lib))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["check-edit", root, str(breaking)]), 1)
                self.assertEqual(main(["check-edit", root, str(harmless)]), 0)
            first = json.loads(out.getvalue().splitlines()[0])
            self.assertEqual(
                [(b["file"], b["line"], b["declaration"]) for b in first["breaks"]],
                [("use.go", 4, "Twice")],
            )
            with contextlib.redirect_stderr(io.StringIO()):
                bad = Path(root) / "bad.diff"
                bad.write_text("--- a/none.go\n+++ b/none.go\n@@ -1 +1 @@\n-x\n+y\n")
                self.assertEqual(main(["check-edit", root, str(bad)]), 2)

    def test_mistakes_in_the_edit_are_not_breaks(self):
        lib = "package calc\n\nfunc Add(a, b int) int {\n\treturn a + b\n}\n"
        with tempfile.TemporaryDirectory() as root:
            _write(
                root, {"go.mod": "module example.com/calc\n\ngo 1.21\n", "calc.go": lib}
            )
            result = check_edit(
                root, _diff("calc.go", lib, lib.replace("a + b", "a + c"))
            )
        self.assertEqual(result["breaks"], [])
        self.assertEqual([d["line"] for d in result["edit_diagnostics"]], [4])

    def test_existing_errors_are_not_breaks(self):
        lib = "package calc\n\nfunc Add(a, b int) int {\n\treturn a + b\n}\n"
        broken = "package calc\n\nfunc Broken() int {\n\treturn missing\n}\n"
        with tempfile.TemporaryDirectory() as root:
            _write(
                root,
                {
                    "go.mod": "module example.com/calc\n\ngo 1.21\n",
                    "calc.go": lib,
                    "broken.go": broken,
                },
            )
            result = check_edit(
                root, _diff("calc.go", lib, "// Package calc adds.\n" + lib)
            )
        self.assertEqual(result["breaks"], [])
        self.assertGreater(result["checks"][0]["diagnostics_before"], 0)


@unittest.skipUnless(
    shutil.which("node") and _typescript(), "needs node and typescript"
)
class TypeScriptEditTest(unittest.TestCase):
    def test_reports_callers_in_the_project(self):
        lib = (
            "export function greet(name: string): string {\n  return `hi ${name}`;\n}\n"
        )
        files = {
            "tsconfig.json": '{\n  // strict\n  "compilerOptions": {"strict": true, "target": "es2020"},\n  "include": ["src"]\n}\n',
            "src/greet.ts": lib,
            "src/app.ts": (
                'import { greet } from "./greet";\n\n'
                "export class App {\n"
                "  run(): string {\n"
                '    return greet("a");\n'
                "  }\n"
                "}\n"
            ),
        }
        edited = lib.replace("name: string", "name: string, loud: boolean")
        previous = os.environ.get("DEEPTHOUGHT_TYPESCRIPT")
        os.environ["DEEPTHOUGHT_TYPESCRIPT"] = _typescript()
        try:
            with tempfile.TemporaryDirectory() as root:
                _write(root, files)
                result = check_edit(root, _diff("src/greet.ts", lib, edited))
        finally:
            if previous is None:
                os.environ.pop("DEEPTHOUGHT_TYPESCRIPT")
            else:
                os.environ["DEEPTHOUGHT_TYPESCRIPT"] = previous
        self.assertTrue(result["complete"], result["checks"])
        self.assertEqual(
            [
                (b["file"], b["line"], b["declaration"], b["code"])
                for b in result["breaks"]
            ],
            [("src/app.ts", 5, "App.run", "TS2554")],
        )


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_PYRIGHT") or shutil.which("pyright"), "needs pyright"
)
class PythonEditTest(unittest.TestCase):
    def test_reports_callers_in_the_project(self):
        lib = "def area(width: float, height: float) -> float:\n    return width * height\n"
        files = {
            "pyproject.toml": '[project]\nname = "shapes"\nversion = "0"\n',
            "shapes/__init__.py": "",
            "shapes/geometry.py": lib,
            "shapes/report.py": (
                "from shapes.geometry import area\n\n\n"
                "class Report:\n"
                "    def total(self) -> float:\n"
                "        return area(1.0, 2.0)\n"
            ),
        }
        edited = lib.replace("height: float)", "height: float, depth: float)")
        with tempfile.TemporaryDirectory() as root:
            _write(root, files)
            result = check_edit(root, _diff("shapes/geometry.py", lib, edited))
        self.assertTrue(result["complete"], result["checks"])
        self.assertEqual(
            [(b["file"], b["line"], b["declaration"]) for b in result["breaks"]],
            [("shapes/report.py", 6, "Report.total")],
        )


if __name__ == "__main__":
    unittest.main()
