"""Installed CLI preserves recorded-file semantics and bounded machine output."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from deepthought import AnalysisResult, check_source_consistency
from deepthought.models import FileCoverage


class ConsistencyCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "sample.py").write_bytes(b"def sample(): pass\n")
        self.source = AnalysisResult(
            "repo",
            "snapshot",
            "succeeded",
            (),
            (
                FileCoverage(
                    "sample.py",
                    "python",
                    "analyzed",
                    sha256((self.root / "sample.py").read_bytes()).hexdigest(),
                ),
            ),
            (),
            "tree-sitter",
            (),
        )
        self.artifact = self.root / "analysis.json"
        self.artifact.write_text(self.source.to_json())

    def args(self, *extra):
        return [
            "check-source",
            str(self.root),
            str(self.artifact),
            "--repository-id",
            "repo",
            *extra,
        ]

    def run_cli(self, *extra):
        return subprocess.run(
            [str(Path(sys.executable).parent / "deepthought"), *self.args(*extra)],
            capture_output=True,
            timeout=10,
        )

    def test_installed_console_matches_sdk_after_source_changes(self):
        for state in ["same", "edited", "renamed", "deleted"]:
            if state == "edited":
                (self.root / "sample.py").write_bytes(b"def changed(): pass\n")
            elif state == "renamed":
                (self.root / "sample.py").rename(self.root / "renamed.py")
            elif state == "deleted":
                (self.root / "renamed.py").unlink()
            run = self.run_cli()
            expected = check_source_consistency(self.root, self.source)
            self.assertEqual(run.returncode, 0 if state == "same" else 1, run.stderr)
            self.assertEqual(run.stdout, (expected.to_json() + "\n").encode())
            self.assertFalse(run.stderr)

    def test_unverifiable_evidence_and_limits_are_exit_one(self):
        for source in [
            replace(self.source, analyzer="scip"),
            replace(self.source, coverage=()),
        ]:
            self.artifact.write_text(source.to_json())
            run = self.run_cli()
            self.assertEqual(run.returncode, 1, run.stderr)
            self.assertEqual(json.loads(run.stdout)["status"], "unverified")
        self.artifact.write_text(self.source.to_json())
        for flag in ["--max-file-bytes", "--max-total-bytes"]:
            run = self.run_cli(flag, "1")
            self.assertEqual(run.returncode, 1, run.stderr)
            self.assertEqual(json.loads(run.stdout)["status"], "unverified")
        source = replace(
            self.source,
            coverage=self.source.coverage
            + (FileCoverage("other.py", "python", "analyzed", "0" * 64),),
        )
        self.artifact.write_text(source.to_json())
        run = self.run_cli("--max-files", "1")
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertEqual(json.loads(run.stdout)["recorded_files"], 2)
        self.assertEqual(len(json.loads(run.stdout)["checks"]), 1)

    def test_invalid_input_has_no_stdout_or_traceback(self):
        extras = [
            ("--max-artifact-bytes", "1"),
            ("--max-artifact-bytes", "0"),
            ("--max-files", "0"),
            ("--max-file-bytes", "0"),
            ("--max-total-bytes", "0"),
            ("--max-output-bytes", "1023"),
            ("--max-output-bytes", "32769"),
            ("--repository-id", "other"),
        ]
        for extra in extras:
            run = self.run_cli(*extra)
            self.assertEqual(run.returncode, 2)
            self.assertFalse(run.stdout)
            self.assertIn(b"deepthought:", run.stderr)
            self.assertNotIn(b"Traceback", run.stderr)
        for content in [
            b"not json",
            b"\xff",
            b"{}",
            b'{"schema_version":"99"}',
            b"[" * 2000,
        ]:
            self.artifact.write_bytes(content)
            run = self.run_cli()
            self.assertEqual(run.returncode, 2)
            self.assertFalse(run.stdout)
            self.assertNotIn(b"Traceback", run.stderr)

    def test_invalid_snapshot_identity_is_exit_two(self):
        for snapshot in [None, [], 7, "", " "]:
            data = self.source.to_dict()
            data["snapshot_id"] = snapshot
            self.artifact.write_text(json.dumps(data))
            run = self.run_cli()
            self.assertEqual(run.returncode, 2)
            self.assertFalse(run.stdout)
            self.assertIn(b"snapshot", run.stderr)
            self.assertNotIn(b"Traceback", run.stderr)

    def test_unsafe_artifact_and_workspace_are_rejected(self):
        self.artifact.unlink()
        self.artifact.symlink_to(self.root / "sample.py")
        run = self.run_cli()
        self.assertEqual(run.returncode, 2)
        self.assertFalse(run.stdout)
        self.artifact.unlink()
        os.mkfifo(self.artifact)
        run = self.run_cli()
        self.assertEqual(run.returncode, 2)
        self.assertFalse(run.stdout)
        self.artifact.unlink()
        self.artifact.write_text(self.source.to_json())
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        args = self.args()
        args[1] = str(alias) + "/"
        run = subprocess.run(
            [str(Path(sys.executable).parent / "deepthought"), *args],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(run.returncode, 2)
        self.assertFalse(run.stdout)

    def test_output_budget_counts_utf8_and_newline_without_truncation(self):
        files = tuple(
            FileCoverage(f"{index}-" + "é" * 50 + ".py", "python", "analyzed", None)
            for index in range(8)
        )
        self.artifact.write_text(replace(self.source, coverage=files).to_json())
        run = self.run_cli()
        self.assertEqual(run.returncode, 1, run.stderr)
        budget = len(run.stdout)
        self.assertGreater(budget, len(run.stdout.decode()))
        exact = self.run_cli("--max-output-bytes", str(budget))
        self.assertEqual(exact.stdout, run.stdout)
        self.assertEqual(exact.returncode, 1)
        overflow = self.run_cli("--max-output-bytes", str(budget - 1))
        self.assertEqual(overflow.returncode, 2)
        self.assertFalse(overflow.stdout)
        self.assertIn(b"output", overflow.stderr.lower())

    def test_repository_mismatch_prevents_workspace_read_and_restores_handlers(self):
        from deepthought import cli

        previous = {
            sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)
        }
        with patch.object(
            cli,
            "check_source_consistency",
            side_effect=AssertionError("must not read workspace"),
        ):
            with (
                redirect_stdout(io.StringIO()) as output,
                redirect_stderr(io.StringIO()),
            ):
                code = cli.main(self.args("--repository-id", "wrong"))
        self.assertEqual(code, 2)
        self.assertFalse(output.getvalue())
        self.assertEqual(previous, {sig: signal.getsignal(sig) for sig in previous})
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(self.args()), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "unchanged")
        self.assertEqual(previous, {sig: signal.getsignal(sig) for sig in previous})

    def test_real_process_signals_produce_cancelled_result(self):
        script = r"""
import os, signal, sys
from deepthought import cli
original = cli.check_source_consistency
def wait_for_signal(*args, **kwargs):
    os.write(int(sys.argv[1]), b'ready')
    if not kwargs["cancel_event"].wait(5):
        raise RuntimeError("Cancellation signal did not arrive")
    return original(*args, **kwargs)
cli.check_source_consistency = wait_for_signal
raise SystemExit(cli.main(sys.argv[2:]))
"""
        for sig in (signal.SIGINT, signal.SIGTERM):
            reader, writer = os.pipe()
            process = subprocess.Popen(
                [sys.executable, "-I", "-c", script, str(writer), *self.args()],
                pass_fds=(writer,),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            os.close(writer)
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(reader, selectors.EVENT_READ)
                    self.assertTrue(selector.select(5), "CLI did not become ready")
                self.assertEqual(os.read(reader, 5), b"ready")
                process.send_signal(sig)
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 1, stderr)
                result = json.loads(stdout)
                self.assertTrue(result["cancelled"])
                self.assertEqual(result["status"], "unverified")
                self.assertFalse(result["checks"])
            finally:
                os.close(reader)
                if process.poll() is None:
                    process.kill()
                process.communicate()
