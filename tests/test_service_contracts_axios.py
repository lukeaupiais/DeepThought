"""Requests sent through axios instances: axios.create in the same module,
on a class field, and imported from another module."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.service_contracts.http import PLACEHOLDER

READY = (
    importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_typescript")
    and importlib.util.find_spec("tree_sitter_javascript")
)

FILES = {
    "web/src/api/index.js": (
        "import axios from 'axios';\n"
        "const service = axios.create({ baseURL: '//localhost:8000/', timeout: 1 });\n"
        "export default service;\n"
        "export const raw = axios.create();\n"
        "export const unknown = axios.create({ ...defaults, baseURL: '/x' });\n"
        "const local = axios.create({ baseURL: 'http://orders:8080/api' });\n"
        "export { local as orders };\n"
    ),
    "web/src/api/beer.js": (
        "import request, { raw, unknown as u, orders } from './index';\n"
        "export const list = () => request.get('/v1/beers');\n"
        "export const absolute = () => request.get('http://other:9000/v1/x');\n"
        "export const plain = () => raw.post('/v1/plain');\n"
        "export const spread = () => u.delete('/v1/spread');\n"
        "export const config = () => orders.request({ url: 'items', method: 'put' });\n"
        "export const called = () => orders({ url: '/items/7' });\n"
    ),
    "booking/src/flight.client.ts": (
        "import axios, { AxiosInstance } from 'axios';\n"
        "\n"
        "export class FlightClient {\n"
        "  private readonly client: AxiosInstance;\n"
        "\n"
        "  constructor() {\n"
        "    this.client = axios.create({ baseURL: 'http://localhost:3344' });\n"
        "  }\n"
        "\n"
        "  reserve(id: number) {\n"
        "    return this.client.post<Seat>(`/api/seats/${id}/reserve`, {});\n"
        "  }\n"
        "}\n"
        "\n"
        "export class Twice {\n"
        "  private client = axios.create({ baseURL: 'http://a' });\n"
        "  reset() { this.client = axios.create({ baseURL: 'http://b' }); }\n"
        "  get() { return this.client.get('/twice'); }\n"
        "}\n"
    ),
    "flight/src/seats.ts": (
        "import express from 'express';\n"
        "const app = express();\n"
        "app.post('/api/seats/:id/reserve', (req, res) => res.json({}));\n"
    ),
}


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class AxiosInstanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        for name, text in FILES.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.tool = ServiceContractTool(root)
        asyncio.run(self.tool.analyze("r"))

    def tearDown(self):
        self.directory.cleanup()

    def test_requests(self):
        _, _, requests, _, _ = self.tool.last_facts
        found = sorted(
            (q.file, q.line, q.client, q.method, q.url.replace(PLACEHOLDER, "{?}"))
            for q in requests
        )
        self.assertEqual(
            found,
            [
                # A field given two values is not followed.
                (
                    "booking/src/flight.client.ts",
                    11,
                    "axios",
                    "POST",
                    "http://localhost:3344/api/seats/{?}/reserve",
                ),
                # The default export, joined with one slash; protocol-relative
                # is read as http.
                (
                    "web/src/api/beer.js",
                    2,
                    "axios",
                    "GET",
                    "http://localhost:8000/v1/beers",
                ),
                # An absolute URL ignores the base.
                ("web/src/api/beer.js", 3, "axios", "GET", "http://other:9000/v1/x"),
                # No config: no base.
                ("web/src/api/beer.js", 4, "axios", "POST", "/v1/plain"),
                # A spread config has an unknown base.
                ("web/src/api/beer.js", 5, "axios", "DELETE", "{?}/v1/spread"),
                # request(config) and the instance called with a config.
                (
                    "web/src/api/beer.js",
                    6,
                    "axios",
                    "PUT",
                    "http://orders:8080/api/items",
                ),
                (
                    "web/src/api/beer.js",
                    7,
                    "axios",
                    "GET",
                    "http://orders:8080/api/items/7",
                ),
            ],
        )

    def test_instance_request_links_to_route(self):
        _, routes, requests, _, resolutions = self.tool.last_facts
        status = {r.request_id: r for r in resolutions}
        by_id = {r.id: r for r in routes}
        linked = [
            (q.line, by_id[status[q.id].route_id].file)
            for q in requests
            if status[q.id].route_id
        ]
        self.assertEqual(linked, [(11, "flight/src/seats.ts")])


if __name__ == "__main__":
    unittest.main()
