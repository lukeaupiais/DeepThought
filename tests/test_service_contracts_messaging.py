"""Message destinations: Kafka topics and AMQP exchanges and queues."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool, TreeSitterTool
from deepthought.analysis_tools.evidence import get_rule
from deepthought.storage.graph.write_coordinator import (
    WriteCoordinatorConfig,
    _combine,
)

from go_stubs import go_mod, go_stub_files

READY = importlib.util.find_spec("tree_sitter") and all(
    importlib.util.find_spec(f"tree_sitter_{name}") for name in ("python", "go", "java")
)

ORDER_SERVICE = """
package order.logic;

import order.config.Legacy;
import order.config.Topics;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.springframework.kafka.core.KafkaTemplate;

public class OrderService {
    private KafkaTemplate<String, Order> kafkaTemplate;

    void created(Order order) {
        kafkaTemplate.send("order", order.getId(), order);
    }

    void shipped(Order order) {
        kafkaTemplate.send(new ProducerRecord<>(Topics.SHIPPED, order));
    }

    void other(String topic, Order order) {
        kafkaTemplate.send(topic, order);
        // order.config.Legacy is declared in two files: its value is unknown.
        kafkaTemplate.send(Legacy.TOPIC, order);
    }
}
"""

TOPICS = """
package order.config;

public interface Topics {
    String SHIPPED = "order-" + "shipped";
}
"""

# A second class named Topics in another package does not confuse the first.
OTHER_TOPICS = """
package billing.config;

public class Topics {
    public static final String SHIPPED = "elsewhere";
}
"""

LEGACY = """
package order.config;

public class Legacy {
    public static final String TOPIC = "legacy";
}
"""

INVOICE_LISTENER = """
package invoicing;

import org.springframework.kafka.annotation.KafkaListener;

public class OrderListener {
    @KafkaListener(topics = {"order", "order-shipped"})
    public void order(Invoice invoice) {}
}
"""

QUEUES = """
package mail.config;

public class Queues {
    public final static String queueName = "email";
}
"""

MAIL_RECEIVE = """
package mail.mq;

import mail.config.Queues;
import org.springframework.amqp.rabbit.annotation.RabbitListener;

public class Receive {
    @RabbitListener(queues = Queues.queueName)
    public void process(String payload) {}
}
"""

BOOKING_SEND = """
package booking.mq;

import org.springframework.amqp.core.AmqpTemplate;

public class Send {
    private AmqpTemplate rabbitTemplate;

    public void send(String value) {
        rabbitTemplate.convertAndSend("email", value);
        rabbitTemplate.convertAndSend("audit", "booking.created", value);
    }
}
"""

# This module sets its template's exchange in code: a routing key alone
# does not fix the destination.
REPORT_SEND = """
package report;

import org.springframework.amqp.rabbit.core.RabbitTemplate;

public class Send {
    private RabbitTemplate template;

    Send(RabbitTemplate template) {
        template.setExchange("reports");
        this.template = template;
    }

    public void send(String value) {
        template.convertAndSend("email", value);
    }
}
"""

PUBLISHER = """
import pika
from kafka import KafkaProducer, KafkaConsumer


class Publisher:
    EXCHANGE = "robot-shop"
    ROUTING_KEY = "orders"
    QUEUE = "a"
    QUEUE = "b"

    def consume(self, channel):
        # QUEUE is assigned twice in the class: its value is unknown.
        channel.basic_consume(queue=self.QUEUE)

    def publish(self, channel, body):
        channel.basic_publish(exchange=self.EXCHANGE, routing_key=self.ROUTING_KEY, body=body)


producer = KafkaProducer(bootstrap_servers="kafka:9092")


def audit(event):
    producer.send("audit-log", event)
    mailer = Mailer()
    mailer.send("not-a-topic", event)


def listen():
    for message in KafkaConsumer("order-shipped", group_id="py"):
        pass
"""

DISPATCH = """
package main

import "github.com/streadway/amqp"

func run(ch *amqp.Channel) {
	ch.ExchangeDeclare("robot-shop", "direct", true, false, false, false, nil)
	queue, _ := ch.QueueDeclare("orders", true, false, false, false, nil)
	ch.QueueBind(queue.Name, "orders", "robot-shop", false, nil)
	ch.QueueBind(queue.Name, "orders.#", "audit", false, nil)
	ch.QueueBind("other", "other", "robot-shop", false, nil)
	msgs, _ := ch.Consume("orders", "", true, false, false, false, nil)
	_ = msgs
	ch.Publish("audit", "dispatch.done", false, false, amqp.Publishing{})
}
"""

AUDIT = """
package main

import amqp "github.com/rabbitmq/amqp091-go"

func audit(ch *amqp.Channel) {
	ch.QueueBind("audit", "booking.created", "audit", false, nil)
	ch.Consume("audit", "", true, false, false, false, nil)
}
"""


def _write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class MessagingContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        java = "src/main/java"
        _write(
            self.root,
            {
                "order/pom.xml": "<project/>",
                f"order/{java}/order/logic/OrderService.java": ORDER_SERVICE,
                f"order/{java}/order/config/Topics.java": TOPICS,
                f"billing/{java}/billing/config/Topics.java": OTHER_TOPICS,
                "invoicing/pom.xml": "<project/>",
                f"invoicing/{java}/invoicing/OrderListener.java": INVOICE_LISTENER,
                "mail/pom.xml": "<project/>",
                f"mail/{java}/mail/config/Queues.java": QUEUES,
                f"mail/{java}/mail/mq/Receive.java": MAIL_RECEIVE,
                "booking/pom.xml": "<project/>",
                f"booking/{java}/booking/mq/Send.java": BOOKING_SEND,
                "report/pom.xml": "<project/>",
                f"report/{java}/report/Send.java": REPORT_SEND,
                "payment/publisher.py": PUBLISHER,
                # Without a pika import, basic_publish is someone else's.
                "payment/fake.py": (
                    "def f(channel):\n"
                    "    channel.basic_publish(exchange='x', routing_key='y')\n"
                ),
                f"order/{java}/order/config/Legacy.java": LEGACY,
                f"legacy/{java}/order/config/Legacy.java": LEGACY,
                **go_stub_files(),
                "dispatch/go.mod": go_mod("dispatch", "github.com/streadway/amqp"),
                "dispatch/main.go": DISPATCH,
                "audit/go.mod": go_mod("audit", "github.com/rabbitmq/amqp091-go"),
                "audit/main.go": AUDIT,
            },
        )
        self.tool = ServiceContractTool(self.root)
        self.output = asyncio.run(self.tool.analyze("r"))
        self.endpoints, self.unread = self.tool.last_messages

    def tearDown(self):
        self.directory.cleanup()

    def test_endpoints(self):
        found = sorted(
            (e.file.rpartition("/")[2], e.role, e.broker, e.destination, e.client)
            for e in self.endpoints
        )
        self.assertEqual(
            found,
            [
                ("OrderListener.java", "consume", "kafka", "order", "spring-kafka"),
                (
                    "OrderListener.java",
                    "consume",
                    "kafka",
                    "order-shipped",
                    "spring-kafka",
                ),
                ("OrderService.java", "publish", "kafka", "order", "spring-kafka"),
                (
                    "OrderService.java",
                    "publish",
                    "kafka",
                    "order-shipped",
                    "spring-kafka",
                ),
                ("Receive.java", "consume", "amqp", "/email", "spring-amqp"),
                ("Send.java", "publish", "amqp", "/email", "spring-amqp"),
                (
                    "Send.java",
                    "publish",
                    "amqp",
                    "audit/booking.created",
                    "spring-amqp",
                ),
                ("main.go", "consume", "amqp", "/audit", "go-amqp"),
                ("main.go", "consume", "amqp", "/orders", "go-amqp"),
                ("main.go", "consume", "amqp", "audit/booking.created", "go-amqp"),
                ("main.go", "consume", "amqp", "robot-shop/orders", "go-amqp"),
                ("main.go", "publish", "amqp", "audit/dispatch.done", "go-amqp"),
                ("publisher.py", "consume", "kafka", "order-shipped", "kafka-python"),
                ("publisher.py", "publish", "amqp", "robot-shop/orders", "pika"),
                ("publisher.py", "publish", "kafka", "audit-log", "kafka-python"),
            ],
        )
        self.assertEqual(
            sorted((u.file.rpartition("/")[2], u.role, u.reason) for u in self.unread),
            [
                ("OrderService.java", "publish", "destination_not_literal"),
                ("OrderService.java", "publish", "destination_not_literal"),
                ("Send.java", "publish", "template_exchange"),
                ("main.go", "consume", "key_pattern"),
                ("publisher.py", "consume", "destination_not_literal"),
            ],
        )

    def test_graph(self):
        destinations = sorted(
            n.properties["display_name"]
            for n in self.output.nodes
            if "MessageDestination" in n.labels
        )
        self.assertEqual(len(destinations), len(set(destinations)))
        self.assertIn("amqp robot-shop/orders", destinations)
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[2],
                r.target_match["qualified_name"].split("::message-destination::")[1],
            )
            for r in self.output.relationships
            if r.type in ("PUBLISHES", "CONSUMES")
        )
        self.assertIn(("PUBLISHES", "Publisher.publish", "robot-shop/orders"), edges)
        self.assertIn(("CONSUMES", "run", "robot-shop/orders"), edges)
        self.assertIn(("CONSUMES", "OrderListener.order", "order"), edges)
        # A module-level publish hangs off its file.
        self.assertIn(("PUBLISHES", "audit", "audit-log"), edges)
        for relationship in self.output.relationships:
            if relationship.type in ("PUBLISHES", "CONSUMES"):
                rule = get_rule(relationship.properties["rule_id"])
                self.assertEqual(rule.evidence_type, "pattern_matched")
        metrics = self.output.metadata.custom_metrics
        self.assertEqual(metrics["message_endpoints"], 15)
        self.assertEqual(metrics["unread"]["consume:key_pattern"], 1)

    def test_graph_joins_tree_sitter_declarations(self):
        trees = asyncio.run(
            TreeSitterTool(self.root).analyze("r", languages={"python", "go", "java"})
        )
        self.output.metadata.custom_metrics["snapshot_id"] = (
            trees.metadata.custom_metrics["snapshot_id"]
        )
        merged, _ = _combine([trees, self.output], "r", "rev", WriteCoordinatorConfig())
        symbols = {
            n.properties["qualified_name"] for n in trees.nodes if "Symbol" in n.labels
        }
        contract_symbols = {
            n.properties["qualified_name"]
            for n in self.output.nodes
            if "Symbol" in n.labels
        }
        self.assertTrue(contract_symbols <= symbols)
        self.assertTrue(
            any(r.type == "CONSUMES" for r in merged.relationships),
        )

    def test_deterministic(self):
        again = asyncio.run(ServiceContractTool(self.root).analyze("r"))
        self.assertEqual(
            [n.properties for n in again.nodes],
            [n.properties for n in self.output.nodes],
        )
        self.assertEqual(
            [r.properties for r in again.relationships],
            [r.properties for r in self.output.relationships],
        )


if __name__ == "__main__":
    unittest.main()
