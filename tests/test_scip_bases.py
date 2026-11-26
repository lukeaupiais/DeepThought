"""Class and interface bases resolved by the SCIP indexers, end to end
through TreeSitterTool."""

import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path

from deepthought import indexers


def write(root, files):
    for path, text in files.items():
        target = Path(root) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def analyze(files, languages):
    from deepthought.analysis_tools.tree_sitter import TreeSitterTool

    with tempfile.TemporaryDirectory() as scratch:
        write(scratch, files)
        return asyncio.run(TreeSitterTool(scratch).analyze("repo", languages=languages))


def bases(output):
    """(class file, base as written, target file, rule) per INHERITS_FROM
    edge."""
    return sorted(
        (
            r.source_match["qualified_name"].split("::")[1],
            r.properties["base"],
            r.target_match["qualified_name"].split("::")[1],
            r.properties["rule_id"],
        )
        for r in output.relationships
        if r.type == "INHERITS_FROM"
    )


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-python"])
    and importlib.util.find_spec("tree_sitter_python"),
    "Install scip-python and the python extra",
)
class PythonBaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.output = analyze(
            {
                "pyproject.toml": "[project]\nname = 'app'\n",
                "app/__init__.py": "",
                "app/base.py": "class Base:\n    pass\n\n\nclass Mixin:\n    pass\n",
                "app/models.py": (
                    "from app import base\n"
                    "from app.base import Base as Root\n\n\n"
                    "class User(Root, base.Mixin):\n    pass\n"
                ),
                # A repository class shares the installed base's name.
                "app/other.py": "class Thread:\n    pass\n",
                "app/worker.py": (
                    "from threading import Thread\n\n\n"
                    "class Worker(Thread):\n    pass\n"
                ),
            },
            {"python"},
        )

    def test_bases_link_to_the_classes_python_binds(self):
        self.assertEqual(
            [b for b in bases(self.output) if b[0] == "app/models.py"],
            [
                ("app/models.py", "Root", "app/base.py", "scip.python-base"),
                ("app/models.py", "base.Mixin", "app/base.py", "scip.python-base"),
            ],
        )

    def test_an_installed_base_links_nothing_whatever_its_name(self):
        self.assertNotIn("app/worker.py", [b[0] for b in bases(self.output)])
        counts = self.output.metadata.custom_metrics["supertype_resolution"]
        self.assertEqual(counts.get("unresolved:scip_external"), 1)

    def test_links_name_the_indexer(self):
        edge = next(r for r in self.output.relationships if r.type == "INHERITS_FROM")
        self.assertTrue(edge.properties["producer"].startswith("scip-python@"))
        self.assertEqual(edge.properties["evidence_type"], "resolved")


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-typescript"])
    and importlib.util.find_spec("tree_sitter_typescript"),
    "Install scip-typescript and the languages extra",
)
class ScriptBaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.output = analyze(
            {
                "package.json": '{"name": "app", "version": "1.0.0"}\n',
                "tsconfig.json": '{"compilerOptions": {"strict": true}}\n',
                "src/base.ts": (
                    "export class Base<T> {\n  value?: T;\n}\n"
                    "export interface Named {\n  name: string;\n}\n"
                ),
                "src/index.ts": "export { Base as Root } from './base';\n",
                "src/user.ts": (
                    "import { Root } from './index';\n"
                    "import * as shapes from './base';\n\n"
                    "export class User extends Root<string> "
                    "implements shapes.Named {\n  name = '';\n}\n"
                ),
            },
            {"typescript"},
        )

    def test_extends_and_implements_link_to_what_the_compiler_binds(self):
        self.assertEqual(
            [b for b in bases(self.output) if b[0] == "src/user.ts"],
            [
                ("src/user.ts", "Root", "src/base.ts", "scip.typescript-base"),
                (
                    "src/user.ts",
                    "shapes.Named",
                    "src/base.ts",
                    "scip.typescript-base",
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
