import base64
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from deepthought import indexers
from deepthought.python_dependencies import declared, lockfiles, project_directories


def wheel(directory, name, version, files, requires=()):
    """A minimal pure-Python wheel, written by hand so no build runs."""
    dist = f"{name}-{version}.dist-info"
    entries = dict(files)
    entries[f"{dist}/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        + "".join(f"Requires-Dist: {r}\n" for r in requires)
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


class NestedProjectsTest(unittest.TestCase):
    def test_every_service_declares_but_not_hidden_dirs_or_environments(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "requirements.txt").write_text("flask\n")
            for service, requirement in (("api", "attrs"), ("worker", "celery")):
                (root / "services" / service).mkdir(parents=True)
                (root / "services" / service / "requirements.txt").write_text(
                    requirement + "\n"
                )
            (root / "services" / "worker" / "setup.cfg").write_text(
                "[options]\ninstall_requires =\n    redis\n"
            )
            for skipped in (".tox/py", "node_modules/x", "venv/lib"):
                (root / skipped).mkdir(parents=True)
                (root / skipped / "requirements.txt").write_text("unwanted\n")
            (root / "venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
            self.assertEqual(
                project_directories(root),
                [".", "services/api", "services/worker"],
            )
            self.assertEqual(declared(root), ["attrs", "celery", "flask", "redis"])


@unittest.skipUnless(importlib.util.find_spec("tomllib"), "Python 3.11+")
class LockfileTest(unittest.TestCase):
    def declared(self, files):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            for name, text in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text)
            return declared(root), lockfiles(root)

    def test_uv_lock_pins_registry_packages_in_place_of_ranges(self):
        found, locks = self.declared(
            {
                "pyproject.toml": '[project]\nname = "app"\ndependencies = ["fastapi>=0.1"]\n',
                "uv.lock": (
                    'version = 1\n[[package]]\nname = "app"\nversion = "0.1.0"\n'
                    'source = { editable = "." }\n'
                    '[[package]]\nname = "fastapi"\nversion = "0.115.0"\n'
                    'source = { registry = "https://pypi.org/simple" }\n'
                    '[[package]]\nname = "starlette"\nversion = "0.38.6"\n'
                    'source = { registry = "https://pypi.org/simple" }\n'
                    '[[package]]\nname = "tool"\nversion = "1.0"\n'
                    'source = { git = "https://example.com/tool.git" }\n'
                ),
            }
        )
        self.assertEqual(found, ["fastapi==0.115.0", "starlette==0.38.6"])
        self.assertEqual(locks, ["uv.lock"])

    def test_poetry_lock_keeps_markers_and_private_indexes(self):
        found, locks = self.declared(
            {
                "svc/pyproject.toml": '[tool.poetry.dependencies]\nflask = "^2"\n',
                "svc/poetry.lock": (
                    '[[package]]\nname = "flask"\nversion = "2.3.3"\n'
                    '[[package]]\nname = "colorama"\nversion = "0.4.6"\n'
                    "markers = \"platform_system == 'Windows'\"\n"
                    '[[package]]\nname = "internal"\nversion = "3.0"\n'
                    '[package.source]\ntype = "legacy"\nurl = "https://pypi.example"\n'
                    'reference = "corp"\n'
                    '[[package]]\nname = "local"\nversion = "0.1"\n'
                    '[package.source]\ntype = "directory"\nurl = "../local"\n'
                ),
            }
        )
        self.assertEqual(
            found,
            [
                "colorama==0.4.6 ; platform_system == 'Windows'",
                "flask==2.3.3",
                "internal==3.0",
            ],
        )
        self.assertEqual(locks, ["svc/poetry.lock"])

    def test_pipfile_lock_and_pipfile(self):
        found, locks = self.declared(
            {
                "a/Pipfile.lock": json.dumps(
                    {
                        "default": {
                            "requests": {"version": "==2.32.3"},
                            "pywin32": {
                                "version": "==306",
                                "markers": "sys_platform == 'win32'",
                            },
                            "mine": {"path": ".", "editable": True},
                        },
                        "develop": {"pytest": {"version": "==8.3.3"}},
                    }
                ),
                "b/Pipfile": (
                    '[packages]\ndjango = ">=4.2,<5"\nrequests = "*"\n'
                    'celery = {version = "==5.3.0", extras = ["redis"]}\n'
                    'lib = {git = "https://example.com/lib.git"}\n'
                    '[dev-packages]\nblack = "*"\n'
                ),
            }
        )
        self.assertEqual(
            found,
            [
                "black",
                "celery[redis]==5.3.0",
                "django>=4.2,<5",
                "pytest==8.3.3",
                "pywin32==306 ; sys_platform == 'win32'",
                "requests",
                "requests==2.32.3",
            ],
        )
        self.assertEqual(locks, ["a/Pipfile.lock"])


class PoetryTest(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("tomllib"), "Python 3.11+")
    def test_poetry_and_dependency_groups_become_pep_508(self):
        with tempfile.TemporaryDirectory() as scratch:
            (Path(scratch) / "pyproject.toml").write_text(
                "[tool.poetry.dependencies]\n"
                'python = "^3.10"\n'
                'flask = "^2.3.1"\n'
                'pydantic = "^0.10"\n'
                'celery = { version = "~5.3", extras = ["redis"] }\n'
                'local = { path = "../local" }\n'
                'tool = { git = "https://example.com/tool.git" }\n'
                "[tool.poetry.group.test.dependencies]\n"
                'pytest = "*"\n'
                "[dependency-groups]\n"
                'lint = ["ruff==0.15.18", { include-group = "test" }]\n'
            )
            found = declared(scratch)
        self.assertEqual(
            found,
            [
                "celery[redis]>=5.3,<5.4",
                "flask>=2.3.1,<3",
                "pydantic>=0.10,<0.11",
                "pytest",
                "ruff==0.15.18",
            ],
        )


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

    def test_a_wheel_whose_dependency_has_none_still_types_calls(self):
        # Like instana, whose wheel needs fysom, which has no wheel.
        wheel(
            self.wheels,
            "needy",
            "1.0",
            {
                "needy/__init__.py": "class Probe:\n    def ping(self):\n        return 1\n"
            },
            requires=["nowheel"],
        )
        (self.root / "requirements.txt").write_text("tinylib==1.0\nneedy==1.0\n")
        (self.root / "probe.py").write_text(
            "from needy import Probe\n\n\ndef run():\n    return Probe().ping()\n"
        )
        report, run = self.index()
        self.assertEqual(run["status"], "indexed", run["detail"])
        self.assertEqual(run["dependencies"]["status"], "partial")
        self.assertEqual(run["dependencies"]["failed"], [])
        self.assertEqual(run["dependencies"]["without_dependencies"], ["needy==1.0"])
        self.assertEqual(
            run["dependencies"]["packages"], ["needy==1.0", "tinylib==1.0"]
        )
        self.assertEqual(
            report["coverage"]["probe.py"]["warning"], "dependencies_unavailable"
        )
        from deepthought._scip import scip_pb2

        index = scip_pb2.Index()
        index.ParseFromString(Path(run["index"]).read_bytes())
        symbols = {o.symbol for d in index.documents for o in d.occurrences}
        self.assertTrue(
            any("needy" in s and s.endswith("Probe#ping().") for s in symbols),
            sorted(s for s in symbols if "needy" in s),
        )

    def test_a_service_below_the_root_gets_its_declared_packages(self):
        service = self.root / "services" / "api"
        service.mkdir(parents=True)
        (self.root / "main.py").rename(service / "main.py")
        (service / "requirements.txt").write_text("tinylib==1.0\n")
        report, run = self.index()
        self.assertEqual(run["status"], "indexed", run["detail"])
        self.assertEqual(run["dependencies"]["projects"], ["services/api"])
        self.assertEqual(run["dependencies"]["packages"], ["tinylib==1.0"])
        self.assertTrue(
            any(s.endswith("Thing#go().") for s in self.external_symbols(run)),
            self.external_symbols(run),
        )

    def test_undeclared_libraries_stay_unresolved(self):
        # Nothing declared, so nothing is installed for the indexer.
        report, run = self.index()
        self.assertEqual(run["dependencies"]["status"], "none")
        self.assertFalse(
            any(s.endswith("Thing#go().") for s in self.external_symbols(run))
        )


if __name__ == "__main__":
    unittest.main()
