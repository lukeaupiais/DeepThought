"""PHP request wrappers: a client request whose method or URL comes from
the parameters of the method around it takes the values its callers pass."""

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

BOOKS_ROUTES = r"""<?php

$router->get('/books', 'BookController@index');
$router->post('/books', 'BookController@store');
$router->get('/books/{book}', 'BookController@show');
"""

TRAIT = r"""<?php
namespace App\Traits;

use GuzzleHttp\Client;

trait ConsumesExternalService
{
    public function performRequest($method, $requestUrl, $formParams = [])
    {
        $client = new Client(['base_uri' => 'http://books']);
        return $client->request($method, $requestUrl, ['form_params' => $formParams]);
    }

    public function fixed()
    {
        $client = new Client(['base_uri' => 'http://books']);
        return $client->request('GET', '/books');
    }
}
"""

BOOK_SERVICE = r"""<?php
namespace App\Services;

use App\Traits\ConsumesExternalService;

class BookService
{
    use ConsumesExternalService;

    public function obtainBooks()
    {
        return $this->performRequest('GET', '/books');
    }

    public function createBook($data)
    {
        return $this->performRequest('POST', '/books', $data);
    }

    public function obtainBook($book)
    {
        return $this->performRequest('GET', "/books/{$book}");
    }

    public function named()
    {
        return $this->performRequest(requestUrl: '/books', method: 'GET');
    }

    public function callsFixed()
    {
        return $this->fixed();
    }
}
"""

HTTP_SERVICE = r"""<?php
namespace App\Services;

use Illuminate\Support\Facades\Http;

class HttpService
{
    public function send(string $baseUri, string $path, string $method = 'get')
    {
        return Http::withHeaders([])->$method("{$baseUri}/{$path}");
    }
}
"""

AUTHOR_SERVICE = r"""<?php
namespace App\Services;

class AuthorService
{
    public HttpService $http;
    private $untyped;

    public function __construct(HttpService $http, HttpService $other)
    {
        $this->http = $http;
        $this->untyped = $other;
    }

    public function books()
    {
        return $this->http->send('http://books', 'books');
    }

    public function create()
    {
        return $this->untyped->send('http://books', 'books', 'post');
    }
}
"""

FILES = {
    "books/composer.json": '{"require": {"laravel/lumen-framework": "^8.0"}}',
    "books/bootstrap/app.php": r"""<?php
$app->router->group([], function ($router) {
    require __DIR__ . '/../routes/web.php';
});
""",
    "books/routes/web.php": BOOKS_ROUTES,
    "gateway/composer.json": '{"require": {"guzzlehttp/guzzle": "^7.0"}}',
    "gateway/app/Traits/ConsumesExternalService.php": TRAIT,
    "gateway/app/Services/BookService.php": BOOK_SERVICE,
    "gateway/app/Services/HttpService.php": HTTP_SERVICE,
    "gateway/app/Services/AuthorService.php": AUTHOR_SERVICE,
}


@unittest.skipUnless(READY, "tree-sitter-php is not installed")
class PhpWrapperTests(unittest.TestCase):
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

    def test_values_from_callers(self):
        _, routes, requests, _, resolutions = self.tool.last_facts
        paths = {r.id: f"{' '.join(r.methods)} {r.path}" for r in routes}
        status = {
            res.request_id: (res.status, paths.get(res.route_id)) for res in resolutions
        }
        found = sorted(
            (
                r.file.rpartition("/")[2],
                r.line,
                r.method,
                r.url.replace(PLACEHOLDER, "{?}"),
                r.argument_from,
                status[r.id][1],
            )
            for r in requests
        )
        books = "gateway/app/Services/BookService.php"
        authors = "gateway/app/Services/AuthorService.php"
        self.assertEqual(
            found,
            [
                # One request per distinct value; named arguments bind by name.
                (
                    "ConsumesExternalService.php",
                    11,
                    "GET",
                    "http://books/books",
                    (f"{books}:12", f"{books}:27"),
                    "GET /books",
                ),
                (
                    "ConsumesExternalService.php",
                    11,
                    "GET",
                    "http://books/books/{?}",
                    (f"{books}:22",),
                    "GET /books/{book}",
                ),
                (
                    "ConsumesExternalService.php",
                    11,
                    "POST",
                    "http://books/books",
                    (f"{books}:17",),
                    "POST /books",
                ),
                # A site that does not use the parameters stays as it is.
                (
                    "ConsumesExternalService.php",
                    17,
                    "GET",
                    "http://books/books",
                    (),
                    "GET /books",
                ),
                # A dynamic method name folds to the verb a caller passes;
                # a parameter it leaves out takes its default.
                (
                    "HttpService.php",
                    10,
                    "GET",
                    "http://books/books",
                    (f"{authors}:17",),
                    "GET /books",
                ),
                (
                    "HttpService.php",
                    10,
                    "POST",
                    "http://books/books",
                    (f"{authors}:22",),
                    "POST /books",
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
