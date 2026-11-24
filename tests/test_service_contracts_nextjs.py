"""Next.js API routes: the pages router, the app router, basePath, and
packages that do not use Next.js."""

import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool

READY = (
    importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_typescript")
    and importlib.util.find_spec("tree_sitter_javascript")
    and importlib.util.find_spec("tree_sitter_python")
)

FILES = {
    "shop/package.json": json.dumps({"dependencies": {"next": "14.0.0"}}),
    "shop/next.config.js": "module.exports = { basePath: '/shop' }\n",
    "shop/pages/api/index.ts": (
        "export default function handler(req, res) { res.json({}) }\n"
    ),
    "shop/pages/api/users/[id].ts": (
        "const user = (req, res) => res.json({})\nexport default user\n"
    ),
    "shop/pages/api/files/[...path].js": (
        "export default async function (req, res) {}\n"
    ),
    "shop/pages/about.tsx": "export default function About() { return null }\n",
    "admin/package.json": json.dumps({"devDependencies": {"next": "15.0.0"}}),
    "admin/src/app/(store)/api/items/[[...slug]]/route.ts": (
        "export async function GET(request) { return Response.json([]) }\n"
        "export const POST = async (request) => Response.json({})\n"
        "function remove(request) {}\n"
        "export { remove as DELETE }\n"
        "export const dynamic = 'force-dynamic'\n"
    ),
    "admin/src/app/@modal/api/login/route.ts": ("export function POST(request) {}\n"),
    "admin/src/app/_lib/route.ts": "export function GET() {}\n",
    "admin/src/app/(.)photo/route.ts": "export function GET() {}\n",
    "admin/src/app/route.js": "export function HEAD() {}\n",
    "plain/package.json": json.dumps({"dependencies": {"express": "4"}}),
    "plain/pages/api/nothing.ts": "export default function handler() {}\n",
    "client/load.py": (
        "import requests\n"
        "\n"
        "def run(host):\n"
        "    requests.get(f'http://{host}/shop/api/users/{7}')\n"
        "    requests.post(f'http://{host}/api/items/a/b')\n"
    ),
}


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class NextjsRouteTests(unittest.TestCase):
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

    def test_routes(self):
        result, routes, _, _, _ = self.tool.last_facts
        names = {d.id: d.symbol.rpartition(".")[2] for d in result.definitions}
        found = sorted(
            (r.file, r.framework, r.methods, r.path, names.get(r.handler_id))
            for r in routes
        )
        self.assertEqual(
            found,
            [
                (
                    "admin/src/app/(store)/api/items/[[...slug]]/route.ts",
                    "nextjs",
                    ("DELETE",),
                    "/api/items/{*slug}",
                    # An exported alias is handled by the local function.
                    "remove",
                ),
                (
                    "admin/src/app/(store)/api/items/[[...slug]]/route.ts",
                    "nextjs",
                    ("GET",),
                    "/api/items/{*slug}",
                    "GET",
                ),
                (
                    "admin/src/app/(store)/api/items/[[...slug]]/route.ts",
                    "nextjs",
                    ("POST",),
                    "/api/items/{*slug}",
                    "POST",
                ),
                (
                    "admin/src/app/@modal/api/login/route.ts",
                    "nextjs",
                    ("POST",),
                    "/api/login",
                    "POST",
                ),
                ("admin/src/app/route.js", "nextjs", ("HEAD",), "/", "HEAD"),
                (
                    "shop/pages/api/files/[...path].js",
                    "nextjs",
                    ("*",),
                    "/shop/api/files/{*path}",
                    None,
                ),
                (
                    "shop/pages/api/index.ts",
                    "nextjs",
                    ("*",),
                    "/shop/api",
                    "handler",
                ),
                (
                    "shop/pages/api/users/[id].ts",
                    "nextjs",
                    ("*",),
                    "/shop/api/users/{id}",
                    "user",
                ),
            ],
        )

    def test_requests_link_to_routes(self):
        _, routes, requests, _, resolutions = self.tool.last_facts
        by_id = {r.id: r for r in routes}
        status = {r.request_id: r for r in resolutions}
        linked = sorted(
            (
                q.line,
                status[q.id].status,
                by_id[status[q.id].route_id].path if status[q.id].route_id else None,
            )
            for q in requests
        )
        self.assertEqual(
            linked,
            [
                (4, "linked", "/shop/api/users/{id}"),
                (5, "linked", "/api/items/{*slug}"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
