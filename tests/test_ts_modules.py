"""JavaScript and TypeScript imports resolved by the TypeScript compiler
(deepthought.ts_modules)."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest

from deepthought import ts_modules
from deepthought.analysis_tools.tree_sitter import TreeSitterTool
from deepthought.engine import analyze_repository

FILES = {
    "tsconfig.json": json.dumps(
        {
            "compilerOptions": {
                "baseUrl": ".",
                "paths": {"@lib/*": ["src/lib/*"]},
                "moduleResolution": "bundler",
                "module": "esnext",
                "resolveJsonModule": True,
                "allowJs": True,
                # The compiler adds an import of react/jsx-runtime itself.
                "jsx": "react-jsx",
            },
            "include": ["src/**/*"],
        }
    ),
    "src/a.ts": (
        'import { x as é } from "@lib/m";\n'
        'import "./side";\n'
        'import pkg from "pkg";\n'
        'import gone from "./gone";\n'
        'import data from "./data.json";\n'
        'export { y } from "./b"; import { y as y2 } from "./b";\n'
        'const lazy = import("./lazy");\n'
        'import types from "typed";\n'
    ),
    "src/b.ts": "export const y = 1;\n",
    "src/j.tsx": "export const e = <div />;\n",
    "src/side.ts": "console.log(1);\n",
    "src/lazy.ts": "export default 1;\n",
    "src/data.json": "{}\n",
    "src/c.js": 'const b = require("./b");\n',
    "src/lib/m.ts": "export const x = 1;\n",
    "node_modules/pkg/package.json": '{"name": "pkg", "types": "index.d.ts"}',
    "node_modules/pkg/index.d.ts": "export default 1;\n",
    # A package of the workspace: its types win over its JavaScript.
    "typed/package.json": '{"name": "typed", "types": "index.d.ts"}',
    "typed/index.d.ts": "export default 1;\n",
    "typed/index.js": "export default 1;\n",
    # No project includes these: the options of the nearest config, here
    # the root's, whose paths apply.
    "other/x.ts": 'import { y } from "./y";\n',
    "other/p.ts": 'import { x } from "@lib/m";\n',
    # Configs above the workspace are not read.
    "other/up.ts": 'import { y } from "@up/y";\n',
    "other/y.ts": "export const y = 1;\n",
    # JavaScript too, as the language service allows it there.
    "other/z.js": 'import { y } from "./y";\n',
    # A jsconfig.json project.
    "js/jsconfig.json": (
        '{"compilerOptions": {"baseUrl": ".", "paths": {"~/*": ["*"]},'
        ' "preserveSymlinks": true}}'
    ),
    # The symbolic link the compiler keeps is followed to the file.
    "js/b.js": 'import l from "linkedpkg";\n',
    "js/a.js": 'import { w } from "~/w";\n',
    "js/w.js": "export const w = 1;\n",
    # The compiler includes one file per name: the .jsx beside d.js is left
    # out of the project, and resolved with its options in a program of
    # its own.
    # Excluded from a project that does not allow JavaScript: allowed here.
    "tsonly/tsconfig.json": (
        '{"compilerOptions": {"baseUrl": ".", "paths": {"@t/*": ["*"]}},'
        ' "include": ["v.ts"]}'
    ),
    "tsonly/s.js": 'import { v } from "@t/v";\n',
    "tsonly/v.ts": "export const v = 1;\n",
    # Of two configs in a directory, the tsconfig.json is the nearest.
    "both/tsconfig.json": (
        '{"compilerOptions": {"allowJs": true, "baseUrl": ".",'
        ' "paths": {"@b/*": ["ts/*"]}}, "include": ["ts"]}'
    ),
    "both/jsconfig.json": (
        '{"compilerOptions": {"baseUrl": ".", "paths": {"@b/*": ["js/*"]}},'
        ' "include": ["js"]}'
    ),
    "both/f.js": 'import { t } from "@b/t";\n',
    "both/ts/t.js": "export const t = 1;\n",
    "both/js/t.js": "export const t = 1;\n",
    "js/d.js": "export const d = 1;\n",
    "js/d.jsx": 'import { w } from "~/w";\n',
    # A solution config includes its files through references, to a file
    # and to a directory; a reference back to it is not followed again.
    "sol/tsconfig.json": (
        '{"files": [], "references": [{"path": "./tsconfig.lib.json"},'
        ' {"path": "./cfg"}]}'
    ),
    "sol/tsconfig.lib.json": (
        '{"compilerOptions": {"composite": true, "baseUrl": ".",'
        ' "paths": {"@p/*": ["pkg/*"]}}, "include": ["pkg/**/*"]}'
    ),
    "sol/cfg/tsconfig.json": (
        '{"compilerOptions": {"composite": true, "baseUrl": "..",'
        ' "paths": {"@q/*": ["lib2/*"]}}, "include": ["../lib2/**/*"],'
        ' "references": [{"path": "../tsconfig.json"}]}'
    ),
    "sol/pkg/a.ts": 'import { v } from "@p/v";\n',
    "sol/pkg/v.ts": "export const v = 1;\n",
    "sol/lib2/a.ts": 'import { v } from "@q/v";\n',
    "sol/lib2/v.ts": "export const v = 1;\n",
    "sol/stray.ts": 'import { v } from "./pkg/v";\n',
    # A workspace package linked into node_modules (as pnpm does) is the
    # workspace's own file.
    "src/linked.ts": 'import l from "linkedpkg";\n',
    "packages/linkedpkg/package.json": '{"name": "linkedpkg", "types": "index.d.ts"}',
    "packages/linkedpkg/index.d.ts": "export default 1;\n",
    # Columns are UTF-16 in the compiler and bytes in the parser.
    "src/u.ts": (
        'import { éééééééééééééééééééééééééééééé } from "./b"; import u from "./b";\n'
    ),
    "p.py": "import q\n",
    "q.py": "",
}


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_SCIP_TYPESCRIPT")
    or __import__("shutil").which("scip-typescript"),
    "Install the pinned scip-typescript",
)
class CompilerImportTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "ws"
        (self.root.parent / "tsconfig.json").write_text(
            '{"compilerOptions": {"baseUrl": ".", "paths": {"@up/*": ["ws/other/*"]}},'
            ' "include": ["**/*"]}'
        )
        for name, text in FILES.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        # The workspace's typed/ package, as a bare specifier.
        paths = json.loads(FILES["tsconfig.json"])
        paths["compilerOptions"]["paths"]["typed"] = ["typed"]
        (self.root / "tsconfig.json").write_text(json.dumps(paths))
        (self.root / "node_modules/linkedpkg").symlink_to(
            self.root / "packages/linkedpkg"
        )

    def outcomes(self, result, resolutions):
        imports = {i.id: i for i in result.imports}
        return {
            (imports[r.import_id].file, imports[r.import_id].line): (
                imports[r.import_id].specifier,
                r.status,
                r.reason,
                tuple((link.target_file, link.rule_id) for link in r.links),
            )
            for r in resolutions
        }

    def test_imports_link_to_the_files_the_compiler_resolves(self):
        result = analyze_repository(
            self.root,
            repository_id="r",
            languages=("typescript", "javascript", "python"),
        )
        scripts = [c.file for c in result.coverage]
        found = ts_modules.compiler_imports(self.root, scripts)
        bound = ts_modules.bind_imports(result, self.root, found)
        # One resolution per JavaScript or TypeScript import, in identity
        # order; other languages and configuration facts get none.
        self.assertEqual(
            [r.import_id for r in bound],
            sorted(
                i.id
                for i in result.imports
                if i.file.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"))
            ),
        )
        rule = ts_modules.TYPESCRIPT_COMPILER_MODULE
        outcomes = self.outcomes(result, bound)
        self.assertEqual(
            {k: v for k, v in outcomes.items() if k[0].endswith(".ts")},
            {
                ("src/a.ts", 1): ("@lib/m", "linked", None, (("src/lib/m.ts", rule),)),
                ("src/a.ts", 2): ("./side", "linked", None, (("src/side.ts", rule),)),
                ("src/a.ts", 3): ("pkg", "unresolved", "compiler_external_module", ()),
                ("src/a.ts", 4): (
                    "./gone",
                    "unresolved",
                    "compiler_module_not_found",
                    (),
                ),
                ("src/a.ts", 5): (
                    "./data.json",
                    "unresolved",
                    "compiler_target_not_analyzed",
                    (),
                ),
                ("src/a.ts", 6): ("./b", "linked", None, (("src/b.ts", rule),)),
                ("src/a.ts", 7): (
                    "./lazy",
                    "linked",
                    None,
                    (("src/lazy.ts", rule),),
                ),
                ("src/a.ts", 8): (
                    "typed",
                    "linked",
                    None,
                    (("typed/index.d.ts", rule),),
                ),
                ("other/x.ts", 1): ("./y", "linked", None, (("other/y.ts", rule),)),
                ("sol/pkg/a.ts", 1): (
                    "@p/v",
                    "linked",
                    None,
                    (("sol/pkg/v.ts", rule),),
                ),
                ("sol/lib2/a.ts", 1): (
                    "@q/v",
                    "linked",
                    None,
                    (("sol/lib2/v.ts", rule),),
                ),
                ("sol/stray.ts", 1): (
                    "./pkg/v",
                    "linked",
                    None,
                    (("sol/pkg/v.ts", rule),),
                ),
                ("other/up.ts", 1): (
                    "@up/y",
                    "unresolved",
                    "compiler_module_not_found",
                    (),
                ),
                ("src/linked.ts", 1): (
                    "linkedpkg",
                    "linked",
                    None,
                    (("packages/linkedpkg/index.d.ts", rule),),
                ),
                ("src/u.ts", 1): ("./b", "linked", None, (("src/b.ts", rule),)),
                ("other/p.ts", 1): (
                    "@lib/m",
                    "linked",
                    None,
                    (("src/lib/m.ts", rule),),
                ),
            },
        )
        for key, target in (
            ("other/z.js", "other/y.ts"),
            ("js/a.js", "js/w.js"),
            ("js/b.js", "packages/linkedpkg/index.d.ts"),
            ("js/d.jsx", "js/w.js"),
            ("tsonly/s.js", "tsonly/v.ts"),
            ("both/f.js", "both/ts/t.js"),
        ):
            self.assertEqual(outcomes[(key, 1)][3], ((target, rule),))
        # Both statements on a line are bound, each at its own specifier.
        twice = [
            r
            for r in bound
            if next(i for i in result.imports if i.id == r.import_id).file == "src/u.ts"
        ]
        self.assertEqual([r.status for r in twice], ["linked", "linked"])
        line6 = [
            r
            for r in bound
            if next(i for i in result.imports if i.id == r.import_id).line == 6
        ]
        self.assertEqual(len(line6), 2)
        self.assertTrue(all(r.status == "linked" for r in line6))
        for key in (("p.py", 1), ("tsconfig.json", 1), ("typed/package.json", 1)):
            self.assertNotIn(key, outcomes)
        # require() in JavaScript and import() are bound like statements.
        self.assertEqual(outcomes[("src/c.js", 1)][3], (("src/b.ts", rule),))
        self.assertNotIn("src/j.tsx", {r.file for r in found})
        projects = {(row.file, row.project) for row in found}
        self.assertIn(("src/a.ts", "tsconfig.json"), projects)
        self.assertIn(("other/x.ts", None), projects)
        options = {(row.file, row.project, row.options_from) for row in found}
        self.assertIn(("other/p.ts", None, "tsconfig.json"), options)
        self.assertIn(("js/d.jsx", None, "js/jsconfig.json"), options)

    def test_an_import_the_compiler_does_not_record_is_reported(self):
        result = analyze_repository(
            self.root, repository_id="r", languages=("typescript",)
        )
        bound = ts_modules.bind_imports(result, self.root, [])
        self.assertEqual({r.reason for r in bound}, {ts_modules.NO_IMPORT})
        # A row for another specifier, or outside the statement, is not it.
        (item,) = [i for i in result.imports if i.file == "other/x.ts"]
        rows = [
            ts_modules.CompilerImport("other/x.ts", 1, 18, "./z", None, None),
            ts_modules.CompilerImport(
                "other/x.ts", 2, 0, "./y", str(self.root / "other/y.ts"), None
            ),
        ]
        (resolution,) = [
            r
            for r in ts_modules.bind_imports(result, self.root, rows)
            if r.import_id == item.id
        ]
        self.assertEqual(resolution.reason, ts_modules.NO_IMPORT)
        # Of two statements on a line, a row belongs to the one around it.
        first, second = sorted(
            (i for i in result.imports if i.file == "src/a.ts" and i.line == 6),
            key=lambda i: i.column,
        )
        row = ts_modules.CompilerImport(
            "src/a.ts", 6, 18, "./b", str(self.root / "src/b.ts"), "tsconfig.json"
        )
        found = {
            r.import_id: r.status
            for r in ts_modules.bind_imports(result, self.root, [row])
        }
        self.assertEqual((found[first.id], found[second.id]), ("linked", "unresolved"))
        # A file outside the workspace is external.
        outside = ts_modules.CompilerImport(
            "other/x.ts", 1, 18, "./y", "/elsewhere/y.ts", None
        )
        (resolution,) = [
            r
            for r in ts_modules.bind_imports(result, self.root, [outside])
            if r.import_id == item.id
        ]
        self.assertEqual(resolution.reason, ts_modules.EXTERNAL)

    def test_graph_edges_name_the_rule_and_the_compiler(self):
        output = asyncio.run(
            TreeSitterTool(self.root).analyze("r", languages=["typescript"])
        )
        edges = sorted(
            (
                r.properties["specifier"],
                r.properties["rule_id"],
                r.properties["evidence_type"],
                r.properties.get("producer"),
            )
            for r in output.relationships
            if r.type == "IMPORTS"
        )
        self.assertIn(
            ("./side", "typescript.compiler-module", "resolved", "typescript@6.0.3"),
            edges,
        )
        self.assertEqual({e[1] for e in edges}, {"typescript.compiler-module"})


class LocateTypeScriptTests(unittest.TestCase):
    def package(self, root, name, version):
        """A fake scip-typescript install whose typescript is ``version``."""
        package = Path(root) / "lib/node_modules/@sourcegraph/scip-typescript"
        (package / "dist").mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"name": name}))
        binary = package / "dist/main.js"
        binary.write_text("")
        binary.chmod(0o755)
        typescript = package / "node_modules/typescript"
        (typescript / "lib").mkdir(parents=True)
        (typescript / "package.json").write_text(
            json.dumps({"name": "typescript", "main": "lib/typescript.js"})
        )
        (typescript / "lib/typescript.js").write_text(
            f"module.exports = {{version: {json.dumps(version)}}};\n"
        )
        return binary

    def test_the_pinned_version_is_found_and_any_other_refused(self):
        with tempfile.TemporaryDirectory() as root:
            binary = self.package(root, "@sourcegraph/scip-typescript", "6.0.3")
            environ = {
                "PATH": os.environ["PATH"],
                "DEEPTHOUGHT_SCIP_TYPESCRIPT": str(binary),
            }
            node, path = ts_modules.locate_typescript(environ)
            self.assertTrue(path.endswith("node_modules/typescript/lib/typescript.js"))
            self.assertTrue(Path(node).name.startswith("node"))
            (
                binary.parents[1] / "node_modules/typescript/lib/typescript.js"
            ).write_text('module.exports = {version: "5.9.3"};\n')
            with self.assertRaisesRegex(ts_modules.TypeScriptUnavailable, "5.9.3"):
                ts_modules.locate_typescript(environ)
            # No node on PATH.
            with self.assertRaisesRegex(ts_modules.TypeScriptUnavailable, "node"):
                ts_modules.locate_typescript({**environ, "PATH": str(root)})
        with tempfile.TemporaryDirectory() as root:
            binary = self.package(root, "other", "6.0.3")
            environ = {
                "PATH": os.environ["PATH"],
                "DEEPTHOUGHT_SCIP_TYPESCRIPT": str(binary),
            }
            with self.assertRaises(ts_modules.TypeScriptUnavailable):
                ts_modules.locate_typescript(environ)
        environ = {
            "PATH": os.environ["PATH"],
            "DEEPTHOUGHT_SCIP_TYPESCRIPT": "/missing",
        }
        with self.assertRaisesRegex(
            ts_modules.TypeScriptUnavailable, "scip-typescript"
        ):
            ts_modules.locate_typescript(environ)


if __name__ == "__main__":
    unittest.main()
