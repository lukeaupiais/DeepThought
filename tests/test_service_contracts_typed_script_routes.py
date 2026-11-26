"""JavaScript routes identified by the method the scip-typescript index
resolves a call to: koa-router routers mounted under a prefix, an Express
route chain, and an Express setting getter that is no route."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_javascript"
)

FILES = {
    "src/users.js": """const Router = require("koa-router")
const router = new Router({ prefix: "/users" })

router.get("/:id", show)
router.del("/:id", remove)
router.get("user-list", "/", list)

module.exports = router.routes()

function show(ctx) {}
function remove(ctx) {}
function list(ctx) {}
""",
    "src/index.js": """const Router = require("koa-router")
const users = require("./users")
const api = new Router()
const root = new Router()

api.use(users)
root.use("/api", api.routes())

module.exports = root
""",
    "src/items.js": """module.exports = function mount(router) {
  router
    .route("/items/:id")
    .get(read)
    .put(write)
  router.get("port")
}

function read(req, res) {}
function write(req, res) {}
""",
}

KOA = "scip-typescript npm @types/koa-router 7.4.10 `index.d.ts`/Router"
EXPRESS = "scip-typescript npm @types/express-serve-static-core 5.1.3 `index.d.ts`/"

# (0-based line, name, nth occurrence on the line, symbol)
OCCURRENCES = {
    "src/users.js": [
        (1, "Router", 0, KOA + "#`<constructor>`()."),
        (3, "get", 0, KOA + "#get()."),
        (4, "del", 0, KOA + "#del()."),
        (5, "get", 0, KOA + "#get()."),
    ],
    "src/index.js": [
        (2, "Router", 0, KOA + "#`<constructor>`()."),
        (3, "Router", 0, KOA + "#`<constructor>`()."),
        (5, "use", 0, KOA + "#use()."),
    ],
    "src/items.js": [
        (2, "route", 0, EXPRESS + "IRouter#route."),
        (3, "get", 0, EXPRESS + "IRoute#get."),
        (4, "put", 0, EXPRESS + "IRoute#put."),
        (5, "get", 0, EXPRESS + "IRouter#get."),
    ],
}


def scip_index(directory):
    import deepthought
    from deepthought._scip import scip_pb2 as wire

    index = wire.Index()
    index.metadata.tool_info.name = "scip-typescript"
    index.metadata.tool_info.version = "0.6.2"
    for path, found in OCCURRENCES.items():
        document = index.documents.add(relative_path=path)
        lines = FILES[path].split("\n")
        for line, name, nth, symbol in found:
            column = -1
            for _ in range(nth + 1):
                column = lines[line].index(name, column + 1)
            document.occurrences.add(
                symbol=symbol, range=[line, column, column + len(name)]
            )
    path = Path(directory) / "index.scip"
    path.write_bytes(index.SerializeToString())
    return deepthought.ingest_scip(
        path, repository_id="r", default_position_encoding="utf-16"
    ), "sha256:test"


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
        output = asyncio.run(
            tool.analyze("r", languages={"javascript"}, scip_indexes=indexes)
        )
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


@unittest.skipUnless(READY, "tree-sitter JavaScript grammar is not installed")
class TypedScriptRouteTest(unittest.TestCase):
    def test_routes_are_the_methods_the_index_resolves(self):
        found, rules = routes(indexed=True)
        self.assertEqual(
            found,
            [
                ("src/items.js", "GET", "/items/{var}", "express"),
                ("src/items.js", "PUT", "/items/{var}", "express"),
                ("src/users.js", "DELETE", "/api/users/{var}", "koa-router"),
                ("src/users.js", "GET", "/api/users", "koa-router"),
                ("src/users.js", "GET", "/api/users/{var}", "koa-router"),
            ],
        )
        self.assertEqual(
            rules,
            [
                "service-contracts.express-route-resolved-method",
                "service-contracts.koa-router-route",
            ],
        )

    def test_without_an_index_none_are_read(self):
        self.assertEqual(routes(indexed=False), ([], []))


if __name__ == "__main__":
    unittest.main()
