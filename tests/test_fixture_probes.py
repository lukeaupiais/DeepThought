import importlib.util
import tempfile
import unittest
from pathlib import Path

from deepthought import fixture_probes, indexers

CONFTEST = """\
import pytest

from shop.service import Service


@pytest.fixture
def service():
    return Service()


@pytest.fixture(name="store")
def make_store(service):
    yield service.store()


@pytest.fixture
async def session():
    return Service()
"""

SERVICE = """\
class Store:
    def count(self):
        return 0


class Service:
    def store(self):
        return Store()

    def run(self):
        return 1
"""

TESTS = """\
import pytest


def test_run(service, tmp_path):
    service.run()


def test_store(store):
    store = store or None
    store.count()


class TestShop:
    def test_both(self, service, store: object):
        service.run()


async def test_session(session):
    session.run()
"""


def write(root, files):
    for name, text in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.sources = {
            "shop/__init__.py": "",
            "shop/service.py": SERVICE,
            "tests/conftest.py": CONFTEST,
            "tests/test_shop.py": TESTS,
        }
        self.rewrite = fixture_probes.plan(self.sources)

    def test_each_fixture_parameter_defaults_to_its_fixture(self):
        lines = self.rewrite.files["tests/test_shop.py"].splitlines()
        self.assertEqual(lines[3], "def test_run(*, service=_f0(), tmp_path):")
        # A parameter the test assigns keeps no default.
        self.assertEqual(lines[7], "def test_store(store):")
        # An annotated parameter is the annotation's.
        self.assertEqual(
            lines[13], "    def test_both(self, *, service=_f0(), store: object):"
        )
        self.assertEqual(lines[17], "async def test_session(session=_a(_f1())):")
        self.assertTrue(
            lines[0].startswith(
                "import pytest; from _deepthought_fixture_probes import"
            ),
            lines[0],
        )
        self.assertIn("from tests.conftest import service as _f0", lines[0])
        # The rest of the file keeps its positions.
        self.assertEqual(lines[4], "    service.run()")
        self.assertEqual(
            self.rewrite.edited["tests/test_shop.py"],
            {0: 13, 3: 13, 13: 24, 17: 30},
        )

    def test_a_fixture_requesting_fixtures_is_rewritten_too(self):
        lines = self.rewrite.files["tests/conftest.py"].splitlines()
        # The decorators are blanked out, in place.
        self.assertEqual(lines[5], " " * len("@pytest.fixture"))
        self.assertEqual(lines[11], "def make_store(service=_f0()):")

    def test_a_yielding_fixture_gives_what_it_yields(self):
        sources = dict(self.sources)
        sources["tests/test_store.py"] = "def test_count(store):\n    store.count()\n"
        rewrite = fixture_probes.plan(sources)
        # No statement before the test can take the import.
        self.assertNotIn("tests/test_store.py", rewrite.edited)
        sources["tests/test_store.py"] = (
            "X = 1\n\n\ndef test_count(store):\n    store.count()\n"
        )
        rewrite = fixture_probes.plan(sources)
        self.assertEqual(
            rewrite.files["tests/test_store.py"].splitlines()[3],
            "def test_count(store=_y(_f0())):",
        )


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-python"])
    and importlib.util.find_spec("google.protobuf"),
    "Install scip-python and the scip extra",
)
class FixtureIndexTest(unittest.TestCase):
    def test_calls_on_fixture_parameters_get_the_fixture_types(self):
        from deepthought._scip import scip_pb2

        with tempfile.TemporaryDirectory() as scratch:
            root, out = Path(scratch) / "work", Path(scratch) / "out"
            write(
                root,
                {
                    "pyproject.toml": '[project]\nname = "shop"\n',
                    "shop/__init__.py": "",
                    "shop/service.py": SERVICE,
                    "tests/conftest.py": CONFTEST,
                    "tests/test_shop.py": TESTS,
                },
            )
            report = indexers.index_workspace(root, out, languages=("python",))
            self.assertEqual(indexers.failed_files(report), {})
            (run,) = report["runs"]
            self.assertEqual(run["fixture_probes"]["status"], "indexed")
            index = scip_pb2.Index()
            index.ParseFromString(Path(run["index"]).read_bytes())
            documents = {d.relative_path: d for d in index.documents}
            at = {
                tuple(o.range[:2]): o.symbol
                for o in documents["tests/test_shop.py"].occurrences
            }
            self.assertTrue(at[(4, 12)].endswith("`shop.service`/Service#run()."))
            self.assertTrue(at[(18, 12)].endswith("`shop.service`/Service#run()."))
            # The assigned parameter is not typed.
            self.assertNotIn((9, 10), at)
            again = indexers.index_workspace(
                root, Path(scratch) / "again", languages=("python",)
            )
            self.assertEqual(
                Path(again["runs"][0]["index"]).read_bytes(),
                Path(run["index"]).read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
