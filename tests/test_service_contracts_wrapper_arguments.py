"""Request URLs, methods and message destinations passed to a JavaScript
wrapper function: each call of the wrapper gives its own value."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_javascript"
)

SERVER = """
const express = require('express');
const app = express();
app.get('/api/products', a);
app.post('/api/orders', b);
app.delete('/api/orders/:id', c);
"""

HTTP = """
const BASE = '/api';
export function api(path, method = 'GET') {
  return fetch(`${BASE}${path}`, { method });
}
export const remove = (id) => fetch(BASE + '/orders/' + id, { method: 'DELETE' });
export function raw(url) {
  return fetch(url);
}
export function send(path, options = {}) {
  return fetch(BASE + path, { method: options.method ?? 'GET', body: options.body });
}
"""

STORE = """
import { api, remove, send } from './http';
import * as http from './http';
export function load() { return api('/products'); }
export function order(o) { return http.api('/orders', 'POST'); }
export function drop(id) { return remove(id); }
export function list() { return send('/products'); }
export function save(body) { return send('/orders', { method: 'POST', body }); }
"""

QUEUE = """
const amqp = require('amqplib');
async function send(channel, queue, msg) {
  channel.sendToQueue(queue, Buffer.from(msg));
}
async function main() {
  const conn = await amqp.connect('amqp://x');
  const channel = await conn.createChannel();
  await channel.consume('jobs', m => m);
  send(channel, 'jobs', 'x');
}
"""

CLIENT = """
export class Client {
  base = '/api';
  products() { return this.get('/products'); }
  later() { return () => this.get('/orders/' + 1, 'DELETE'); }
  detached() {
    return function () { return this.get('/ignored'); };
  }
  private get(path: string, method = 'GET') {
    return fetch(this.base + path, { method });
  }
  static find(path: string) {
    return fetch(path);
  }
}
"""

FILES = {
    "api/package.json": '{"name": "api"}',
    "api/server.js": SERVER,
    "web/package.json": '{"name": "web"}',
    "web/src/http.js": HTTP,
    "web/src/store.js": STORE,
    "web/src/client.ts": CLIENT,
    "worker/package.json": '{"name": "worker"}',
    "worker/queue.js": QUEUE,
}


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class WrapperArgumentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        for name, text in FILES.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.output = asyncio.run(ServiceContractTool(root).analyze("r"))

    def tearDown(self):
        self.directory.cleanup()

    def test_requests(self):
        edges = sorted(
            (
                r.properties["request"],
                r.properties["line"],
                tuple(r.properties.get("argument_from", ())),
            )
            for r in self.output.relationships
            if r.type == "REQUESTS"
        )
        self.assertEqual(
            edges,
            [
                # drop passes its own parameter: no call fixes the id.
                ("DELETE /api/orders/{?}", 6, ()),
                # this.get(...) inside an arrow function of the class; the
                # call in a plain function has another this.
                ("DELETE /api/orders/{?}", 10, ("web/src/client.ts:5",)),
                # The call leaves the method out: the parameter's default.
                ("GET /api/products", 4, ("web/src/store.js:4",)),
                # A private method called as this.get(...)
                ("GET /api/products", 10, ("web/src/client.ts:4",)),
                # options = {} and options.method ?? 'GET'
                ("GET /api/products", 11, ("web/src/store.js:7",)),
                # import * as http: http.api(...)
                ("POST /api/orders", 4, ("web/src/store.js:5",)),
                # An object literal argument: { method: 'POST', body }
                ("POST /api/orders", 11, ("web/src/store.js:8",)),
            ],
        )

    def test_unresolved(self):
        # raw is never called; a static method is not followed.
        found = {
            n.properties["path"]: n.properties["unresolved_http_requests"]
            for n in self.output.nodes
            if n.properties.get("unresolved_http_requests")
        }
        self.assertEqual(
            found,
            {"web/src/client.ts": ["GET {?}"], "web/src/http.js": ["GET {?}"]},
        )

    def test_messages(self):
        edges = sorted(
            (
                r.type,
                r.target_match["qualified_name"].split("::")[-1],
                r.properties["line"],
                r.properties["rule_id"],
                tuple(r.properties.get("argument_from", ())),
            )
            for r in self.output.relationships
            if r.type in ("PUBLISHES", "CONSUMES")
        )
        self.assertEqual(
            edges,
            [
                ("CONSUMES", "/jobs", 9, "service-contracts.amqplib-consume", ()),
                (
                    "PUBLISHES",
                    "/jobs",
                    4,
                    "service-contracts.destination-argument",
                    ("worker/queue.js:10",),
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
