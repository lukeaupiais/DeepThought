"""Types a producer sends: the data of a NestJS ClientProxy's send/emit and
the value a kafkajs message serializes. The destination CARRIES the
declarations the TypeScript compiler gives for the value."""

import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from typescript_ready import needs_typescript

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_typescript"
)

FILES = {
    "package.json": json.dumps(
        {
            "dependencies": {
                "@nestjs/common": "10",
                "@nestjs/microservices": "10",
                "kafkajs": "2",
            }
        }
    ),
    "tsconfig.json": json.dumps(
        {"compilerOptions": {"experimentalDecorators": True, "baseUrl": "."}}
    ),
    "src/events.ts": (
        "export class OrderCreated { id: string; }\n"
        "export interface Shipment { order: string; }\n"
        "export interface Audit { who: string; }\n"
    ),
    "src/orders.service.ts": (
        "import { Inject, Injectable } from '@nestjs/common';\n"
        "import { ClientProxy } from '@nestjs/microservices';\n"
        "import { OrderCreated, Shipment, Audit } from './events';\n"
        "@Injectable()\n"
        "export class OrdersService {\n"
        "  constructor(@Inject('BUS') private client: ClientProxy) {}\n"
        "  created(event: OrderCreated) {\n"
        "    this.client.emit('order_created', event);\n"
        "  }\n"
        "  ship(shipment: Shipment, audit: Audit) {\n"
        "    return this.client.send('ship', { shipment, audit, n: 1 });\n"
        "  }\n"
        "  ping() {\n"
        "    this.client.emit('ping', 'hello');\n"
        "  }\n"
        "}\n"
    ),
    "src/producer.ts": (
        "import { Kafka } from 'kafkajs';\n"
        "import { OrderCreated } from './events';\n"
        "const kafka = new Kafka({ brokers: ['k:9092'] });\n"
        "const producer = kafka.producer();\n"
        "export async function publish(order: OrderCreated) {\n"
        "  await producer.send({\n"
        "    topic: 'orders',\n"
        "    messages: [{ key: order.id, value: JSON.stringify(order) }],\n"
        "  });\n"
        "}\n"
        "export async function audit() {\n"
        "  await producer.send({\n"
        "    topic: 'audits',\n"
        "    messages: [{ value: Buffer.from(JSON.stringify(new OrderCreated())) }],\n"
        "  });\n"
        "}\n"
    ),
}


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
@needs_typescript
class SentPayloadTypeTests(unittest.TestCase):
    def test_destinations_carry_the_types_producers_send(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in FILES.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            output = asyncio.run(ServiceContractTool(root).analyze("r"))
        found = sorted(
            (
                r.source_match["qualified_name"].split("::")[-1],
                r.target_match["qualified_name"].split("::")[2],
                r.properties["direction"],
                r.properties["rule_id"],
            )
            for r in output.relationships
            if r.type == "CARRIES"
        )
        nest = "service-contracts.nestjs-client-data-type"
        kafkajs = "service-contracts.kafkajs-message-value-type"
        self.assertEqual(
            found,
            [
                ("audits", "OrderCreated", "request", kafkajs),
                ("event:order_created", "OrderCreated", "request", nest),
                ("message:ship", "Audit", "request", nest),
                ("message:ship", "Shipment", "request", nest),
                ("orders", "OrderCreated", "request", kafkajs),
            ],
        )


if __name__ == "__main__":
    unittest.main()
