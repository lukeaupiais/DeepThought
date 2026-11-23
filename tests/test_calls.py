"""Named call sites and their name-matched links to declarations."""

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import deepthought
from deepthought import analyze_repository
from deepthought.analysis_tools.evidence import registered_rules
from deepthought.calls import (
    CALL_IMPORTED_NAME,
    CALL_LEXICAL_NAME,
    CALL_MODULE_ATTRIBUTE,
    CALL_SELF_METHOD,
    resolve_calls,
)
from deepthought.models import AnalysisResult, CallSite, FileCoverage

FIXTURES = Path(__file__).parent / "fixtures"
PYTHON_READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_python"
)
TYPESCRIPT_READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_typescript"
)


def analyze(files, language):
    with tempfile.TemporaryDirectory() as directory:
        for name, content in files.items():
            path = Path(directory) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return analyze_repository(
            directory, repository_id="fixture", languages=(language,)
        )


def links(result):
    """{(file, line, callee): (status, target 'file::symbol', rule, reason)}"""
    sites = {site.id: site for site in result.call_sites}
    definitions = {d.id: d for d in result.definitions}
    found = {}
    for r in resolve_calls(result):
        site = sites[r.call_id]
        target = definitions.get(r.target_id)
        found[(site.file, site.line, site.callee)] = (
            r.status,
            f"{target.file}::{target.symbol}" if target else None,
            r.rule_id,
            r.reason,
        )
    return found


@unittest.skipUnless(PYTHON_READY, "Provision the python extra")
class PythonCallTests(unittest.TestCase):
    def test_call_sites_record_callee_kind_and_innermost_caller(self):
        result = analyze(
            {
                "main.py": (
                    "def outer():\n"
                    "    def inner():\n"
                    "        return a.b.c()\n"
                    "    return inner()\n"
                    "setup()\n"
                    "f()()\n"
                )
            },
            "python",
        )
        symbols = {d.id: d.symbol for d in result.definitions}
        self.assertEqual(
            [
                (s.callee, s.kind, s.line, symbols.get(s.caller_id))
                for s in result.call_sites
            ],
            [
                ("a.b.c", "call", 3, "outer.inner"),
                ("inner", "call", 4, "outer"),
                ("setup", "call", 5, None),
                ("f", "call", 6, None),
            ],
        )

    def test_lexical_scopes_skip_class_bodies_and_report_ambiguity(self):
        result = analyze(
            {
                "main.py": (
                    "def helper(): pass\n"
                    "class C:\n"
                    "    def helper(self): pass\n"
                    "    def run(self):\n"
                    "        helper()\n"
                    "        self.helper()\n"
                    "        self.missing()\n"
                    "def twice(): pass\n"
                    "def twice(): pass\n"
                    "def go():\n"
                    "    twice()\n"
                    "    undefined()\n"
                    "helper()\n"
                    "self.outside()\n"
                )
            },
            "python",
        )
        found = links(result)
        self.assertEqual(
            found[("main.py", 5, "helper")],
            ("linked", "main.py::helper", CALL_LEXICAL_NAME, None),
        )
        self.assertEqual(
            found[("main.py", 6, "self.helper")],
            ("linked", "main.py::C.helper", CALL_SELF_METHOD, None),
        )
        self.assertEqual(
            found[("main.py", 7, "self.missing")],
            ("unresolved", None, None, "not_declared_in_class"),
        )
        self.assertEqual(
            found[("main.py", 11, "twice")],
            ("ambiguous", None, None, "multiple_declarations"),
        )
        self.assertEqual(
            found[("main.py", 12, "undefined")],
            ("unresolved", None, None, "unbound_name"),
        )
        self.assertEqual(
            found[("main.py", 13, "helper")],
            ("linked", "main.py::helper", CALL_LEXICAL_NAME, None),
        )
        self.assertEqual(
            found[("main.py", 14, "self.outside")],
            ("unresolved", None, None, "outside_class"),
        )

    def test_import_bindings_follow_aliases_and_module_attributes(self):
        result = analyze(
            {
                "lib/__init__.py": "",
                "lib/tools.py": "def work(): pass\nclass Tool: pass\n",
                "main.py": (
                    "from lib.tools import work as run, Tool\n"
                    "import lib.tools\n"
                    "import lib.tools as t\n"
                    "from lib.tools import gone\n"
                    "import json\n"
                    "def go():\n"
                    "    run()\n"
                    "    Tool()\n"
                    "    lib.tools.work()\n"
                    "    t.work()\n"
                    "    gone()\n"
                    "    json.dumps()\n"
                ),
                "shadow.py": (
                    "from lib.tools import work\ndef work(): pass\ndef go():\n    work()\n"
                ),
            },
            "python",
        )
        found = links(result)
        for line, callee, target, rule in (
            (7, "run", "lib/tools.py::work", CALL_IMPORTED_NAME),
            (8, "Tool", "lib/tools.py::Tool", CALL_IMPORTED_NAME),
            (9, "lib.tools.work", "lib/tools.py::work", CALL_MODULE_ATTRIBUTE),
            (10, "t.work", "lib/tools.py::work", CALL_MODULE_ATTRIBUTE),
        ):
            with self.subTest(callee=callee):
                self.assertEqual(
                    found[("main.py", line, callee)], ("linked", target, rule, None)
                )
        self.assertEqual(
            found[("main.py", 11, "gone")],
            ("unresolved", None, None, "imported_name_not_declared"),
        )
        self.assertEqual(
            found[("main.py", 12, "json.dumps")],
            ("unresolved", None, None, "module_attribute_not_declared"),
        )
        self.assertEqual(
            found[("shadow.py", 4, "work")],
            ("ambiguous", None, None, "multiple_bindings"),
        )


@unittest.skipUnless(TYPESCRIPT_READY, "Provision the typescript extra")
class TypeScriptCallTests(unittest.TestCase):
    def test_typescript_bindings_constructors_and_this_methods(self):
        result = analyze(
            {
                "lib.ts": "export function make() {}\nexport class Box {}\n",
                "main.ts": (
                    "import { make as build, Box } from './lib';\n"
                    "import * as lib from './lib';\n"
                    "import Def from './lib';\n"
                    "class Shelf {\n"
                    "  add() { this.store(); new Box(); }\n"
                    "  store() {}\n"
                    "}\n"
                    "export const go = () => { build(); lib.make(); Def(); obj?.x(); };\n"
                ),
            },
            "typescript",
        )
        found = links(result)
        for line, callee, target, rule in (
            (5, "this.store", "main.ts::Shelf.store", CALL_SELF_METHOD),
            (5, "Box", "lib.ts::Box", CALL_IMPORTED_NAME),
            (8, "build", "lib.ts::make", CALL_IMPORTED_NAME),
            (8, "lib.make", "lib.ts::make", CALL_MODULE_ATTRIBUTE),
        ):
            with self.subTest(callee=callee):
                self.assertEqual(
                    found[("main.ts", line, callee)], ("linked", target, rule, None)
                )
        self.assertEqual(
            found[("main.ts", 8, "Def")],
            ("unresolved", None, None, "unsupported_binding"),
        )
        self.assertNotIn(("main.ts", 8, "obj.x"), found)
        kinds = {(s.callee, s.kind) for s in result.call_sites}
        self.assertIn(("Box", "new"), kinds)


class CallSchemaTests(unittest.TestCase):
    def test_call_sites_need_capability_and_same_file_callers(self):
        base = dict(
            repository_id="r",
            snapshot_id="s",
            status="succeeded",
            definitions=(),
            coverage=(FileCoverage("a.py", "python", "analyzed"),),
            diagnostics=(),
            analyzer="tree-sitter",
            analyzer_versions=(),
            schema_version="3",
        )
        site = CallSite("c1", "a.py", "f", "call", 1, 0, 1, 3)
        with self.assertRaisesRegex(ValueError, "capability"):
            AnalysisResult(**base, call_sites=(site,))
        with self.assertRaisesRegex(ValueError, "same file"):
            AnalysisResult(
                **base,
                capabilities=("call_sites", "definitions"),
                call_sites=(CallSite("c2", "a.py", "f", "call", 1, 0, 1, 3, "x"),),
            )
        current = AnalysisResult(
            **base, capabilities=("call_sites", "definitions"), call_sites=(site,)
        )
        self.assertEqual(AnalysisResult.from_json(current.to_json()), current)
        for bad in (dict(kind="await"), dict(callee=""), dict(caller_id="")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                CallSite(
                    **{
                        **dict(
                            id="c",
                            file="a.py",
                            callee="f",
                            kind="call",
                            line=1,
                            column=0,
                            end_line=1,
                            end_column=3,
                        ),
                        **bad,
                    }
                )


@unittest.skipUnless(
    PYTHON_READY and TYPESCRIPT_READY and importlib.util.find_spec("google.protobuf"),
    "Provision the python, typescript and scip extras",
)
class CallDifferentialTests(unittest.TestCase):
    def test_linked_calls_agree_with_scip_references_and_are_deterministic(self):
        """Independent check: compiler-backed SCIP indexes of the same fixtures."""
        for language in ("python", "typescript"):
            with self.subTest(language=language):
                result = analyze_repository(FIXTURES / language, repository_id="r")
                self.assertEqual(
                    resolve_calls(result),
                    resolve_calls(
                        analyze_repository(FIXTURES / language, repository_id="r")
                    ),
                )
                graph = deepthought.compose_graph(
                    [
                        deepthought.ingest_scip(
                            FIXTURES / "scip" / f"{language}.scip",
                            repository_id=language,
                        )
                    ]
                )
                scip_defs = {n.id: n.definition for n in graph.definitions}
                referenced = {
                    (r.reference.file, r.reference.line, r.reference.column): scip_defs[
                        r.target_ids[0]
                    ]
                    for r in graph.references
                    if r.resolution == "resolved"
                }
                sites = {s.id: s for s in result.call_sites}
                definitions = {d.id: d for d in result.definitions}
                linked = [r for r in resolve_calls(result) if r.status == "linked"]
                self.assertEqual(len(linked), 3)
                for resolution in linked:
                    site = sites[resolution.call_id]
                    target = definitions[resolution.target_id]
                    text = (FIXTURES / language / site.file).read_text()
                    line = text.splitlines()[site.line - 1]
                    column = line.index(site.callee.split(".")[-1], site.column)
                    expected = referenced[(site.file, site.line, column)]
                    self.assertEqual(expected.file, target.file)
                    self.assertTrue(target.line <= expected.line <= target.end_line)


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_NEO4J_URI")
    and importlib.util.find_spec("neo4j")
    and PYTHON_READY,
    "Explicit isolated Neo4j test settings, neo4j and python extras required",
)
class CallNeo4jIntegrationTests(unittest.IsolatedAsyncioTestCase):
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
        self.coordinator = WriteCoordinator(self.driver, database=self.database)

    async def asyncTearDown(self):
        try:
            async with self.driver.session(database=self.database) as session:
                result = await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=self.repo
                )
                await result.consume()
        finally:
            await self.manager.close()

    async def test_callers_and_callees_queries_return_name_matched_edges(self):
        from deepthought.analysis_tools import TreeSitterTool
        from deepthought.graph_queries import GraphQueryService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text(
                "def helper(): pass\ndef run():\n    helper()\nhelper()\n"
            )
            output = await TreeSitterTool(root).analyze(self.repo)
        result = await self.coordinator.write_tool_output(
            "tree-sitter", output, self.repo
        )
        self.assertTrue(result.success, result.errors)
        helper = next(
            n.properties["qualified_name"]
            for n in output.nodes
            if n.properties.get("name") == "helper"
        )
        service = GraphQueryService(self.driver, self.database)
        callers = await service.get_function_callers(
            helper, self.repo, snapshot_id=result.snapshot_id
        )
        observed = sorted(
            (
                item["element"].get("name") or item["element"]["qualified_name"],
                item["relationship"]["properties"]["rule_id"],
                item["relationship"]["properties"]["evidence_type"],
            )
            for item in callers["items"]
        )
        self.assertEqual(
            observed,
            [
                ("main.py", CALL_LEXICAL_NAME, "name_matched"),
                ("run", CALL_LEXICAL_NAME, "name_matched"),
            ],
        )
        self.assertEqual(
            registered_rules()[CALL_LEXICAL_NAME].evidence_type, "name_matched"
        )
        run = next(
            n.properties["qualified_name"]
            for n in output.nodes
            if n.properties.get("name") == "run"
        )
        callees = await service.get_function_calls(
            run, self.repo, snapshot_id=result.snapshot_id
        )
        self.assertEqual([i["element"]["name"] for i in callees["items"]], ["helper"])
        # Name matches never claim completeness: unresolved calls may exist.
        self.assertNotIn("calls", callees["capabilities"])


if __name__ == "__main__":
    unittest.main()
