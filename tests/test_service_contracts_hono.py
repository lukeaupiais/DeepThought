"""Hono routes identified by the method the scip-typescript index resolves
a call to, under the app's basePath and the paths it is mounted at with
``route``, including apps built as one chain of calls."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from typescript_ready import needs_typescript

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_typescript"
)

FILES = {
    "src/api.ts": """import { Hono } from 'hono'
const api = new Hono().basePath('/v1')
api.get('/posts/:id', show)
export const items = new Hono()
  .get('/items', list)
  .post('/items', list)
export default api
function show(c) {}
function list(c) {}
""",
    "src/index.ts": """import { Hono } from 'hono'
import api, { items } from './api'
const app = new Hono()
app.route('/api', api)
app.route('/shop', items)
app.get('/', home)
function home(c) {}
""",
}

HONO = "scip-typescript npm hono 4.6.0 dist/types/"
BASE = HONO + "`hono-base.d.ts`/Hono#"
NEW = HONO + "`hono.d.ts`/Hono#`<constructor>`()."

# (0-based line, name, nth occurrence on the line, symbol)
OCCURRENCES = {
    "src/api.ts": [
        (1, "Hono", 0, NEW),
        (2, "get", 0, BASE + "get."),
        (3, "Hono", 0, NEW),
        (4, "get", 0, BASE + "get."),
        (5, "post", 0, BASE + "post."),
    ],
    "src/index.ts": [
        (2, "Hono", 0, NEW),
        (3, "route", 0, BASE + "route()."),
        (4, "route", 0, BASE + "route()."),
        (5, "get", 0, BASE + "get."),
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
            tool.analyze("r", languages={"typescript"}, scip_indexes=indexes)
        )
        result, found, _, _, _ = tool.last_facts
        names = {d.id: d.symbol.rpartition(".")[2] for d in result.definitions}
        rules = {
            r.properties["rule_id"] for r in output.relationships if r.type == "HANDLES"
        }
        return (
            sorted(
                (r.file, m, r.path, names.get(r.handler_id))
                for r in found
                if r.framework == "hono"
                for m in r.methods
            ),
            rules,
        )


@unittest.skipUnless(READY, "tree-sitter TypeScript grammar is not installed")
@needs_typescript
class HonoRouteTest(unittest.TestCase):
    def test_routes_join_base_paths_and_mounts(self):
        found, rules = routes(indexed=True)
        self.assertEqual(
            found,
            [
                ("src/api.ts", "GET", "/api/v1/posts/{var}", "show"),
                ("src/api.ts", "GET", "/shop/items", "list"),
                ("src/api.ts", "POST", "/shop/items", "list"),
                ("src/index.ts", "GET", "/", "home"),
            ],
        )
        self.assertEqual(rules, {"service-contracts.hono-route"})

    def test_without_an_index_none_are_read(self):
        self.assertEqual(routes(indexed=False), ([], set()))


if __name__ == "__main__":
    unittest.main()
