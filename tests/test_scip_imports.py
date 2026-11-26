"""Python imports resolved by scip-python, end to end through TreeSitterTool."""

import asyncio
import importlib.util
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
            self.output.metadata.custom_metrics["scip_python_imports"],
            {
                "linked": 2,
                "scip_external": 3,
                "scip_no_occurrence": 1,
            },
        )
        self.assertEqual(self.output.metadata.custom_metrics["python_packages"], 1)


if __name__ == "__main__":
    unittest.main()
