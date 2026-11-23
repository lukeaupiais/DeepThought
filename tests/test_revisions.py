"""Commit-linked snapshots: when a snapshot may name its commit, and storage."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

import deepthought
from deepthought.revisions import CommitLink, commit_link, head_commit, is_commit

GIT = shutil.which("git")
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(root, *arguments):
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        env={**os.environ, **GIT_ENV},
    )


def commit_all(root, message):
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return head_commit(root)


class GitRepository:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        git(self.root, "init", "-q", "-b", "main")
        (self.root / "app.py").write_text("def run():\n    return 1\n")
        (self.root / ".gitignore").write_text("generated.py\n")
        self.first = commit_all(self.root, "first")

    def cleanup(self):
        self.directory.cleanup()


@unittest.skipUnless(GIT, "git is required")
class CommitLinkTests(unittest.TestCase):
    def setUp(self):
        self.repo = GitRepository()
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root

    def test_clean_checkout_links_its_commit(self):
        self.assertTrue(is_commit(self.repo.first))
        self.assertEqual(
            commit_link(self.root, {"app.py"}, self.repo.first),
            CommitLink(self.repo.first, True),
        )

    def test_changed_analyzed_files_prevent_the_link(self):
        (self.root / "app.py").write_text("def run():\n    return 2\n")
        self.assertEqual(
            commit_link(self.root, {"app.py"}, self.repo.first).reason,
            "uncommitted_changes",
        )
        git(self.root, "add", "app.py")
        self.assertEqual(
            commit_link(self.root, {"app.py"}, self.repo.first).reason,
            "uncommitted_changes",
        )

    def test_untracked_or_ignored_analyzed_files_prevent_the_link(self):
        for name in ("new.py", "generated.py"):
            with self.subTest(name=name):
                (self.root / name).write_text("x = 1\n")
                self.assertEqual(
                    commit_link(self.root, {"app.py", name}, self.repo.first).reason,
                    "untracked_files_analyzed",
                )
                # Files that were not analyzed do not matter.
                self.assertTrue(
                    commit_link(self.root, {"app.py"}, self.repo.first).linked
                )

    def test_unrelated_changes_do_not_prevent_the_link(self):
        (self.root / "README.md").write_text("notes\n")
        git(self.root, "add", "README.md")
        self.assertTrue(commit_link(self.root, {"app.py"}, self.repo.first).linked)

    def test_workspace_may_be_a_subdirectory(self):
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "mod.py").write_text("def f():\n    pass\n")
        second = commit_all(self.root, "second")
        workspace = self.root / "pkg"
        self.assertEqual(head_commit(workspace), second)
        self.assertTrue(commit_link(workspace, {"mod.py"}, second).linked)
        (workspace / "mod.py").write_text("def g():\n    pass\n")
        self.assertEqual(
            commit_link(workspace, {"mod.py"}, second).reason, "uncommitted_changes"
        )

    def test_head_moving_during_analysis_prevents_the_link(self):
        (self.root / "README.md").write_text("notes\n")
        commit_all(self.root, "second")
        self.assertEqual(
            commit_link(self.root, {"app.py"}, self.repo.first).reason,
            "head_moved_during_analysis",
        )

    def test_outside_git_there_is_no_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(head_commit(directory))
            self.assertEqual(
                commit_link(directory, {"a.py"}, None).reason, "not_a_git_checkout"
            )

    def test_commit_names_must_be_full_lowercase_hex(self):
        for value in ("abc123", self.repo.first.upper(), None, "g" * 40):
            with self.subTest(value=value):
                self.assertFalse(is_commit(value))
        self.assertTrue(is_commit("a" * 64))


@unittest.skipUnless(
    GIT
    and os.environ.get("DEEPTHOUGHT_NEO4J_URI")
    and all(
        importlib.util.find_spec(name)
        for name in ("neo4j", "tree_sitter", "tree_sitter_python")
    ),
    "git, explicit isolated Neo4j settings, neo4j and python extras required",
)
class CommitLinkedSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.repo = GitRepository()
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root
        self.repository_id = "https://github.com/deepthought-revisions/" + uuid4().hex
        self.env = dict(os.environ)
        package_parent = Path(deepthought.__file__).resolve().parent.parent
        if package_parent.name == "src":
            self.env["PYTHONPATH"] = str(package_parent)
        else:
            self.env.pop("PYTHONPATH", None)
        self.addCleanup(lambda: asyncio.run(self.cleanup_repository()))

    async def cleanup_repository(self):
        from neo4j import AsyncGraphDatabase

        driver = AsyncGraphDatabase.driver(
            self.env["DEEPTHOUGHT_NEO4J_URI"],
            auth=(
                self.env.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
                self.env["DEEPTHOUGHT_NEO4J_PASSWORD"],
            ),
        )
        try:
            async with driver.session(
                database=self.env.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j")
            ) as session:
                result = await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n",
                    repo=self.repository_id,
                )
                await result.consume()
        finally:
            await driver.close()

    def command(self, *arguments):
        result = subprocess.run(
            [sys.executable, "-m", "deepthought.graph_cli", *arguments],
            env=self.env,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def index(self):
        return self.command(
            "index",
            str(self.root),
            "--repository-id",
            self.repository_id,
            "--language",
            "python",
        )

    def snapshots(self, *arguments):
        return self.command(
            "snapshots", "--repository-id", self.repository_id, *arguments
        )

    def test_snapshots_are_linked_to_the_commits_they_were_analyzed_at(self):
        first = self.index()
        self.assertEqual(
            first["source_revision"],
            {"commit": self.repo.first, "linked": True, "reason": None},
        )
        snapshot = first["snapshot_id"]
        listed = self.snapshots("--commit", self.repo.first)
        self.assertEqual([i["snapshot_id"] for i in listed["items"]], [snapshot])
        self.assertEqual(listed["items"][0]["commits"], [self.repo.first])
        self.assertEqual(listed["scope"], "repository")

        # Re-indexing the same commit is idempotent.
        again = self.index()
        self.assertTrue(again["stats"]["reused"])
        self.assertEqual(
            self.snapshots("--commit", self.repo.first)["items"][0]["commits"],
            [self.repo.first],
        )

        # A commit that changes no analyzed file gives the same snapshot.
        (self.root / "README.md").write_text("notes\n")
        second = commit_all(self.root, "docs")
        unchanged = self.index()
        self.assertEqual(unchanged["snapshot_id"], snapshot)
        self.assertEqual(
            self.snapshots("--commit", second)["items"][0]["commits"],
            sorted([self.repo.first, second]),
        )

        # Uncommitted analyzed changes give a new snapshot with no commit.
        (self.root / "app.py").write_text("def run():\n    return 2\n")
        dirty = self.index()
        self.assertNotEqual(dirty["snapshot_id"], snapshot)
        self.assertEqual(
            dirty["source_revision"],
            {"commit": second, "linked": False, "reason": "uncommitted_changes"},
        )
        listed = self.snapshots()
        self.assertEqual(
            {i["snapshot_id"]: i["commits"] for i in listed["items"]},
            {snapshot: sorted([self.repo.first, second]), dirty["snapshot_id"]: []},
        )
        self.assertEqual(self.snapshots("--commit", "0" * 40)["status"], "not_found")

    def test_link_commit_requires_a_complete_snapshot_and_a_full_commit(self):
        from deepthought.storage.graph import Neo4jConnectionManager, WriteCoordinator

        async def check():
            manager = Neo4jConnectionManager(
                self.env["DEEPTHOUGHT_NEO4J_URI"],
                self.env.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
                self.env["DEEPTHOUGHT_NEO4J_PASSWORD"],
                database=self.env.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j"),
            )
            driver = await manager.connect()
            try:
                coordinator = WriteCoordinator(driver, database=manager.database)
                with self.assertRaisesRegex(ValueError, "full lowercase hex"):
                    await coordinator.link_commit(self.repository_id, "s", "abc")
                with self.assertRaisesRegex(ValueError, "missing or incomplete"):
                    await coordinator.link_commit(
                        self.repository_id, "missing", self.repo.first
                    )
            finally:
                await manager.close()

        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
