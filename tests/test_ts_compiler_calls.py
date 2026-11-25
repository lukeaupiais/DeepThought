"""JavaScript and TypeScript calls bound through the TypeScript compiler's
modules and exports (deepthought.ts_modules.with_compiler_modules)."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from deepthought import ts_modules
from deepthought.analysis_tools.tree_sitter import TreeSitterTool
from deepthought.calls import (
    CALL_IMPORTED_NAME,
    CALL_MODULE_ATTRIBUTE,
    CALL_TS_COMPILER_EXPORT,
    CALL_TS_DECLARATION_FILE,
    resolve_calls,
)
from deepthought.engine import analyze_repository

FILES = {
    "tsconfig.json": json.dumps(
        {
            "compilerOptions": {
                "allowJs": True,
                "module": "esnext",
                "moduleResolution": "bundler",
            },
            "include": ["**/*"],
        }
    ),
    # An implementation and the declaration file that describes it.
    "lib/impl.js": (
        "export function run() {}\n"
        "export function other() {}\n"
        "export default function main() {}\n"
        # Not a callable: no declaration around its name.
        "export const value = 1;\n"
    ),
    "lib/impl.d.ts": (
        "export declare function run(): void;\n"
        "export declare function other(): void;\n"
        "declare function main(): void;\n"
        "export default main;\n"
        "export declare const value: number;\n"
    ),
    # Re-exports, which the compiler follows into impl.d.ts: the call runs
    # what impl.js exports under the name impl.d.ts gives.
    "lib/index.js": (
        "export * from './impl';\nexport { other as renamed } from './impl';\n"
        "export { typed as retyped } from './types';\n"
        "export { ext } from 'extpkg';\n"
        "export { cjs as cjs2 } from './cjs.cjs';\n"
    ),
    "lib/index.d.ts": (
        "export * from './impl';\nexport { other as renamed } from './impl';\n"
        "export { typed as retyped } from './types';\n"
        "export { ext } from 'extpkg';\n"
        "export { cjs as cjs2 } from './cjs.cjs';\n"
    ),
    # A declaration file with no implementation beside it.
    "lib/types.d.ts": "export declare function typed(): void;\n",
    # ES module and CommonJS declaration files and their implementations.
    "lib/esm.mjs": "export function esm() {}\n",
    "lib/esm.d.mts": "export declare function esm(): void;\n",
    "lib/cjs.cjs": "export function cjs() {}\n",
    "lib/cjs.d.cts": "export declare function cjs(): void;\n",
    # An installed package: its declarations are not the workspace's.
    "node_modules/extpkg/package.json": '{"name": "extpkg", "types": "index.d.ts"}',
    "node_modules/extpkg/index.d.ts": "export declare function ext(): void;\n",
    "app.ts": (
        "import { run, renamed } from './lib/index';\n"
        "import main from './lib/impl';\n"
        "import { typed } from './lib/types';\n"
        "import * as ns from './lib/index';\n"
        "run();\n"
        "renamed();\n"
        "main();\n"
        "typed();\n"
        "ns.run();\n"
        "import { value, retyped, ext } from './lib/index';\n"
        "import { gone } from './missing';\n"
        "import { esm } from './lib/esm.mjs';\n"
        "import { cjs } from './lib/cjs.cjs';\n"
        "value();\n"
        "retyped();\n"
        "ext();\n"
        "gone();\n"
        "esm();\n"
        "cjs();\n"
        "import { cjs2 } from './lib/index';\n"
        "cjs2();\n"
    ),
    "app.js": "import { run } from './lib/index';\nrun();\n",
}

READY = os.environ.get("DEEPTHOUGHT_SCIP_TYPESCRIPT") or shutil.which("scip-typescript")


@unittest.skipUnless(READY, "Install the pinned scip-typescript")
class CompilerCallTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, text in FILES.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def calls(self, languages):
        result = ts_modules.with_compiler_modules(
            analyze_repository(self.root, repository_id="r", languages=languages),
            self.root,
        )
        sites = {s.id: s for s in result.call_sites}
        by_id = {d.id: d for d in result.definitions}
        return {
            (sites[r.call_id].file, sites[r.call_id].line): (
                r.status,
                r.target_id and (by_id[r.target_id].file, by_id[r.target_id].symbol),
                r.rule_id,
            )
            for r in resolve_calls(result)
        }

    def test_calls_run_the_implementation_the_compiler_exports(self):
        self.assertEqual(
            self.calls(("typescript", "javascript")),
            {
                ("app.ts", 5): (
                    "linked",
                    ("lib/impl.js", "run"),
                    CALL_TS_COMPILER_EXPORT,
                ),
                ("app.ts", 6): (
                    "linked",
                    ("lib/impl.js", "other"),
                    CALL_TS_COMPILER_EXPORT,
                ),
                # The default export, declared in the module imported.
                ("app.ts", 7): ("linked", ("lib/impl.js", "main"), CALL_IMPORTED_NAME),
                ("app.ts", 8): (
                    "linked",
                    ("lib/types.d.ts", "typed"),
                    CALL_TS_DECLARATION_FILE,
                ),
                ("app.ts", 9): (
                    "linked",
                    ("lib/impl.js", "run"),
                    CALL_TS_COMPILER_EXPORT,
                ),
                ("app.ts", 14): ("unresolved", None, None),
                # Re-exported from a declaration file with nothing beside it.
                ("app.ts", 15): (
                    "linked",
                    ("lib/types.d.ts", "typed"),
                    CALL_TS_DECLARATION_FILE,
                ),
                ("app.ts", 16): ("unresolved", None, None),
                ("app.ts", 17): ("unresolved", None, None),
                ("app.ts", 18): ("linked", ("lib/esm.mjs", "esm"), CALL_IMPORTED_NAME),
                ("app.ts", 19): ("linked", ("lib/cjs.cjs", "cjs"), CALL_IMPORTED_NAME),
                ("app.ts", 21): (
                    "linked",
                    ("lib/cjs.cjs", "cjs"),
                    CALL_TS_COMPILER_EXPORT,
                ),
                ("app.js", 2): (
                    "linked",
                    ("lib/impl.js", "run"),
                    CALL_TS_COMPILER_EXPORT,
                ),
            },
        )

    def test_a_declaration_file_not_analyzed_still_leads_to_its_implementation(self):
        self.assertEqual(
            self.calls(("javascript",)),
            {
                ("app.js", 2): (
                    "linked",
                    ("lib/impl.js", "run"),
                    CALL_TS_COMPILER_EXPORT,
                )
            },
        )

    def test_imports_stay_the_compilers(self):
        result = ts_modules.with_compiler_modules(
            analyze_repository(
                self.root, repository_id="r", languages=("typescript", "javascript")
            ),
            self.root,
        )
        # Attached once: a result that carries the compiler's view is
        # returned as it is, without running the compiler again.
        self.assertIs(ts_modules.with_compiler_modules(result, self.root), result)
        imports = {i.id: i for i in result.imports}
        # The IMPORTS edge names the declaration file the compiler resolves;
        # only calls run the implementation beside it.
        self.assertIn(
            ("app.ts", "./lib/index", ("lib/index.d.ts",)),
            {
                (
                    imports[r.import_id].file,
                    imports[r.import_id].specifier,
                    tuple(link.target_file for link in r.links),
                )
                for r in result.compiler_modules.resolutions
            },
        )

    def test_exports_are_the_workspace_declarations_each_module_gives(self):
        result = ts_modules.with_compiler_modules(
            analyze_repository(
                self.root, repository_id="r", languages=("typescript", "javascript")
            ),
            self.root,
        )
        exports = result.compiler_modules.exports
        # A declaration file's own exports stay its declarations.
        self.assertEqual(exports["lib/impl.d.ts"]["run"], (("lib/impl.d.ts", 1, 24),))
        self.assertEqual(exports["lib/index.js"]["run"], (("lib/impl.js", 1, 16),))
        # A package outside the workspace declares nothing here.
        self.assertNotIn("ext", exports["lib/index.js"])

    def test_graph_calls_carry_the_compiler_rules(self):
        output = asyncio.run(
            TreeSitterTool(self.root).analyze(
                "r", languages=("typescript", "javascript"), scip_indexes=()
            )
        )
        rules = {
            r.properties["rule_id"]: r.properties["evidence_type"]
            for r in output.relationships
            if r.type == "CALLS"
        }
        self.assertEqual(rules[CALL_TS_COMPILER_EXPORT], "resolved")
        self.assertEqual(rules[CALL_TS_DECLARATION_FILE], "resolved")
        self.assertNotIn(CALL_MODULE_ATTRIBUTE, rules)


if __name__ == "__main__":
    unittest.main()
