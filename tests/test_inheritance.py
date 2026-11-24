"""Declared bases and their name-matched INHERITS_FROM links."""

import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from deepthought import analyze_repository
from deepthought.analysis_tools import TreeSitterTool
from deepthought.analysis_tools.evidence import registered_rules
from deepthought.inheritance import (
    BASE_IMPORTED_NAME,
    BASE_LEXICAL_NAME,
    BASE_MODULE_ATTRIBUTE,
    resolve_supertypes,
)
from deepthought.models import AnalysisResult

READY = {
    language: importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_" + language)
    for language in ("javascript", "python", "typescript")
}


def write(directory, files):
    for name, content in files.items():
        path = Path(directory) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def analyze(files, language):
    with tempfile.TemporaryDirectory() as directory:
        write(directory, files)
        return analyze_repository(
            directory, repository_id="fixture", languages=(language,)
        )


def facts(result):
    """[(owner symbol, base as written, kind, line, column)]"""
    owners = {d.id: d.symbol for d in result.definitions}
    return sorted(
        (owners[t.owner_id], t.name, t.kind, t.line, t.column)
        for t in result.supertypes
    )


def links(result):
    """{(owner symbol, base): (status, target 'file::symbol', rule, reason)}"""
    items = {t.id: t for t in result.supertypes}
    definitions = {d.id: d for d in result.definitions}
    found = {}
    for r in resolve_supertypes(result):
        item = items[r.call_id]
        target = definitions.get(r.target_id)
        found[(definitions[item.owner_id].symbol, item.name)] = (
            r.status,
            f"{target.file}::{target.symbol}" if target else None,
            r.rule_id,
            r.reason,
        )
    return found


@unittest.skipUnless(READY["python"], "Provision the python extra")
class PythonBaseTests(unittest.TestCase):
    def test_bases_are_recorded_as_written_without_guessing_expressions(self):
        result = analyze(
            {
                "a.py": "class A(B, m.C, Generic[T], metaclass=M, *bases):\n"
                "    class Inner(A): pass\n"
                "class D(make()): pass\n"
            },
            "python",
        )
        self.assertEqual(
            facts(result),
            [
                ("A", "B", "extends", 1, 8),
                ("A", "Generic", "extends", 1, 16),
                ("A", "m.C", "extends", 1, 11),
                ("A.Inner", "A", "extends", 2, 16),
            ],
        )
        self.assertIn("supertypes", result.capabilities)

    def test_name_rules_link_only_one_declaration(self):
        result = analyze(
            {
                "pkg/__init__.py": "",
                "pkg/base.py": "class Base: pass\nclass Twice: pass\n",
                "pkg/other.py": "class Twice: pass\n",
                "app.py": (
                    "from pkg.base import Base\n"
                    "from pkg import base\n"
                    "import pkg.other\n"
                    "class Local: pass\n"
                    "class Dup: pass\n"
                    "class Dup: pass\n"
                    "class A(Base): pass\n"
                    "class B(base.Twice): pass\n"
                    "class C(Local, Dup, Exception, pkg.other.Twice): pass\n"
                    "class Outer:\n"
                    "    class Local: pass\n"
                    "    class Inner(Local): pass\n"
                    "def f():\n"
                    "    class Nested(Local): pass\n"
                ),
            },
            "python",
        )
        found = links(result)
        self.assertEqual(
            found[("A", "Base")],
            ("linked", "pkg/base.py::Base", BASE_IMPORTED_NAME, None),
        )
        # `from pkg import base` binds the submodule, which pkg/__init__.py
        # neither declares nor imports.
        self.assertEqual(
            found[("B", "base.Twice")],
            ("linked", "pkg/base.py::Twice", BASE_MODULE_ATTRIBUTE, None),
        )
        self.assertEqual(
            found[("C", "pkg.other.Twice")],
            ("linked", "pkg/other.py::Twice", BASE_MODULE_ATTRIBUTE, None),
        )
        self.assertEqual(
            found[("C", "Local")], ("linked", "app.py::Local", BASE_LEXICAL_NAME, None)
        )
        self.assertEqual(found[("C", "Dup")][:2], ("ambiguous", None))
        self.assertEqual(
            found[("C", "Exception")], ("unresolved", None, None, "unbound_name")
        )
        # A base is evaluated in the class body that encloses its class...
        self.assertEqual(found[("Outer.Inner", "Local")][1], "app.py::Outer.Local")
        # ...while function scopes see module names, not class-body names.
        self.assertEqual(found[("f.Nested", "Local")][1], "app.py::Local")


@unittest.skipUnless(READY["typescript"], "Provision the typescript extra")
class TypeScriptBaseTests(unittest.TestCase):
    def test_heritage_clauses_keep_their_kind_and_drop_type_arguments(self):
        result = analyze(
            {
                "a.ts": "export abstract class D<T> extends m.Base<T> "
                "implements I, n.J<T>, a.b.K {}\n"
                "interface K extends I, n.J<X> {}\n"
                "class Z extends mixin(A) {}\n"
                "const E = class extends D {};\n"
            },
            "typescript",
        )
        self.assertEqual(
            facts(result),
            [
                ("D", "I", "implements", 1, 56),
                ("D", "a.b.K", "implements", 1, 67),
                ("D", "m.Base", "extends", 1, 35),
                ("D", "n.J", "implements", 1, 59),
                ("K", "I", "extends", 2, 20),
                ("K", "n.J", "extends", 2, 23),
            ],
        )

    def test_imported_and_local_types_link(self):
        result = analyze(
            {
                "base.ts": "export class Base {}\nexport interface Shape {}\n",
                "a.ts": 'import { Base, Shape as S } from "./base";\n'
                'import * as lib from "./base";\n'
                "interface Named extends S {}\n"
                "class A extends Base implements Named, lib.Shape {}\n"
                "class B extends Error {}\n",
            },
            "typescript",
        )
        found = links(result)
        self.assertEqual(
            found[("A", "Base")],
            ("linked", "base.ts::Base", BASE_IMPORTED_NAME, None),
        )
        self.assertEqual(
            found[("Named", "S")],
            ("linked", "base.ts::Shape", BASE_IMPORTED_NAME, None),
        )
        self.assertEqual(
            found[("A", "Named")],
            ("linked", "a.ts::Named", BASE_LEXICAL_NAME, None),
        )
        self.assertEqual(
            found[("A", "lib.Shape")],
            ("linked", "base.ts::Shape", BASE_MODULE_ATTRIBUTE, None),
        )
        self.assertEqual(found[("B", "Error")][0], "unresolved")

    def test_bases_exported_under_another_name_link(self):
        result = analyze(
            {
                "base.ts": "class Hono {}\nexport { Hono as HonoBase }\n",
                "a.ts": 'import { HonoBase } from "./base";\n'
                'import * as lib from "./base";\n'
                "class A extends HonoBase {}\n"
                "class B extends lib.HonoBase {}\n",
            },
            "typescript",
        )
        found = links(result)
        self.assertEqual(
            found[("A", "HonoBase")],
            ("linked", "base.ts::Hono", BASE_IMPORTED_NAME, None),
        )
        self.assertEqual(
            found[("B", "lib.HonoBase")],
            ("linked", "base.ts::Hono", BASE_MODULE_ATTRIBUTE, None),
        )


@unittest.skipUnless(READY["javascript"], "Provision the javascript extra")
class JavaScriptBaseTests(unittest.TestCase):
    def test_extends_expression(self):
        result = analyze(
            {
                "base.js": "export class Base {}\n",
                "a.js": 'import { Base } from "./base.js";\n'
                "class A extends Base {}\nclass B extends (Base) {}\n",
            },
            "javascript",
        )
        self.assertEqual(facts(result), [("A", "Base", "extends", 2, 16)])
        self.assertEqual(links(result)[("A", "Base")][1], "base.js::Base")


@unittest.skipUnless(READY["typescript"], "Provision the typescript extra")
class InheritanceToolTests(unittest.TestCase):
    def test_edges_carry_rule_heritage_and_location(self):
        with tempfile.TemporaryDirectory() as directory:
            write(
                directory,
                {
                    "base.ts": "export class Base {}\nexport interface Shape {}\n",
                    "a.ts": 'import { Base, Shape } from "./base";\n'
                    "class A extends Base implements Shape {}\n"
                    "class B extends Error {}\n",
                },
            )
            output = asyncio.run(
                TreeSitterTool(directory).analyze("r", languages=("typescript",))
            )
        names = {n.properties["qualified_name"]: n for n in output.nodes}
        edges = sorted(
            (
                names[r.source_match["qualified_name"]].properties["name"],
                names[r.target_match["qualified_name"]].properties["name"],
                r.target_labels[0],
                r.properties["heritage"],
                r.properties["base"],
                r.properties["line"],
                r.properties["evidence_type"],
                r.properties["rule_id"],
            )
            for r in output.relationships
            if r.type == "INHERITS_FROM"
        )
        self.assertEqual(
            edges,
            [
                (
                    "A",
                    "Base",
                    "Class",
                    "extends",
                    "Base",
                    2,
                    "name_matched",
                    BASE_IMPORTED_NAME,
                ),
                (
                    "A",
                    "Shape",
                    "Interface",
                    "implements",
                    "Shape",
                    2,
                    "name_matched",
                    BASE_IMPORTED_NAME,
                ),
            ],
        )
        self.assertEqual(
            output.metadata.custom_metrics["supertype_resolution"],
            {"linked": 2, "unresolved:unbound_name": 1},
        )
        rules = registered_rules()
        for rule_id in (BASE_LEXICAL_NAME, BASE_IMPORTED_NAME, BASE_MODULE_ATTRIBUTE):
            self.assertEqual(rules[rule_id].evidence_type, "name_matched")


@unittest.skipUnless(READY["python"], "Provision the python extra")
class SupertypeSchemaTests(unittest.TestCase):
    def test_round_trip_and_owner_validation(self):
        result = analyze({"a.py": "class A: pass\nclass B(A): pass\n"}, "python")
        restored = AnalysisResult.from_json(result.to_json())
        self.assertEqual(restored, result)
        data = json.loads(result.to_json())
        data["supertypes"][0]["owner_id"] = "missing"
        with self.assertRaisesRegex(ValueError, "owners must be definitions"):
            AnalysisResult.from_json(json.dumps(data))

    def test_earlier_artifact_without_supertypes_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, {"a.py": "class A: pass\nclass B(A): pass\n"})
            full = analyze_repository(
                directory, repository_id="r", languages=("python",)
            )
            data = full.to_dict()
            del data["supertypes"]
            data["capabilities"].remove("supertypes")
            earlier = AnalysisResult.from_json(json.dumps(data))
            reuse = {}
            again = analyze_repository(
                directory,
                repository_id="r",
                languages=("python",),
                previous=earlier,
                reuse=reuse,
            )
        self.assertEqual(reuse["previous_rejected"], "missing_fact_kinds")
        self.assertEqual(again, full)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(
    READY["typescript"] and __import__("os").environ.get("DEEPTHOUGHT_NEO4J_URI"),
    "Provision isolated Neo4j and the typescript extra",
)
class InheritanceCommandTests(unittest.TestCase):
    def test_index_then_query_bases_subtypes_and_hierarchy(self):
        from uuid import uuid4

        from test_incremental import run_module
        from test_scip_graph import ScipCommandTests

        repo = "bases-cli-" + uuid4().hex
        self.addCleanup(lambda: asyncio.run(ScipCommandTests.cleanup(None, repo)))
        with tempfile.TemporaryDirectory() as directory:
            write(
                directory,
                {
                    "base.ts": "export class Base {}\nexport interface Shape {}\n"
                    "export interface Round extends Shape {}\n",
                    "a.ts": 'import { Base, Round } from "./base";\n'
                    "export class A extends Base implements Round {}\n"
                    "export class B extends A {}\n",
                },
            )

            def command(*arguments):
                result = run_module("deepthought.graph_cli", *arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            indexed = command(
                "index",
                directory,
                "--allow-uncovered",
                "--tool",
                "tree-sitter",
                "--repository-id",
                repo,
            )
            scope = ["--repository-id", repo, "--snapshot", indexed["snapshot_id"]]

            def qname(name):
                found = command("search", *scope, "--query", name, "--limit", "20")
                return next(
                    item["element"]["qualified_name"]
                    for item in found["items"]
                    if item["element"]["name"] == name
                )

            def rows(kind, name, *extra):
                answer = command(kind, *scope, "--symbol", qname(name), *extra)
                return [
                    (
                        item["element"]["name"],
                        item["relationship"]["properties"]["heritage"],
                        item["relationship"]["properties"]["evidence_type"],
                    )
                    for item in answer["items"]
                ]

            self.assertEqual(
                sorted(rows("bases", "A")),
                [
                    ("Base", "extends", "name_matched"),
                    ("Round", "implements", "name_matched"),
                ],
            )
            self.assertEqual(rows("subtypes", "A"), [("B", "extends", "name_matched")])
            self.assertEqual(rows("bases", "A", "--evidence", "resolved"), [])
            self.assertEqual(
                rows("subtypes", "Shape"), [("Round", "extends", "name_matched")]
            )
            # Ancestry follows classes and interfaces alike: B -> A -> Round -> Shape.
            ancestors = asyncio.run(
                self.hierarchy(qname("B"), repo, indexed["snapshot_id"])
            )
            self.assertEqual(
                [
                    item["element"]["properties"]["name"]
                    for item in ancestors["items"]
                    if item["direction"] == "ancestor"
                ],
                ["A", "Base", "Round", "Shape"],
            )

    async def hierarchy(self, qualified_name, repo, snapshot):
        import os

        from deepthought.graph_queries import GraphQueryService
        from deepthought.storage.graph import Neo4jConnectionManager

        manager = Neo4jConnectionManager(
            os.environ["DEEPTHOUGHT_NEO4J_URI"],
            os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
            os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
            database=os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j"),
        )
        await manager.connect()
        try:
            service = GraphQueryService(manager.driver, database=manager.database)
            return await service.get_class_hierarchy(
                qualified_name, repo, snapshot_id=snapshot
            )
        finally:
            await manager.close()
