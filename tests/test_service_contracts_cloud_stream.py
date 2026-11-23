"""Spring Cloud Stream bindings and the destinations services bind them to."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.analysis_tools.evidence import get_rule

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_java"
)

JAVA = "src/main/java"
RESOURCES = "src/main/resources"


def _pom(artifact, *dependencies):
    found = "".join(
        f"<dependency><groupId>g</groupId><artifactId>{d}</artifactId></dependency>"
        for d in dependencies
    )
    return (
        '<project xmlns="http://maven.apache.org/POM/4.0.0">'
        f"<artifactId>{artifact}</artifactId>"
        f"<dependencies>{found}</dependencies></project>"
    )


# A library module: it runs in every service whose pom.xml depends on it.
PUBLISHER = """
package events;

import org.springframework.cloud.stream.function.StreamBridge;
import org.springframework.cloud.stream.messaging.Source;
import org.springframework.messaging.handler.annotation.SendTo;
import org.springframework.messaging.MessageChannel;
import org.springframework.messaging.support.MessageBuilder;

public class Publisher {
    private Source source;
    private final MessageChannel channel;
    private StreamBridge bridge;
    private Mailer mailer;

    Publisher(Source source) {
        this.source = source;
        this.channel = source.output();
    }

    void created(Order order) {
        source.output().send(MessageBuilder.withPayload(order).build());
    }

    void again(Order order) {
        channel.send(MessageBuilder.withPayload(order).build());
    }

    void audit(Order order) {
        bridge.send("audit", order);
        // Not a channel: neither declared as Source nor assigned output().
        mailer.send(order);
    }

    void viaThis(Order order) {
        this.source.output().send(MessageBuilder.withPayload(order).build());
    }

    // Assigned something besides output(): not read as the output channel.
    private MessageChannel mixed;

    void mix(MessageChannel other, Order order) {
        mixed = source.output();
        mixed = other;
        mixed.send(MessageBuilder.withPayload(order).build());
    }

    // @SendTo only publishes from a stream listener.
    @SendTo("nowhere")
    public Order relay(Order order) { return order; }
}
"""

LISTENER = """
package invoice;

import org.springframework.cloud.stream.annotation.StreamListener;
import org.springframework.cloud.stream.messaging.Processor;
import org.springframework.messaging.handler.annotation.SendTo;

public class Invoices {
    private static final String RECEIPTS = "receipts";

    @StreamListener(Processor.INPUT)
    @SendTo(RECEIPTS)
    public Receipt invoice(Order order) { return null; }

    @StreamListener
    public void untargeted(Order order) {}

    @StreamListener(target = Config.name())
    public void computed(Order order) {}
}
"""

ORPHAN = """
package orphan;

import org.springframework.cloud.stream.annotation.StreamListener;
import org.springframework.cloud.stream.messaging.Sink;

public class Orphan {
    @StreamListener(Sink.INPUT)
    public void on(Order order) {}
}
"""

FILES = {
    "events/pom.xml": _pom("events"),
    f"events/{JAVA}/events/Publisher.java": PUBLISHER,
    "orders/pom.xml": _pom("orders", "events", "spring-cloud-stream-binder-kafka"),
    f"orders/{RESOURCES}/application.yml": (
        "spring:\n  application:\n    name: orders\n"
        "  cloud:\n    stream:\n      bindings:\n        output:\n"
        "          destination: order-events\n"
    ),
    # The config server's shared file names shipping's output destination
    # (camelCase searchLocations, as Spring's relaxed binding allows).
    "config/pom.xml": _pom("config"),
    f"config/{RESOURCES}/application.yml": (
        "spring:\n  cloud:\n    config:\n      server:\n        native:\n"
        "          searchLocations: classpath:/shared\n"
    ),
    f"config/{RESOURCES}/shared/shipping.yml": (
        "spring.cloud.stream.bindings.output.destination: shipments\n"
    ),
    # No destination configured for audit: the binding name is the destination.
    "shipping/pom.xml": _pom(
        "shipping", "events", "spring-cloud-starter-stream-rabbit"
    ),
    f"shipping/{RESOURCES}/application.properties": "spring.application.name=shipping\n",
    # Neither binder: unread.
    "mailer/pom.xml": _pom("mailer", "events"),
    f"mailer/{RESOURCES}/application.properties": "spring.application.name=mailer\n",
    # Both binders: unread.
    "both/pom.xml": _pom(
        "both",
        "events",
        "spring-cloud-stream-binder-kafka",
        "spring-cloud-stream-binder-rabbit",
    ),
    f"both/{RESOURCES}/application.properties": "spring.application.name=both\n",
    # Depends on the library but is not a service: the code does not run there.
    "events-tests/pom.xml": _pom(
        "events-tests", "events", "spring-cloud-stream-binder-kafka"
    ),
    # The binder comes from a repository module this one depends on.
    "kafka-starter/pom.xml": _pom("kafka-starter", "spring-cloud-stream-binder-kafka"),
    "invoice/pom.xml": _pom("invoice", "kafka-starter"),
    f"invoice/{RESOURCES}/application.properties": (
        "spring.application.name=invoice\n"
        "spring.cloud.stream.bindings.input.destination=order-events\n"
        "spring.cloud.stream.bindings.receipts.destination=${RECEIPTS_TOPIC}\n"
    ),
    f"invoice/{JAVA}/invoice/Invoices.java": LISTENER,
    "orphan/pom.xml": _pom("orphan"),
    f"orphan/{JAVA}/orphan/Orphan.java": ORPHAN,
}


def _write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class CloudStreamTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        _write(self.root, FILES)
        self.tool = ServiceContractTool(self.root)
        self.output = asyncio.run(self.tool.analyze("r"))
        self.endpoints, self.unread = self.tool.last_messages

    def tearDown(self):
        self.directory.cleanup()

    def test_endpoints(self):
        found = sorted(
            (e.file.rpartition("/")[2], e.line, e.role, e.broker, e.destination)
            for e in self.endpoints
        )
        self.assertEqual(
            found,
            [
                ("Invoices.java", 11, "consume", "kafka", "order-events"),
                # Published by the code in each service that depends on it.
                ("Publisher.java", 22, "publish", "amqp", "shipments/#"),
                ("Publisher.java", 22, "publish", "kafka", "order-events"),
                ("Publisher.java", 26, "publish", "amqp", "shipments/#"),
                ("Publisher.java", 26, "publish", "kafka", "order-events"),
                ("Publisher.java", 30, "publish", "amqp", "audit/#"),
                ("Publisher.java", 30, "publish", "kafka", "audit"),
                ("Publisher.java", 36, "publish", "amqp", "shipments/#"),
                ("Publisher.java", 36, "publish", "kafka", "order-events"),
            ],
        )
        self.assertEqual({e.client for e in self.endpoints}, {"spring-cloud-stream"})
        # mailer and both have no single binder.
        binder_unknown = [
            ("Publisher.java", line, "publish", "binder_unknown")
            for line in (22, 22, 26, 26, 30, 30, 36, 36)
        ]
        self.assertEqual(
            sorted(
                (u.file.rpartition("/")[2], u.line, u.role, u.reason)
                for u in self.unread
            ),
            [
                ("Invoices.java", 12, "publish", "destination_not_literal"),
                ("Invoices.java", 15, "consume", "no_target"),
                ("Invoices.java", 18, "consume", "binding_not_literal"),
                ("Orphan.java", 8, "consume", "no_service"),
                *binder_unknown,
            ],
        )

    def test_graph(self):
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[2],
                r.target_match["qualified_name"].split("::", 1)[1],
                r.properties["rule_id"],
            )
            for r in self.output.relationships
            if r.type in ("PUBLISHES", "CONSUMES")
        )
        self.assertIn(
            (
                "CONSUMES",
                "Invoices.invoice",
                "kafka::message-destination::order-events",
                "service-contracts.spring-cloud-stream-listener",
            ),
            edges,
        )
        self.assertIn(
            (
                "PUBLISHES",
                "Publisher.created",
                "kafka::message-destination::order-events",
                "service-contracts.spring-cloud-stream-output",
            ),
            edges,
        )
        for relationship in self.output.relationships:
            if relationship.type in ("PUBLISHES", "CONSUMES"):
                rule = get_rule(relationship.properties["rule_id"])
                self.assertEqual(rule.evidence_type, "pattern_matched")
                self.assertNotIn("confidence", relationship.properties)

    def test_deterministic(self):
        again = asyncio.run(ServiceContractTool(self.root).analyze("r"))
        self.assertEqual(
            [r.properties for r in again.relationships],
            [r.properties for r in self.output.relationships],
        )


if __name__ == "__main__":
    unittest.main()
