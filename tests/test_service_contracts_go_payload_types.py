"""Types a Go HTTP handler decodes its request body into and encodes its
response from, as the Go type checker gives them: the route CARRIES their
declarations."""

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

TYPES = """package api

type CreateOrder struct {
\tItem string `json:"item"`
}

type Order struct {
\tID string `json:"id"`
}
"""

NET_HTTP = """package main

import (
\t"encoding/json"
\t"net/http"

\t"example.com/shop/api"
)

func createOrder(w http.ResponseWriter, r *http.Request) {
\tvar in api.CreateOrder
\tif err := json.NewDecoder(r.Body).Decode(&in); err != nil {
\t\treturn
\t}
\tenc := json.NewEncoder(w)
\tenc.Encode([]api.Order{{ID: in.Item}})
}

func proxy(w http.ResponseWriter, r *http.Request) {
\tresp, _ := http.Get("http://stock/items")
\tvar other api.Order
\tjson.NewDecoder(resp.Body).Decode(&other)
}

func main() {
\thttp.HandleFunc("/orders", createOrder)
\thttp.HandleFunc("/proxy", proxy)
\thttp.ListenAndServe(":8080", nil)
}
"""

GIN_HANDLERS = """package main

import (
\t"github.com/gin-gonic/gin"

\t"example.com/shop/api"
)

func create(c *gin.Context) {
\tvar in api.CreateOrder
\tif err := c.ShouldBindJSON(&in); err != nil {
\t\tc.JSON(400, gin.H{"error": err.Error()})
\t\treturn
\t}
\tc.JSON(201, &api.Order{ID: in.Item})
}
"""

GIN = """package main

import "github.com/gin-gonic/gin"

func main() {
\tr := gin.Default()
\tr.POST("/gin/orders", create)
\tr.Run()
}
"""


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
@needs_go
class GoPayloadTypeTests(unittest.TestCase):
    def carried(self, files):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            output = asyncio.run(ServiceContractTool(root).analyze("r"))
        self.unresolved = output.metadata.custom_metrics["payload_types_unresolved"]
        self.via = [
            r.properties["via"]
            for r in output.relationships
            if r.type == "CARRIES" and "via" in r.properties
        ]
        self.paths = {
            n.properties["qualified_name"]: n.properties["path"]
            for n in output.nodes
            if "HttpRoute" in n.labels
        }
        return sorted(
            (
                r.source_match["qualified_name"].split("::")[1],
                r.target_match["qualified_name"].split("::")[2],
                r.properties["direction"],
                r.properties["rule_id"],
                r.properties["evidence_type"],
                r.source_match["qualified_name"],
            )
            for r in output.relationships
            if r.type == "CARRIES"
        )

    def test_net_http_json_decoder_and_encoder_of_the_handler(self):
        # The proxy decodes another service's response, not its request.
        found = self.carried(
            {
                "svc/go.mod": "module example.com/shop\n\ngo 1.22\n",
                "svc/api/types.go": TYPES,
                "svc/main.go": NET_HTTP,
            }
        )
        self.assertEqual(
            [f[:5] for f in found],
            [
                (
                    "svc/main.go",
                    "CreateOrder",
                    "request",
                    "service-contracts.go-request-body-type",
                    "resolved",
                ),
                (
                    "svc/main.go",
                    "Order",
                    "response",
                    "service-contracts.go-response-body-type",
                    "resolved",
                ),
            ],
        )

    def test_gin_binds_and_writes_json(self):
        # gin.H is the library's, so the error response carries nothing.
        # The handler is declared in another file of the package.
        found = self.carried(
            {
                **go_stub_files(),
                "svc/go.mod": go_mod("example.com/shop", "github.com/gin-gonic/gin"),
                "svc/api/types.go": TYPES,
                "svc/main.go": GIN,
                "svc/handlers.go": GIN_HANDLERS,
            }
        )
        self.assertEqual(
            [f[1:3] for f in found],
            [("CreateOrder", "request"), ("Order", "response")],
        )

    def test_fiber_v3_binds_the_body_and_writes_json(self):
        # A body bound into an any (raw) is not known: unresolved.
        found = self.carried(
            {
                **go_stub_files(),
                "svc/go.mod": go_mod("example.com/shop", "github.com/gofiber/fiber/v3"),
                "svc/api/types.go": TYPES,
                "svc/main.go": FIBER_V3,
            }
        )
        self.assertEqual(
            [f[1:4] for f in found],
            [
                ("CreateOrder", "request", "service-contracts.go-request-body-type"),
                ("Order", "response", "service-contracts.go-response-body-type"),
            ],
        )
        self.assertEqual(self.unresolved, 1)

    def test_func_literal_handlers(self):
        # The handler written inline is read; a middleware literal before
        # it (gin runs the handler last) is not.
        found = self.carried(
            {
                **go_stub_files(),
                "svc/go.mod": go_mod(
                    "example.com/shop",
                    "github.com/gin-gonic/gin",
                    "github.com/gofiber/fiber/v2",
                ),
                "svc/api/types.go": TYPES,
                "svc/main.go": INLINE,
            }
        )
        self.assertEqual(
            sorted((self.paths[f[5]], f[1], f[2]) for f in found),
            [
                ("/api/orders", "CreateOrder", "request"),
                ("/api/orders", "Order", "response"),
                ("/gin/orders", "Order", "response"),
            ],
        )

    def test_helpers_the_handler_passes_its_context_to(self):
        # Two calls deep from a func literal, and one from a declared
        # handler; a helper given another value is not followed.
        found = self.carried(
            {
                **go_stub_files(),
                "svc/go.mod": go_mod("example.com/shop", "github.com/gofiber/fiber/v2"),
                "svc/api/types.go": TYPES,
                "svc/main.go": HELPERS,
                # A file whose handler only delegates: no context call of its
                # own.
                "svc/health.go": HEALTH,
            }
        )
        self.assertEqual(
            sorted((self.paths[f[5]], f[1], f[2], f[3]) for f in found),
            [
                (
                    "/health",
                    "Order",
                    "response",
                    "service-contracts.go-response-body-type-in-helper",
                ),
                (
                    "/orders",
                    "CreateOrder",
                    "request",
                    "service-contracts.go-request-body-type-in-helper",
                ),
                (
                    "/orders",
                    "Order",
                    "response",
                    "service-contracts.go-response-body-type-in-helper",
                ),
                (
                    "/orders/{id}",
                    "Order",
                    "response",
                    "service-contracts.go-response-body-type-in-helper",
                ),
            ],
        )
        self.assertEqual(
            sorted(tuple(v) for v in self.via),
            [
                ("svc/main.go::doCreate",),
                ("svc/main.go::doCreate", "svc/main.go::respond"),
                ("svc/main.go::respond",),
                ("svc/main.go::respond",),
            ],
        )

    def test_fiber_endpoint_is_the_first_handler_not_calling_next(self):
        # Fiber runs a route's handlers in order; one calling c.Next() is
        # middleware, wherever it is written.
        found = self.carried(
            {
                **go_stub_files(),
                "svc/go.mod": go_mod("example.com/shop", "github.com/gofiber/fiber/v3"),
                "svc/api/types.go": TYPES,
                "svc/main.go": FIBER_ORDER,
            }
        )
        self.assertEqual(
            sorted((self.paths[f[5]], f[1], f[2]) for f in found),
            [
                ("/factory", "CreateOrder", "request"),
                ("/first", "CreateOrder", "request"),
                ("/inline", "CreateOrder", "request"),
                ("/last", "CreateOrder", "request"),
            ],
        )

    def test_net_http_helpers_and_values_given_as_any(self):
        # net/http's request and response writer are followed into helpers;
        # a helper's interface parameter (v any) carries the type of what
        # each call gives it, through a second helper too.
        found = self.carried(
            {
                "svc/go.mod": "module example.com/shop\n\ngo 1.22\n",
                "svc/api/types.go": TYPES + ERROR_TYPE,
                "svc/main.go": NET_HTTP_HELPERS,
            }
        )
        self.assertEqual(
            sorted(
                (self.paths[f[5]], f[1], f[2], f[3].rsplit(".", 1)[1]) for f in found
            ),
            [
                ("/orders", "CreateOrder", "request", "go-request-body-type-in-helper"),
                ("/orders", "Order", "response", "go-response-body-type-in-helper"),
                ("/orders", "Problem", "response", "go-response-body-type-in-helper"),
                ("/status", "Order", "response", "go-response-body-type-in-helper"),
            ],
        )
        # The value given to writeJSON as an any is not known.
        self.assertEqual(self.unresolved, 1)


ERROR_TYPE = """
type Problem struct {
\tDetail string `json:"detail"`
}
"""

NET_HTTP_HELPERS = """package main

import (
\t"encoding/json"
\t"net/http"

\t"example.com/shop/api"
)

func readJSON(r *http.Request, v any) error {
\treturn json.NewDecoder(r.Body).Decode(v)
}

func writeJSON(w http.ResponseWriter, v any) {
\tjson.NewEncoder(w).Encode(v)
}

func fail(w http.ResponseWriter, detail string) {
\twriteJSON(w, api.Problem{Detail: detail})
}

func writeOrder(w http.ResponseWriter, o api.Order) {
\tjson.NewEncoder(w).Encode(o)
}

func create(w http.ResponseWriter, r *http.Request) {
\tvar in api.CreateOrder
\tif err := readJSON(r, &in); err != nil {
\t\tfail(w, err.Error())
\t\treturn
\t}
\twriteJSON(w, &api.Order{ID: in.Item})
}

func status(w http.ResponseWriter, r *http.Request) {
\twriteOrder(w, api.Order{})
\tvar unknown any = api.Order{}
\twriteJSON(w, unknown)
}

func main() {
\thttp.HandleFunc("/orders", create)
\thttp.HandleFunc("/status", status)
\thttp.ListenAndServe(":8080", nil)
}
"""


FIBER_ORDER = """package main

import (
\t"github.com/gofiber/fiber/v3"

\t"example.com/shop/api"
)

type controller struct{}

func (controller) create(c fiber.Ctx) error {
\tvar in api.CreateOrder
\treturn c.Bind().Body(&in)
}

func restrict() fiber.Handler {
\treturn func(c fiber.Ctx) error {
\t\treturn c.Next()
\t}
}

// A factory returning a handler that is not the module's (a library's
// middleware): whether it passes the request on is not known.
func limit() fiber.Handler {
\tvar library fiber.Handler
\treturn library
}

func audit(c fiber.Ctx) error {
\tc.JSON(api.Order{})
\treturn c.Next()
}

func main() {
\tapp := fiber.New()
\tctl := controller{}
\tapp.Post("/first", ctl.create, restrict())
\tapp.Post("/factory", limit(), ctl.create)
\tapp.Post("/last", restrict(), ctl.create)
\tapp.Post("/inline", func(c fiber.Ctx) error {
\t\treturn c.Next()
\t}, audit, ctl.create)
}
"""


HELPERS = """package main

import (
\t"github.com/gofiber/fiber/v2"

\t"example.com/shop/api"
)

func respond(c *fiber.Ctx, order api.Order) error {
\treturn c.JSON(order)
}

func doCreate(c *fiber.Ctx) error {
\tvar in api.CreateOrder
\tif err := c.BodyParser(&in); err != nil {
\t\treturn err
\t}
\treturn respond(c, api.Order{ID: in.Item})
}

func audit(in api.CreateOrder) {
\t_ = in
}

func get(c *fiber.Ctx) error {
\taudit(api.CreateOrder{})
\treturn respond(c, api.Order{ID: c.Params("id")})
}

func main() {
\tapp := fiber.New()
\tapp.Post("/orders", func(c *fiber.Ctx) error { return doCreate(c) })
\tapp.Get("/orders/:id", get)
\tapp.Get("/health", health)
}
"""

HEALTH = """package main

import (
\t"github.com/gofiber/fiber/v2"

\t"example.com/shop/api"
)

func health(c *fiber.Ctx) error {
\treturn respond(c, api.Order{ID: "ok"})
}
"""


INLINE = """package main

import (
\t"github.com/gin-gonic/gin"
\t"github.com/gofiber/fiber/v2"

\t"example.com/shop/api"
)

func main() {
\tr := gin.Default()
\tr.GET("/gin/orders", func(c *gin.Context) {
\t\tvar in api.CreateOrder
\t\tc.ShouldBindJSON(&in)
\t}, func(c *gin.Context) {
\t\tc.JSON(200, []api.Order{})
\t})

\tapp := fiber.New()
\torders := app.Group("/api")
\torders.Post("/orders", func(c *fiber.Ctx) error {
\t\tvar in api.CreateOrder
\t\tif err := c.BodyParser(&in); err != nil {
\t\t\treturn err
\t\t}
\t\treturn c.JSON(api.Order{ID: in.Item})
\t})
}
"""


FIBER_V3 = """package main

import (
\t"github.com/gofiber/fiber/v3"

\t"example.com/shop/api"
)

func create(c fiber.Ctx) error {
\tvar in api.CreateOrder
\tif err := c.Bind().Body(&in); err != nil {
\t\treturn err
\t}
\treturn c.JSON(api.Order{ID: in.Item})
}

func raw(c fiber.Ctx) error {
\tvar body any = &api.CreateOrder{}
\treturn c.Bind().Body(body)
}

func main() {
\tapp := fiber.New()
\tapp.Post("/orders", create)
\tapp.Put("/orders", raw)
}
"""


if __name__ == "__main__":
    unittest.main()
