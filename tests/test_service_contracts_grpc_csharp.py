"""C# gRPC servers: classes deriving from the X.XBase class Grpc.Tools
generates."""

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

FILES = {"protos/shop.proto": PROTO, "cart/CartService.cs": SERVER}


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


if __name__ == "__main__":
    unittest.main()
