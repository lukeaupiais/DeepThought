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
        return sorted(
            (
                r.source_match["qualified_name"].split("::")[1],
                r.target_match["qualified_name"].split("::")[2],
                r.properties["direction"],
                r.properties["rule_id"],
                r.properties["evidence_type"],
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
            found,
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

func main() {
\tapp := fiber.New()
\tapp.Post("/orders", create)
}
"""


if __name__ == "__main__":
    unittest.main()
