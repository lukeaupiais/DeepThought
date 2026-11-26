"""Requests forwarded by API gateways and reverse proxies."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.analysis_tools.evidence import get_rule
from deepthought.service_contracts.gateways import (
    GatewayRoute,
    forward,
    nginx_routes,
    ocelot_routes,
    spring_gateway_routes,
    yarp_routes,
)
from deepthought.service_contracts.http import PLACEHOLDER
from deepthought.service_contracts.model import HttpRequest, HttpRoute
from deepthought.service_contracts.resolve import (
    HTTP_GATEWAY,
    HTTP_PATH,
    resolve_requests,
)
from typescript_ready import needs_typescript

YAML = importlib.util.find_spec("yaml")
READY = (
    YAML
    and importlib.util.find_spec("tree_sitter")
    and all(
        importlib.util.find_spec(f"tree_sitter_{n}")
        for n in ("java", "javascript", "python")
    )
)
U = PLACEHOLDER

GATEWAY_YAML = """
spring:
  application:
    name: api-gateway
  cloud:
    gateway:
      routes:
        - id: vets
          uri: lb://vets-service
          predicates:
            - Path=/api/vet/**,/vets-api/**
          filters:
            - StripPrefix=2
        - id: visits
          uri: http://visits-service:8080
          predicates:
            - name: Path
              args:
                patterns: /api/visit/**
          filters:
            - name: StripPrefix
              args:
                parts: 1
            - PrefixPath=/v2
        - id: rewritten
          uri: lb://other
          predicates:
            - Path=/api/other/**
          filters:
            - RewritePath=/api/other/(?<rest>.*), /$\\{rest}
        - id: renamed
          uri: lb://renamed
          predicates:
            - Path=/api/renamed/v1/**
          filters:
            - RewritePath=^/api/renamed(?<rest>/?.*)$, /v2$\\{rest}
        - id: moved
          uri: lb://other
          predicates:
            - Path=/api/moved/**
          filters:
            - RewritePath=/api/moved/(?<rest>.*), /x/$\\{rest}/y
        - id: twice
          uri: lb://other
          predicates:
            - Path=/api/twice/**
          filters:
            - StripPrefix=1
            - RewritePath=/twice/(?<rest>.*), /$\\{rest}
        - id: elsewhere
          uri: lb://other
          predicates:
            - Path=/api/elsewhere/**
          filters:
            - RewritePath=/elsewhere/?(?<rest>.*), /$\\{rest}
        - id: pattern
          uri: lb://other
          predicates:
            - Path=/api/*/x/**
---
spring:
  config:
    activate:
      on-profile: docker
  cloud:
    gateway:
      routes:
        - id: docker-only
          uri: lb://docker
          predicates:
            - Path=/docker/**
"""

NGINX = """
server {
    location / {
        root /usr/share/nginx/html;
    }
    # location /commented/ { proxy_pass http://nowhere/; }
    location /api/cart/ {
        proxy_pass http://${CART_HOST}:8080/;
    }
    location /api/keep/ {
        proxy_pass http://keep:8080;
    }
    location ^~ /api/v2/ {
        proxy_pass http://user:8080/users/;
    }
    location = /api/exact/ {
        proxy_pass http://exact/;
    }
    location ~ \\.php$ {
        proxy_pass http://php;
    }
    location /api/var/ {
        proxy_pass http://user:8080/$1;
    }
}
"""

YARP = """{
  // .NET configuration allows comments.
  "Logging": { "LogLevel": { "Default": "Information" } },
  "ReverseProxy": {
    "Routes": {
      "catalog-route": {
        "ClusterId": "catalog",
        "Match": { "Path": "/catalog-service/{**catch-all}" },
        "Transforms": [ { "PathPattern": "{**catch-all}" } ]
      },
      "orders-route": {
        "ClusterId": "orders",
        "Match": { "Path": "/api/orders/{**rest}", "Methods": [ "GET" ] },
        "Transforms": [ { "PathRemovePrefix": "/api" }, { "PathPrefix": "/v1" } ]
      },
      "kept-route": {
        "ClusterId": "spread",
        "Match": { "Path": "/kept/{*rest}" }
      },
      "set-route": {
        "ClusterId": "catalog",
        "Match": { "Path": "/set/{**rest}" },
        "Transforms": [ { "PathSet": "/fixed" } ]
      },
      "value-route": {
        "ClusterId": "catalog",
        "Match": { "Path": "/items/{id}/{**rest}" }
      },
      "other-pattern": {
        "ClusterId": "catalog",
        "Match": { "Path": "/other/{**rest}" },
        "Transforms": [ { "PathPattern": "/x/{**else}" } ]
      },
      "split-route": {
        "ClusterId": "split",
        "Match": { "Path": "/split/{**rest}" }
      },
    },
    "Clusters": {
      "catalog": { "Destinations": { "d1": { "Address": "http://catalog.api:8080" } } },
      "orders": { "Destinations": { "d1": { "Address": "http://orders/base/" } } },
      "spread": {
        "Destinations": {
          "a": { "Address": "http://a:80/" },
          "b": { "Address": "http://b:80/" }
        }
      },
      "split": {
        "Destinations": {
          "a": { "Address": "http://a/one" },
          "b": { "Address": "http://a/two" }
        }
      }
    }
  }
}
"""

OCELOT = """{
  "Routes": [
    {
      "DownstreamPathTemplate": "/api/{everything}",
      "UpstreamPathTemplate": "/merchant/api/{everything}",
      "ServiceName": "Merchant.Api",
      "UpstreamHttpMethod": [ "Get", "POST" ]
    },
    {
      "DownstreamPathTemplate": "/api/catalog/{id}",
      "DownstreamHostAndPorts": [ { "Host": "catalog", "Port": 80 } ],
      "UpstreamPathTemplate": "/catalog/{id}"
    },
    {
      "DownstreamPathTemplate": "/api/basket?userId={userId}",
      "DownstreamHostAndPorts": [ { "Host": "a", "Port": 80 }, { "Host": "b" } ],
      "UpstreamPathTemplate": "/basket?userId={userId}"
    },
    {
      "DownstreamPathTemplate": "/{url}",
      "UpstreamPathTemplate": "/{url}"
    },
    {
      "DownstreamPathTemplate": "/api/{id}/items",
      "UpstreamPathTemplate": "/items/{id}/list"
    },
    {
      "DownstreamPathTemplate": "/api/{b}/{a}",
      "UpstreamPathTemplate": "/pair/{a}/{b}"
    }
  ]
}
"""


class GatewayReaderTests(unittest.TestCase):
    @unittest.skipUnless(YAML, "pyyaml is not installed")
    def test_spring_cloud_gateway(self):
        routes, unread = spring_gateway_routes(None, GATEWAY_YAML, "g/application.yml")
        self.assertEqual(
            [(r.prefix, r.replacement, r.target) for r in routes],
            [
                (("api", "vet"), (), "vets-service"),
                # /vets-api/** cannot lose two segments to StripPrefix=2.
                (("api", "visit"), ("v2", "visit"), "visits-service"),
                # RewritePath that swaps a literal prefix.
                (("api", "other"), (), "other"),
                (("api", "renamed", "v1"), ("v2", "v1"), "renamed"),
            ],
        )
        self.assertEqual(
            routes[0].line,
            GATEWAY_YAML[: GATEWAY_YAML.find("id: vets")].count("\n") + 1,
        )
        self.assertEqual(
            [u.reason for u in unread],
            [
                "strip_beyond_prefix",
                # The replacement does not end with the rest of the path.
                "path_rewritten",
                # Read alone only: filters apply in order.
                "path_rewritten",
                # The regex does not match the start of every routed path.
                "path_rewritten",
                "path_pattern",
            ],
        )

    def test_yarp(self):
        routes, unread = yarp_routes(YARP, "gw/appsettings.json")
        self.assertEqual(
            [(r.prefix, r.replacement, r.target, r.kind) for r in routes],
            [
                (("catalog-service",), (), "catalog.api", "yarp"),
                # PathRemovePrefix then PathPrefix; the destination's path
                # comes first.
                (("api", "orders"), ("base", "v1", "orders"), "orders", "yarp"),
                # No transform passes the path on; destinations on different
                # hosts name no target.
                (("kept",), ("kept",), None, "yarp"),
            ],
        )
        self.assertEqual(
            routes[0].line, YARP[: YARP.find('"catalog-route"')].count("\n") + 1
        )
        self.assertEqual(
            [u.reason for u in unread],
            [
                "path_rewritten",
                "path_pattern",
                "path_rewritten",
                "destination_not_literal",
            ],
        )
        self.assertEqual(
            yarp_routes("{ not json", "x.json")[1][0].reason, "json_not_parsed"
        )
        self.assertEqual(yarp_routes('{"Logging": {}}', "x.json"), ([], []))

    def test_ocelot(self):
        routes, unread = ocelot_routes(OCELOT, "gw/ocelot.json")
        self.assertEqual(
            [
                (r.prefix, r.replacement, r.target, r.methods, r.min_rest, r.max_rest)
                for r in routes
            ],
            [
                # The last placeholder takes the rest of the path.
                (
                    ("merchant", "api"),
                    ("api",),
                    "merchant.api",
                    ("GET", "POST"),
                    1,
                    None,
                ),
                (("catalog",), ("api", "catalog"), "catalog", (), 1, None),
                # No placeholder in the path: that path only. Hosts differ.
                (("basket",), ("api", "basket"), None, (), 0, 0),
            ],
        )
        self.assertEqual(
            routes[1].line, OCELOT[: OCELOT.find('"/catalog/{id}"')].count("\n") + 1
        )
        # No literal prefix, a placeholder before literal text, placeholders
        # in another order.
        self.assertEqual([u.reason for u in unread], ["path_pattern"] * 3)
        self.assertEqual(ocelot_routes("[", "x.json")[1][0].reason, "json_not_parsed")

    def test_forward_checks_methods_and_the_rest_of_the_path(self):
        routes, _ = ocelot_routes(OCELOT, "gw/ocelot.json")
        self.assertEqual(len(forward(routes, ("merchant", "api", "x"), "GET")), 1)
        self.assertEqual(len(forward(routes, ("merchant", "api", "x"), None)), 1)
        self.assertEqual(forward(routes, ("merchant", "api", "x"), "DELETE"), [])
        self.assertEqual(forward(routes, ("merchant", "api"), "GET"), [])
        self.assertEqual(len(forward(routes, ("basket",))), 1)
        self.assertEqual(forward(routes, ("basket", "x")), [])

    def test_nginx(self):
        routes, unread = nginx_routes(NGINX, "web/default.conf")
        self.assertEqual(
            [(r.prefix, r.replacement, r.target) for r in routes],
            [
                (("api", "cart"), (), None),
                (("api", "keep"), ("api", "keep"), "keep"),
                (("api", "v2"), ("users",), "user"),
            ],
        )
        self.assertEqual(
            [u.reason for u in unread],
            ["location_not_prefix", "location_not_prefix", "proxy_pass_not_literal"],
        )

    def test_forward_takes_the_longest_prefix(self):
        a = GatewayRoute("f", 1, "nginx", ("api",), (), "a")
        b = GatewayRoute("f", 2, "nginx", ("api", "b"), (), "b")
        self.assertEqual(forward([a, b], ("api", "b", "x")), [b])
        self.assertEqual(forward([b, a], ("api", "b", "x")), [b])
        self.assertEqual(forward([a, b], ("api", "c")), [a])
        self.assertEqual(forward([a, b], ("apix",)), [])


def _route(identifier, path, service=None, methods=("GET",)):
    return HttpRoute(
        identifier, "s.java", 1, 0, "h", methods, path, "spring-mvc", service
    )


def _request(identifier, url, method="GET"):
    return HttpRequest(identifier, "c.js", 1, 0, None, method, url, "fetch")


class GatewayResolutionTests(unittest.TestCase):
    def test_resolution(self):
        routes = [
            _route("vets", "/vets", "vets-service"),
            _route("other-vets", "/vets", "other-service"),
            _route("direct", "/api/vet/vets", "api-gateway"),
            _route("cart", "/cart/{id}"),
            _route("item", "/{id}"),
        ]
        gateways = [
            GatewayRoute(
                "g.yml", 3, "spring-cloud-gateway", ("api", "vet"), (), "vets-service"
            ),
            GatewayRoute("n.conf", 7, "nginx", ("api", "cart"), (), None),
            GatewayRoute("a.conf", 1, "nginx", ("twice",), (), "a"),
            GatewayRoute("b.conf", 1, "nginx", ("twice",), (), "b"),
        ]
        requests = [
            _request("through", "/api/vet/vets"),
            _request("unknown-base", U + "/api/cart/7"),
            # A literal host is not the gateway's: no forwarding.
            _request("literal", "http://example.org/api/vet/vets"),
            _request("tie", "/twice/x"),
            _request("not-forwarded", "/cart/1"),
        ]
        found = {r.request_id: r for r in resolve_requests(routes, requests, gateways)}
        self.assertEqual(
            (
                found["through"].route_id,
                found["through"].rule_id,
                found["through"].gateway,
            ),
            ("vets", HTTP_GATEWAY, "g.yml:3"),
        )
        self.assertEqual(found["through"].path, "/vets")
        self.assertEqual(found["through"].target_service, "vets-service")
        self.assertEqual(
            (found["unknown-base"].route_id, found["unknown-base"].rule_id),
            ("item", HTTP_GATEWAY),
        )
        self.assertEqual(
            (found["literal"].route_id, found["literal"].rule_id), ("direct", HTTP_PATH)
        )
        self.assertIsNone(found["literal"].gateway)
        self.assertEqual(
            (found["tie"].status, found["tie"].reason),
            ("ambiguous", "several_gateways"),
        )
        self.assertEqual(
            (found["not-forwarded"].route_id, found["not-forwarded"].rule_id),
            ("cart", HTTP_PATH),
        )


@unittest.skipUnless(READY, "tree-sitter grammars or pyyaml are not installed")
@needs_typescript
class GatewayToolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        files = {
            "gateway/pom.xml": "<project/>",
            "gateway/src/main/resources/application.yml": GATEWAY_YAML,
            "gateway/src/main/java/g/App.java": "package g;\nclass App {}\n",
            "gateway/src/main/resources/static/scripts/vets.js": (
                "angular.module('v').controller('V', function ($http) {\n"
                "    $http.get('api/vet/vets').then(function () {});\n"
                "    $http({method: 'POST', url: '/api/cart/' + id});\n"
                "});\n"
            ),
            "vets/pom.xml": "<project/>",
            "vets/src/main/resources/application.properties": (
                "spring.application.name=vets-service\n"
            ),
            "vets/src/main/java/v/VetResource.java": (
                "package v;\n"
                "import org.springframework.web.bind.annotation.*;\n"
                "@RestController\n"
                "class VetResource {\n"
                '    @GetMapping("/vets")\n'
                "    String vets() { return null; }\n"
                "}\n"
            ),
            "cart/server.py": (
                "from flask import Flask\n"
                "app = Flask(__name__)\n"
                "@app.post('/<id>')\n"
                "def add(id):\n"
                "    pass\n"
            ),
            "web/default.conf.template": NGINX,
            "web/node_modules/x/nginx.conf": "location /api/vet/ { proxy_pass http://x/; }",
        }
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.tool = ServiceContractTool(root)
        self.output = asyncio.run(self.tool.analyze("r"))

    def tearDown(self):
        self.directory.cleanup()

    def test_browser_requests_follow_gateways(self):
        edges = sorted(
            (
                r.properties["request"],
                r.properties["gateway"],
                r.target_match["qualified_name"].split("::")[1],
            )
            for r in self.output.relationships
            if r.type == "REQUESTS"
        )
        self.assertEqual(
            edges,
            [
                (
                    "GET /api/vet/vets",
                    "gateway/src/main/resources/application.yml:8",
                    "vets/src/main/java/v/VetResource.java",
                ),
                ("POST /api/cart/{?}", "web/default.conf.template:7", "cart/server.py"),
            ],
        )
        rules = {
            r.properties["rule_id"]
            for r in self.output.relationships
            if r.type == "REQUESTS"
        }
        self.assertEqual(rules, {HTTP_GATEWAY})
        self.assertEqual(get_rule(HTTP_GATEWAY).evidence_type, "pattern_matched")
        metrics = self.output.metadata.custom_metrics
        # Four Spring routes, three nginx ones; node_modules is not read.
        self.assertEqual(metrics["gateway_routes"], 7)
        self.assertEqual(metrics["unread"]["gateway:path_rewritten"], 3)


@unittest.skipUnless(
    YAML
    and importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_c_sharp"),
    "tree-sitter-c-sharp is not installed",
)
class YarpGatewayTests(unittest.TestCase):
    def test_requests_follow_the_base_configuration(self):
        overlay = YARP.replace("catalog-service", "other-service")
        files = {
            "src/Catalog/Catalog.csproj": "<Project />",
            "src/Catalog/Api.cs": (
                "public static class Api {\n"
                "  public static void Map(IEndpointRouteBuilder app) {\n"
                '    app.MapGet("/products/{id}", (int id) => id);\n'
                "  }\n"
                "}\n"
            ),
            "src/Gateway/Gateway.csproj": "<Project />",
            "src/Gateway/Program.cs": "var app = WebApplication.Create();\n",
            "src/Gateway/appsettings.json": YARP,
            # An environment overlay is not read.
            "src/Gateway/appsettings.Development.json": overlay,
            # Nor is the configuration of a project with no analyzed sources.
            "tools/appsettings.json": overlay,
            "src/Web/Web.csproj": "<Project />",
            "src/Web/Client.cs": (
                "class Client {\n"
                "  Task F() => new HttpClient().GetAsync("
                '"/catalog-service/products/1");\n'
                "}\n"
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            output = asyncio.run(ServiceContractTool(root).analyze("r"))
        edges = [
            (
                r.properties["request"],
                r.properties["rule_id"],
                r.properties["gateway"],
                r.target_match["qualified_name"].split("::")[1],
            )
            for r in output.relationships
            if r.type == "REQUESTS"
        ]
        self.assertEqual(
            edges,
            [
                (
                    "GET /catalog-service/products/1",
                    HTTP_GATEWAY,
                    "src/Gateway/appsettings.json:6",
                    "src/Catalog/Api.cs",
                )
            ],
        )
        self.assertEqual(output.metadata.custom_metrics["gateway_routes"], 3)

    def test_ocelot_requests_follow_the_base_configuration(self):
        overlay = OCELOT.replace("/catalog/{id}", "/other/{id}")
        files = {
            "src/Catalog/Catalog.csproj": "<Project />",
            "src/Catalog/Api.cs": (
                "public static class Api {\n"
                "  public static void Map(IEndpointRouteBuilder app) {\n"
                '    app.MapGet("/api/catalog/{id}", (int id) => id);\n'
                "  }\n"
                "}\n"
            ),
            "src/Gateway/Gateway.csproj": "<Project />",
            "src/Gateway/Program.cs": "var app = WebApplication.Create();\n",
            "src/Gateway/ocelot.json": OCELOT,
            # An environment overlay beside its base file is not read.
            "src/Gateway/ocelot.Docker.json": overlay,
            "src/Web/Web.csproj": "<Project />",
            "src/Web/Client.cs": (
                "class Client {\n"
                '  Task F() => new HttpClient().GetAsync("/catalog/1");\n'
                '  Task G() => new HttpClient().GetAsync("/other/1");\n'
                "}\n"
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            output = asyncio.run(ServiceContractTool(root).analyze("r"))
        edges = [
            (
                r.properties["request"],
                r.properties["rule_id"],
                r.properties["gateway"],
                r.target_match["qualified_name"].split("::")[1],
            )
            for r in output.relationships
            if r.type == "REQUESTS"
        ]
        line = OCELOT[: OCELOT.find('"/catalog/{id}"')].count("\n") + 1
        self.assertEqual(
            edges,
            [
                (
                    "GET /catalog/1",
                    HTTP_GATEWAY,
                    f"src/Gateway/ocelot.json:{line}",
                    "src/Catalog/Api.cs",
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
