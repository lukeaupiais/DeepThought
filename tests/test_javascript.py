"""JavaScript declarations, ES module imports and name-matched calls."""

import asyncio
from dataclasses import replace
import importlib.util
from pathlib import Path
import tempfile
import unittest

import deepthought
from deepthought import analyze_repository
from deepthought.models import AnalysisResult
from deepthought.analysis_tools.evidence import registered_rules
from deepthought.calls import (
    CALL_IMPORTED_NAME,
    CALL_JS_REEXPORT,
    CALL_PRIVATE_NAME,
    CALL_SELF_METHOD,
    resolve_calls,
)
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

    def test_private_names_link_to_the_enclosing_class(self):
        result = analyze(
            {
                "main.js": (
                    "class Shelf {\n"
                    "  #store() {}\n"
                    "  add(other) { this.#store(); other.#store(); }\n"
                    "}\n"
                ),
            }
        )
        sites = {site.id: site for site in result.call_sites}
        definitions = {d.id: d for d in result.definitions}
        found = {
            sites[r.call_id].callee: (
                definitions[r.target_id].symbol if r.target_id else None,
                r.rule_id,
            )
            for r in resolve_calls(result)
        }
        self.assertEqual(
            found,
            {
                "this.#store": ("Shelf.#store", CALL_SELF_METHOD),
                "other.#store": ("Shelf.#store", CALL_PRIVATE_NAME),
            },
        )

    def test_reexports_are_followed_to_the_declaring_module(self):
        files = {
            "a.js": (
                "export function render() {}\n"
                "export function hh() {}\n"
                "export function dup() {}\n"
            ),
            "b.js": "export function dup() {}\nexport function only() {}\n",
            "c.js": "export default function Comp() {}\n",
            "named.js": (
                "export { render, hh as h } from './a.js';\n"
                "export { default as Comp } from './c.js';\n"
            ),
            "chain.js": "export { h as hyper } from './named.js';\n",
            "star.js": "export * from './a.js';\nexport * from './b.js';\n",
            "ns.js": "export * as ns from './a.js';\n",
            "cyc1.js": "export { loop } from './cyc2.js';\n",
            "cyc2.js": "export { loop } from './cyc1.js';\n",
            "imp.js": "import { render } from './a.js';\nexport { render };\n",
            "dflt.js": "export { only as default } from './b.js';\n",
            "star2.js": "export * from './dflt.js';\n",
            "named2.js": "export { default as D2 } from './dflt.js';\n",
            "twice.js": (
                "export { only } from './b.js';\nexport { hh as only } from './a.js';\n"
            ),
            "main.js": (
                "import { render, h, Comp } from './named.js';\n"
                "import { hyper } from './chain.js';\n"
                "import { only, dup } from './star.js';\n"
                "import * as S from './star.js';\n"
                "import { ns } from './ns.js';\n"
                "import { loop } from './cyc1.js';\n"
                "import { render as r2 } from './imp.js';\n"
                "import { D2 } from './named2.js';\n"
                "import * as S2 from './star2.js';\n"
                "import { render as nr } from './ns.js';\n"
                "import { only as o2 } from './twice.js';\n"
                "render(); h(); Comp(); hyper(); only(); dup();\n"
                "S.only(); ns.render(); loop(); r2(); D2(); S2.default(); nr(); o2();\n"
            ),
        }
        result = analyze(files)
        sites = {site.id: site for site in result.call_sites}
        definitions = {d.id: d for d in result.definitions}
        found = {
            sites[r.call_id].callee: (
                r.status,
                (
                    f"{definitions[r.target_id].file}:{definitions[r.target_id].symbol}"
                    if r.target_id
                    else r.reason
                ),
                r.rule_id,
            )
            for r in resolve_calls(result)
            if sites[r.call_id].file == "main.js"
        }
        self.assertEqual(
            found,
            {
                # Named re-exports, with aliases, one and two steps away.
                "render": ("linked", "a.js:render", CALL_JS_REEXPORT),
                "h": ("linked", "a.js:hh", CALL_JS_REEXPORT),
                "hyper": ("linked", "a.js:hh", CALL_JS_REEXPORT),
                # A star export gives every name but default; a name two
                # stars give is ambiguous.
                "only": ("linked", "b.js:only", CALL_JS_REEXPORT),
                "S.only": ("linked", "b.js:only", CALL_JS_REEXPORT),
                "dup": ("ambiguous", "multiple_declarations", None),
                # A default export declared in place, a namespace re-export,
                # a cycle and an import exported by a separate clause are
                # not followed.
                "Comp": ("unresolved", "imported_name_not_declared", None),
                "ns.render": ("unresolved", "attribute_call", None),
                "loop": ("unresolved", "imported_name_not_declared", None),
                "r2": ("unresolved", "imported_name_not_declared", None),
                # A default that a named re-export gives is followed; a star
                # export gives no default, and a namespace export no names.
                "D2": ("linked", "b.js:only", CALL_JS_REEXPORT),
                "S2.default": ("unresolved", "module_attribute_not_declared", None),
                "nr": ("unresolved", "imported_name_not_declared", None),
                # Two re-exports of one name, which the language rejects.
                "o2": ("unresolved", "imported_name_not_declared", None),
            },
        )
        # The same in TypeScript, through an extensionless index module.
        result = analyze(
            {
                "lib.ts": "export function make(): void {}\n",
                "index.ts": "export { make } from './lib';\n",
                "main.ts": "import { make } from './index';\nmake();\n",
            },
            languages=("typescript",),
        )
        (resolution,) = resolve_calls(result)
        definitions = {d.id: d for d in result.definitions}
        self.assertEqual(
            (resolution.rule_id, definitions[resolution.target_id].file),
            (CALL_JS_REEXPORT, "lib.ts"),
        )

    def test_a_plain_name_calls_the_declaration_its_scopes_give(self):
        lines = [
            "import { helper } from './lib.js';",
            "function top() {}",
            "export const exported = () => {};",
            "describe('x', () => {",
            "  it('a', () => {",
            "    let click = () => {};",  # 6
            "    click();",  # 7: the declaration in the same block
            "  });",
            "  it('b', () => {",
            "    let click = make();",
            "    click();",  # 11: a local that declares no function
            "    top(); exported(); helper();",  # 12: module scope, an import
            "  });",
            "  it('c', () => { click(); });",  # 14: declared in no scope around
            "  function outer(fn, { top }) {",
            "    fn(); top();",  # 16: parameters, destructured too
            "    try {} catch (helper) { helper(); }",  # 17: a catch parameter
            "    for (let step = () => {}; ; ) { step(); }",  # 18
            "    hoisted();",  # 19: var is hoisted to the function
            "    { var hoisted = () => {}; }",  # 20
            "    const named = function inner() { inner(); };",  # 21
            "    if (x) { function sloppy() {} }",
            "    sloppy();",  # 23: sloppy mode may bind it here
            "    switch (k) { case 1: const sw = () => {}; sw(); }",  # 24
            "  }",
            "  const req = require('./lib.js');",
            "  req();",  # 27: require is an import
            "  with (o) { top(); }",  # 28: with may bind any name
            "});",
        ]
        result = analyze(
            {
                "main.js": "\n".join(lines) + "\n",
                "lib.js": "export function helper() {}\n",
            }
        )
        definitions = {d.id: d for d in result.definitions}
        sites = {s.id: s for s in result.call_sites}
        found = {
            (sites[r.call_id].line, sites[r.call_id].callee): (
                f"{definitions[r.target_id].file}:{definitions[r.target_id].line}"
                if r.target_id
                else r.reason
            )
            for r in resolve_calls(result)
            if sites[r.call_id].file == "main.js"
            and sites[r.call_id].callee not in {"describe", "it", "make", "require"}
        }
        self.assertEqual(
            found,
            {
                (7, "click"): "main.js:6",
                (11, "click"): "local_binding",
                (12, "top"): "main.js:2",
                (12, "exported"): "main.js:3",
                (12, "helper"): "lib.js:1",
                (14, "click"): "unbound_name",
                (16, "fn"): "local_binding",
                (16, "top"): "local_binding",
                (17, "helper"): "local_binding",
                (18, "step"): "main.js:18",
                (19, "hoisted"): "main.js:20",
                (21, "inner"): "local_binding",
                (23, "sloppy"): "main.js:22",
                (24, "sw"): "main.js:24",
                (27, "req"): "unbound_name",
                (28, "top"): "main.js:2",
            },
        )
        bound = {(s.line, s.callee): s.binding for s in sites.values()}
        self.assertEqual(bound[(7, "click")], (6, 8))
        self.assertEqual(bound[(14, "click")], ())
        self.assertEqual(bound[(12, "top")], (2, 0))
        for line, callee in ((12, "helper"), (23, "sloppy"), (27, "req"), (28, "top")):
            with self.subTest(line=line, callee=callee):
                self.assertIsNone(bound[(line, callee)])
        self.assertEqual(AnalysisResult.from_json(result.to_json()), result)
        site = next(iter(sites.values()))
        for bad in ((1,), (0, 0), (1, -1), [1, 0], (1.0, 0)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                replace(site, binding=bad)

    def test_every_kind_of_binding_hides_the_names_further_out(self):
        lines = [
            "var mod = () => {};",
            "function f({ a: renamed }, p = 1, [first], ...rest) {",
            "  renamed(); p(); first(); rest(); mod();",  # 3
            "  for (var key in o) { key(); }",  # 4
            "  for (const item of o) { item(); }",  # 5
            "  var twice = () => {};",  # 6
            "  var twice = () => {};",
            "  twice();",  # 8: declared twice, left to the name lookup
            "}",
            "function renamed() {}",
            "function p() {}",
            "function first() {}",
            "function rest() {}",
            "function key() {}",
            "function item() {}",
        ]
        result = analyze({"main.js": "\n".join(lines) + "\n"})
        sites = {s.id: s for s in result.call_sites}
        definitions = {d.id: d for d in result.definitions}
        found = {
            (sites[r.call_id].line, sites[r.call_id].callee): (
                definitions[r.target_id].line if r.target_id else r.reason
            )
            for r in resolve_calls(result)
        }
        self.assertEqual(
            found,
            {
                (3, "renamed"): "local_binding",
                (3, "p"): "local_binding",
                (3, "first"): "local_binding",
                (3, "rest"): "local_binding",
                (3, "mod"): 1,
                (4, "key"): "local_binding",
                (5, "item"): "local_binding",
                (8, "twice"): "multiple_declarations",
            },
        )
        typescript = analyze(
            {
                "main.ts": (
                    "declare function amb(): void;\n"
                    "function amb2(x: number, y?: string) { x(); y(); amb(); }\n"
                    "function x() {}\nfunction y() {}\n"
                )
            },
            languages=("typescript",),
        )
        sites = {s.id: s for s in typescript.call_sites}
        definitions = {d.id: d for d in typescript.definitions}
        found = {
            sites[r.call_id].callee: (
                definitions[r.target_id].line if r.target_id else r.reason
            )
            for r in resolve_calls(typescript)
        }
        self.assertEqual(found, {"x": "local_binding", "y": "local_binding", "amb": 1})

    def test_a_file_that_does_not_parse_keeps_the_name_lookup(self):
        result = analyze(
            {
                "main.js": (
                    "function f() {}\n"
                    "describe(() => {\n"
                    "  const g = () => {};\n"
                    "  it(() => { f(); g(); });\n"
                    "  let = ;\n"
                    "});\n"
                )
            }
        )
        sites = {s.id: s for s in result.call_sites}
        definitions = {d.id: d for d in result.definitions}
        found = {
            sites[r.call_id].callee: (
                definitions[r.target_id].line if r.target_id else r.reason
            )
            for r in resolve_calls(result)
        }
        self.assertEqual((found["f"], found["g"]), (1, 3))
        self.assertTrue(all(s.binding is None for s in sites.values()))


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
