"""Requests sent through Python client objects and their base URLs."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.service_contracts.http import PLACEHOLDER

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_python"
)

ORDERS = """
from fastapi import FastAPI

app = FastAPI()

@app.get("/api/items/{item_id}")
def item(item_id):
    pass

@app.post("/api/orders")
def create():
    pass
"""

CLIENTS = """
import aiohttp
import httpx
import requests
from httpx import AsyncClient

session = requests.Session()


def fetch(item_id):
    return session.get(f"http://orders/api/items/{item_id}")


def with_base():
    with httpx.Client(base_url="http://orders/api") as client:
        client.post("/orders")
        client.get("http://elsewhere/ping")


async def joined():
    async with aiohttp.ClientSession(base_url="http://orders/api/") as s:
        await s.get("items/7")
        await s.get("/health")


class Gateway:
    def __init__(self, base):
        self.http = AsyncClient(base_url="http://orders")
        self.other = AsyncClient(base_url=base)

    async def item(self, item_id):
        return await self.http.request("GET", f"/api/items/{item_id}")

    async def unknown_base(self):
        return await self.other.post("/api/orders")


def rebound():
    client = httpx.Client()
    client = object()
    client.get("http://orders/api/orders")


def not_a_client(cache):
    cache.get("/api/orders")
"""

FILES = {
    "orders/main.py": ORDERS,
    "web/clients.py": CLIENTS,
}


@unittest.skipUnless(READY, "tree-sitter-python is not installed")
class PythonClientObjectTests(unittest.TestCase):
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
            (r.line, r.client, r.method, r.url.replace(PLACEHOLDER, "{?}"))
            for r in requests
            if r.file.endswith("clients.py")
        )
        self.assertEqual(
            found,
            [
                # A session bound once to the name.
                (11, "requests", "GET", "http://orders/api/items/{?}"),
                # httpx appends the path to the base path.
                (16, "httpx", "POST", "http://orders/api/orders"),
                # An absolute URL ignores the base.
                (17, "httpx", "GET", "http://elsewhere/ping"),
                # aiohttp joins a relative reference to the base.
                (22, "aiohttp", "GET", "http://orders/api/items/7"),
                (23, "aiohttp", "GET", "http://orders/health"),
                # self.NAME set once in the class.
                (32, "httpx", "GET", "http://orders/api/items/{?}"),
                # A base that is not a literal leaves the path as written.
                (35, "httpx", "POST", "/api/orders"),
            ],
        )

    def test_links(self):
        _, routes, requests, _, resolutions = self.tool.last_facts
        paths = {r.id: f"{' '.join(r.methods)} {r.path}" for r in routes}
        by_id = {r.id: r for r in requests}
        linked = sorted(
            (by_id[res.request_id].line, paths[res.route_id])
            for res in resolutions
            if res.status == "linked"
        )
        self.assertIn((11, "GET /api/items/{item_id}"), linked)
        self.assertIn((16, "POST /api/orders"), linked)
        self.assertIn((32, "GET /api/items/{item_id}"), linked)


if __name__ == "__main__":
    unittest.main()
