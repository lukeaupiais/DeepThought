"""The interpreter that reads Python sources (python_runtime.py)."""

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from deepthought import python_runtime
from deepthought.python_ast_facts import ast_facts
from deepthought.python_runtime import (
    Interpreter,
    _minimum,
    declared_pythons,
    interpreter_for,
    read_ast_facts,
)

RUNNING = "{}.{}".format(*sys.version_info[:2])
NEWER = "{}.{}".format(sys.version_info[0], sys.version_info[1] + 1)

SOURCES = {
    "conftest.py": "import pytest\n\n@pytest.fixture\ndef db():\n    pass\n",
    "test_a.py": "from fastapi import Depends\n\ndef test_a(db, x=Depends(db)):\n"
    "    pass\n",
    "broken.py": "def (:\n",
}


def write(root, files):
    for path, text in files.items():
        (Path(root) / path).parent.mkdir(parents=True, exist_ok=True)
        (Path(root) / path).write_text(text)


class DeclaredPythonTests(unittest.TestCase):
    def test_version_files_and_requires_python_are_read(self):
        with tempfile.TemporaryDirectory() as root:
            write(
                root,
                {
                    ".python-version": "# pinned\n\n3.14\n3.13\n",
                    "app/pyproject.toml": '[project]\nrequires-python = ">=3.12"\n',
                    "lib/pyproject.toml": "[project]\nname = 'x'\n",
                    "bad/pyproject.toml": "[project\n",
                    "number/pyproject.toml": "[project]\nrequires-python = 3\n",
                    "blank/pyproject.toml": '[project]\nrequires-python = "  "\n',
                    "empty/.python-version": "# nothing\n",
                    # Dependency trees and environments are not the project.
                    "node_modules/p/pyproject.toml": '[project]\nrequires-python = ">=3.15"\n',
                    "env/pyvenv.cfg": "",
                    "env/lib/.python-version": "3.15\n",
                },
            )
            self.assertEqual(declared_pythons(root), ("3.14", ">=3.12"))

    def test_the_lowest_admitted_version(self):
        self.assertEqual(_minimum("3.14"), (3, 14))
        self.assertEqual(_minimum("cpython-3.14.1"), (3, 14))
        self.assertEqual(_minimum(">=3.12, <4.0"), (3, 12))
        self.assertEqual(_minimum("!=3.15.*, >=3.8, ~=3.10"), (3, 10))
        self.assertIsNone(_minimum("<4"))
        self.assertIsNone(_minimum("pypy"))


class InterpreterTests(unittest.TestCase):
    def test_the_running_interpreter_reads_what_it_can_parse(self):
        with tempfile.TemporaryDirectory() as root:
            write(
                root,
                {
                    ".python-version": RUNNING,
                    "pyproject.toml": "",
                    "other/.python-version": "pypy",
                },
            )
            with mock.patch.object(python_runtime, "_find") as find:
                chosen = interpreter_for(root)
            find.assert_not_called()
            self.assertIsNone(chosen.executable)
            self.assertEqual(chosen.declared, (RUNNING, "pypy"))

    def test_uv_finds_a_declared_interpreter(self):
        # The running version needs no download; uv finds it.
        executable = python_runtime._find(RUNNING, None, 600)
        self.assertEqual(
            python_runtime._version(executable, None).rsplit(".", 1)[0], RUNNING
        )

    def test_a_newer_declared_interpreter_is_chosen(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, {".python-version": NEWER})
            with (
                mock.patch.object(python_runtime, "_find", return_value="/py") as find,
                mock.patch.object(
                    python_runtime, "_version", return_value=NEWER + ".0"
                ),
            ):
                chosen = interpreter_for(root)
            find.assert_called_once_with(NEWER, None, 600)
            self.assertEqual(chosen, Interpreter("/py", NEWER + ".0", (NEWER,), ()))

    def test_a_found_interpreter_no_newer_than_this_one_is_not_used(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, {".python-version": NEWER})
            with (
                mock.patch.object(python_runtime, "_find", return_value="/py"),
                mock.patch.object(
                    python_runtime, "_version", return_value=RUNNING + ".0"
                ),
            ):
                chosen = interpreter_for(root)
            self.assertIsNone(chosen.executable)

    def test_an_interpreter_that_cannot_be_obtained_is_reported(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, {".python-version": NEWER})
            with mock.patch.object(
                python_runtime, "_find", side_effect=RuntimeError("offline")
            ):
                chosen = interpreter_for(root)
            self.assertIsNone(chosen.executable)
            self.assertEqual(chosen.errors, (f"{NEWER}: offline",))


class MissingUvTests(unittest.TestCase):
    def test_a_missing_uv_is_a_reason_not_a_crash(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, {".python-version": NEWER})
            with mock.patch.dict(sys.modules, {"uv": None}):
                chosen = interpreter_for(root)
            self.assertIsNone(chosen.executable)
            self.assertEqual(len(chosen.errors), 1)
            self.assertTrue(
                chosen.errors[0].startswith(f"{NEWER}: uv is not available ("),
                chosen.errors,
            )

    def test_a_missing_uv_binary_is_a_reason_too(self):
        import uv

        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.object(uv, "find_uv_bin", side_effect=FileNotFoundError("gone")),
        ):
            write(root, {".python-version": NEWER})
            chosen = interpreter_for(root)
        self.assertEqual(chosen.errors, (f"{NEWER}: uv is not available (gone)",))


class ReadTests(unittest.TestCase):
    def test_another_interpreter_reads_the_same_facts(self):
        other = Interpreter(sys.executable, "9.9.9", ("9.9",), ())
        with mock.patch.object(python_runtime, "interpreter_for", return_value=other):
            facts, used = read_ast_facts(".", SOURCES)
        self.assertIs(used, other)
        expected = ast_facts(SOURCES)
        # JSON turns tuples into lists.
        for use in expected["fixtures"]:
            use["consumer"], use["fixture"] = (
                list(use["consumer"]),
                list(use["fixture"]),
            )
        self.assertEqual(facts, expected)
        self.assertEqual(list(facts["unparsed"]), ["broken.py"])
        self.assertEqual(len(facts["fixtures"]), 1)
        self.assertEqual(len(facts["dependencies"]["test_a.py"]["calls"]), 1)

    def test_a_failing_interpreter_falls_back_to_the_running_one(self):
        other = Interpreter("/nonexistent/python", "9.9.9", ("9.9",), ("x",))
        with mock.patch.object(python_runtime, "interpreter_for", return_value=other):
            facts, used = read_ast_facts(".", SOURCES)
        self.assertEqual(facts, ast_facts(SOURCES))
        self.assertIsNone(used.executable)
        self.assertEqual(used.version, python_runtime._RUNNING)
        self.assertEqual(used.declared, ("9.9",))
        self.assertEqual(used.errors[0], "x")
        self.assertTrue(used.errors[1].startswith("9.9.9: "), used.errors)

    def test_an_interpreter_killed_without_output_falls_back(self):
        # A signal (the OOM killer) ends the process with no stderr.
        other = Interpreter("/py", "9.9.9", ("9.9",), ())
        killed = subprocess.CompletedProcess([], -9, stdout="", stderr="")
        with (
            mock.patch.object(python_runtime, "interpreter_for", return_value=other),
            mock.patch.object(python_runtime, "_run", return_value=killed),
        ):
            facts, used = read_ast_facts(".", SOURCES)
        self.assertEqual(facts, ast_facts(SOURCES))
        self.assertIsNone(used.executable)
        self.assertEqual(used.errors, ("9.9.9: exit status -9",))

    def test_a_killed_install_is_a_reason(self):
        killed = subprocess.CompletedProcess([], -9, stdout="", stderr="")
        with (
            mock.patch.object(python_runtime, "_uv", return_value="/uv"),
            mock.patch.object(python_runtime, "_run", return_value=killed),
        ):
            with self.assertRaisesRegex(RuntimeError, "exit status -9"):
                python_runtime._find("3.14", None, 60)


@unittest.skipUnless(
    importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_python"),
    "Provision the python extra",
)
class AnalysisTests(unittest.TestCase):
    def analyze(self, files):
        import asyncio

        from deepthought.analysis_tools import TreeSitterTool

        with tempfile.TemporaryDirectory() as root:
            write(root, files)
            return asyncio.run(
                TreeSitterTool(root).analyze(
                    "r", languages=("python",), scip_indexes=()
                )
            ).metadata

    def test_the_interpreter_and_its_failures_are_reported(self):
        with mock.patch.object(
            python_runtime, "_find", side_effect=RuntimeError("offline")
        ):
            metadata = self.analyze({**SOURCES, ".python-version": NEWER})
        self.assertEqual(
            metadata.custom_metrics["python_interpreter"],
            {
                "version": python_runtime._RUNNING,
                "declared": [NEWER],
                "managed": False,
                "errors": [f"{NEWER}: offline"],
            },
        )
        self.assertIn(
            f"python_interpreter_unavailable: {NEWER}: offline", metadata.warnings
        )
        self.assertEqual(
            metadata.custom_metrics["python_ast_unparsed"]["files"], ["broken.py"]
        )

    def test_a_declared_interpreter_reads_the_sources(self):
        other = Interpreter(sys.executable, "9.9.9", ("9.9",), ())
        with mock.patch.object(python_runtime, "interpreter_for", return_value=other):
            metadata = self.analyze(SOURCES)
        self.assertEqual(
            metadata.custom_metrics["python_interpreter"],
            {
                "version": python_runtime._RUNNING,
                "declared": ["9.9"],
                "managed": True,
                "errors": [],
            },
        )

    def test_nothing_runs_without_python_sources(self):
        with mock.patch.object(python_runtime, "interpreter_for") as chosen:
            metadata = self.analyze({".python-version": NEWER, "a.txt": ""})
        chosen.assert_not_called()
        self.assertEqual(
            metadata.custom_metrics["python_interpreter"],
            {
                "version": python_runtime._RUNNING,
                "declared": [],
                "managed": False,
                "errors": [],
            },
        )


if __name__ == "__main__":
    unittest.main()
