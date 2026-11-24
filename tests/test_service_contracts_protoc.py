"""Reading .proto files with protoc (grpcio-tools)."""

import asyncio
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool

from deepthought.service_contracts.grpc import read_proto
from deepthought.service_contracts.protoc import available, read_protos

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
        package, methods = self.read["services/cart/api/shop.proto"]
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
        _, health = self.read["proto/common/v1/common.proto"]
        self.assertEqual([m.full_name for m in health], ["common.v1.Health/Check"])

    def test_unresolved_import_is_left_to_the_fallback(self):
        self.assertNotIn("gateway/broken.proto", self.read)

    def test_agrees_with_the_fallback_reader(self):
        for path, (package, methods) in self.read.items():
            self.assertEqual((package, methods), read_proto(self.files[path], path))

    def test_metric_names_the_reader_of_each_file(self):
        output = asyncio.run(ServiceContractTool(self.root).analyze("r"))
        metrics = output.metadata.custom_metrics
        self.assertEqual(metrics["proto_readers"], {"fallback": 1, "protoc": 2})
        methods = sorted(
            n.properties["qualified_name"]
            for n in output.nodes
            if "GrpcMethod" in n.labels
        )
        # The file protoc cannot compile is still read.
        self.assertIn("r::grpc::broken.Gateway/Get", methods)


if __name__ == "__main__":
    unittest.main()
