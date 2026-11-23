"""Go declarations, import facts and name-matched calls (opt-in language)."""

import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from deepthought import AnalysisResult, analyze_repository
from deepthought.analysis_tools import TreeSitterTool
from deepthought.calls import CALL_LEXICAL_NAME, CALL_PACKAGE_NAME, resolve_calls
from deepthought.imports import GO_MODULE_PACKAGE, resolve_imports
from deepthought.syntax import extract_go_module

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_go"
)
SOURCE = """package server

import (
\t"fmt"
\tstr "strings"
\t. "os"
\t_ "embed"
)

type Server struct{ name string }
type Reader interface {
\tRead(p []byte) (int, error)
}
type ID = string
type Num int
type Box[T any] struct{ v T }

func (s *Server) Start() error { return helper() }
func (b Box[T]) Get() T { return b.v }
func New() *Server { fmt.Println(str.ToUpper("x")); return &Server{} }
func helper() error {
\ttype local struct{}
\treturn nil
}
"""


def analyze(files, languages=("go",)):
    with tempfile.TemporaryDirectory() as directory:
        for name, content in files.items():
            path = Path(directory) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        options = {} if languages is None else {"languages": languages}
        return analyze_repository(directory, repository_id="fixture", **options)


@unittest.skipUnless(READY, "Provision the go extra")
class GoTests(unittest.TestCase):
    def test_declarations_methods_by_receiver_and_type_kinds(self):
        result = analyze({"server.go": SOURCE})
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(
            [(d.symbol, d.kind, d.line) for d in result.definitions],
            [
                ("Server", "Struct", 10),
                ("Reader", "Interface", 11),
                ("Reader.Read", "Method", 12),
                ("ID", "TypeAlias", 14),
                ("Num", "Type", 15),
                ("Box", "Struct", 16),
                ("Server.Start", "Method", 18),
                ("Box.Get", "Method", 19),
                ("New", "Function", 20),
                ("helper", "Function", 21),
                ("helper.local", "Struct", 22),
            ],
        )
        self.assertEqual(result.coverage[0].language, "go")

    def test_imports_are_facts_and_stay_unresolved(self):
        result = analyze({"server.go": SOURCE})
        self.assertEqual(
            [(i.specifier, i.names, i.local_names, i.line) for i in result.imports],
            [
                ("fmt", (), (), 4),
                ("strings", (), ("str",), 5),
                ("os", ("*",), ("*",), 6),
                ("embed", (), (), 7),
            ],
        )
        self.assertEqual(
            {(r.status, r.reason) for r in resolve_imports(result)},
            {("unresolved", "go_module_path")},
        )

    def test_calls_link_within_the_declared_package_only(self):
        result = analyze(
            {
                "server.go": SOURCE,
                "other.go": "package server\nfunc Use() { New(); plat() }\n",
                "plat_linux.go": "package server\nfunc plat() {}\n",
                "plat_darwin.go": "package server\nfunc plat() {}\n",
                # An external test package sees only its own package's names.
                "ext_test.go": "package server_test\nfunc T() { New(); fixture() }\n",
                "fix_test.go": "package server_test\nfunc fixture() {}\n",
                # Same package name in another directory is another package.
                "sub/sub.go": "package server\nfunc S() { New() }\n",
                "gen.go": "//go:build ignore\n\npackage main\nfunc G() { New() }\n",
            }
        )
        self.assertEqual(
            sorted((p.file, p.name, p.line) for p in result.packages),
            [
                ("ext_test.go", "server_test", 1),
                ("fix_test.go", "server_test", 1),
                ("gen.go", "main", 3),
                ("other.go", "server", 1),
                ("plat_darwin.go", "server", 1),
                ("plat_linux.go", "server", 1),
                ("server.go", "server", 1),
                ("sub/sub.go", "server", 1),
            ],
        )
        sites = {s.id: s for s in result.call_sites}
        definitions = {d.id: d for d in result.definitions}
        found = {
            (sites[r.call_id].file, sites[r.call_id].callee): (
                r.status,
                (
                    (definitions[r.target_id].file, definitions[r.target_id].symbol)
                    if r.target_id
                    else r.reason
                ),
                r.rule_id,
            )
            for r in resolve_calls(result)
        }
        self.assertEqual(
            found[("server.go", "helper")],
            ("linked", ("server.go", "helper"), CALL_LEXICAL_NAME),
        )
        self.assertEqual(
            found[("other.go", "New")],
            ("linked", ("server.go", "New"), CALL_PACKAGE_NAME),
        )
        self.assertEqual(
            found[("ext_test.go", "fixture")],
            ("linked", ("fix_test.go", "fixture"), CALL_PACKAGE_NAME),
        )
        # Build constraints are not evaluated: both platform files declare it.
        self.assertEqual(
            found[("other.go", "plat")], ("ambiguous", "multiple_declarations", None)
        )
        for file in ("ext_test.go", "sub/sub.go", "gen.go"):
            with self.subTest(file=file):
                self.assertEqual(
                    found[(file, "New")], ("unresolved", "no_declaration", None)
                )
        # Selector calls are recorded as written and not guessed.
        self.assertEqual(found[("server.go", "fmt.Println")][0], "unresolved")
        self.assertEqual(found[("server.go", "str.ToUpper")][0], "unresolved")

    def test_reindex_reuses_package_clauses_and_rejects_earlier_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "a.go").write_text("package a\nfunc A() { B() }\n")
            (Path(directory) / "b.go").write_text("package a\nfunc B() {}\n")
            (Path(directory) / "go.mod").write_text("module example.com/a\n")
            full = analyze_repository(directory, repository_id="r", languages=("go",))
            reuse = {}
            again = analyze_repository(
                directory,
                repository_id="r",
                languages=("go",),
                previous=full,
                reuse=reuse,
            )
            self.assertEqual((reuse["reused"], reuse["parsed"]), (2, 0))
            self.assertEqual(again.to_json(), full.to_json())
            data = full.to_dict()
            del data["packages"]
            data["capabilities"].remove("packages")
            earlier = AnalysisResult.from_json(json.dumps(data))
            reuse = {}
            analyze_repository(
                directory,
                repository_id="r",
                languages=("go",),
                previous=earlier,
                reuse=reuse,
            )
            self.assertEqual(reuse["previous_rejected"], "missing_fact_kinds")

    def test_invalid_package_facts_are_rejected(self):
        full = analyze({"a.go": "package a\n"})
        data = full.to_dict()
        self.assertEqual(AnalysisResult.from_json(json.dumps(data)), full)
        for change, message in (
            (lambda d: d["packages"].append(dict(d["packages"][0])), "Duplicate"),
            (
                lambda d: d["packages"].append({**d["packages"][0], "id": "x"}),
                "at most one package",
            ),
            (lambda d: d["capabilities"].remove("packages"), "capability"),
            (lambda d: d["packages"][0].update(file="b.go"), "coverage"),
            (lambda d: d["packages"][0].update(name=""), "nonempty"),
        ):
            changed = json.loads(json.dumps(data))
            change(changed)
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    AnalysisResult.from_json(json.dumps(changed))

    def test_go_mod_module_directive_forms(self):
        cases = (
            (b"module github.com/a/b\n\ngo 1.21\n", ("github.com/a/b", 1, 7)),
            (b'// c\nmodule "example.com/x" // y\n', ("example.com/x", 2, 7)),
            (b"module (\n  example.com/blk\n)\n", ("example.com/blk", 2, 2)),
            (b"module ( example.com/one )\n", ("example.com/one", 1, 9)),
            (b"module `raw/x`\r\n", ("raw/x", 1, 7)),
            (b"go 1.21\n", None),
            (b"module\n", None),
            (b"module (\n)\n", None),
            (b"module a b\n", None),
        )
        for content, expected in cases:
            with self.subTest(content=content):
                found = [
                    (m.name, m.line, m.column, m.kind)
                    for m in extract_go_module(content, "r", "go.mod")
                ]
                self.assertEqual(found, [(*expected, "module")] if expected else [])
        result = analyze({"go.mod": "go 1.21\n", "a.go": "package a\n"})
        self.assertEqual(
            [(c.file, c.status) for c in result.coverage],
            [("a.go", "analyzed"), ("go.mod", "partial")],
        )
        self.assertEqual(
            [(d.code, d.file) for d in result.diagnostics], [("syntax_error", "go.mod")]
        )
        # go.mod is read only when Go is selected.
        other = analyze({"go.mod": "module m\n", "a.py": ""}, languages=("python",))
        self.assertEqual([c.file for c in other.coverage], ["a.py"])

    def test_imports_resolve_to_package_files_within_their_own_module(self):
        files = {
            "go.mod": "module example.com/app\n",
            "main.go": (
                "package main\n"
                "import (\n"
                '\t"example.com/app/lib"\n'
                '\t"example.com/app/lib/missing"\n'
                '\t"fmt"\n'
                '\t"example.com/other/x"\n'
                '\t"example.com/app/nested/x"\n'
                '\t"example.com/app/amb"\n'
                '\t"example.com/applib"\n'
                '\t"example.com/app/cmd/tool"\n'
                ")\n"
            ),
            "lib/a.go": "package lib\n",
            "lib/b.go": "package lib\n",
            "lib/a_test.go": 'package lib_test\nimport "example.com/app/lib"\n',
            "lib/gen.go": "//go:build ignore\n\npackage main\n",
            "cmd/tool/main.go": "package main\n",
            "amb/a.go": "package a\n",
            "amb/b.go": "package b\n",
            "nested/go.mod": "module example.com/nested\n",
            "nested/x/use.go": (
                'package x\nimport (\n\t"example.com/nested/y"\n'
                '\t"example.com/app/lib"\n)\n'
            ),
            "nested/y/y.go": "package y\n",
        }
        links = {
            (key[0], key[1]): value
            for key, value in self.resolved(analyze(files)).items()
        }
        lib = (
            "linked",
            (("lib/a.go", GO_MODULE_PACKAGE), ("lib/b.go", GO_MODULE_PACKAGE)),
            None,
        )
        self.assertEqual(
            links,
            {
                ("main.go", "example.com/app/lib"): lib,
                # An external test package imports the package beside it.
                ("lib/a_test.go", "example.com/app/lib"): lib,
                ("main.go", "example.com/app/lib/missing"): (
                    "unresolved",
                    (),
                    "no_analyzed_file",
                ),
                ("main.go", "fmt"): ("unresolved", (), "outside_module"),
                # A module path is a prefix by whole path elements only.
                ("main.go", "example.com/applib"): ("unresolved", (), "outside_module"),
                ("main.go", "example.com/app/cmd/tool"): (
                    "unresolved",
                    (),
                    "main_package",
                ),
                ("main.go", "example.com/other/x"): (
                    "unresolved",
                    (),
                    "outside_module",
                ),
                # nested/ is its own module, so the outer module cannot reach it.
                ("main.go", "example.com/app/nested/x"): (
                    "unresolved",
                    (),
                    "other_repository_module",
                ),
                ("main.go", "example.com/app/amb"): (
                    "ambiguous",
                    (),
                    "multiple_packages",
                ),
                ("nested/x/use.go", "example.com/nested/y"): (
                    "linked",
                    (("nested/y/y.go", GO_MODULE_PACKAGE),),
                    None,
                ),
                ("nested/x/use.go", "example.com/app/lib"): (
                    "unresolved",
                    (),
                    "other_repository_module",
                ),
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            for name, content in files.items():
                path = Path(directory) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            output = asyncio.run(
                TreeSitterTool(directory).analyze("r", languages=("go",))
            )
        modules = {
            n.properties["path"]: n.properties.get("module")
            for n in output.nodes
            if "File" in n.labels and n.properties["path"].endswith("go.mod")
        }
        self.assertEqual(
            modules,
            {"go.mod": "example.com/app", "nested/go.mod": "example.com/nested"},
        )
        imports = sorted(
            (
                r.source_match["qualified_name"].split("::")[1],
                r.target_match["qualified_name"].split("::")[1],
                r.properties["rule_id"],
                r.properties["evidence_type"],
            )
            for r in output.relationships
            if r.type == "IMPORTS"
        )
        self.assertEqual(
            imports,
            [
                ("lib/a_test.go", "lib/a.go", GO_MODULE_PACKAGE, "resolved"),
                ("lib/a_test.go", "lib/b.go", GO_MODULE_PACKAGE, "resolved"),
                ("main.go", "lib/a.go", GO_MODULE_PACKAGE, "resolved"),
                ("main.go", "lib/b.go", GO_MODULE_PACKAGE, "resolved"),
                ("nested/x/use.go", "nested/y/y.go", GO_MODULE_PACKAGE, "resolved"),
            ],
        )

    @staticmethod
    def resolved(result):
        imports = {item.id: item for item in result.imports}
        return {
            (imports[r.import_id].file, imports[r.import_id].specifier): (
                r.status,
                tuple((link.target_file, link.rule_id) for link in r.links),
                r.reason,
            )
            for r in resolve_imports(result)
        }

    def test_go_is_opt_in_and_tool_projects_nesting(self):
        default = analyze({"server.go": SOURCE, "a.py": "x = 1\n"}, languages=None)
        self.assertEqual([c.file for c in default.coverage], ["a.py"])
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "server.go").write_text(SOURCE)
            output = asyncio.run(
                TreeSitterTool(directory).analyze("r", languages=("go",))
            )
        names = {n.properties["qualified_name"]: n.properties for n in output.nodes}
        nested = sorted(
            (
                names[r.source_match["qualified_name"]]["name"],
                names[r.target_match["qualified_name"]]["name"],
            )
            for r in output.relationships
            if r.type == "CONTAINS" and "Symbol" in r.source_labels
        )
        self.assertEqual(
            nested, [("Reader", "Reader.Read"), ("helper", "helper.local")]
        )
        files = [n.properties for n in output.nodes if "File" in n.labels]
        self.assertEqual([f.get("package") for f in files], ["server"])
        self.assertEqual(default.packages, ())


if __name__ == "__main__":
    unittest.main()
