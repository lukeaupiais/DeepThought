"""C# gRPC servers (classes deriving from the X.XBase class Grpc.Tools
generates) and clients (values of its X.XClient class)."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.service_contracts.grpc import CSHARP_SERVICE_BASE

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_c_sharp"
)

PROTO = """
syntax = "proto3";
package shop;
service CartService {
  rpc AddItem(Req) returns (Res);
  rpc GetCart(Req) returns (Res);
}
service AdService {
  rpc GetAds(Req) returns (Res);
}
message Req {}
message Res {}
"""

SERVER = """
using Grpc.Core;

namespace cart.services
{
    public class CartService : Shop.CartService.CartServiceBase
    {
        public override Task<Res> AddItem(Req request, ServerCallContext context)
        {
            return null;
        }

        public override Task<Res> GetCart(Req request, ServerCallContext context)
        {
            return null;
        }

        private void Helper() { }
    }

    public class Logged : CartService
    {
    }

    // Not a generated base: XBase must be nested in X.
    public class Other : AdServiceBase
    {
        public override Task<Res> GetAds(Req request, ServerCallContext context)
        {
            return null;
        }
    }
}
"""

CLIENT = """
using CartClient = Shop.CartService.CartServiceClient;

namespace web
{
    public class Basket(CartClient cart)
    {
        private readonly Shop.AdService.AdServiceClient _ads;
        private Shop.CartService.CartServiceClient _cached;

        public Task Load() => cart.GetCartAsync(new Req());

        public void Ads() { this._ads.GetAds(new Req()); }

        private Shop.CartService.CartServiceClient Client()
        {
            _cached = new Shop.CartService.CartServiceClient(channel);
            return _cached;
        }

        public void Add()
        {
            Client()
                .AddItemAsync(new Req());
        }

        public void Locals()
        {
            var client = new Shop.AdService.AdServiceClient(channel);
            client.GetAds(new Req());
        }

        public void OtherLocals()
        {
            var client = new Shop.CartService.CartServiceClient(channel);
            client.GetCart(new Req());
            client.WithHost("x");  // a ClientBase method, not an rpc
        }

        public void NotStubs()
        {
            var client = Factory.Create(channel);
            client.GetCart(new Req());
            cart.ToString();
        }
    }
}
"""

FILES = {
    "protos/shop.proto": PROTO,
    "cart/CartService.cs": SERVER,
    "web/Basket.cs": CLIENT,
}


@unittest.skipUnless(READY, "tree-sitter-c-sharp is not installed")
class CSharpServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        for name, text in FILES.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.output = asyncio.run(ServiceContractTool(root).analyze("r"))

    def tearDown(self):
        self.directory.cleanup()

    def test_handlers(self):
        handles = sorted(
            (
                r.source_match["qualified_name"].split("::")[2],
                r.target_match["qualified_name"].rpartition("::")[2],
                r.properties["rule_id"],
            )
            for r in self.output.relationships
            if r.type == "HANDLES" and r.target_labels == ["GrpcMethod", "Contract"]
        )
        self.assertEqual(
            handles,
            [
                (
                    "cart.services.CartService.AddItem",
                    "shop.CartService/AddItem",
                    CSHARP_SERVICE_BASE,
                ),
                (
                    "cart.services.CartService.GetCart",
                    "shop.CartService/GetCart",
                    CSHARP_SERVICE_BASE,
                ),
            ],
        )

    def test_client_calls(self):
        requests = sorted(
            (
                r.properties["line"],
                r.source_match["qualified_name"].split("::")[2],
                r.target_match["qualified_name"].rpartition("::")[2],
            )
            for r in self.output.relationships
            if r.type == "REQUESTS"
            and r.properties["rule_id"] == "service-contracts.grpc-stub-call"
        )
        self.assertEqual(
            requests,
            [
                (11, "web.Basket.Load", "shop.CartService/GetCart"),
                (13, "web.Basket.Ads", "shop.AdService/GetAds"),
                (24, "web.Basket.Add", "shop.CartService/AddItem"),
                (30, "web.Basket.Locals", "shop.AdService/GetAds"),
                (36, "web.Basket.OtherLocals", "shop.CartService/GetCart"),
            ],
        )
        unresolved = next(
            n.properties.get("unresolved_grpc_calls")
            for n in self.output.nodes
            if n.properties.get("qualified_name") == "r::web/Basket.cs::file"
        )
        # Calls of ClientBase or object methods stay unresolved, not linked.
        self.assertEqual(unresolved, ["CartService.ToString", "CartService.WithHost"])


if __name__ == "__main__":
    unittest.main()
