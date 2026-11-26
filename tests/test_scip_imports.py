"""Python imports resolved by scip-python, end to end through TreeSitterTool."""

import asyncio
import importlib.util
import shutil
from pathlib import Path
import tempfile
import unittest

from deepthought import indexers


def write(root, files):
    for path, text in files.items():
        target = Path(root) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-python"])
    and importlib.util.find_spec("tree_sitter_python"),
    "Install scip-python and the python extra",
)
class PythonImportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from deepthought.analysis_tools.tree_sitter import TreeSitterTool

        # The FastAPI template's layout: the project is backend/, so its
        # package `app` imports as `app`, not `backend.app`.
        with tempfile.TemporaryDirectory() as scratch:
            write(
                scratch,
                {
                    "backend/pyproject.toml": "[project]\nname = 'app'\n",
                    "backend/app/__init__.py": "",
                    "backend/app/models.py": (
                        "import os\n"
                        "from dataclasses import dataclass\n\n\n"
                        "@dataclass\nclass User:\n    name: str = os.sep\n"
                    ),
                    "backend/app/api/__init__.py": "",
                    "backend/app/api/deps.py": "def get():\n    return 1\n",
                    "backend/app/api/routes.py": (
                        "import json\n"
                        "from app.models import User\n"
                        "from app import models\n"
                        "from .deps import get\n"
                        "import not_installed_anywhere\n\n\n"
                        "def run():\n"
                        "    return json.dumps(get()), User, models\n"
                    ),
                },
            )
            cls.output = asyncio.run(
                TreeSitterTool(scratch).analyze("repo", languages={"python"})
            )

    def edges(self, kind, source):
        return {
            (
                r.properties["specifier"],
                r.target_match["qualified_name"],
                r.properties["rule_id"],
            )
            for r in self.output.relationships
            if r.type == kind
            and r.source_match["qualified_name"] == f"repo::{source}::file"
        }

    def file(self, path):
        return next(
            n.properties
            for n in self.output.nodes
            if "File" in n.labels and n.properties["path"] == path
        )

    def test_project_imports_link_to_the_files_python_would_load(self):
        self.assertEqual(
            self.edges("IMPORTS", "backend/app/api/routes.py"),
            {
                (
                    "app.models",
                    "repo::backend/app/models.py::file",
                    "scip.python-import-module",
                ),
                (
                    "app",
                    "repo::backend/app/__init__.py::file",
                    "scip.python-import-module",
                ),
                (
                    "app",
                    "repo::backend/app/models.py::file",
                    "scip.python-import-module",
                ),
                # Relative imports keep the language's own path rule.
                (
                    ".deps",
                    "repo::backend/app/api/deps.py::file",
                    "tree-sitter.python-relative-module",
                ),
            },
        )

    def test_external_modules_resolve_to_their_package(self):
        stdlib = {
            n.properties["qualified_name"]: n.properties
            for n in self.output.nodes
            if "ExternalPackage" in n.labels
        }
        ((name, package),) = stdlib.items()
        self.assertEqual(package["name"], "python-stdlib")
        self.assertEqual(package["ecosystem"], "python")
        self.assertTrue(name.startswith("repo::python-package::python-stdlib::"))
        imported = {
            r.properties["specifier"]: r
            for r in self.output.relationships
            if r.type == "IMPORTS_PACKAGE"
        }
        self.assertEqual(set(imported), {"json", "os", "dataclasses"})
        self.assertEqual(imported["json"].properties["modules"], ["json"])
        self.assertEqual(
            imported["json"].properties["rule_id"], "scip.python-import-package"
        )
        self.assertEqual(
            self.file("backend/app/api/routes.py")["external_imports"], ["json"]
        )

    def test_a_module_found_nowhere_stays_unresolved(self):
        routes = self.file("backend/app/api/routes.py")
        self.assertEqual(routes["unresolved_imports"], ["not_installed_anywhere"])
        self.assertEqual(
            self.output.metadata.custom_metrics["scip_imports"],
            {
                "python": {
                    "linked": 2,
                    "scip_external": 3,
                    "scip_no_occurrence": 1,
                }
            },
        )
        self.assertEqual(self.output.metadata.custom_metrics["external_packages"], 1)


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-python"])
    and importlib.util.find_spec("tree_sitter_python"),
    "Install scip-python and the python extra",
)
class ShadowedPackageTest(unittest.TestCase):
    def test_the_repositorys_package_wins_over_an_installed_one_of_its_name(self):
        """A wheel that ships a top-level ``tests`` package (msal-extensions
        does) must not take the repository's own ``tests`` from it."""
        import os
        import subprocess

        from deepthought.analysis_tools.tree_sitter import TreeSitterTool
        from deepthought.scip import ingest_scip

        with tempfile.TemporaryDirectory() as scratch:
            root, site = Path(scratch) / "repo", Path(scratch) / "site"
            write(
                root,
                {
                    "pyproject.toml": "[project]\nname = 'r'\n",
                    "tests/__init__.py": "def authenticated_user():\n    pass\n",
                    "tests/handlers/__init__.py": "",
                    "tests/handlers/test_a.py": (
                        "from tests import authenticated_user\n"
                    ),
                },
            )
            info = "shadow-1.0.dist-info"
            write(
                site,
                {
                    "tests/__init__.py": "",
                    f"{info}/METADATA": (
                        "Metadata-Version: 2.1\nName: shadow\nVersion: 1.0\n"
                    ),
                    f"{info}/RECORD": f"tests/__init__.py,,\n{info}/METADATA,,\n",
                },
            )
            output = Path(scratch) / "index.scip"
            subprocess.run(
                [
                    indexers.locate(indexers.INDEXERS["scip-python"]),
                    "index",
                    "--cwd",
                    str(root),
                    "--output",
                    str(output),
                    "--project-name",
                    "r",
                    "--project-version",
                    "0",
                    "--quiet",
                ],
                cwd=root,
                env={**os.environ, "PYTHONPATH": f"{root}{os.pathsep}{site}"},
                check=True,
                capture_output=True,
            )
            index = ingest_scip(output, repository_id="repo")
            result = asyncio.run(
                TreeSitterTool(root).analyze(
                    "repo", languages={"python"}, scip_indexes=[(index, "r")]
                )
            )
        (edge,) = [r for r in result.relationships if r.type == "IMPORTS"]
        self.assertEqual(
            edge.target_match["qualified_name"], "repo::tests/__init__.py::file"
        )
        self.assertFalse(
            [r for r in result.relationships if r.type == "IMPORTS_PACKAGE"]
        )


def _imports(output, source):
    return sorted(
        (
            r.type,
            r.properties["specifier"],
            r.target_match["qualified_name"],
            r.properties["rule_id"],
        )
        for r in output.relationships
        if r.type in {"IMPORTS", "IMPORTS_PACKAGE"}
        and r.source_match["qualified_name"] == f"repo::{source}::file"
    )


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-typescript"])
    and importlib.util.find_spec("tree_sitter_typescript"),
    "Install scip-typescript and the typescript extra",
)
class ScriptImportTest(unittest.TestCase):
    def test_packages_files_and_ambient_modules(self):
        from deepthought.analysis_tools.tree_sitter import TreeSitterTool

        def package(name, version, declaration):
            return {
                f"node_modules/{name}/package.json": (
                    f'{{"name": "{name}", "version": "{version}", '
                    '"types": "index.d.ts"}'
                ),
                f"node_modules/{name}/index.d.ts": declaration,
            }

        with tempfile.TemporaryDirectory() as scratch:
            write(
                scratch,
                {
                    "package.json": '{"name": "w", "version": "1.0.0"}',
                    "tsconfig.json": (
                        '{"compilerOptions": {"strict": true, "module": "esnext",'
                        ' "moduleResolution": "bundler", "types": ["fakenode"]},'
                        ' "include": ["src"]}'
                    ),
                    **package(
                        "left-pad",
                        "1.3.0",
                        "export default function leftPad(s: string): string;",
                    ),
                    **package(
                        "@scope/util", "2.0.1", "export declare function u(): number;"
                    ),
                    **package(
                        "@types/fakenode",
                        "3.0.0",
                        'declare module "fake:buffer" { export const B: number; }',
                    ),
                    "src/a.ts": "export const A = 1;\n",
                    "src/main.ts": (
                        'import leftPad from "left-pad";\n'
                        'import { u } from "@scope/util";\n'
                        'import { A } from "./a";\n'
                        'import { B } from "fake:buffer";\n'
                        'import * as missing from "not-there";\n'
                        'import "./style.css";\n'
                        'export const x = [leftPad("a"), u(), A, B, missing];\n'
                    ),
                },
            )
            output = asyncio.run(
                TreeSitterTool(scratch).analyze("repo", languages={"typescript"})
            )
        package_rule = "scip.typescript-import-package"
        self.assertEqual(
            _imports(output, "src/main.ts"),
            [
                (
                    "IMPORTS",
                    "./a",
                    "repo::src/a.ts::file",
                    "scip.typescript-import-module",
                ),
                (
                    "IMPORTS_PACKAGE",
                    "@scope/util",
                    "repo::npm-package::@scope/util::2.0.1",
                    package_rule,
                ),
                # The ambient module belongs to the package declaring it.
                (
                    "IMPORTS_PACKAGE",
                    "fake:buffer",
                    "repo::npm-package::@types/fakenode::3.0.0",
                    package_rule,
                ),
                (
                    "IMPORTS_PACKAGE",
                    "left-pad",
                    "repo::npm-package::left-pad::1.3.0",
                    package_rule,
                ),
            ],
        )
        main = next(
            n.properties
            for n in output.nodes
            if n.properties.get("path") == "src/main.ts"
        )
        # Nothing resolves either: no module, and a stylesheet no rule reads.
        self.assertEqual(main["unresolved_imports"], ["./style.css", "not-there"])
        self.assertEqual(
            main["external_imports"], ["@scope/util", "fake:buffer", "left-pad"]
        )
        self.assertEqual(
            output.metadata.custom_metrics["scip_imports"]["typescript"],
            {"linked": 1, "scip_external": 3, "scip_no_occurrence": 2},
        )


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-go"])
    and shutil.which("go")
    and importlib.util.find_spec("tree_sitter_go"),
    "Install scip-go, Go and the go extra",
)
class GoImportTest(unittest.TestCase):
    def test_module_packages_link_and_others_name_their_module(self):
        from deepthought.analysis_tools.tree_sitter import TreeSitterTool

        with tempfile.TemporaryDirectory() as scratch:
            write(
                scratch,
                {
                    "go.mod": "module example.com/w\n\ngo 1.21\n",
                    "inner/inner.go": "package inner\n\nfunc F() int { return 1 }\n",
                    "inner/g.go": "package inner\n\nfunc G() int { return 2 }\n",
                    # Built only on Windows: not what the import loads here.
                    "inner/w.go": (
                        "//go:build windows\n\npackage inner\n\n"
                        "func W() int { return 3 }\n"
                    ),
                    "inner/g_test.go": (
                        'package inner\n\nimport "testing"\n\n'
                        "func TestG(t *testing.T) {}\n"
                    ),
                    "main.go": (
                        'package main\n\nimport (\n\t"fmt"\n\tstrs "strings"\n'
                        '\t"example.com/w/inner"\n)\n\n'
                        'func main() { fmt.Println(strs.ToUpper("a"), inner.F()) }\n'
                    ),
                },
            )
            output = asyncio.run(
                TreeSitterTool(scratch).analyze("repo", languages={"go"})
            )
        found = _imports(output, "main.go")
        self.assertEqual(
            [f[:3] for f in found if f[0] == "IMPORTS"],
            [
                ("IMPORTS", "example.com/w/inner", "repo::inner/g.go::file"),
                ("IMPORTS", "example.com/w/inner", "repo::inner/inner.go::file"),
            ],
        )
        self.assertEqual(
            {f[3] for f in found if f[0] == "IMPORTS"}, {"scip.go-import-module"}
        )
        packages = {
            n.properties["name"]: n.properties
            for n in output.nodes
            if "ExternalPackage" in n.labels
        }
        # The standard library is the Go repository's src module.
        (std,) = packages.values()
        self.assertEqual(std["name"], "github.com/golang/go/src")
        self.assertEqual(std["ecosystem"], "go")
        modules = {
            r.properties["specifier"]: r.properties["modules"]
            for r in output.relationships
            if r.type == "IMPORTS_PACKAGE"
        }
        self.assertEqual(
            modules,
            {"fmt": ["fmt"], "strings": ["strings"], "testing": ["testing"]},
        )

    def test_versions_are_the_ones_go_mod_requires(self):
        from deepthought.scip_imports import _GoModules

        with tempfile.TemporaryDirectory() as scratch:
            write(
                scratch,
                {
                    "go.mod": (
                        "module example.com/w\n\ngo 1.21\n\nrequire (\n"
                        "\tgithub.com/go-playground/validator v9.31.0+incompatible\n"
                        "\tgithub.com/heptiolabs/healthcheck "
                        "v0.0.0-20180807145615-6ff867650f40\n"
                        "\texample.com/old v1.0.0\n)\n\n"
                        "replace example.com/old => example.com/new v1.2.0\n"
                    ),
                    "main.go": "package main\n\nfunc main() {}\n",
                },
            )
            versions, files = _GoModules(scratch).module("main.go")
        # scip-go writes v9.31.0 and 6ff867650f40 for these.
        self.assertEqual(
            versions,
            {
                "github.com/go-playground/validator": "v9.31.0+incompatible",
                "github.com/heptiolabs/healthcheck": (
                    "v0.0.0-20180807145615-6ff867650f40"
                ),
                "example.com/old": "v1.2.0",
            },
        )
        self.assertEqual(files, {".": {"main.go"}})


if __name__ == "__main__":
    unittest.main()
