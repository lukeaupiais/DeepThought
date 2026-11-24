"""Guzzle and Laravel HTTP client requests and their base URLs."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.service_contracts.http import PLACEHOLDER

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_php"
)

ORDERS_ROUTES = r"""<?php

use Illuminate\Support\Facades\Route;

Route::get('orders/{id}', fn ($id) => $id);
Route::post('orders', fn () => 1);
"""

CLIENTS = r"""<?php
namespace App\Services;

use GuzzleHttp\Client;
use GuzzleHttp\ClientInterface;
use Illuminate\Support\Facades\Http;

class Orders
{
    private Client $client;

    public function __construct(private ClientInterface $injected)
    {
        $this->client = new Client(['base_uri' => 'http://orders/api/']);
    }

    public function find($id)
    {
        return $this->client->get("orders/$id");
    }

    public function create()
    {
        return $this->client->request('POST', 'orders');
    }

    public function absolute()
    {
        return $this->client->getAsync('http://elsewhere/ping');
    }

    public function viaInterface()
    {
        return $this->injected->post('/api/orders');
    }

    public function local()
    {
        $http = new \GuzzleHttp\Client(['base_uri' => 'http://orders']);
        return $http->delete('/api/orders/7');
    }

    public function facade($id)
    {
        Http::withToken('t')->acceptJson()->baseUrl('http://orders/api')->get("orders/{$id}");
        return Http::post('http://orders/api/orders', ['a' => 1]);
    }

    public function notAClient($cache)
    {
        return $cache->get('orders');
    }
}
"""

FILES = {
    "orders/composer.json": '{"require": {"laravel/framework": "^10.0"}}',
    "orders/app/Providers/RouteServiceProvider.php": r"""<?php
use Illuminate\Support\Facades\Route;
Route::prefix('api')->group(base_path('routes/api.php'));
""",
    "orders/routes/api.php": ORDERS_ROUTES,
    "web/composer.json": '{"require": {"guzzlehttp/guzzle": "^7.0"}}',
    "web/src/Orders.php": CLIENTS,
}


@unittest.skipUnless(READY, "tree-sitter-php is not installed")
class PhpClientTests(unittest.TestCase):
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

    def test_requests_and_links(self):
        _, routes, requests, _, resolutions = self.tool.last_facts
        paths = {r.id: f"{' '.join(r.methods)} {r.path}" for r in routes}
        status = {
            res.request_id: (res.status, paths.get(res.route_id)) for res in resolutions
        }
        found = sorted(
            (
                r.line,
                r.client,
                r.method,
                r.url.replace(PLACEHOLDER, "{?}"),
                status[r.id][0],
                status[r.id][1],
            )
            for r in requests
        )
        self.assertEqual(
            found,
            [
                # Guzzle resolves a relative path against the base's directory.
                (
                    19,
                    "guzzle",
                    "GET",
                    "http://orders/api/orders/{?}",
                    "linked",
                    "GET /api/orders/{id}",
                ),
                (
                    24,
                    "guzzle",
                    "POST",
                    "http://orders/api/orders",
                    "linked",
                    "POST /api/orders",
                ),
                # An absolute URL ignores the base.
                (29, "guzzle", "GET", "http://elsewhere/ping", "unresolved", None),
                # An injected client has no known base: the URL as written.
                (34, "guzzle", "POST", "/api/orders", "linked", "POST /api/orders"),
                # A rooted path replaces the base's path (no DELETE route).
                (
                    40,
                    "guzzle",
                    "DELETE",
                    "http://orders/api/orders/7",
                    "unresolved",
                    None,
                ),
                # Laravel appends the path to baseUrl.
                (
                    45,
                    "laravel-http",
                    "GET",
                    "http://orders/api/orders/{?}",
                    "linked",
                    "GET /api/orders/{id}",
                ),
                (
                    46,
                    "laravel-http",
                    "POST",
                    "http://orders/api/orders",
                    "linked",
                    "POST /api/orders",
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
