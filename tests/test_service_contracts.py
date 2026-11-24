"""HTTP contracts between services: Spring routes, Feign and RestTemplate."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool, TreeSitterTool
from deepthought.analysis_tools.evidence import get_rule
from deepthought.service_contracts.http import (
    PLACEHOLDER,
    best_routes,
    join_paths,
    match_rank,
    request_target,
    route_template,
)
from deepthought.service_contracts.resolve import HTTP_PATH, HTTP_SERVICE_PATH
from deepthought.storage.graph.write_coordinator import (
    WriteCoordinatorConfig,
    _combine,
)

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_java"
)
U = PLACEHOLDER


class HttpTemplateTests(unittest.TestCase):
    def test_route_templates(self):
        self.assertEqual(join_paths("/a/", "", "b//c/"), "/a/b/c")
        self.assertEqual(join_paths(), "/")
        kinds = [s.kind for s in route_template("/a/{id}/{x:[0-9]+}/f{n}.json/*/**")]
        self.assertEqual(
            kinds, ["literal", "variable", "variable", "pattern", "variable", "rest"]
        )

    def test_request_targets(self):
        target = request_target("http://User@Accounts-Service:8080/a/{id}?q=1#f")
        self.assertEqual(target.host, "accounts-service")
        self.assertEqual(target.segments, ("a", U))
        self.assertEqual(target.display, "/a/{?}")
        self.assertEqual(request_target(U + "/a/" + U + U).segments, ("a", U))
        self.assertIsNone(request_target("http://" + U + "/a").host)
        self.assertEqual(request_target("http://" + U + "/a").segments, ("a",))
        self.assertEqual(request_target(U + "a").reason, "base_not_separated")
        self.assertEqual(request_target("a/b").reason, "no_literal_path")
        self.assertEqual(request_target("http:///a").reason, "no_host")
        self.assertEqual(request_target(U).reason, "no_literal_path")
        # A literal host with no path is the root; unknown text after an
        # unknown host, or a path with no literal text, fixes no path.
        self.assertEqual(request_target("http://Cart:8080").segments, ())
        self.assertEqual(request_target("http://Cart:8080").host, "cart")
        self.assertEqual(request_target("http://cart:" + U + "/a").host, "cart")
        self.assertEqual(request_target("http://cart:" + U).reason, "no_literal_path")
        self.assertEqual(request_target("http://" + U).reason, "no_literal_path")
        self.assertEqual(request_target(U + "/" + U).reason, "no_literal_path")

    def test_segment_ranks(self):
        route = route_template("/a/{id}")
        self.assertEqual(match_rank(("a", "7"), route), (3, 1))
        self.assertEqual(match_rank(("a", U), route), (3, 2))
        self.assertIsNone(match_rank(("a",), route))
        self.assertIsNone(match_rank(("b", U), route))
        self.assertEqual(match_rank(("x" + U,), route_template("/xyz")), (1,))
        self.assertIsNone(match_rank(("q" + U,), route_template("/xyz")))
        self.assertEqual(
            match_rank(("a", "b", "c"), route_template("/a/**")), (3, 0, 0)
        )
        self.assertIsNone(match_rank(("a", "b"), route_template("/**/b")))
        self.assertEqual(match_rank(("f7.json",), route_template("/f{n}.json")), (1,))

    def test_most_specific_route_wins(self):
        routes = [
            ("var", ("GET",), route_template("/a/{id}")),
            ("current", ("GET",), route_template("/a/current")),
            ("any", ("*",), route_template("/a/{id}")),
            ("post", ("POST",), route_template("/a/{id}")),
        ]
        self.assertEqual(best_routes("GET", ("a", "current"), routes), ("current",))
        # Unknown text fits a path variable better than a literal.
        self.assertEqual(best_routes("GET", ("a", U), routes), ("var",))
        # An exact method beats a route accepting any method.
        self.assertEqual(best_routes("PUT", ("a", U), routes), ("any",))
        self.assertEqual(best_routes("POST", ("a", "1"), routes), ("post",))
        # An unknown method fits every method: a tie.
        self.assertEqual(best_routes(None, ("a", U), routes), ("any", "post", "var"))
        self.assertEqual(best_routes("GET", ("b",), routes), ())


ACCOUNT = """package a;

import java.util.Map;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.client.RestTemplate;

@RestController
@RequestMapping({"/accounts", "/v2/accounts"})
public class AccountController {
  private static final String CURRENT = "/" + "current";
  private static final String TWICE = "/a";
  private static final String TWICE = "/b";
  private final RestTemplate rest;
  private final Map<String, String> cache;
  private String statsHost = "http://statistics-service";
  @Value("${x}") private String injected = "/never";

  @GetMapping(CURRENT)
  public String current() { return ""; }

  @RequestMapping(value = "/{name}", method = {RequestMethod.PUT, RequestMethod.POST})
  public void save(String name) {
    String url = statsHost + "/stats/" + name;
    rest.put(url, name);
    cache.put("k", "/stats/x");
    String later;
    later = lookup();
    rest.getForObject(later, String.class);
    rest.exchange(base("statistics-service") + "/stats/" + name, HttpMethod.GET, null, String.class);
    rest.postForObject(String.format("http://statistics-service/stats/%s/notes", name), null, String.class);
    rest.delete(injected);
  }

  @RequestMapping(TWICE)
  public void twice() { }

  @DeleteMapping
  public void root() { }

  @RequestMapping(value = "/m", method = METHOD)
  public void unknownMethod() { }

  private String base(String service) { return "http://" + service; }
}
"""
FEIGN = """package a;

import org.springframework.cloud.openfeign.FeignClient;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestMethod;

@FeignClient(name = "statistics-service", path = "/stats")
public interface StatisticsClient {
  @RequestMapping(method = RequestMethod.GET, value = "/{accountName}")
  String get(String accountName);
  @GetMapping("/missing")
  String missing();
}

@FeignClient(value = "rates", url = "${rates.url}")
interface RatesClient {
  @GetMapping("/latest")
  String latest();
}
"""
STATISTICS = """package s;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class StatisticsController {
  @GetMapping("/{accountName}")
  public String get(String accountName) { return ""; }
  @PutMapping("/{accountName}")
  public void put(String accountName) { }
  @PostMapping("/{accountName}/notes")
  public void notes(String accountName) { }
  @GetMapping("/latest")
  public String latest() { return ""; }
}

class NotAController {
  @org.springframework.web.bind.annotation.GetMapping("/hidden")
  public void hidden() { }
}

@RestController
class Shadowed {
  @GetMapping("/shadow")
  public void shadow() { }
}
"""
SHADOW = """package s;

import com.example.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
class Other {
  @GetMapping("/not-spring")
  public void notSpring() { }
}
"""
TEST = """package s;

import org.springframework.web.client.RestTemplate;

class StatisticsTest {
  RestTemplate rest;
  void test() { rest.getForObject("http://statistics-service/stats/x", String.class); }
}
"""


def _write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _workspace(root):
    _write(
        root,
        {
            "account/pom.xml": "<project/>",
            "account/src/main/resources/bootstrap.yml": (
                "spring:\n  application:\n    name: account-service\n"
            ),
            "account/src/main/java/a/AccountController.java": ACCOUNT,
            "account/src/main/java/a/StatisticsClient.java": FEIGN,
            "statistics/build.gradle": "",
            "statistics/src/main/resources/application.properties": (
                "spring.application.name = statistics-service\n"
            ),
            "statistics/src/main/java/s/StatisticsController.java": STATISTICS,
            "statistics/src/main/java/s/Other.java": SHADOW,
            "statistics/src/test/java/s/StatisticsTest.java": TEST,
            "config/pom.xml": "<project/>",
            "config/src/main/resources/application.yml": (
                "spring:\n  cloud:\n    config:\n      server:\n        native:\n"
                "          search-locations: classpath:/shared\n"
            ),
            "config/src/main/resources/shared/statistics-service.yml": (
                "server:\n  servlet:\n    context-path: /stats # served here\n"
            ),
        },
    )


@unittest.skipUnless(READY, "tree-sitter-java is not installed")
class SpringContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        _workspace(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def analyze(self):
        tool = ServiceContractTool(self.root)
        output = asyncio.run(tool.analyze("r"))
        return tool, output

    def test_routes(self):
        tool, _ = self.analyze()
        routes = {(r.service, r.methods, r.path) for r in tool.last_facts[1]}
        self.assertEqual(
            routes,
            {
                ("account-service", ("GET",), "/accounts/current"),
                ("account-service", ("GET",), "/v2/accounts/current"),
                ("account-service", ("POST", "PUT"), "/accounts/{name}"),
                ("account-service", ("POST", "PUT"), "/v2/accounts/{name}"),
                ("account-service", ("DELETE",), "/accounts"),
                ("account-service", ("DELETE",), "/v2/accounts"),
                # The context path comes from the config server's shared file.
                ("statistics-service", ("GET",), "/stats/{accountName}"),
                ("statistics-service", ("PUT",), "/stats/{accountName}"),
                ("statistics-service", ("POST",), "/stats/{accountName}/notes"),
                ("statistics-service", ("GET",), "/stats/latest"),
                ("statistics-service", ("GET",), "/stats/shadow"),
            },
        )
        unread = {(u.kind, u.reason) for u in tool.last_facts[3]}
        # A constant declared twice is not folded; a method constant unknown.
        self.assertEqual(
            unread, {("route", "path_not_literal"), ("route", "method_not_literal")}
        )

    def test_requests_and_resolution(self):
        tool, output = self.analyze()
        _, routes, requests, _, resolutions = tool.last_facts
        route_by_id = {r.id: r for r in routes}
        found = {}
        for request, resolution in zip(requests, resolutions):
            target = route_by_id.get(resolution.route_id)
            found[(request.client, request.method, request.url)] = (
                resolution.status,
                resolution.rule_id or resolution.reason,
                target and (target.methods, target.path),
            )
        self.assertEqual(
            found,
            {
                ("feign", "GET", "/stats/{accountName}"): (
                    "linked",
                    HTTP_SERVICE_PATH,
                    (("GET",), "/stats/{accountName}"),
                ),
                # Spring would route it to the path variable too.
                ("feign", "GET", "/stats/missing"): (
                    "linked",
                    HTTP_SERVICE_PATH,
                    (("GET",), "/stats/{accountName}"),
                ),
                # An explicit property URL bypasses discovery: the base is
                # unknown and /latest alone fits no route.
                ("feign", "GET", U + "/latest"): (
                    "unresolved",
                    "no_route",
                    None,
                ),
                ("rest-template", "PUT", "http://statistics-service/stats/" + U): (
                    "linked",
                    HTTP_SERVICE_PATH,
                    (("PUT",), "/stats/{accountName}"),
                ),
                ("rest-template", "GET", U): (
                    "unresolved",
                    "no_literal_path",
                    None,
                ),
                ("rest-template", "GET", "http://statistics-service/stats/" + U): (
                    "linked",
                    HTTP_SERVICE_PATH,
                    (("GET",), "/stats/{accountName}"),
                ),
                (
                    "rest-template",
                    "POST",
                    "http://statistics-service/stats/" + U + "/notes",
                ): (
                    "linked",
                    HTTP_SERVICE_PATH,
                    (("POST",), "/stats/{accountName}/notes"),
                ),
                ("rest-template", "DELETE", U): (
                    "unresolved",
                    "no_literal_path",
                    None,
                ),
            },
        )
        metrics = output.metadata.custom_metrics
        self.assertEqual(metrics["test_source_files_skipped"], 1)
        self.assertEqual(metrics["http_requests"], 8)

    def test_unknown_service_falls_back_to_path(self):
        _write(
            self.root,
            {
                "account/src/main/java/a/Direct.java": (
                    "package a;\nimport org.springframework.web.client.RestTemplate;\n"
                    "class Direct { RestTemplate t;\n"
                    '  void f() { t.getForObject("http://localhost:7000/stats/latest", String.class); } }\n'
                ),
            },
        )
        tool, _ = self.analyze()
        _, routes, requests, _, resolutions = tool.last_facts
        resolution = next(
            r for q, r in zip(requests, resolutions) if "localhost" in q.url
        )
        self.assertEqual(resolution.status, "linked")
        self.assertEqual(resolution.rule_id, HTTP_PATH)
        self.assertIsNone(resolution.target_service)

    def test_output_edges_evidence_and_identity(self):
        tool, output = self.analyze()
        again = asyncio.run(ServiceContractTool(self.root).analyze("r"))
        self.assertEqual(
            [n.properties for n in output.nodes], [n.properties for n in again.nodes]
        )
        self.assertEqual(
            [r.properties for r in output.relationships],
            [r.properties for r in again.relationships],
        )
        kinds = {}
        for relationship in output.relationships:
            kinds.setdefault(relationship.type, set()).add(
                relationship.properties["rule_id"]
            )
            rule = get_rule(relationship.properties["rule_id"])
            self.assertEqual(rule.evidence_type, "pattern_matched")
        self.assertEqual(
            kinds,
            {
                "HANDLES": {"service-contracts.spring-route-mapping"},
                "REQUESTS": {HTTP_SERVICE_PATH},
            },
        )
        requests = [r for r in output.relationships if r.type == "REQUESTS"]
        self.assertEqual(len(requests), 5)
        feign = next(r for r in requests if r.properties["client"] == "feign")
        self.assertIn("StatisticsClient.get", feign.source_match["qualified_name"])
        self.assertEqual(feign.properties["target_service"], "statistics-service")
        files = {
            n.properties["path"]: n.properties
            for n in output.nodes
            if "File" in n.labels
        }
        client = files["account/src/main/java/a/StatisticsClient.java"]
        self.assertEqual(
            client["unresolved_http_requests"],
            ["GET {?}/latest"],
        )
        # Handlers and callers carry the tree-sitter declaration identities,
        # so one revision of both tools merges them.
        trees = asyncio.run(TreeSitterTool(self.root).analyze("r", languages={"java"}))
        output.metadata.custom_metrics["snapshot_id"] = trees.metadata.custom_metrics[
            "snapshot_id"
        ]
        merged, _ = _combine([trees, output], "r", "rev", WriteCoordinatorConfig())
        symbols = {
            n.properties["qualified_name"] for n in trees.nodes if "Symbol" in n.labels
        }
        contract_symbols = {
            n.properties["qualified_name"] for n in output.nodes if "Symbol" in n.labels
        }
        self.assertTrue(contract_symbols <= symbols)
        self.assertEqual(
            len([n for n in merged.nodes if "Symbol" in n.labels]), len(symbols)
        )

    def test_web_client_chains(self):
        _write(
            self.root,
            {
                "account/src/main/java/a/Gateway.java": (
                    "package a;\n"
                    "import org.springframework.http.HttpMethod;\n"
                    "import org.springframework.web.reactive.function.client.WebClient;\n"
                    "class Gateway {\n"
                    "  private final WebClient.Builder builder;\n"
                    '  private String host = "http://statistics-service/";\n'
                    "  void f(String id) {\n"
                    '    builder.build().get().uri("http://statistics-service/stats/{id}", id);\n'
                    '    WebClient.create().method(HttpMethod.PUT).uri(host + "stats/" + id);\n'
                    '    builder.build().post().uri(b -> b.path("/x").build());\n'
                    '    other.get().uri("http://statistics-service/stats/x");\n'
                    "  }\n"
                    "  void setHost(String host) { this.host = host; }\n"
                    "}\n"
                ),
            },
        )
        tool, _ = self.analyze()
        _, routes, requests, unread, resolutions = tool.last_facts
        route_by_id = {r.id: r for r in routes}
        found = sorted(
            (
                q.method,
                q.url,
                r.status,
                route_by_id[r.route_id].path if r.route_id else r.reason,
            )
            for q, r in zip(requests, resolutions)
            if q.client == "web-client"
        )
        self.assertEqual(
            found,
            [
                (
                    "GET",
                    "http://statistics-service/stats/{id}",
                    "linked",
                    "/stats/{accountName}",
                ),
                # The field has a setter, so its value is not fixed.
                ("PUT", U + "stats/" + U, "unresolved", "base_not_separated"),
            ],
        )
        self.assertIn(
            ("request", "url_not_string"), {(u.kind, u.reason) for u in unread}
        )

    def test_http_interfaces_and_rest_client_chains(self):
        _write(
            self.root,
            {
                "account/src/main/java/a/StatsApi.java": (
                    "package a;\n"
                    "import org.springframework.web.service.annotation.*;\n"
                    '@HttpExchange("/stats")\n'
                    "interface StatsApi {\n"
                    '  @GetExchange("/{name}") String get(String name);\n'
                    '  @PostExchange(url = "/{name}/notes") void note(String name);\n'
                    '  @HttpExchange(value = "/latest", method = "GET") String latest();\n'
                    '  @HttpExchange("/{name}") void any(String name);\n'
                    '  default String local() { return ""; }\n'
                    "}\n"
                ),
                "account/src/main/java/a/Direct.java": (
                    "package a;\n"
                    "import org.springframework.web.service.annotation.GetExchange;\n"
                    "interface Direct {\n"
                    '  @GetExchange("http://statistics-service/stats/latest")\n'
                    "  String latest();\n"
                    '  @GetExchange("${stats.url}/stats/latest")\n'
                    "  String configured();\n"
                    "}\n"
                ),
                "account/src/main/java/a/NotSpring.java": (
                    "package a;\n"
                    "import com.example.GetExchange;\n"
                    "interface NotSpring {\n"
                    '  @GetExchange("/stats/latest") String latest();\n'
                    "}\n"
                ),
                "account/src/main/java/a/Rest.java": (
                    "package a;\n"
                    "import org.springframework.http.HttpMethod;\n"
                    "import org.springframework.web.client.RestClient;\n"
                    "class Rest {\n"
                    "  private final RestClient client;\n"
                    "  void f(String id) {\n"
                    '    client.put().uri("http://statistics-service/stats/{id}", id);\n'
                    "    RestClient.create().method(HttpMethod.POST)\n"
                    '        .uri("/stats/" + id + "/notes");\n'
                    '    other.get().uri("/stats/latest");\n'
                    "  }\n"
                    "}\n"
                ),
            },
        )
        tool, _ = self.analyze()
        _, routes, requests, unread, resolutions = tool.last_facts
        route_by_id = {r.id: r for r in routes}
        found = sorted(
            (
                q.client,
                q.file.rpartition("/")[2],
                q.line,
                q.method or "",
                q.url,
                r.rule_id or r.reason,
                route_by_id[r.route_id].path if r.route_id else "",
            )
            for q, r in zip(requests, resolutions)
            if q.client in ("http-interface", "rest-client")
        )
        self.assertEqual(
            found,
            [
                (
                    "http-interface",
                    "Direct.java",
                    4,
                    "GET",
                    "http://statistics-service/stats/latest",
                    "service-contracts.http-service-path",
                    "/stats/latest",
                ),
                (
                    "http-interface",
                    "Direct.java",
                    6,
                    "GET",
                    U + "/stats/latest",
                    "service-contracts.http-path",
                    "/stats/latest",
                ),
                (
                    "http-interface",
                    "StatsApi.java",
                    5,
                    "GET",
                    U + "/stats/{name}",
                    "service-contracts.http-path",
                    "/stats/{accountName}",
                ),
                (
                    "http-interface",
                    "StatsApi.java",
                    6,
                    "POST",
                    U + "/stats/{name}/notes",
                    "service-contracts.http-path",
                    "/stats/{accountName}/notes",
                ),
                (
                    "http-interface",
                    "StatsApi.java",
                    7,
                    "GET",
                    U + "/stats/latest",
                    "service-contracts.http-path",
                    "/stats/latest",
                ),
                # No method: the GET and the PUT route both fit.
                (
                    "http-interface",
                    "StatsApi.java",
                    8,
                    "",
                    U + "/stats/{name}",
                    "several_routes",
                    "",
                ),
                (
                    "rest-client",
                    "Rest.java",
                    7,
                    "PUT",
                    "http://statistics-service/stats/{id}",
                    "service-contracts.http-service-path",
                    "/stats/{accountName}",
                ),
                (
                    "rest-client",
                    "Rest.java",
                    8,
                    "POST",
                    "/stats/" + U + "/notes",
                    "service-contracts.http-path",
                    "/stats/{accountName}/notes",
                ),
            ],
        )

    def test_a_later_local_is_not_folded_into_an_earlier_use(self):
        _write(
            self.root,
            {
                "account/src/main/java/a/Later.java": (
                    "package a;\nimport org.springframework.web.client.RestTemplate;\n"
                    "class Later { RestTemplate t;\n"
                    "  void f() {\n"
                    "    t.getForObject(u, String.class);\n"
                    '    { String u = "http://statistics-service/stats/latest"; }\n'
                    "  } }\n"
                ),
            },
        )
        tool, _ = self.analyze()
        urls = [q.url for q in tool.last_facts[2] if q.file.endswith("Later.java")]
        self.assertEqual(urls, [U])

    def test_urls_chosen_in_branches(self):
        body = """
  void exhaustive(boolean c) {
    String u = "";
    if (c) { u = "http://statistics-service/stats/a"; }
    else if (!c) u = "http://statistics-service/stats/b/notes";
    else { u = "http://statistics-service/stats/latest"; }
    t.getForObject(u, String.class);
  }
  void partial(boolean c) {
    String u = "http://statistics-service/stats/latest";
    if (c) { u = "http://statistics-service/stats/a"; }
    t.getForObject(u, String.class);
  }
  void nullPartial(boolean c) {
    String u = null;
    if (c) { u = "http://statistics-service/stats/a"; }
    t.getForObject(u, String.class);
  }
  void sequential() {
    String u = "http://statistics-service/stats/a";
    if (true) { u = "/x"; u = "/y"; }
    t.getForObject(u, String.class);
  }
  void straight() {
    String u = "http://statistics-service/stats/a";
    u = "http://statistics-service/stats/latest";
    t.getForObject(u, String.class);
  }
  void after(boolean c) {
    String u = null;
    t.getForObject(u, String.class);
    if (c) { u = "/a"; } else { u = "/b"; }
  }
  void compound() {
    String u = "http://statistics-service/stats";
    u += "/latest";
    t.getForObject(u, String.class);
  }
"""
        _write(
            self.root,
            {
                "account/src/main/java/a/Branches.java": (
                    "package a;\nimport org.springframework.web.client.RestTemplate;\n"
                    "class Branches { RestTemplate t;\n" + body + "}\n"
                ),
            },
        )
        tool, output = self.analyze()
        definitions = {d.id: d.symbol for d in tool.last_facts[0].definitions}
        found = {}
        for request in tool.last_facts[2]:
            if request.file.endswith("Branches.java"):
                found.setdefault(definitions[request.caller_id], []).append(
                    (request.url.rpartition("/stats")[2], request.alternatives)
                )
        self.assertEqual(
            found,
            {
                "Branches.exhaustive": [("/a", 3), ("/b/notes", 3), ("/latest", 3)],
                "Branches.partial": [("/latest", 2), ("/a", 2)],
                "Branches.nullPartial": [(U, 1)],
                "Branches.sequential": [(U, 1)],
                "Branches.straight": [("/latest", 1)],
                "Branches.after": [(U, 1)],
                "Branches.compound": [(U, 1)],
            },
        )
        alternatives = [
            r.properties.get("url_alternatives")
            for r in output.relationships
            if r.type == "REQUESTS"
            and "Branches.exhaustive" in r.source_match["qualified_name"]
        ]
        # GET /stats/b/notes fits no route: the notes route accepts POST only.
        self.assertEqual(alternatives, [3, 3])

    def test_unsupported_languages_are_rejected(self):
        with self.assertRaises(ValueError):
            asyncio.run(ServiceContractTool(self.root).analyze("r", languages={"rust"}))


if __name__ == "__main__":
    unittest.main()
