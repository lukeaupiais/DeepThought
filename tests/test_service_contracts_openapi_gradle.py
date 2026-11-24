"""openapi-generator executions in Gradle builds."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.service_contracts.openapi import generated_apis
from deepthought.service_contracts.openapi_gradle import gradle_executions

READY = (
    importlib.util.find_spec("yaml")
    and importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_java")
)
JAVA = "src/main/java"

SPEC = """
openapi: 3.0.1
servers:
  - url: http://orders:8080
paths:
  /orders/{id}:
    get:
      tags: [orders]
      operationId: getOrder
"""

GROOVY = """
plugins {
    id 'java'
    id "org.openapi.generator" version "$openapiVersion"
}

// openApiGenerate { generatorName = "kotlin" }
openApiGenerate {
    generatorName = "spring"
    inputSpec = "$rootDir/specs/orders.yaml".toString()
    outputDir = "$buildDir/generated"
    apiPackage = 'shop.orders.api'
    configOptions = [
        interfaceOnly: "true",
        "useTags": true,
        dateLibrary: "java8",
    ]
    globalProperties = [modelDocs: "false"]
}

task ordersClient(type: org.openapitools.generator.gradle.plugin.tasks.GenerateTask) {
    generatorName = "java"
    library "resttemplate"
    inputSpec = "${rootDir}/specs/orders.yaml"
    apiPackage = "shop.client.api"
}

task dynamic(type: GenerateTask) {
    generatorName = "spring"
    inputSpec = "$specDir/orders.yaml"
}
"""

KOTLIN = """
plugins {
    id("org.openapi.generator") version "7.4.0"
}

tasks.register<org.openapitools.generator.gradle.plugin.tasks.GenerateTask>("api") {
    generatorName.set("spring")
    inputSpec.set("$projectDir/api.yaml")
    apiPackage.set("billing.api")
    configOptions.set(mapOf("interfaceOnly" to "true", "useSpringBoot3" to "true"))
}

val client by tasks.registering(GenerateTask::class) {
    generatorName.set("java")
    inputSpec.set("api.yaml")
    configOptions.set(options)
}
"""

CONTROLLER = """
package shop.orders;

import org.springframework.web.bind.annotation.RestController;
import shop.orders.api.OrdersApi;

@RestController
public class OrderController implements OrdersApi {
    @Override
    public Order getOrder(String id) { return null; }
}
"""

CALLER = """
package shop.front;

import shop.client.api.OrdersApi;

public class Front {
    private final OrdersApi orders;

    Front(OrdersApi orders) { this.orders = orders; }

    Order show(String id) { return orders.getOrder(id); }
}
"""

FILES = {
    "settings.gradle": "include 'orders', 'front'\n",
    "gradle.properties": "openapiVersion=7.4.0\n",
    "specs/orders.yaml": SPEC,
    "orders/build.gradle": GROOVY,
    "orders/src/main/resources/application.properties": "spring.application.name=orders\n",
    f"orders/{JAVA}/shop/orders/OrderController.java": CONTROLLER,
    "front/build.gradle": "dependencies { implementation project(':orders') }\n",
    f"front/{JAVA}/shop/front/Front.java": CALLER,
    # A build of its own (no settings above it): Kotlin DSL.
    "billing/settings.gradle.kts": "",
    "billing/build.gradle.kts": KOTLIN,
    "billing/api.yaml": SPEC.replace("/orders/{id}", "/bills/{id}").replace(
        "getOrder", "getBill"
    ),
}


def _write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@unittest.skipUnless(READY, "pyyaml or tree-sitter grammars are not installed")
class GradleOpenApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        _write(self.root, FILES)

    def tearDown(self):
        self.directory.cleanup()

    def test_executions(self):
        found = [
            (
                build,
                line,
                version,
                config
                if isinstance(config, str)
                else (
                    config.get("generatorName"),
                    config.get("inputSpec").lstrip("\\0/"),
                    config.get("apiPackage"),
                    config.get("library"),
                    config["configOptions"],
                ),
            )
            for build, line, version, config in gradle_executions(self.root)
        ]
        self.assertEqual(
            found,
            [
                (
                    "billing/build.gradle.kts",
                    6,
                    "7.4.0",
                    (
                        "spring",
                        "billing/api.yaml",
                        "billing.api",
                        None,
                        {"interfaceOnly": "true", "useSpringBoot3": "true"},
                    ),
                ),
                ("billing/build.gradle.kts", 13, "7.4.0", "config_not_literal"),
                (
                    "orders/build.gradle",
                    8,
                    "7.4.0",
                    (
                        "spring",
                        "specs/orders.yaml",
                        "shop.orders.api",
                        None,
                        {
                            "interfaceOnly": "true",
                            "useTags": "true",
                            "dateLibrary": "java8",
                        },
                    ),
                ),
                (
                    "orders/build.gradle",
                    21,
                    "7.4.0",
                    (
                        "java",
                        "specs/orders.yaml",
                        "shop.client.api",
                        "resttemplate",
                        {},
                    ),
                ),
                ("orders/build.gradle", 28, "7.4.0", "config_not_literal"),
            ],
        )

    def test_generated(self):
        servers, clients, unread = generated_apis(self.root)
        self.assertEqual(
            {k: [(o.method_name, o.path) for o in v] for k, v in servers.items()},
            {
                "shop.orders.api.OrdersApi": [("getOrder", "/orders/{id}")],
                "billing.api.BillsApi": [("getBill", "/bills/{id}")],
            },
        )
        self.assertEqual(
            {k: [o.base_url for o in v] for k, v in clients.items()},
            {"shop.client.api.OrdersApi": ["http://orders:8080"]},
        )
        self.assertEqual(
            sorted((u.file, u.line, u.reason) for u in unread),
            [
                ("billing/build.gradle.kts", 13, "config_not_literal"),
                ("orders/build.gradle", 28, "config_not_literal"),
            ],
        )

    def test_linked(self):
        tool = ServiceContractTool(self.root)
        asyncio.run(tool.analyze("r"))
        _, routes, requests, _, resolutions = tool.last_facts
        by_id = {r.id: r for r in routes}
        self.assertEqual(
            sorted((r.status, r.rule_id, by_id[r.route_id].file) for r in resolutions),
            [
                (
                    "linked",
                    "service-contracts.http-service-path",
                    f"orders/{JAVA}/shop/orders/OrderController.java",
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
