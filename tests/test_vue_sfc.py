"""Vue single-file components read as their script blocks
(deepthought.vue_sfc)."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from deepthought import vue_sfc
from deepthought.analysis_tools.data_contracts import DataContractTool
from deepthought.analysis_tools.service_contracts import ServiceContractTool
from deepthought.analysis_tools.tree_sitter import TreeSitterTool
from deepthought.engine import analyze_repository
from deepthought.ts_modules import with_compiler_modules
from typescript_ready import needs_typescript


def _available():
    try:
        vue_sfc.locate()
    except vue_sfc.VueUnavailable:
        return False
    return True


# A template with text that is not ASCII before the scripts, a plain
# script and a script setup block, and a style.
GREETING = """<template>
  <p class="greeting">Grüße, {{ shown }} — ✓</p>
</template>

<script lang="ts">
export default { name: "Greeting" };
</script>

<script setup lang="ts">
import { computed } from "vue";
import { formatName } from "@/helper";

const props = defineProps<{ name: string }>();
function greet(): string {
  return formatName(props.name);
}
const shown = computed(() => greet());
</script>

<style scoped>
.greeting { color: red; }
</style>
"""

FILES = {
    "tsconfig.json": json.dumps(
        {
            "compilerOptions": {
                "module": "esnext",
                "moduleResolution": "bundler",
                "baseUrl": ".",
                "paths": {"@/*": ["src/*"]},
            },
            "include": ["src/**/*.ts", "src/**/*.vue"],
        }
    ),
    "src/helper.ts": (
        "export function formatName(name: string): string {\n  return name.trim();\n}\n"
    ),
    "src/components/Greeting.vue": GREETING,
    # A component written in TSX, and one whose script is another file.
    "src/components/Badge.vue": (
        '<script setup lang="tsx">\n'
        "function Icon() {\n"
        '  return <span class="icon" />;\n'
        "}\n"
        "</script>\n"
    ),
    "src/components/External.vue": (
        '<template><b>x</b></template>\n<script src="./external.ts"></script>\n'
    ),
    "src/main.ts": (
        'import Greeting from "./components/Greeting.vue";\n'
        'import Badge from "@/components/Badge.vue";\n'
        "export const parts = [Greeting, Badge];\n"
    ),
}


class ScriptSourceTests(unittest.TestCase):
    def test_bytes_outside_the_blocks_are_blanked_and_lines_kept(self):
        source = "<t>é</t>\r\n<script>\nlet x = 1;\n</script>\n".encode()
        start = source.index(b"\nlet")
        end = source.index(b"</script>")
        masked = vue_sfc.script_source(
            source,
            vue_sfc.Component((vue_sfc.ScriptBlock(start, end, None, False),), ()),
        )
        self.assertEqual(len(masked), len(source))
        self.assertEqual(masked[start:end], source[start:end])
        self.assertEqual(masked.split(b"\n")[0], b" " * 9 + b"\r")
        self.assertEqual(masked.count(b"\n"), source.count(b"\n"))
        self.assertEqual(masked[end:].strip(), b"")

    def test_a_missing_parser_is_an_error(self):
        environ = {
            "PATH": os.environ.get("PATH", ""),
            vue_sfc.ENVIRONMENT_VARIABLE: "/nonexistent/compiler-sfc",
        }
        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        with self.assertRaisesRegex(vue_sfc.VueUnavailable, "compiler-sfc@"):
            vue_sfc.locate(environ)


@unittest.skipUnless(_available(), f"Install {vue_sfc.VUE_COMPILER_SFC_TOOL}")
class ComponentTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, text in FILES.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, "utf-8")

    def test_vue_parser_finds_the_script_blocks(self):
        source = GREETING.encode()
        component = vue_sfc.component(source)
        self.assertEqual(
            [(b.lang, b.setup) for b in component.blocks],
            [("ts", False), ("ts", True)],
        )
        first, second = component.blocks
        self.assertEqual(
            source[first.start : first.end].strip(),
            b'export default { name: "Greeting" };',
        )
        self.assertTrue(source[second.start : second.end].strip().startswith(b"import"))
        self.assertEqual(
            vue_sfc.component(FILES["src/components/External.vue"].encode()).blocks,
            (),
        )

    def test_declarations_calls_and_imports_are_read_at_component_lines(self):
        result = analyze_repository(self.root, repository_id="r")
        coverage = {c.file: (c.language, c.status) for c in result.coverage}
        for name in ("Greeting", "Badge", "External"):
            self.assertEqual(
                coverage[f"src/components/{name}.vue"], ("typescript", "analyzed")
            )
        vue = "src/components/Greeting.vue"
        self.assertIn(
            ("greet", "Function", 14, 0, 16, 1),
            [
                (d.symbol, d.kind, d.line, d.column, d.end_line, d.end_column)
                for d in result.definitions
                if d.file == vue
            ],
        )
        self.assertIn(
            ("Icon", "Function", 2),
            [
                (d.symbol, d.kind, d.line)
                for d in result.definitions
                if d.file == "src/components/Badge.vue"
            ],
        )
        calls = {
            (c.callee, c.line, c.column) for c in result.call_sites if c.file == vue
        }
        self.assertTrue(
            {("formatName", 15, 9), ("greet", 17, 29), ("computed", 17, 14)} <= calls
        )
        self.assertEqual(
            sorted((i.specifier, i.line) for i in result.imports if i.file == vue),
            [("@/helper", 11), ("vue", 10)],
        )

    @needs_typescript
    def test_a_component_is_recorded_by_the_digest_of_its_file(self):
        # The digest is the file's, not its script's: the contract tools
        # compare it with the file on disk to find files changed meanwhile.
        result = analyze_repository(self.root, repository_id="r")
        vue = "src/components/Greeting.vue"
        (coverage,) = [c for c in result.coverage if c.file == vue]
        self.assertEqual(
            coverage.digest,
            hashlib.sha256((Path(self.root) / vue).read_bytes()).hexdigest(),
        )
        for tool in (ServiceContractTool, DataContractTool):
            asyncio.run(tool(self.root).analyze("r", languages=["typescript"]))

    @unittest.skipUnless(
        os.environ.get("DEEPTHOUGHT_SCIP_TYPESCRIPT")
        or shutil.which("scip-typescript"),
        "Install the pinned scip-typescript",
    )
    def test_the_compiler_resolves_imports_of_and_from_components(self):
        result = analyze_repository(self.root, repository_id="r")
        result = with_compiler_modules(result, self.root)
        imports = {i.id: i for i in result.imports}
        links = {
            (imports[r.import_id].file, imports[r.import_id].specifier): tuple(
                link.target_file for link in r.links
            )
            for r in result.compiler_modules.resolutions
        }
        self.assertEqual(
            links[("src/main.ts", "./components/Greeting.vue")],
            ("src/components/Greeting.vue",),
        )
        self.assertEqual(
            links[("src/main.ts", "@/components/Badge.vue")],
            ("src/components/Badge.vue",),
        )
        self.assertEqual(
            links[("src/components/Greeting.vue", "@/helper")], ("src/helper.ts",)
        )
        self.assertEqual(
            result.compiler_modules.exports["src/components/Greeting.vue"]["default"],
            (("src/components/Greeting.vue", 6, 0),),
        )

    @unittest.skipUnless(
        os.environ.get("DEEPTHOUGHT_SCIP_TYPESCRIPT")
        or shutil.which("scip-typescript"),
        "Install the pinned scip-typescript",
    )
    def test_graph_links_a_module_to_the_component_it_imports(self):
        output = asyncio.run(
            TreeSitterTool(self.root).analyze("r", languages=["typescript"])
        )
        edges = {
            (r.properties["specifier"], r.properties["rule_id"])
            for r in output.relationships
            if r.type == "IMPORTS"
        }
        self.assertIn(
            ("./components/Greeting.vue", "typescript.compiler-module"), edges
        )
        self.assertIn(("@/helper", "typescript.compiler-module"), edges)


if __name__ == "__main__":
    unittest.main()
