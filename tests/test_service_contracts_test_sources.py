"""Client calls in test source files: linked and marked test_source, with
nothing else read from those files."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool

READY = all(
    importlib.util.find_spec(name)
    for name in ("tree_sitter", "tree_sitter_go", "tree_sitter_python")
)

PROTO = """\
syntax = "proto3";
package shop;
service CartService { rpc AddItem(A) returns (B) {} }
message A {}
message B {}
"""

GO_SERVER = """\
package main

type cartServer struct{}

func (c *cartServer) AddItem(ctx context.Context, r *pb.A) (*pb.B, error) {
\treturn nil, nil
}

func main() {
\tpb.RegisterCartServiceServer(srv, &cartServer{})
}
"""

GO_TEST = """\
package main

type fakeServer struct{}

func (f *fakeServer) AddItem(ctx context.Context, r *pb.A) (*pb.B, error) {
\treturn nil, nil
}

func TestAddItem(t *testing.T) {
\tpb.RegisterCartServiceServer(srv, &fakeServer{})
\tpb.NewCartServiceClient(conn).AddItem(ctx, &pb.A{})
}
"""

PY_APP = """\
from flask import Flask

app = Flask(__name__)


@app.route("/orders/<id>")
def order(id):
    return id
"""

PY_TEST = """\
import requests
from flask import Flask

BASE = "http://localhost:5000"
stub = Flask(__name__)


@stub.route("/stub-only")
def fake():
    return ""


def test_order():
    requests.get(f"{BASE}/orders/7")


def test_unknown_base(base_url):
    requests.get(base_url + "/orders/8")
"""

PY_CLIENT = """\
import requests


def fetch(base_url):
    return requests.get(base_url + "/orders/9")
"""

FILES = {
    "proto/cart.proto": PROTO,
    "cart/go.mod": "module example.com/cart\n",
    "cart/server.go": GO_SERVER,
    "cart/server_test.go": GO_TEST,
    "orders/requirements.txt": "flask\n",
    "orders/app.py": PY_APP,
    "orders/client.py": PY_CLIENT,
    "orders/tests/test_orders.py": PY_TEST,
}


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class TestSourceCallTests(unittest.TestCase):
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

    def test_http(self):
        _, routes, requests, _, resolutions = self.tool.last_facts
        # A route a test declares is not read.
        self.assertEqual([r.path for r in routes], ["/orders/{var}"])
        status = {r.request_id: r.status for r in resolutions}
        self.assertEqual(
            sorted((q.file, q.line, q.test_source, status[q.id]) for q in requests),
            [
                # Application code with an unknown base skips its own project.
                ("orders/client.py", 5, False, "unresolved"),
                ("orders/tests/test_orders.py", 14, True, "linked"),
                # A test's unknown base may be its own project.
                ("orders/tests/test_orders.py", 18, True, "linked"),
            ],
        )
        marked = [
            r.properties.get("test_source")
            for r in self.output.relationships
            if r.type == "REQUESTS" and "HttpRoute" in r.target_labels
        ]
        self.assertEqual(marked, [True, True])

    def test_grpc(self):
        grpc = self.tool.last_grpc
        # The test's fake server is not a handler.
        self.assertEqual([h.file for h in grpc.handlers], ["cart/server.go"])
        self.assertEqual(
            [(c.file, c.method, c.test_source) for c in grpc.calls],
            [("cart/server_test.go", "AddItem", True)],
        )
        calls = [
            r.properties
            for r in self.output.relationships
            if r.type == "REQUESTS" and "GrpcMethod" in r.target_labels
        ]
        self.assertEqual([p.get("test_source") for p in calls], [True])


if __name__ == "__main__":
    unittest.main()
