"""gRPC contracts: .proto services, their servers and stub calls."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.analysis_tools.evidence import get_rule
from deepthought.service_contracts.grpc import (
    GO_REGISTER,
    GO_REGISTER_CONSTRUCTOR,
    JAVA_IMPL_BASE,
    NODE_ADD_SERVICE,
    NODE_STATIC_ADD_SERVICE,
    PYTHON_SERVICER,
    STUB_CALL,
    ProtoIndex,
    ProtoMethod,
    read_proto,
)

READY = all(
    importlib.util.find_spec(m)
    for m in (
        "tree_sitter",
        "tree_sitter_go",
        "tree_sitter_java",
        "tree_sitter_javascript",
        "tree_sitter_python",
    )
)

PROTO = """\
syntax = "proto3";
// service Fake { rpc Nope (A) returns (B) {} }
package shop;
option go_package = "x/y;pb"; /* service Hidden { rpc No(A) returns (B); } */

service CartService {
    rpc AddItem(AddItemRequest) returns (Empty) {}
    rpc GetCart (GetCartRequest) returns (Cart) {
        option (google.api.http) = { get: "/v1/cart" };
    }
}

message Empty {}

service CurrencyService {
  rpc GetSupportedCurrencies(Empty) returns (Currencies) {}
  rpc Convert(ConversionRequest) returns (Money) {}
}

service EmailService { rpc SendOrderConfirmation(Req) returns (Empty) {} }
service AdService { rpc GetAds(AdRequest) returns (AdResponse) {} }
"""

OTHER_PROTO = """\
package other;
service CartService { rpc GetCart(A) returns (B); }
service PingService { rpc Ping(A) returns (B); }
"""

GO_SERVER = """\
package main

func main() {
\tsvc := &cartServer{}
\tpb.RegisterCartServiceServer(srv, svc)
\thealthpb.RegisterHealthServer(srv, health.NewServer())
}
"""

GO_METHODS = """\
package main

type cartServer struct{}

func (c *cartServer) AddItem(ctx context.Context, r *pb.AddItemRequest) (*pb.Empty, error) {
\treturn nil, nil
}

func (c *cartServer) helper() {}
"""

GO_CLIENT = """\
package main

type frontend struct {
\tcurrency pb.CurrencyServiceClient
}

func (f *frontend) currencies(ctx context.Context) {
\tcurrs, _ := pb.NewCurrencyServiceClient(f.conn).
\t\tGetSupportedCurrencies(ctx, &pb.Empty{})
\t_ = currs
\tf.currency = pb.NewCurrencyServiceClient(f.conn)
\tf.currency.Convert(ctx, nil)
\tpb.NewCurrencyServiceClient(f.conn).Refund(ctx, nil)
\tother := pb.NewCurrencyServiceClient(f.conn)
\tother = wrap(other)
\tother.Convert(ctx, nil)
}
"""

GO_GENERATED = """\
package pb

func (c *cartServiceClient) GetCart(ctx context.Context) {
\tc.cc.Invoke(ctx)
}

func f() { NewCartServiceClient(cc).GetCart(nil) }
"""

PY_SERVER = """\
import demo_pb2_grpc


class BaseEmail(demo_pb2_grpc.EmailServiceServicer):
    def Check(self, request, context):
        pass


class RealEmail(BaseEmail):
    def SendOrderConfirmation(self, request, context):
        pass


stub = demo_pb2_grpc.CurrencyServiceStub(channel)


def convert():
    return stub.Convert(None)
"""

JAVA = """\
package shop;

public final class Ads {
  private static class AdServiceImpl extends shop.AdServiceGrpc.AdServiceImplBase {
    @Override
    public void getAds(AdRequest req, StreamObserver<AdResponse> out) {}
  }

  private CartServiceGrpc.CartServiceBlockingStub cart;

  void call() {
    cart = CartServiceGrpc.newBlockingStub(channel);
    cart.getCart(null);
  }
}
"""

NODE = """\
function getSupportedCurrencies(call, callback) {}

class Server {
  static ConvertHandler(call, callback) {}

  listen() {
    this.server.addService(shopProto.CurrencyService.service, {
      getSupportedCurrencies,
      convert: Server.ConvertHandler.bind(this),
    });
    this.server.addService(shopProto.CartService.service, { getCart: () => {} });
  }
}

const sdk = new opentelemetry.NodeSDK({});
sdk.start();
const client = new shopProto.CurrencyService('localhost:7000', creds);
client.convert({}, () => {});
"""


def _write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class ProtoReaderTests(unittest.TestCase):
    def test_services_and_rpcs_with_lines(self):
        package, methods = read_proto(PROTO, "demo.proto")
        self.assertEqual(package, "shop")
        self.assertEqual(
            [(m.full_name, m.line) for m in methods],
            [
                ("shop.CartService/AddItem", 7),
                ("shop.CartService/GetCart", 8),
                ("shop.CurrencyService/GetSupportedCurrencies", 16),
                ("shop.CurrencyService/Convert", 17),
                ("shop.EmailService/SendOrderConfirmation", 20),
                ("shop.AdService/GetAds", 21),
            ],
        )

    def test_comment_markers_inside_strings_are_kept(self):
        text = 'package a;\noption x = "//no";\nservice S { rpc M(A) returns (B); }\n'
        self.assertEqual(
            [m.full_name for m in read_proto(text, "a.proto")[1]], ["a.S/M"]
        )

    def test_index_needs_one_package_per_simple_name(self):
        index = ProtoIndex(
            read_proto(PROTO, "a.proto")[1]
            + read_proto(PROTO, "copy/a.proto")[1]
            + read_proto(OTHER_PROTO, "b.proto")[1]
        )
        self.assertEqual(
            index.service("CurrencyService"), ("shop.CurrencyService", None)
        )
        self.assertEqual(index.service("CartService"), (None, "several_proto_packages"))
        self.assertEqual(index.service("Nope"), (None, "no_proto_service"))
        self.assertEqual(len(index.methods[("shop.CurrencyService", "Convert")]), 2)
        self.assertEqual(index.method("shop.CurrencyService", "convert"), "Convert")
        self.assertIsNone(index.method("shop.CurrencyService", "refund"))

    def test_lower_camel_must_be_unique(self):
        index = ProtoIndex(
            [ProtoMethod("p.S", "Get", "a", 1), ProtoMethod("p.S", "get", "a", 2)]
        )
        self.assertEqual(index.method("p.S", "get"), "get")
        self.assertEqual(index.method("p.S", "Get"), "Get")


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class GrpcContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        _write(
            self.root,
            {
                "protos/demo.proto": PROTO,
                "cart/demo.proto": PROTO,
                "node_modules/x/demo.proto": OTHER_PROTO,
                "cart/main.go": GO_SERVER,
                "cart/server.go": GO_METHODS,
                "cart/server_test.go": GO_CLIENT,
                "frontend/rpc.go": GO_CLIENT,
                "frontend/genproto/demo.pb.go": GO_GENERATED,
                "email/server.py": PY_SERVER,
                "email/demo_pb2_grpc.py": "class CartServiceStub:\n    pass\n",
                "ads/src/main/java/shop/Ads.java": JAVA,
                "currency/server.js": NODE,
            },
        )

    def tearDown(self):
        self.directory.cleanup()

    def analyze(self):
        tool = ServiceContractTool(self.root)
        output = asyncio.run(tool.analyze("r"))
        return tool, output

    def edges(self, output, kind):
        def name(qualified):
            parts = qualified.split("::")
            return parts[1] if parts[2] == "file" else parts[2]

        return sorted(
            (
                name(r.source_match["qualified_name"]),
                r.target_match["qualified_name"].rpartition("::")[2],
                r.properties["rule_id"],
            )
            for r in output.relationships
            if r.type == kind and r.target_labels == ["GrpcMethod", "Contract"]
        )

    def test_methods_are_merged_across_proto_copies(self):
        _, output = self.analyze()
        methods = {
            n.properties["name"]: n.properties
            for n in output.nodes
            if "GrpcMethod" in n.labels
        }
        self.assertEqual(len(methods), 6)  # node_modules is not read
        convert = methods["shop.CurrencyService/Convert"]
        self.assertEqual(
            convert["qualified_name"], "r::grpc::shop.CurrencyService/Convert"
        )
        self.assertEqual(
            convert["declared_in"], ["cart/demo.proto:17", "protos/demo.proto:17"]
        )
        self.assertEqual(
            (convert["file_path"], convert["line"]), ("cart/demo.proto", 17)
        )

    def test_servers(self):
        _, output = self.analyze()
        self.assertEqual(
            self.edges(output, "HANDLES"),
            [
                ("Ads.AdServiceImpl.getAds", "shop.AdService/GetAds", JAVA_IMPL_BASE),
                (
                    "RealEmail.SendOrderConfirmation",
                    "shop.EmailService/SendOrderConfirmation",
                    PYTHON_SERVICER,
                ),
                (
                    "Server.ConvertHandler",
                    "shop.CurrencyService/Convert",
                    NODE_ADD_SERVICE,
                ),
                ("cartServer.AddItem", "shop.CartService/AddItem", GO_REGISTER),
                (
                    "getSupportedCurrencies",
                    "shop.CurrencyService/GetSupportedCurrencies",
                    NODE_ADD_SERVICE,
                ),
            ],
        )

    def test_stub_calls(self):
        tool, output = self.analyze()
        self.assertEqual(
            self.edges(output, "REQUESTS"),
            [
                ("Ads.call", "shop.CartService/GetCart", STUB_CALL),
                ("convert", "shop.CurrencyService/Convert", STUB_CALL),
                ("currency/server.js", "shop.CurrencyService/Convert", STUB_CALL),
                # Once from frontend/rpc.go and once from cart/server_test.go,
                # whose stub calls are read and marked test_source.
                ("frontend.currencies", "shop.CurrencyService/Convert", STUB_CALL),
                ("frontend.currencies", "shop.CurrencyService/Convert", STUB_CALL),
                (
                    "frontend.currencies",
                    "shop.CurrencyService/GetSupportedCurrencies",
                    STUB_CALL,
                ),
                (
                    "frontend.currencies",
                    "shop.CurrencyService/GetSupportedCurrencies",
                    STUB_CALL,
                ),
            ],
        )
        self.assertEqual(
            sorted(
                r.properties["line"]
                for r in output.relationships
                if r.properties.get("test_source")
            ),
            [9, 12],
        )
        chained = [
            r
            for r in output.relationships
            if r.properties.get("request") == "CurrencyService.GetSupportedCurrencies"
        ]
        self.assertEqual(chained[0].properties["line"], 9)  # the method's name
        metrics = output.metadata.custom_metrics
        self.assertEqual(
            metrics["grpc_call_resolution"],
            {"linked": 7, "unresolved:no_proto_method": 2},
        )
        # RegisterHealthServer(srv, health.NewServer()) names no declared type;
        # an inline arrow function handler has no declaration to link.
        self.assertEqual(
            metrics["unread"],
            {
                "grpc_server:handler_not_declared": 1,
                "grpc_server:implementation_not_read": 1,
            },
        )
        self.assertEqual(
            sorted(metrics["proto_files"]), ["cart/demo.proto", "protos/demo.proto"]
        )
        files = {
            n.properties["path"]: n.properties
            for n in output.nodes
            if "File" in n.labels
        }
        self.assertEqual(
            files["frontend/rpc.go"]["unresolved_grpc_calls"],
            ["CurrencyService.Refund"],
        )
        # Generated code is neither server nor client; a Go test is only a
        # client, marked test_source.
        called = {(c.file, c.test_source) for c in tool.last_grpc.calls}
        self.assertEqual(
            called,
            {
                ("ads/src/main/java/shop/Ads.java", False),
                ("cart/server_test.go", True),
                ("currency/server.js", False),
                ("email/server.py", False),
                ("frontend/rpc.go", False),
            },
        )

    def test_ambiguous_service_links_nothing(self):
        _write(self.root, {"other/other.proto": OTHER_PROTO})
        tool, output = self.analyze()
        targets = {
            e[1] for e in self.edges(output, "REQUESTS") + self.edges(output, "HANDLES")
        }
        self.assertFalse(any("CartService" in t for t in targets))
        self.assertEqual(
            output.metadata.custom_metrics["grpc_call_resolution"][
                "unresolved:several_proto_packages"
            ],
            1,
        )

    def test_edges_carry_registered_evidence_only(self):
        _, output = self.analyze()
        grpc = [
            r
            for r in output.relationships
            if r.target_labels == ["GrpcMethod", "Contract"]
        ]
        self.assertTrue(grpc)
        for relationship in grpc:
            rule = get_rule(relationship.properties["rule_id"])
            self.assertEqual(
                relationship.properties["evidence_type"], rule.evidence_type
            )
            self.assertEqual(rule.evidence_type, "pattern_matched")
            self.assertNotIn("confidence", relationship.properties)
        anchors = {n.properties["qualified_name"] for n in output.nodes}
        for relationship in grpc:
            self.assertIn(relationship.source_match["qualified_name"], anchors)
            self.assertIn(relationship.target_match["qualified_name"], anchors)

    def test_deterministic(self):
        def run():
            _, output = self.analyze()
            return (
                [
                    (n.labels, sorted(n.properties.items(), key=str))
                    for n in output.nodes
                ],
                [
                    (
                        r.type,
                        r.source_match,
                        r.target_match,
                        r.properties,
                    )
                    for r in output.relationships
                ],
            )

        self.assertEqual(run(), run())


STREAM_PROTO = """\
package route;
service Guide {
  rpc Get(Point) returns (Feature) {}
  rpc List(Rect) returns (stream Feature) {}
  rpc Record(stream Point) returns (Summary) {}
  rpc Chat ( stream Note ) returns ( stream .route.Note ) {}
}
"""

# A copy that disagrees about Get: the kind is left out.
STREAM_COPY = STREAM_PROTO.replace("returns (Feature)", "returns (stream Feature)")

GO_ROUTE_MAIN = """\
package main

func main() {
\tpb.RegisterGuideServer(s, newServer())
}
"""

GO_ROUTE_SERVER = """\
package main

type guideServer struct{}

func newServer() *guideServer { return &guideServer{} }

func (g *guideServer) Get(ctx context.Context, p *pb.Point) (*pb.Feature, error) {
\treturn nil, nil
}
func (g *guideServer) List(r *pb.Rect, stream pb.Guide_ListServer) error { return nil }
func (g *guideServer) Record(stream pb.Guide_RecordServer) error { return nil }
func (g *guideServer) Chat(stream pb.Guide_ChatServer) error { return nil }
"""

# The constructor returns an interface of the package: not read.
GO_INTERFACE = """\
package iface

type guide interface {
\tGet(ctx context.Context, p *pb.Point) (*pb.Feature, error)
}

func newGuide() guide { return nil }

func main() {
\tpb.RegisterGuideServer(s, newGuide())
}
"""

# Two files of one package declare newServer (build tags): not read.
GO_TAGGED = """\
package tagged

type guideServer struct{}

func newServer() *guideServer { return &guideServer{} }

func (g *guideServer) Get(ctx context.Context, p *pb.Point) (*pb.Feature, error) {
\treturn nil, nil
}
"""


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class GrpcStreamingTests(unittest.TestCase):
    def test_streaming_kinds(self):
        self.assertEqual(
            [(m.method, m.streaming) for m in read_proto(STREAM_PROTO, "r.proto")[1]],
            [
                ("Get", "unary"),
                ("List", "server"),
                ("Record", "client"),
                ("Chat", "bidi"),
            ],
        )

    def test_go_constructors_and_streaming_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(
                root,
                {
                    "route.proto": STREAM_PROTO,
                    "copy/route.proto": STREAM_COPY,
                    "route/main.go": GO_ROUTE_MAIN,
                    "route/server.go": GO_ROUTE_SERVER,
                    "iface/main.go": GO_INTERFACE,
                    "tagged/a.go": GO_TAGGED,
                    "tagged/b.go": GO_TAGGED.replace("type guideServer struct{}", "")
                    .replace("func (g", "func (h")
                    .replace("Get(", "Other("),
                    "tagged/main.go": GO_ROUTE_MAIN.replace("main", "tagged", 1),
                },
            )
            output = asyncio.run(ServiceContractTool(root).analyze("r"))
        streaming = {
            n.properties["method"]: n.properties.get("streaming")
            for n in output.nodes
            if "GrpcMethod" in n.labels
        }
        self.assertEqual(
            streaming,
            {"Get": None, "List": "server", "Record": "client", "Chat": "bidi"},
        )
        handles = sorted(
            (
                r.source_match["qualified_name"].split("::")[2],
                r.properties["rule_id"],
            )
            for r in output.relationships
            if r.type == "HANDLES"
        )
        self.assertEqual(
            handles,
            [
                (f"guideServer.{m}", GO_REGISTER_CONSTRUCTOR)
                for m in ("Chat", "Get", "List", "Record")
            ],
        )
        self.assertEqual(
            output.metadata.custom_metrics["unread"],
            {"grpc_server:implementation_not_read": 2},
        )
        self.assertEqual(
            get_rule(GO_REGISTER_CONSTRUCTOR).evidence_type, "pattern_matched"
        )


NODE_STATIC_SERVER = """\
var services = require('./route_grpc_pb');

function get(call, callback) {}
function chat(call) {}

function main() {
  var server = new grpc.Server();
  server.addService(services.GuideService, {get: get, chat: chat});
}
"""

NODE_STATIC_CLIENT = """\
import { GuideClient as Client } from './gen/route_grpc_pb.js';
import * as lookalike from './route_client';

const client = new Client('localhost:50051', credentials);

export function run() {
  client.list({});
  // Not a grpc-tools module: not a stub.
  new lookalike.GuideClient('x').record();
}
"""


@unittest.skipUnless(
    READY and importlib.util.find_spec("tree_sitter_typescript"),
    "tree-sitter grammars are not installed",
)
class GrpcNodeStaticCodegenTests(unittest.TestCase):
    def test_static_service_and_client(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(
                root,
                {
                    "route.proto": STREAM_PROTO,
                    "server/server.js": NODE_STATIC_SERVER,
                    "server/route_grpc_pb.js": "exports.GuideService = {};",
                    "web/client.ts": NODE_STATIC_CLIENT,
                },
            )
            output = asyncio.run(ServiceContractTool(root).analyze("r"))
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[2],
                r.target_match["qualified_name"].rpartition("::")[2],
                r.properties["rule_id"],
            )
            for r in output.relationships
            if r.target_labels == ["GrpcMethod", "Contract"]
        )
        self.assertEqual(
            edges,
            [
                ("HANDLES", "chat", "route.Guide/Chat", NODE_STATIC_ADD_SERVICE),
                ("HANDLES", "get", "route.Guide/Get", NODE_STATIC_ADD_SERVICE),
                ("REQUESTS", "run", "route.Guide/List", STUB_CALL),
            ],
        )
        self.assertEqual(
            get_rule(NODE_STATIC_ADD_SERVICE).evidence_type, "pattern_matched"
        )


if __name__ == "__main__":
    unittest.main()
