"""Gin, Echo, chi and Fiber routes, as the Go type checker gives them:
verbs, Any/Handle/Match, Group prefixes, routers passed to functions, and
handlers."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool

from go_stubs import go_mod, go_stub_files, needs_go

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_go"
)

GIN = """package api

import (
	"net/http"

	"github.com/gin-gonic/gin"
)

type Server struct {
	router *gin.Engine
}

func Health(c *gin.Context) {}

func (s *Server) List(c *gin.Context) {}

func (s *Server) Routes() {
	api := s.router.Group("/api")
	{
		v1 := api.Group("/v1", auth())
		v1.GET("/orders/:id", auth(), s.List)
		v1.Handle(http.MethodPost, "/orders", s.List)
		v1.Match([]string{"PUT", "PATCH"}, "/orders/:id", s.List)
	}
	protected := s.router.Use(auth())
	protected.DELETE("/orders/:id", s.List)
	s.router.Any("/files/*path", Health)
}

func Mount(g *gin.RouterGroup) {
	g.POST("/carts", Health)
}

func main() {
	r := gin.Default()
	r.GET("/health", Health)
	var dynamic = "/x"
	r.Handle(method(), dynamic, Health)
}

func auth() gin.HandlerFunc { return nil }

func method() string { return http.MethodGet }
"""

ECHO = """package main

import (
	"github.com/labstack/echo/v4"
)

func Message(c echo.Context) error { return nil }

func main() {
	e := echo.New()
	api := e.Group("/api")
	api.GET("/message/:id", Message, middleware())
	api.Add("DELETE", "/message/:id", Message)
	e.Any("/", Message)
}

func middleware() echo.MiddlewareFunc { return nil }
"""

NOT_A_ROUTER = """package other

import "github.com/gin-gonic/gin"

type Client struct{}

func (c *Client) GET(path string, handler any) {}

func f(client *Client, c *gin.Context) {
	client.GET("/not-a-route", nil)
	c.GET("/neither")
}
"""

ROUTES = """package routes

import (
	"example.com/orders/api"
	"github.com/gin-gonic/gin"
)

func Register(r *gin.Engine) {
	v1 := r.Group("/v1")
	api.Mount(v1)
}
"""

MORE = """package api

import "github.com/gin-gonic/gin"

func more() {
	Mount(gin.New().Group("/v2"))
}
"""

UNCALLED = """package api

import "github.com/gin-gonic/gin"

func Lonely(g *gin.RouterGroup) {
	g.GET("/lonely", Health)
}
"""

FILES = {
    **go_stub_files(),
    "orders/go.mod": go_mod("example.com/orders", "github.com/gin-gonic/gin"),
    "web/go.mod": go_mod("example.com/web", "github.com/labstack/echo/v4"),
    "other/go.mod": go_mod("example.com/other", "github.com/gin-gonic/gin"),
    "orders/api/server.go": GIN,
    "orders/api/more.go": MORE,
    "orders/api/uncalled.go": UNCALLED,
    "orders/routes/routes.go": ROUTES,
    "web/main.go": ECHO,
    "other/other.go": NOT_A_ROUTER,
}


@unittest.skipUnless(READY, "tree-sitter-go is not installed")
@needs_go
class GoRouterTests(unittest.TestCase):
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
        result, routes, _, unread, _ = self.tool.last_facts
        names = {d.id: d.symbol.rpartition(".")[2] for d in result.definitions}
        found = sorted(
            (
                r.file.rpartition("/")[2],
                r.line,
                r.framework,
                r.methods,
                r.path,
                r.base_unknown,
                names.get(r.handler_id),
            )
            for r in routes
        )
        self.assertEqual(
            found,
            [
                (
                    "main.go",
                    12,
                    "echo",
                    ("GET",),
                    "/api/message/{id}",
                    False,
                    "Message",
                ),
                (
                    "main.go",
                    13,
                    "echo",
                    ("DELETE",),
                    "/api/message/{id}",
                    False,
                    "Message",
                ),
                ("main.go", 14, "echo", ("*",), "/", False, "Message"),
                # A struct field typed *gin.Engine is the root; groups nest,
                # and the handler is the last argument.
                (
                    "server.go",
                    21,
                    "gin",
                    ("GET",),
                    "/api/v1/orders/{id}",
                    False,
                    "List",
                ),
                ("server.go", 22, "gin", ("POST",), "/api/v1/orders", False, "List"),
                (
                    "server.go",
                    23,
                    "gin",
                    ("PATCH", "PUT"),
                    "/api/v1/orders/{id}",
                    False,
                    "List",
                ),
                # Use returns the router it was called on.
                ("server.go", 26, "gin", ("DELETE",), "/orders/{id}", False, "List"),
                ("server.go", 27, "gin", ("*",), "/files/{*path}", False, "Health"),
                # A group passed in takes the prefixes of the groups that
                # calls in the package and in importing packages pass it.
                ("server.go", 31, "gin", ("POST",), "/v1/carts", False, "Health"),
                ("server.go", 31, "gin", ("POST",), "/v2/carts", False, "Health"),
                ("server.go", 36, "gin", ("GET",), "/health", False, "Health"),
                # With no call passing it, the base is unknown.
                ("uncalled.go", 6, "gin", ("GET",), "/lonely", True, "Health"),
            ],
        )
        self.assertIn(
            ("orders/api/server.go", 38, "method_not_literal"),
            {(u.file, u.line, u.reason) for u in unread},
        )


CHI = """package main

import (
	"net/http"

	"github.com/go-chi/chi/v5"
)

func List(w http.ResponseWriter, r *http.Request) {}

func Mount(r chi.Router) {
	r.Get("/mounted", List)
}

func main() {
	r := chi.NewRouter()
	r.Get("/health", List)
	r.Route("/api", func(r chi.Router) {
		r.With(auth).Post("/orders", List)
		r.Route("/orders/{id:[0-9]+}", func(r chi.Router) {
			r.Delete("/", List)
			r.Method(http.MethodPut, "/", http.HandlerFunc(List))
		})
		r.Group(func(r chi.Router) {
			r.HandleFunc("/files/*", List)
		})
	})
	admin := r.With(auth).Route("/admin", nil)
	admin.Patch("/users", List)
	shop := chi.NewRouter()
	shop.Get("/items", List)
	r.Mount("/shop", shop)
	http.ListenAndServe(":8080", r)
}

func auth(next http.Handler) http.Handler { return next }
"""

CLIENT = """package client

import "net/http"

func Call(base string) {
	http.Get(base + "/api/files/a/b.txt")
	http.Post(base+"/api/orders", "application/json", nil)
	req, _ := http.NewRequest(http.MethodDelete, base+"/api/orders/7", nil)
	http.DefaultClient.Do(req)
}
"""


@unittest.skipUnless(READY, "tree-sitter-go is not installed")
@needs_go
class ChiRouterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        for name, text in {
            **go_stub_files(),
            "shop/go.mod": go_mod("example.com/shop", "github.com/go-chi/chi/v5"),
            "shop/main.go": CHI,
            "client/go.mod": "module example.com/client\n\ngo 1.22\n",
            "client/client.go": CLIENT,
        }.items():
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
            (
                r.line,
                r.framework,
                r.methods,
                r.path,
                r.base_unknown,
                names.get(r.handler_id),
            )
            for r in routes
        )
        self.assertEqual(
            found,
            [
                # A chi.Router parameter of a named function is passed in.
                (12, "chi", ("GET",), "/mounted", True, "List"),
                (17, "chi", ("GET",), "/health", False, "List"),
                # With keeps the router; Route joins its prefix.
                (19, "chi", ("POST",), "/api/orders", False, "List"),
                (21, "chi", ("DELETE",), "/api/orders/{id:[0-9]+}", False, "List"),
                # Method is (method, path, handler); a wrapped handler is not
                # anchored.
                (22, "chi", ("PUT",), "/api/orders/{id:[0-9]+}", False, None),
                # Group keeps the prefix; HandleFunc takes any method, and a
                # trailing * matches the rest of the path.
                (25, "chi", ("*",), "/api/files/{*}", False, "List"),
                # Route returns the router it mounts.
                (29, "chi", ("PATCH",), "/admin/users", False, "List"),
                # Mount gives a router held in a variable its prefix.
                (31, "chi", ("GET",), "/shop/items", False, "List"),
            ],
        )

    def test_requests_link_to_routes(self):
        _, routes, requests, _, resolutions = self.tool.last_facts
        by_id = {r.id: r for r in routes}
        status = {r.request_id: r for r in resolutions}
        self.assertEqual(
            sorted(
                (
                    q.line,
                    by_id[status[q.id].route_id].line
                    if status[q.id].route_id
                    else None,
                )
                for q in requests
            ),
            [(6, 25), (7, 19), (8, 21)],
        )


FIBER_V2 = """package main

import "github.com/gofiber/fiber/v2"

type Products struct{}

func (p *Products) List(c *fiber.Ctx) error { return nil }

// A handler factory: the route is anchored to the function returning the
// handler.
func (p *Products) Create() fiber.Handler {
	return func(c *fiber.Ctx) error { return nil }
}

func register(api fiber.Router, p *Products) {
	api.Post("/products", p.Create())
	api.Route("/admin", func(admin fiber.Router) {
		admin.Delete("/products/:id", auth, p.List)
	})
}

func main() {
	app := fiber.New()
	p := &Products{}
	app.Get("/health", p.List)
	v1 := app.Group("/api/v1", auth)
	v1.Get("/products/:id", p.List)
	v1.Add("PATCH", "/products/:id", p.List)
	v1.All("/files/*", p.List)
	register(v1, p)
	app.Listen(":3000")
}

func auth(c *fiber.Ctx) error { return nil }
"""

FIBER_V3 = """package main

import "github.com/gofiber/fiber/v3"

func Show(c fiber.Ctx) error { return nil }

func main() {
	app := fiber.New()
	api := app.Group("/api")
	api.Get("/items/:id", Show, auth)
	api.Add([]string{"PUT", "PATCH"}, "/items/:id", Show)
	api.Route("/orders").Get(Show).Post(Show, auth)
	app.Listen(":3000")
}

func auth(c fiber.Ctx) error { return nil }
"""


@unittest.skipUnless(READY, "tree-sitter-go is not installed")
@needs_go
class FiberRouterTests(unittest.TestCase):
    def routes(self, files):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in {**go_stub_files(), **files}.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            tool = ServiceContractTool(root)
            asyncio.run(tool.analyze("r"))
        result, routes, _, _, _ = tool.last_facts
        names = {d.id: d.symbol.rpartition(".")[2] for d in result.definitions}
        return sorted(
            (
                r.line,
                r.framework,
                r.methods,
                r.path,
                r.base_unknown,
                names.get(r.handler_id),
            )
            for r in routes
        )

    def test_fiber_v2(self):
        found = self.routes(
            {
                "shop/go.mod": go_mod(
                    "example.com/shop", "github.com/gofiber/fiber/v2"
                ),
                "shop/main.go": FIBER_V2,
            }
        )
        self.assertEqual(
            found,
            [
                # The router passed to register is the /api/v1 group.
                (16, "fiber", ("POST",), "/api/v1/products", False, "Create"),
                # Route calls its func literal with the router under /admin;
                # the handler is the last argument.
                (
                    18,
                    "fiber",
                    ("DELETE",),
                    "/api/v1/admin/products/{id}",
                    False,
                    "List",
                ),
                (25, "fiber", ("GET",), "/health", False, "List"),
                (27, "fiber", ("GET",), "/api/v1/products/{id}", False, "List"),
                (28, "fiber", ("PATCH",), "/api/v1/products/{id}", False, "List"),
                (29, "fiber", ("*",), "/api/v1/files/{*}", False, "List"),
            ],
        )

    def test_fiber_v3(self):
        found = self.routes(
            {
                "shop/go.mod": go_mod(
                    "example.com/shop", "github.com/gofiber/fiber/v3"
                ),
                "shop/main.go": FIBER_V3,
            }
        )
        self.assertEqual(
            found,
            [
                # v3 takes the handler first, then middleware.
                (10, "fiber", ("GET",), "/api/items/{id}", False, "Show"),
                (11, "fiber", ("PATCH", "PUT"), "/api/items/{id}", False, "Show"),
                # Route(path) returns a Register whose verbs take no path.
                (12, "fiber", ("GET",), "/api/orders", False, "Show"),
                (12, "fiber", ("POST",), "/api/orders", False, "Show"),
            ],
        )

    def test_a_path_the_flow_cannot_value_is_folded(self):
        # oapi-codegen's chi server: the router and the base URL are
        # parameters no call passes, so the path's base is one unknown. A
        # path with nothing known is unread.
        found = self.routes(
            {
                "shop/go.mod": go_mod("example.com/shop", "github.com/go-chi/chi/v5"),
                "shop/server.go": CHI_BASE_URL,
            }
        )
        self.assertEqual(
            found,
            [(10, "chi", ("GET",), "/items/{id}", True, "Show")],
        )


CHI_BASE_URL = """package main

import (
\t"net/http"

\t"github.com/go-chi/chi/v5"
)

func Handler(r chi.Router, baseURL string, any string) {
\tr.Get(baseURL+"/items/{id}", Show)
\tr.Get(any, Show)
}

func Show(w http.ResponseWriter, r *http.Request) {}
"""


if __name__ == "__main__":
    unittest.main()
