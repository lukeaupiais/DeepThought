"""Services named and configured by the Compose services that build them."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.analysis_tools.evidence import get_rule
from deepthought.service_contracts.compose import compose_units
from deepthought.service_contracts.spring import SpringConfig
from go_ready import needs_go

READY = (
    importlib.util.find_spec("yaml")
    and importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_java")
)
JAVA = "src/main/java"
RESOURCES = "src/main/resources"

CONSUMER = """
package {package};

import java.util.function.Consumer;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class Processor {{
    @Bean
    public Consumer<String> messageProcessor() {{
        return event -> {{}};
    }}
}}
"""

BOTH_BINDERS = (
    "dependencies {\n"
    "  implementation 'org.springframework.cloud:spring-cloud-starter-stream-rabbit'\n"
    "  implementation 'org.springframework.cloud:spring-cloud-starter-stream-kafka'\n"
    "}\n"
)

PRODUCT_CONFIG = (
    "spring.cloud.stream:\n"
    "  defaultBinder: rabbit\n"
    "  bindings.messageProcessor-in-0.destination: products\n"
    "---\n"
    "spring.config.activate.on-profile: kafka\n"
    "spring.cloud.stream.defaultBinder: kafka\n"
)

FILES = {
    "settings.gradle": "include ':product', ':review', ':orders', ':odd'\n",
    # No spring.application.name: named by the Compose services, which run
    # it with rabbit in one deployment and kafka (a profile) in another.
    "product/build.gradle": BOTH_BINDERS,
    f"product/{RESOURCES}/application.yml": PRODUCT_CONFIG,
    f"product/{JAVA}/product/Processor.java": CONSUMER.format(package="product"),
    "docker-compose.yml": (
        "services:\n"
        "  product:\n"
        "    build: product\n"
        "    environment:\n"
        "      - SPRING_PROFILES_ACTIVE=docker\n"
        "  review:\n"
        "    build: {context: ./review}\n"
        "    environment:\n"
        "      SPRING_PROFILES_ACTIVE: docker\n"
        "      SPRING_CONFIG_LOCATION: file:/config/application.yml,file:/config/review.yml,optional:file:/config/none.yml\n"
        "    volumes:\n"
        "      - $PWD/config-repo:/config:ro\n"
        "  orders:\n"
        "    build: orders\n"
        "    environment: {SPRING_APPLICATION_NAME: order-service}\n"
        "  odd:\n"
        "    build: odd\n"
        "    environment: [SPRING_PROFILES_ACTIVE=$PROFILES]\n"
        "  mongodb:\n"
        "    image: mongo\n"
    ),
    "docker-compose-kafka.yml": (
        "services:\n"
        "  product:\n"
        "    build: product\n"
        "    environment:\n"
        "      - SPRING_PROFILES_ACTIVE=docker,kafka\n"
        "  review:\n"
        "    build: ./review\n"
        "    environment:\n"
        "      - SPRING_PROFILES_ACTIVE=docker,kafka\n"
        "      - SPRING_CONFIG_LOCATION=file:/config/application.yml,file:/config/review.yml\n"
        "    volumes:\n"
        "      - type: bind\n"
        "        source: ./config-repo\n"
        "        target: /config\n"
        "      - data:/data\n"
    ),
    # Replicas under two names: named after the module's directory.
    "docker-compose-partitions.yml": (
        "services:\n  orders-p0: {build: orders}\n  orders-p1: {build: orders}\n"
    ),
    # The review module's own config is not read: the location replaces it.
    "review/build.gradle": BOTH_BINDERS,
    f"review/{RESOURCES}/application.yml": "spring.application.name: ignored\n",
    f"review/{JAVA}/review/Processor.java": CONSUMER.format(package="review"),
    "config-repo/application.yml": (
        "spring.cloud.stream.defaultBinder: rabbit\n"
        "---\n"
        "spring.config.activate.on-profile: kafka\n"
        "spring.cloud.stream.defaultBinder: kafka\n"
    ),
    "config-repo/review.yml": (
        "spring.application.name: review\n"
        "spring.cloud.stream.bindings.messageProcessor-in-0.destination: reviews\n"
    ),
    "orders/build.gradle": (
        "dependencies { implementation 'org.springframework.cloud:spring-cloud-starter-stream-kafka' }\n"
    ),
    f"orders/{JAVA}/orders/Processor.java": CONSUMER.format(package="orders"),
    # The active profiles are not literal: its binder is not read.
    "odd/build.gradle": BOTH_BINDERS,
    f"odd/{RESOURCES}/application.yml": PRODUCT_CONFIG,
    f"odd/{JAVA}/odd/Processor.java": CONSUMER.format(package="odd"),
}


def _write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@unittest.skipUnless(READY, "pyyaml or tree-sitter grammars are not installed")
@needs_go
class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        _write(self.root, FILES)

    def tearDown(self):
        self.directory.cleanup()

    def test_units(self):
        units = {u.label: u for u in compose_units(self.root)}
        self.assertEqual(
            sorted(units),
            [
                "docker-compose-kafka.yml:product",
                "docker-compose-kafka.yml:review",
                "docker-compose-partitions.yml:orders-p0",
                "docker-compose-partitions.yml:orders-p1",
                "docker-compose.yml:odd",
                "docker-compose.yml:orders",
                "docker-compose.yml:product",
                "docker-compose.yml:review",
            ],
        )
        review = units["docker-compose.yml:review"]
        self.assertEqual(review.context, "review")
        self.assertEqual(
            review.host_path("/config/review.yml"), "config-repo/review.yml"
        )
        self.assertIsNone(review.host_path("/elsewhere/x.yml"))
        self.assertEqual(
            units["docker-compose-kafka.yml:review"].volumes,
            (("config-repo", "/config"), (None, "/data")),
        )
        self.assertIsNone(units["docker-compose.yml:odd"].env("SPRING_PROFILES_ACTIVE"))
        self.assertEqual(units["docker-compose.yml:odd"].env("UNSET"), "")

    def test_identity(self):
        config = SpringConfig(self.root)
        found = {
            module: (
                config.service(f"{module}/build.gradle")[0],
                config.identity(f"{module}/build.gradle")[0],
            )
            for module in ("product", "review", "orders", "odd")
        }
        self.assertEqual(
            found,
            {
                "product": ("product", "compose-service"),
                "review": ("review", "spring-application-name"),
                "orders": ("orders", "module-name"),
                "odd": ("odd", "compose-service"),
            },
        )

    def test_endpoints(self):
        tool = ServiceContractTool(self.root)
        output = asyncio.run(tool.analyze("r"))
        endpoints, unread = tool.last_messages
        self.assertEqual(
            sorted(
                (e.file.split("/")[0], e.broker, e.destination, e.deployed_as)
                for e in endpoints
            ),
            [
                (
                    "orders",
                    "kafka",
                    "messageProcessor-in-0",
                    (
                        "docker-compose-partitions.yml:orders-p0",
                        "docker-compose-partitions.yml:orders-p1",
                        "docker-compose.yml:orders",
                    ),
                ),
                ("product", "amqp", "products/#", ("docker-compose.yml:product",)),
                ("product", "kafka", "products", ("docker-compose-kafka.yml:product",)),
                ("review", "amqp", "reviews/#", ("docker-compose.yml:review",)),
                ("review", "kafka", "reviews", ("docker-compose-kafka.yml:review",)),
            ],
        )
        self.assertEqual(
            [(u.file.split("/")[0], u.reason) for u in unread],
            [("odd", "binder_unknown")],
        )
        consumes = {
            (r.target_match["qualified_name"].rsplit("::", 1)[1]): r.properties
            for r in output.relationships
            if r.type == "CONSUMES"
        }
        self.assertEqual(
            consumes["products"]["deployed_as"], ["docker-compose-kafka.yml:product"]
        )
        rule = get_rule(consumes["products"]["service_identity_rule"])
        self.assertEqual(rule.rule_id, "service-contracts.service-compose-name")
        self.assertEqual(
            consumes["reviews"]["service_identity_rule"],
            "service-contracts.service-spring-application-name",
        )


if __name__ == "__main__":
    unittest.main()
