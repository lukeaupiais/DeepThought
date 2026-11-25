import importlib.util
import tempfile
import unittest
from pathlib import Path

from deepthought import indexers


def write(root, files):
    for name, content in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


SERVICE_A = {
    "services/a/pyproject.toml": '[project]\nname = "a"\nversion = "0"\n',
    "services/a/src/alpha/__init__.py": "",
    "services/a/src/alpha/core.py": "def helper():\n    return 1\n",
    "services/a/tests/test_core.py": (
        "from alpha.core import helper\n\n\ndef test_it():\n    helper()\n"
    ),
}


class SourceRootsTest(unittest.TestCase):
    def test_each_projects_src_directory_unless_two_share_a_module(self):
        with tempfile.TemporaryDirectory() as scratch:
            write(
                scratch,
                {
                    **SERVICE_A,
                    "src/tool.py": "",
                    # No project file: not a project, so its src is not a root.
                    "scripts/src/x.py": "",
                },
            )
            self.assertEqual(
                indexers.python_source_roots(scratch), [".", "src", "services/a/src"]
            )
            write(
                scratch,
                {
                    "services/b/requirements.txt": "",
                    "services/b/src/alpha/__init__.py": "",
                },
            )
            self.assertEqual(indexers.python_source_roots(scratch), [".", "src"])

    def test_namespace_package_portions_do_not_clash(self):
        with tempfile.TemporaryDirectory() as scratch:
            write(
                scratch,
                {
                    f"integrations/{name}/pyproject.toml": "[project]\n"
                    for name in ("one", "two")
                }
                | {
                    f"integrations/{name}/src/company/{name}/__init__.py": ""
                    for name in ("one", "two")
                },
            )
            self.assertEqual(
                indexers.python_source_roots(scratch),
                [".", "integrations/one/src", "integrations/two/src"],
            )


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-python"])
    and importlib.util.find_spec("google.protobuf"),
    "Install scip-python and the scip extra",
)
class SourceLayoutIndexTest(unittest.TestCase):
    def test_a_services_tests_bind_to_its_src_layout_package(self):
        from deepthought._scip import scip_pb2

        with tempfile.TemporaryDirectory() as scratch:
            root, out = Path(scratch) / "work", Path(scratch) / "out"
            write(root, SERVICE_A)
            report = indexers.index_workspace(root, out, languages=("python",))
            (run,) = report["runs"]
            self.assertEqual(run["status"], "indexed", run["detail"])
            index = scip_pb2.Index()
            index.ParseFromString(Path(run["index"]).read_bytes())
            symbols = {
                d.relative_path: [o.symbol for o in d.occurrences]
                for d in index.documents
            }
        defined = [s for s in symbols["services/a/src/alpha/core.py"] if "helper" in s]
        self.assertEqual(len(defined), 1)
        self.assertEqual(symbols["services/a/tests/test_core.py"].count(defined[0]), 2)


if __name__ == "__main__":
    unittest.main()
