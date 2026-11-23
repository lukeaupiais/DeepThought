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
from deepthought.imports import resolve_imports

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
