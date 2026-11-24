"""Declaration CONTAINS edges from parse-tree nesting."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from deepthought.analysis_tools import TreeSitterTool
from deepthought.analysis_tools.evidence import registered_rules

READY = all(
    importlib.util.find_spec(name)
    for name in ("tree_sitter", "tree_sitter_python", "tree_sitter_typescript")
)
RULE = "tree-sitter.declaration-contains-declaration"
SOURCES = {
    "a.py": "class Outer:\n"
    "    def run(self):\n"
    "        def helper(): pass\n"
    "        class Local: pass\n"
    "    class Inner:\n"
    "        def go(self): pass\n"
    "def top(): pass\n",
    "b.ts": "export namespace N {\n"
    "  export class C {\n"
    "    m(): void { const f = () => 1; }\n"
    "  }\n"
    "}\n"
    "interface I { x(): void; }\n"
    "function g() {}\n",
}


def write(directory):
    for name, content in SOURCES.items():
        (Path(directory) / name).write_text(content)


def nesting(output):
    names = {n.properties["qualified_name"]: n.properties for n in output.nodes}
    found = []
    for r in output.relationships:
        if r.type == "CONTAINS" and "Symbol" in r.source_labels:
            assert (r.properties["evidence_type"], r.properties["rule_id"]) == (
                "resolved",
                RULE,
            )
            inner = names[r.target_match["qualified_name"]]
            found.append(
                (names[r.source_match["qualified_name"]]["name"], inner["name"])
            )
            assert r.properties["line"] == inner["start_line"]
    return sorted(found)


EXPECTED = [
    ("I", "I.x"),
    ("N", "N.C"),
    ("N.C", "N.C.m"),
    ("N.C.m", "N.C.m.f"),
    ("Outer", "Outer.Inner"),
    ("Outer", "Outer.run"),
    ("Outer.Inner", "Outer.Inner.go"),
    ("Outer.run", "Outer.run.Local"),
    ("Outer.run", "Outer.run.helper"),
]


@unittest.skipUnless(READY, "Provision the python and typescript extras")
class ContainmentTests(unittest.TestCase):
    def test_each_nested_declaration_has_its_innermost_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory)
            output = asyncio.run(TreeSitterTool(directory).analyze("r"))
            again = asyncio.run(TreeSitterTool(directory).analyze("r"))
        self.assertEqual(nesting(output), EXPECTED)
        self.assertEqual(
            [vars(r) for r in output.relationships],
            [vars(r) for r in again.relationships],
        )
        self.assertEqual(registered_rules()[RULE].evidence_type, "resolved")


@unittest.skipUnless(
    READY and os.environ.get("DEEPTHOUGHT_NEO4J_URI"),
    "Provision isolated Neo4j and the python and typescript extras",
)
class ContainmentCommandTests(unittest.TestCase):
    def test_members_and_owner_queries(self):
        from test_incremental import run_module
        from test_scip_graph import ScipCommandTests

        repo = "nesting-cli-" + uuid4().hex
        self.addCleanup(lambda: asyncio.run(ScipCommandTests.cleanup(None, repo)))
        with tempfile.TemporaryDirectory() as directory:
            write(directory)

            def command(*arguments):
                result = run_module("deepthought.graph_cli", *arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            indexed = command(
                "index",
                directory,
                "--tool",
                "tree-sitter",
                "--repository-id",
                repo,
            )
            scope = ["--repository-id", repo, "--snapshot", indexed["snapshot_id"]]

            def qname(name):
                found = command("search", *scope, "--query", name, "--limit", "50")
                return next(
                    item["element"]["qualified_name"]
                    for item in found["items"]
                    if item["element"]["name"] == name
                )

            def names(kind, name):
                answer = command(kind, *scope, "--symbol", qname(name))
                for item in answer["items"]:
                    self.assertEqual(
                        item["relationship"]["properties"]["rule_id"], RULE
                    )
                return [item["element"]["name"] for item in answer["items"]]

            self.assertEqual(names("members", "Outer"), ["Outer.run", "Outer.Inner"])
            self.assertEqual(names("owner", "Outer.run.helper"), ["Outer.run"])
            self.assertEqual(names("owner", "top"), [])
            self.assertEqual(names("members", "N.C"), ["N.C.m"])


if __name__ == "__main__":
    unittest.main()
