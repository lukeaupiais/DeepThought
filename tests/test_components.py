"""Components that build manifests declare, and file membership by root."""

import asyncio
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from deepthought.analysis_tools import ComponentsTool, TreeSitterTool
from deepthought.analysis_tools.evidence import get_rule
from deepthought.components import (
    Component,
    enclosing_components,
    extract_components,
    scan_components,
)
from deepthought.storage.graph.write_coordinator import (
    WriteCoordinatorConfig,
    _combine,
)

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_python"
)
YAML = importlib.util.find_spec("yaml")

WORKSPACE = {
    "pyproject.toml": '[build-system]\nrequires = ["hatchling"]\n\n'
    '[project]\nname = "app"\nversion = "1"\n',
    "app/main.py": "from app import util\n",
    "app/util.py": "def helper():\n    return 1\n",
    "web/package.json": '{"name": "@acme/web", "private": true}\n',
    "web/src/index.ts": "export const x = 1;\n",
    "web/nested/package.json": '{"private": true}\n',
    "web/nested/a.ts": "export const y = 2;\n",
    "svc/go.mod": "// service\nmodule example.com/svc\n\ngo 1.22\n",
    "svc/main.go": "package main\n",
    "crates/core/Cargo.toml": '[package]\nname = "core-lib"\nversion = "0.1.0"\n',
    "crates/Cargo.toml": '[workspace]\nmembers = ["core"]\n',
    "java/pom.xml": '<?xml version="1.0"?>\n'
    '<project xmlns="http://maven.apache.org/POM/4.0.0">'
    "<parent><artifactId>parent</artifactId></parent>"
    "<artifactId>billing</artifactId></project>\n",
    "tools/pyproject.toml": "[tool.ruff]\nline-length = 100\n",
    "tools/check.py": "print(1)\n",
}


def write(directory, files):
    for path, content in files.items():
        target = Path(directory) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


class ExtractionTests(unittest.TestCase):
    def test_each_manifest_kind(self):
        cases = {
            "pyproject.toml": [Component("python_project", "pyproject.toml", "app", 4)],
            "web/package.json": [
                Component("node_package", "web/package.json", "@acme/web")
            ],
            "web/nested/package.json": [
                Component("node_package", "web/nested/package.json", None)
            ],
            "svc/go.mod": [Component("go_module", "svc/go.mod", "example.com/svc", 2)],
            "crates/core/Cargo.toml": [
                Component("cargo_package", "crates/core/Cargo.toml", "core-lib", 1)
            ],
            "crates/Cargo.toml": [],
            "java/pom.xml": [Component("maven_module", "java/pom.xml", "billing")],
            "tools/pyproject.toml": [],
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(
                    extract_components(path, WORKSPACE[path].encode()), expected
                )

    def test_poetry_name_and_invalid_manifests(self):
        poetry = b'[tool.poetry]\nname = "legacy"\n'
        self.assertEqual(
            extract_components("pyproject.toml", poetry),
            [Component("python_project", "pyproject.toml", "legacy", 1)],
        )
        for path, content in (
            ("package.json", b"{not json"),
            ("package.json", b"[1, 2]"),
            ("pyproject.toml", b"[project\n"),
            ("go.mod", b"go 1.22\n"),
            ("pom.xml", b"<project><parent/></project>"),
            ("Cargo.toml", b"[package]\nversion = 1\n"),
        ):
            with self.subTest(path=path, content=content):
                self.assertIsNone(extract_components(path, content))

    def test_roots(self):
        self.assertEqual(Component("go_module", "go.mod", "m").root, "")
        self.assertEqual(Component("go_module", "a/b/go.mod", "m").root, "a/b")


class MembershipTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        write(directory.name, WORKSPACE)
        self.root = directory.name
        self.scan = scan_components(self.root)

    def names(self, path):
        return [
            (c.kind, c.root) for c in enclosing_components(path, self.scan.components)
        ]

    def test_nearest_root_of_each_kind(self):
        self.assertEqual(self.names("app/main.py"), [("python_project", "")])
        # The nested package.json is its own package root, without a name.
        self.assertEqual(
            self.names("web/nested/a.ts"),
            [("node_package", "web/nested"), ("python_project", "")],
        )
        self.assertEqual(
            self.names("web/src/index.ts"),
            [("node_package", "web"), ("python_project", "")],
        )
        self.assertEqual(
            self.names("svc/main.go"),
            [("go_module", "svc"), ("python_project", "")],
        )
        # A prefix of a directory name is not an enclosing directory.
        self.assertEqual(self.names("svcx/main.go"), [("python_project", "")])

    def test_tool_configuration_and_workspaces_declare_nothing(self):
        roots = {(c.kind, c.root) for c in self.scan.components}
        self.assertNotIn(("python_project", "tools"), roots)
        self.assertNotIn(("cargo_package", "crates"), roots)
        self.assertEqual(len(self.scan.components), 6)
        self.assertEqual(self.scan.diagnostics, ())

    def test_patterns_and_skipped_directories(self):
        write(self.root, {"node_modules/dep/package.json": '{"name":"dep"}'})
        scan = scan_components(self.root, exclude=("web/*",))
        manifests = {c.manifest for c in scan.components}
        self.assertNotIn("node_modules/dep/package.json", manifests)
        self.assertFalse(any(m.startswith("web/") for m in manifests))

    def test_invalid_manifest_is_a_diagnostic(self):
        write(self.root, {"broken/package.json": "{"})
        scan = scan_components(self.root)
        self.assertEqual(
            scan.diagnostics,
            (("broken/package.json", "Manifest is not valid or declares no name"),),
        )


class ToolTests(unittest.TestCase):
    def analyze(self, files, tool=ComponentsTool, **options):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            return asyncio.run(tool(directory).analyze("repo", **options))

    def test_output_records_evidence_and_identity(self):
        output = self.analyze(WORKSPACE)
        metrics = output.metadata.custom_metrics
        self.assertEqual(metrics["analysis_status"], "succeeded")
        self.assertEqual(metrics["capabilities"], ["components"])
        self.assertEqual(metrics["model_tokens"], 0)
        self.assertEqual(
            metrics["component_counts"],
            {
                "cargo_package": 1,
                "go_module": 1,
                "maven_module": 1,
                "node_package": 2,
                "python_project": 1,
            },
        )
        components = {
            n.properties["manifest"]: n.properties
            for n in output.nodes
            if "Component" in n.labels
        }
        self.assertEqual(
            components["web/package.json"]["qualified_name"],
            "repo::web/package.json::node_package::component",
        )
        self.assertNotIn("name", components["web/nested/package.json"])
        self.assertEqual(
            components["web/nested/package.json"]["display_name"], "web/nested"
        )
        for relationship in output.relationships:
            rule = get_rule(relationship.properties["rule_id"])
            self.assertEqual(rule.evidence_type, "resolved")
            self.assertNotIn("confidence", relationship.properties)
        declares = [r for r in output.relationships if r.type == "DECLARES"]
        self.assertEqual(len(declares), 6)
        # The same inputs give the same snapshot; a changed manifest does not.
        again = self.analyze(WORKSPACE)
        self.assertEqual(
            metrics["snapshot_id"], again.metadata.custom_metrics["snapshot_id"]
        )
        changed = self.analyze({**WORKSPACE, "svc/go.mod": "module example.com/x\n"})
        self.assertNotEqual(
            metrics["snapshot_id"], changed.metadata.custom_metrics["snapshot_id"]
        )

    def test_invalid_manifest_makes_analysis_partial(self):
        output = self.analyze({**WORKSPACE, "bad/package.json": "{"})
        self.assertEqual(output.metadata.custom_metrics["analysis_status"], "partial")
        self.assertEqual(
            output.metadata.warnings,
            ["bad/package.json: Manifest is not valid or declares no name"],
        )

    @unittest.skipUnless(READY, "Provision the python extra")
    def test_combined_revision_links_every_tool_file(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, WORKSPACE)
            outputs = [
                asyncio.run(
                    TreeSitterTool(directory).analyze("repo", languages={"python"})
                ),
                asyncio.run(ComponentsTool(directory).analyze("repo")),
            ]
        combined, _ = _combine(outputs, "repo", "rev", WriteCoordinatorConfig())
        part_of = sorted(
            (
                r.source_match["qualified_name"].split("::")[1],
                r.target_match["qualified_name"].split("::")[1],
            )
            for r in combined.relationships
            if r.type == "PART_OF"
        )
        self.assertIn(("app/main.py", "pyproject.toml"), part_of)
        self.assertIn(("tools/check.py", "pyproject.toml"), part_of)
        self.assertIn(("svc/go.mod", "svc/go.mod"), part_of)
        # Each edge once, though the components tool linked its manifests too.
        self.assertEqual(len(part_of), len(set(part_of)))
        membership = combined.metadata.custom_metrics["component_membership"]
        self.assertEqual(membership["files_outside"], 0)
        # Tree-sitter alone is unchanged: no membership metrics or edges.
        alone, _ = _combine(outputs[:1], "repo", "rev", WriteCoordinatorConfig())
        self.assertNotIn("component_membership", alone.metadata.custom_metrics)


DEPENDENCIES = {
    "Cargo.toml": '[workspace]\nmembers = ["a", "b", "c"]\n'
    '[workspace.dependencies]\nb = { path = "b" }\n'
    '[patch.crates-io]\nc = { path = "c" }\n',
    "a/Cargo.toml": '[package]\nname = "a"\n[dependencies]\n'
    'b = { workspace = true }\nc = "1"\nserde = "1"\n'
    '[dev-dependencies]\nown-b = { package = "b", path = "../b" }\n',
    "b/Cargo.toml": '[package]\nname = "b"\n',
    "c/Cargo.toml": '[package]\nname = "c"\n',
    "go/app/go.mod": "module example.com/app\n\nrequire (\n"
    "\texample.com/lib v1.0.0\n\texample.com/other v1.0.0\n)\n\n"
    "replace example.com/lib => ../lib\n",
    "go/lib/go.mod": "module example.com/lib\n",
    "go/other/go.mod": "module example.com/other\n",
    "js/web/package.json": '{"name": "web", "dependencies": '
    '{"ui": "workspace:*", "util": "file:../util", "core": "^1.0.0", '
    '"react": "^18"}}',
    "js/ui/package.json": '{"name": "ui"}',
    "js/util/package.json": '{"name": "util"}',
    "js/core/package.json": '{"name": "core"}',
    "py/app/pyproject.toml": '[project]\nname = "app"\n'
    'dependencies = ["Shared_Lib>=1", "models", "requests"]\n'
    "[tool.uv.sources]\nshared-lib = { workspace = true }\n"
    'models = { path = "../models" }\n',
    "py/shared/pyproject.toml": '[project]\nname = "shared.lib"\n',
    "py/models/pyproject.toml": '[project]\nname = "models"\n',
    "java/api/pom.xml": "<project><groupId>org.acme</groupId>"
    "<artifactId>api</artifactId></project>",
    "java/app/pom.xml": "<project><parent><groupId>org.acme</groupId>"
    "<artifactId>parent</artifactId></parent><artifactId>app</artifactId>"
    "<dependencies><dependency><groupId>org.acme</groupId>"
    "<artifactId>api</artifactId><scope>test</scope></dependency>"
    "<dependency><groupId>other</groupId><artifactId>api</artifactId>"
    "</dependency></dependencies></project>",
}


class DependencyTests(unittest.TestCase):
    def test_each_rule_and_external_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, DEPENDENCIES)
            output = asyncio.run(ComponentsTool(directory).analyze("repo"))
        edges = sorted(
            (
                r.source_match["qualified_name"].split("::")[1],
                r.target_match["qualified_name"].split("::")[1],
                r.properties["rule_id"],
                r.properties["evidence_type"],
                r.properties["section"],
            )
            for r in output.relationships
            if r.type == "REQUIRES"
        )
        self.assertEqual(
            edges,
            [
                (
                    "a/Cargo.toml",
                    "b/Cargo.toml",
                    "components.cargo-path-dependency",
                    "resolved",
                    "dev-dependencies",
                ),
                (
                    "a/Cargo.toml",
                    "b/Cargo.toml",
                    "components.cargo-workspace-dependency",
                    "resolved",
                    "dependencies",
                ),
                (
                    "a/Cargo.toml",
                    "c/Cargo.toml",
                    "components.cargo-patch-path",
                    "resolved",
                    "dependencies",
                ),
                (
                    "go/app/go.mod",
                    "go/lib/go.mod",
                    "components.go-replace-directory",
                    "resolved",
                    "require",
                ),
                (
                    "go/app/go.mod",
                    "go/other/go.mod",
                    "components.dependency-name",
                    "name_matched",
                    "require",
                ),
                (
                    "java/app/pom.xml",
                    "java/api/pom.xml",
                    "components.dependency-name",
                    "name_matched",
                    "dependencies.test",
                ),
                (
                    "js/web/package.json",
                    "js/core/package.json",
                    "components.dependency-name",
                    "name_matched",
                    "dependencies",
                ),
                (
                    "js/web/package.json",
                    "js/ui/package.json",
                    "components.npm-workspace-protocol",
                    "resolved",
                    "dependencies",
                ),
                (
                    "js/web/package.json",
                    "js/util/package.json",
                    "components.npm-file-dependency",
                    "resolved",
                    "dependencies",
                ),
                (
                    "py/app/pyproject.toml",
                    "py/models/pyproject.toml",
                    "components.python-path-source",
                    "resolved",
                    "project.dependencies",
                ),
                (
                    "py/app/pyproject.toml",
                    "py/shared/pyproject.toml",
                    "components.python-workspace-source",
                    "resolved",
                    "project.dependencies",
                ),
            ],
        )
        counts = output.metadata.custom_metrics["dependency_resolution"]
        # serde, react, requests and other:api are not in the repository.
        self.assertEqual(counts, {"linked": 11, "external": 4, "ambiguous": 0})
        api = next(
            n
            for n in output.nodes
            if n.properties.get("manifest") == "java/app/pom.xml"
        )
        self.assertEqual(api.properties["coordinates"], "org.acme:app")

    def test_paths_outside_the_repository_and_ambiguous_names(self):
        files = {
            "a/package.json": '{"name": "a", "dependencies": '
            '{"x": "file:../../x", "dup": "1"}}',
            "b/package.json": '{"name": "dup"}',
            "c/package.json": '{"name": "dup"}',
        }
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            output = asyncio.run(ComponentsTool(directory).analyze("repo"))
        self.assertFalse([r for r in output.relationships if r.type == "REQUIRES"])
        self.assertEqual(
            output.metadata.custom_metrics["dependency_resolution"],
            {"linked": 0, "external": 1, "ambiguous": 1},
        )


COMPOSE = {
    "compose.yaml": "services:\n"
    "  api:\n    build: ./api\n    depends_on: [db, cache]\n"
    "  worker:\n    build:\n      context: api\n      dockerfile: Worker\n"
    "    depends_on:\n      db:\n        condition: service_healthy\n"
    "  web:\n    image: registry.example:5000/acme/web:1.2\n"
    "  db:\n    image: postgres:16\n"
    "  remote:\n    build: https://github.com/acme/x.git\n"
    "  env:\n    build: ${CONTEXT}\n",
    "api/pyproject.toml": '[project]\nname = "api"\n',
    "api/app.py": "print(1)\n",
    "frontend/package.json": '{"name": "@acme/web"}',
    "ops/docker-compose.prod.yml": "services:\n  api:\n    build: ../api\n",
    "notcompose.yaml": "services: {}\n",
}


# Two Maven services joined by one HTTP contract, plus a client in the
# provider's own package, which consumes nothing.
CONTRACTS = {
    "stats/pom.xml": "<project><artifactId>stats</artifactId></project>",
    "stats/src/main/resources/application.properties": "spring.application.name=stats\n",
    "stats/src/main/java/s/StatsController.java": "package s;\n"
    "import org.springframework.web.bind.annotation.*;\n"
    "@RestController\n"
    '@RequestMapping("/stats")\n'
    "public class StatsController {\n"
    '  @GetMapping("/latest")\n'
    '  public String latest() { return ""; }\n'
    "}\n",
    "stats/src/main/java/s/Self.java": "package s;\n"
    "import org.springframework.web.client.RestTemplate;\n"
    "public class Self {\n"
    "  RestTemplate rest;\n"
    '  public String own() { return rest.getForObject("http://stats/stats/latest", String.class); }\n'
    "}\n",
    "web/pom.xml": "<project><artifactId>web</artifactId></project>",
    "web/src/main/java/w/StatsReader.java": "package w;\n"
    "import org.springframework.web.client.RestTemplate;\n"
    "public class StatsReader {\n"
    "  RestTemplate rest;\n"
    '  public String read() { return rest.getForObject("http://stats/stats/latest", String.class); }\n'
    '  public String again() { return rest.getForObject("http://stats/stats/latest", String.class); }\n'
    "}\n",
}
JAVA = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_java"
)


def contract_outputs(directory, repo):
    from deepthought.analysis_tools.service_contracts import ServiceContractTool

    write(directory, CONTRACTS)
    return [
        asyncio.run(TreeSitterTool(directory).analyze(repo, languages={"java"})),
        asyncio.run(ComponentsTool(directory).analyze(repo)),
        asyncio.run(ServiceContractTool(Path(directory)).analyze(repo)),
    ]


@unittest.skipUnless(JAVA, "Provision the java grammar")
class ConsumerTests(unittest.TestCase):
    def test_packages_joined_by_a_route(self):
        with tempfile.TemporaryDirectory() as directory:
            outputs = contract_outputs(directory, "repo")
        combined, _ = _combine(outputs, "repo", "rev", WriteCoordinatorConfig())
        consumes = [r for r in combined.relationships if r.type == "CONSUMES"]
        # Two requests from web to one route make one edge; the request from
        # inside stats makes none.
        self.assertEqual(
            [
                (
                    r.source_match["qualified_name"],
                    r.target_match["qualified_name"],
                    r.properties["route"].split("::")[1],
                    r.properties["rule_id"],
                    r.properties["evidence_type"],
                )
                for r in consumes
            ],
            [
                (
                    "repo::web/pom.xml::maven_module::component",
                    "repo::stats/pom.xml::maven_module::component",
                    "stats/src/main/java/s/StatsController.java",
                    "components.http-route-consumer",
                    "pattern_matched",
                )
            ],
        )
        self.assertEqual(
            combined.metadata.custom_metrics["component_consumers"], {"consumes": 1}
        )
        # Without the components tool there is nothing to join.
        alone, _ = _combine(
            [outputs[0], outputs[2]], "repo", "rev", WriteCoordinatorConfig()
        )
        self.assertFalse([r for r in alone.relationships if r.type == "CONSUMES"])


@unittest.skipUnless(YAML, "Provision PyYAML")
class ComposeTests(unittest.TestCase):
    def analyze(self, files):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            return asyncio.run(ComponentsTool(directory).analyze("repo"))

    def test_services_contexts_and_images(self):
        found, _ = __import__(
            "deepthought.deployments", fromlist=["extract_compose"]
        ).extract_compose("compose.yaml", COMPOSE["compose.yaml"].encode())
        self.assertEqual(
            [(c.name, c.context, c.image) for c in found],
            [
                ("api", "api", None),
                ("db", None, "postgres:16"),
                ("env", None, None),
                ("remote", None, None),
                ("web", None, "registry.example:5000/acme/web:1.2"),
                ("worker", "api", None),
            ],
        )

    def test_edges(self):
        output = self.analyze(COMPOSE)
        edges = sorted(
            (
                r.type,
                "::".join(r.source_match["qualified_name"].split("::")[1:-1]),
                "::".join(r.target_match["qualified_name"].split("::")[1:-1]),
                r.properties["rule_id"],
            )
            for r in output.relationships
            if r.type in {"REQUIRES", "DEPLOYS"}
        )
        self.assertEqual(
            edges,
            [
                (
                    "DEPLOYS",
                    "compose.yaml::compose_service::api",
                    "api/pyproject.toml::python_project",
                    "components.compose-build-context",
                ),
                (
                    "DEPLOYS",
                    "compose.yaml::compose_service::web",
                    "frontend/package.json::node_package",
                    "components.compose-image-name",
                ),
                (
                    "DEPLOYS",
                    "compose.yaml::compose_service::worker",
                    "api/pyproject.toml::python_project",
                    "components.compose-build-context",
                ),
                (
                    "DEPLOYS",
                    "ops/docker-compose.prod.yml::compose_service::api",
                    "api/pyproject.toml::python_project",
                    "components.compose-build-context",
                ),
                (
                    "REQUIRES",
                    "compose.yaml::compose_service::api",
                    "compose.yaml::compose_service::db",
                    "components.compose-depends-on",
                ),
                (
                    "REQUIRES",
                    "compose.yaml::compose_service::worker",
                    "compose.yaml::compose_service::db",
                    "components.compose-depends-on",
                ),
            ],
        )
        # depends_on naming no service of the file is external.
        self.assertEqual(
            output.metadata.custom_metrics["dependency_resolution"]["external"], 1
        )
        # Files under a build context belong to every service built from it.
        part_of = sorted(
            r.target_match["qualified_name"].split("::")[-2]
            for r in output.relationships
            if r.type == "PART_OF"
            and r.source_match["qualified_name"] == "repo::api/pyproject.toml::file"
        )
        self.assertEqual(part_of, ["api", "api", "python_project", "worker"])

    def test_invalid_compose_file(self):
        output = self.analyze({"compose.yaml": "services: [\n"})
        self.assertEqual(output.metadata.custom_metrics["analysis_status"], "partial")


KUBERNETES = {
    "src/api/go.mod": "module example.com/shop/api\n",
    "src/api/main.go": "package main\n",
    "src/web/package.json": '{"name": "web"}',
    "skaffold.yaml": "apiVersion: skaffold/v4beta6\nkind: Config\n"
    "build:\n  artifacts:\n  - image: api\n    context: src/api\n",
    "deploy/app.yaml": "apiVersion: apps/v1\nkind: Deployment\n"
    "metadata: {name: api, namespace: shop}\n"
    "spec:\n  template:\n    metadata:\n      labels: {app: api, tier: be}\n"
    "    spec:\n      containers:\n      - {name: server, image: api}\n"
    "---\napiVersion: v1\nkind: Service\n"
    "metadata: {name: api, namespace: shop}\nspec:\n  selector: {app: api}\n"
    "---\napiVersion: v1\nkind: Service\n"
    "metadata: {name: other-namespace}\nspec:\n  selector: {app: api}\n"
    "---\napiVersion: v1\nkind: Service\n"
    "metadata: {name: headless, namespace: shop}\nspec: {}\n",
    "deploy/web.yaml": "apiVersion: v1\nkind: List\nitems:\n"
    "- apiVersion: batch/v1\n  kind: CronJob\n"
    "  metadata: {name: web-job, namespace: shop}\n"
    "  spec:\n    jobTemplate:\n      spec:\n        template:\n"
    "          metadata: {labels: {app: web}}\n"
    "          spec: {containers: [{name: c, image: 'gcr.io/x/web:1'}]}\n",
    "elsewhere/svc.yaml": "apiVersion: v1\nkind: Service\n"
    "metadata: {name: api, namespace: shop}\nspec:\n  selector: {app: api}\n",
    "chart/templates/deploy.yaml": "apiVersion: apps/v1\nkind: Deployment\n"
    "metadata:\n  name: {{ .Release.Name }}\n",
    "ci.yml": "on: push\njobs: {}\n",
}


@unittest.skipUnless(YAML, "Provision PyYAML")
class KubernetesTests(unittest.TestCase):
    def test_objects_selectors_and_skaffold_contexts(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, KUBERNETES)
            output = asyncio.run(ComponentsTool(directory).analyze("repo"))
        self.assertEqual(output.metadata.custom_metrics["analysis_status"], "succeeded")
        units = {
            n.properties["name"]: n.properties
            for n in output.nodes
            if n.properties.get("kind") in {"k8s_workload", "k8s_service"}
        }
        self.assertEqual(
            sorted(units),
            [
                "CronJob/shop/web-job",
                "Deployment/shop/api",
                "Service/other-namespace",
                "Service/shop/api",
                "Service/shop/headless",
            ],
        )
        api = units["Deployment/shop/api"]
        self.assertEqual(api["root"], "src/api")
        self.assertEqual(api["labels"], ["app=api", "tier=be"])
        self.assertEqual(api["images"], ["api"])
        self.assertNotIn("root", units["CronJob/shop/web-job"])
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[-2],
                r.target_match["qualified_name"].split("::")[-2],
                r.properties["rule_id"],
            )
            for r in output.relationships
            if r.type in {"SELECTS", "DEPLOYS"}
        )
        self.assertEqual(
            edges,
            [
                (
                    "DEPLOYS",
                    "CronJob/shop/web-job",
                    "node_package",
                    "components.k8s-image-name",
                ),
                (
                    "DEPLOYS",
                    "Deployment/shop/api",
                    "go_module",
                    "components.k8s-skaffold-context",
                ),
                (
                    "SELECTS",
                    "Service/shop/api",
                    "Deployment/shop/api",
                    "components.k8s-service-selector",
                ),
            ],
        )
        part_of = sorted(
            r.target_match["qualified_name"].split("::")[-2]
            for r in output.relationships
            if r.type == "PART_OF"
            and r.source_match["qualified_name"] == "repo::src/api/go.mod::file"
        )
        self.assertEqual(part_of, ["Deployment/shop/api", "go_module"])


@unittest.skipUnless(
    READY and os.environ.get("DEEPTHOUGHT_NEO4J_URI"),
    "Provision isolated Neo4j and the python extra",
)
class ComponentQueryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from deepthought.graph_queries import GraphQueryService
        from deepthought.storage.graph import (
            Neo4jConnectionManager,
            WriteCoordinator,
            setup_schema,
        )

        self.manager = Neo4jConnectionManager(
            os.environ["DEEPTHOUGHT_NEO4J_URI"],
            os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
            os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
            database=os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j"),
        )
        await self.manager.connect()
        self.addAsyncCleanup(self.manager.close)
        await setup_schema(self.manager.driver, database=self.manager.database)
        self.repo = "components-" + uuid4().hex
        self.addAsyncCleanup(self.remove)
        self.service = GraphQueryService(
            self.manager.driver, database=self.manager.database
        )
        with tempfile.TemporaryDirectory() as directory:
            write(directory, WORKSPACE)
            outputs = [
                await TreeSitterTool(directory).analyze(
                    self.repo, languages={"python", "typescript", "go"}
                ),
                await ComponentsTool(directory).analyze(self.repo),
            ]
        self.snapshot = "rev-" + uuid4().hex
        written = await WriteCoordinator(
            self.manager.driver, database=self.manager.database
        ).write_outputs(outputs, self.repo, snapshot_id=self.snapshot)
        self.assertTrue(written.success, written.errors)

    async def remove(self):
        async with self.manager.driver.session(
            database=self.manager.database
        ) as session:
            await (
                await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=self.repo
                )
            ).consume()

    async def test_list_components_with_file_counts(self):
        listed = await self.service.list_components(
            self.repo, snapshot_id=self.snapshot, limit=50
        )
        rows = [
            (
                item["element"]["properties"]["kind"],
                item["element"]["properties"]["root"],
                item["files"],
            )
            for item in listed["items"]
        ]
        self.assertEqual(
            rows,
            [
                ("python_project", "", 12),
                ("cargo_package", "crates/core", 1),
                ("maven_module", "java", 1),
                ("go_module", "svc", 2),
                ("node_package", "web", 2),
                ("node_package", "web/nested", 2),
            ],
        )
        go = await self.service.list_components(
            self.repo, snapshot_id=self.snapshot, kind="go_module"
        )
        self.assertEqual(
            [item["element"]["properties"]["name"] for item in go["items"]],
            ["example.com/svc"],
        )

    async def test_file_components_and_component_files(self):
        found = await self.service.get_file_components(
            "web/nested/a.ts", self.repo, snapshot_id=self.snapshot
        )
        self.assertEqual(
            sorted(item["element"]["properties"]["root"] for item in found["items"]),
            ["", "web/nested"],
        )
        for item in found["items"]:
            self.assertEqual(
                item["relationship"]["properties"]["rule_id"], "components.nearest-root"
            )
        web = "%s::web/package.json::node_package::component" % self.repo
        first = await self.service.get_component_files(
            web, self.repo, snapshot_id=self.snapshot, limit=1
        )
        self.assertEqual([i["path"] for i in first["items"]], ["web/package.json"])
        rest = await self.service.get_component_files(
            web,
            self.repo,
            snapshot_id=self.snapshot,
            after_path=first["next_after_path"],
        )
        self.assertEqual([i["path"] for i in rest["items"]], ["web/src/index.ts"])

    async def test_dependencies_and_component_impact(self):
        from deepthought.storage.graph import WriteCoordinator

        repo = self.repo + "-deps"
        self.addAsyncCleanup(self.remove_repo, repo)
        with tempfile.TemporaryDirectory() as directory:
            write(directory, DEPENDENCIES)
            output = await ComponentsTool(directory).analyze(repo)
        written = await WriteCoordinator(
            self.manager.driver, database=self.manager.database
        ).write_tool_output("components", output, repo)
        self.assertTrue(written.success, written.errors)
        snapshot = output.metadata.custom_metrics["snapshot_id"]
        b = repo + "::b/Cargo.toml::cargo_package::component"
        required_by = await self.service.get_component_dependencies(
            b, repo, snapshot_id=snapshot, incoming=True
        )
        self.assertEqual(
            [
                (
                    i["element"]["properties"]["manifest"],
                    i["relationship"]["properties"]["rule_id"],
                )
                for i in required_by["items"]
            ],
            # Ordered by section: dependencies, then dev-dependencies.
            [
                ("a/Cargo.toml", "components.cargo-workspace-dependency"),
                ("a/Cargo.toml", "components.cargo-path-dependency"),
            ],
        )
        core = repo + "::js/core/package.json::node_package::component"
        impact = await self.service.get_component_impact(
            core, repo, snapshot_id=snapshot
        )
        self.assertEqual(
            [
                (i["element"]["display_name"], i["evidence_type"])
                for i in impact["items"]
            ],
            [("web", "name_matched")],
        )
        strict = await self.service.get_component_impact(
            core, repo, snapshot_id=snapshot, evidence_types=["resolved"]
        )
        self.assertEqual(strict["items"], [])

    @unittest.skipUnless(JAVA, "Provision the java grammar")
    async def test_component_impact_crosses_http_contracts(self):
        from deepthought.storage.graph import WriteCoordinator

        repo = self.repo + "-contracts"
        self.addAsyncCleanup(self.remove_repo, repo)
        with tempfile.TemporaryDirectory() as directory:
            outputs = contract_outputs(directory, repo)
        snapshot = "rev-" + uuid4().hex
        written = await WriteCoordinator(
            self.manager.driver, database=self.manager.database
        ).write_outputs(outputs, repo, snapshot_id=snapshot)
        self.assertTrue(written.success, written.errors)
        impact = await self.service.get_component_impact(
            repo + "::stats/pom.xml::maven_module::component",
            repo,
            snapshot_id=snapshot,
        )
        self.assertEqual(
            [
                (
                    i["element"]["display_name"],
                    i["depth"],
                    i["evidence_type"],
                    [e["type"] for e in i["edges"]],
                    [e["rule_id"] for e in i["edges"]],
                )
                for i in impact["items"]
            ],
            [
                (
                    "web",
                    1,
                    "pattern_matched",
                    ["CONSUMES"],
                    ["components.http-route-consumer"],
                )
            ],
        )

    @unittest.skipUnless(YAML, "Provision PyYAML")
    async def test_component_impact_reaches_deployments(self):
        from deepthought.storage.graph import WriteCoordinator

        found = {}
        for name, files in (("k8s", KUBERNETES), ("compose", COMPOSE)):
            repo = "%s-%s" % (self.repo, name)
            self.addAsyncCleanup(self.remove_repo, repo)
            with tempfile.TemporaryDirectory() as directory:
                write(directory, files)
                output = await ComponentsTool(directory).analyze(repo)
            written = await WriteCoordinator(
                self.manager.driver, database=self.manager.database
            ).write_tool_output("components", output, repo)
            self.assertTrue(written.success, written.errors)
            found[name] = (repo, output.metadata.custom_metrics["snapshot_id"])

        async def impact(name, component):
            repo, snapshot = found[name]
            response = await self.service.get_component_impact(
                "%s::%s" % (repo, component), repo, snapshot_id=snapshot
            )
            return [
                (
                    i["element"]["qualified_name"].split("::", 1)[1],
                    i["depth"],
                    i["evidence_type"],
                    [e["type"] for e in i["edges"]],
                )
                for i in response["items"]
            ]

        # A package reaches the workload that runs it and the Service in
        # front of that workload.
        self.assertEqual(
            await impact("k8s", "src/api/go.mod::go_module::component"),
            [
                (
                    "deploy/app.yaml::k8s_workload::Deployment/shop/api::component",
                    1,
                    "resolved",
                    ["DEPLOYS"],
                ),
                (
                    "deploy/app.yaml::k8s_service::Service/shop/api::component",
                    2,
                    "resolved",
                    ["SELECTS", "DEPLOYS"],
                ),
            ],
        )
        self.assertEqual(
            await impact("compose", "api/pyproject.toml::python_project::component"),
            [
                (
                    "compose.yaml::compose_service::api::component",
                    1,
                    "resolved",
                    ["DEPLOYS"],
                ),
                (
                    "compose.yaml::compose_service::worker::component",
                    1,
                    "resolved",
                    ["DEPLOYS"],
                ),
                (
                    "ops/docker-compose.prod.yml::compose_service::api::component",
                    1,
                    "resolved",
                    ["DEPLOYS"],
                ),
            ],
        )
        self.assertEqual(
            await impact("compose", "compose.yaml::compose_service::db::component"),
            [
                (
                    "compose.yaml::compose_service::api::component",
                    1,
                    "resolved",
                    ["REQUIRES"],
                ),
                (
                    "compose.yaml::compose_service::worker::component",
                    1,
                    "resolved",
                    ["REQUIRES"],
                ),
            ],
        )

    async def remove_repo(self, repo):
        async with self.manager.driver.session(
            database=self.manager.database
        ) as session:
            await (
                await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=repo
                )
            ).consume()


if __name__ == "__main__":
    unittest.main()
