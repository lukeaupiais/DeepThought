"""Reading .proto files with protoc (grpcio-tools)."""

import asyncio
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool

from deepthought.service_contracts.protoc import available, read_protos
from typescript_ready import needs_typescript

COMMON = """
syntax = "proto3";
package common.v1;
import "google/protobuf/empty.proto";
message Money { string currency = 1; int64 units = 2; }
service Health {
  rpc Check (google.protobuf.Empty) returns (google.protobuf.Empty);
}
"""

SHOP = """
syntax = "proto3";
package shop.v1;

import "common/v1/common.proto";

// rpc Commented (A) returns (B);
service Cart {
  rpc Add (Item) returns (common.v1.Money);
  rpc Watch (Item) returns (stream common.v1.Money);
  rpc Upload (stream Item)
      returns (common.v1.Money);
  rpc Chat (stream Item) returns (stream Item);
}

message Item { string id = 1; }
"""

BROKEN = """
syntax = "proto3";
package broken;
import "google/api/annotations.proto";
service Gateway {
  rpc Get (A) returns (B);
}
"""


def _write(root, files):
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


@unittest.skipUnless(available(), "grpcio-tools is not installed")
class ProtocReaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.files = {
            "proto/common/v1/common.proto": COMMON,
            "services/cart/api/shop.proto": SHOP,
            "gateway/broken.proto": BROKEN,
        }
        _write(self.root, self.files)
        self.read = read_protos(self.root, sorted(self.files), self.files)

    def tearDown(self):
        self.directory.cleanup()

    def test_imports_resolve_across_directories(self):
        package, methods, bindings = self.read["services/cart/api/shop.proto"]
        self.assertEqual(package, "shop.v1")
        self.assertEqual(
            [(m.full_name, m.line, m.streaming) for m in methods],
            [
                ("shop.v1.Cart/Add", 9, "unary"),
                ("shop.v1.Cart/Watch", 10, "server"),
                ("shop.v1.Cart/Upload", 11, "client"),
                ("shop.v1.Cart/Chat", 13, "bidi"),
            ],
        )
        self.assertEqual(bindings, [])
        _, health, _ = self.read["proto/common/v1/common.proto"]
        self.assertEqual([m.full_name for m in health], ["common.v1.Health/Check"])

    def test_unresolved_import_is_not_read(self):
        self.assertNotIn("gateway/broken.proto", self.read)

    def test_rejected_file_is_unread(self):
        output = asyncio.run(ServiceContractTool(self.root).analyze("r"))
        metrics = output.metadata.custom_metrics
        self.assertEqual(metrics["proto_readers"], {"not_compiled": 1, "protoc": 2})
        self.assertEqual(metrics["unread"].get("grpc_proto:not_compiled"), 1)
        methods = sorted(
            n.properties["qualified_name"]
            for n in output.nodes
            if "GrpcMethod" in n.labels
        )
        # What protoc rejects declares nothing.
        self.assertNotIn("r::grpc::broken.Gateway/Get", methods)

    def test_missing_protoc_fails_loudly(self):
        from unittest import mock

        from deepthought.service_contracts import protoc

        with mock.patch.object(protoc, "available", return_value=None):
            with self.assertRaises(protoc.ProtocMissing):
                read_protos(self.root, sorted(self.files), self.files)


if __name__ == "__main__":
    unittest.main()


ANNOTATED = """
syntax = "proto3";
package shop.admin.v1;
import "google/api/annotations.proto";

service ShopAdmin {
  rpc Login (Req) returns (Reply) {
    option (google.api.http) = { post: "/admin/v1/login" body: "*" };
  }
  rpc GetBook (Req) returns (Reply) {
    option (google.api.http) = {
      get: "/admin/v1/{name=shelves/*/books/*}"
      additional_bindings { get: "/admin/v1/books/{id}:peek" }
    };
  }
}
message Req {}
message Reply {}
"""

SHOP_INTERFACE = """
syntax = "proto3";
package shop.interface.v1;
import "google/api/annotations.proto";

service ShopInterface {
  rpc Login (Req) returns (Reply) {
    option (google.api.http) = { post: "/v1/login" body: "*" };
  }
}
message Req {}
message Reply {}
"""

ADMIN_HTTP = """
package server

import v1 "example.com/shop/api/shop/admin/v1"

func NewHTTPServer(s *service.ShopAdmin) *http.Server {
	srv := http.NewServer()
	v1.RegisterShopAdminHTTPServer(srv, s)
	return srv
}
"""

SHOP_HTTP = ADMIN_HTTP.replace("ShopAdmin", "ShopInterface").replace(
    "admin", "interface"
)

ADMIN_WEB = """
import axios from 'axios';
const service = axios.create({
  baseURL: process.env.NODE_ENV === 'production' ? '//localhost:8100' : '//localhost:8100',
});
export const login = () => service.post('/admin/v1/login', {});
export const stale = () => service.post('/v1/login', {});
export const book = (id) => service.get('/admin/v1/books/' + id + ':peek');
"""


def _google_api():
    try:
        from google.api import annotations_pb2  # noqa: F401
        import yaml  # noqa: F401
    except ImportError:
        return False
    return available() is not None


@unittest.skipUnless(_google_api(), "googleapis-common-protos or pyyaml missing")
@needs_typescript
class HttpAnnotationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        _write(
            root,
            {
                "api/shop/admin/v1/admin.proto": ANNOTATED,
                "api/shop/interface/v1/shop.proto": SHOP_INTERFACE,
                "app/admin/configs/config.yaml": "server:\n  http:\n    addr: 0.0.0.0:8100\n",
                "app/admin/internal/server/http.go": ADMIN_HTTP,
                "app/shop/configs/config.yaml": "server:\n  http:\n    addr: 0.0.0.0:8000\n",
                "app/shop/internal/server/http.go": SHOP_HTTP,
                "web/admin/package.json": '{"dependencies": {"axios": "1"}}',
                "web/admin/src/api.js": ADMIN_WEB,
            },
        )
        self.tool = ServiceContractTool(root)
        self.output = asyncio.run(self.tool.analyze("r"))

    def tearDown(self):
        self.directory.cleanup()

    def test_routes_from_annotations(self):
        _, routes, _, _, _ = self.tool.last_facts
        self.assertEqual(
            sorted(
                (r.methods, r.path, r.rpc)
                for r in routes
                if r.framework == "grpc-http-annotation"
            ),
            [
                (
                    ("GET",),
                    "/admin/v1/books/{id}:peek",
                    "shop.admin.v1.ShopAdmin/GetBook",
                ),
                (
                    ("GET",),
                    "/admin/v1/shelves/{name}/books/{name}",
                    "shop.admin.v1.ShopAdmin/GetBook",
                ),
                (("POST",), "/admin/v1/login", "shop.admin.v1.ShopAdmin/Login"),
                (("POST",), "/v1/login", "shop.interface.v1.ShopInterface/Login"),
            ],
        )
        # The gRPC method handles its HTTP routes.
        handles = [
            (r.source_match["qualified_name"], r.target_match["qualified_name"])
            for r in self.output.relationships
            if r.type == "HANDLES" and r.source_labels[0] == "GrpcMethod"
        ]
        self.assertEqual(len(handles), 4)
        self.assertIn("r::grpc::shop.admin.v1.ShopAdmin/Login", dict(handles))

    def test_loopback_port_narrows_to_the_listening_program(self):
        _, routes, requests, _, resolutions = self.tool.last_facts
        by_id = {r.id: r for r in routes}
        found = {
            q.url.rpartition("8100")[2]: (
                s.status,
                s.reason,
                s.rule_id,
                by_id[s.route_id].rpc if s.route_id else None,
            )
            for q, s in zip(requests, resolutions)
        }
        self.assertEqual(
            found["/admin/v1/login"],
            (
                "linked",
                None,
                "service-contracts.http-local-port",
                "shop.admin.v1.ShopAdmin/Login",
            ),
        )
        # Port 8100 is the admin program, which serves no /v1/login: the
        # shop's route on port 8000 is not a candidate.
        self.assertEqual(
            found["/v1/login"], ("unresolved", "no_route_on_port", None, None)
        )
        self.assertEqual(found["/admin/v1/books/\x00:peek"][0], "linked")
