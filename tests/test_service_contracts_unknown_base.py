"""A request whose URL starts with an unknown base goes to a host configured
elsewhere: path-only matching skips the routes of its own project."""

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

GATEWAY_ROUTES = r"""<?php

$router->get('/books', 'BookController@index');
$router->get('/authors', 'AuthorController@index');
"""

GATEWAY_CLIENT = r"""<?php
namespace App\Services;

use GuzzleHttp\Client;

class Downstream
{
    public $baseUri;

    public function books()
    {
        $client = new Client(['base_uri' => $this->baseUri]);
        return $client->get('/books');
    }

    public function authors()
    {
        $client = new Client(['base_uri' => $this->baseUri]);
        return $client->get('/authors');
    }

    public function relative()
    {
        $client = new Client(['base_uri' => $this->baseUri]);
        return $client->get('books');
    }

    public function own()
    {
        $client = new Client();
        return $client->get('/authors');
    }
}
"""

BOOKS_ROUTES = r"""<?php

$router->get('/books', 'BookController@index');
"""

LUMEN = '{"require": {"laravel/lumen-framework": "^8.0"}}'
BOOTSTRAP = r"""<?php
$app->router->group([], function ($router) {
    require __DIR__ . '/../routes/web.php';
});
"""

FILES = {
    "gateway/composer.json": LUMEN,
    "gateway/bootstrap/app.php": BOOTSTRAP,
    "gateway/routes/web.php": GATEWAY_ROUTES,
    "gateway/app/Services/Downstream.php": GATEWAY_CLIENT,
    "books/composer.json": LUMEN,
    "books/bootstrap/app.php": BOOTSTRAP,
    "books/routes/web.php": BOOKS_ROUTES,
}


@unittest.skipUnless(READY, "tree-sitter-php is not installed")
class UnknownBaseTests(unittest.TestCase):
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

    def test_own_project_routes_are_not_candidates(self):
        _, routes, requests, _, resolutions = self.tool.last_facts
        where = {r.id: r.file.partition("/")[0] for r in routes}
        status = {res.request_id: res for res in resolutions}
        found = sorted(
            (
                r.line,
                r.url.replace(PLACEHOLDER, "{?}"),
                status[r.id].status,
                status[r.id].reason,
                where.get(status[r.id].route_id),
            )
            for r in requests
        )
        self.assertEqual(
            found,
            [
                # The gateway's own GET /books is skipped: the books service's.
                (13, "{?}/books", "linked", None, "books"),
                # Only the gateway serves /authors, so nothing is left.
                (19, "{?}/authors", "unresolved", "no_route", None),
                # The unknown base's own path is not known: not matched.
                (25, "{?}books", "unresolved", "base_not_separated", None),
                # No base: a relative URL may well be the service's own.
                (31, "/authors", "linked", None, "gateway"),
            ],
        )


GO_READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_go"
)

GO_SERVER = """package main

import "net/http"

func main() {
	http.HandleFunc("/api/v1/auth", auth)
}
"""

GO_CLIENT = """package main

import "net/http"

var base string

func main() {
	http.Post(base+"/api/v1/auth", "application/json", nil)
}
"""


@unittest.skipUnless(GO_READY, "tree-sitter-go is not installed")
class SeveralProgramsTests(unittest.TestCase):
    """One Go module (or none) holding several programs does not say which
    program a file belongs to, so nothing is excluded."""

    def check(self, files):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, body in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
            tool = ServiceContractTool(root)
            asyncio.run(tool.analyze("r"))
            _, routes, requests, _, resolutions = tool.last_facts
            where = {r.id: r.file for r in routes}
            return [(r.status, where.get(r.route_id)) for r in resolutions]

    def test_one_module_two_programs(self):
        files = {
            "go.mod": "module example.com/shop\n",
            "auth/main.go": GO_SERVER,
            "movies/main.go": GO_CLIENT,
        }
        self.assertEqual(self.check(files), [("linked", "auth/main.go")])

    def test_no_manifest(self):
        files = {"auth/main.go": GO_SERVER, "movies/main.go": GO_CLIENT}
        self.assertEqual(self.check(files), [("linked", "auth/main.go")])

    def test_one_module_per_program(self):
        files = {
            "auth/go.mod": "module example.com/auth\n",
            "auth/main.go": GO_SERVER,
            "auth/client.go": GO_CLIENT.replace("func main()", "func call()"),
        }
        self.assertEqual(self.check(files), [("unresolved", None)])


class ReferenceTests(unittest.TestCase):
    def test_unknown_base(self):
        from deepthought.service_contracts.php import _appended, _resolve_reference

        unknown = PLACEHOLDER
        cases = [
            # A rooted path follows the unknown origin.
            (_resolve_reference(unknown, "/books"), "{?}/books"),
            (_resolve_reference(unknown + "/api/", "/books"), "{?}/books"),
            # A relative one joins the base's literal directory.
            (_resolve_reference(unknown + "/api/", "books/1"), "{?}/api/books/1"),
            (_resolve_reference(unknown + "/api", "books"), "{?}/books"),
            # With none, it stays glued to the unknown text.
            (_resolve_reference(unknown, "books"), "{?}books"),
            # An absolute URL ignores the base.
            (_resolve_reference(unknown, "http://x/y"), "http://x/y"),
            # Laravel appends.
            (_appended(unknown + "/api", "books"), "{?}/api/books"),
        ]
        self.assertEqual(
            [
                (found.replace(PLACEHOLDER, "{?}"), expected)
                for found, expected in cases
            ],
            [(expected, expected) for _, expected in cases],
        )


if __name__ == "__main__":
    unittest.main()
