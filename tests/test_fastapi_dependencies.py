"""FastAPI dependencies and what injects them (fastapi_dependencies.py)."""

import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

PYTHON = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_python"
)

DEPS = """\
def get_db():
    yield 1


class Params:
    pass


class Auth:
    def __call__(self):
        return 2

if True:
    def pick():
        pass
else:
    def pick():
        pass
"""

MAIN = """\
from typing import Annotated
from fastapi import APIRouter, Depends, Security
from app.deps import Auth, Params, get_db
import other
router = APIRouter(dependencies=[Depends(get_db)])
auth = Auth()

@router.get("/a", dependencies=[Security(dependency=Auth)])
def a(db=Depends(get_db), user: Annotated[int, Security(auth)] = 0):
    return other.Depends(get_db)

def b(q: Annotated[Params, Depends()], p: Params = Depends(), *, k: Params = Depends()):
    def inner():
        return Depends(get_db)
    return inner

def c(x: Annotated[Params, Depends(get_db, use_cache=False)], y=Depends(Params, get_db)):
    pass

import fastapi
from app import deps
def d(x=fastapi.Depends(dependency=deps.get_db), y: "Params" = Depends(), z: Optional[Params, Depends()] = None, u=Depends(deps.pick), *, v: Annotated[Params, Depends()], w: Annotated[Params] = Depends()):
    pass
from app import aliases
from app.aliases import ParamsDep, Plain
def f(db: aliases.DbDep, p: ParamsDep, x: Plain, y: int, z: aliases.Deps.Db):
    pass
"""

ALIASES = """\
from typing import Annotated, TypeAlias
from fastapi import Depends
from app.deps import Params, get_db
DbDep = Annotated[int, Depends(get_db)]
ParamsDep: TypeAlias = Annotated[Params, Depends()]
Plain = Annotated[int, "meta"]
async def helper(db: DbDep, /, *, again: DbDep):
    return (lambda: db)()
class Deps:
    Db = Annotated[int, Depends(get_db)]
"""

SOURCES = {
    "app/__init__.py": "",
    "app/deps.py": DEPS,
    "app/main.py": MAIN,
    "app/aliases.py": ALIASES,
    # No index covers this file; the next one does not parse.
    "app/unindexed.py": "from fastapi import Depends\nfrom app.deps import get_db\n"
    "def e(db=Depends(get_db)):\n    pass\n",
    "app/broken.py": "def (:\n",
}

_APP = "scip-python python app 0 "
_FASTAPI = "scip-python python fastapi 0.115.0 `fastapi.param_functions`/"
DEPENDS = _FASTAPI + "Depends()."
SECURITY = _FASTAPI + "Security()."
GET_DB = _APP + "`app.deps`/get_db()."
PARAMS = _APP + "`app.deps`/Params#"
AUTH = _APP + "`app.deps`/Auth#"
PICK = _APP + "`app.deps`/pick()."
DB_DEP = _APP + "`app.aliases`/DbDep."
PARAMS_DEP = _APP + "`app.aliases`/ParamsDep."
PLAIN = _APP + "`app.aliases`/Plain."
DEPS_DB = _APP + "`app.aliases`/Deps#Db."

# (line, text, nth occurrence of text on the line, symbol, definition)
OCCURRENCES = {
    "app/deps.py": [
        (0, "get_db", 0, GET_DB, True),
        (4, "Params", 0, PARAMS, True),
        (8, "Auth", 0, AUTH, True),
        (13, "pick", 0, PICK, True),
        (16, "pick", 0, PICK, True),
    ],
    "app/main.py": [
        (4, "Depends", 0, DEPENDS, False),
        (4, "get_db", 0, GET_DB, False),
        (5, "auth", 0, _APP + "`app.main`/auth.", True),
        (5, "Auth", 0, AUTH, False),
        (7, "Security", 0, SECURITY, False),
        (7, "Auth", 0, AUTH, False),
        (8, "Depends", 0, DEPENDS, False),
        (8, "get_db", 0, GET_DB, False),
        (8, "Security", 0, SECURITY, False),
        (8, "auth", 0, _APP + "`app.main`/auth.", False),
        (9, "Depends", 0, "scip-python python other 1 other/Depends().", False),
        (9, "get_db", 0, GET_DB, False),
        (11, "Params", 0, PARAMS, False),
        (11, "Depends", 0, DEPENDS, False),
        (11, "Params", 1, PARAMS, False),
        (11, "Depends", 1, DEPENDS, False),
        (11, "Depends", 2, DEPENDS, False),
        (11, "Params", 2, PARAMS, False),
        (13, "Depends", 0, DEPENDS, False),
        (13, "get_db", 0, GET_DB, False),
        (16, "Params", 0, PARAMS, False),
        (16, "Depends", 0, DEPENDS, False),
        (16, "get_db", 0, GET_DB, False),
        (16, "Depends", 1, DEPENDS, False),
        (16, "Params", 1, PARAMS, False),
        (16, "get_db", 1, GET_DB, False),
        (21, "Depends", 0, DEPENDS, False),
        (21, "get_db", 0, GET_DB, False),
        (21, "Depends", 1, DEPENDS, False),
        (21, "Params", 1, PARAMS, False),
        (21, "Depends", 2, DEPENDS, False),
        (21, "Depends", 3, DEPENDS, False),
        (21, "pick", 0, PICK, False),
        (21, "Params", 2, PARAMS, False),
        (21, "Depends", 4, DEPENDS, False),
        (21, "Depends", 5, DEPENDS, False),
        (25, "DbDep", 0, DB_DEP, False),
        (25, "ParamsDep", 0, PARAMS_DEP, False),
        (25, "Plain", 0, PLAIN, False),
        (25, "Db", 1, DEPS_DB, False),
    ],
    "app/aliases.py": [
        (3, "DbDep", 0, DB_DEP, True),
        (3, "Depends", 0, DEPENDS, False),
        (3, "get_db", 0, GET_DB, False),
        (4, "ParamsDep", 0, PARAMS_DEP, True),
        (4, "Params", 1, PARAMS, False),
        (4, "Depends", 0, DEPENDS, False),
        (5, "Plain", 0, PLAIN, True),
        (6, "DbDep", 0, DB_DEP, False),
        (6, "DbDep", 1, DB_DEP, False),
        (9, "Db", 0, DEPS_DB, True),
        (9, "Depends", 0, DEPENDS, False),
        (9, "get_db", 0, GET_DB, False),
    ],
    "app/broken.py": [],
}


def scip_index(sources, occurrences, directory):
    """A scip-python index of ``occurrences`` in ``sources``, ingested."""
    import deepthought
    from deepthought._scip import scip_pb2 as wire

    index = wire.Index()
    index.metadata.tool_info.name = "scip-python"
    index.metadata.tool_info.version = "0.6.8"
    for path, found in occurrences.items():
        document = index.documents.add(relative_path=path)
        lines = sources[path].split("\n")
        for line, name, nth, symbol, definition in found:
            column = -1
            for _ in range(nth + 1):
                column = lines[line].index(name, column + 1)
            document.occurrences.add(
                symbol=symbol,
                range=[line, column, column + len(name)],
                symbol_roles=wire.Definition if definition else 0,
            )
    path = Path(directory) / "index.scip"
    path.write_bytes(index.SerializeToString())
    return deepthought.ingest_scip(path, repository_id="r"), "sha256:test"


def analyze(sources, occurrences):
    from deepthought.analysis_tools import TreeSitterTool

    with (
        tempfile.TemporaryDirectory() as directory,
        tempfile.TemporaryDirectory() as scratch,
    ):
        for path, text in sources.items():
            (Path(directory) / path).parent.mkdir(parents=True, exist_ok=True)
            (Path(directory) / path).write_text(text)
        indexes = (scip_index(sources, occurrences, scratch),) if occurrences else ()
        return asyncio.run(
            TreeSitterTool(directory).analyze(
                "r", languages=("python",), scip_indexes=indexes
            )
        )


def edges(output):
    def name(match):
        return match["qualified_name"].split("::")[2]

    return sorted(
        (
            name(r.source_match),
            name(r.target_match),
            r.properties["line"],
            r.properties["column"],
            r.properties["name"],
            r.properties["injector"],
            r.properties["rule_id"],
            r.properties["evidence_type"],
            r.properties["producer"],
            r.properties.get("alias"),
        )
        for r in output.relationships
        if r.type == "USES_DEPENDENCY"
    )


@unittest.skipUnless(PYTHON, "Provision the python grammar")
class DependencyTests(unittest.TestCase):
    def test_endpoints_routers_and_dependencies_use_what_they_inject(self):
        found = ("fastapi.depends", "resolved", "scip-python@0.6.8")
        rule = (*found, None)

        def via(line):
            return (*found, f"app/aliases.py:{line}")

        self.assertEqual(
            edges(analyze(SOURCES, OCCURRENCES)),
            [
                # A parameter default and an Annotated extra belong to the
                # function, as does a decorator's dependencies=[...];
                # dependency= names the dependency too.
                ("a", "Auth", 8, 52, "Auth", "Security", *rule),
                ("a", "get_db", 9, 17, "get_db", "Depends", *rule),
                # Depends() calls the parameter's annotation, or T of
                # Annotated[T, Depends()], keyword-only parameters too.
                ("b", "Params", 12, 19, "Params", "Depends", *rule),
                ("b", "Params", 12, 42, "Params", "Depends", *rule),
                ("b", "Params", 12, 68, "Params", "Depends", *rule),
                # Elsewhere, the innermost function around the call.
                ("b.inner", "get_db", 14, 23, "get_db", "Depends", *rule),
                # The dependency is the first argument, whatever else
                # Annotated or Depends holds.
                ("c", "Params", 17, 72, "Params", "Depends", *rule),
                ("c", "get_db", 17, 35, "get_db", "Depends", *rule),
                # A dotted callee and dependency. A string annotation, a
                # subscript other than Annotated, Annotated with one argument
                # and a name the index resolves to two declarations give
                # nothing.
                ("d", "Params", 22, 151, "Params", "Depends", *rule),
                ("d", "get_db", 22, 40, "get_db", "Depends", *rule),
                # A parameter annotated with an alias of Annotated[T,
                # Depends(...)] (in this module or another, at module or
                # class level) uses what the alias injects; Depends() there
                # calls T. The alias itself is no use, and an alias without
                # Depends gives nothing.
                ("f", "Params", 26, 28, "ParamsDep", "Depends", *via(5)),
                ("f", "get_db", 26, 18, "DbDep", "Depends", *via(4)),
                ("f", "get_db", 26, 73, "Db", "Depends", *via(10)),
                # A router's dependencies=[...] belong to the file.
                ("file", "get_db", 5, 41, "get_db", "Depends", *rule),
                # Positional-only and keyword-only parameters of an async
                # function too.
                ("helper", "get_db", 7, 21, "DbDep", "Depends", *via(4)),
                ("helper", "get_db", 7, 41, "DbDep", "Depends", *via(4)),
            ],
        )

    @unittest.skipUnless(sys.version_info >= (3, 12), "type statements")
    def test_a_type_statement_alias_injects_too(self):
        sources = {
            "app/deps.py": DEPS,
            "app/main.py": "from typing import Annotated\n"
            "from fastapi import Depends\n"
            "from app.deps import get_db\n"
            "type DbDep = Annotated[int, Depends(get_db)]\n"
            "def g(db: DbDep):\n"
            "    pass\n",
        }
        db_dep = _APP + "`app.main`/DbDep."
        occurrences = {
            "app/deps.py": OCCURRENCES["app/deps.py"],
            "app/main.py": [
                (3, "DbDep", 0, db_dep, True),
                (3, "Depends", 0, DEPENDS, False),
                (3, "get_db", 0, GET_DB, False),
                (4, "DbDep", 0, db_dep, False),
            ],
        }
        self.assertEqual(
            [e[:2] + e[-1:] for e in edges(analyze(sources, occurrences))],
            [("g", "get_db", "app/main.py:4")],
        )

    def test_files_python_cannot_parse_are_reported(self):
        metadata = analyze(SOURCES, None).metadata
        python = "{}.{}".format(*sys.version_info[:2])
        self.assertEqual(
            metadata.custom_metrics["python_ast_unparsed"],
            {"python": python, "files": ["app/broken.py"]},
        )
        warnings = [w for w in metadata.warnings if "python_ast_unparsed" in w]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(
            warnings[0].startswith(
                f"python_ast_unparsed: app/broken.py: Python {python}'s ast cannot "
                "parse it (invalid syntax"
            ),
            warnings[0],
        )

    def test_nothing_is_linked_without_an_index(self):
        # Without the index, Depends is not known to be FastAPI's.
        self.assertEqual(edges(analyze(SOURCES, None)), [])


if __name__ == "__main__":
    unittest.main()
