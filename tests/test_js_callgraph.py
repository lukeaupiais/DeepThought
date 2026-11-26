"""JavaScript and TypeScript calls through function values, from the pinned
Jelly call graph (deepthought.js_callgraph)."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest

from deepthought import js_callgraph
from deepthought.analysis_tools.tree_sitter import TreeSitterTool
from deepthought.engine import analyze_repository


def _pinned():
    try:
        return js_callgraph.locate_jelly()
    except js_callgraph.JellyUnavailable:
        return None


FILES = {
    "tsconfig.json": json.dumps(
        {"compilerOptions": {"module": "commonjs"}, "include": ["src"]}
    ),
    # Plugins kept in an array and hooks in a map, run later.
    "src/registry.ts": (
        "type Plugin = (input: string) => string;\n"
        "const plugins: Plugin[] = [];\n"
        "export function register(plugin: Plugin): void {\n"
        "  plugins.push(plugin);\n"
        "}\n"
        "export function runAll(input: string): string {\n"
        "  let value = input;\n"
        "  for (const plugin of plugins) {\n"
        "    value = plugin(value);\n"
        "  }\n"
        "  return value;\n"
        "}\n"
        "const hooks = new Map<string, () => void>();\n"
        "export function on(name: string, hook: () => void) {\n"
        "  hooks.set(name, hook);\n"
        "}\n"
        "export function emit(name: string) {\n"
        "  const hook = hooks.get(name);\n"
        "  if (hook) hook();\n"
        "}\n"
    ),
    "src/plugins.ts": (
        "import { register, on } from './registry';\n"
        "import { expect } from 'unanalyzed';\n"
        "export function upper(s: string): string { return s.toUpperCase(); }\n"
        "export const trim = (s: string): string => s.trim();\n"
        "export function unused(s: string): string { return s; }\n"
        "export function onStart(): void {}\n"
        "register(upper);\n"
        "register(trim);\n"
        "on('start', onStart);\n"
        # A function handed to code that is not analyzed is not called here.
        "expect(unused);\n"
    ),
    "src/main.ts": (
        "import './plugins';\n"
        "import { runAll, emit } from './registry';\n"
        "runAll(' a ');\n"
        "emit('start');\n"
    ),
}


class LocateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)

    def test_missing_tool_is_an_error(self):
        with self.assertRaisesRegex(js_callgraph.JellyUnavailable, "npm install -g"):
            js_callgraph.locate_jelly({"PATH": self.directory})

    def test_another_version_is_refused(self):
        fake = Path(self.directory) / "jelly"
        fake.write_text("#!/bin/sh\necho 0.1.0\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        with self.assertRaisesRegex(
            js_callgraph.JellyUnavailable, "0.13.0 is required"
        ):
            js_callgraph.locate_jelly(
                {js_callgraph.JELLY_ENVIRONMENT_VARIABLE: str(fake), "PATH": ""}
            )


@unittest.skipUnless(_pinned(), "Install the pinned Jelly")
@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_SCIP_TYPESCRIPT") or shutil.which("scip-typescript"),
    "Install the pinned scip-typescript",
)
class PointsToTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, text in FILES.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def test_calls_through_values_reach_the_functions_that_flow_there(self):
        result = analyze_repository(self.root, repository_id="r")
        calls = js_callgraph.points_to_calls(
            self.root, js_callgraph.analyzed_scripts(result), _pinned()
        )
        symbols = {d.id: d.symbol for d in result.definitions}
        sites = {s.id: s for s in result.call_sites}
        bound = js_callgraph.bind_points_to_calls(result, calls, self.root)
        self.assertEqual(
            sorted(
                (sites[s].file, sites[s].line, sites[s].callee, symbols[t])
                for s, targets in bound.items()
                if sites[s].file == "src/registry.ts"
                for t in targets
            ),
            # Not unused, which is only handed to code that is not analyzed.
            [
                ("src/registry.ts", 9, "plugin", "trim"),
                ("src/registry.ts", 9, "plugin", "upper"),
                ("src/registry.ts", 19, "hook", "onStart"),
            ],
        )
        self.assertNotIn(
            "unused", {symbols[t] for targets in bound.values() for t in targets}
        )

    def test_graph_edges_only_for_calls_no_other_rule_links(self):
        output = asyncio.run(
            TreeSitterTool(self.root).analyze("r", languages=["typescript"])
        )
        edges = sorted(
            (
                r.source_match["qualified_name"].split("::")[2],
                r.properties["callee"],
                r.target_match["qualified_name"].split("::")[2],
                r.properties["producer"],
            )
            for r in output.relationships
            if r.type == "CALLS"
            and r.properties.get("rule_id") == js_callgraph.JS_CALL_POINTS_TO
        )
        self.assertEqual(
            edges,
            [
                ("emit", "hook", "onStart", js_callgraph.JELLY_TOOL),
                ("runAll", "plugin", "trim", js_callgraph.JELLY_TOOL),
                ("runAll", "plugin", "upper", js_callgraph.JELLY_TOOL),
            ],
        )
        metrics = output.metadata.custom_metrics["js_points_to_calls"]
        self.assertEqual(
            (metrics["sites"], metrics["edges"], metrics["error"]), (2, 3, None)
        )


if __name__ == "__main__":
    unittest.main()
