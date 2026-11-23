"""JavaScript declarations, ES module imports and name-matched calls."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

import deepthought
from deepthought import analyze_repository
from deepthought.analysis_tools.evidence import registered_rules
from deepthought.calls import CALL_IMPORTED_NAME, CALL_SELF_METHOD, resolve_calls
from deepthought.imports import (
    JAVASCRIPT_EXTENSIONLESS_MODULE,
    JAVASCRIPT_RELATIVE_FILE,
    TYPESCRIPT_RELATIVE_MODULE,
    resolve_imports,
)

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_javascript"
)
TYPESCRIPT_READY = READY and importlib.util.find_spec("tree_sitter_typescript")
SCIP_READY = READY and importlib.util.find_spec("google.protobuf")
FIXTURE = Path(__file__).parent / "fixtures" / "javascript"
INDEX = Path(__file__).parent / "fixtures" / "scip" / "javascript.scip"


def analyze(files, languages=("javascript",)):
    with tempfile.TemporaryDirectory() as directory:
        for name, content in files.items():
            path = Path(directory) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        options = {} if languages is None else {"languages": languages}
        return analyze_repository(directory, repository_id="fixture", **options)


def resolved(result):
    """{(importing file, specifier): (status, ((target, rule), ...), reason)}"""
    imports = {item.id: item for item in result.imports}
    return {
        (imports[r.import_id].file, imports[r.import_id].specifier): (
            r.status,
            tuple((link.target_file, link.rule_id) for link in r.links),
            r.reason,
        )
        for r in resolve_imports(result)
    }


@unittest.skipUnless(READY, "Provision the javascript extra")
class JavaScriptDeclarationTests(unittest.TestCase):
    def test_declarations_in_every_javascript_extension(self):
        result = analyze(
            {
                "a.js": (
                    "export function make() {}\n"
                    "function* items() {}\n"
                    "export class Box {\n"
                    "  open() {}\n"
                    "  static of() {}\n"
                    "}\n"
                    "export const go = () => 1;\n"
                    "const plain = 1;\n"
                ),
                "b.jsx": "export const View = () => <div onClick={go} />;\n",
                "c.mjs": "export function esm() {}\n",
                "d.cjs": "function common() {}\nmodule.exports = { common };\n",
                "e.ts": "export function typed() {}\n",
            }
        )
        self.assertEqual(
            sorted((d.file, d.symbol, d.kind) for d in result.definitions),
            [
                ("a.js", "Box", "Class"),
                ("a.js", "Box.of", "Method"),
                ("a.js", "Box.open", "Method"),
                ("a.js", "go", "Function"),
                ("a.js", "items", "Function"),
                ("a.js", "make", "Function"),
                ("b.jsx", "View", "Function"),
                ("c.mjs", "esm", "Function"),
                ("d.cjs", "common", "Function"),
            ],
        )
        self.assertEqual(
            {(c.file, c.language, c.status) for c in result.coverage},
            {
                (name, "javascript", "analyzed")
                for name in ("a.js", "b.jsx", "c.mjs", "d.cjs")
            },
        )
        self.assertIn("tree-sitter-javascript", dict(result.analyzer_versions))

    def test_javascript_is_analyzed_only_when_selected(self):
        files = {"main.py": "def f():\n    pass\n", "app.js": "function g() {}\n"}
        default = analyze(files, languages=None)
        self.assertEqual({c.language for c in default.coverage}, {"python"})
        self.assertNotIn("tree-sitter-javascript", dict(default.analyzer_versions))

    def test_syntax_errors_are_partial(self):
        result = analyze({"bad.js": "function broken( {\n"})
        self.assertEqual([c.status for c in result.coverage], ["partial"])


@unittest.skipUnless(READY, "Provision the javascript extra")
class JavaScriptImportTests(unittest.TestCase):
    def test_exact_files_resolve_and_extensionless_specifiers_are_conventions(self):
        result = analyze(
            {
                "src/main.js": (
                    "import { a } from './a.js';\n"
                    "import b from './b';\n"
                    "import * as dir from './dir';\n"
                    "export { c as d } from '../lib/c.mjs';\n"
                    "import { m } from './missing.js';\n"
                    "import React from 'react';\n"
                    "const r = require('./a.js');\n"
                    "import('./a.js');\n"
                ),
                "src/a.js": "",
                "src/a.ts": "",
                "src/b.jsx": "",
                "src/dir/index.js": "",
                "lib/c.mjs": "",
            }
        )
        self.assertEqual(
            [(i.specifier, i.kind, i.names, i.local_names) for i in result.imports],
            [
                ("./a.js", "import", ("a",), ("a",)),
                ("./b", "import", ("default",), ("b",)),
                ("./dir", "import", ("*",), ("dir",)),
                ("../lib/c.mjs", "export_from", ("c",), ()),
                ("./missing.js", "import", ("m",), ("m",)),
                ("react", "import", ("default",), ("React",)),
            ],
        )
        links = resolved(result)
        for specifier, target, rule in (
            # src/a.ts is not JavaScript; a .js specifier names exactly a.js.
            ("./a.js", "src/a.js", JAVASCRIPT_RELATIVE_FILE),
            ("./b", "src/b.jsx", JAVASCRIPT_EXTENSIONLESS_MODULE),
            ("./dir", "src/dir/index.js", JAVASCRIPT_EXTENSIONLESS_MODULE),
            ("../lib/c.mjs", "lib/c.mjs", JAVASCRIPT_RELATIVE_FILE),
        ):
            with self.subTest(specifier=specifier):
                self.assertEqual(
                    links[("src/main.js", specifier)],
                    ("linked", ((target, rule),), None),
                )
        self.assertEqual(
            links[("src/main.js", "./missing.js")],
            ("unresolved", (), "no_analyzed_file"),
        )
        self.assertEqual(
            links[("src/main.js", "react")],
            ("unresolved", (), "non_relative_specifier"),
        )

    def test_extensionless_lookup_prefers_js_then_jsx_mjs_cjs_then_index(self):
        cases = (
            (("x.js", "x.jsx", "x/index.js"), "x.js"),
            (("x.jsx", "x.mjs"), "x.jsx"),
            (("x.mjs", "x.cjs"), "x.mjs"),
            (("x.cjs", "x/index.js"), "x.cjs"),
            (("x/index.js", "x/index.jsx"), "x/index.js"),
            (("x/index.jsx",), "x/index.jsx"),
        )
        for present, target in cases:
            with self.subTest(present=present):
                files = {"main.js": "import './x';\n"}
                files.update({name: "" for name in present})
                self.assertEqual(
                    resolved(analyze(files))[("main.js", "./x")],
                    ("linked", ((target, JAVASCRIPT_EXTENSIONLESS_MODULE),), None),
                )

    def test_rules_are_registered_with_their_evidence_type(self):
        import deepthought.analysis_tools.tree_sitter  # noqa: F401 (registers)

        rules = registered_rules()
        self.assertEqual(rules[JAVASCRIPT_RELATIVE_FILE].evidence_type, "resolved")
        self.assertEqual(
            rules[JAVASCRIPT_EXTENSIONLESS_MODULE].evidence_type, "pattern_matched"
        )


@unittest.skipUnless(TYPESCRIPT_READY, "Provision the javascript and typescript extras")
class TypeScriptToJavaScriptTests(unittest.TestCase):
    def test_typescript_reaches_javascript_only_after_every_typescript_candidate(self):
        files = {
            "main.ts": (
                "import { a } from './a';\n"
                "import { b } from './b';\n"
                "import { c } from './c.js';\n"
                "import { d } from './d.js';\n"
                "import { e } from './e';\n"
            ),
            "a.ts": "",
            "a.js": "",
            "b.js": "",
            "c.js": "",
            "d.ts": "",
            "d.js": "",
            "e.jsx": "",
            "e.js": "",
        }
        links = resolved(analyze(files, languages=("javascript", "typescript")))
        for specifier, target in (
            ("./a", "a.ts"),
            ("./b", "b.js"),
            ("./c.js", "c.js"),
            ("./d.js", "d.ts"),
            ("./e", "e.js"),
        ):
            with self.subTest(specifier=specifier):
                self.assertEqual(
                    links[("main.ts", specifier)],
                    ("linked", ((target, TYPESCRIPT_RELATIVE_MODULE),), None),
                )
        # Without the javascript language no .js file is analyzed, so the
        # TypeScript-only result is unchanged.
        only = resolved(analyze(files, languages=("typescript",)))
        self.assertEqual(
            only[("main.ts", "./b")], ("unresolved", (), "no_analyzed_file")
        )


@unittest.skipUnless(READY, "Provision the javascript extra")
class JavaScriptCallTests(unittest.TestCase):
    def test_imported_names_and_this_methods_link_by_name(self):
        result = analyze(
            {
                "lib.js": "export function make() {}\n",
                "main.js": (
                    "import { make as build } from './lib.js';\n"
                    "class Shelf {\n"
                    "  add() { this.store(); build(); }\n"
                    "  store() {}\n"
                    "}\n"
                ),
            }
        )
        sites = {site.id: site for site in result.call_sites}
        definitions = {d.id: d for d in result.definitions}
        found = {
            sites[r.call_id].callee: (
                r.status,
                definitions[r.target_id].symbol if r.target_id else None,
                r.rule_id,
            )
            for r in resolve_calls(result)
        }
        self.assertEqual(
            found,
            {
                "this.store": ("linked", "Shelf.store", CALL_SELF_METHOD),
                "build": ("linked", "make", CALL_IMPORTED_NAME),
            },
        )


@unittest.skipUnless(SCIP_READY, "Provision the javascript and scip extras")
class JavaScriptDifferentialTests(unittest.TestCase):
    """Independent check: a scip-typescript index of the same fixture (allowJs)."""

    def setUp(self):
        self.result = analyze_repository(
            FIXTURE, repository_id="r", languages=("javascript",)
        )
        self.graph = deepthought.compose_graph(
            [deepthought.ingest_scip(INDEX, repository_id="javascript")]
        )

    def test_import_edges_match_scip_witnessed_file_dependencies(self):
        from deepthought.analysis_tools import TreeSitterTool

        output = asyncio.run(
            TreeSitterTool(FIXTURE).analyze("r", languages=["javascript"])
        )
        observed = {
            (
                r.source_match["qualified_name"].split("::")[1],
                r.target_match["qualified_name"].split("::")[1],
            )
            for r in output.relationships
            if r.type == "IMPORTS"
        }
        expected = {(e.source_file, e.target_file) for e in self.graph.edges}
        self.assertEqual(len(expected), 3)
        self.assertEqual(observed, expected)

    def test_linked_calls_agree_with_scip_references_and_are_deterministic(self):
        again = analyze_repository(
            FIXTURE, repository_id="r", languages=("javascript",)
        )
        self.assertEqual(self.result.to_json(), again.to_json())
        self.assertEqual(resolve_calls(self.result), resolve_calls(again))
        scip_defs = {n.id: n.definition for n in self.graph.definitions}
        referenced = {
            (r.reference.file, r.reference.line, r.reference.column): scip_defs[
                r.target_ids[0]
            ]
            for r in self.graph.references
            if r.resolution == "resolved"
        }
        sites = {s.id: s for s in self.result.call_sites}
        definitions = {d.id: d for d in self.result.definitions}
        linked = [r for r in resolve_calls(self.result) if r.status == "linked"]
        self.assertEqual(len(linked), 3)
        for resolution in linked:
            site = sites[resolution.call_id]
            target = definitions[resolution.target_id]
            line = (FIXTURE / site.file).read_text().splitlines()[site.line - 1]
            column = line.index(site.callee.split(".")[-1], site.column)
            expected = referenced[(site.file, site.line, column)]
            self.assertEqual(expected.file, target.file)
            self.assertTrue(target.line <= expected.line <= target.end_line)


if __name__ == "__main__":
    unittest.main()
