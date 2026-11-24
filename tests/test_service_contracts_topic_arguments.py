"""A kafkajs topic passed to a wrapper function: each call of the wrapper
gives the topic its site sends to or subscribes to."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.analysis_tools.evidence import get_rule

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_typescript"
)

BROKER = """
import { Kafka } from 'kafkajs';

const ORDERS = 'orders';

export const publish = async (kafka: Kafka, topic: string, payload: any) => {
  const producer = kafka.producer();
  await producer.send({ topic, messages: [{ value: payload }] });
};

export async function listen(kafka: Kafka, topic = 'unused') {
  const consumer = kafka.consumer({ groupId: 'g' });
  await consumer.subscribe({ topic: topic });
}

// Assigned again inside the function: not the caller's value.
export const rewritten = async (kafka: Kafka, topic: string) => {
  topic = topic + '.v2';
  const producer = kafka.producer();
  await producer.send({ topic });
};

listen(new Kafka({ brokers: [] }), ORDERS);
"""

API = """
import { Kafka } from 'kafkajs';
import { publish as send, listen, rewritten } from './broker';

const kafka = new Kafka({ brokers: [] });
const PAYMENTS = 'payments';

export function run(name: string) {
  send(kafka, 'orders', '{}');
  send(kafka, PAYMENTS, '{}');
  send(kafka, name, '{}');
  rewritten(kafka, 'orders');
}

function shadowed() {
  const listen = (k: Kafka, t: string) => t;
  listen(kafka, 'ignored');
}
"""

FILES = {
    "svc/package.json": '{"name": "svc"}',
    "svc/src/broker.ts": BROKER,
    "svc/src/api.ts": API,
}


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class TopicArgumentTests(unittest.TestCase):
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

    def test_sites(self):
        edges = sorted(
            (
                r.target_match["qualified_name"].split("::")[-1],
                r.type,
                r.properties["line"],
                r.properties["rule_id"],
                tuple(r.properties.get("argument_from", ())),
            )
            for r in self.output.relationships
            if r.type in ("PUBLISHES", "CONSUMES")
        )
        rule = "service-contracts.destination-argument"
        self.assertEqual(
            edges,
            [
                ("orders", "CONSUMES", 13, rule, ("svc/src/broker.ts:23",)),
                ("orders", "PUBLISHES", 8, rule, ("svc/src/api.ts:9",)),
                ("payments", "PUBLISHES", 8, rule, ("svc/src/api.ts:10",)),
            ],
        )
        sends = [
            r
            for r in self.output.relationships
            if r.type == "PUBLISHES" and r.properties["line"] == 8
        ]
        self.assertEqual(
            {r.properties["site_rule"] for r in sends},
            {"service-contracts.kafkajs-send"},
        )

    def test_unread(self):
        # The name parameter of run, and the rewritten topic.
        self.assertEqual(
            self.output.metadata.custom_metrics["unread"],
            {"publish:destination_not_literal": 2},
        )

    def test_rule(self):
        rule = get_rule("service-contracts.destination-argument")
        self.assertEqual(rule.evidence_type, "pattern_matched")


if __name__ == "__main__":
    unittest.main()
