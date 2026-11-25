"""What a Go Kafka producer sends: a message whose value is json.Marshal
of a value carries that value's type, as the Go type checker gives it."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool

from go_stubs import go_mod, go_stub_files, needs_go

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_go"
)

EVENTS = """package events

type OrderPlaced struct {
\tID string `json:"id"`
}

type Shipped struct {
\tOrder string `json:"order"`
}
"""

PRODUCER = """package producer

import (
\t"context"
\t"encoding/json"

\t"github.com/IBM/sarama"
\t"github.com/confluentinc/confluent-kafka-go/v2/kafka"
\tkgo "github.com/segmentio/kafka-go"

\t"example.com/shop/events"
)

func placed(w *kgo.Writer, e events.OrderPlaced) {
\tb, _ := json.Marshal(e)
\tw.WriteMessages(context.TODO(), kgo.Message{Topic: "orders", Value: b})
}

func shipped(p sarama.SyncProducer, s *events.Shipped) {
\tdata, err := json.Marshal(s)
\tif err != nil {
\t\treturn
\t}
\tp.SendMessage(&sarama.ProducerMessage{Topic: "shipped", Value: sarama.ByteEncoder(data)})
}

func notice(p *kafka.Producer, e events.OrderPlaced) {
\tname := "notices"
\tpayload, _ := json.Marshal(e)
\tp.Produce(&kafka.Message{TopicPartition: kafka.TopicPartition{Topic: &name}, Value: payload}, nil)
}

func raw(p sarama.SyncProducer) {
\tbody, _ := json.Marshal(map[string]string{"a": "b"})
\tp.SendMessage(&sarama.ProducerMessage{Topic: "raw", Value: sarama.ByteEncoder(body)})
}
"""


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
@needs_go
class GoKafkaPayloadTypeTests(unittest.TestCase):
    def test_messages_carry_the_types_they_marshal(self):
        # A map is no type of the repository, so "raw" carries nothing.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                **go_stub_files(),
                "svc/go.mod": go_mod(
                    "example.com/shop",
                    "github.com/IBM/sarama",
                    "github.com/confluentinc/confluent-kafka-go/v2",
                    "github.com/segmentio/kafka-go",
                ),
                "svc/events/events.go": EVENTS,
                "svc/producer/producer.go": PRODUCER,
            }
            for name, text in files.items():
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
        rule = "service-contracts.go-kafka-message-value-type"
        self.assertEqual(
            found,
            [
                ("notices", "OrderPlaced", "request", rule),
                ("orders", "OrderPlaced", "request", rule),
                ("shipped", "Shipped", "request", rule),
            ],
        )


if __name__ == "__main__":
    unittest.main()
