"""Requests sent through openapi-fetch clients: createClient in the same
module, exported from another module, with and without a base URL."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.service_contracts.http import PLACEHOLDER

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_typescript"
)

FILES = {
    "web/src/api/client.ts": (
        'import createClient from "openapi-fetch";\n'
        'import type { paths } from "./schema";\n'
        'export const api = createClient<paths>({ baseUrl: "http://pets:8080/" });\n'
        "export const bare = createClient<paths>();\n"
    ),
    "web/src/pets.ts": (
        'import { api, bare } from "./api/client";\n'
        'import { createClient } from "openapi-fetch";\n'
        'export const list = () => api.GET("/pets");\n'
        "export const show = (petId: string) =>\n"
        '  api.GET("/pets/{petId}", { params: { path: { petId } } });\n'
        'export const remove = (id: string) => bare.DELETE("/pets/{id}");\n'
        "const local = createClient({ baseUrl: process.env.API, fetch });\n"
        'export const create = () => local.POST("/pets", { body: {} });\n'
        # Not openapi-fetch methods: lower case, or not a client.
        'export const lower = () => api.get("/pets");\n'
        'export const other = () => store.GET("/pets");\n'
    ),
    "server/src/app.ts": (
        "import express from 'express';\n"
        "const app = express();\n"
        "app.delete('/pets/:id', (req, res) => res.json({}));\n"
    ),
}


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class OpenapiFetchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        for name, text in FILES.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.tool = ServiceContractTool(root)
        self.output = asyncio.run(self.tool.analyze("r"))

    def tearDown(self):
        self.directory.cleanup()

    def test_requests(self):
        _, _, requests, _, _ = self.tool.last_facts
        found = sorted(
            (q.line, q.client, q.method, q.url.replace(PLACEHOLDER, "{?}"))
            for q in requests
            if q.file == "web/src/pets.ts"
        )
        self.assertEqual(
            found,
            [
                (3, "openapi-fetch", "GET", "http://pets:8080/pets"),
                # Path parameters are filled in at run time.
                (5, "openapi-fetch", "GET", "http://pets:8080/pets/{?}"),
                (6, "openapi-fetch", "DELETE", "/pets/{?}"),
                (8, "openapi-fetch", "POST", "{?}/pets"),
            ],
        )

    def test_request_without_a_base_links_to_route(self):
        linked = sorted(
            r.properties["request"]
            for r in self.output.relationships
            if r.type == "REQUESTS"
        )
        self.assertEqual(linked, ["DELETE /pets/{?}"])


if __name__ == "__main__":
    unittest.main()
