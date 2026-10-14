"""Behavior through the standalone SDK; source truth is independent fixture data."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from threading import Event
import unittest
from pathlib import Path

from deepthought import AnalysisResult, analyze_repository

FIXTURES = Path(__file__).parent / "fixtures"


class EngineTests(unittest.TestCase):
    def test_oversized_files_consume_the_aggregate_read_budget(self):
        with tempfile.TemporaryDirectory() as workspace:
            for name in ("a.py", "b.py", "c.py"):
                (Path(workspace) / name).write_text("#" * 100)
            result = analyze_repository(
                workspace,
                repository_id="fixture",
                languages=("python",),
                max_file_bytes=8,
                max_total_bytes=10,
            )
        self.assertEqual(result.status, "failed")
        self.assertIn("total_byte_limit", {d.code for d in result.diagnostics})
        self.assertEqual([c.file for c in result.coverage], ["a.py", "b.py"])

    def test_snapshot_distinguishes_complete_and_cancelled_empty_analysis(self):
        with tempfile.TemporaryDirectory() as workspace:
            complete = analyze_repository(
                workspace, repository_id="fixture", languages=("python",)
            )
            cancel = Event()
            cancel.set()
            cancelled = analyze_repository(
                workspace,
                repository_id="fixture",
                languages=("python",),
                cancel_event=cancel,
            )
        self.assertEqual(complete.status, "succeeded")
        self.assertEqual(cancelled.status, "cancelled")
        self.assertNotEqual(complete.snapshot_id, cancelled.snapshot_id)

    def test_incompatible_source_paths_are_disclosed_without_aborting_analysis(self):
        with tempfile.TemporaryDirectory() as workspace:
            (Path(workspace) / "good.py").write_text("class Good: pass\n")
            (Path(workspace) / "C:helper.py").write_text("class DriveName: pass\n")
            (Path(workspace) / "bad\\helper.py").write_text("class Backslash: pass\n")
            result = analyze_repository(
                workspace, repository_id="fixture", languages=("python",)
            )
        self.assertEqual(result.status, "partial")
        self.assertEqual([d.symbol for d in result.definitions], ["Good"])
        self.assertEqual(
            [d.code for d in result.diagnostics], ["invalid_source_path"] * 2
        )
        self.assertTrue(all(d.file is None for d in result.diagnostics))
        self.assertEqual(AnalysisResult.from_json(result.to_json()), result)

    def test_python_definitions_match_owned_fixture(self):
        result = analyze_repository(
            FIXTURES / "python", repository_id="fixture", languages=("python",)
        )
        expected = json.loads((FIXTURES / "expected.json").read_text())["languages"][
            "python"
        ]["definitions"]
        observed = [
            {key: item[key] for key in ("file", "symbol", "kind", "line")}
            for item in result.to_dict()["definitions"]
        ]
        self.assertCountEqual(observed, expected)
        self.assertEqual(result.status, "succeeded")

    def test_cli_emits_the_same_artifact_as_sdk(self):
        result = analyze_repository(
            FIXTURES / "python", repository_id="fixture", languages=("python",)
        )
        command = [
            sys.executable,
            "-m",
            "deepthought",
            "analyze",
            str(FIXTURES / "python"),
            "--repository-id",
            "fixture",
            "--language",
            "python",
        ]
        run = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), result.to_dict())

    def test_typescript_definitions_and_file_coverage_match_fixture(self):
        result = analyze_repository(
            FIXTURES / "typescript", repository_id="fixture", languages=("typescript",)
        )
        expected = json.loads((FIXTURES / "expected.json").read_text())["languages"][
            "typescript"
        ]["definitions"]
        observed = [
            {key: item[key] for key in ("file", "symbol", "kind", "line")}
            for item in result.to_dict()["definitions"]
        ]
        self.assertCountEqual(observed, expected)
        self.assertEqual(
            {item.file for item in result.coverage}, {item["file"] for item in expected}
        )
        self.assertTrue(all(item.status == "analyzed" for item in result.coverage))
        self.assertEqual(result.capabilities, ("definitions",))

    def test_artifact_round_trip_and_relocation_are_stable(self):
        original = analyze_repository(
            FIXTURES / "python", repository_id="fixture", languages=("python",)
        )
        self.assertEqual(AnalysisResult.from_json(original.to_json()), original)
        with tempfile.TemporaryDirectory() as workspace:
            shutil.copytree(FIXTURES / "python", Path(workspace) / "copy")
            relocated = analyze_repository(
                Path(workspace) / "copy", repository_id="fixture", languages=("python",)
            )
        self.assertEqual(original.to_json(), relocated.to_json())
        encoded = json.loads(original.to_json())
        encoded["schema_version"] = "999"
        with self.assertRaises(ValueError):
            AnalysisResult.from_json(json.dumps(encoded))

    def test_syntax_error_is_partial_and_cli_is_nonzero(self):
        with tempfile.TemporaryDirectory() as workspace:
            (Path(workspace) / "valid.py").write_text("def useful():\n    return 1\n")
            (Path(workspace) / "broken.py").write_text("def broken(:\n")
            result = analyze_repository(
                workspace, repository_id="fixture", languages=("python",)
            )
            self.assertEqual(result.status, "partial")
            self.assertIn("syntax_error", {d.code for d in result.diagnostics})
            run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "deepthought",
                    "analyze",
                    workspace,
                    "--repository-id",
                    "fixture",
                    "--language",
                    "python",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(run.returncode, 1, run.stderr)
            self.assertEqual(json.loads(run.stdout)["status"], "partial")

    def test_symlink_escape_is_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            outer = Path(directory)
            workspace = outer / "workspace"
            workspace.mkdir()
            (outer / "secret.py").write_text("class Confidential: pass\n")
            (workspace / "escape.py").symlink_to(outer / "secret.py")
            (workspace / "escape_dir").symlink_to(outer, target_is_directory=True)
            result = analyze_repository(
                workspace, repository_id="fixture", languages=("python",)
            )
            self.assertFalse(result.definitions)
            self.assertNotEqual(result.status, "succeeded")

    def test_oversized_source_is_not_analyzed(self):
        with tempfile.TemporaryDirectory() as workspace:
            (Path(workspace) / "large.py").write_text("class TooLarge: pass\n")
            result = analyze_repository(
                workspace,
                repository_id="fixture",
                languages=("python",),
                max_file_bytes=8,
            )
            self.assertEqual(result.status, "failed")
            self.assertFalse(result.definitions)
            self.assertIn("file_limit", {d.code for d in result.diagnostics})

    def test_precancelled_run_reports_cancelled_without_facts(self):
        cancel = Event()
        cancel.set()
        result = analyze_repository(
            FIXTURES / "python",
            repository_id="fixture",
            languages=("python",),
            cancel_event=cancel,
        )
        self.assertEqual(result.status, "cancelled")
        self.assertFalse(result.definitions)

    def test_file_budget_returns_incomplete_instead_of_silent_success(self):
        result = analyze_repository(
            FIXTURES / "python",
            repository_id="fixture",
            languages=("python",),
            max_files=1,
        )
        self.assertNotEqual(result.status, "succeeded")
        self.assertIn("file_count_limit", {d.code for d in result.diagnostics})

    def test_explicit_exclusion_is_not_read(self):
        with tempfile.TemporaryDirectory() as workspace:
            (Path(workspace) / "keep.py").write_text("class Keep: pass\n")
            (Path(workspace) / "secret.py").write_text("class Secret: pass\n")
            result = analyze_repository(
                workspace,
                repository_id="fixture",
                languages=("python",),
                exclude=("secret.py",),
            )
            self.assertEqual([d.symbol for d in result.definitions], ["Keep"])
            self.assertEqual(result.status, "succeeded")

    def test_untrusted_artifacts_reject_invalid_status_and_paths(self):
        result = analyze_repository(
            FIXTURES / "python", repository_id="fixture", languages=("python",)
        )
        for field in ("status", "path"):
            data = result.to_dict()
            if field == "status":
                data["status"] = "invented-success"
            else:
                data["definitions"][0]["file"] = "../outside.py"
            with self.subTest(field=field), self.assertRaises(ValueError):
                AnalysisResult.from_json(json.dumps(data))

    def test_dirty_content_changes_snapshot_but_preserves_symbol_identity(self):
        with tempfile.TemporaryDirectory() as workspace:
            source = Path(workspace) / "sample.py"
            source.write_text("def sample():\n    return 1\n")
            first = analyze_repository(
                workspace, repository_id="fixture", languages=("python",)
            )
            source.write_text("def sample():\n    return 2\n")
            second = analyze_repository(
                workspace, repository_id="fixture", languages=("python",)
            )
        self.assertNotEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(first.definitions[0].id, second.definitions[0].id)

    def test_nested_methods_have_distinct_scopes(self):
        with tempfile.TemporaryDirectory() as workspace:
            source = Path(workspace) / "sample.py"
            source.write_text(
                "class One:\n    def action(self): pass\n\nclass Two:\n    def action(self): pass\n"
            )
            result = analyze_repository(
                workspace, repository_id="fixture", languages=("python",)
            )
        self.assertEqual(
            {(d.symbol, d.kind) for d in result.definitions},
            {
                ("One", "Class"),
                ("Two", "Class"),
                ("One.action", "Method"),
                ("Two.action", "Method"),
            },
        )

    def test_unreadable_directory_does_not_become_empty_success(self):
        if os.geteuid() == 0:
            self.skipTest("Permission behavior requires an unprivileged user")
        with tempfile.TemporaryDirectory() as workspace:
            hidden = Path(workspace) / "hidden"
            hidden.mkdir()
            (hidden / "secret.py").write_text("class Hidden: pass\n")
            hidden.chmod(0)
            try:
                result = analyze_repository(
                    workspace, repository_id="fixture", languages=("python",)
                )
            finally:
                hidden.chmod(0o700)
        self.assertEqual(result.status, "failed")
        self.assertIn("directory_unreadable", {d.code for d in result.diagnostics})


if __name__ == "__main__":
    unittest.main()
