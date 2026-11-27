"""The text check: every line naming a result's declaration, accounted for."""

import asyncio
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from deepthought.analysis_tools.contracts import (
    NodeData,
    RelationshipData,
    StandardizedOutput,
    ToolExecutionMetadata,
)
from deepthought.graph_queries import GraphQueryService
from deepthought.result_text import text_check_lines
from deepthought.text_check import declaration_lines, mentions

from evidence_fixtures import FIXTURE_EVIDENCE

GRAMMARS = all(
    importlib.util.find_spec(m)
    for m in (
        "tree_sitter",
        "tree_sitter_python",
        "tree_sitter_go",
        "tree_sitter_typescript",
    )
)

A_PY = '''class Child:
    def run(self):
        helper()

def helper():
    """helper's docstring"""
    return "helper"

# helper is called by run
def main(callback=helper):
    obj.helper()
    log(f"{helper()}")
'''
B_PY = """from a import helper
def helper():
    pass
helper()
"""


@unittest.skipUnless(GRAMMARS, "tree-sitter grammars required")
class MentionTests(unittest.TestCase):
    def test_each_line_is_code_a_comment_or_a_string_from_the_syntax_tree(self):
        self.assertEqual(
            mentions("src/a.py", A_PY.encode(), "helper"),
            {
                3: "code",
                5: "code",
                # A docstring reads like a comment.
                6: "comment",
                7: "string",
                9: "comment",
                10: "code",
                11: "code",
                12: "code",
            },
        )
        # Whole identifiers only.
        self.assertEqual(mentions("a.py", b"helpers = my_helper\n", "helper"), {})
        go = b'package a\n\n// Run runs.\nfunc Run() {}\n\nvar s = "Run"\nvar f = Run\n'
        self.assertEqual(
            mentions("a.go", go, "Run"),
            {3: "comment", 4: "code", 6: "string", 7: "code"},
        )
        ts = b"const a = `x ${load()}`;\nconst b = `load`;\n/* load */ load();\n"
        self.assertEqual(
            mentions("a.ts", ts, "load"), {1: "code", 2: "string", 3: "code"}
        )
        # A file of no analyzed language: every mention reads as code.
        self.assertEqual(mentions("notes.txt", b"# helper\n", "helper"), {1: "code"})

    def test_a_declaration_spans_its_header_down_to_its_name(self):
        source = "@decorator\n@other(helper)\ndef helper():\n    pass\n"
        # The decorator naming it is part of the header too.
        self.assertEqual(declaration_lines(source, 10, "helper"), {10, 11})
        self.assertEqual(declaration_lines(None, 4, "x"), {4})


class SentenceTests(unittest.TestCase):
    def check(self, **fields):
        return {
            "name": "helper",
            "lines": 3,
            "files": 1,
            "declared_at": ["src/a.py:5"],
            "in_result": 0,
            "declaration": 1,
            "other_declaration": 0,
            "import": 0,
            "comment": 0,
            "left_out": [],
            "unlinked": [],
            "other_targets": [],
            "strings": [],
            "not_in_graph": [],
            "result_links": True,
            **fields,
        }

    def test_a_fully_accounted_name_needs_no_text_search(self):
        self.assertEqual(
            text_check_lines([self.check(in_result=2)]),
            [
                "Text check of 'helper' (declared at src/a.py:5): 3 lines in 1"
                " analyzed file name it: 2 are links in this result; 1 is its"
                " declaration. A text search for 'helper' finds nothing more"
                " than this."
            ],
        )

    def test_links_the_result_leaves_out_are_named(self):
        left = [
            {
                "kind": "REFERENCES_FUNCTION",
                "source": "main",
                "file": "src/a.py",
                "start": 10,
                "line": 10,
            }
        ]
        line = text_check_lines([self.check(in_result=1, left_out=left)])[0]
        self.assertIn(
            "1 is a link the graph holds that this result leaves out: main"
            " (src/a.py:10) refers to the function helper at line 10.",
            line,
        )
        # When the result holds no links into it, they are the graph's links.
        line = text_check_lines([self.check(left_out=left, result_links=False)])[0]
        self.assertIn("1 is a link the graph holds to it: main", line)

    def test_lines_still_to_read_are_named_and_the_rest_is_accounted_for(self):
        line = text_check_lines(
            [
                self.check(
                    lines=6,
                    unlinked=[
                        {"at": "src/a.py:11", "reason": "receiver_type_unknown"},
                        {"at": "src/a.py:13", "reason": "scip_external"},
                    ],
                    strings=["src/a.py:6"],
                    not_in_graph=["src/a.py:12"],
                    unchecked_files=["src/c.py"],
                )
            ]
        )[0]
        self.assertTrue(
            line.endswith(
                "1 is a call the compiler index resolved outside the repository;"
                " 1 is a call no resolver linked: src/a.py:11 (receiver_type_unknown);"
                " 1 is in a string: src/a.py:6; 1 is code the graph does not link:"
                " src/a.py:12. Every other line is accounted for. Not checked,"
                " edited since the graph was built: src/c.py."
            ),
            line,
        )

    def test_a_name_too_common_to_account_for_says_so(self):
        self.assertEqual(
            text_check_lines(
                [{"name": "Run", "lines": 401, "files": 0, "too_many": True}]
            ),
            [
                "Text check of 'Run': more than 400 lines name it, too common a"
                " word to account for line by line."
            ],
        )


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_NEO4J_URI")
    and importlib.util.find_spec("neo4j")
    and GRAMMARS,
    "explicit isolated Neo4j test instance and grammars required",
)
class TextCheckIntegrationTests(unittest.TestCase):
    def test_every_line_naming_the_declaration_is_accounted_for(self):
        asyncio.run(self.exercise())

    async def exercise(self):
        from neo4j import AsyncGraphDatabase
        from deepthought.storage.graph import WriteCoordinator, setup_schema

        driver = AsyncGraphDatabase.driver(
            os.environ["DEEPTHOUGHT_NEO4J_URI"],
            auth=(
                os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
                os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
            ),
        )
        database = os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j")
        repo = "https://github.com/public/text-check-" + uuid4().hex
        snapshot = "snapshot-" + uuid4().hex

        def node(qname, labels, **props):
            return NodeData(
                labels, {"qualified_name": qname, "repo_url": repo, **props}
            )

        def symbol(qname, kind, name, path, start, end, **props):
            return node(
                qname,
                [kind, "Symbol"],
                name=name,
                kind=kind,
                file_path=path,
                start_line=start,
                end_line=end,
                **props,
            )

        nodes = [
            node(
                "fa",
                ["File"],
                path="src/a.py",
                language="python",
                file_sha256=sha256(A_PY.encode()).hexdigest(),
            ),
            node(
                "fb",
                ["File"],
                path="src/b.py",
                language="python",
                file_sha256=sha256(B_PY.encode()).hexdigest(),
            ),
            symbol("child", "Class", "Child", "src/a.py", 1, 3),
            symbol("run", "Method", "Child.run", "src/a.py", 2, 3),
            symbol(
                "helper",
                "Function",
                "helper",
                "src/a.py",
                5,
                7,
                source_code="def helper():\n    ...\n",
            ),
            symbol("main", "Function", "main", "src/a.py", 10, 12),
            symbol(
                "bhelper",
                "Function",
                "helper",
                "src/b.py",
                2,
                3,
                source_code="def helper():\n    pass\n",
            ),
            node(
                "u",
                ["UnresolvedCall"],
                name="helper",
                callee="obj.helper",
                file_path="src/a.py",
                line=11,
                status="unresolved",
                reason="receiver_type_unknown",
            ),
        ]
        byname = {n.properties["qualified_name"]: n for n in nodes}

        def edge(source, target, kind, **props):
            return RelationshipData(
                kind,
                byname[source].labels,
                {"qualified_name": source},
                byname[target].labels,
                {"qualified_name": target},
                {**FIXTURE_EVIDENCE, **props},
            )

        rels = [
            *(edge("fa", q, "DEFINES") for q in ("child", "run", "helper", "main")),
            edge("fb", "bhelper", "DEFINES"),
            edge("run", "helper", "CALLS", line=3),
            edge("main", "helper", "REFERENCES_FUNCTION", line=10, sites=1),
            edge("main", "u", "HAS_UNRESOLVED_CALL", line=11),
            edge("fb", "fa", "IMPORTS", line=1),
            edge("fb", "bhelper", "CALLS", line=4),
        ]
        meta = ToolExecutionMetadata(
            "fixture",
            "1",
            0,
            custom_metrics={
                "snapshot_id": snapshot,
                "analysis_status": "succeeded",
                "capabilities": ["definitions", "calls"],
            },
        )
        query = (
            "MATCH (c)-[r:CALLS]->(s:Symbol {name: 'helper', file_path: 'src/a.py'})"
            " RETURN r"
        )
        try:
            await setup_schema(driver, database=database)
            written = await WriteCoordinator(
                driver, database=database
            ).write_tool_output("fixture", StandardizedOutput(nodes, rels, meta), repo)
            self.assertTrue(written.success, written.errors)
            with tempfile.TemporaryDirectory() as root:
                (Path(root) / "src").mkdir()
                (Path(root) / "src/a.py").write_text(A_PY)
                (Path(root) / "src/b.py").write_text(B_PY)
                service = GraphQueryService(driver, database, workspace=root)

                async def checked(query, parameters):
                    # Rows as query_cypher returns them; raw Cypher itself
                    # needs a single-snapshot database (below).
                    from deepthought.graph_queries import _value

                    async with driver.session(database=database) as session:
                        result = await session.run(
                            query.replace(
                                " RETURN", " WITH * WHERE s.repo_url = $repo RETURN"
                            ),
                            {**parameters, "repo": repo},
                        )
                        items = [
                            {k: _value(v) for k, v in record.items()}
                            async for record in result
                        ]
                    return await service._text_check(
                        items, query, parameters, repo, snapshot, 5
                    )

                check = (await checked(query, {}))[0]
                self.assertEqual(
                    {k: v for k, v in check.items() if k != "left_out"},
                    {
                        "name": "helper",
                        "lines": 11,
                        "files": 2,
                        "declared_at": ["src/a.py:5"],
                        "in_result": 1,
                        "declaration": 1,
                        "other_declaration": 1,
                        "import": 1,
                        "comment": 2,
                        "unlinked": [
                            {"at": "src/a.py:11", "reason": "receiver_type_unknown"}
                        ],
                        "other_targets": [
                            {
                                "at": "src/b.py:4",
                                "target": "helper",
                                "target_at": "src/b.py:2",
                            }
                        ],
                        "strings": ["src/a.py:7"],
                        "not_in_graph": ["src/a.py:12"],
                        "result_links": True,
                    },
                )
                self.assertEqual(
                    check["left_out"],
                    [
                        {
                            "kind": "REFERENCES_FUNCTION",
                            "source": "main",
                            "file": "src/a.py",
                            "start": 10,
                            "line": 10,
                        }
                    ],
                )
                if os.environ.get("DEEPTHOUGHT_NEO4J_SINGLE_SNAPSHOT_TEST") == "1":
                    raw = await service.query_cypher(
                        query, {}, repo, snapshot_id=snapshot, allow_database_scope=True
                    )
                    self.assertEqual(raw["text_check"], [check])
                    text = await service.query_cypher_text(
                        query, {}, repo, snapshot_id=snapshot, allow_database_scope=True
                    )
                    self.assertEqual(
                        text.splitlines()[-1], text_check_lines([check])[0]
                    )
                # The declaration a query anchors on by file and line is the
                # subject when the result holds no links.
                check = (
                    await checked(
                        "MATCH (s:Symbol {file_path: $file}) WHERE s.start_line <= $line"
                        " <= s.end_line RETURN s.name AS name",
                        {"file": "src/a.py", "line": 6},
                    )
                )[0]
                self.assertEqual(
                    (check["in_result"], check["result_links"]), (0, False)
                )
                self.assertEqual(len(check["left_out"]), 2)
                # An edited file is not read; it is named instead.
                (Path(root) / "src/b.py").write_text(B_PY + "helper()\n")
                check = (await checked(query, {}))[0]
                self.assertEqual(check["unchecked_files"], ["src/b.py"])
                self.assertEqual(check["lines"], 8)
            # Without a working copy there is nothing to read.
            self.assertEqual(
                await GraphQueryService(driver, database)._text_check(
                    [], query, {}, repo, snapshot, 5
                ),
                [],
            )
            # Without a working copy there is nothing to read.
        finally:
            async with driver.session(database=database) as session:
                result = await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=repo
                )
                await result.consume()
            await driver.close()
