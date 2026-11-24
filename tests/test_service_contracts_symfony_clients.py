"""Symfony scoped HTTP clients: autowired by argument name, with the base
URI that framework configuration declares."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.service_contracts.http import PLACEHOLDER
from deepthought.service_contracts.php import symfony_argument_name

READY = (
    importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_php")
    and importlib.util.find_spec("yaml")
)

ORDERS_ROUTES = r"""<?php

use Illuminate\Support\Facades\Route;

Route::get('orders/{id}', fn ($id) => $id);
Route::post('orders', fn () => 1);
"""

FRAMEWORK = """framework:
    http_client:
        scoped_clients:
            orders.client:
                base_uri: 'http://orders/api/'
            billing_api.client:
                base_uri: '%env(BILLING_URL)%'
            github.client:
                scope: 'https://api\\\\.github\\\\.com'
"""

CATALOG = r"""<?php
namespace App\Service;

use Symfony\Component\DependencyInjection\Attribute\Autowire;
use Symfony\Component\DependencyInjection\Attribute\Target;
use Symfony\Contracts\HttpClient\HttpClientInterface;

class Catalog
{
    private $orders;

    public function __construct(
        private HttpClientInterface $ordersClient,
        private HttpClientInterface $billingApiClient,
        HttpClientInterface $orders_client,
    ) {
        $this->orders = $orders_client;
    }

    public function find($id)
    {
        return $this->ordersClient->request('GET', "orders/$id");
    }

    public function viaProperty()
    {
        return $this->orders->request('POST', 'orders');
    }

    public function notLiteral()
    {
        return $this->billingApiClient->request('GET', '/invoices');
    }

    public function plain(HttpClientInterface $client)
    {
        return $client->request('POST', '/api/orders');
    }

    public function attributes(
        #[Autowire(service: 'orders.client')] HttpClientInterface $a,
        #[Target('orders.client')] HttpClientInterface $b,
        #[Autowire(service: 'other')] HttpClientInterface $ordersClient,
    ) {
        $a->request('GET', 'orders/1');
        $b->request('GET', 'orders/2');
        return $ordersClient->request('GET', 'orders/3');
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
    "shop/composer.json": '{"require": {"symfony/http-client": "^6.0"}}',
    "shop/config/packages/framework.yaml": FRAMEWORK,
    "shop/src/Catalog.php": CATALOG,
}


class ArgumentNameTests(unittest.TestCase):
    def test_parsed_like_symfony(self):
        self.assertEqual(symfony_argument_name("orders.client"), "ordersClient")
        self.assertEqual(symfony_argument_name("remove_bg.client"), "removeBgClient")
        self.assertEqual(symfony_argument_name("my-API.client"), "myAPIClient")


@unittest.skipUnless(READY, "tree-sitter-php or pyyaml is not installed")
class SymfonyScopedClientTests(unittest.TestCase):
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

    def test_scoped_bases(self):
        _, routes, requests, _, resolutions = self.tool.last_facts
        paths = {r.id: f"{' '.join(r.methods)} {r.path}" for r in routes}
        status = {
            res.request_id: (res.status, paths.get(res.route_id)) for res in resolutions
        }
        found = sorted(
            (
                r.line,
                r.method,
                r.url.replace(PLACEHOLDER, "{?}"),
                status[r.id][0],
                status[r.id][1],
            )
            for r in requests
            if r.client == "symfony-http"
        )
        self.assertEqual(
            found,
            [
                # $ordersClient is orders.client, so its base applies.
                (
                    22,
                    "GET",
                    "http://orders/api/orders/{?}",
                    "linked",
                    "GET /api/orders/{id}",
                ),
                # A property assigned from the snake_case parameter: Symfony
                # parses $orders_client to the same alias.
                (27, "POST", "http://orders/api/orders", "linked", "POST /api/orders"),
                # An %env(...)% base is not literal: the URL as written.
                (32, "GET", "/invoices", "unresolved", None),
                # The default client has no base.
                (37, "POST", "/api/orders", "linked", "POST /api/orders"),
                # An attribute names the service instead of the argument name.
                (
                    45,
                    "GET",
                    "http://orders/api/orders/1",
                    "linked",
                    "GET /api/orders/{id}",
                ),
                (
                    46,
                    "GET",
                    "http://orders/api/orders/2",
                    "linked",
                    "GET /api/orders/{id}",
                ),
                (47, "GET", "orders/3", "unresolved", None),
            ],
        )


if __name__ == "__main__":
    unittest.main()
