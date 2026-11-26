"""Protobuf messages, fields and enums as graph nodes, read by protoc."""

import asyncio
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.service_contracts.protoc import (
    ProtoEnum,
    ProtoMessage,
    available,
    read_protos,
)
from typescript_ready import needs_typescript

TYPES = """
syntax = "proto3";
package shop.v1;

import "google/protobuf/timestamp.proto";
import "common/v1/money.proto";

message Order {
  string id = 1;
  repeated Line lines = 2;
  map<string, common.v1.Money> totals = 3;
  oneof payment {
    string card = 4;
    string voucher = 5;
  }
  optional string note = 6;
  Status status = 7;
  google.protobuf.Timestamp placed_at = 8;
  int32 legacy = 9 [deprecated = true];

  message Line {
    string sku = 1;
    int32 quantity = 2;
  }
  enum Status {
    STATUS_UNSPECIFIED = 0;
    STATUS_PAID = 1;
  }
}

enum Channel {
  CHANNEL_UNSPECIFIED = 0;
  CHANNEL_WEB = 1;
}

service Orders {
  rpc Place (Order) returns (common.v1.Money);
  rpc Get (google.protobuf.Timestamp) returns (Order);
}
"""

MONEY = """
syntax = "proto3";
package common.v1;
message Money { string currency = 1; int64 units = 2; }
"""

# The same message vendored into a second service, with other fields.
MONEY_COPY = """
syntax = "proto3";
package common.v1;
message Money { string currency = 1; int64 nanos = 3; }
"""


def _write(root, files):
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


@unittest.skipUnless(available(), "grpcio-tools is not installed")
class ProtoTypesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.files = {
            "proto/shop/v1/order.proto": TYPES,
            "proto/common/v1/money.proto": MONEY,
        }
        _write(self.root, self.files)

    def tearDown(self):
        self.directory.cleanup()

    def read(self):
        found = read_protos(self.root, sorted(self.files), self.files)
        return {t.full_name: t for t in found["proto/shop/v1/order.proto"][3]}

    def test_messages_fields_and_enums(self):
        types = self.read()
        self.assertEqual(
            sorted(types),
            [
                "shop.v1.Channel",
                "shop.v1.Order",
                "shop.v1.Order.Line",
                "shop.v1.Order.Status",
            ],
        )
        order = types["shop.v1.Order"]
        self.assertIsInstance(order, ProtoMessage)
        self.assertEqual((order.line, order.parent), (8, None))
        fields = {f.name: f for f in order.fields}
        self.assertEqual(
            [(f.name, f.number, f.type, f.kind, f.label) for f in order.fields],
            [
                ("id", 1, "string", "scalar", None),
                ("lines", 2, "shop.v1.Order.Line", "message", "repeated"),
                ("totals", 3, "common.v1.Money", "message", None),
                ("card", 4, "string", "scalar", None),
                ("voucher", 5, "string", "scalar", None),
                ("note", 6, "string", "scalar", "optional"),
                ("status", 7, "shop.v1.Order.Status", "enum", None),
                ("placed_at", 8, "google.protobuf.Timestamp", "message", None),
                ("legacy", 9, "int32", "scalar", None),
            ],
        )
        self.assertEqual(fields["totals"].map_key, "string")
        self.assertEqual(fields["card"].oneof, "payment")
        # proto3 optional's synthetic oneof is not a oneof of the message.
        self.assertIsNone(fields["note"].oneof)
        self.assertEqual(fields["placed_at"].json_name, "placedAt")
        self.assertTrue(fields["legacy"].deprecated)
        self.assertEqual(fields["lines"].line, 10)
        line = types["shop.v1.Order.Line"]
        self.assertEqual((line.parent, line.line), ("shop.v1.Order", 21))
        status = types["shop.v1.Order.Status"]
        self.assertIsInstance(status, ProtoEnum)
        self.assertEqual(
            [(n, v) for n, v, _ in status.values],
            [("STATUS_UNSPECIFIED", 0), ("STATUS_PAID", 1)],
        )
        self.assertEqual(types["shop.v1.Channel"].parent, None)

    @needs_typescript
    def test_graph_nodes_and_edges(self):
        output = asyncio.run(ServiceContractTool(self.root).analyze("r"))
        nodes = {n.properties["qualified_name"]: n for n in output.nodes}
        order = nodes["r::proto::message::shop.v1.Order"]
        self.assertEqual(order.labels, ["ProtoMessage"])
        self.assertEqual(order.properties["file_path"], "proto/shop/v1/order.proto")
        self.assertEqual(
            order.properties["declared_in"], ["proto/shop/v1/order.proto:8"]
        )
        totals = nodes["r::proto::message::shop.v1.Order::field::totals"]
        self.assertEqual(totals.labels, ["ProtoField"])
        self.assertEqual(
            {
                k: totals.properties[k]
                for k in ("number", "type", "type_kind", "map_key", "message")
            },
            {
                "number": 3,
                "type": "common.v1.Money",
                "type_kind": "message",
                "map_key": "string",
                "message": "shop.v1.Order",
            },
        )
        self.assertEqual(
            nodes["r::proto::enum::shop.v1.Order.Status"].properties["values"],
            ["STATUS_UNSPECIFIED=0", "STATUS_PAID=1"],
        )
        self.assertEqual(
            nodes["r::proto/shop/v1/order.proto::file"].properties["language"],
            "protobuf",
        )
        edges = {
            (
                r.type,
                r.source_match["qualified_name"].removeprefix("r::"),
                r.target_match["qualified_name"].removeprefix("r::"),
                r.properties.get("direction"),
            ): r
            for r in output.relationships
        }
        expected = [
            (
                "DECLARES",
                "proto/shop/v1/order.proto::file",
                "proto::message::shop.v1.Order",
                None,
            ),
            (
                "DECLARES",
                "proto/shop/v1/order.proto::file",
                "proto::enum::shop.v1.Channel",
                None,
            ),
            (
                "CONTAINS",
                "proto::message::shop.v1.Order",
                "proto::message::shop.v1.Order.Line",
                None,
            ),
            (
                "CONTAINS",
                "proto::message::shop.v1.Order",
                "proto::enum::shop.v1.Order.Status",
                None,
            ),
            (
                "HAS_FIELD",
                "proto::message::shop.v1.Order",
                "proto::message::shop.v1.Order::field::lines",
                None,
            ),
            (
                "USES_TYPE",
                "proto::message::shop.v1.Order::field::lines",
                "proto::message::shop.v1.Order.Line",
                None,
            ),
            (
                "USES_TYPE",
                "proto::message::shop.v1.Order::field::totals",
                "proto::message::common.v1.Money",
                None,
            ),
            (
                "USES_TYPE",
                "proto::message::shop.v1.Order::field::status",
                "proto::enum::shop.v1.Order.Status",
                None,
            ),
            (
                "CARRIES",
                "grpc::shop.v1.Orders/Place",
                "proto::message::shop.v1.Order",
                "request",
            ),
            (
                "CARRIES",
                "grpc::shop.v1.Orders/Place",
                "proto::message::common.v1.Money",
                "response",
            ),
            (
                "CARRIES",
                "grpc::shop.v1.Orders/Get",
                "proto::message::shop.v1.Order",
                "response",
            ),
            ("CONTAINS", "repository", "proto/shop/v1/order.proto::file", None),
        ]
        for key in expected:
            self.assertIn(key, edges)
            self.assertEqual(edges[key].properties["evidence_type"], "resolved")
        # A nested type is declared by its message, not the file.
        self.assertNotIn(
            (
                "DECLARES",
                "proto/shop/v1/order.proto::file",
                "proto::message::shop.v1.Order.Line",
                None,
            ),
            edges,
        )
        # Well-known types protoc ships are not the repository's: the
        # field and the rpc name them, no edge does.
        names = {target for _, _, target, _ in edges}
        self.assertFalse(any("google.protobuf" in n for n in names))
        self.assertEqual(
            nodes["r::proto::message::shop.v1.Order::field::placed_at"].properties[
                "type"
            ],
            "google.protobuf.Timestamp",
        )
        metrics = output.metadata.custom_metrics
        self.assertEqual(metrics["proto_types"], {"ProtoEnum": 2, "ProtoMessage": 3})
        self.assertEqual(metrics["grpc_proto_message_edges"], 3)

    @needs_typescript
    def test_disagreeing_copies_keep_no_fields(self):
        self.files["services/pay/proto/common/v1/money.proto"] = MONEY_COPY
        _write(self.root, self.files)
        output = asyncio.run(ServiceContractTool(self.root).analyze("r"))
        nodes = {n.properties["qualified_name"]: n for n in output.nodes}
        money = nodes["r::proto::message::common.v1.Money"]
        self.assertTrue(money.properties["copies_disagree"])
        self.assertEqual(
            money.properties["declared_in"],
            [
                "proto/common/v1/money.proto:4",
                "services/pay/proto/common/v1/money.proto:4",
            ],
        )
        self.assertFalse(
            any(
                name.startswith("r::proto::message::common.v1.Money::field::")
                for name in nodes
            )
        )
        declares = [
            r.source_match["qualified_name"]
            for r in output.relationships
            if r.type == "DECLARES"
            and r.target_match["qualified_name"] == "r::proto::message::common.v1.Money"
        ]
        self.assertEqual(len(declares), 2)


if __name__ == "__main__":
    unittest.main()
