"""HTTP contracts in Python, JavaScript/TypeScript and Go sources."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool, TreeSitterTool
from deepthought.analysis_tools.evidence import get_rule
from deepthought.service_contracts.folding import Folder
from deepthought.service_contracts.http import PLACEHOLDER
from deepthought.syntax import make_parser
from deepthought.storage.graph.write_coordinator import (
    WriteCoordinatorConfig,
    _combine,
)

READY = importlib.util.find_spec("tree_sitter") and all(
    importlib.util.find_spec(f"tree_sitter_{name}")
    for name in ("python", "javascript", "go", "java")
)
U = PLACEHOLDER

FLASK = """
from flask import Flask, Blueprint

orders = Blueprint("orders", __name__, url_prefix="/orders")

@orders.route("/<int:order_id>", methods=["GET", "DELETE"])
def order(order_id):
    pass

@orders.post("/<order_id>/items/<path:rest>")
def add_item(order_id, rest):
    pass

def create_app():
    app = Flask(__name__)

    @app.route("/")
    def index():
        pass

    @app.route("/unknown/" + name)
    def unknown():
        pass

    return app

app = create_app()
"""

PAYMENT = """
import requests

CART = "http://cart:8080"

def pay(user_id, cart_id):
    requests.get("http://user:8080/check/{}".format(user_id))
    url = CART + "/cart/%s" % cart_id
    requests.delete(url)
    requests.request("POST", f"http://orders/orders/{cart_id}/items/a/b")
    requests.get(unknown_base)
"""

EXPRESS = """
const express = require('express');
const request = require('request');
const axios = require('axios');

const app = express();
const router = express.Router();

app.get('/products/:sku', getProduct);
app.delete('/products/:sku(\\\\d+)?', (req, res) => {});
router.all('/health', (req, res) => {});

function getProduct(req, res) {}

function load(sku) {
    const base = 'http://catalogue:8080';
    request(base + '/products/' + sku, () => {});
    request({ uri: `${base}/orders/${sku}`, method: 'DELETE' });
    axios.post('/products/' + sku);
    fetch('/orders/7', { method: 'GET' });
}
"""

GO_SERVER = """
package main

import (
	"net/http"

	"github.com/gorilla/mux"
)

var baseUrl = ""

func main() {
	baseUrl = getenv()
	r := mux.NewRouter()
	svc := new(server)
	r.HandleFunc(baseUrl+"/cart", svc.viewCart).Methods(http.MethodGet, http.MethodHead)
	r.HandleFunc(baseUrl+"/cart", svc.addToCart).Methods(http.MethodPost)
	http.HandleFunc("PUT /items/{id}", putItem)
	http.HandleFunc("/static/{path...}", func(w http.ResponseWriter, r *http.Request) {})
}

func putItem(w http.ResponseWriter, r *http.Request) {}
"""

GO_HANDLERS = """
package main

import "net/http"

type server struct{}

func (s *server) viewCart(w http.ResponseWriter, r *http.Request) {}

func (s *server) addToCart(w http.ResponseWriter, r *http.Request) {}

type admin struct{}

// A second addToCart in the package: the handler name is not unique.
func (a *admin) addToCart(w http.ResponseWriter, r *http.Request) {}

func call(id string) {
	req, _ := http.NewRequest(http.MethodPut, "http://frontend/items/"+id, nil)
	_ = req
	http.Post("http://" + addr, "application/json", nil)
}
"""

LOCUST = """
from locust import HttpUser, task

class User(HttpUser):
    @task
    def cart(self):
        self.client.get("/cart")
        self.client.post("/cart", {"product_id": 1})
        self.client.get("/static/css/a.css")
"""

TEST = """
import requests

def test_pay():
    requests.get("http://user:8080/check/1")
"""


def _write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class FoldingTests(unittest.TestCase):
    def fold(self, language, source, suffix):
        content = source.encode()
        tree = make_parser(language, suffix).parse(content)
        folder = Folder(language, tree.root_node, content)
        found = []
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type in ("call", "call_expression"):
                arguments = node.child_by_field_name("arguments")
                if arguments is not None and arguments.named_children:
                    found.append(folder.evaluate(arguments.named_children[0]))
            stack.extend(node.named_children)
        return sorted(found)

    def test_python(self):
        source = (
            "A = 'http://a'\n"
            "B = 'x'\n"
            "B = 'y'\n"
            "def f(p):\n"
            "    c = A + '/c'\n"
            "    g(c + '/' + p)\n"
            "    g(B)\n"
            "    g('%s/%d' % (p, 1))\n"
            "    g('{}/z'.format(p))\n"
            "    g(f'{A}/{p}')\n"
            "    g(b'raw')\n"
            "    g(d)\n"
            "    d = '/late'\n"
        )
        self.assertEqual(
            self.fold("python", source, ".py"),
            # f-string and %-format fields are unknown text, as is d (bound
            # after its use) and B (bound twice); b"raw" is not text.
            sorted([U, U, U, U, U + "/" + U, U + "/" + U, U + "/z", "http://a/c/" + U]),
        )

    def test_global_rebinding_is_unknown(self):
        source = "A = '/a'\ndef f():\n    global A\n    A = '/b'\ndef g():\n    h(A)\n"
        self.assertEqual(self.fold("python", source, ".py"), [U])
        source = "A = '/a'\ndef f():\n    A = '/b'\ndef g():\n    h(A)\n"
        self.assertEqual(self.fold("python", source, ".py"), ["/a"])

    def test_conditionals_with_one_value(self):
        source = (
            "const prod = process.env.NODE_ENV === 'production';\n"
            "const host = '//localhost:8100';\n"
            "f(prod ? '//localhost:8100' : host);\n"
            "f(prod ? '//a' : '//b');\n"
            "f(prod ? x : x);\n"
        )
        # Equal literal branches fold; different or unknown ones do not.
        self.assertEqual(
            self.fold("javascript", source, ".js"), sorted(["//localhost:8100", U, U])
        )
        source = "g('/a' if p else '/a')\ng('/a' if p else '/b')\n"
        self.assertEqual(self.fold("python", source, ".py"), sorted(["/a", U]))

    def test_javascript_and_go(self):
        source = "const a = '/a';\nlet b = a;\nb = '/x';\nf(`${a}/t`);\nf(b);\nf(a + '/' + c);\n"
        self.assertEqual(
            self.fold("javascript", source, ".js"), sorted(["/a/t", U, "/a/" + U])
        )
        source = (
            'package p\nconst base = "http://h"\n'
            'func f(id string) {\n\tu := base + "/x/" + id\n'
            '\tg(u)\n\tg(fmt.Sprintf("%s/y/%d", base, 3))\n}\n'
        )
        self.assertEqual(
            self.fold("go", source, ".go"),
            sorted(["http://h/x/" + U, U + "/y/" + U, "%s/y/%d"]),
        )


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class WebContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        _write(
            self.root,
            {
                "orders/app.py": FLASK,
                "payment/payment.py": PAYMENT,
                "payment/test_payment.py": TEST,
                "catalogue/server.js": EXPRESS,
                "frontend/main.go": GO_SERVER,
                "frontend/handlers.go": GO_HANDLERS,
                "loadgen/locustfile.py": LOCUST,
                # Without a locust import, self.client is not an HTTP client.
                "loadgen/other.py": (
                    "class C:\n    def f(self):\n        self.client.get('/cart')\n"
                ),
            },
        )
        self.tool = ServiceContractTool(self.root)
        self.output = asyncio.run(self.tool.analyze("r"))
        _, self.routes, self.requests, self.unread, self.resolutions = (
            self.tool.last_facts
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_routes(self):
        routes = {(r.framework, r.methods, r.path, r.base_unknown) for r in self.routes}
        self.assertEqual(
            routes,
            {
                ("flask", ("DELETE", "GET"), "/orders/{var}", False),
                ("flask", ("POST",), "/orders/{var}/items/{*rest}", False),
                ("flask", ("GET",), "/", False),
                ("express", ("GET",), "/products/{var}", False),
                ("express", ("DELETE",), "/products/{var}", False),
                ("express", ("*",), "/health", False),
                ("go-net-http", ("GET", "HEAD"), "/cart", True),
                ("go-net-http", ("POST",), "/cart", True),
                ("go-net-http", ("PUT",), "/items/{id}", False),
                ("go-net-http", ("*",), "/static/{path...}", False),
            },
        )
        self.assertEqual(
            [(u.file, u.reason) for u in self.unread],
            [("orders/app.py", "path_not_literal")],
        )

    def test_handlers(self):
        handlers = {
            r.target_match["qualified_name"].split("::")[1]
            + " "
            + r.source_match["qualified_name"].split("::")[2]
            for r in self.output.relationships
            if r.type == "HANDLES"
        }
        self.assertEqual(
            handlers,
            {
                "orders/app.py order",
                "orders/app.py add_item",
                "orders/app.py create_app.index",
                "catalogue/server.js getProduct",
                # Closures have no declaration: the route hangs off its file.
                "catalogue/server.js file",
                "frontend/main.go server.viewCart",
                "frontend/main.go putItem",
                "frontend/main.go file",
            },
        )
        rules = {
            r.properties["rule_id"]
            for r in self.output.relationships
            if r.type == "HANDLES"
        }
        for rule in rules:
            self.assertEqual(get_rule(rule).evidence_type, "pattern_matched")
        self.assertEqual(len(rules), 3)

    def test_requests_link_across_languages(self):
        routes = {r.id: r for r in self.routes}
        requests = {r.id: r for r in self.requests}
        linked = sorted(
            (
                requests[x.request_id].file,
                requests[x.request_id].method,
                routes[x.route_id].framework,
                routes[x.route_id].path,
            )
            for x in self.resolutions
            if x.status == "linked"
        )
        self.assertEqual(
            linked,
            [
                ("catalogue/server.js", "DELETE", "flask", "/orders/{var}"),
                ("catalogue/server.js", "GET", "express", "/products/{var}"),
                ("catalogue/server.js", "GET", "flask", "/orders/{var}"),
                ("frontend/handlers.go", "PUT", "go-net-http", "/items/{id}"),
                ("loadgen/locustfile.py", "GET", "go-net-http", "/cart"),
                ("loadgen/locustfile.py", "GET", "go-net-http", "/static/{path...}"),
                ("loadgen/locustfile.py", "POST", "go-net-http", "/cart"),
                ("payment/payment.py", "POST", "flask", "/orders/{var}/items/{*rest}"),
            ],
        )
        unresolved = sorted(
            (requests[x.request_id].file, x.reason)
            for x in self.resolutions
            if x.status != "linked"
        )
        self.assertEqual(
            unresolved,
            [
                # axios.post('/products/' + sku): no POST route.
                ("catalogue/server.js", "no_route"),
                # "http://" + addr: the path is not separated from the host.
                ("frontend/handlers.go", "no_literal_path"),
                ("payment/payment.py", "no_literal_path"),
                ("payment/payment.py", "no_route"),
                ("payment/payment.py", "no_route"),
            ],
        )
        # Test files are not read.
        self.assertNotIn("payment/test_payment.py", {r.file for r in self.requests})
        self.assertEqual(
            self.output.metadata.custom_metrics["test_source_files_skipped"], 1
        )

    def test_graph_joins_tree_sitter_declarations(self):
        trees = asyncio.run(
            TreeSitterTool(self.root).analyze(
                "r", languages={"python", "javascript", "go"}
            )
        )
        self.output.metadata.custom_metrics["snapshot_id"] = (
            trees.metadata.custom_metrics["snapshot_id"]
        )
        merged, _ = _combine([trees, self.output], "r", "rev", WriteCoordinatorConfig())
        symbols = {
            n.properties["qualified_name"] for n in trees.nodes if "Symbol" in n.labels
        }
        contract_symbols = {
            n.properties["qualified_name"]
            for n in self.output.nodes
            if "Symbol" in n.labels
        }
        self.assertTrue(contract_symbols <= symbols)
        self.assertEqual(
            len([n for n in merged.nodes if "Symbol" in n.labels]), len(symbols)
        )

    def test_deterministic(self):
        again = asyncio.run(ServiceContractTool(self.root).analyze("r"))
        self.assertEqual(
            [n.properties for n in again.nodes],
            [n.properties for n in self.output.nodes],
        )
        self.assertEqual(
            [r.properties for r in again.relationships],
            [r.properties for r in self.output.relationships],
        )


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class ImportCommentTests(unittest.TestCase):
    def test_comment_inside_import_braces(self):
        # JHipster frontends keep a "needle" comment between the braces.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(
                root,
                {
                    "web/package.json": '{"name": "web"}',
                    "web/src/server.ts": (
                        "import express from 'express';\n"
                        "import {\n  faA,\n  // jhipster-needle-add-icon-import\n"
                        "  faB,\n} from 'icons';\n"
                        "const app = express();\n"
                        "app.get('/api/orders', function list(req, res) {});\n"
                    ),
                },
            )
            output = asyncio.run(ServiceContractTool(root).analyze("r"))
        routes = {
            n.properties.get("name") for n in output.nodes if "HttpRoute" in n.labels
        }
        self.assertIn("GET /api/orders", routes)


if __name__ == "__main__":
    unittest.main()
