"""Gin and Echo routes: verbs, Any/Handle/Match, Group prefixes and
handlers."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool

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
"""

NOT_A_ROUTER = """package other

import "github.com/gin-gonic/gin"

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
    "orders/go.mod": "module example.com/orders\n",
    "orders/api/server.go": GIN,
    "orders/api/more.go": MORE,
    "orders/api/uncalled.go": UNCALLED,
    "orders/routes/routes.go": ROUTES,
    "web/main.go": ECHO,
    "other/other.go": NOT_A_ROUTER,
}


@unittest.skipUnless(READY, "tree-sitter-go is not installed")
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


if __name__ == "__main__":
    unittest.main()


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
	http.ListenAndServe(":8080", r)
}
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
class ChiRouterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        for name, text in {
            "shop/go.mod": "module example.com/shop\n",
            "shop/main.go": CHI,
            "client/go.mod": "module example.com/client\n",
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
