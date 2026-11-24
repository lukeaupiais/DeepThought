"""Go interfaces implemented by method sets (go_interfaces.py)."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought import analyze_repository
from deepthought.analysis_tools import TreeSitterTool
from deepthought.go_interfaces import (
    GO_INTERFACE_METHOD_SET,
    _import_name,
    resolve_go_implementations,
)

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_go"
)

FILES = {
    "go.mod": "module example.com/app\n\ngo 1.22\n",
    "store/store.go": """package store

import (
\t"io"
\tstdio "io"
)

type Store interface {
\tGet(key string) ([]byte, error)
\tPut(key string, v []uint8) error
}

type Mem struct{}

func (m *Mem) Get(key string) ([]byte, error) { return nil, nil }
func (m *Mem) Put(k string, v []byte) error   { return nil }

// Get takes an int: not the interface's method.
type Wrong struct{}

func (Wrong) Get(key int) ([]byte, error)   { return nil, nil }
func (Wrong) Put(k string, v []byte) error { return nil }

type ReadStore interface {
\tStore
\tio.Reader
}

type Both struct{ Mem }

func (b *Both) Get(key string) ([]byte, error) { return nil, nil }
func (b *Both) Put(k string, v []byte) error   { return nil }
func (b *Both) Read(p []byte) (n int, err error) { return 0, nil }

type Failure interface {
\terror
\tCode() int
}

type codeError struct{}

func (codeError) Error() string { return "" }
func (codeError) Code() int     { return 1 }

type sealed interface{ seal(w stdio.Writer) }

type Sealed struct{}

func (Sealed) seal(w io.Writer) {}

type Number interface{ ~int | ~int64 }

type Box[T any] struct{ v T }

func (b *Box[T]) Get(key string) ([]byte, error) { return nil, nil }
func (b *Box[T]) Put(k string, v []byte) error   { return nil }
func (b *Box[T]) Value() T                       { return b.v }

type Valuer interface{ Value() int }
""",
    "other/other.go": """package other

import st "example.com/app/store"

// Same method names, but seal is unexported in package store.
type Outside struct{}

func (Outside) seal(w interface{}) {}

type Cache struct{}

func (*Cache) Get(key string) ([]byte, error) { return nil, nil }
func (*Cache) Put(k string, v []byte) error   { return nil }

func Use(s st.Store) { s.Get("k") }
""",
    "nested/go.mod": "module example.com/nested\n\ngo 1.22\n",
    "nested/n.go": """package nested

type Remote struct{}

func (Remote) Get(key string) ([]byte, error) { return nil, nil }
func (Remote) Put(k string, v []byte) error   { return nil }
""",
}


def analyze(files):
    with tempfile.TemporaryDirectory() as directory:
        for name, content in files.items():
            path = Path(directory) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return analyze_repository(directory, repository_id="r", languages=("go",))


def implementations(result):
    names = {d.id: f"{d.file}::{d.symbol}" for d in result.definitions}
    return {
        (names[item.type_id], names[item.interface_id]): sorted(
            (names[a], names[b]) for a, b in item.methods
        )
        for item in resolve_go_implementations(result)
    }


@unittest.skipUnless(READY, "Provision the go grammar")
class GoInterfaceTests(unittest.TestCase):
    def test_method_sets_decide_implementations(self):
        found = implementations(analyze(FILES))
        self.assertEqual(
            sorted(found),
            [
                ("other/other.go::Cache", "store/store.go::Store"),
                ("store/store.go::Both", "store/store.go::ReadStore"),
                ("store/store.go::Both", "store/store.go::Store"),
                ("store/store.go::Box", "store/store.go::Store"),
                ("store/store.go::Mem", "store/store.go::Store"),
                ("store/store.go::Sealed", "store/store.go::sealed"),
                ("store/store.go::codeError", "store/store.go::Failure"),
            ],
        )
        # Each interface method is paired with the method a call runs; the
        # embedded io.Reader's Read has no declaration here to pair.
        self.assertEqual(
            found[("store/store.go::Both", "store/store.go::ReadStore")],
            [
                ("store/store.go::Store.Get", "store/store.go::Both.Get"),
                ("store/store.go::Store.Put", "store/store.go::Both.Put"),
            ],
        )
        self.assertEqual(
            found[("store/store.go::codeError", "store/store.go::Failure")],
            [("store/store.go::Failure.Code", "store/store.go::codeError.Code")],
        )

    def test_signatures_are_written_without_names(self):
        result = analyze(FILES)
        signatures = {d.symbol: d.signature for d in result.definitions}
        self.assertEqual(signatures["Mem.Put"], "(string,[]byte)(error)")
        self.assertEqual(signatures["Both.Read"], "([]byte)(int,error)")
        self.assertEqual(signatures["Box.Value"], None)  # returns T
        self.assertEqual(signatures["Use"], "(st.Store)()")
        embeds = {d.symbol: d.embeds for d in result.definitions}
        self.assertEqual(embeds["ReadStore"], ("Store", "io.Reader"))
        self.assertEqual(embeds["Number"], ("~int|~int64",))

    def test_unknown_embeds_leave_an_interface_out(self):
        files = {
            "a.go": 'package a\n\nimport x "example.com/ext"\n\n'
            "type I interface {\n\tx.Thing\n\tM()\n}\n\n"
            "type T struct{}\n\nfunc (T) M() {}\n"
        }
        self.assertEqual(implementations(analyze(files)), {})

    def test_types_declared_twice_are_left_out(self):
        files = {
            "a.go": "package a\n\ntype I interface{ M() }\n",
            "t_linux.go": "package a\n\ntype T struct{}\n\nfunc (T) M() {}\n",
            "t_windows.go": "package a\n\ntype T struct{}\n",
        }
        self.assertEqual(implementations(analyze(files)), {})

    def test_import_names(self):
        self.assertEqual(_import_name("gopkg.in/yaml.v3"), "yaml")
        self.assertEqual(_import_name("github.com/a/b/v2"), "b")
        self.assertEqual(_import_name("github.com/mattn/go-isatty"), "isatty")

    def test_edges_carry_the_method_set_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, content in FILES.items():
                path = Path(directory) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            output = asyncio.run(
                TreeSitterTool(directory).analyze("r", languages=("go",))
            )
        names = {n.properties["qualified_name"]: n.properties for n in output.nodes}
        edges = sorted(
            (
                r.type,
                names[r.source_match["qualified_name"]]["name"],
                names[r.target_match["qualified_name"]]["name"],
            )
            for r in output.relationships
            if r.type in {"IMPLEMENTS", "DISPATCHES_TO"}
            and "Store" in names[r.target_match["qualified_name"]]["name"]
            or r.type == "DISPATCHES_TO"
            and names[r.source_match["qualified_name"]]["name"] == "Store.Get"
        )
        self.assertIn(("IMPLEMENTS", "Mem", "Store"), edges)
        self.assertIn(("DISPATCHES_TO", "Store.Get", "Cache.Get"), edges)
        self.assertNotIn(("DISPATCHES_TO", "Store.Get", "Wrong.Get"), edges)
        self.assertNotIn(("DISPATCHES_TO", "Store.Get", "Remote.Get"), edges)
        rules = {
            r.properties["rule_id"]
            for r in output.relationships
            if r.type in {"IMPLEMENTS", "DISPATCHES_TO"}
        }
        self.assertEqual(rules, {GO_INTERFACE_METHOD_SET})
        self.assertEqual(output.metadata.custom_metrics["go_implementations"], 7)


if __name__ == "__main__":
    unittest.main()
