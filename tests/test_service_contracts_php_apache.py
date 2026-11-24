"""Symfony routes and curl requests in PHP, and Apache HttpClient requests
in Java whose base URL a constructor parameter sets."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool

READY = (
    importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_php")
    and importlib.util.find_spec("tree_sitter_java")
    and importlib.util.find_spec("tree_sitter_javascript")
)

RATINGS = """<?php
namespace App\\Controller;

use Symfony\\Component\\Routing\\Annotation\\Route;

/**
 * @Route("/api")
 */
class RatingsController
{
    /**
     * @Route(path="/rate/{sku}/{score}", methods={"PUT"})
     */
    public function put(string $sku, int $score) {}

    /**
     * @Route("/fetch/{sku}", methods={"GET"})
     */
    public function get(string $sku) {}

    #[Route('/health', methods: ['GET'])]
    public function health() {}

    /**
     * @Route($dynamic)
     */
    public function unread() {}
}
"""

CLIENT = """<?php
namespace App\\Service;

class CatalogueService
{
    public function check(string $sku): bool
    {
        $url = sprintf('%s/product/%s', $this->catalogueUrl, $sku);
        $curl = curl_init($url);
        curl_setopt_array($curl, [CURLOPT_RETURNTRANSFER => true]);
        return (bool) curl_exec($curl);
    }

    public function remove(string $sku)
    {
        $curl = curl_init("http://catalogue/product/$sku");
        curl_setopt($curl, CURLOPT_CUSTOMREQUEST, 'DELETE');
        curl_exec($curl);
    }
}
"""

CATALOGUE = """
const express = require('express');
const app = express();
app.get('/product/:sku', a);
app.delete('/product/:sku', b);
app.post('/shipping/:id', c);
"""

HELPER = """
package shop;

import org.apache.http.client.methods.HttpPost;

public class CartHelper {
    private String baseUrl;

    public CartHelper(String baseUrl) {
        this.baseUrl = baseUrl;
    }

    public void add(String id) {
        HttpPost post = new HttpPost(baseUrl + id);
    }
}
"""

CONTROLLER = """
package shop;

public class Controller {
    private String CART_URL = String.format("http://%s/shipping/", "cart");

    public void ship(String id) {
        new CartHelper(CART_URL).add(id);
    }
}
"""


# Negative cases: one `new` passes unknown text; a constructor sets the
# field to a literal instead of its parameter.
CLIENTS = """
package shop;

import org.apache.http.client.methods.HttpGet;
import org.apache.http.client.methods.HttpPut;

class Mixed {
    private String base;

    Mixed(String base) {
        this.base = base;
    }

    void get(String id) {
        new HttpGet(base + id);
    }
}

class Disagree {
    private String base;

    Disagree(String base) {
        this.base = base;
    }

    Disagree() {
        this.base = "http://other/";
    }

    void put(String id) {
        new HttpPut(base + id);
    }
}

class Make {
    void make(String from) {
        new Mixed("http://cart/shipping/");
        new Mixed(from);
        new Disagree("http://cart/shipping/");
    }
}
"""

FILES = {
    "ratings/src/Controller/RatingsController.php": RATINGS,
    "ratings/src/Service/CatalogueService.php": CLIENT,
    "catalogue/package.json": '{"name": "catalogue"}',
    "catalogue/server.js": CATALOGUE,
    "shipping/src/main/java/shop/CartHelper.java": HELPER,
    "shipping/src/main/java/shop/Controller.java": CONTROLLER,
    "shipping/src/main/java/shop/Clients.java": CLIENTS,
}


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class PhpApacheTests(unittest.TestCase):
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

    def test_routes(self):
        routes = sorted(
            (n.properties["path"], tuple(n.properties["methods"]))
            for n in self.output.nodes
            if "HttpRoute" in n.labels and n.properties["file_path"].endswith(".php")
        )
        self.assertEqual(
            routes,
            [
                ("/api/fetch/{sku}", ("GET",)),
                ("/api/health", ("GET",)),
                ("/api/rate/{sku}/{score}", ("PUT",)),
            ],
        )
        rules = {
            r.properties["rule_id"]
            for r in self.output.relationships
            if r.type == "HANDLES"
            and r.source_match["qualified_name"].startswith("r::ratings/src/Controller")
        }
        self.assertEqual(rules, {"service-contracts.symfony-route"})

    def test_constructor_fields_without_one_value(self):
        # Mixed: one new passes a parameter; Disagree: a constructor sets a
        # literal. Neither base is folded, so neither request is linked.
        requests = {
            n.properties["path"]: sorted(n.properties["unresolved_http_requests"])
            for n in self.output.nodes
            if n.properties.get("unresolved_http_requests")
        }
        self.assertEqual(
            requests.get("shipping/src/main/java/shop/Clients.java"),
            ["GET {?}{?}", "PUT {?}{?}"],
        )

    def test_requests(self):
        edges = sorted(
            (
                r.source_match["qualified_name"].split("::")[1],
                r.properties["request"],
                r.properties["client"],
            )
            for r in self.output.relationships
            if r.type == "REQUESTS"
        )
        self.assertEqual(
            edges,
            [
                (
                    "ratings/src/Service/CatalogueService.php",
                    "DELETE http://catalogue/product/{?}",
                    "php-curl",
                ),
                (
                    "ratings/src/Service/CatalogueService.php",
                    "GET {?}/product/{?}",
                    "php-curl",
                ),
                (
                    "shipping/src/main/java/shop/CartHelper.java",
                    "POST http://{?}/shipping/{?}",
                    "apache-httpclient",
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
