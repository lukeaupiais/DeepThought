"""Go Kafka clients: segmentio/kafka-go, sarama and confluent-kafka-go."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.analysis_tools.evidence import get_rule

from go_stubs import go_mod, go_stub_files

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_go"
)

KAFKA_GO = """package orders

import (
\t"context"

\t"github.com/segmentio/kafka-go"
)

const topic = "orders"

func writer() *kafka.Writer {
\treturn &kafka.Writer{Addr: kafka.TCP("k:9092"), Topic: topic}
}

func audit(w *kafka.Writer) {
\tw.WriteMessages(context.TODO(), kafka.Message{Topic: "audit"}, kafka.Message{Value: nil})
}
"""

KAFKA_GO_READER = """package billing

import kgo "github.com/segmentio/kafka-go"

func readers() {
\tkgo.NewReader(kgo.ReaderConfig{Brokers: nil, Topic: "orders", GroupID: "billing"})
\tkgo.NewReader(kgo.ReaderConfig{GroupID: "g", GroupTopics: []string{"audit", "refunds"}})
}
"""

SARAMA = """package shipping

import (
\t"context"
\t"flag"

\t"github.com/IBM/sarama"
)

var topic = ""

func init() { flag.StringVar(&topic, "topic", "", "") }

func run(p sarama.SyncProducer, c sarama.Consumer, g sarama.ConsumerGroup) {
\tp.SendMessage(&sarama.ProducerMessage{Topic: "shipped"})
\tp.SendMessage(&sarama.ProducerMessage{Topic: topic})
\tc.ConsumePartition("orders", 0, sarama.OffsetNewest)
\tg.Consume(context.TODO(), []string{"refunds"}, nil)
}
"""

CONFLUENT = """package notify

import "github.com/confluentinc/confluent-kafka-go/v2/kafka"

func run(p *kafka.Producer, c *kafka.Consumer) {
\tname := "notices"
\tp.Produce(&kafka.Message{TopicPartition: kafka.TopicPartition{Topic: &name}}, nil)
\tc.SubscribeTopics([]string{"shipped", "^audit-.*"}, nil)
\tc.Subscribe("orders", nil)
}
"""

# No Kafka import: the same names mean nothing.
PLAIN = """package plain

type consumer interface {
\tConsumePartition(topic string, partition, offset int) (int, error)
\tSubscribe(topic string, cb interface{}) error
}

func run(c consumer) {
\tc.ConsumePartition("orders", 0, 0)
\tc.Subscribe("orders", nil)
}
"""

FILES = {
    **go_stub_files(),
    "orders/go.mod": go_mod("orders", "github.com/segmentio/kafka-go"),
    "orders/kafka.go": KAFKA_GO,
    "billing/go.mod": go_mod("billing", "github.com/segmentio/kafka-go"),
    "billing/reader.go": KAFKA_GO_READER,
    "shipping/go.mod": go_mod("shipping", "github.com/IBM/sarama"),
    "shipping/main.go": SARAMA,
    "notify/go.mod": go_mod("notify", "github.com/confluentinc/confluent-kafka-go/v2"),
    "notify/main.go": CONFLUENT,
    "plain/go.mod": "module plain\n",
    "plain/main.go": PLAIN,
}


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class GoKafkaTests(unittest.TestCase):
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
                r.target_match["qualified_name"].split("::")[-1],
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
                ("audit", "CONSUMES", "kafka-go", "billing/reader.go", 7),
                # The Message literal, not the Writer, names the topic.
                ("audit", "PUBLISHES", "kafka-go", "orders/kafka.go", 16),
                ("notices", "PUBLISHES", "confluent-kafka-go", "notify/main.go", 7),
                ("orders", "CONSUMES", "confluent-kafka-go", "notify/main.go", 9),
                ("orders", "CONSUMES", "kafka-go", "billing/reader.go", 6),
                ("orders", "CONSUMES", "sarama", "shipping/main.go", 17),
                ("orders", "PUBLISHES", "kafka-go", "orders/kafka.go", 12),
                ("refunds", "CONSUMES", "kafka-go", "billing/reader.go", 7),
                ("refunds", "CONSUMES", "sarama", "shipping/main.go", 18),
                ("shipped", "CONSUMES", "confluent-kafka-go", "notify/main.go", 8),
                ("shipped", "PUBLISHES", "sarama", "shipping/main.go", 15),
            ],
        )

    def test_unread(self):
        self.assertEqual(
            self.output.metadata.custom_metrics["unread"],
            # "^audit-.*", and the flag-set package variable.
            {"consume:topic_pattern": 1, "publish:destination_not_literal": 1},
        )

    def test_module_files_are_not_written(self):
        # The go command records into copies of go.mod and go.sum.
        root = Path(self.directory.name)
        for name, text in FILES.items():
            if name.endswith("go.mod"):
                self.assertEqual((root / name).read_text(encoding="utf-8"), text)
        self.assertEqual(list(root.rglob("go.sum")), [])

    def test_rules(self):
        for rule_id in (
            "service-contracts.kafka-go-writer",
            "service-contracts.kafka-go-reader",
            "service-contracts.sarama-producer-message",
            "service-contracts.sarama-consume",
            "service-contracts.confluent-kafka-go-produce",
            "service-contracts.confluent-kafka-go-subscribe",
        ):
            self.assertEqual(get_rule(rule_id).evidence_type, "pattern_matched")


if __name__ == "__main__":
    unittest.main()
