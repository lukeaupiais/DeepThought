import difflib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from deepthought.edit_check import (
    TOP_LEVEL,
    check_edit,
    check_working_copy,
    environment_reason,
    parse_diff,
    report_text,
    summary_line,
    working_copy_diff,
)


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

    def test_importers_are_checked_when_the_edited_package_breaks(self):
        # go build and go vet stop at a package with errors and never check
        # its importers or its tests.
        lib = "package calc\n\nfunc Add(a, b int) int {\n\treturn a + b\n}\n"
        files = {
            "go.mod": "module example.com/calc\n\ngo 1.21\n",
            "calc.go": lib,
            "own.go": "package calc\n\nfunc Inc(a int) int {\n\treturn Add(a, 1)\n}\n",
            "calc_test.go": (
                'package calc\n\nimport "testing"\n\n'
                "func TestAdd(t *testing.T) {\n\t_ = Add(1, 2)\n}\n"
            ),
            "app/app.go": (
                'package app\n\nimport "example.com/calc"\n\n'
                "func Run() int {\n\treturn calc.Add(2, 3)\n}\n"
            ),
            "_examples/demo/main.go": (
                'package main\n\nimport "example.com/calc"\n\n'
                "func main() { _ = calc.Add(1, 1) }\n"
            ),
        }
        edited = lib.replace("a, b int", "a, b, c int")
        with tempfile.TemporaryDirectory() as root:
            _write(root, files)
            result = check_edit(root, _diff("calc.go", lib, edited))
        self.assertEqual(
            {(b["file"], b["line"], b["declaration"]) for b in result["breaks"]},
            {
                ("own.go", 4, "Inc"),
                ("calc_test.go", 6, "TestAdd"),
                ("app/app.go", 6, "Run"),
            },
        )
        # No build reads _examples: it is listed, and the check is incomplete.
        self.assertEqual(result["unchecked_files"], ["_examples/demo/main.go"])
        self.assertFalse(result["complete"])

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

    def test_a_config_error_fails_the_check(self):
        # tsc stops before checking any file when its config does not load.
        lib = "export function greet(name: string): string {\n  return name;\n}\n"
        files = {
            "tsconfig.json": '{"extends": "./missing.json", "include": ["src"]}\n',
            "src/greet.ts": lib,
            "src/app.ts": 'import { greet } from "./greet";\nexport const a = greet("a");\n',
        }
        previous = os.environ.get("DEEPTHOUGHT_TYPESCRIPT")
        os.environ["DEEPTHOUGHT_TYPESCRIPT"] = _typescript()
        try:
            with tempfile.TemporaryDirectory() as root:
                _write(root, files)
                edited = lib.replace("name: string)", "name: string, x: number)")
                result = check_edit(root, _diff("src/greet.ts", lib, edited))
        finally:
            if previous is None:
                os.environ.pop("DEEPTHOUGHT_TYPESCRIPT")
            else:
                os.environ["DEEPTHOUGHT_TYPESCRIPT"] = previous
        self.assertEqual(result["checks"][0]["status"], "failed")
        self.assertIn("TS", result["checks"][0]["reason"])
        self.assertFalse(result["complete"])

    def _check(self, files, path, before, after):
        previous = os.environ.get("DEEPTHOUGHT_TYPESCRIPT")
        os.environ["DEEPTHOUGHT_TYPESCRIPT"] = _typescript()
        try:
            with tempfile.TemporaryDirectory() as root:
                _write(root, files)
                if before is None:
                    diff = "".join(
                        difflib.unified_diff(
                            [],
                            after.splitlines(keepends=True),
                            "/dev/null",
                            f"b/{path}",
                        )
                    )
                else:
                    diff = _diff(path, before, after)
                return check_edit(root, diff)
        finally:
            if previous is None:
                os.environ.pop("DEEPTHOUGHT_TYPESCRIPT")
            else:
                os.environ["DEEPTHOUGHT_TYPESCRIPT"] = previous

    def test_a_standard_library_that_does_not_load_is_not_a_check(self):
        # With only lib es5 tsc has no Promise: every type is suspect, so the
        # edited file is reported as not checked, never as "no breaks".
        lib = "export const load = () => Promise.resolve(1);\n"
        files = {
            "tsconfig.json": '{"compilerOptions": {"target": "es2017", "lib": ["es5"]}}\n',
            "src/load.ts": lib,
        }
        result = self._check(files, "src/load.ts", lib, lib.replace("(1)", "(2)"))
        self.assertEqual(result["checks"][0]["status"], "failed")
        self.assertIn("standard library", result["checks"][0]["reason"])
        self.assertEqual(result["verdict"], "not_fully_checked")
        self.assertFalse(result["files"]["src/load.ts"]["checked"])
        self.assertTrue(result["summary"].startswith("NOT CHECKED: src/load.ts"))
        self.assertNotIn("No breaks", report_text(result))

    def test_a_file_the_project_does_not_include_is_not_checked(self):
        lib = "export const a: number = 1;\n"
        files = {
            "tsconfig.json": '{"compilerOptions": {"strict": true}, "include": ["src"]}\n',
            "src/a.ts": lib,
            "scripts/run.ts": lib,
        }
        result = self._check(
            files, "scripts/run.ts", lib, 'export const a: number = "x";\n'
        )
        self.assertEqual(result["checks"][0]["status"], "checked")
        self.assertEqual(result["edit_diagnostics"], [])
        self.assertEqual(result["verdict"], "not_fully_checked")
        self.assertIn(
            "no TypeScript project includes it",
            result["files"]["scripts/run.ts"]["reason"],
        )
        self.assertIn("scripts/run.ts", result["unchecked_files"])

    def test_missing_test_runner_types_are_environment_not_the_edit(self):
        files = {
            "tsconfig.json": '{"compilerOptions": {"strict": true, "target": "es2020"}}\n',
            "src/a.ts": "export const add = (x: number, y: number) => x + y;\n",
        }
        test = (
            'import { add } from "./a";\n'
            'test("adds", () => {\n'
            "  expect(add(1, 2)).toBe(3);\n"
            "});\n"
            "const wrong: number = add(1, 2) + '';\n"
        )
        result = self._check(files, "src/a.test.ts", None, test)
        self.assertEqual(
            sorted(row["code"] for row in result["environment"]), ["TS2304", "TS2593"]
        )
        # The real mistake in the new file is still the edit's.
        self.assertEqual(
            [(r["line"], r["code"]) for r in result["edit_diagnostics"]],
            [(5, "TS2322")],
        )
        self.assertEqual(result["verdict"], "breaks")
        self.assertIn("not from the edit", result["summary"])

    def test_the_workspace_own_packages_resolve_without_node_modules(self):
        # The project extends a config from a sibling package of the
        # monorepo; nothing is installed, and no registry is asked for it.
        lib = "export function greet(name: string): string {\n  return name;\n}\n"
        files = {
            "package.json": '{"name": "acme", "private": true, "workspaces": ["packages/*", "app"]}\n',
            "packages/config/package.json": '{"name": "@acme/config", "version": "1.0.0"}\n',
            "packages/config/base.json": '{"compilerOptions": {"strict": true, "target": "es2020"}}\n',
            "app/package.json": '{"name": "app", "version": "1.0.0"}\n',
            "app/tsconfig.json": '{"extends": "@acme/config/base.json", "include": ["src"]}\n',
            "app/src/greet.ts": lib,
            "app/src/main.ts": 'import { greet } from "./greet";\nexport const x = greet("a");\n',
        }
        with tempfile.TemporaryDirectory() as cache:
            previous = os.environ.get("DEEPTHOUGHT_CACHE")
            os.environ["DEEPTHOUGHT_CACHE"] = cache
            try:
                result = self._check(
                    files,
                    "app/src/greet.ts",
                    lib,
                    lib.replace("name: string)", "name: string, n: number)"),
                )
            finally:
                if previous is None:
                    os.environ.pop("DEEPTHOUGHT_CACHE")
                else:
                    os.environ["DEEPTHOUGHT_CACHE"] = previous
        self.assertEqual(result["checks"][0]["status"], "checked", result["checks"])
        self.assertEqual(
            [(b["file"], b["line"], b["code"]) for b in result["breaks"]],
            [("app/src/main.ts", 2, "TS2554")],
        )
        self.assertTrue(
            any("@acme/config" in n for n in result["checks"][0]["notes"]),
            result["checks"][0]["notes"],
        )


class EnvironmentTest(unittest.TestCase):
    def test_imports_of_packages_not_installed_are_environment(self):
        self.assertEqual(
            environment_reason(
                "typescript",
                "TS2307",
                "Cannot find module 'react' or its corresponding type declarations.",
            ),
            "package react not installed",
        )
        # A relative import or a path alias that does not resolve is a
        # mistake in the code.
        self.assertIsNone(
            environment_reason("typescript", "TS2307", "Cannot find module './gone'.")
        )
        self.assertIsNone(
            environment_reason(
                "typescript", "TS2307", "Cannot find module '@/gone'.", aliases=["@/*"]
            )
        )
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "app").mkdir()
            self.assertEqual(
                environment_reason(
                    "python",
                    "reportMissingImports",
                    'Import "django.db" could not be resolved',
                    root=root,
                ),
                "package django not installed",
            )
            self.assertIsNone(
                environment_reason(
                    "python",
                    "reportMissingImports",
                    'Import "app.models" could not be resolved',
                    root=root,
                )
            )

    def test_the_summary_never_says_no_breaks_for_unchecked_files(self):
        report = {
            "breaks": [],
            "edit_diagnostics": [],
            "checks": [
                {
                    "language": "typescript",
                    "project": "web/tsconfig.json",
                    "status": "failed",
                    "reason": "x",
                }
            ],
            "files": {
                "web/a.tsx": {"checked": False, "reason": "TypeScript check failed: x"}
            },
            "unchecked_files": ["web/a.tsx"],
        }
        line = summary_line(report)
        self.assertTrue(line.startswith("NOT CHECKED: web/a.tsx"), line)
        self.assertNotIn("No breaks", line)


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_PYRIGHT") or shutil.which("pyright"), "needs pyright"
)
class PythonEditTest(unittest.TestCase):
    def test_reports_callers_in_the_project(self):
        lib = "def area(width: float, height: float) -> float:\n    return width * height\n"
        files = {
            "pyproject.toml": (
                '[project]\nname = "shapes"\nversion = "0"\n\n'
                '[tool.pyright]\ninclude = ["shapes"]\n'
            ),
            # Outside the configured include: still checked.
            "examples/demo.py": "from shapes.geometry import area\n\nprint(area(3.0, 4.0))\n",
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
            [
                ("examples/demo.py", 3, TOP_LEVEL),
                ("shapes/report.py", 6, "Report.total"),
            ],
        )


def _git(*args, cwd=None):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@unittest.skipUnless(shutil.which("go"), "needs the go toolchain")
class WorkingCopyTest(unittest.TestCase):
    """The agent edits the files in place: the check reads its edits from git."""

    lib = "package calc\n\nfunc Add(a, b int) int {\n\treturn a + b\n}\n"
    files = {
        "go.mod": "module example.com/calc\n\ngo 1.21\n",
        "calc.go": lib,
        "use.go": "package calc\n\nfunc Twice(x int) int {\n\treturn Add(x, x)\n}\n",
    }

    def _edit(self, root):
        _write(
            root,
            {
                "calc.go": self.lib.replace("a, b int", "a, b, c int"),
                # A new file the edit adds, with its own call.
                "more.go": "package calc\n\nvar three = Add(1, 1, 1)\n",
                # Installed packages are not edits.
                "node_modules/x/index.js": "module.exports = 1\n",
            },
        )

    def _assert_breaks(self, result):
        self.assertTrue(result["complete"], result["checks"])
        self.assertEqual(result["edited_files"], ["calc.go", "more.go"])
        self.assertEqual(result["edit_diagnostics"], [])
        self.assertEqual(
            [(b["file"], b["line"], b["declaration"]) for b in result["breaks"]],
            [("use.go", 4, "Twice")],
        )

    def test_checks_the_edits_in_the_repository(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, self.files)
            _git("init", "-q", cwd=root)
            _git("add", "-A", cwd=root)
            _git("commit", "-qm", "base", cwd=root)
            clean = check_working_copy(root)
            self.assertEqual(clean["breaks"], [])
            self.assertIn("no edits", clean["note"])
            self._edit(root)
            result = check_working_copy(root)
            # The repository's own index and files are untouched.
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn(" M calc.go", status)
            self.assertIn("?? more.go", status)
            self.assertIn("c int", (Path(root) / "calc.go").read_text())
        self._assert_breaks(result)

    @unittest.skipUnless(
        __import__("importlib").util.find_spec("mcp"), "Provision the mcp extra"
    )
    def test_the_mcp_tool_checks_the_working_copy(self):
        import asyncio
        import json
        from unittest.mock import patch

        from deepthought.graph_mcp import create_server

        with tempfile.TemporaryDirectory() as root:
            _write(root, self.files)
            _git("init", "-q", cwd=root)
            _git("add", "-A", cwd=root)
            _git("commit", "-qm", "base", cwd=root)
            self._edit(root)
            with patch.dict(os.environ, {"DEEPTHOUGHT_WORKSPACE": root}):
                server = create_server()
            result = asyncio.run(server.call_tool("checkEdit", {"max_elements": 5}))
            # A proposed diff is checked on top of the edited working copy.
            use = self.files["use.go"]
            proposed = _diff("use.go", use, use.replace("Add(x, x)", "Add(x, x, 0)"))
            fixed = asyncio.run(server.call_tool("checkEdit", {"diff": proposed}))

        def text(called):  # (content, structured) in newer mcp releases
            content = called[0] if isinstance(called, tuple) else called
            return json.loads(content[0].text)

        report = text(result)
        self.assertEqual(report["status"], "succeeded")
        self.assertEqual(report["total_breaks"], 1)
        self._assert_breaks(report)
        fixed = text(fixed)
        self.assertEqual(fixed["edited_files"], ["use.go"])
        self.assertEqual(fixed["breaks"], [])

    def test_checks_a_work_tree_whose_git_dir_is_elsewhere(self):
        with tempfile.TemporaryDirectory() as scratch:
            root, git_dir = Path(scratch) / "work", Path(scratch) / "repo.git"
            _write(root, self.files)
            tree = ["--git-dir", str(git_dir), "--work-tree", str(root)]
            _git("init", "-q", "--bare", str(git_dir))
            _git(*tree, "add", "-A")
            _git(*tree, "commit", "-qm", "base")
            self._edit(root)
            diff = working_copy_diff(root, git_dir=git_dir)
            self.assertNotIn("node_modules", diff)
            result = check_working_copy(root, git_dir=git_dir)
        self._assert_breaks(result)


if __name__ == "__main__":
    unittest.main()
