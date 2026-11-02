"""Actual CLI and optional MCP stdio journeys on an explicitly isolated Neo4j.

Tests use the public owned declaration fixture and clean only their unique repo.
Raw-Cypher acceptance requires no unrelated snapshots in the test database; run
these tests serially before populating a public-agent evaluation snapshot.
"""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import deepthought


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "evals/fixtures/tree_sitter_declarations"
PACK = ROOT / "evals/scenarios/tree_sitter/declarations.json"
DRIVER_AVAILABLE = all(
    importlib.util.find_spec(name) is not None
    for name in ("neo4j", "tree_sitter", "tree_sitter_python")
)
NEO4J_ENABLED = bool(os.environ.get("DEEPTHOUGHT_NEO4J_URI"))
MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None


@unittest.skipUnless(
    NEO4J_ENABLED and DRIVER_AVAILABLE,
    "Explicit isolated Neo4j and Python parser extras required",
)
class GraphTransportIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.repo = "https://github.com/deepthought-transport/" + uuid4().hex
        self.env = dict(os.environ)
        # Exercise source modules during development and installed modules during
        # installed acceptance; never import a different checkout into a child.
        package_parent = Path(deepthought.__file__).resolve().parent.parent
        if package_parent.name == "src":
            self.env["PYTHONPATH"] = str(package_parent)
        else:
            self.env.pop("PYTHONPATH", None)
        self.env.pop("DEEPTHOUGHT_ALLOW_DATABASE_SCOPE_CYPHER", None)
        self.expected = [
            row
            for row in json.loads(PACK.read_text())["expected"]
            if row["file"] == "cases.py"
        ]
        self.addCleanup(lambda: asyncio.run(self.cleanup_repo()))
        imported = self.command(
            "index",
            str(SOURCE),
            "--repository-id",
            self.repo,
            "--language",
            "python",
            "--include",
            "cases.py",
        )
        self.assertTrue(imported["success"], imported.get("errors"))
        self.snapshot = imported["snapshot_id"]
        self.assertIsInstance(self.snapshot, str)
        self.assertTrue(self.snapshot)

    async def cleanup_repo(self):
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
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=self.repo
                )
                await result.consume()
        finally:
            await driver.close()

    def command(self, *arguments, expected_exit=0):
        result = subprocess.run(
            [sys.executable, "-m", "deepthought.graph_cli", *arguments],
            env=self.env,
            cwd=SOURCE,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, expected_exit, result.stderr)
        if expected_exit:
            self.assertFalse(result.stdout)
            return result.stderr
        return json.loads(result.stdout)

    def scope_args(self):
        return ["--repository-id", self.repo, "--snapshot", self.snapshot]

    def assert_declarations(self, result):
        claims = [
            {
                "file": row["element"]["file_path"],
                "symbol": row["element"]["name"],
                "kind": row["element"]["kind"],
                "line": row["element"]["start_line"],
            }
            for row in result["items"]
        ]
        self.assertCountEqual(claims, self.expected)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["repo_url"], self.repo)
        self.assertEqual(result["snapshot_id"], self.snapshot)

    def test_cli_index_search_context_structure_and_cypher(self):
        selected = self.command("search", *self.scope_args(), "--query", "")
        self.assert_declarations(selected)
        context = self.command(
            "context", *self.scope_args(), "--file", "cases.py", "--line", "11"
        )
        self.assertEqual(context["items"][0]["element"]["name"], "Reader.parse")
        self.assertTrue(context["relationships"])
        structure = self.command("structure", *self.scope_args(), "--file", "cases.py")
        self.assertEqual(structure["returned"], 1)
        message = self.command(
            "cypher",
            *self.scope_args(),
            "--query",
            "RETURN $message AS value",
            "--parameters",
            '{"message":"transport-check"}',
            "--allow-database-scope",
        )
        self.assertEqual(message["items"], [{"value": "transport-check"}])
        self.assertEqual(message["scope"], "database")
        denied = self.command(
            "cypher",
            *self.scope_args(),
            "--query",
            "CREATE (n:UnexpectedTransportWrite)",
            "--allow-database-scope",
            expected_exit=2,
        )
        self.assertIn("read query", denied)
        self.assert_declarations(
            self.command("search", *self.scope_args(), "--query", "")
        )

    @unittest.skipUnless(MCP_AVAILABLE, "Provision the optional MCP extra")
    def test_mcp_stdio_initialization_discovery_and_actual_queries(self):
        asyncio.run(asyncio.wait_for(self.exercise_mcp(), timeout=60))

    async def exercise_mcp(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def session_journey(raw_enabled):
            env = dict(self.env)
            if raw_enabled:
                env["DEEPTHOUGHT_ALLOW_DATABASE_SCOPE_CYPHER"] = "1"
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "deepthought.graph_mcp"],
                env=env,
                cwd=SOURCE,
            )
            with tempfile.TemporaryFile(mode="w+") as stderr:
                async with stdio_client(parameters, errlog=stderr) as streams:
                    async with ClientSession(
                        *streams, read_timeout_seconds=timedelta(seconds=15)
                    ) as session:
                        initialized = await session.initialize()
                        self.assertEqual(initialized.serverInfo.name, "DeepThought")
                        tools = (await session.list_tools()).tools
                        expected = {
                            "getCodeContext",
                            "getFileStructure",
                            "searchCodeElements",
                        }
                        if raw_enabled:
                            expected.add("queryGraph")
                        self.assertEqual({tool.name for tool in tools}, expected)
                        for tool in tools:
                            self.assertNotIn(
                                "ctx", tool.inputSchema.get("properties", {})
                            )

                        async def call(name, **arguments):
                            result = await session.call_tool(
                                name,
                                {
                                    "repository_id": self.repo,
                                    "snapshot_id": self.snapshot,
                                    **arguments,
                                },
                            )
                            self.assertFalse(result.isError, result.content)
                            if result.structuredContent is not None:
                                self.assertIsInstance(result.structuredContent, dict)
                                return result.structuredContent
                            # The legacy bare-dict FastMCP tools return JSON text;
                            # structuredContent is optional in the MCP protocol.
                            texts = [
                                item.text
                                for item in result.content
                                if item.type == "text"
                            ]
                            self.assertEqual(len(texts), 1)
                            payload = json.loads(texts[0])
                            self.assertIsInstance(payload, dict)
                            return payload

                        if raw_enabled:
                            result = await call(
                                "queryGraph",
                                query="MATCH (n:Symbol) RETURN count(n) AS count",
                                parameters={},
                            )
                            self.assertEqual(
                                result["items"], [{"count": len(self.expected)}]
                            )
                            denied = await session.call_tool(
                                "queryGraph",
                                {
                                    "repository_id": self.repo,
                                    "snapshot_id": self.snapshot,
                                    "query": "CREATE (n:UnexpectedTransportWrite)",
                                    "parameters": {},
                                },
                            )
                            self.assertTrue(denied.isError)
                        else:
                            self.assert_declarations(
                                await call("searchCodeElements", query="")
                            )
                            context = await call(
                                "getCodeContext", file_path="cases.py", line_number=11
                            )
                            self.assertEqual(
                                context["items"][0]["element"]["name"], "Reader.parse"
                            )
                            structure = await call(
                                "getFileStructure", file_path="cases.py"
                            )
                            self.assertEqual(structure["returned"], 1)

        await session_journey(False)
        await session_journey(True)


if __name__ == "__main__":
    unittest.main()
