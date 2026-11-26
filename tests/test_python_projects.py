import importlib.util
import tempfile
import unittest
from pathlib import Path

from deepthought import indexers


def write(root, files):
    for name, text in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


# Two services that each import their own `schemas` by bare name, from a
# subdirectory (joe0106/fastapi-microservices-orchestrator's layout).
SERVICES = {
    f"src/{service}/{name}": text
    for service in ("OrderService", "ShippingService")
    for name, text in {
        "pyproject.toml": f'[project]\nname = "{service.lower()}"\n',
        "schemas.py": f"class {service}Schema:\n    pass\n",
        "routers/api.py": (
            f"from schemas import {service}Schema\n\n"
            f"def make():\n    return {service}Schema()\n"
        ),
    }.items()
}


class PythonProjectsTest(unittest.TestCase):
    def test_a_project_importing_its_own_modules_gets_a_run(self):
        with tempfile.TemporaryDirectory() as scratch:
            write(
                scratch,
                {
                    **SERVICES,
                    # A fixture project that never imports itself.
                    "tests/fixtures/sample/pyproject.toml": "[project]\n",
                    "tests/fixtures/sample/sample/__init__.py": "X = 1\n",
                },
            )
            self.assertEqual(
                indexers.python_projects(scratch),
                ["src/OrderService", "src/ShippingService"],
            )

    def test_a_nested_run_keeps_the_workspace_roots_after_its_own(self):
        with tempfile.TemporaryDirectory() as scratch:
            write(
                scratch,
                {
                    "pyproject.toml": "[project]\n",
                    "src/lib/__init__.py": "",
                    "examples/app/pyproject.toml": "[project]\n",
                    "examples/app/src/app/__init__.py": "",
                },
            )
            root = Path(scratch)
            self.assertEqual(
                indexers.python_path(root, root / "examples/app"),
                ["examples/app", "examples/app/src", ".", "src"],
            )
            # The workspace's own run already has every project's src.
            self.assertEqual(
                indexers.python_path(root, root), [".", "src", "examples/app/src"]
            )


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-python"])
    and importlib.util.find_spec("google.protobuf"),
    "Install scip-python and the scip extra",
)
class ServiceIndexTest(unittest.TestCase):
    def test_each_service_resolves_its_own_bare_imports(self):
        from deepthought._scip import scip_pb2

        with tempfile.TemporaryDirectory() as scratch:
            root, out = Path(scratch) / "work", Path(scratch) / "out"
            write(root, SERVICES)
            report = indexers.index_workspace(root, out, languages=("python",))
            self.assertEqual(indexers.failed_files(report), {})
            runs = {r.get("module"): r for r in report["runs"]}
            self.assertEqual(
                sorted(m for m in runs if m),
                ["src/OrderService", "src/ShippingService"],
            )
            for service in ("OrderService", "ShippingService"):
                run = runs[f"src/{service}"]
                index = scip_pb2.Index()
                index.ParseFromString(Path(run["index"]).read_bytes())
                documents = {d.relative_path: d for d in index.documents}
                defined = {
                    o.symbol
                    for o in documents[f"src/{service}/schemas.py"].occurrences
                    if o.symbol_roles & 1 and f"{service}Schema" in o.symbol
                }
                used = {
                    o.symbol
                    for o in documents[f"src/{service}/routers/api.py"].occurrences
                }
                self.assertTrue(defined and defined <= used, (service, used))


if __name__ == "__main__":
    unittest.main()
