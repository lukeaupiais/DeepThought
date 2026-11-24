"""Spring routes declared by OpenAPI specs that openapi-generator turns
into interfaces, handled by the controllers that implement them."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.analysis_tools.evidence import get_rule
from deepthought.service_contracts.openapi import generated_apis

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
        apis, clients, unread = generated_apis(self.root)
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
                # Once per tag.
                "shop.api.PetTypesApi": [("listPetTypes", "GET", "/pet-types", 35)],
                "shop.api.PetsApi": [("listPetTypes", "GET", "/pet-types", 35)],
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
                # spring-cloud makes Feign clients only without interfaceOnly.
                ("owners/pom.xml", 1, "client_not_generated"),
                ("owners/pom.xml", 1, "generated_controller"),
                ("owners/pom.xml", 1, "generated_controller"),
                ("owners/pom.xml", 1, "spec_not_found"),
                # Read by the spring and the java execution.
                (f"owners/{RESOURCES}/openapi.yml", 25, "operation_id"),
                (f"owners/{RESOURCES}/openapi.yml", 25, "operation_id"),
                (f"owners/{RESOURCES}/openapi.yml", 28, "operation_id"),
                (f"owners/{RESOURCES}/openapi.yml", 28, "operation_id"),
            ],
        )
        # The java execution generates clients, grouped by tag only.
        self.assertEqual(
            sorted(clients),
            [
                "shop.api.DefaultApi",
                "shop.api.OwnerV2Api",
                "shop.api.PetTypesApi",
                "shop.api.PetsApi",
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
        self.assertEqual(metrics["openapi:client_not_generated"], 1)
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


def _client_pom(execution, *, parent="../pom.xml"):
    return (
        "<project><parent><artifactId>clients</artifactId>"
        f"<relativePath>{parent}</relativePath></parent>"
        "<artifactId>trips-client</artifactId>"
        "<build><plugins><plugin>"
        "<artifactId>openapi-generator-maven-plugin</artifactId>"
        f"<executions><execution><configuration>{execution}"
        "</configuration></execution></executions>"
        "</plugin></plugins></build></project>"
    )


TRIPS_SPEC = """
openapi: 3.0.1
servers:
  - url: "http://{service}:8080/v1"
    variables:
      service:
        default: trips
paths:
  /trips:
    post:
      tags: [trips]
      operationId: CreateTrip
  /trips/{id}:
    get:
      tags: [trips, admin]
      operationId: getTrip
"""

TRIPS = """
package trips;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/v1/trips")
public class TripController {
    @PostMapping
    public Trip create(Trip trip) { return trip; }

    @GetMapping("/{id}")
    public Trip get(String id) { return null; }
}

// The path the base URL set at run time is assumed to lead to.
@RestController
class LegacyTripController {
    @GetMapping("/trips/{id}")
    public Trip get(String id) { return null; }
}
"""

BOOKING = """
package booking;

import trips.client.api.TripsApi;
import trips.client.api.AdminApi;

public class Booking {
    private final TripsApi trips = new TripsApi();

    void book(AdminApi admin, String id) {
        this.trips.createTrip(null);
        trips.getTripWithHttpInfo(id);
        new TripsApi().getTripAsync(id, null);
        admin.getTrip(id);
        // Not an operation.
        trips.getApiClient();
    }

    // A name declared with two types is not read.
    void twice(TripsApi dual) { dual.getTrip("1"); }

    void again(Object dual) { dual.toString(); }
}
"""

REPORTS = """
package reports;

import trips.client.ApiClient;
import trips.client.api.TripsApi;

public class Reports {
    void run(TripsApi api, ApiClient client) {
        client.setBasePath("http://localhost:9000");
        api.getTrip("1");
    }
}
"""

CLIENT_FILES = {
    # The parent gives the plugin its version and generator.
    "pom.xml": (
        "<project><artifactId>clients</artifactId>"
        "<properties><openapi.version>4.3.1</openapi.version></properties>"
        "<build><pluginManagement><plugins><plugin>"
        "<artifactId>openapi-generator-maven-plugin</artifactId>"
        "<version>${openapi.version}</version>"
        "<configuration><generatorName>java</generatorName></configuration>"
        "</plugin></plugins></pluginManagement></build></project>"
    ),
    "trips-client/pom.xml": _client_pom(
        "<inputSpec>${project.basedir}/api.yml</inputSpec>"
        "<apiPackage>trips.client.api</apiPackage>"
    ),
    "trips-client/api.yml": TRIPS_SPEC,
    # Not read: a library whose classes do not send, a version before 4,
    # and a parent that is not the declared one.
    "assured/pom.xml": _client_pom(
        "<inputSpec>../trips-client/api.yml</inputSpec><apiPackage>a</apiPackage>"
        "<library>rest-assured</library>"
    ),
    "old/pom.xml": (
        "<project><build><plugins><plugin>"
        "<artifactId>openapi-generator-maven-plugin</artifactId>"
        "<version>3.3.4</version><executions><execution><configuration>"
        "<generatorName>java</generatorName><inputSpec>../trips-client/api.yml"
        "</inputSpec><apiPackage>o</apiPackage>"
        "</configuration></execution></executions></plugin></plugins></build>"
        "</project>"
    ),
    "orphan/pom.xml": _client_pom(
        "<inputSpec>../trips-client/api.yml</inputSpec><apiPackage>x</apiPackage>",
        parent="../old/pom.xml",
    ),
    # A configurationFile's settings apply under the pom's.
    "configured/pom.xml": _client_pom(
        "<inputSpec>../trips-client/api.yml</inputSpec><apiPackage>c</apiPackage>"
        "<configurationFile>${project.basedir}/generator.json</configurationFile>"
    ),
    "configured/generator.json": '{"library": "rest-assured", "apiPackage": "z"}',
    # No apiPackage: the java generator's default.
    "defaulted/pom.xml": _client_pom("<inputSpec>../trips-client/api.yml</inputSpec>"),
    f"trips/{RESOURCES}/application.properties": "spring.application.name=trips\n",
    "trips/pom.xml": "<project/>",
    f"trips/{JAVA}/trips/TripController.java": TRIPS,
    "booking/pom.xml": "<project/>",
    f"booking/{JAVA}/booking/Booking.java": BOOKING,
    "reports/pom.xml": "<project/>",
    f"reports/{JAVA}/reports/Reports.java": REPORTS,
}


@unittest.skipUnless(READY, "pyyaml or tree-sitter grammars are not installed")
class OpenApiClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        _write(self.root, CLIENT_FILES)
        self.tool = ServiceContractTool(self.root)
        self.output = asyncio.run(self.tool.analyze("r"))

    def tearDown(self):
        self.directory.cleanup()

    def test_generated_classes(self):
        servers, clients, unread = generated_apis(self.root)
        self.assertEqual(servers, {})
        self.assertEqual(
            sorted(c for c in clients if c.startswith("org.")),
            [
                "org.openapitools.client.api.AdminApi",
                "org.openapitools.client.api.TripsApi",
            ],
        )
        clients = {c: o for c, o in clients.items() if not c.startswith("org.")}
        self.assertEqual(
            {
                api: sorted(
                    (o.method_name, o.http_method, o.base_url + o.path, o.spec_line)
                    for o in operations
                )
                for api, operations in clients.items()
            },
            {
                "trips.client.api.TripsApi": [
                    ("createTrip", "POST", "http://trips:8080/v1/trips", 12),
                    ("getTrip", "GET", "http://trips:8080/v1/trips/{id}", 16),
                ],
                "trips.client.api.AdminApi": [
                    ("getTrip", "GET", "http://trips:8080/v1/trips/{id}", 16),
                ],
            },
        )
        self.assertEqual(
            sorted((u.file, u.reason) for u in unread),
            [
                ("assured/pom.xml", "client_library"),
                ("configured/pom.xml", "client_library"),
                ("old/pom.xml", "generator_version"),
                ("orphan/pom.xml", "not_spring_server"),
            ],
        )

    def test_requests(self):
        _, routes, requests, _, resolutions = self.tool.last_facts
        found = sorted(
            (r.file.rpartition("/")[2], r.line, r.method, r.url, r.declared_in)
            for r in requests
        )
        self.assertEqual(
            found,
            [
                (
                    "Booking.java",
                    11,
                    "POST",
                    "http://trips:8080/v1/trips",
                    "trips-client/api.yml:12",
                ),
                (
                    "Booking.java",
                    12,
                    "GET",
                    "http://trips:8080/v1/trips/{id}",
                    "trips-client/api.yml:16",
                ),
                (
                    "Booking.java",
                    13,
                    "GET",
                    "http://trips:8080/v1/trips/{id}",
                    "trips-client/api.yml:16",
                ),
                (
                    "Booking.java",
                    14,
                    "GET",
                    "http://trips:8080/v1/trips/{id}",
                    "trips-client/api.yml:16",
                ),
                # The module sets the base URL at run time.
                (
                    "Reports.java",
                    10,
                    "GET",
                    "\x00/trips/{id}",
                    "trips-client/api.yml:16",
                ),
            ],
        )
        by_id = {r.id: r for r in routes}
        self.assertEqual(
            sorted(
                (res.status, res.rule_id, by_id[res.route_id].path)
                for res in resolutions
            ),
            [("linked", "service-contracts.http-path", "/trips/{id}")]
            + [("linked", "service-contracts.http-service-path", "/v1/trips")]
            + [("linked", "service-contracts.http-service-path", "/v1/trips/{id}")] * 3,
        )

    def test_graph(self):
        requests = sorted(
            (
                r.properties["line"],
                r.properties["client"],
                r.properties["declared_in"],
                r.properties["rule_id"],
                r.source_match["qualified_name"].split("::")[2],
            )
            for r in self.output.relationships
            if r.type == "REQUESTS"
        )
        self.assertEqual(
            requests[0],
            (
                10,
                "openapi-java",
                "trips-client/api.yml:16",
                "service-contracts.openapi-client-path",
                "Reports.run",
            ),
        )
        self.assertEqual(
            requests[1],
            (
                11,
                "openapi-java",
                "trips-client/api.yml:12",
                "service-contracts.openapi-client-service-path",
                "Booking.book",
            ),
        )
        rule = get_rule("service-contracts.openapi-client-service-path")
        self.assertEqual(rule.evidence_type, "pattern_matched")


if __name__ == "__main__":
    unittest.main()


def _feign_pom(options, *, version="7.4.0", package="trips.feign.api"):
    return (
        "<project><build><plugins><plugin>"
        "<artifactId>openapi-generator-maven-plugin</artifactId>"
        f"<version>{version}</version><executions><execution><configuration>"
        "<generatorName>spring</generatorName><library>spring-cloud</library>"
        "<inputSpec>${project.basedir}/api.yml</inputSpec>"
        + (f"<apiPackage>{package}</apiPackage>" if package else "")
        # Spring takes no apiNamePrefix.
        + "<apiNamePrefix>Ignored</apiNamePrefix>"
        f"<configOptions><useTags>true</useTags>{options}</configOptions>"
        "</configuration></execution></executions></plugin></plugins></build>"
        "</project>"
    )


FEIGN_SPEC = """
openapi: 3.0.1
servers:
  - url: http://trips:8080
paths:
  /v1/trips:
    post:
      tags: [trips]
      operationId: createTrip
  /v1/trips/{id}:
    get:
      tags: [trips]
      operationId: getTrip
  # No operationId: the generator names it from the path and method.
  /v1/trips/{id}/stop-points:
    get:
      tags: [trips]
  # A segment it sanitizes: not read.
  /v1/trip_logs:
    get:
      tags: [trips]
"""

CHECKOUT = """
package checkout;

import trips.feign.api.TripsApi;
import trips.feign.api.TripsApiClient;

public class Checkout {
    private final TripsApiClient trips;

    Checkout(TripsApiClient trips) { this.trips = trips; }

    void run(TripsApi plain) {
        trips.createTrip(null);
        trips.getTrip("1");
        // Not a method of the Feign client.
        trips.getTripWithHttpInfo("1");
        // The interface, not the client: not read.
        plain.getTrip("1");
    }
}
"""

FEIGN_FILES = {
    "trips-feign/pom.xml": _feign_pom(""),
    "trips-feign/api.yml": FEIGN_SPEC,
    # Not read: no client with interfaceOnly, and a plugin before 7.
    "interfaces/pom.xml": _feign_pom("<interfaceOnly>true</interfaceOnly>"),
    "interfaces/api.yml": FEIGN_SPEC,
    "older/pom.xml": _feign_pom("", version="6.6.0"),
    "older/api.yml": FEIGN_SPEC,
    # apiPackage in configOptions, as the generator also reads it; given
    # twice with different values, not read.
    "optioned/pom.xml": _feign_pom(
        "<apiPackage>optioned.api</apiPackage>", package=None
    ),
    "optioned/api.yml": FEIGN_SPEC,
    "conflicting/pom.xml": _feign_pom("<apiPackage>other.api</apiPackage>"),
    "conflicting/api.yml": FEIGN_SPEC,
    f"trips/{RESOURCES}/application.properties": "spring.application.name=trips\n",
    "trips/pom.xml": "<project/>",
    f"trips/{JAVA}/trips/TripController.java": TRIPS,
    "checkout/pom.xml": "<project/>",
    f"checkout/{JAVA}/checkout/Checkout.java": CHECKOUT,
}


@unittest.skipUnless(READY, "pyyaml or tree-sitter grammars are not installed")
class OpenApiFeignClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        _write(self.root, FEIGN_FILES)
        self.tool = ServiceContractTool(self.root)
        self.output = asyncio.run(self.tool.analyze("r"))

    def tearDown(self):
        self.directory.cleanup()

    def test_generated_clients(self):
        servers, clients, unread = generated_apis(self.root)
        self.assertEqual(servers, {})
        self.assertEqual(
            {
                api: sorted(
                    (o.method_name, o.http_method, o.base_url + o.path, o.client)
                    for o in operations
                )
                for api, operations in clients.items()
            },
            {
                # The base is a property or service discovery: not fixed.
                api: [
                    ("createTrip", "POST", "\x00/v1/trips", "openapi-spring-cloud"),
                    ("getTrip", "GET", "\x00/v1/trips/{id}", "openapi-spring-cloud"),
                    (
                        "v1TripsIdStopPointsGet",
                        "GET",
                        "\x00/v1/trips/{id}/stop-points",
                        "openapi-spring-cloud",
                    ),
                ]
                for api in (
                    "trips.feign.api.TripsApiClient",
                    "optioned.api.TripsApiClient",
                )
            },
        )
        lines = {
            o.method_name: o.spec_line
            for o in clients["trips.feign.api.TripsApiClient"]
        }
        # The path key's line, without an operationId.
        self.assertEqual(lines["v1TripsIdStopPointsGet"], 15)
        self.assertEqual(
            sorted((u.file, u.line, u.reason) for u in unread),
            [
                ("conflicting/pom.xml", 1, "config_conflict"),
                ("interfaces/pom.xml", 1, "client_not_generated"),
                ("older/pom.xml", 1, "generator_version"),
                ("optioned/api.yml", 19, "operation_id"),
                ("trips-feign/api.yml", 19, "operation_id"),
            ],
        )

    def test_graph(self):
        requests = sorted(
            (
                r.properties["line"],
                r.properties["client"],
                r.properties["request"],
                r.properties["declared_in"],
                r.properties["rule_id"],
                r.source_match["qualified_name"].split("::")[2],
                r.target_match["qualified_name"].split("::")[1],
            )
            for r in self.output.relationships
            if r.type == "REQUESTS"
        )
        self.assertEqual(
            requests,
            [
                (
                    13,
                    "openapi-spring-cloud",
                    "POST {?}/v1/trips",
                    "trips-feign/api.yml:9",
                    "service-contracts.openapi-feign-client-path",
                    "Checkout.run",
                    f"trips/{JAVA}/trips/TripController.java",
                ),
                (
                    14,
                    "openapi-spring-cloud",
                    "GET {?}/v1/trips/{id}",
                    "trips-feign/api.yml:13",
                    "service-contracts.openapi-feign-client-path",
                    "Checkout.run",
                    f"trips/{JAVA}/trips/TripController.java",
                ),
            ],
        )
        rule = get_rule("service-contracts.openapi-feign-client-path")
        self.assertEqual(rule.evidence_type, "pattern_matched")
