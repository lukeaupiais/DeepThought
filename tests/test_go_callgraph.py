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
        with self.assertRaisesRegex(GoCallgraphUnavailable, "go install"):
            locate_go_callgraph({"PATH": self.directory})

    @unittest.skipUnless(GO_READY, "Provision Go")
    def test_a_binary_not_built_from_the_pinned_module_is_refused(self):
        fake = Path(self.directory) / "callgraph"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        with self.assertRaisesRegex(GoCallgraphUnavailable, "is not built from"):
            locate_go_callgraph({GO_CALLGRAPH_ENVIRONMENT_VARIABLE: str(fake)})

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
            {"sites": 0, "edges": 0, "failed_modules": {}},
        )


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
                "broken/go.mod": "module example.com/broken\n\ngo 1.21\n",
                "broken/main.go": "package main\n\nfunc main() { undefined() }\n",
            },
        )

    def test_vta_gives_the_implementations_that_reach_each_call(self):
        calls, failures = dynamic_calls(self.root, ["", "broken"], _pinned())
        self.assertEqual(sorted(failures), ["broken"])
        self.assertEqual(
            sorted((c.line, c.callee_name, c.callee_line) for c in calls),
            # Unused.Area never reaches s.Area(): VTA leaves it out.
            [(17, "Area", 7), (17, "Area", 11), (19, "one", 21)],
        )

    def test_graph_edges_carry_the_rule_and_the_producer(self):
        output = asyncio.run(TreeSitterTool(self.root).analyze("r", languages={"go"}))
        edges = sorted(
            (r.properties["callee"], r.target_match["qualified_name"])
            for r in output.relationships
            if r.type == "CALLS" and r.properties.get("rule_id") == GO_CALL_VTA
        )
        self.assertEqual(
            [(callee, target.split("::")[2]) for callee, target in edges],
            [("f", "one"), ("s.Area", "Circle.Area"), ("s.Area", "Square.Area")],
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
        self.assertEqual((metrics["sites"], metrics["edges"]), (2, 3))
        self.assertEqual(sorted(metrics["failed_modules"]), ["broken"])
        self.assertTrue(
            any("module broken not loaded" in w for w in output.metadata.warnings)
        )


if __name__ == "__main__":
    unittest.main()
