"""Stored declaration source text, spans and name positions."""

import asyncio
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from deepthought import analyze_repository
from deepthought.analysis_tools import TreeSitterTool
from deepthought.analysis_tools.tree_sitter import _declaration_sources
from deepthought.storage.graph.writer import _prepare
from deepthought.syntax import _signature

READY = all(
    importlib.util.find_spec(m)
    for m in ("tree_sitter", "tree_sitter_python", "tree_sitter_typescript")
)
TYPESCRIPT = """export class Outer {
  run() {
    const marker = "😀"; const nested = () => "é";
    function duplicate() { return marker; }
    return nested();
  }
}
function duplicate() { return "outside"; }
"""
PYTHON = (
    "class Outer:\n"
    "    @staticmethod\n"
    "    @property\n"
    "    def run():\n"
    "        def inner():\n"
    '            return "é"\n'
    "        return inner()  # trailing\n"
)


def write(directory, files):
    for name, content in files.items():
        path = Path(directory) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def symbols(output):
    return {
        n.properties["name"]: n.properties for n in output.nodes if "Symbol" in n.labels
    }


@unittest.skipUnless(READY, "Provision the Python and TypeScript grammars")
class SymbolSourceTests(unittest.TestCase):
    def analyze(self, files, languages, previous=None):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            tool = TreeSitterTool(directory)
            return asyncio.run(
                tool.analyze("r", languages=languages, previous=previous)
            ), tool

    def test_typescript_text_spans_and_name_positions_in_three_encodings(self):
        output, _ = self.analyze({"a.ts": TYPESCRIPT}, ("typescript",))
        nodes = symbols(output)
        # The export keyword belongs to the declaration's text.
        self.assertEqual(
            nodes["Outer"]["source_code"],
            TYPESCRIPT[: TYPESCRIPT.index("\nfunction duplicate")],
        )
        self.assertEqual(
            (nodes["Outer"]["source_start_line"], nodes["Outer"]["start_line"]),
            (1, 1),
        )
        self.assertEqual(nodes["Outer.run.nested"]["source_code"], 'nested = () => "é"')
        self.assertEqual(
            nodes["duplicate"]["source_code"],
            'function duplicate() { return "outside"; }',
        )
        line = TYPESCRIPT.splitlines()[2]
        prefix = line[: line.index("nested")]
        self.assertEqual(
            {
                encoding: nodes["Outer.run.nested"]["name_range_" + encoding]
                for encoding in ("utf8", "utf16", "utf32")
            },
            {
                "utf8": [3, len(prefix.encode()), 3, len(prefix.encode()) + 6],
                "utf16": [3, len(prefix) + 1, 3, len(prefix) + 7],
                "utf32": [3, len(prefix), 3, len(prefix) + 6],
            },
        )
        for name, properties in nodes.items():
            self.assertEqual(
                properties["source_sha256"],
                sha256(properties["source_code"].encode()).hexdigest(),
                name,
            )
        self.assertEqual(
            output.metadata.custom_metrics["source_context"],
            {"declarations": len(nodes), "files_changed_since_analysis": []},
        )

    def test_python_decorators_and_trailing_comments_belong_to_the_text(self):
        output, _ = self.analyze({"a.py": PYTHON}, ("python",))
        nodes = symbols(output)
        run = nodes["Outer.run"]
        self.assertTrue(run["source_code"].startswith("@staticmethod\n    @property\n"))
        self.assertTrue(run["source_code"].endswith("return inner()  # trailing"))
        self.assertEqual(
            (run["source_start_line"], run["source_start_column"], run["start_line"]),
            (2, 4, 4),
        )
        self.assertEqual(run["name_range_utf8"], [4, 8, 4, 11])
        self.assertEqual(
            nodes["Outer.run.inner"]["source_code"],
            'def inner():\n            return "é"',
        )

    def test_signatures_are_the_head_up_to_the_body(self):
        files = {
            "a.py": (
                "@dec(1)\ndef f(a: int,\n      b: str = 'x') -> bool:\n    return True\n"
                "class C(Base):\n    def m(self): pass\n"
                "def long(\n" + "".join(f"    a{i},\n" for i in range(9)) + "):\n"
                "    pass\n"
                "def wide(" + ", ".join(f"argument{i}" for i in range(60)) + "): pass\n"
                "def noted(a):  # why\n    # first\n    # second\n    return a\n"
                + "".join(f"@option('--o{i}')\n" for i in range(6))
                + "def command(ctx, o0):\n    pass\n"
                "class Group:\n    @a\n    @b\n    def short(self): pass\n"
            ),
            "b.ts": (
                "export async function g<T>(x: T): Promise<T> { return x }\n"
                "export const h = (a: number) => { return a }\n"
                "interface I { p(): void }\n"
                "export type U = string | number\n"
                "let one = 1, two = (x: number) => { return x }\n"
                "declare type D = number\n"
                "declare abstract class K { }\n"
                "const commented = (a: number): number => a // why\n"
                "type Route<T> = [RegExp, T] // [pattern, handler]\n"
            ),
        }
        output, _ = self.analyze(files, ("python", "typescript"))
        nodes = symbols(output)
        self.assertEqual(
            {name: nodes[name]["signature"] for name in ("f", "C", "C.m", "g", "h")},
            {
                "f": "@dec(1)\ndef f(a: int,\n      b: str = 'x') -> bool:",
                "C": "class C(Base):",
                "C.m": "def m(self):",
                "g": "export async function g<T>(x: T): Promise<T>",
                # The statement's keyword belongs to its one declaration.
                "h": "export const h = (a: number) =>",
            },
        )
        # Decorators that would push a declaration past the cut keep the
        # first one and the declaration itself; a few stay whole.
        self.assertEqual(
            nodes["command"]["signature"], "@option('--o0')\n…\ndef command(ctx, o0):"
        )
        self.assertEqual(
            nodes["Group.short"]["signature"], "@a\n    @b\n    def short(self):"
        )
        # Comments ahead of a Python body are the body's, not the head's.
        self.assertEqual(nodes["noted"]["signature"], "def noted(a):")
        # Without a body, the whole declaration is its head.
        self.assertEqual(nodes["I.p"]["signature"], "p(): void")
        self.assertEqual(nodes["U"]["signature"], "export type U = string | number")
        # `declare` is part of the head; trailing comments are not, and a
        # comment in a statement does not count as a second declaration.
        self.assertEqual(nodes["D"]["signature"], "declare type D = number")
        self.assertEqual(nodes["K"]["signature"], "declare abstract class K")
        self.assertEqual(
            nodes["commented"]["signature"], "const commented = (a: number): number =>"
        )
        self.assertEqual(nodes["Route"]["signature"], "type Route<T> = [RegExp, T]")
        # A statement declaring more than one name keeps its keyword out.
        self.assertEqual(nodes["two"]["signature"], "two = (x: number) =>")
        # A long head is cut at 400 characters, or at five lines.
        self.assertEqual(len(nodes["wide"]["signature"]), 401)
        self.assertTrue(nodes["wide"]["signature"].endswith("…"))
        self.assertEqual(_signature("x" * 400), "x" * 400)
        self.assertEqual(_signature("x" * 401), "x" * 400 + "…")
        self.assertEqual(
            nodes["long"]["signature"],
            "def long(\n    a0,\n    a1,\n    a2,\n    a3,…",
        )

    @unittest.skipUnless(
        importlib.util.find_spec("tree_sitter_go"), "Provision the Go grammar"
    )
    def test_go_signatures_keep_the_type_keyword(self):
        output, _ = self.analyze(
            {
                "go.mod": "module m\n\ngo 1.22\n",
                "c.go": (
                    "package c\n\nfunc (r *R) M(a int) (int, error) {\n"
                    "\treturn a, nil\n}\n\ntype R struct {\n\tA int\n}\n"
                ),
            },
            ("go",),
        )
        nodes = symbols(output)
        self.assertEqual(nodes["R.M"]["signature"], "func (r *R) M(a int) (int, error)")
        self.assertEqual(nodes["R"]["signature"], "type R struct {\n\tA int\n}")

    def test_text_is_taken_only_from_the_analyzed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            write(
                directory, {"a.py": "def f():\n    pass\n", "b.py": "def g(): pass\n"}
            )
            result = analyze_repository(
                directory, repository_id="r", languages=("python",)
            )
            (Path(directory) / "a.py").write_text("def f():\n    return 1\n")
            sources, changed = _declaration_sources(directory, result)
            (Path(directory) / "b.py").unlink()
            _, gone = _declaration_sources(directory, result)
        by_symbol = {d.symbol: d.id for d in result.definitions}
        self.assertEqual(changed, ["a.py"])
        self.assertEqual(set(sources), {by_symbol["g"]})
        self.assertEqual(gone, ["a.py", "b.py"])

    def test_reused_files_carry_text_and_output_is_deterministic(self):
        files = {"a.py": PYTHON, "b.ts": TYPESCRIPT}
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            tool = TreeSitterTool(directory)
            languages = ("python", "typescript")
            full = asyncio.run(tool.analyze("r", languages=languages))
            again = asyncio.run(
                tool.analyze("r", languages=languages, previous=tool.last_analysis)
            )
            self.assertEqual(tool.last_reuse["reused"], 2)
        snapshot = full.metadata.custom_metrics["snapshot_id"]
        self.assertEqual(
            _prepare(full, "r", snapshot)[1], _prepare(again, "r", snapshot)[1]
        )
        self.assertEqual(symbols(full), symbols(again))


@unittest.skipUnless(
    READY and os.environ.get("DEEPTHOUGHT_NEO4J_URI"),
    "Provision isolated Neo4j and the grammars",
)
class SymbolSourceQueryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from deepthought.graph_queries import GraphQueryService
        from deepthought.storage.graph import Neo4jConnectionManager, setup_schema

        self.manager = Neo4jConnectionManager(
            os.environ["DEEPTHOUGHT_NEO4J_URI"],
            os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
            os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
            database=os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j"),
        )
        await self.manager.connect()
        self.addAsyncCleanup(self.manager.close)
        await setup_schema(self.manager.driver, database=self.manager.database)
        self.repo = "source-" + uuid4().hex
        self.addAsyncCleanup(self.remove)
        self.service = GraphQueryService(
            self.manager.driver, database=self.manager.database
        )

    async def remove(self):
        async with self.manager.driver.session(
            database=self.manager.database
        ) as session:
            await (
                await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=self.repo
                )
            ).consume()

    async def write(self, files):
        from deepthought.storage.graph import WriteCoordinator

        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            output = await TreeSitterTool(directory).analyze(
                self.repo, languages=("typescript",)
            )
        written = await WriteCoordinator(
            self.manager.driver, database=self.manager.database
        ).write_tool_output("tree-sitter", output, self.repo)
        self.assertTrue(written.success, written.errors)
        return output.metadata.custom_metrics["snapshot_id"], symbols(output)

    async def test_context_pages_source_by_code_point(self):
        snapshot, nodes = await self.write({"a.ts": TYPESCRIPT})
        context = await self.service.get_code_context(
            "a.ts", self.repo, snapshot_id=snapshot, line_number=3
        )
        elements = [row["element"] for row in context["items"]]
        # Innermost first: the arrow function, then run, then the class.
        self.assertEqual(
            [e["name"] for e in elements], ["Outer.run.nested", "Outer.run", "Outer"]
        )
        nested = elements[0]
        self.assertEqual(nested["source_code"], 'nested = () => "é"')
        self.assertEqual(nested["source_characters"], len('nested = () => "é"'))
        self.assertEqual(nested["source_offset"], 0)
        self.assertEqual(
            nested["name_range_utf16"], nodes["Outer.run.nested"]["name_range_utf16"]
        )
        run = nodes["Outer.run"]
        # Pages count Unicode code points, so the emoji is one character.
        start = run["source_code"].index("😀")
        page = await self.service.get_code_context(
            "a.ts",
            self.repo,
            snapshot_id=snapshot,
            definition_id=run["definition_id"],
            source_offset=start,
            max_source_characters=3,
            include_relationships=False,
        )
        self.assertEqual(
            [row["element"]["source_code"] for row in page["items"]], ['😀";']
        )
        self.assertEqual(
            page["items"][0]["element"]["source_characters"], len(run["source_code"])
        )
        bare = await self.service.get_code_context(
            "a.ts",
            self.repo,
            snapshot_id=snapshot,
            line_number=8,
            max_source_characters=0,
            include_relationships=False,
        )
        self.assertEqual(bare["items"][0]["element"]["source_code"], "")
        for bad in (
            {"source_offset": -1},
            {"max_source_characters": 8193},
            {"definition_id": ""},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                await self.service.get_code_context(
                    "a.ts", self.repo, snapshot_id=snapshot, **bad
                )

    async def test_other_queries_and_diffs_leave_the_text_out(self):
        base, _ = await self.write({"a.ts": TYPESCRIPT})
        found = await self.service.find_symbol_by_name(
            "Outer", self.repo, snapshot_id=base
        )
        properties = found["items"][0]["element"]["properties"]
        self.assertEqual(properties["name"], "Outer")
        # The head stays: a lookup can end at the graph.
        self.assertEqual(properties["signature"], "export class Outer")
        searched = await self.service.search_elements(
            "duplicate", self.repo, snapshot_id=base
        )
        self.assertEqual(
            {i["element"]["signature"] for i in searched["items"]},
            {"function duplicate()"},
        )
        listed = await self.service.search_elements("", self.repo, snapshot_id=base)
        self.assertIn(
            "export class Outer",
            {i["element"]["signature"] for i in listed["items"]},
        )
        for key in (
            "source_code",
            "source_sha256",
            "source_start_line",
            "name_range_utf16",
        ):
            self.assertNotIn(key, properties)
        head, _ = await self.write({"a.ts": TYPESCRIPT.replace("outside", "OUTSIDE")})
        diff = await self.service.diff_snapshots(
            self.repo, base_snapshot_id=base, head_snapshot_id=head
        )
        changed = {
            item["node"]["name"]: [p["key"] for p in item["properties"]]
            for listing in diff["changes"].values()
            for item in listing["items"]
            if "properties" in item
        }
        # A same-length edit changes only the text hash and the file's digest.
        self.assertEqual(
            changed, {"a.ts": ["file_sha256"], "duplicate": ["source_sha256"]}
        )


if __name__ == "__main__":
    unittest.main()
