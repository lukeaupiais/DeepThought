"""Static import facts and their rule-based resolution to analyzed files."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import deepthought
from deepthought import analyze_repository
from deepthought.imports import (
    PYTHON_ABSOLUTE_MODULE,
    PYTHON_FROM_IMPORT_SUBMODULE,
    PYTHON_RELATIVE_MODULE,
    TypeScriptModulesRequired,
    resolve_imports,
)
from deepthought.ts_modules import TYPESCRIPT_COMPILER_MODULE, with_compiler_modules
from deepthought.models import AnalysisResult, Import

FIXTURES = Path(__file__).parent / "fixtures"
PYTHON_READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_python"
)
TYPESCRIPT_READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_typescript"
)
SCIP_READY = importlib.util.find_spec("google.protobuf")
COMPILER_READY = os.environ.get("DEEPTHOUGHT_SCIP_TYPESCRIPT") or __import__(
    "shutil"
).which("scip-typescript")


def analyze(files, language, compiled=False):
    with tempfile.TemporaryDirectory() as directory:
        for name, content in files.items():
            path = Path(directory) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        result = analyze_repository(
            directory, repository_id="fixture", languages=(language,)
        )
        return with_compiler_modules(result, directory) if compiled else result


def resolved(result):
    """{(importing file, specifier): (status, ((target, rule), ...), reason)}"""
    imports = {item.id: item for item in result.imports}
    return {
        (imports[r.import_id].file, imports[r.import_id].specifier): (
            r.status,
            tuple((link.target_file, link.rule_id) for link in r.links),
            r.reason,
        )
        for r in resolve_imports(result)
    }


@unittest.skipUnless(PYTHON_READY, "Provision the python extra")
class PythonImportTests(unittest.TestCase):
    def test_statements_are_extracted_with_names_kinds_and_locations(self):
        result = analyze(
            {
                "main.py": (
                    "import a.b as c, d\n"
                    "from .m import x as y, z\n"
                    "from e import *\n"
                    "def later():\n"
                    "    import f\n"
                    "    importlib.import_module('g')\n"
                )
            },
            "python",
        )
        self.assertEqual(result.schema_version, "3")
        self.assertEqual(
            result.capabilities,
            ("call_sites", "definitions", "impls", "imports", "packages", "supertypes"),
        )
        self.assertEqual(
            [
                (i.specifier, i.kind, i.names, i.local_names, i.line)
                for i in result.imports
            ],
            [
                ("a.b", "import", (), ("c",), 1),
                ("d", "import", (), ("d",), 1),
                (".m", "from_import", ("x", "z"), ("y", "z"), 2),
                ("e", "from_import", ("*",), ("*",), 3),
                ("f", "import", (), ("f",), 5),
            ],
        )

    def test_relative_imports_resolve_with_package_precedence(self):
        result = analyze(
            {
                "pkg/__init__.py": "",
                "pkg/main.py": (
                    "from .m import x\nfrom . import sub\nfrom .. import top\n"
                ),
                "pkg/m.py": "",
                "pkg/m/__init__.py": "",
                "pkg/sub.py": "",
                "top.py": "from .. import beyond\n",
            },
            "python",
        )
        links = resolved(result)
        self.assertEqual(
            links[("pkg/main.py", ".m")],
            ("linked", (("pkg/m/__init__.py", PYTHON_RELATIVE_MODULE),), None),
        )
        self.assertEqual(
            links[("pkg/main.py", ".")],
            (
                "linked",
                (
                    ("pkg/__init__.py", PYTHON_RELATIVE_MODULE),
                    ("pkg/sub.py", PYTHON_FROM_IMPORT_SUBMODULE),
                ),
                None,
            ),
        )
        # The repository root is not a package, so top.py is not a submodule.
        self.assertEqual(
            links[("pkg/main.py", "..")], ("unresolved", (), "outside_package")
        )
        self.assertEqual(
            links[("top.py", "..")], ("unresolved", (), "beyond_repository_root")
        )

    def test_absolute_imports_match_root_or_src_layout_and_disclose_ambiguity(self):
        result = analyze(
            {
                "app.py": "import pkg.mod\nimport os\nfrom pkg import mod\n",
                "src/pkg/__init__.py": "",
                "src/pkg/mod.py": "import shared\n",
                "shared.py": "",
                "src/shared.py": "",
            },
            "python",
        )
        links = resolved(result)
        self.assertEqual(
            links[("app.py", "pkg.mod")],
            ("linked", (("src/pkg/mod.py", PYTHON_ABSOLUTE_MODULE),), None),
        )
        self.assertEqual(
            links[("app.py", "pkg")],
            (
                "linked",
                (
                    ("src/pkg/__init__.py", PYTHON_ABSOLUTE_MODULE),
                    ("src/pkg/mod.py", PYTHON_FROM_IMPORT_SUBMODULE),
                ),
                None,
            ),
        )
        self.assertEqual(
            links[("app.py", "os")], ("unresolved", (), "no_analyzed_file")
        )
        ambiguous = resolve_imports(result)
        ambiguous = next(
            r
            for r in ambiguous
            if any(
                i.id == r.import_id and i.specifier == "shared" for i in result.imports
            )
        )
        self.assertEqual(ambiguous.status, "ambiguous")
        self.assertEqual(ambiguous.candidates, ("shared.py", "src/shared.py"))
        self.assertEqual(ambiguous.links, ())

    def test_parent_relative_import_resolves_inside_a_package(self):
        result = analyze(
            {
                "pkg/__init__.py": "",
                "pkg/util.py": "",
                "pkg/sub/__init__.py": "",
                "pkg/sub/main.py": "from .. import util\nfrom ..util import f\n",
            },
            "python",
        )
        links = resolved(result)
        self.assertEqual(
            links[("pkg/sub/main.py", "..")],
            (
                "linked",
                (
                    ("pkg/__init__.py", PYTHON_RELATIVE_MODULE),
                    ("pkg/util.py", PYTHON_FROM_IMPORT_SUBMODULE),
                ),
                None,
            ),
        )
        self.assertEqual(
            links[("pkg/sub/main.py", "..util")],
            ("linked", (("pkg/util.py", PYTHON_RELATIVE_MODULE),), None),
        )

    def test_self_import_produces_no_link(self):
        result = analyze({"pkg/__init__.py": "from . import helper\n"}, "python")
        self.assertEqual(
            resolved(result)[("pkg/__init__.py", ".")],
            ("unresolved", (), "no_analyzed_file"),
        )


@unittest.skipUnless(TYPESCRIPT_READY, "Provision the typescript extra")
class TypeScriptImportTests(unittest.TestCase):
    def test_imports_resolve_only_through_the_compiler(self):
        files = {
            "src/main.ts": (
                "import { a } from './a';\n"
                "import type { T } from './dir';\n"
                "import { j } from './j.js';\n"
                "export * from '../lib/b';\n"
                "import * as pkg from 'pkg';\n"
                "import '../../outside';\n"
                "const r = require('./a');\n"
            ),
            "src/a.ts": "",
            "src/a.tsx": "",
            "src/dir/index.ts": "",
            "src/j.ts": "",
            "lib/b.tsx": "",
        }
        result = analyze(files, "typescript")
        self.assertEqual(
            [(i.specifier, i.kind, i.names, i.local_names) for i in result.imports],
            [
                ("./a", "import", ("a",), ("a",)),
                ("./dir", "import", ("T",), ("T",)),
                ("./j.js", "import", ("j",), ("j",)),
                ("../lib/b", "export_from", ("*",), ()),
                ("pkg", "import", ("*",), ("pkg",)),
                ("../../outside", "import", (), ()),
            ],
        )
        # The parser's facts alone resolve nothing: the compiler must run.
        with self.assertRaises(TypeScriptModulesRequired):
            resolve_imports(result)
        if not COMPILER_READY:
            return
        links = resolved(analyze(files, "typescript", compiled=True))
        for specifier, target in (
            ("./a", "src/a.ts"),
            ("./dir", "src/dir/index.ts"),
            ("./j.js", "src/j.ts"),
            ("../lib/b", "lib/b.tsx"),
        ):
            with self.subTest(specifier=specifier):
                self.assertEqual(
                    links[("src/main.ts", specifier)],
                    ("linked", ((target, TYPESCRIPT_COMPILER_MODULE),), None),
                )
        for specifier in ("pkg", "../../outside"):
            self.assertEqual(
                links[("src/main.ts", specifier)],
                ("unresolved", (), "compiler_module_not_found"),
            )

    def test_default_aliased_and_re_exported_names_keep_their_bindings(self):
        result = analyze(
            {
                "main.ts": (
                    "import d, { a as b, c } from './x';\n"
                    "export { q as r } from './w';\n"
                )
            },
            "typescript",
        )
        self.assertEqual(
            [(i.names, i.local_names) for i in result.imports],
            [(("default", "a", "c"), ("d", "b", "c")), (("q",), ())],
        )


@unittest.skipUnless(TYPESCRIPT_READY, "Provision the typescript extra")
class ScriptPathAliasTests(unittest.TestCase):
    def test_config_paths_and_package_entries_are_configuration_facts(self):
        files = {
            "tsconfig.json": (
                "{\n  // baseUrl and paths, as tsc reads them\n"
                '  "compilerOptions": {"baseUrl": "src", "paths": {\n'
                '    "@app/*": ["app/*"], "@app/deep/*": ["deep/*"],\n'
                '    "@lib": ["lib/main"], "@l*": ["nowhere/*"],\n'
                '    "@pkg/*": ["missing/*", "packages/*"],\n'
                '    "@out/*": ["../../*"],\n'
                "  }}\n}\n"
            ),
            "src/main.ts": (
                "import { a } from '@app/a';\n"
                "import { y } from '@app/deep/y';\n"
                "import { m } from '@lib';\n"
                "import { c } from '@pkg/core';\n"
                "import { t } from '@pkg/typed';\n"
                "import { n } from '@app/none';\n"
                "import { o } from '@out/x';\n"
                "import * as react from 'react';\n"
                "import { e } from '@app/a.ts';\n"
            ),
            "src/app/a.ts": "import { a } from '@app/a';\n",
            "src/app/deep/y.ts": "",
            "src/deep/y.ts": "",
            "src/lib/main.ts": "",
            "src/packages/core/package.json": (
                '{"types": "dist/index.d.ts", "main": "dist/index.js"}'
            ),
            "src/packages/core/index.ts": "",
            "src/packages/typed/package.json": '{"typings": "lib/api.ts"}',
            "src/packages/typed/lib/api.ts": "",
            "src/packages/typed/index.ts": "",
            "sub/tsconfig.json": "{}",
            "sub/main.ts": "import { a } from '@app/a';\n",
            "ext/tsconfig.json": '{"extends": "../tsconfig.json"}',
            "ext/main.ts": "import { a } from '@app/a';\n",
            "ext2/tsconfig.json": (
                '{"extends": "../tsconfig.json", '
                '"compilerOptions": {"paths": {"@app/*": ["*"]}}}'
            ),
            "ext2/main.ts": "import { a } from '@app/a';\n",
            "ext2/a.ts": "",
            "bad/tsconfig.json": "{",
            "bad/main.ts": "import { a } from '@app/a';\n",
            "star/tsconfig.json": '{"compilerOptions": {"paths": {"*": ["*"]}}}',
            "star/main.ts": "import { x } from '../../x';\n",
        }
        result = analyze(files, "typescript")
        aliases = {
            i.specifier: i.names
            for i in result.imports
            if i.kind == "path_alias" and i.file == "tsconfig.json"
        }
        self.assertEqual(aliases["@app/*"], ("src/app/*",))
        self.assertEqual(aliases["@out/*"], ())
        coverage = {c.file: (c.language, c.status) for c in result.coverage}
        self.assertEqual(coverage["tsconfig.json"], ("typescript", "analyzed"))
        self.assertEqual(coverage["bad/tsconfig.json"], ("typescript", "partial"))
        self.assertEqual(
            coverage["src/packages/core/package.json"], ("typescript", "analyzed")
        )
        if not COMPILER_READY:
            return
        # The compiler reads the configs itself; their facts link nothing.
        compiled = analyze(files, "typescript", compiled=True)
        configs = {i.id for i in compiled.imports if i.file.endswith(".json")}
        self.assertEqual(
            {r.reason for r in resolve_imports(compiled) if r.import_id in configs},
            {"resolution_configuration"},
        )
        self.assertFalse(
            configs & {r.import_id for r in compiled.compiler_modules.resolutions}
        )


class ImportSchemaTests(unittest.TestCase):
    def base(self, **changes):
        values = dict(
            repository_id="r",
            snapshot_id="s",
            status="succeeded",
            definitions=(),
            coverage=(deepthought.models.FileCoverage("a.py", "python", "analyzed"),),
            diagnostics=(),
            analyzer="tree-sitter",
            analyzer_versions=(),
        )
        values.update(changes)
        return AnalysisResult(**values)

    def test_schema_three_round_trips_and_schema_two_stays_byte_identical(self):
        item = Import("i1", "a.py", "os", "import", 1, 0, 1, 9)
        current = self.base(
            schema_version="3",
            capabilities=("call_sites", "definitions", "imports", "supertypes"),
            imports=(item,),
        )
        self.assertEqual(AnalysisResult.from_json(current.to_json()), current)
        legacy = self.base()
        self.assertEqual(legacy.schema_version, "2")
        self.assertNotIn("imports", legacy.to_dict())
        self.assertEqual(
            AnalysisResult.from_json(legacy.to_json()).to_json(), legacy.to_json()
        )

    def test_invalid_import_facts_are_rejected(self):
        item = Import("i1", "a.py", "os", "import", 1, 0, 1, 9)
        with self.assertRaisesRegex(ValueError, "capability"):
            self.base(schema_version="3", imports=(item,))
        with self.assertRaisesRegex(ValueError, "Schema 2"):
            self.base(
                capabilities=("call_sites", "definitions", "imports", "supertypes")
            )
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.base(
                schema_version="3",
                capabilities=("call_sites", "definitions", "imports", "supertypes"),
                imports=(item, item),
            )
        with self.assertRaisesRegex(ValueError, "coverage"):
            self.base(
                schema_version="3",
                capabilities=("call_sites", "definitions", "imports", "supertypes"),
                imports=(Import("i2", "b.py", "os", "import", 1, 0, 1, 9),),
            )
        for bad in (
            dict(kind="load"),
            dict(specifier=""),
            dict(names=["x"]),
            dict(names=("",)),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Import(
                    **{
                        **dict(
                            id="i",
                            file="a.py",
                            specifier="os",
                            kind="import",
                            line=1,
                            column=0,
                            end_line=1,
                            end_column=9,
                        ),
                        **bad,
                    }
                )
        data = self.base().to_dict()
        data["imports"] = []
        with self.assertRaisesRegex(ValueError, "Schema 2"):
            AnalysisResult.from_json(json.dumps(data))


@unittest.skipUnless(
    PYTHON_READY and TYPESCRIPT_READY and SCIP_READY,
    "Provision the python, typescript and scip extras",
)
class ImportDifferentialTests(unittest.TestCase):
    def test_tree_sitter_import_edges_match_scip_witnessed_file_dependencies(self):
        """Independent check: compiler-backed SCIP indexes of the same fixtures."""
        from deepthought.analysis_tools import TreeSitterTool

        for language in ("python", "typescript"):
            with self.subTest(language=language):
                output = asyncio.run(
                    TreeSitterTool(FIXTURES / language).analyze(
                        "r", languages=[language]
                    )
                )
                observed = {
                    (
                        r.source_match["qualified_name"].split("::")[1],
                        r.target_match["qualified_name"].split("::")[1],
                    )
                    for r in output.relationships
                    if r.type == "IMPORTS"
                }
                scip = deepthought.ingest_scip(
                    FIXTURES / "scip" / f"{language}.scip", repository_id=language
                )
                expected = {
                    (edge.source_file, edge.target_file)
                    for edge in deepthought.compose_graph([scip]).edges
                }
                self.assertEqual(len(expected), 3)
                self.assertEqual(observed, expected)

    def test_same_source_gives_identical_import_facts_and_resolutions(self):
        for language in ("python", "typescript"):
            with self.subTest(language=language):
                first, second = (
                    with_compiler_modules(
                        analyze_repository(FIXTURES / language, repository_id="r"),
                        FIXTURES / language,
                    )
                    for _ in range(2)
                )
                self.assertEqual(first.to_json(), second.to_json())
                self.assertEqual(resolve_imports(first), resolve_imports(second))


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_NEO4J_URI")
    and importlib.util.find_spec("neo4j")
    and PYTHON_READY
    and TYPESCRIPT_READY,
    "Explicit isolated Neo4j test settings, neo4j, python and typescript extras",
)
class ImportNeo4jIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from deepthought.storage.graph import (
            Neo4jConnectionManager,
            WriteCoordinator,
            setup_schema,
        )

        self.repo = "https://github.com/deepthought-integration/" + uuid4().hex
        self.manager = Neo4jConnectionManager(
            os.environ["DEEPTHOUGHT_NEO4J_URI"],
            os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
            os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
            database=os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j"),
        )
        self.driver = await self.manager.connect()
        self.database = self.manager.database
        await setup_schema(self.driver, database=self.database)
        self.coordinator = WriteCoordinator(self.driver, database=self.database)

    async def asyncTearDown(self):
        try:
            async with self.driver.session(database=self.database) as session:
                result = await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=self.repo
                )
                await result.consume()
        finally:
            await self.manager.close()

    async def test_file_imports_query_returns_stored_edges_with_evidence(self):
        from deepthought.analysis_tools import TreeSitterTool
        from deepthought.graph_queries import GraphQueryService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "lib").mkdir()
            (root / "main.ts").write_text(
                "import { h } from './lib/helpers';\nimport x from 'external';\n"
            )
            (root / "lib/helpers.ts").write_text("export const h = 1;\n")
            output = await TreeSitterTool(root).analyze(self.repo)
        result = await self.coordinator.write_tool_output(
            "tree-sitter", output, self.repo
        )
        self.assertTrue(result.success, result.errors)
        snapshot = result.snapshot_id
        service = GraphQueryService(self.driver, self.database)
        response = await service.get_file_imports(
            "main.ts", self.repo, snapshot_id=snapshot
        )
        self.assertEqual(len(response["items"]), 1)
        item = response["items"][0]
        self.assertEqual(item["element"]["properties"]["path"], "lib/helpers.ts")
        properties = item["relationship"]["properties"]
        self.assertEqual(properties["evidence_type"], "resolved")
        self.assertEqual(properties["rule_id"], "typescript.compiler-module")
        self.assertEqual(properties["producer"], "typescript@6.0.3")
        self.assertEqual(properties["specifier"], "./lib/helpers")
        self.assertEqual(properties["names"], ["h"])
        async with self.driver.session(database=self.database) as session:
            rows = await session.run(
                "MATCH (f:File {repo_url:$repo,snapshot_id:$snapshot,path:'main.ts'}) "
                "RETURN f.unresolved_imports AS unresolved",
                repo=self.repo,
                snapshot=snapshot,
            )
            row = await rows.single(strict=True)
        self.assertEqual(row["unresolved"], ["external"])


if __name__ == "__main__":
    unittest.main()
