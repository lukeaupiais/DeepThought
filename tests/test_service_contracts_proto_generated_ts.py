"""JavaScript/TypeScript code generated from .proto files, linked to the
protobuf types it is generated from (GENERATED_FROM): protoc-gen-js
(google-protobuf, as grpc-web and grpc-tools use it), protobuf-es 1 and 2,
and ts-proto. The fixtures are the generators' output for order.proto
(protoc-gen-js 3.21.4, protoc-gen-es 2.10.1, ts-proto 2.6.1; the
protoc-gen-js file cut after its exportSymbol calls), and protobuf-es's own
erasable_syntax test file and its output with erasable_syntax=true."""

import asyncio
from pathlib import Path
import shutil
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.service_contracts.protoc import available, indexed_type
from typescript_ready import needs_typescript

FIXTURES = Path(__file__).parent / "fixtures" / "proto_generated"
TYPES = [
    ("enum", "Channel"),
    ("enum", "Order.Line.Kind"),
    ("enum", "Order.Status"),
    ("message", "Item"),
    ("message", "Item.Tag"),
    ("message", "Order"),
    ("message", "Order.Line"),
]


def _descriptor():
    from grpc_tools import protoc
    from google.protobuf import descriptor_pb2

    with tempfile.TemporaryDirectory() as directory:
        out = Path(directory) / "set.pb"
        assert (
            protoc.main(
                [
                    "protoc",
                    f"-I{FIXTURES}",
                    f"--descriptor_set_out={out}",
                    "order.proto",
                ]
            )
            == 0
        )
        files = descriptor_pb2.FileDescriptorSet.FromString(out.read_bytes())
    return files.file[0].SerializeToString()


@unittest.skipUnless(available(), "grpcio-tools is not installed")
class IndexedTypeTests(unittest.TestCase):
    def test_protobuf_es_indexes(self):
        data = _descriptor()
        for kind, path, name in (
            ("message", [0], "shop.v1.Order"),
            ("message", [0, 0], "shop.v1.Order.Line"),
            ("message", [1, 0], "shop.v1.Item.Tag"),
            ("enum", [0], "shop.v1.Channel"),
            ("enum", [0, 0], "shop.v1.Order.Status"),
            ("enum", [0, 0, 0], "shop.v1.Order.Line.Kind"),
            ("message", [5], None),
            ("enum", [1, 3], None),
        ):
            with self.subTest(kind=kind, path=path):
                self.assertEqual(indexed_type(data, kind, path), name)


@unittest.skipUnless(available(), "grpcio-tools is not installed")
@needs_typescript
class GeneratedScriptTests(unittest.TestCase):
    def links(self, generated, proto="order.proto"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "proto").mkdir()
            shutil.copy(FIXTURES / proto, root / "proto" / proto)
            (root / "web" / "src").mkdir(parents=True)
            target = root / "web" / "src" / generated
            shutil.copy(FIXTURES / generated, target)
            output = asyncio.run(ServiceContractTool(root).analyze("r"))
        return sorted(
            (
                r.source_match["qualified_name"].split("::")[2],
                r.target_match["qualified_name"].removeprefix("r::proto::"),
                r.properties["evidence_type"],
                r.properties["rule_id"],
            )
            for r in output.relationships
            if r.type == "GENERATED_FROM"
        )

    def test_protoc_gen_js_module(self):
        rule = "service-contracts.js-generated-proto-module"
        self.assertEqual(
            self.links("order_pb.js"),
            sorted(
                ("file", f"{kind}::shop.v1.{name}", "resolved", rule)
                for kind, name in TYPES
            ),
        )

    def test_protobuf_es_2(self):
        rule = "service-contracts.ts-generated-proto-type"
        found = self.links("order_es_pb.ts")
        # Each generated type and enum: messages through Message<"name">
        # and their schema, enums through their schema's GenEnum<E>.
        self.assertEqual(
            found,
            sorted(
                (name.replace(".", "_"), f"{kind}::shop.v1.{name}", "resolved", rule)
                for kind, name in TYPES
            ),
        )

    def test_protobuf_es_2_erasable_enums(self):
        # erasable_syntax=true: an enum is a const object and a type of
        # one name, not a TypeScript enum; the type links.
        rule = "service-contracts.ts-generated-proto-type"
        self.assertEqual(
            self.links("erasable_syntax_pb.ts", "erasable_syntax.proto"),
            [
                ("Color", "enum::Color", "resolved", rule),
                ("ESyntax", "message::ESyntax", "resolved", rule),
                ("Season", "enum::Season", "resolved", rule),
            ],
        )

    def test_protobuf_es_1(self):
        rule = "service-contracts.ts-generated-proto-type"
        self.assertEqual(
            self.links("order_es1_pb.ts"),
            [
                ("Channel", "enum::shop.v1.Channel", "resolved", rule),
                ("Item", "message::shop.v1.Item", "resolved", rule),
                ("Item_Tag", "message::shop.v1.Item.Tag", "resolved", rule),
            ],
        )

    def test_ts_proto(self):
        rule = "service-contracts.ts-proto-generated-type"
        found = self.links("order_ts_proto.ts")
        names = {(declared, target) for declared, target, _, _ in found}
        for kind, name in TYPES:
            self.assertIn((name.replace(".", "_"), f"{kind}::shop.v1.{name}"), names)
        self.assertTrue(all(e == "name_matched" and r == rule for _, _, e, r in found))
        # The map entry is no type of the .proto: Order_TotalsEntry links nothing.
        self.assertFalse(any(d == "Order_TotalsEntry" for d, _ in names))


if __name__ == "__main__":
    unittest.main()
