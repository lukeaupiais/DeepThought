import asyncio
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from deepthought.analysis_tools.tree_sitter import TreeSitterTool
from deepthought.engine import analyze_repository
from deepthought.go_callgraph import (
    GO_CALL_VTA,
    GO_CALLGRAPH_ENVIRONMENT_VARIABLE,
    DynamicCall,
    GoCallgraphUnavailable,
    bind_dynamic_calls,
    dynamic_calls,
    locate_go_callgraph,
)


def _pinned():
    try:
        return locate_go_callgraph()
    except (GoCallgraphUnavailable, OSError):
        return None


GO_READY = shutil.which("go") is not None

# A package in a directory ``./...`` skips, which the go tool still builds.
EXAMPLE = """package main

type T interface{ M() }

type A struct{}

func (A) M() {}

func call(t T) { t.M() }

func main() { call(A{}) }
"""

MAIN = """package main

type Shape interface{ Area() int }

type Square struct{}

func (Square) Area() int { return 1 }

type Circle struct{}

func (Circle) Area() int { return 2 }

type Unused struct{}

func (Unused) Area() int { return 3 }

func total(s Shape) int { return s.Area() }

func run(f func() int) int { return f() }

func one() int { return 1 }

func main() {
	total(Square{})
	total(Circle{})
	run(one)
}

type Dead struct{}

func (Dead) Area() int { return 4 }

// Never called: VTA still follows Dead{} into total, RTA does not.
func dead() int { return total(Dead{}) }
"""

# A library package without tests: nothing to reach it from, so every
# function may be called.
LIB = """package lib

type I interface{ M() }

type A struct{}

func (A) M() {}

func Call(i I) { i.M() }

func Use() { Call(A{}) }
"""


def _write(root, files):
    for name, text in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


class LocateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)

    def test_missing_tool_is_an_error(self):
        with self.assertRaisesRegex(GoCallgraphUnavailable, "needs go"):
            locate_go_callgraph(
                {"PATH": self.directory, "DEEPTHOUGHT_CACHE": self.directory}
            )

    @unittest.skipUnless(GO_READY, "Provision Go")
    def test_a_binary_not_built_from_the_pinned_module_is_refused(self):
        fake = Path(self.directory) / "callgraph"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        with self.assertRaisesRegex(GoCallgraphUnavailable, "is not built from"):
            locate_go_callgraph(
                {
                    GO_CALLGRAPH_ENVIRONMENT_VARIABLE: str(fake),
                    "PATH": os.environ.get("PATH", ""),
                }
            )

    @unittest.skipUnless(
        os.environ.get(GO_CALLGRAPH_ENVIRONMENT_VARIABLE), "No configured tool"
    )
    def test_the_configured_tool_is_the_pinned_one(self):
        self.assertEqual(
            locate_go_callgraph(),
            Path(os.environ[GO_CALLGRAPH_ENVIRONMENT_VARIABLE]),
        )

    def test_go_without_modules_needs_no_call_graph(self):
        _write(self.directory, {"main.go": MAIN})
        with mock.patch.dict(
            os.environ, {GO_CALLGRAPH_ENVIRONMENT_VARIABLE: "/nonexistent/callgraph"}
        ):
            output = asyncio.run(
                TreeSitterTool(self.directory).analyze("r", languages={"go"})
            )
        self.assertEqual(
            output.metadata.custom_metrics["go_dynamic_calls"],
            {"sites": 0, "edges": 0, "failed_packages": {}},
        )


class ReachabilityFailureTests(unittest.TestCase):
    def test_a_package_rta_cannot_load_is_a_failure(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        binary = Path(root) / "callgraph"
        # Loads with VTA, fails with RTA for another reason than no roots.
        binary.write_text(
            "#!/bin/sh\n"
            'case "$1" in -algo=rta) echo "rta: out of memory" >&2; exit 1;; esac\n'
        )
        binary.chmod(0o755)
        calls, failures = dynamic_calls(root, {"": ["."]}, binary)
        self.assertEqual((calls, failures), ([], {".": "rta: out of memory"}))


class BindTests(unittest.TestCase):
    def test_sites_and_callees_are_matched_by_position_and_name(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        _write(root, {"main.go": MAIN})
        result = analyze_repository(root, repository_id="r", languages=("go",))
        lines = MAIN.splitlines()

        def at(line, text):
            return line, lines[line - 1].index(text)

        area_line, area_column = at(17, "s.Area(")
        square = at(7, "Area")
        circle = at(11, "Area")
        f_line, f_column = at(19, "f()")
        calls = [
            # `s.Area(`: the parenthesis after the name.
            DynamicCall("main.go", 17, area_column + 6, "main.go", *square, "Area"),
            DynamicCall("main.go", 17, area_column + 6, "main.go", *circle, "Area"),
            DynamicCall("main.go", 19, f_column + 1, "main.go", *at(21, "one"), "one"),
            # No call site ends there; a callee of another name; a callee
            # position no declaration of that name holds.
            DynamicCall("main.go", 17, area_column + 5, "main.go", *square, "Area"),
            DynamicCall("main.go", 19, f_column + 1, "main.go", *square, "one"),
            DynamicCall("main.go", 19, f_column + 1, "main.go", 1, 0, "one"),
        ]
        bound = bind_dynamic_calls(result, calls)
        sites = {s.id: s for s in result.call_sites}
        names = {d.id: (d.symbol, d.line) for d in result.definitions}
        self.assertEqual(
            {
                sites[s].callee: sorted(names[t] for t in targets)
                for s, targets in bound.items()
            },
            {"s.Area": [("Circle.Area", 11), ("Square.Area", 7)], "f": [("one", 21)]},
        )


@unittest.skipUnless(GO_READY and _pinned(), "Provision the pinned Go call graph")
class CallgraphToolTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        _write(
            self.root,
            {
                "go.mod": "module example.com/shapes\n\ngo 1.21\n",
                "main.go": MAIN,
                "lib/lib.go": LIB,
                "_examples/ex/main.go": EXAMPLE,
                # Never built: no edges from it.
                "testdata/td/main.go": EXAMPLE.replace("A{}", "B{}")
                .replace("type A struct{}", "type B struct{}")
                .replace("func (A) M", "func (B) M"),
                # A nested module, named from its own directory.
                "sub/go.mod": "module example.com/sub\n\ngo 1.21\n",
                "sub/main.go": EXAMPLE.replace("A{}", "C{}")
                .replace("type A struct{}", "type C struct{}")
                .replace("func (A) M", "func (C) M"),
                # A module with no Go files has no packages to load.
                "empty/go.mod": "module example.com/empty\n\ngo 1.21\n",
                "broken/go.mod": "module example.com/broken\n\ngo 1.21\n",
                "broken/main.go": "package main\n\nfunc main() { undefined() }\n",
            },
        )

    def test_vta_gives_the_implementations_that_reach_each_call(self):
        calls, failures = dynamic_calls(
            self.root, {"": [".", "lib"], "broken": ["."]}, _pinned()
        )
        self.assertEqual(sorted(failures), ["broken"])
        self.assertEqual(
            sorted((c.file, c.line, c.callee_name, c.callee_line) for c in calls),
            # Unused.Area never reaches s.Area(), and Dead.Area only from
            # code nothing calls: both are left out.
            [
                ("lib/lib.go", 9, "M", 7),
                ("main.go", 17, "Area", 7),
                ("main.go", 17, "Area", 11),
                ("main.go", 19, "one", 21),
            ],
        )

    def test_graph_edges_carry_the_rule_and_the_producer(self):
        import deepthought.go_callgraph as go_callgraph

        with mock.patch.object(
            go_callgraph, "dynamic_calls", wraps=go_callgraph.dynamic_calls
        ) as spy:
            output = asyncio.run(
                TreeSitterTool(self.root).analyze("r", languages={"go"})
            )
        # The tool would print its usage and succeed for a module with no
        # packages; it is not run for one.
        self.assertEqual(
            spy.call_args.args[1],
            {"": [".", "_examples/ex", "lib"], "broken": ["."], "sub": ["."]},
        )
        edges = sorted(
            (r.properties["callee"], r.target_match["qualified_name"])
            for r in output.relationships
            if r.type == "CALLS" and r.properties.get("rule_id") == GO_CALL_VTA
        )
        self.assertEqual(
            [(callee, target.split("::")[2]) for callee, target in edges],
            [
                ("f", "one"),
                ("i.M", "A.M"),
                ("s.Area", "Circle.Area"),
                ("s.Area", "Square.Area"),
                ("t.M", "A.M"),
                ("t.M", "C.M"),
            ],
        )
        properties = next(
            r.properties
            for r in output.relationships
            if r.properties.get("rule_id") == GO_CALL_VTA
        )
        self.assertEqual(
            (properties["producer"], properties["dispatch"]),
            ("callgraph@v0.31.0", "vta"),
        )
        metrics = output.metadata.custom_metrics["go_dynamic_calls"]
        self.assertEqual((metrics["sites"], metrics["edges"]), (5, 6))
        self.assertEqual(sorted(metrics["failed_packages"]), ["broken"])
        self.assertTrue(
            any("package broken not loaded" in w for w in output.metadata.warnings)
        )


# Functions kept in a slice and run later, as an HTTP client's response
# middleware: the call f(v) in the loop runs what Use appended.
MIDDLEWARE = """package main

type Client struct{ after []func(int) int }

func (c *Client) Use(f func(int) int) { c.after = append(c.after, f) }

func (c *Client) Do(v int) int {
	for _, f := range c.after {
		v = f(v)
	}
	return v
}

func double(v int) int { return v * 2 }

func inc(v int) int { return v + 1 }

func never(v int) int { return v }

func main() {
	c := &Client{}
	c.Use(double)
	c.Use(inc)
	c.Do(1)
}
"""


@unittest.skipUnless(GO_READY and _pinned(), "Install the pinned callgraph")
class FunctionValueTests(unittest.TestCase):
    def test_a_call_through_a_slice_of_functions_reaches_each_appended(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        _write(
            root,
            {"go.mod": "module example.com/mw\n\ngo 1.21\n", "main.go": MIDDLEWARE},
        )
        output = asyncio.run(TreeSitterTool(root).analyze("r", languages={"go"}))
        edges = sorted(
            (
                r.source_match["qualified_name"].split("::")[2],
                r.properties["callee"],
                r.properties["line"],
                r.target_match["qualified_name"].split("::")[2],
            )
            for r in output.relationships
            if r.type == "CALLS" and r.properties.get("rule_id") == GO_CALL_VTA
        )
        # never is not appended, so no call reaches it.
        self.assertEqual(
            edges,
            [("Client.Do", "f", 9, "double"), ("Client.Do", "f", 9, "inc")],
        )


if __name__ == "__main__":
    unittest.main()
