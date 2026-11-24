"""Go declarations, import facts and name-matched calls (opt-in language)."""

import asyncio
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from deepthought import AnalysisResult, analyze_repository
from deepthought.analysis_tools import TreeSitterTool
from deepthought.calls import (
    CALL_LEXICAL_NAME,
    CALL_MODULE_ATTRIBUTE,
    CALL_PACKAGE_NAME,
    GO_CALL_TYPED_RECEIVER,
    resolve_calls,
)
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

    def test_calls_on_variables_whose_declaration_writes_the_type(self):
        files = {
            "go.mod": "module example.com/app\n",
            "cmd.go": (
                "package app\n"
                "type Command struct{ Base }\n"
                "type Base struct{}\n"
                "type Runner interface{ Run() }\n"
                "func (c *Command) Root() *Command { return c }\n"
                "func (b Base) Up() {}\n"
                'func (c *Command) Name() string { return "" }\n'
                "type Wrap struct{ B Base }\n"
                "func (w Wrap) Up() {}\n"
                "type Num int\n"
                "func (n Num) Get() {}\n"
                "type Set map[string]bool\n"
                "func (s Set) Add() {}\n"
            ),
            "lib/lib.go": "package lib\ntype T struct{}\nfunc (t T) Go() {}\n",
            "use.go": (
                "package app\n"
                'import "example.com/app/lib"\n'
                "func (c *Command) Use(p *Command, q lib.T, r Runner) (out *Command) {\n"
                "\tc.Root()\n"  # 4: receiver
                "\tp.Name()\n"  # 5: parameter
                "\tq.Go()\n"  # 6: qualified type
                "\tr.Run()\n"  # 7: interface method
                "\tout.Root()\n"  # 8: named result
                "\tx := &Command{}\n"
                "\tx.Name()\n"  # 10: &T{}
                "\ty, z := Command{}, new(Command)\n"
                "\ty.Root(); z.Root()\n"  # 12
                "\tvar w *Command\n"
                "\tw.Root()\n"  # 14: var with a type
                "\tc.Up()\n"  # 15: promoted from Base: not followed
                "\tif p := make(); true {\n"
                "\t\tp.Name()\n"  # 17: inner p has no written type
                "\t}\n"
                "\tp.Name()\n"  # 19: the parameter again
                "\tfunc(p Base) { p.Up() }(Base{})\n"  # 20: literal parameter
                "\tv.Root()\n"  # 21: declared later
                "\tv := Command{}\n"
                "\tn := n.Root()\n"  # 23: the new n is not in scope yet
                "}\n"
                "func More(t *Command, xs ...Command) {\n"  # 25
                "\tswitch t := t.Root().(type) {\n"
                "\tcase Base:\n"
                "\t\tt.Name()\n"  # 28: the switch's t has no written type
                "\t}\n"
                "\tt.Name()\n"  # 30: the parameter again
                "\txs.Root()\n"  # 31: a variadic parameter is a slice
                "\tm := build(Command{})\n"
                "\tm.Root()\n"  # 33: a call's result
                "\ta, b := pair()\n"
                "\tb.Root()\n"  # 35
                "\twr := Wrap{}\n"
                "\twr.B.Up()\n"  # 37: a field's method, through its type
                "\tconst k Num = 1\n"
                "\tk.Get()\n"  # 39: a typed constant
                "\tset := make(Set)\n"
                "\tset.Add()\n"  # 41: make(T)
                "}\n"
                "var hook = func(c *Command) { c.Root() }\n"  # 43
                "func Other() {\n"
                "\ttype Command struct{}\n"
                "\tl := Command{}\n"
                "\tl.Root()\n"  # 47: a local type
                "}\n"
            ),
        }
        result = analyze(files)
        sites = {s.id: s for s in result.call_sites}
        definitions = {d.id: d for d in result.definitions}
        found = {
            (sites[r.call_id].line, sites[r.call_id].callee): (
                r.status,
                (
                    (definitions[r.target_id].file, definitions[r.target_id].symbol)
                    if r.target_id
                    else r.reason
                ),
                r.rule_id,
            )
            for r in resolve_calls(result)
            if sites[r.call_id].file == "use.go"
        }
        for line, callee, target in (
            (4, "c.Root", ("cmd.go", "Command.Root")),
            (5, "p.Name", ("cmd.go", "Command.Name")),
            (6, "q.Go", ("lib/lib.go", "T.Go")),
            (7, "r.Run", ("cmd.go", "Runner.Run")),
            (8, "out.Root", ("cmd.go", "Command.Root")),
            (10, "x.Name", ("cmd.go", "Command.Name")),
            (12, "y.Root", ("cmd.go", "Command.Root")),
            (12, "z.Root", ("cmd.go", "Command.Root")),
            (14, "w.Root", ("cmd.go", "Command.Root")),
            (19, "p.Name", ("cmd.go", "Command.Name")),
            (20, "p.Up", ("cmd.go", "Base.Up")),
            (30, "t.Name", ("cmd.go", "Command.Name")),
            (39, "k.Get", ("cmd.go", "Num.Get")),
            (41, "set.Add", ("cmd.go", "Set.Add")),
            (37, "wr.B.Up", ("cmd.go", "Base.Up")),
            (43, "c.Root", ("cmd.go", "Command.Root")),
        ):
            with self.subTest(line=line, callee=callee):
                self.assertEqual(
                    found[(line, callee)], ("linked", target, GO_CALL_TYPED_RECEIVER)
                )
        self.assertEqual(
            found[(15, "c.Up")], ("unresolved", "not_declared_on_type", None)
        )
        for line, callee in (
            (17, "p.Name"),
            (21, "v.Root"),
            (23, "n.Root"),
            (47, "l.Root"),
            (28, "t.Name"),
            (31, "xs.Root"),
            (33, "m.Root"),
            (35, "b.Root"),
        ):
            with self.subTest(line=line, callee=callee):
                self.assertEqual(
                    found[(line, callee)], ("unresolved", "attribute_call", None)
                )

    def test_receiver_types_follow_fields_results_elements_and_variables(self):
        files = {
            "go.mod": "module example.com/app\n",
            "lib/lib.go": ("package lib\ntype T struct{}\nfunc (t *T) Go() {}\n"),
            "cmd.go": (
                "package app\n"
                'import "example.com/app/lib"\n'
                "type Command struct {\n"
                "\tparent, next *Command\n"
                "\tcommands []*Command\n"
                "\tgrid [2][]Command\n"
                "\tbyName map[string]*Command\n"
                "\t*Base\n"
                "\tlib.T\n"
                "\ttool *lib.T\n"
                "\tnamed Commands\n"
                "\tfn func() *Command\n"
                "}\n"
                "type Base struct{}\n"
                "func (b *Base) Up() {}\n"
                "type Commands []*Command\n"
                "func (c *Command) Root() *Command { return c }\n"
                "func (c *Command) Find() (*Command, []string, error) { return c, nil, nil }\n"
                "func (c *Command) Commands() []*Command { return nil }\n"
                "func (c *Command) Tool() *lib.T { return nil }\n"
                'func (c *Command) Name() string { return "" }\n'
                "type Getter interface{ Get() *Command }\n"
                "type Box[T any] struct{ v T; all []T }\n"
                "func (b *Box[T]) Get() T { return b.v }\n"
                "type T struct{}\n"
                "func (T) Root() {}\n"
                "type Twice struct{ c *Command }\n"
            ),
            "twice.go": (
                "package app\n"
                "type Twice struct{ c *Command }\n"
                "func (c *Command) Dup() *Command { return c }\n"
            ),
            "dup.go": "package app\nfunc (c *Command) Dup() *Command { return c }\n",
            "use.go": (
                "package app\n"
                "func Use(c *Command, g Getter, b *Box[Command], xs ...*Command) {\n"
                "\tc.parent.Root()\n"  # 3: a field
                "\tc.next.parent.Root()\n"  # 4: two fields
                "\tc.Base.Up()\n"  # 5: an embedded field by its type name
                "\tc.T.Go()\n"  # 6: an embedded qualified type
                "\tc.tool.Go()\n"  # 7: a qualified field type
                "\tr := c.Root()\n"
                "\tr.Name()\n"  # 9: a method's result
                "\tf, rest, err := c.Find()\n"
                "\tf.Name()\n"  # 11: the first result
                "\tfor _, s := range c.Commands() {\n"
                "\t\ts.Name()\n"  # 13: an element of a method's result
                "\t}\n"
                "\tfor _, s := range c.commands {\n"
                "\t\ts.Name()\n"  # 16: an element of a field
                "\t}\n"
                "\tfor _, x := range xs {\n"
                "\t\tx.Name()\n"  # 19: an element of a variadic parameter
                "\t}\n"
                "\tfirst := xs[0]\n"
                "\tfirst.Name()\n"  # 22: an index
                "\tcell := c.grid[0][1]\n"
                "\tcell.Name()\n"  # 24: two indexes
                "\tp := c\n"
                "\tp.Name()\n"  # 26: a variable
                "\tq := (*c).parent\n"
                "\tq.Name()\n"  # 28: through a dereference
                "\tgot := g.Get()\n"
                "\tgot.Name()\n"  # 30: an interface method's result
                "\tany := interface{}(c)\n"
                "\tas := any.(*Command)\n"
                "\tas.Name()\n"  # 33: a type assertion
                "\ttool := c.Tool()\n"
                "\ttool.Go()\n"  # 35: a result type written in another package
                "}\n"
                "func Not(c *Command, b *Box[Command], tw Twice) {\n"
                "\tfor k, v := range c.byName {\n"
                "\t\tk.Name(); v.Name()\n"  # 39: a map is not followed
                "\t}\n"
                "\tfor i := range c.commands {\n"
                "\t\ti.Name()\n"  # 42: the index
                "\t}\n"
                "\tfor _, n := range c.named {\n"
                "\t\tn.Name()\n"  # 45: a named slice type is not followed
                "\t}\n"
                "\t_, args, _ := c.Find()\n"
                "\targs.Name()\n"  # 48: not the first result
                "\tc.missing.Root()\n"  # 49: no such field
                "\tc.Name().Root()\n"  # 50: not a dotted name
                "\tc.fn.Root()\n"  # 51: a field of a function type
                "\tv := b.Get()\n"
                "\tv.Root()\n"  # 53: a type parameter result
                "\tb.v.Root()\n"  # 54: a type parameter field
                "\ttw.c.Name()\n"  # 55: the struct is declared twice
                "\tc.Up()\n"  # 56: promoted through an embedded field
                "\tc.commands.Root()\n"  # 57: a slice has no methods
                "\td := c.Dup()\n"
                "\td.Name()\n"  # 59: the method is declared twice
                "}\n"
                "func Generic[T any](t T, xs ...*Command) {\n"
                "\tt.Root()\n"  # 62: a type parameter, not the type T
                "\txs.parent.Root()\n"  # 63: a slice has no fields
                "}\n"
            ),
        }
        result = analyze(files)
        cmd = next(d for d in result.definitions if d.symbol == "Command")
        self.assertEqual(
            cmd.fields,
            (
                ("parent", "Command"),
                ("next", "Command"),
                ("commands", "[]Command"),
                ("grid", "[][]Command"),
                ("Base", "Base"),
                ("T", "lib.T"),
                ("tool", "lib.T"),
                ("named", "Commands"),
            ),
        )
        results = {d.symbol: d.result_type for d in result.definitions}
        self.assertEqual(results["Command.Find"], "Command")
        self.assertEqual(results["Command.Commands"], "[]Command")
        self.assertEqual(results["Getter.Get"], "Command")
        self.assertIsNone(results["Box.Get"])
        self.assertEqual(results["Command.Name"], "string")
        box = next(d for d in result.definitions if d.symbol == "Box")
        self.assertIsNone(box.fields)
        sites = {s.id: s for s in result.call_sites}
        definitions = {d.id: d for d in result.definitions}
        found = {
            (sites[r.call_id].line, sites[r.call_id].callee): (
                r.status,
                (
                    (definitions[r.target_id].file, definitions[r.target_id].symbol)
                    if r.target_id
                    else r.reason
                ),
                r.rule_id,
            )
            for r in resolve_calls(result)
            if sites[r.call_id].file == "use.go"
        }
        for line, callee, target in (
            (3, "c.parent.Root", ("cmd.go", "Command.Root")),
            (4, "c.next.parent.Root", ("cmd.go", "Command.Root")),
            (5, "c.Base.Up", ("cmd.go", "Base.Up")),
            (6, "c.T.Go", ("lib/lib.go", "T.Go")),
            (7, "c.tool.Go", ("lib/lib.go", "T.Go")),
            (9, "r.Name", ("cmd.go", "Command.Name")),
            (11, "f.Name", ("cmd.go", "Command.Name")),
            (13, "s.Name", ("cmd.go", "Command.Name")),
            (16, "s.Name", ("cmd.go", "Command.Name")),
            (19, "x.Name", ("cmd.go", "Command.Name")),
            (22, "first.Name", ("cmd.go", "Command.Name")),
            (24, "cell.Name", ("cmd.go", "Command.Name")),
            (26, "p.Name", ("cmd.go", "Command.Name")),
            (28, "q.Name", ("cmd.go", "Command.Name")),
            (30, "got.Name", ("cmd.go", "Command.Name")),
            (33, "as.Name", ("cmd.go", "Command.Name")),
            (35, "tool.Go", ("lib/lib.go", "T.Go")),
        ):
            with self.subTest(line=line, callee=callee):
                self.assertEqual(
                    found[(line, callee)], ("linked", target, GO_CALL_TYPED_RECEIVER)
                )
        unknown = ("unresolved", "receiver_type_unknown", None)
        for line, callee, outcome in (
            (39, "k.Name", ("unresolved", "attribute_call", None)),
            (39, "v.Name", unknown),
            (42, "i.Name", ("unresolved", "attribute_call", None)),
            (45, "n.Name", unknown),
            (48, "args.Name", ("unresolved", "attribute_call", None)),
            (49, "c.missing.Root", unknown),
            (51, "c.fn.Root", unknown),
            (53, "v.Root", unknown),
            (54, "b.v.Root", unknown),
            (55, "tw.c.Name", unknown),
            (56, "c.Up", ("unresolved", "not_declared_on_type", None)),
            (57, "c.commands.Root", unknown),
            (59, "d.Name", unknown),
            (62, "t.Root", ("unresolved", "attribute_call", None)),
            (63, "xs.parent.Root", ("unresolved", "attribute_call", None)),
        ):
            with self.subTest(line=line, callee=callee):
                self.assertEqual(found[(line, callee)], outcome)
        self.assertNotIn((50, "c.Name().Root"), found)
        steps = {
            (sites[s].line, sites[s].callee): sites[s].receiver_steps for s in sites
        }
        self.assertEqual(steps[(13, "s.Name")], ("Commands()", "[]"))
        self.assertEqual(steps[(22, "first.Name")], None)
        roundtrip = AnalysisResult.from_json(result.to_json())
        self.assertEqual(roundtrip, result)
        site = sites[next(iter(sites))]
        for steps in ((), ("",), ["f"]):
            with self.subTest(steps=steps), self.assertRaises(ValueError):
                replace(site, receiver_type="T", receiver_steps=steps)
        with self.assertRaises(ValueError):
            replace(site, receiver_type=None, receiver_steps=("f",))
        for fields in ((), (("a",),), (("a", ""),), [("a", "T")]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                replace(cmd, fields=fields)
        with self.assertRaises(ValueError):
            replace(cmd, result_type="")

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

    def test_package_qualified_calls_bind_the_declared_package_name(self):
        result = analyze(
            {
                "go.mod": "module example.com/app\n",
                "main.go": (
                    "package main\n"
                    "import (\n"
                    '\t"example.com/app/lib"\n'
                    '\tl2 "example.com/app/lib"\n'
                    '\t"example.com/app/v2"\n'
                    '\t"fmt"\n'
                    ")\n"
                    "func main() {\n"
                    "\tlib.Make(); l2.Make(); lib.Missing()\n"
                    "\tshiny.New(); v2.New(); fmt.Println()\n"
                    "}\n"
                ),
                "lib/a.go": "package lib\nfunc Make() {}\n",
                # The binding is the declared package name, not the directory.
                "v2/v.go": "package shiny\nfunc New() {}\n",
            }
        )
        sites = {s.id: s for s in result.call_sites}
        definitions = {d.id: d for d in result.definitions}
        found = {
            sites[r.call_id].callee: (
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
            found,
            {
                "lib.Make": ("linked", ("lib/a.go", "Make"), CALL_MODULE_ATTRIBUTE),
                "l2.Make": ("linked", ("lib/a.go", "Make"), CALL_MODULE_ATTRIBUTE),
                "lib.Missing": (
                    "unresolved",
                    "module_attribute_not_declared",
                    None,
                ),
                "shiny.New": ("linked", ("v2/v.go", "New"), CALL_MODULE_ATTRIBUTE),
                "v2.New": ("unresolved", "attribute_call", None),
                # Unresolved imports bind nothing.
                "fmt.Println": ("unresolved", "attribute_call", None),
            },
        )

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
