"""Named declaration regressions through the SDK, reduced from real projects."""

from pathlib import Path
import tempfile
import unittest

from deepthought import QueryRequest, analyze_repository, compose_graph, query_graph

FIXTURES = Path(__file__).parent / "fixtures"


class DeclarationTests(unittest.TestCase):
    def test_typescript_overloads_preserve_every_declaration_and_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "overloads.ts").write_text(
                "export function choose(value: string): string;\n"
                "export function choose(value: number): number;\n"
                "export function choose(value: string | number) { return value; }\n"
            )
            result = analyze_repository(
                directory, repository_id="fixture", languages=("typescript",)
            )
            self.assertEqual(
                [(d.symbol, d.kind, d.line) for d in result.definitions],
                [("choose", "Function", line) for line in (1, 2, 3)],
            )
            self.assertEqual(len({d.id for d in result.definitions}), 3)

    def test_interface_methods_and_bound_arrows_are_named_declarations(self):
        result = analyze_repository(
            FIXTURES / "declarations",
            repository_id="fixture",
            languages=("typescript",),
            include=("declaration_cases.ts",),
        )
        self.assertEqual(
            {(d.symbol, d.kind, d.line) for d in result.definitions},
            {
                ("Service", "Interface", 1),
                ("Service.execute", "Method", 2),
                ("createService", "Function", 5),
                ("createService.normalize", "Function", 6),
            },
        )

    def test_tsx_and_unicode_locations_use_utf8_bytes(self):
        result = analyze_repository(
            FIXTURES / "declarations",
            repository_id="fixture",
            languages=("typescript",),
            include=("components.tsx",),
        )
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(
            {(d.symbol, d.kind, d.line, d.column) for d in result.definitions},
            {("Card", "Function", 1, 13), ("café", "Function", 2, 18)},
        )
        self.assertTrue(
            all(d.coordinate_encoding == "utf-8" for d in result.definitions)
        )

    def test_namespace_owners_distinguish_same_named_classes(self):
        result = analyze_repository(
            FIXTURES / "declarations",
            repository_id="fixture",
            languages=("typescript",),
            include=("namespaces.ts",),
        )
        self.assertEqual(
            {(d.symbol, d.kind) for d in result.definitions},
            {
                ("First", "Namespace"),
                ("First.Item", "Class"),
                ("First.Item.execute", "Method"),
                ("Second", "Namespace"),
                ("Second.Item", "Class"),
                ("Second.Item.execute", "Method"),
            },
        )

    def test_import_aliases_do_not_invent_function_definitions(self):
        for language, name, content in [
            (
                "python",
                "sample.py",
                "from helpers import helper as alternate\ndef consume():\n    return alternate()\n",
            ),
            (
                "typescript",
                "sample.ts",
                'import { helper as alternate } from "./helpers";\nexport function consume() { return alternate(); }\n',
            ),
        ]:
            with (
                self.subTest(language=language),
                tempfile.TemporaryDirectory() as directory,
            ):
                (Path(directory) / name).write_text(content)
                result = analyze_repository(
                    directory, repository_id="fixture", languages=(language,)
                )
                self.assertEqual([d.symbol for d in result.definitions], ["consume"])
                self.assertEqual(
                    result.capabilities,
                    (
                        "call_sites",
                        "definitions",
                        "impls",
                        "imports",
                        "packages",
                        "supertypes",
                    ),
                )
                self.assertEqual(
                    [i.specifier for i in result.imports],
                    ["helpers" if language == "python" else "./helpers"],
                )

    def test_jsx_class_attributes_parse(self):
        # The JavaScript grammar rejects `class` as a JSX attribute name.
        with tempfile.TemporaryDirectory() as directory:
            for name, text in (
                (
                    "view.jsx",
                    'function View() { return <div class="a" />; }\n'
                    'function Other() { render(<ul class = "b">x</ul>); }\n'
                    "const same = a.class == b;\n",
                ),
                ("bad.jsx", 'function Bad( { <div class="a" />\n'),
            ):
                (Path(directory) / name).write_text(text)
            result = analyze_repository(
                directory, repository_id="fixture", languages=("javascript",)
            )
        self.assertEqual(
            {c.file: c.status for c in result.coverage},
            {"view.jsx": "analyzed", "bad.jsx": "partial"},
        )
        self.assertEqual(
            sorted(
                (d.symbol, d.line, d.column, d.end_column)
                for d in result.definitions
                if d.file == "view.jsx"
            ),
            [("Other", 2, 0, 52), ("View", 1, 0, 45)],
        )
        self.assertEqual(
            [(s.callee, s.line) for s in result.call_sites], [("render", 2)]
        )

    def test_generic_call_signatures_without_separators_parse(self):
        # The grammar reads `<K ...>(...)` after a member that ends without
        # a separator as part of that member's type.
        interface = (
            "export interface Getter {\n"
            "  <K extends string>(key: K): K\n"
            "  <K extends number>(key: K): K\n"
            "  (key: boolean): boolean\n"
            "}\n"
            "export class After { run() {} }\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            for name, text in (
                ("getter.ts", interface),
                ("view.tsx", interface),
                ("bad.ts", "export function Bad( {\n  <T>(x: T): T\n"),
            ):
                (Path(directory) / name).write_text(text)
            result = analyze_repository(
                directory, repository_id="fixture", languages=("typescript",)
            )
        self.assertEqual(
            {c.file: c.status for c in result.coverage},
            {"getter.ts": "analyzed", "view.tsx": "partial", "bad.ts": "partial"},
        )
        self.assertIn(
            ("Getter", "Interface", 1, 5, 7, 1),
            {
                (d.symbol, d.kind, d.line, d.end_line, d.column, d.end_column)
                for d in result.definitions
                if d.file == "getter.ts"
            },
        )
        self.assertIn(
            ("After.run", 6),
            {(d.symbol, d.line) for d in result.definitions if d.file == "getter.ts"},
        )

    def test_signature_recovery_reads_only_blanks_before_errors(self):
        from deepthought.syntax import _error_rows, make_parser, parse_source

        parser = make_parser("typescript", ".ts")
        for text, errors in (
            # Each `<` line after a member without a separator, also after a
            # method or a signature over several lines, and in a type literal.
            ("interface G {\n  (a: string): string\n  <K>(k: K): K\n}\n", 0),
            ("interface G {\n  get(a: string): string\n  <K>(k: K): K\n}\n", 0),
            (
                "interface G {\n  (a: string): string\n  <K>(k: K): K\n"
                "  <J>(k: J): J\n  <L>(k: L): L\n}\n",
                0,
            ),
            (
                "interface G {\n  (a: string): Promise<\n    string\n  >\n"
                "  <K>(k: K): K\n  <J>(\n    k: J\n  ): J\n  <L>(k: L): L\n}\n",
                0,
            ),
            ("type G = {\n  (a: string): string\n  <K>(k: K): K\n}\n", 0),
            # Signatures over several lines, where the error ends on the
            # line before the `<`.
            (
                "interface G {\n  <\n    P extends string,\n  >(\n    handler: P\n"
                "  ): Base<\n    P\n  >\n\n  // comment\n  <\n    Q extends string,\n"
                "  >(\n    q: Q\n  ): Base<Q>\n  <R>(r: R): R\n}\n",
                0,
            ),
            # A `<` that continues an expression is read as written.
            (
                "interface G {\n  (a: string): string\n  <K>(k: K): K\n}\n"
                "let y = 1\n  < 2\n",
                0,
            ),
            # A `;` would not help: the tree is the one parsed as written.
            ("let y = [1\n  < 2\nfunction f() {}\n", 2),
            # Without a blank before `<`, nothing is read differently.
            ("interface G {\n  (a: string): string\n<K>(k: K): K\n}\n", 2),
        ):
            with self.subTest(text=text):
                content = text.encode()
                tree = parse_source(parser, "typescript", ".ts", content)
                self.assertEqual(_error_rows(tree)[0], errors)
                if errors:
                    self.assertEqual(
                        str(tree.root_node), str(parser.parse(content).root_node)
                    )

    def test_malformed_tsx_has_explicit_partial_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "good.tsx").write_text(
                "export function Good() { return <div/>; }\n"
            )
            (Path(directory) / "bad.tsx").write_text(
                "export function Bad( { return <\n"
            )
            result = analyze_repository(
                directory, repository_id="fixture", languages=("typescript",)
            )
            self.assertEqual(result.status, "partial")
            self.assertIn("syntax_error", {d.code for d in result.diagnostics})
            self.assertEqual(
                {c.file: c.status for c in result.coverage},
                {"good.tsx": "analyzed", "bad.tsx": "partial"},
            )

    def test_python_unicode_symbol_and_end_column_are_lossless(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "sample.py").write_text("def café(): pass\n")
            result = analyze_repository(
                directory, repository_id="fixture", languages=("python",)
            )
            self.assertEqual(
                (result.definitions[0].symbol, result.definitions[0].end_column),
                ("café", 17),
            )


class ModuleScopeTests(unittest.TestCase):
    def test_ambient_modules_preserve_owners_in_definitions_and_queries(self):
        result = analyze_repository(
            FIXTURES / "declarations",
            repository_id="fixture",
            languages=("typescript",),
            include=("modules.ts",),
        )
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(
            {(d.symbol, d.kind) for d in result.definitions},
            {
                (f'"@example/{module}"{suffix}', kind)
                for module in ("first", "second")
                for suffix, kind in (
                    ("", "Namespace"),
                    (".Item", "Interface"),
                    (".Item.run", "Method"),
                    (".create", "Function"),
                )
            },
        )
        graph = compose_graph([result])
        for module in ("first", "second"):
            symbol = f'"@example/{module}".Item'
            query = query_graph(
                graph, QueryRequest("definitions", "fixture", symbol=symbol)
            )
            self.assertEqual(query.total_matches, 1)
            self.assertEqual(query.matches[0]["definition"]["symbol"], symbol)
        self.assertEqual(
            query_graph(
                graph, QueryRequest("definitions", "fixture", symbol="Item")
            ).total_matches,
            0,
        )

    def test_adding_another_module_does_not_rename_existing_declarations(self):
        source = (FIXTURES / "declarations/modules.ts").read_text()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "modules.ts"
            path.write_text(source)
            before = analyze_repository(
                directory, repository_id="fixture", languages=("typescript",)
            )
            path.write_text(
                'declare module "@example/earlier" { export interface Item {} }\n'
                + source
            )
            after = analyze_repository(
                directory, repository_id="fixture", languages=("typescript",)
            )
        self.assertEqual(
            {d.symbol: d.id for d in before.definitions},
            {
                d.symbol: d.id
                for d in after.definitions
                if not d.symbol.startswith('"@example/earlier"')
            },
        )
