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
        # Library code records the service that runs it; a service's own
        # code does not.
        self.assertEqual(
            sorted(
                {
                    (e.file.rpartition("/")[2], e.module, e.service)
                    for e in self.endpoints
                },
                key=str,
            ),
            [
                ("Invoices.java", None, None),
                ("Publisher.java", "orders", "orders"),
                ("Publisher.java", "shipping", "shipping"),
            ],
        )
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
        self.assertEqual(
            sorted(
                r.properties.get("service", "")
                for r in self.output.relationships
                if r.type == "PUBLISHES"
            ),
            ["orders"] * 4 + ["shipping"] * 4,
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


FUNCTIONS = """
package billing;

import java.util.function.Consumer;
import java.util.function.Function;
import java.util.function.Supplier;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class Functions {
    @Bean
    public Function<Order, Receipt> charge() { return o -> null; }

    @Bean
    public Consumer<Order> audit() { return o -> {}; }

    @Bean
    public Consumer<Order> log() { return o -> {}; }

    @Bean
    public Supplier<Order> heartbeat() { return () -> null; }

    // Not a functional bean.
    @Bean
    public Clock clock() { return null; }

    // Not a bean.
    @Deprecated
    public Consumer<Order> helper() { return o -> {}; }
}
"""

STREAMS = """
package joins;

import java.util.function.BiFunction;
import java.util.function.Function;
import org.springframework.context.annotation.Bean;

public class Joins {
    @Bean
    public BiFunction<KStream<String, Long>, KTable<String, String>, KStream<String, Long>> join() { return null; }

    @Bean
    public Function<KStream<Object, String>, KStream<?, WordCount>[]> branch() { return null; }

    @Bean
    public Function<byte[], byte[]> raw() { return null; }

    @Bean
    public Function<KStream<String, Long>, Function<KTable<String, String>, KStream<String, Long>>> curried() { return null; }
}
"""

TICKER = """
package ticker;

import java.util.function.Supplier;
import org.springframework.context.annotation.Bean;

public class Ticker {
    @Bean(name = "ticks")
    public Supplier<Long> source() { return () -> 1L; }

    // Not java.util.function: not a functional bean.
    @Bean
    public com.acme.Function<Long, Long> mapper() { return null; }
}
"""

MULTI = """
package multi;

import java.util.function.Consumer;
import org.springframework.context.annotation.Bean;

public class Handlers {
    @Bean
    public Consumer<String> first() { return s -> {}; }

    @Bean
    public Consumer<String> second() { return s -> {}; }
}
"""

FUNCTION_FILES = {
    "billing/pom.xml": _pom("billing", "spring-cloud-stream-binder-kafka"),
    f"billing/{RESOURCES}/application.properties": (
        "spring.application.name=billing\n"
        "spring.cloud.function.definition=charge;audit|log\n"
        "spring.cloud.stream.bindings.charge-in-0.destination=orders\n"
        "spring.cloud.stream.function.bindings.charge-out-0=receipts\n"
        "spring.cloud.stream.bindings.receipts.destination=billing-receipts\n"
    ),
    f"billing/{JAVA}/billing/Functions.java": FUNCTIONS,
    # No definition: the only functional bean is bound, under its bean name.
    "ticker/pom.xml": _pom("ticker", "spring-cloud-stream-binder-rabbit"),
    f"ticker/{RESOURCES}/application.properties": "spring.application.name=ticker\n",
    f"ticker/{JAVA}/ticker/Ticker.java": TICKER,
    # No definition and two functional beans: neither is bound.
    "multi/pom.xml": _pom("multi", "spring-cloud-stream-binder-kafka"),
    f"multi/{RESOURCES}/application.properties": "spring.application.name=multi\n",
    f"multi/{JAVA}/multi/Handlers.java": MULTI,
    # Kafka Streams shapes; the older definition key.
    "joins/pom.xml": _pom("joins", "spring-cloud-stream-binder-kafka-streams"),
    f"joins/{RESOURCES}/application.properties": (
        "spring.application.name=joins\n"
        "spring.cloud.stream.function.definition=join;branch;raw;curried\n"
        "spring.cloud.stream.bindings.join-in-0.destination=clicks\n"
        "spring.cloud.stream.bindings.join-in-1.destination=regions\n"
    ),
    f"joins/{JAVA}/joins/Joins.java": STREAMS,
    # Two binders and no rule to pick one: unread.
    "both/pom.xml": _pom(
        "both", "spring-cloud-stream-binder-kafka", "spring-cloud-stream-binder-rabbit"
    ),
    f"both/{RESOURCES}/application.properties": "spring.application.name=both\n",
    f"both/{JAVA}/both/Ticker.java": TICKER.replace("package ticker", "package both"),
    # Not a stream application: its functional bean binds nothing.
    "web/pom.xml": _pom("web"),
    f"web/{RESOURCES}/application.properties": "spring.application.name=web\n",
    f"web/{JAVA}/web/Handlers.java": MULTI.replace("package multi", "package web"),
}


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class FunctionalBindingTests(unittest.TestCase):
    def test_functions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root, FUNCTION_FILES)
            tool = ServiceContractTool(root)
            asyncio.run(tool.analyze("r"))
            endpoints, unread = tool.last_messages
        self.assertEqual(
            sorted(
                (e.file.rpartition("/")[2], e.line, e.role, e.broker, e.destination)
                for e in endpoints
            ),
            [
                ("Functions.java", 12, "consume", "kafka", "orders"),
                ("Functions.java", 12, "publish", "kafka", "billing-receipts"),
                ("Joins.java", 9, "consume", "kafka", "clicks"),
                ("Joins.java", 9, "consume", "kafka", "regions"),
                ("Joins.java", 9, "publish", "kafka", "join-out-0"),
                ("Joins.java", 12, "consume", "kafka", "branch-in-0"),
                ("Joins.java", 15, "consume", "kafka", "raw-in-0"),
                ("Joins.java", 15, "publish", "kafka", "raw-out-0"),
                ("Ticker.java", 8, "publish", "amqp", "ticks-out-0/#"),
            ],
        )
        self.assertEqual({e.client for e in endpoints}, {"spring-cloud-function"})
        self.assertEqual(
            sorted(
                (u.file.rpartition("/")[2], u.line, u.role, u.reason) for u in unread
            ),
            [
                ("Functions.java", 15, "consume", "function_composed"),
                ("Functions.java", 18, "consume", "function_composed"),
                ("Functions.java", 21, "publish", "function_not_bound"),
                ("Handlers.java", 8, "consume", "function_not_bound"),
                ("Handlers.java", 11, "consume", "function_not_bound"),
                ("Joins.java", 12, "publish", "function_outputs"),
                ("Joins.java", 18, "consume", "function_curried"),
                ("Ticker.java", 8, "publish", "binder_unknown"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
