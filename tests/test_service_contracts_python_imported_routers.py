"""Python route decorators whose router another module defines: the
scip-python index resolves the receiver name to that router, so its routes
are read with the router's full prefix."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_python"
)

FILES = {
    # Flask: the package makes the Blueprint, a sibling module adds routes
    # to it, the application factory mounts it.
    "app/__init__.py": """from flask import Flask

def create_app():
    app = Flask(__name__)
    from app.api import bp as api_bp
    app.register_blueprint(api_bp, url_prefix="/api")
    return app
""",
    "app/api/__init__.py": """from flask import Blueprint

bp = Blueprint("api", __name__)

from app.api import users
""",
    "app/api/users.py": """from app.api import bp

@bp.route("/users/<int:id>", methods=["GET"])
def get_user(id):
    pass

@bp.post("/users")
def create_user():
    pass
""",
    # FastAPI: a router made in one module, routes added in another.
    "svc/__init__.py": "",
    "svc/routers.py": """from fastapi import APIRouter

items = APIRouter(prefix="/items")
""",
    "svc/views.py": """from svc.routers import items

@items.get("/{item_id}")
def read_item(item_id: int):
    pass
""",
    # A name the index resolves to something that is not a router.
    "svc/cache.py": """class Cache:
    def route(self, path):
        return lambda f: f

cache = Cache()
""",
    "svc/other.py": """from svc.cache import cache

@cache.route("/not-a-route")
def cached():
    pass
""",
}

BP = "scip-python python app 0 `app.api`/bp."
ITEMS = "scip-python python app 0 `svc.routers`/items."
CACHE = "scip-python python app 0 `svc.cache`/cache."

# (0-based line, name, nth occurrence on the line, symbol, is definition)
OCCURRENCES = {
    "app/api/__init__.py": [(2, "bp", 0, BP, True)],
    "app/api/users.py": [
        (0, "bp", 0, BP, False),
        (2, "bp", 0, BP, False),
        (6, "bp", 0, BP, False),
    ],
    "app/__init__.py": [(5, "api_bp", 0, BP, False)],
    "svc/routers.py": [(2, "items", 0, ITEMS, True)],
    "svc/views.py": [(0, "items", 0, ITEMS, False), (2, "items", 0, ITEMS, False)],
    "svc/cache.py": [(4, "cache", 0, CACHE, True)],
    "svc/other.py": [(0, "cache", 0, CACHE, False), (2, "cache", 0, CACHE, False)],
    "svc/__init__.py": [],
}


def scip_index(directory):
    import deepthought
    from deepthought._scip import scip_pb2 as wire

    index = wire.Index()
    index.metadata.tool_info.name = "scip-python"
    index.metadata.tool_info.version = "0.6.8"
    for path, found in OCCURRENCES.items():
        document = index.documents.add(relative_path=path)
        lines = FILES[path].split("\n")
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


def routes(indexed):
    with (
        tempfile.TemporaryDirectory() as directory,
        tempfile.TemporaryDirectory() as scratch,
    ):
        for path, text in FILES.items():
            (Path(directory) / path).parent.mkdir(parents=True, exist_ok=True)
            (Path(directory) / path).write_text(text)
        tool = ServiceContractTool(Path(directory))
        indexes = [scip_index(scratch)] if indexed else []
        output = asyncio.run(tool.analyze("r", scip_indexes=indexes))
        _, found, _, _, _ = tool.last_facts
        rules = sorted(
            {
                r.properties["rule_id"]
                for r in output.relationships
                if r.type == "HANDLES"
            }
        )
        return (
            sorted((r.file, m, r.path, r.framework) for r in found for m in r.methods),
            rules,
        )


@unittest.skipUnless(READY, "tree-sitter Python grammar is not installed")
class ImportedRouterTest(unittest.TestCase):
    def test_routes_on_imported_routers_carry_their_prefix(self):
        found, rules = routes(indexed=True)
        self.assertEqual(
            found,
            [
                ("app/api/users.py", "GET", "/api/users/{var}", "flask"),
                ("app/api/users.py", "POST", "/api/users", "flask"),
                ("svc/views.py", "GET", "/items/{item_id}", "fastapi"),
            ],
        )
        self.assertEqual(
            rules,
            [
                "service-contracts.fastapi-route-imported-router",
                "service-contracts.flask-route-imported-router",
            ],
        )

    def test_without_an_index_imported_routers_are_not_read(self):
        self.assertEqual(routes(indexed=False), ([], []))


if __name__ == "__main__":
    unittest.main()
