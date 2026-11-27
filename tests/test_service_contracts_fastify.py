"""Fastify routes on instances known as plugin parameters, under the
prefixes their plugins are registered with: ``register`` options,
fastify-plugin wrappers, and the directories @fastify/autoload loads."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from typescript_ready import needs_typescript

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_javascript"
)

FILES = {
    "index.js": """const fastify = require('fastify')()
const autoload = require('@fastify/autoload')
const path = require('path')

fastify.register(require('./plugins/api'), { prefix: '/api' })
fastify.register(autoload, { dir: path.join(__dirname, 'routes') })
fastify.register(async (child) => {
  child.get('/ping', ping)
}, { prefix: '/v2' })

function ping (request, reply) {}
""",
    "plugins/api.js": """module.exports = async function (app, opts) {
  app.get('/items/:id', show)
  app.route({ method: ['PUT', 'PATCH'], url: '/items/:id', handler: show })
}

function show (request, reply) {}
""",
    "routes/users/index.js": """module.exports = async function (server) {
  server.post('/', create)
}

function create (request, reply) {}
""",
    "routes/users/helpers.js": """module.exports = async function (server) {
  server.get('/never', never)
}

function never (request, reply) {}
""",
    "routes/health.js": """const fp = require('fastify-plugin')

module.exports = fp(async (server) => {
  server.get('/health', health)
})

function health (request, reply) {}
""",
    "routes/admin/autohooks.js": """module.exports = async function (server) {
  server.get('/hook', hook)
}

function hook (request, reply) {}
""",
    "routes/admin/stats.js": """module.exports = async function (server) {
  server.get('/', stats)
}
module.exports.autoPrefix = '/statistics'

function stats (request, reply) {}
""",
    "web/app.js": """const express = require('express')
const app = express()

function mount (router) {
  router.get('/not-fastify', handler)
}

function handler (req, res) {}
""",
}


def routes():
    with tempfile.TemporaryDirectory() as directory:
        for path, text in FILES.items():
            (Path(directory) / path).parent.mkdir(parents=True, exist_ok=True)
            (Path(directory) / path).write_text(text)
        tool = ServiceContractTool(Path(directory))
        output = asyncio.run(tool.analyze("r", languages={"javascript"}))
        result, found, _, _, _ = tool.last_facts
        names = {d.id: d.symbol.rpartition(".")[2] for d in result.definitions}
        rules = {
            r.properties["rule_id"]
            for r in output.relationships
            if r.type == "HANDLES" and r.properties["rule_id"].endswith("fastify-route")
        }
        return (
            sorted(
                (r.path, r.methods, names.get(r.handler_id), r.file)
                for r in found
                if r.framework == "fastify"
            ),
            rules,
        )


@unittest.skipUnless(READY, "tree-sitter JavaScript grammar is not installed")
@needs_typescript
class FastifyRouteTest(unittest.TestCase):
    def test_plugin_routes_take_the_prefixes_they_are_registered_with(self):
        found, rules = routes()
        self.assertEqual(
            found,
            [
                ("/api/items/{var}", ("GET",), "show", "plugins/api.js"),
                ("/api/items/{var}", ("PATCH", "PUT"), "show", "plugins/api.js"),
                ("/health", ("GET",), "health", "routes/health.js"),
                ("/statistics", ("GET",), "stats", "routes/admin/stats.js"),
                ("/users", ("POST",), "create", "routes/users/index.js"),
                ("/v2/ping", ("GET",), "ping", "index.js"),
            ],
        )
        self.assertEqual(rules, {"service-contracts.fastify-route"})


if __name__ == "__main__":
    unittest.main()
