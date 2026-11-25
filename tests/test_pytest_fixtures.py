"""pytest fixtures and the tests that request them (pytest_fixtures.py)."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.pytest_fixtures import fixture_uses

CONFTEST_ROOT = """\
import pytest

@pytest.fixture
def db():
    return 1

@pytest.fixture
def client(db):
    return 2

@pytest.fixture(name="api")
def api_fixture():
    return 3
"""

CONFTEST_INNER = """\
from pytest import fixture

@fixture
def client(client):
    return client
"""

TEST_MODULE = """\
import pytest as pt

@pt.fixture
def local():
    return 4

def helper(db):
    return db

def test_one(client, api, local, missing, db=1):
    pass

async def test_async(*, db, api=2):
    pass

@pt.mark.usefixtures("db", "nowhere")
def test_marked():
    pass

@pt.mark.usefixtures("api")
class TestGroup:
    @pt.fixture
    def local(self):
        return 5

    def test_method(self, local, db):
        pass

    @staticmethod
    def test_static(db):
        pass

    def helper(self, db):
        pass

class Other:
    def test_not_collected(self, db):
        pass

class TestUnbound:
    def test_first(client):
        pass

@pt.mark.parametrize("db", [1])
@pt.other.usefixtures("db")
def test_parametrized():
    pass
"""


def uses(sources):
    return [
        (u.consumer, u.name, u.fixture, u.line, u.rule) for u in fixture_uses(sources)
    ]


class FixtureUseTests(unittest.TestCase):
    def setUp(self):
        self.sources = {
            "tests/conftest.py": CONFTEST_ROOT,
            "tests/inner/conftest.py": CONFTEST_INNER,
            "tests/inner/test_a.py": TEST_MODULE,
            "tests/test_b.py": "def test_b(client, api):\n    pass\n",
            "tests/b_test.py": "def test_c(db):\n    pass\n",
            # Not a test module: its functions request nothing.
            "tests/helpers.py": "def test_like(db):\n    pass\n",
            "tests/test_broken.py": "def test_(:\n",
        }

    def test_tests_and_fixtures_request_fixtures_as_pytest_finds_them(self):
        a = "tests/inner/test_a.py"
        b = "tests/test_b.py"
        root, inner = "tests/conftest.py", "tests/inner/conftest.py"
        self.assertEqual(
            uses(self.sources),
            [
                # *_test.py modules are test modules too.
                (("tests/b_test.py", 1), "db", (root, 4), 1, "parameter"),
                # A fixture requests another fixture.
                ((root, 8), "db", (root, 4), 8, "parameter"),
                # An override requesting its own name gets the one it overrides.
                ((inner, 4), "client", (root, 8), 4, "parameter"),
                # A name= fixture is found by that name, the nearest conftest.py
                # wins, and the module's own fixtures come first; parameters
                # with defaults and unknown names request nothing.
                ((a, 10), "api", (root, 12), 10, "parameter"),
                ((a, 10), "client", (inner, 4), 10, "parameter"),
                ((a, 10), "local", (a, 4), 10, "parameter"),
                # Keyword-only parameters without defaults are fixtures too.
                ((a, 13), "db", (root, 4), 13, "parameter"),
                # usefixtures names fixtures by string, on a test or a class.
                ((a, 17), "db", (root, 4), 16, "usefixtures"),
                ((a, 21), "api", (root, 12), 20, "usefixtures"),
                # In a test class, self is no fixture and the class's own
                # fixtures come first; a static method has no self.
                ((a, 26), "db", (root, 4), 26, "parameter"),
                ((a, 26), "local", (a, 23), 26, "parameter"),
                ((a, 30), "db", (root, 4), 30, "parameter"),
                # A module elsewhere sees only its own directory's conftest.py.
                ((b, 1), "api", (root, 12), 1, "parameter"),
                ((b, 1), "client", (root, 8), 1, "parameter"),
            ],
        )

    def test_a_fixture_is_found_only_as_pytest_would(self):
        sources = {
            # A fixture decorator from elsewhere is not pytest's.
            "test_x.py": "from mylib import fixture\nimport mylib\n\n@fixture\n"
            "def db():\n    pass\n\n@mylib.fixture\ndef api():\n    pass\n\n"
            "def test_x(db, api):\n    pass\n",
            # A sibling directory's conftest.py is not visible.
            "a/conftest.py": "import pytest\n\n@pytest.fixture\ndef only_a():\n"
            "    pass\n",
            "b/test_y.py": "def test_y(only_a):\n    pass\n",
            # A name= that is not a string keeps the function's name.
            "conftest.py": "import pytest\n\nNAME = 'n'\n\n"
            "@pytest.fixture(name=NAME)\ndef named():\n    pass\n",
            "test_z.py": "def test_z(named, n):\n    pass\n",
        }
        self.assertEqual(
            uses(sources),
            [(("test_z.py", 1), "named", ("conftest.py", 6), 1, "parameter")],
        )

    def test_autouse_fixtures_reach_every_test_that_sees_them(self):
        sources = {
            "conftest.py": "import pytest\n\n@pytest.fixture(autouse=True)\n"
            "def reset():\n    pass\n\n@pytest.fixture(autouse=False)\n"
            "def manual():\n    pass\n",
            # A test requesting the name gets one parameter use, not two;
            # a fixture gets no autouse uses.
            "a/test_a.py": "import pytest\n\n@pytest.fixture\ndef f():\n"
            "    pass\n\ndef test_a(reset):\n    pass\n\n"
            "class TestA:\n    @pytest.fixture(autouse=True)\n"
            "    def setup(self):\n        pass\n\n"
            "    def test_m(self):\n        pass\n",
            # A conftest.py autouse fixture covers only its own subtree.
            "b/conftest.py": "import pytest\n\n@pytest.fixture(autouse=True)\n"
            "def only_b():\n    pass\n",
            "c/test_c.py": "def test_c():\n    pass\n",
        }
        root, a = "conftest.py", "a/test_a.py"
        self.assertEqual(
            uses(sources),
            [
                ((a, 7), "reset", (root, 4), 7, "parameter"),
                ((a, 15), "reset", (root, 4), 15, "autouse"),
                ((a, 15), "setup", (a, 12), 15, "autouse"),
                (("c/test_c.py", 1), "reset", (root, 4), 1, "autouse"),
            ],
        )

    def test_a_directly_parametrized_argument_requests_no_fixture(self):
        conftest = (
            "import pytest\n\n@pytest.fixture\ndef db():\n    pass\n\n"
            "@pytest.fixture\ndef api():\n    pass\n"
        )
        test = """\
import pytest
import other

@pytest.mark.parametrize("db, api,", [(1, 2)])
def test_both(db, api):
    pass

@pytest.mark.parametrize(argnames=["db"], argvalues=[1], indirect=True)
def test_indirect(db):
    pass

@pytest.mark.parametrize(("db", "api"), [(1, 2)], indirect=["db"])
def test_some_indirect(db, api):
    pass

@pytest.mark.parametrize("db", [1])
class TestP:
    def test_m(self, db, api):
        pass

@pytest.mark.parametrize(names, [1])
def test_unknown(db):
    pass

@pytest.mark.parametrize(argvalues=[1], argnames="db", indirect=False)
@pytest.mark.parametrize(["api", NAME], [(1, 2)])
def test_keywords(db, api):
    pass

@other.mark.parametrize("db", [1])
def test_other(db):
    pass
"""
        root, t = "conftest.py", "test_p.py"
        self.assertEqual(
            uses({root: conftest, t: test}),
            [
                ((t, 9), "db", (root, 4), 9, "parameter"),
                ((t, 13), "db", (root, 4), 13, "parameter"),
                ((t, 18), "api", (root, 8), 18, "parameter"),
                ((t, 22), "db", (root, 4), 22, "parameter"),
                ((t, 31), "db", (root, 4), 31, "parameter"),
            ],
        )


@unittest.skipUnless(
    importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_python"),
    "Provision the python extra",
)
class FixtureEdgeTests(unittest.TestCase):
    def test_uses_fixture_edges_join_tests_to_their_fixtures(self):
        from deepthought.analysis_tools import TreeSitterTool

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "tests" / "conftest.py").write_text(CONFTEST_ROOT)
            (root / "tests" / "test_b.py").write_text(
                "import pytest\n\n@pytest.mark.usefixtures('db')\n"
                "class TestB:\n    def test_b(self, client):\n        pass\n\n"
                "@pytest.fixture(autouse=True)\ndef auto():\n    pass\n"
            )
            output = asyncio.run(
                TreeSitterTool(root).analyze(
                    "r", languages=("python",), scip_indexes=()
                )
            )
        names = {
            n.properties["qualified_name"]: n.properties["name"]
            for n in output.nodes
            if "Symbol" in n.labels
        }
        edges = sorted(
            (
                names[r.source_match["qualified_name"]],
                names[r.target_match["qualified_name"]],
                r.properties["line"],
                r.properties["name"],
                r.properties["rule_id"],
                r.properties["evidence_type"],
            )
            for r in output.relationships
            if r.type == "USES_FIXTURE"
        )
        self.assertEqual(
            edges,
            [
                (
                    "TestB",
                    "db",
                    3,
                    "db",
                    "pytest.fixture-usefixtures",
                    "pattern_matched",
                ),
                (
                    "TestB.test_b",
                    "auto",
                    5,
                    "auto",
                    "pytest.fixture-autouse",
                    "pattern_matched",
                ),
                (
                    "TestB.test_b",
                    "client",
                    5,
                    "client",
                    "pytest.fixture-parameter",
                    "pattern_matched",
                ),
                (
                    "client",
                    "db",
                    8,
                    "db",
                    "pytest.fixture-parameter",
                    "pattern_matched",
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
