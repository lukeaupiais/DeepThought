"""Recorded source evidence is checked independently of historical queries."""

from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from deepthought import AnalysisResult, check_source_consistency
from deepthought.models import FileCoverage


def artifact(files, *, analyzer="tree-sitter"):
    return AnalysisResult(
        "repo",
        "snapshot",
        "succeeded",
        (),
        tuple(
            FileCoverage(name, "python", "analyzed", sha256(content).hexdigest())
            for name, content in files.items()
        ),
        (),
        analyzer,
        (),
    )


class ConsistencyTests(unittest.TestCase):
    def test_real_analysis_round_trip_and_source_changes(self):
        from deepthought import (
            analyze_repository,
            compose_graph,
            QueryRequest,
            query_graph,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.py"
            source.write_text("def sample(): pass\n")
            result = analyze_repository(
                root, repository_id="repo", languages=("python",)
            )
            result = AnalysisResult.from_json(result.to_json())
            before = result.to_json()
            checked = check_source_consistency(root, result)
            self.assertEqual(checked.status, "unchanged")
            self.assertEqual(checked.repository_id, "repo")
            self.assertEqual(checked.snapshot_id, result.snapshot_id)
            self.assertEqual(json.loads(checked.to_json()), checked.to_dict())
            (root / "new.py").write_text("def new(): pass\n")
            self.assertEqual(check_source_consistency(root, result).status, "unchanged")
            source.write_text("def changed(): pass\n")
            self.assertEqual(
                check_source_consistency(root, result).checks[0].reason,
                "digest_mismatch",
            )
            source.rename(root / "renamed.py")
            self.assertEqual(
                check_source_consistency(root, result).checks[0].reason, "missing"
            )
            (root / "renamed.py").unlink()
            self.assertEqual(check_source_consistency(root, result).status, "changed")
            self.assertEqual(result.to_json(), before)
            self.assertEqual(
                query_graph(
                    compose_graph([result]), QueryRequest("definitions", "repo")
                ).workspace_freshness,
                "unverified",
            )

    def test_untrusted_digest_semantics_and_empty_coverage_never_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.py").write_bytes(b"same")
            source = artifact({"a.py": b"same"})
            for other in [
                replace(source, analyzer="scip"),
                replace(source, analyzer="unknown"),
                replace(source, coverage=(replace(source.coverage[0], digest=None),)),
                replace(source, coverage=(replace(source.coverage[0], digest="bad"),)),
                replace(source, coverage=()),
            ]:
                with (
                    self.subTest(other=other),
                    patch(
                        "os.open", side_effect=AssertionError("unexpected source read")
                    ),
                ):
                    self.assertEqual(
                        check_source_consistency(root, other).status, "unverified"
                    )

    def test_links_and_special_files_are_never_followed_or_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            (outside / "a.py").write_bytes(b"secret")
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "alias").symlink_to(outside, target_is_directory=True)
            (workspace / "link.py").symlink_to(outside / "a.py")
            os.mkfifo(workspace / "pipe.py")
            (workspace / "dir.py").mkdir()
            result = check_source_consistency(
                workspace,
                artifact(
                    {
                        "alias/a.py": b"secret",
                        "link.py": b"secret",
                        "pipe.py": b"secret",
                        "dir.py": b"secret",
                    }
                ),
            )
            self.assertEqual(result.status, "unverified")
            self.assertEqual(result.bytes_read, 0)
            self.assertTrue(all(item.status == "unverified" for item in result.checks))

    def test_budgets_cancellation_and_invalid_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ["a.py", "b.py"]:
                (root / name).write_bytes(b"1234")
            source = artifact({"a.py": b"1234", "b.py": b"1234"})
            for kwargs in [
                {"max_files": 1},
                {"max_file_bytes": 3},
                {"max_total_bytes": 4},
            ]:
                with self.subTest(kwargs=kwargs):
                    result = check_source_consistency(root, source, **kwargs)
                    self.assertEqual(result.status, "unverified")
                    self.assertLessEqual(
                        result.bytes_read,
                        kwargs.get("max_total_bytes", 100_000_000) + 1,
                    )
            stop = Event()
            stop.set()
            result = check_source_consistency(root, source, cancel_event=stop)
            self.assertEqual(result.status, "unverified")
            self.assertTrue(result.cancelled)
            self.assertFalse(result.checks)
            for key in ["max_files", "max_file_bytes", "max_total_bytes"]:
                for value in [0, -1, True, 1.5]:
                    with self.assertRaises(ValueError):
                        check_source_consistency(root, source, **{key: value})

    def test_mutation_and_path_replacement_during_read_are_unverified(self):
        import deepthought.consistency as implementation

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "a.py"
            original_read = implementation.os.read
            for mode in ["edit", "replace"]:
                path.write_bytes(b"original")
                mutated = False

                def read(fd, size):
                    nonlocal mutated
                    data = original_read(fd, size)
                    if data and not mutated:
                        mutated = True
                        if mode == "replace":
                            path.unlink()
                        path.write_bytes(b"different")
                    return data

                with patch.object(implementation.os, "read", side_effect=read):
                    result = check_source_consistency(
                        root, artifact({"a.py": b"original"})
                    )
                self.assertEqual(result.status, "unverified")
                self.assertEqual(result.checks[0].reason, "changed_during_check")

    def test_cancellation_between_files_and_no_descriptor_leaks(self):
        import deepthought.consistency as implementation

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.py").write_bytes(b"same")
            (root / "b.py").write_bytes(b"same")
            source = artifact({"a.py": b"same", "b.py": b"same"})
            stop = Event()
            original_read = implementation.os.read

            def read(fd, size):
                data = original_read(fd, size)
                stop.set()
                return data

            with patch.object(implementation.os, "read", side_effect=read):
                checked = check_source_consistency(root, source, cancel_event=stop)
            self.assertEqual(checked.status, "unverified")
            self.assertTrue(checked.cancelled)
            before = len(os.listdir("/proc/self/fd"))
            for _ in range(30):
                check_source_consistency(root, source)
                check_source_consistency(root, source, max_total_bytes=1)
                check_source_consistency(root, source, cancel_event=stop)
            self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_scip_ingestion_cannot_be_verified_using_document_digest(self):
        from deepthought import ingest_scip

        source = ingest_scip(
            Path(__file__).parent / "fixtures/scip/python.scip", repository_id="repo"
        )
        self.assertTrue(source.coverage)
        with patch(
            "os.open", side_effect=AssertionError("SCIP must not read workspace source")
        ):
            checked = check_source_consistency("/unused", source)
        self.assertEqual(checked.status, "unverified")
        self.assertTrue(
            all(check.reason == "no_source_digest" for check in checked.checks)
        )

    def test_partial_evidence_aggregation_and_unreadable_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "same.py").write_bytes(b"same")
            (root / "unknown.py").write_bytes(b"same")
            source = artifact({"same.py": b"same", "unknown.py": b"same"})
            source = replace(
                source,
                coverage=(source.coverage[0], replace(source.coverage[1], digest=None)),
            )
            self.assertEqual(
                check_source_consistency(root, source).status, "unverified"
            )
            (root / "same.py").write_bytes(b"edit")
            self.assertEqual(check_source_consistency(root, source).status, "changed")
            source = artifact({"same.py": b"edit"})
            (root / "same.py").chmod(0)
            try:
                checked = check_source_consistency(root, source)
                self.assertEqual(checked.status, "unverified")
                self.assertEqual(checked.checks[0].reason, "source_unreadable")
            finally:
                (root / "same.py").chmod(0o600)

    def test_growth_uses_at_most_one_byte_beyond_total_budget(self):
        import deepthought.consistency as implementation

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "a.py"
            path.write_bytes(b"same")
            original_read = implementation.os.read
            grown = False

            def read(fd, size):
                nonlocal grown
                if not grown:
                    grown = True
                    with path.open("ab") as output:
                        output.write(b"more than the budget")
                return original_read(fd, size)

            with patch.object(implementation.os, "read", side_effect=read):
                checked = check_source_consistency(
                    root, artifact({"a.py": b"same"}), max_total_bytes=4
                )
            self.assertEqual(checked.status, "unverified")
            self.assertEqual(checked.bytes_read, 5)

    def test_parent_replacement_does_not_hide_the_race(self):
        import deepthought.consistency as implementation

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sub").mkdir()
            (root / "sub/a.py").write_bytes(b"same")
            original_read = implementation.os.read
            moved = False

            def read(fd, size):
                nonlocal moved
                data = original_read(fd, size)
                if data and not moved:
                    moved = True
                    (root / "sub").rename(root / "old")
                    (root / "sub").mkdir()
                    (root / "sub/a.py").write_bytes(b"same")
                return data

            with patch.object(implementation.os, "read", side_effect=read):
                checked = check_source_consistency(
                    root, artifact({"sub/a.py": b"same"})
                )
            self.assertEqual(checked.status, "unverified")
            self.assertEqual(checked.checks[0].reason, "changed_during_check")

    def test_workspace_root_alias_cannot_bypass_nofollow_with_trailing_separator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "a.py").write_bytes(b"same")
            alias = root / "alias"
            alias.symlink_to(workspace, target_is_directory=True)
            source = artifact({"a.py": b"same"})
            for value in [str(alias), str(alias) + "/", str(alias) + "/."]:
                with self.subTest(value=value), self.assertRaises(OSError):
                    check_source_consistency(value, source)
            self.assertEqual(
                check_source_consistency(str(workspace) + "/", source).status,
                "unchanged",
            )
