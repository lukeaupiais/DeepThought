"""JavaScript and TypeScript imports bound from SCIP module references
(deepthought.scip_imports)."""

import asyncio
from pathlib import Path
import tempfile
import unittest

import deepthought
from deepthought import imports as import_rules
from deepthought.analysis_tools.tree_sitter import TreeSitterTool
from deepthought.engine import analyze_repository
from deepthought.scip_imports import bind_imports
import test_scip_tool as fixtures

P = "scip-typescript npm demo 1 "
A = (
    'import { x as é } from "./m";\n'
    "import React from 'react';\n"
    'import "./side";\n'
    'import { y } from "./aug";\n'
    'import { z } from "./two";\n'
    'import q from "./gone";\n'
    'import k from "./m2"; const s = "./m";\n'
    'import { m } from "./m3"; import {m} from "m";\n'
    'import e from "ext";\n'
    'import w from "./multi";\n'
    '"./v"; import v from "./v";\n'
)
FILES = {
    "a.ts": A,
    "b.ts": 'import { x } from "./m";\n',
    "m.ts": "export const x = 1;\n",
    "m2.ts": "export default 2;\n",
    "m3.ts": "export const m = 3;\n",
    "multi.ts": "export default 4;\n",
    "v.ts": "export default 5;\n",
    "side.ts": "console.log(1);\n",
    "aug.ts": "export const y = 1;\n",
    "d.ts": 'declare module "./aug" { export const extra: number }\n',
    "two.ts": "export const z = 1;\n",
    "two.js": "export const z = 1;\n",
    "p.py": "import m\n",
    "m.py": "",
}


def _module(name):
    return f"{P}`{name}`/"


@unittest.skipUnless(fixtures.PROTOBUF, "Provision the scip extra")
class ScipImportTests(unittest.TestCase):
    def setUp(self):
        from deepthought._scip import scip_pb2

        self.wire = scip_pb2
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "ws"
        self.root.mkdir()
        for name, text in FILES.items():
            (self.root / name).write_text(text)
        self.indexes = Path(temporary.name)

    def index(self, documents, name="ts"):
        """An ingested index whose documents are (path, [(line, text, nth,
        symbol, definition)]); a definition with no text is the module's,
        at the start of the file. ``(range, symbol, definition)`` gives
        the range itself."""
        wire = self.wire
        index = wire.Index()
        index.metadata.tool_info.name = "scip-typescript"
        index.metadata.tool_info.version = "1.0"
        for path, occurrences in documents:
            source = self.root / path
            lines = source.read_text().split("\n") if source.exists() else [""]
            document = index.documents.add(relative_path=path)
            document.position_encoding = 2
            for occurrence in occurrences:
                if len(occurrence) == 3:
                    span, symbol, definition = occurrence
                    document.occurrences.add(
                        symbol=symbol,
                        range=span,
                        symbol_roles=wire.Definition if definition else 0,
                    )
                    continue
                line, text, nth, symbol, definition = occurrence
                start = -1
                for _ in range(nth + 1):
                    start = lines[line].index(text, start + 1)
                document.occurrences.add(
                    symbol=symbol,
                    range=[line, start, start + len(text)],
                    symbol_roles=wire.Definition if definition else 0,
                )
        path = self.indexes / f"{name}.scip"
        path.write_bytes(index.SerializeToString())
        return deepthought.ingest_scip(path, repository_id="r"), f"sha256:{name}"

    def typescript(self):
        defined = [
            (name, [(0, "", 0, _module(name), True)])
            for name in (
                "m.ts",
                "m2.ts",
                "m3.ts",
                "multi.ts",
                "v.ts",
                "side.ts",
                "aug.ts",
                "two.ts",
                "two.js",
            )
        ]
        return self.index(
            [
                (
                    "a.ts",
                    [
                        (0, "", 0, _module("a.ts"), True),
                        (0, '"./m"', 0, _module("m.ts"), False),
                        # A package: its module symbol has no definition.
                        (
                            1,
                            "'react'",
                            0,
                            "scip-typescript npm react 18 `index.d.ts`/",
                            False,
                        ),
                        # `import "./side"`: scip-typescript records nothing.
                        (3, '"./aug"', 0, _module("aug.ts"), False),
                        (3, '"./aug"', 0, f'{P}`d.ts`/"./aug"/', False),
                        (4, '"./two"', 0, _module("two.ts"), False),
                        (4, '"./two"', 0, _module("two.js"), False),
                        (5, '"./gone"', 0, _module("gone.ts"), False),
                        # The module of `./m2` referenced over another
                        # string: only the specifier names the module.
                        (6, '"./m"', 0, _module("m2.ts"), False),
                        # Not over the specifier, or not a module symbol.
                        (7, "m", 1, _module("m3.ts"), False),
                        (7, '"./m3"', 0, f"{P}`m3.ts`/m.", False),
                        (7, "{m}", 0, _module("m3.ts"), False),
                        # Two packages, neither defined here.
                        (8, '"ext"', 0, "scip-typescript npm ext 1 `a.d.ts`/", False),
                        (8, '"ext"', 0, "scip-typescript npm ext 1 `b.d.ts`/", False),
                        # A range over two lines is no specifier.
                        ([9, 14, 10, 23], _module("multi.ts"), False),
                        # Before the statement.
                        (10, '"./v"', 0, _module("v.ts"), False),
                    ],
                ),
                (
                    "d.ts",
                    [
                        (0, "", 0, _module("d.ts"), True),
                        (0, '"./aug"', 0, f'{P}`d.ts`/"./aug"/', True),
                    ],
                ),
                # Covered, but not JavaScript or TypeScript.
                ("p.py", []),
                # Indexed, not analyzed: not in the workspace.
                ("gone.ts", [(0, "", 0, _module("gone.ts"), True)]),
                *defined,
            ]
        )

    def analyzed(self):
        return analyze_repository(
            self.root,
            repository_id="r",
            languages=("typescript", "javascript", "python"),
        )

    def outcomes(self, result, binding):
        imports = {i.id: i for i in result.imports}
        return {
            (imports[r.import_id].file, imports[r.import_id].specifier): (
                r.status,
                r.reason,
                tuple(link.target_file for link in r.links),
                tuple(link.rule_id for link in r.links),
                r.candidates,
            )
            for r in binding.resolutions
        }

    def test_imports_take_the_module_the_index_names(self):
        result = self.analyzed()
        ingested = self.typescript()
        parsed = import_rules.resolve_imports(result)
        binding = bind_imports(result, [ingested], self.root, parsed)
        self.assertEqual(len(binding.resolutions), len(parsed))
        self.assertEqual(
            [r.import_id for r in binding.resolutions],
            [r.import_id for r in parsed],
        )
        found = self.outcomes(result, binding)
        linked = "scip.import-module"
        self.assertEqual(
            {k: v for k, v in found.items() if k[0] == "a.ts"},
            {
                ("a.ts", "./m"): ("linked", None, ("m.ts",), (linked,), ()),
                ("a.ts", "react"): ("unresolved", "scip_external", (), (), ()),
                ("a.ts", "./side"): ("unresolved", "scip_no_occurrence", (), (), ()),
                ("a.ts", "./aug"): ("linked", None, ("aug.ts",), (linked,), ()),
                ("a.ts", "./two"): (
                    "ambiguous",
                    "multiple_module_references",
                    (),
                    (),
                    (_module("two.js"), _module("two.ts")),
                ),
                ("a.ts", "./gone"): (
                    "unresolved",
                    "scip_target_not_analyzed",
                    (),
                    (),
                    (),
                ),
                ("a.ts", "./m2"): ("unresolved", "scip_no_occurrence", (), (), ()),
                ("a.ts", "./m3"): ("unresolved", "scip_no_occurrence", (), (), ()),
                ("a.ts", "m"): ("unresolved", "scip_no_occurrence", (), (), ()),
                ("a.ts", "ext"): (
                    "ambiguous",
                    "multiple_module_references",
                    (),
                    (),
                    (
                        "scip-typescript npm ext 1 `a.d.ts`/",
                        "scip-typescript npm ext 1 `b.d.ts`/",
                    ),
                ),
                ("a.ts", "./multi"): (
                    "unresolved",
                    "scip_no_occurrence",
                    (),
                    (),
                    (),
                ),
                ("a.ts", "./v"): ("unresolved", "scip_no_occurrence", (), (), ()),
            },
        )
        # Files no index covers, and other languages, keep the parser's.
        by_parser = self.outcomes(result, bind_imports(result, [], self.root, parsed))
        for key in (("b.ts", "./m"), ("p.py", "m")):
            self.assertEqual(found[key], by_parser[key])
            self.assertNotIn(linked, found[key][3])
        self.assertEqual(found[("b.ts", "./m")][2], ("m.ts",))
        self.assertEqual(
            set(binding.producers),
            {i.id for i in result.imports if i.file == "a.ts"},
        )
        self.assertEqual(
            set(binding.producers.values()), {("scip-typescript@1.0", "sha256:ts")}
        )

    def test_the_first_index_covering_a_file_decides(self):
        (self.root / "a.ts").write_text('import { x } from "./m";\n')
        result = self.analyzed()
        first = self.index(
            [
                ("a.ts", [(0, '"./m"', 0, _module("m.ts"), False)]),
                ("m.ts", [(0, "", 0, _module("m.ts"), True)]),
            ],
            "first",
        )
        second = self.index(
            [
                ("a.ts", [(0, '"./m"', 0, _module("m2.ts"), False)]),
                ("m2.ts", [(0, "", 0, _module("m2.ts"), True)]),
            ],
            "second",
        )
        binding = bind_imports(
            result, [first, second], self.root, import_rules.resolve_imports(result)
        )
        self.assertEqual(self.outcomes(result, binding)[("a.ts", "./m")][2], ("m.ts",))
        self.assertEqual(
            set(binding.producers.values()), {("scip-typescript@1.0", "sha256:first")}
        )

    def test_a_module_defined_in_two_files_is_ambiguous(self):
        (self.root / "a.ts").write_text('import { x } from "./m";\n')
        (self.root / "x").mkdir()
        (self.root / "x" / "m.ts").write_text("export const x = 1;\n")
        result = self.analyzed()
        first = self.index(
            [
                ("a.ts", [(0, '"./m"', 0, _module("m.ts"), False)]),
                ("m.ts", [(0, "", 0, _module("m.ts"), True)]),
            ],
            "first",
        )
        second = self.index([("x/m.ts", [(0, "", 0, _module("m.ts"), True)])], "second")
        binding = bind_imports(
            result, [first, second], self.root, import_rules.resolve_imports(result)
        )
        self.assertEqual(
            self.outcomes(result, binding)[("a.ts", "./m")],
            ("ambiguous", "multiple_module_references", (), (), ("m.ts", "x/m.ts")),
        )

    def test_a_module_reference_must_lie_inside_the_statement(self):
        (self.root / "a.ts").write_text('import { x } from "./m";\n"./m";\n')
        result = self.analyzed()
        ingested = self.index(
            [
                ("a.ts", [(1, '"./m"', 0, _module("m.ts"), False)]),
                ("m.ts", [(0, "", 0, _module("m.ts"), True)]),
            ]
        )
        binding = bind_imports(
            result, [ingested], self.root, import_rules.resolve_imports(result)
        )
        found = self.outcomes(result, binding)
        self.assertEqual(
            found[("a.ts", "./m")][:2], ("unresolved", "scip_no_occurrence")
        )

    def test_graph_edges_name_the_rule_producer_and_receipt(self):
        ingested = self.typescript()
        output = asyncio.run(
            TreeSitterTool(self.root).analyze(
                "r",
                languages=["typescript", "javascript"],
                scip_indexes=[ingested],
            )
        )
        edges = sorted(
            (
                r.properties["specifier"],
                r.properties["rule_id"],
                r.properties["evidence_type"],
                r.properties.get("producer"),
                r.properties.get("receipt"),
            )
            for r in output.relationships
            if r.type == "IMPORTS" and r.properties.get("specifier") in {"./m", "./aug"}
        )
        self.assertEqual(
            edges,
            [
                (
                    "./aug",
                    "scip.import-module",
                    "resolved",
                    "scip-typescript@1.0",
                    "sha256:ts",
                ),
                (
                    "./m",
                    "scip.import-module",
                    "resolved",
                    "scip-typescript@1.0",
                    "sha256:ts",
                ),
                (
                    "./m",
                    "tree-sitter.typescript-relative-module",
                    "resolved",
                    None,
                    None,
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
