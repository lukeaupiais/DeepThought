"""Node messaging clients: kafkajs producers and consumers, and amqplib
channels."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.analysis_tools.evidence import get_rule

READY = (
    importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_javascript")
    and importlib.util.find_spec("tree_sitter_typescript")
)

PRODUCER = """
const { Kafka } = require('kafkajs')
const kafka = new Kafka({ clientId: 'shop', brokers: ['kafka:9092'] })
const producer = kafka.producer()

async function placed(order) {
  await producer.send({ topic: 'orders', messages: [{ value: order }] })
  await producer.sendBatch({
    topicMessages: [{ topic: 'audit', messages: [] }, { topic: order.kind }],
  })
}
"""

CONSUMER = """
import { Kafka as Client } from 'kafkajs';

export class Billing {
  private consumer = new Client({ brokers: [] }).consumer({ groupId: 'billing' });

  async start() {
    await this.consumer.subscribe({ topics: ['orders', /audit-.*/] });
  }
}
"""

# Not kafkajs: the same calls name nothing.
LOOKALIKE = """
const { Kafka } = require('./my-kafka')
const producer = new Kafka().producer()
producer.send({ topic: 'orders' })
"""

# The RabbitMQ tutorials' callback API.
EMIT = """
var amqp = require('amqplib/callback_api');

amqp.connect('amqp://localhost', function (error0, connection) {
  connection.createChannel(function (error1, channel) {
    channel.publish('logs', 'info', Buffer.from('hi'));
    channel.sendToQueue('task_queue', Buffer.from('work'));
  });
});
"""

RECEIVE = """
const amqp = require('amqplib');

async function main() {
  const connection = await amqp.connect('amqp://localhost');
  const channel = await connection.createChannel();
  await channel.bindQueue('info_logs', 'logs', 'info');
  await channel.bindQueue('info_logs', 'logs', '*.info');
  channel.consume('info_logs', (msg) => {});
  connection.createChannel().then((ch) => ch.consume('task_queue', () => {}));
}
"""

FILES = {
    "shop/package.json": '{"name": "shop"}',
    "shop/producer.js": PRODUCER,
    "shop/lookalike.js": LOOKALIKE,
    "billing/package.json": '{"name": "billing"}',
    "billing/src/billing.ts": CONSUMER,
    "logs/package.json": '{"name": "logs"}',
    "logs/emit.js": EMIT,
    "logs/receive.js": RECEIVE,
}


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class NodeMessagingTests(unittest.TestCase):
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

    def test_edges(self):
        edges = sorted(
            (
                r.target_match["qualified_name"]
                .split("::", 1)[-1]
                .replace("::message-destination::", " "),
                r.type,
                r.properties["client"],
                r.source_match["qualified_name"].split("::")[1],
                r.properties["line"],
            )
            for r in self.output.relationships
            if r.type in ("PUBLISHES", "CONSUMES")
        )
        self.maxDiff = None
        self.assertEqual(
            edges,
            [
                # Default exchange, keyed with the queue name.
                ("amqp /info_logs", "CONSUMES", "amqplib", "logs/receive.js", 9),
                ("amqp /task_queue", "CONSUMES", "amqplib", "logs/receive.js", 10),
                ("amqp /task_queue", "PUBLISHES", "amqplib", "logs/emit.js", 7),
                # bindQueue('info_logs', 'logs', 'info').
                ("amqp logs/info", "CONSUMES", "amqplib", "logs/receive.js", 9),
                ("amqp logs/info", "PUBLISHES", "amqplib", "logs/emit.js", 6),
                ("kafka audit", "PUBLISHES", "kafkajs", "shop/producer.js", 8),
                ("kafka orders", "CONSUMES", "kafkajs", "billing/src/billing.ts", 8),
                ("kafka orders", "PUBLISHES", "kafkajs", "shop/producer.js", 7),
            ],
        )

    def test_unread(self):
        self.assertEqual(
            self.output.metadata.custom_metrics["unread"],
            {
                # The '*.info' binding.
                "consume:key_pattern": 1,
                # /audit-.*/.
                "consume:topic_pattern": 1,
                # order.kind.
                "publish:destination_not_literal": 1,
            },
        )

    def test_rules(self):
        for rule_id in (
            "service-contracts.kafkajs-send",
            "service-contracts.kafkajs-subscribe",
            "service-contracts.amqplib-publish",
            "service-contracts.amqplib-consume",
        ):
            self.assertEqual(get_rule(rule_id).evidence_type, "pattern_matched")


if __name__ == "__main__":
    unittest.main()
