"""Spring routes declared by OpenAPI specs that openapi-generator turns
into interfaces, handled by the controllers that implement them."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.analysis_tools.evidence import get_rule
from deepthought.service_contracts.openapi import generated_spring_apis

READY = (
    importlib.util.find_spec("yaml")
    and importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_java")
)
JAVA = "src/main/java"
RESOURCES = "src/main/resources"


def _pom(*executions, version="${openapi.version}"):
    found = "".join(
        f"<execution><configuration>{e}</configuration></execution>" for e in executions
    )
    return (
        '<project xmlns="http://maven.apache.org/POM/4.0.0">'
        "<artifactId>a</artifactId>"
        "<properties><openapi.version>7.4.0</openapi.version>"
        "<api.package>shop.api</api.package></properties>"
        "<build><plugins><plugin>"
        "<groupId>org.openapitools</groupId>"
        "<artifactId>openapi-generator-maven-plugin</artifactId>"
        f"<version>{version}</version>"
        f"<executions>{found}</executions>"
        "</plugin></plugins></build></project>"
    )


def _execution(spec, *, options="", extra="", generator="spring"):
    return (
        f"<generatorName>{generator}</generatorName>"
        f"<inputSpec>${{project.basedir}}/{spec}</inputSpec>"
        "<apiPackage>${api.package}</apiPackage>"
        f"{extra}<configOptions><interfaceOnly>true</interfaceOnly>{options}"
        "</configOptions>"
    )


SPEC = """
openapi: 3.0.1
paths:
  /owners:
    get:
      tags: [owner-v2]
      operationId: listOwners
    post:
      tags: [owner-v2]
      operationId: addOwner
  /owners/{ownerId}:
    get:
      tags: [owner-v2]
      operationId: getOwnerById
    delete:
      tags: [owner-v2]
      operationId: deleteOwner
  /owners/{ownerId}/pets:
    put:
      tags: [owner-v2]
      operationId: updatePets
  /reports:
    get:
      tags: [reports]
      operationId: get-report
    post:
      tags: [reports]
      operationId: Default
  /audit:
    get:
      operationId: audit
  /pet-types:
    get:
      tags: [petTypes, pets]
      operationId: listPetTypes
"""

# Grouped by first path segment: no useTags.
SWAGGER = """{
  "swagger": "2.0",
  "paths": {
    "/carts/{id}": {
      "get": {"operationId": "getCart"},
      "delete": {"operationId": "dropCart"}
    },
    "/": {"get": {"operationId": "root"}},
    "/{id}": {"get": {"operationId": "byId"}}
  }
}
"""

OWNERS = """
package shop.web;

import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import shop.api.OwnerV2Api;

@RestController
@RequestMapping("api")
public class OwnerController implements OwnerV2Api, java.io.Serializable {
    @Override
    public ResponseEntity<List<Owner>> listOwners(String lastName) {
        return null;
    }

    @Override
    public ResponseEntity<Owner> addOwner(Owner owner) { return null; }

    @Override
    public ResponseEntity<Owner> getOwnerById(Integer ownerId) { return null; }

    // deleteOwner is not overridden: the generated default answers 501.

    public void updatePets(Integer ownerId) {}

    public void updatePets(Integer ownerId, Pet pet) {}

    // Not an operation of this interface.
    public void audit() {}
}
"""

# Not a controller: Spring maps nothing for it.
PLAIN = """
package shop.web;

import shop.api.DefaultApi;

public class Plain implements DefaultApi {
    public void audit() {}
}
"""

CARTS = """
package carts;

import org.springframework.web.bind.annotation.RestController;

@RestController
public class CartController implements carts.api.CartsApi {
    public Cart getCart(String id) { return null; }

    public void dropCart(String id) {}
}
"""

CLIENT = """
package front;

import org.springframework.web.client.RestTemplate;

public class OwnersClient {
    private RestTemplate rest;

    Owner first() {
        return rest.getForObject("http://owners/shop/api/owners/1", Owner.class);
    }
}
"""

FILES = {
    "owners/pom.xml": _pom(
        _execution(
            "src/main/resources/openapi.yml",
            options="<useTags>true</useTags>",
        ),
        # Not read: another generator, a generated controller, a base path
        # on the interface, a spring-cloud (Feign) client, a missing spec.
        _execution("src/main/resources/openapi.yml", generator="java"),
        "<generatorName>spring</generatorName>"
        "<inputSpec>src/main/resources/openapi.yml</inputSpec>"
        "<apiPackage>x</apiPackage>",
        _execution(
            "src/main/resources/openapi.yml",
            options="<requestMappingMode>api_interface</requestMappingMode>",
        ),
        _execution(
            "src/main/resources/openapi.yml",
            extra="<library>spring-cloud</library>",
        ),
        _execution("src/main/resources/missing.yml"),
        _execution(
            "src/main/resources/openapi.yml",
            options="<delegatePattern>true</delegatePattern>",
        ),
    ),
    f"owners/{RESOURCES}/openapi.yml": SPEC,
    f"owners/{RESOURCES}/application.properties": (
        "spring.application.name=owners\nserver.servlet.context-path=/shop\n"
    ),
    f"owners/{JAVA}/shop/web/OwnerController.java": OWNERS,
    f"owners/{JAVA}/shop/web/Plain.java": PLAIN,
    # Plugin-level configuration applies to each execution.
    "carts/pom.xml": (
        "<project><build><plugins><plugin>"
        "<artifactId>openapi-generator-maven-plugin</artifactId>"
        "<version>7.0.1</version>"
        "<configuration><configOptions><interfaceOnly>true</interfaceOnly>"
        "</configOptions></configuration>"
        "<executions><execution><configuration>"
        "<generatorName>spring</generatorName>"
        "<inputSpec>${basedir}/api/carts.json</inputSpec>"
        "<apiPackage>carts.api</apiPackage>"
        "</configuration></execution></executions>"
        "</plugin></plugins></build></project>"
    ),
    "carts/api/carts.json": SWAGGER,
    f"carts/{JAVA}/carts/CartController.java": CARTS,
    # Plugin 6 generates a base path on the interface: not read.
    "legacy/pom.xml": _pom(_execution("openapi.yml"), version="6.6.0"),
    "legacy/openapi.yml": SPEC,
    "front/pom.xml": "<project/>",
    f"front/{RESOURCES}/application.properties": "spring.application.name=front\n",
    f"front/{JAVA}/front/OwnersClient.java": CLIENT,
}


def _write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@unittest.skipUnless(READY, "pyyaml or tree-sitter grammars are not installed")
class OpenApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        _write(self.root, FILES)
        self.tool = ServiceContractTool(self.root)
        self.output = asyncio.run(self.tool.analyze("r"))

    def tearDown(self):
        self.directory.cleanup()

    def test_generated_interfaces(self):
        apis, unread = generated_spring_apis(self.root)
        self.assertEqual(
            {
                interface: sorted(
                    (o.method_name, o.http_method, o.path, o.spec_line)
                    for o in operations
                )
                for interface, operations in apis.items()
            },
            {
                "shop.api.OwnerV2Api": [
                    ("addOwner", "POST", "/owners", 10),
                    ("deleteOwner", "DELETE", "/owners/{ownerId}", 17),
                    ("getOwnerById", "GET", "/owners/{ownerId}", 14),
                    ("listOwners", "GET", "/owners", 7),
                    ("updatePets", "PUT", "/owners/{ownerId}/pets", 21),
                ],
                "shop.api.DefaultApi": [("audit", "GET", "/audit", 31)],
                "shop.api.PetTypesApi": [("listPetTypes", "GET", "/pet-types", 35)],
                "carts.api.CartsApi": [
                    ("dropCart", "DELETE", "/carts/{id}", 6),
                    ("getCart", "GET", "/carts/{id}", 5),
                ],
                "carts.api.DefaultApi": [("root", "GET", "/", 8)],
            },
        )
        self.assertEqual(
            sorted((u.file, u.line, u.reason) for u in unread),
            [
                ("carts/api/carts.json", 9, "api_name"),
                ("legacy/pom.xml", 1, "generator_version"),
                ("owners/pom.xml", 1, "base_path_on_interface"),
                ("owners/pom.xml", 1, "generated_controller"),
                ("owners/pom.xml", 1, "generated_controller"),
                ("owners/pom.xml", 1, "not_spring_server"),
                ("owners/pom.xml", 1, "not_spring_server"),
                ("owners/pom.xml", 1, "spec_not_found"),
                (f"owners/{RESOURCES}/openapi.yml", 25, "operation_id"),
                (f"owners/{RESOURCES}/openapi.yml", 28, "operation_id"),
            ],
        )

    def test_routes(self):
        _, routes, _, unread, resolutions = self.tool.last_facts
        found = sorted(
            (
                r.file.rpartition("/")[2],
                r.line,
                r.methods,
                r.path,
                r.framework,
                r.service,
                r.declared_in.rpartition("/")[2],
            )
            for r in routes
            if r.framework == "openapi-spring"
        )
        self.assertEqual(
            found,
            [
                (
                    "CartController.java",
                    8,
                    ("GET",),
                    "/carts/{id}",
                    "openapi-spring",
                    None,
                    "carts.json:5",
                ),
                (
                    "CartController.java",
                    10,
                    ("DELETE",),
                    "/carts/{id}",
                    "openapi-spring",
                    None,
                    "carts.json:6",
                ),
                (
                    "OwnerController.java",
                    11,
                    ("GET",),
                    "/shop/api/owners",
                    "openapi-spring",
                    "owners",
                    "openapi.yml:7",
                ),
                (
                    "OwnerController.java",
                    16,
                    ("POST",),
                    "/shop/api/owners",
                    "openapi-spring",
                    "owners",
                    "openapi.yml:10",
                ),
                (
                    "OwnerController.java",
                    19,
                    ("GET",),
                    "/shop/api/owners/{ownerId}",
                    "openapi-spring",
                    "owners",
                    "openapi.yml:14",
                ),
            ],
        )
        self.assertEqual(
            sorted(
                (u.file.rpartition("/")[2], u.reason)
                for u in unread
                if u.reason in ("not_overridden", "overloaded")
            ),
            [
                ("OwnerController.java", "not_overridden"),
                ("OwnerController.java", "overloaded"),
            ],
        )
        metrics = self.output.metadata.custom_metrics["unread"]
        # deleteOwner, updatePets, audit, listPetTypes and root have no
        # handler.
        self.assertEqual(metrics["openapi:operation_not_handled"], 5)
        self.assertEqual(metrics["openapi:not_spring_server"], 2)
        linked = [r for r in resolutions if r.status == "linked"]
        self.assertEqual(len(linked), 1)

    def test_graph(self):
        nodes = {
            n.properties["display_name"]: n.properties
            for n in self.output.nodes
            if "HttpRoute" in n.labels
        }
        owners = nodes["GET /shop/api/owners/{ownerId}"]
        self.assertEqual(owners["declared_in"], f"owners/{RESOURCES}/openapi.yml:14")
        handles = [
            r
            for r in self.output.relationships
            if r.type == "HANDLES"
            and r.target_match["qualified_name"].endswith(owners["route_id"])
        ]
        self.assertEqual(len(handles), 1)
        self.assertEqual(
            handles[0].source_match["qualified_name"].split("::")[2],
            "OwnerController.getOwnerById",
        )
        rule = get_rule(handles[0].properties["rule_id"])
        self.assertEqual(rule.rule_id, "service-contracts.openapi-spring-route")
        self.assertEqual(rule.evidence_type, "pattern_matched")
        requests = [
            r
            for r in self.output.relationships
            if r.type == "REQUESTS"
            and r.target_match["qualified_name"].endswith(owners["route_id"])
        ]
        self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
