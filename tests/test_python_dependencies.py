import base64
import hashlib
import importlib.util
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from deepthought import indexers
from deepthought.python_dependencies import declared


def wheel(directory, name, version, files):
    """A minimal pure-Python wheel, written by hand so no build runs."""
    dist = f"{name}-{version}.dist-info"
    entries = dict(files)
    entries[f"{dist}/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    )
    entries[f"{dist}/WHEEL"] = (
        "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    )
    record = []
    for path, content in entries.items():
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(content.encode()).digest()
        ).rstrip(b"=")
        record.append(f"{path},sha256={digest.decode()},{len(content.encode())}")
    record.append(f"{dist}/RECORD,,")
    entries[f"{dist}/RECORD"] = "\n".join(record) + "\n"
    target = Path(directory) / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(target, "w") as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    return target


class DeclaredTest(unittest.TestCase):
    def test_reads_requirements_pyproject_and_setup_cfg_but_not_paths(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "requirements.txt").write_text(
                "# pinned\nflask==3.0.0\n-r requirements-dev.txt\n"
                "-e .\n./vendor/pkg\ngit+https://example.com/x.git\n"
                "local @ file:///tmp/x\n"
            )
            (root / "requirements-dev.txt").write_text(
                "pytest>=8 ; python_version>'3'\n"
            )
            (root / "setup.cfg").write_text(
                "[options]\ninstall_requires =\n    attrs\n"
                "[options.extras_require]\nfast = orjson\n"
            )
            (root / "pyproject.toml").write_text(
                '[project]\nname = "app"\ndependencies = ["sqlalchemy[asyncio]>=2"]\n'
                '[project.optional-dependencies]\ndb = ["psycopg"]\n'
            )
            found = declared(root)
        expected = ["attrs", "flask==3.0.0", "orjson", "pytest>=8 ; python_version>'3'"]
        if importlib.util.find_spec("tomllib"):
            expected += ["psycopg", "sqlalchemy[asyncio]>=2"]
        self.assertEqual(found, sorted(expected))


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-python"])
    and importlib.util.find_spec("google.protobuf"),
    "Install scip-python and the scip extra",
)
class InstalledDependenciesTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        base = Path(self.scratch.name)
        self.root, self.wheels, self.out = base / "work", base / "wheels", base / "out"
        for d in (self.root, self.wheels, self.out):
            d.mkdir()
        wheel(
            self.wheels,
            "tinylib",
            "1.0",
            {
                "tinylib/__init__.py": "class Thing:\n    def go(self):\n        return 1\n"
            },
        )
        (self.root / "main.py").write_text(
            "from tinylib import Thing\n\n\ndef run():\n    t = Thing()\n    return t.go()\n"
        )
        # Offline: pip reads its index settings from the environment.
        self.environ = {
            **os.environ,
            "PIP_NO_INDEX": "1",
            "PIP_FIND_LINKS": str(self.wheels),
        }

    def index(self):
        report = indexers.index_workspace(
            self.root, self.out, languages=("python",), environ=self.environ
        )
        (run,) = report["runs"]
        return report, run

    def external_symbols(self, run):
        from deepthought._scip import scip_pb2

        index = scip_pb2.Index()
        index.ParseFromString(Path(run["index"]).read_bytes())
        return {
            o.symbol
            for d in index.documents
            for o in d.occurrences
            if "tinylib" in o.symbol
        }

    def test_declared_wheels_type_library_calls(self):
        (self.root / "requirements.txt").write_text("tinylib==1.0\n")
        report, run = self.index()
        self.assertEqual(run["status"], "indexed", run["detail"])
        self.assertEqual(run["dependencies"]["status"], "installed")
        self.assertEqual(run["dependencies"]["packages"], ["tinylib==1.0"])
        self.assertNotIn("warning", report["coverage"]["main.py"])
        # The method call on the instance names the library's method.
        self.assertTrue(
            any(s.endswith("Thing#go().") for s in self.external_symbols(run)),
            self.external_symbols(run),
        )
        receipt = Path(run["index"] + ".sources.json").read_text()
        self.assertIn("tinylib==1.0", receipt)

    def test_a_requirement_without_a_wheel_is_named_on_each_file(self):
        (self.root / "requirements.txt").write_text("tinylib==1.0\nnowheel==2.0\n")
        report, run = self.index()
        self.assertEqual(run["status"], "indexed", run["detail"])
        self.assertEqual(run["dependencies"]["status"], "partial")
        self.assertEqual(run["dependencies"]["failed"], ["nowheel==2.0"])
        self.assertEqual(
            report["coverage"]["main.py"]["warning"], "dependencies_unavailable"
        )
        self.assertEqual(indexers.failed_files(report), {})

    def test_undeclared_libraries_stay_unresolved(self):
        # Nothing declared, so nothing is installed for the indexer.
        report, run = self.index()
        self.assertEqual(run["dependencies"]["status"], "none")
        self.assertFalse(
            any(s.endswith("Thing#go().") for s in self.external_symbols(run))
        )


if __name__ == "__main__":
    unittest.main()
