"""HTTP client calls on client objects made elsewhere (a parameter, an
imported instance), identified by the request method the SCIP index
resolves the called name to."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from typescript_ready import needs_typescript

READY = (
    importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_python")
    and importlib.util.find_spec("tree_sitter_javascript")
)

FILES = {
    "app/__init__.py": "",
    "app/sync.py": """import httpx

async def push(client: httpx.AsyncClient, cache):
    await client.post("/v1/items", json={})
    cache.get("/not-a-request")
""",
    "web/api.js": """export function load(http, store) {
  http.get("/api/orders")
  store.get("/not-a-request")
}
""",
}

HTTPX = "scip-python python httpx 0.27.0 `httpx._client`/AsyncClient#post()."
AXIOS = "scip-typescript npm axios 1.7.2 index.d.ts/Axios#get."

# (0-based line, name, nth occurrence on the line, symbol)
OCCURRENCES = {
    "python": {"app/sync.py": [(3, "post", 0, HTTPX)], "app/__init__.py": []},
    "javascript": {"web/api.js": [(1, "get", 0, AXIOS)]},
}


def scip_index(directory, language):
    import deepthought
    from deepthought._scip import scip_pb2 as wire

    index = wire.Index()
    index.metadata.tool_info.name = (
        "scip-python" if language == "python" else "scip-typescript"
    )
    index.metadata.tool_info.version = "0.6.8" if language == "python" else "0.6.2"
    for path, found in OCCURRENCES[language].items():
        document = index.documents.add(relative_path=path)
        lines = FILES[path].split("\n")
        for line, name, nth, symbol in found:
            column = -1
            for _ in range(nth + 1):
                column = lines[line].index(name, column + 1)
            document.occurrences.add(
                symbol=symbol, range=[line, column, column + len(name)]
            )
    path = Path(directory) / f"{language}.scip"
    path.write_bytes(index.SerializeToString())
    return deepthought.ingest_scip(
        path, repository_id="r", default_position_encoding="utf-8"
    ), "sha256:test"


def requests(indexed):
    with (
        tempfile.TemporaryDirectory() as directory,
        tempfile.TemporaryDirectory() as scratch,
    ):
        for path, text in FILES.items():
            (Path(directory) / path).parent.mkdir(parents=True, exist_ok=True)
            (Path(directory) / path).write_text(text)
        tool = ServiceContractTool(Path(directory))
        indexes = (
            [scip_index(scratch, "python"), scip_index(scratch, "javascript")]
            if indexed
            else []
        )
        asyncio.run(
            tool.analyze("r", languages={"python", "javascript"}, scip_indexes=indexes)
        )
        _, _, found, _, _ = tool.last_facts
        return sorted((r.file, r.method, r.url, r.client) for r in found)


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
@needs_typescript
class TypedClientTest(unittest.TestCase):
    def test_client_objects_are_known_by_the_method_the_index_resolves(self):
        self.assertEqual(
            requests(indexed=True),
            [
                ("app/sync.py", "POST", "/v1/items", "httpx"),
                ("web/api.js", "GET", "/api/orders", "axios"),
            ],
        )

    def test_without_an_index_none_are_read(self):
        self.assertEqual(requests(indexed=False), [])


if __name__ == "__main__":
    unittest.main()
