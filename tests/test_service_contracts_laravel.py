"""Laravel and Lumen routes, their loaders' prefixes and handlers."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool

READY = (
    importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_php")
    and importlib.util.find_spec("tree_sitter_javascript")
)

LARAVEL = '{"require": {"laravel/framework": "^8.0"}}'

PROVIDER = r"""<?php
namespace App\Providers;

use Illuminate\Support\Facades\Route;

class RouteServiceProvider extends ServiceProvider
{
    protected $namespace = 'App\\Http\\Controllers';

    public function boot()
    {
        $this->routes(function () {
            Route::prefix('api')
                ->middleware('api')
                ->namespace($this->namespace)
                ->group(base_path('routes/api.php'));

            Route::middleware('web')
                ->namespace($this->namespace)
                ->group(base_path('routes/web.php'));
        });
    }
}
"""

API = r"""<?php

use App\Http\Controllers\PhotoController;
use App\Http\Controllers\UserController;
use Illuminate\Support\Facades\Route;

Route::get('user', [UserController::class, 'current']);
Route::post('/users/{user}/avatar', 'UserController@avatar');

Route::group(['prefix' => 'admin', 'middleware' => 'auth'], function () {
    Route::prefix('v1')->group(function () {
        Route::delete('cache', [UserController::class, 'clear'])->name('cache');
    });
    Route::controller(UserController::class)->group(function () {
        Route::get('users/{id?}', 'show');
    });
});

Route::apiResource('photos', PhotoController::class)->only(['index', 'show']);
Route::resource('photos.comments', PhotoController::class)->except('create', 'edit', 'store', 'update', 'destroy');
Route::match(['get', 'post'], 'search', [UserController::class, 'search']);
Route::any('ping', fn () => 'pong');
Route::get($dynamic, [UserController::class, 'current']);
"""

WEB = r"""<?php

Route::view('/about', 'about');
"""

USERS = r"""<?php
namespace App\Http\Controllers;

class UserController
{
    public function current() {}
    public function avatar() {}
    public function clear() {}
    public function show() {}
    public function search() {}
}
"""

PHOTOS = r"""<?php
namespace App\Http\Controllers;

class PhotoController
{
    public function index() {}
    public function show() {}
}
"""

CLIENT = """
import axios from 'axios';

export function load(id) {
  return axios.get('/api/user');
}
"""

# Laravel 11: routes loaded by bootstrap/app.php.
BOOTSTRAP = r"""<?php

return Application::configure(basePath: dirname(__DIR__))
    ->withRouting(
        web: __DIR__.'/../routes/web.php',
        api: __DIR__.'/../routes/api.php',
        apiPrefix: 'api/v2',
    )
    ->create();
"""

V2_API = r"""<?php

use App\Http\Controllers\OrderController;
use Illuminate\Support\Facades\Route;

Route::get('orders', OrderController::class);
"""

ORDERS = r"""<?php
namespace App\Http\Controllers;

class OrderController
{
    public function __invoke() {}
}
"""

LUMEN_BOOTSTRAP = r"""<?php

$app->router->group([
    'namespace' => 'App\Http\Controllers',
], function ($router) {
    require __DIR__ . '/../routes/web.php';
});
"""

LUMEN_ROUTES = r"""<?php

$router->group(['prefix' => 'api'], function () use ($router) {
    $router->get('books/{book}', 'BookController@show');
    $router->post('books', ['uses' => 'BookController@store', 'as' => 'store']);
});
"""

BOOKS = r"""<?php
namespace App\Http\Controllers;

class BookController
{
    public function show() {}
    public function store() {}
}
"""

FILES = {
    "shop/composer.json": LARAVEL,
    "shop/app/Providers/RouteServiceProvider.php": PROVIDER,
    "shop/routes/api.php": API,
    "shop/routes/web.php": WEB,
    "shop/app/Http/Controllers/UserController.php": USERS,
    "shop/app/Http/Controllers/PhotoController.php": PHOTOS,
    "shop/resources/js/api.js": CLIENT,
    "orders/composer.json": '{"require": {"laravel/framework": "^11.0"}}',
    "orders/bootstrap/app.php": BOOTSTRAP,
    "orders/routes/api.php": V2_API,
    "orders/app/Http/Controllers/OrderController.php": ORDERS,
    "books/composer.json": '{"require": {"laravel/lumen-framework": "^8.0"}}',
    "books/bootstrap/app.php": LUMEN_BOOTSTRAP,
    "books/routes/web.php": LUMEN_ROUTES,
    "books/app/Http/Controllers/BookController.php": BOOKS,
    # Not a Laravel project: an unrelated Route class is not read.
    "other/composer.json": "{}",
    "other/routes.php": "<?php\nRoute::get('/nope', 'X@y');\n",
}


@unittest.skipUnless(READY, "tree-sitter PHP/JavaScript is not installed")
class LaravelRouteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        for name, text in FILES.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.tool = ServiceContractTool(root)
        self.output = asyncio.run(self.tool.analyze("r"))

    def tearDown(self):
        self.directory.cleanup()

    def test_routes_and_handlers(self):
        handlers = {
            r.properties["route_id"]: r.source_match["qualified_name"].split("::")[2]
            for r in self.output.relationships
            if r.type == "HANDLES" and r.source_match["qualified_name"].count("::") >= 3
        }
        _, routes, _, unread, _ = self.tool.last_facts
        found = sorted(
            (
                r.file.split("/")[0],
                " ".join(r.methods),
                r.path,
                r.framework,
                handlers.get(r.id.rpartition("::")[2]),
            )
            for r in routes
        )
        self.assertEqual(
            found,
            [
                (
                    "books",
                    "GET",
                    "/api/books/{book}",
                    "lumen",
                    "App.Http.Controllers.BookController.show",
                ),
                (
                    "books",
                    "POST",
                    "/api/books",
                    "lumen",
                    "App.Http.Controllers.BookController.store",
                ),
                (
                    "orders",
                    "GET",
                    "/api/v2/orders",
                    "laravel",
                    "App.Http.Controllers.OrderController.__invoke",
                ),
                (
                    "shop",
                    "*",
                    "/api/ping",
                    "laravel",
                    None,
                ),
                (
                    "shop",
                    "DELETE",
                    "/api/admin/v1/cache",
                    "laravel",
                    "App.Http.Controllers.UserController.clear",
                ),
                (
                    "shop",
                    "GET",
                    "/about",
                    "laravel",
                    None,
                ),
                (
                    "shop",
                    "GET",
                    "/api/admin/users/{id}",
                    "laravel",
                    "App.Http.Controllers.UserController.show",
                ),
                (
                    "shop",
                    "GET",
                    "/api/photos",
                    "laravel",
                    "App.Http.Controllers.PhotoController.index",
                ),
                (
                    "shop",
                    "GET",
                    "/api/photos/{var}",
                    "laravel",
                    "App.Http.Controllers.PhotoController.show",
                ),
                (
                    "shop",
                    "GET",
                    "/api/photos/{var}/comments",
                    "laravel",
                    "App.Http.Controllers.PhotoController.index",
                ),
                (
                    "shop",
                    "GET",
                    "/api/photos/{var}/comments/{var}",
                    "laravel",
                    "App.Http.Controllers.PhotoController.show",
                ),
                (
                    "shop",
                    "GET",
                    "/api/user",
                    "laravel",
                    "App.Http.Controllers.UserController.current",
                ),
                (
                    "shop",
                    "GET POST",
                    "/api/search",
                    "laravel",
                    "App.Http.Controllers.UserController.search",
                ),
                (
                    "shop",
                    "POST",
                    "/api/users/{user}/avatar",
                    "laravel",
                    "App.Http.Controllers.UserController.avatar",
                ),
            ],
        )
        self.assertEqual(
            [(u.file, u.reason) for u in unread if u.kind == "route"],
            [("shop/routes/api.php", "path_not_literal")],
        )

    def test_request_links(self):
        _, routes, requests, _, resolutions = self.tool.last_facts
        paths = {r.id: r.path for r in routes}
        self.assertEqual(
            [(res.status, paths.get(res.route_id)) for res in resolutions],
            [("linked", "/api/user")],
        )

    def test_rule(self):
        rules = {
            r.properties["rule_id"]
            for r in self.output.relationships
            if r.type == "HANDLES" and "routes/" in r.target_match["qualified_name"]
        }
        self.assertEqual(rules, {"service-contracts.laravel-route"})


if __name__ == "__main__":
    unittest.main()
