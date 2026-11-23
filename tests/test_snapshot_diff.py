"""Graph diff between two complete snapshots, checked against the writer payloads."""

import asyncio
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from deepthought.graph_queries import GraphQueryService
from deepthought.storage.graph.writer import _prepare

TREE_SITTER_READY = importlib.util.find_spec(
    "tree_sitter"
) and importlib.util.find_spec("tree_sitter_python")

BASE = {
    "a.py": "def helper():\n    pass\n\n\ndef run():\n    helper()\n",
    "b.py": "def gone():\n    pass\n",
}
HEAD = {
    "a.py": "\n\ndef helper():\n    pass\n\n\ndef run():\n    helper()\n\n\n"
    "def added():\n    pass\n",
    "c.py": "def new():\n    pass\n",
}


def expected_diff(base, head):
    """Independent oracle: compare the payloads the writer stores."""

    def nodes(payload):
        return {
            n["properties"]["qualified_name"]: (
                n["labels"],
                {k: v for k, v in n["properties"].items() if k != "snapshot_id"},
            )
            for n in payload["nodes"]
        }

    old, new = nodes(base), nodes(head)
    old_edges = {r["edge_id"] for r in base["relationships"]}
    new_edges = {r["edge_id"] for r in head["relationships"]}
    return {
        "nodes": {
            "added": len(new.keys() - old.keys()),
            "removed": len(old.keys() - new.keys()),
            "changed": sum(old[q] != new[q] for q in old.keys() & new.keys()),
            "unchanged": sum(old[q] == new[q] for q in old.keys() & new.keys()),
        },
        "edges": {
            "added": len(new_edges - old_edges),
            "removed": len(old_edges - new_edges),
            "unchanged": len(new_edges & old_edges),
        },
    }


class DiffContractTests(unittest.TestCase):
    def test_change_categories_are_validated_before_reading(self):
        service = GraphQueryService(None)
        for changes in ([], "nodes.added", ["nodes.moved"], ["edges.changed"]):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, "changes"):
                    asyncio.run(
                        service.diff_snapshots(
                            "r",
                            base_snapshot_id="a",
                            head_snapshot_id="b",
                            changes=changes,
                        )
                    )
        with self.assertRaisesRegex(ValueError, "base_snapshot_id"):
            asyncio.run(
                service.diff_snapshots("r", base_snapshot_id="", head_snapshot_id="b")
            )


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_NEO4J_URI")
    and importlib.util.find_spec("neo4j")
    and TREE_SITTER_READY,
    "Explicit isolated Neo4j test settings, neo4j and python extras required",
)
class SnapshotDiffNeo4jTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from deepthought.storage.graph import (
            Neo4jConnectionManager,
            WriteCoordinator,
            setup_schema,
        )

        self.repo = "https://github.com/deepthought-integration/" + uuid4().hex
        self.manager = Neo4jConnectionManager(
            os.environ["DEEPTHOUGHT_NEO4J_URI"],
            os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
            os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
            database=os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j"),
        )
        self.driver = await self.manager.connect()
        self.database = self.manager.database
        await setup_schema(self.driver, database=self.database)
        coordinator = WriteCoordinator(self.driver, database=self.database)
        self.payloads = {}
        self.snapshots = {}
        for name, files in (("base", BASE), ("head", HEAD)):
            output = await self.analyze(files)
            written = await coordinator.write_tool_output(
                "tree-sitter", output, self.repo
            )
            self.assertTrue(written.success, written.errors)
            self.snapshots[name] = written.snapshot_id
            self.payloads[name] = _prepare(output, self.repo, written.snapshot_id)[0]
        self.service = GraphQueryService(self.driver, self.database)

    async def asyncTearDown(self):
        try:
            async with self.driver.session(database=self.database) as session:
                result = await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=self.repo
                )
                await result.consume()
        finally:
            await self.manager.close()

    async def analyze(self, files):
        from deepthought.analysis_tools import TreeSitterTool

        with tempfile.TemporaryDirectory() as directory:
            for name, text in files.items():
                (Path(directory) / name).write_text(text)
            return await TreeSitterTool(Path(directory)).analyze(
                self.repo, languages=["python"]
            )

    async def diff(self, base="base", head="head", **options):
        return await self.service.diff_snapshots(
            self.repo,
            base_snapshot_id=self.snapshots[base],
            head_snapshot_id=self.snapshots[head],
            **options,
        )

    async def test_counts_equal_the_writer_payload_comparison(self):
        result = await self.diff(limit=100, max_output_bytes=65536)
        self.assertEqual(
            result["summary"],
            expected_diff(self.payloads["base"], self.payloads["head"]),
        )
        self.assertFalse(result["truncated"])
        for category, listing in result["changes"].items():
            element, change = category.split(".")
            self.assertEqual(len(listing["items"]), result["summary"][element][change])

    async def test_listed_changes_name_what_moved_and_what_was_added(self):
        result = await self.diff(limit=100, max_output_bytes=65536)
        names = {
            category: sorted((item["node"]["name"]) for item in listing["items"])
            for category, listing in result["changes"].items()
            if category.startswith("nodes")
        }
        self.assertEqual(names["nodes.added"], ["added", "c.py", "new"])
        self.assertEqual(names["nodes.removed"], ["b.py", "gone"])
        # Moving a declaration changes its position, not its identity.
        # The edited file itself changed too (its file_sha256).
        self.assertEqual(names["nodes.changed"], ["a.py", "helper", "run"])
        helper = next(
            item
            for item in result["changes"]["nodes.changed"]["items"]
            if item["node"]["name"] == "helper"
        )
        self.assertIn({"key": "start_line", "base": 1, "head": 3}, helper["properties"])
        self.assertIsNone(helper["labels"])
        calls = {
            category: [
                (item["line"], item["evidence_type"])
                for item in result["changes"][category]["items"]
                if item["type"] == "CALLS"
            ]
            for category in ("edges.added", "edges.removed")
        }
        # A call that moved line is a removed edge and an added edge.
        self.assertEqual(calls["edges.removed"], [(6, "name_matched")])
        self.assertEqual(calls["edges.added"], [(8, "name_matched")])

    async def test_diff_is_deterministic_symmetric_and_empty_on_itself(self):
        first = await self.diff()
        self.assertEqual(first, await self.diff())
        reverse = await self.diff("head", "base")
        for element in ("nodes", "edges"):
            self.assertEqual(
                reverse["summary"][element]["added"],
                first["summary"][element]["removed"],
            )
            self.assertEqual(
                reverse["summary"][element]["removed"],
                first["summary"][element]["added"],
            )
        same = await self.diff("base", "base")
        self.assertEqual(same["summary"]["nodes"]["added"], 0)
        self.assertEqual(same["summary"]["nodes"]["changed"], 0)
        self.assertEqual(same["summary"]["edges"]["removed"], 0)
        self.assertTrue(all(not v["items"] for v in same["changes"].values()))

    async def test_listings_are_bounded_but_counts_are_not(self):
        full = await self.diff(limit=100, max_output_bytes=65536)
        bounded = await self.diff(limit=1, changes=["nodes.added"])
        self.assertEqual(list(bounded["changes"]), ["nodes.added"])
        self.assertEqual(len(bounded["changes"]["nodes.added"]["items"]), 1)
        self.assertTrue(bounded["truncated"])
        self.assertEqual(bounded["summary"], full["summary"])
        small = await self.diff(limit=100, max_output_bytes=2048)
        self.assertLessEqual(len(str(small).encode()), 4096)
        self.assertTrue(small["truncated"])
        self.assertEqual(small["summary"], full["summary"])

    async def test_both_snapshots_must_be_complete(self):
        with self.assertRaisesRegex(ValueError, "missing or incomplete"):
            await self.service.diff_snapshots(
                self.repo,
                base_snapshot_id="missing",
                head_snapshot_id=self.snapshots["head"],
            )


if __name__ == "__main__":
    unittest.main()
