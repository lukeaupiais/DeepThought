"""Go declarations, import facts and name-matched calls (opt-in language)."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought import analyze_repository
from deepthought.analysis_tools import TreeSitterTool
from deepthought.calls import CALL_LEXICAL_NAME, resolve_calls
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

    def test_calls_link_only_same_file_functions(self):
        result = analyze(
            {"server.go": SOURCE, "other.go": "package server\nfunc Use() { New() }\n"}
        )
        sites = {s.id: s for s in result.call_sites}
        definitions = {d.id: d for d in result.definitions}
        found = {
            (sites[r.call_id].file, sites[r.call_id].callee): (
                r.status,
                definitions[r.target_id].symbol if r.target_id else None,
                r.rule_id,
            )
            for r in resolve_calls(result)
        }
        self.assertEqual(
            found[("server.go", "helper")], ("linked", "helper", CALL_LEXICAL_NAME)
        )
        # Selector calls are recorded as written and not guessed.
        self.assertEqual(found[("server.go", "fmt.Println")][0], "unresolved")
        self.assertEqual(found[("server.go", "str.ToUpper")][0], "unresolved")
        # Same-package calls across files need package facts; not linked yet.
        self.assertEqual(found[("other.go", "New")][0], "unresolved")

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


if __name__ == "__main__":
    unittest.main()
