"""Compose projects, Helm charts and kustomizations rendered by their tools."""

from pathlib import Path
import shutil
import tempfile
import unittest
import unittest.mock

from deepthought import deploy_facts
from deepthought.service_contracts.compose import compose_units
from deepthought.service_contracts.deployed import kubernetes_units

# TEMPORARY: the tools are required; this skip exists only because the
# offline acceptance sandbox does not provision Go yet (#323, #344).
needs_go = unittest.skipUnless(shutil.which("go"), "the Go toolchain is not installed")

COMPOSE = {
    "deploy/.env": "REGISTRY=registry.example\n",
    "deploy/docker-compose.yml": (
        "services:\n"
        "  orders:\n"
        "    build: ../orders\n"
        "    image: ${REGISTRY}/orders:${TAG}\n"
        "    environment:\n"
        "      DB_URL: postgres://db/${DB_NAME:-orders}\n"
        "      SECRET: ${SECRET}\n"
        "    volumes:\n"
        "      - ${PWD}/config:/config\n"
        "      - data:/data\n"
        "      - ${APPDATA}/secrets:/secrets:ro\n"
        "  billing:\n"
        "    extends:\n"
        "      file: common.yml\n"
        "      service: java\n"
        "    build: ../billing\n"
        "volumes:\n"
        "  data: {}\n"
    ),
    "deploy/docker-compose.override.yml": (
        "services:\n  orders:\n    environment:\n      DEBUG: 'true'\n"
    ),
    "deploy/common.yml": (
        "services:\n  java:\n    environment:\n      JAVA_OPTS: -Xmx512m\n"
    ),
    "deploy/docker-compose-kafka.yml": (
        "services:\n  kafka:\n    image: bitnami/kafka\n"
    ),
}

CHART = {
    "charts/shop/Chart.yaml": "apiVersion: v2\nname: shop\nversion: 1.0.0\n",
    "charts/shop/values.yaml": "port: 8080\n",
    "charts/shop/templates/deployment.yaml": (
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: {{ .Release.Name }}-cart\n"
        "spec:\n"
        "  selector:\n"
        "    matchLabels: {app: cart}\n"
        "  template:\n"
        "    metadata:\n"
        "      labels: {app: cart}\n"
        "    spec:\n"
        "      containers:\n"
        "        - name: cart\n"
        "          image: cart\n"
        "          env:\n"
        "            - name: PORT\n"
        "              value: {{ .Values.port | quote }}\n"
    ),
    "charts/shop/templates/service.yaml": (
        "apiVersion: v1\n"
        "kind: Service\n"
        "metadata:\n"
        "  name: cartservice\n"
        "spec:\n"
        "  selector: {app: cart}\n"
        "  ports: [{port: 80}]\n"
    ),
    "skaffold.yaml": (
        "apiVersion: skaffold/v4beta6\n"
        "kind: Config\n"
        "build:\n"
        "  artifacts:\n"
        "    - image: cart\n"
        "      context: src/cart\n"
    ),
}

KUSTOMIZE = {
    "k8s/base/kustomization.yaml": "resources: [deployment.yaml]\n",
    "k8s/base/deployment.yaml": (
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata: {name: web}\n"
        "spec:\n"
        "  template:\n"
        "    metadata: {labels: {app: web}}\n"
        "    spec:\n"
        "      containers: [{name: web, image: web}]\n"
    ),
    "k8s/base/job.yaml": "apiVersion: batch/v1\nkind: Job\nmetadata: {name: j}\n",
    "k8s/overlays/prod/kustomization.yaml": (
        "resources: [../../base]\nnamePrefix: prod-\ncomponents: [../../extra]\n"
    ),
    "k8s/extra/kustomization.yaml": (
        "apiVersion: kustomize.config.k8s.io/v1alpha1\n"
        "kind: Component\n"
        "commonLabels: {tier: front}\n"
    ),
}


def _write(root, files):
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")


@needs_go
class DeployFactsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_compose_as_the_loader_reads_it(self):
        _write(self.root, COMPOSE)
        facts = deploy_facts.collect(self.root)
        projects = {p.file: p for p in facts.compose}
        self.assertEqual(
            sorted(projects),
            ["deploy/docker-compose-kafka.yml", "deploy/docker-compose.yml"],
        )
        project = projects["deploy/docker-compose.yml"]
        self.assertEqual(
            project.files,
            ("deploy/docker-compose.yml", "deploy/docker-compose.override.yml"),
        )
        self.assertEqual(project.missing, ("APPDATA", "SECRET", "TAG"))
        services = {s.name: s for s in project.services}
        orders = services["orders"]
        self.assertEqual(orders.context, "orders")
        self.assertEqual(orders.image, "registry.example/orders:\x00")
        self.assertEqual(
            dict(orders.environment),
            {"DB_URL": "postgres://db/orders", "SECRET": "\x00", "DEBUG": "true"},
        )
        self.assertEqual(
            orders.volumes[:2], (("deploy/config", "/config"), (None, "/data"))
        )
        # An unset variable in a bind source: still parsed, source unknown.
        self.assertEqual(orders.volumes[2], (None, "/secrets"))
        # extends from another file.
        self.assertEqual(
            dict(services["billing"].environment), {"JAVA_OPTS": "-Xmx512m"}
        )
        self.assertEqual(facts.errors, [])

    def test_compose_units(self):
        _write(self.root, COMPOSE)
        units = {u.label: u for u in compose_units(self.root)}
        self.assertEqual(
            sorted(units),
            ["deploy/docker-compose.yml:billing", "deploy/docker-compose.yml:orders"],
        )
        orders = units["deploy/docker-compose.yml:orders"]
        self.assertEqual(orders.env("DB_URL"), "postgres://db/orders")
        self.assertIsNone(orders.env("SECRET"))  # nothing sets it
        self.assertEqual(orders.env("DEBUG"), "true")  # the override file's
        self.assertEqual(orders.host_path("/config/app.yml"), "deploy/config/app.yml")
        self.assertIsNone(orders.host_path("/data/x"))
        self.assertIsNone(orders.host_path("/secrets/x"))
        self.assertEqual(orders.dockerfile, "orders/Dockerfile")

    def test_helm_chart_rendered(self):
        _write(self.root, CHART)
        facts = deploy_facts.collect(self.root)
        self.assertEqual(facts.charts, {"charts/shop"})
        self.assertTrue(facts.tool_input("charts/shop/templates/service.yaml"))
        (rendering,) = facts.renderings
        self.assertEqual(
            (rendering.source, rendering.tool), ("charts/shop/Chart.yaml", "helm")
        )
        self.assertEqual(
            sorted(d["metadata"]["name"] for d in rendering.documents),
            ["cartservice", "release-name-cart"],
        )
        (unit,) = kubernetes_units(self.root)
        self.assertEqual(
            (unit.label, unit.context, unit.hosts, unit.env("PORT")),
            (
                "charts/shop/Chart.yaml:Deployment/release-name-cart",
                "src/cart",
                ("cartservice",),
                "8080",
            ),
        )

    def test_kustomizations_rendered_components_not_alone(self):
        _write(self.root, KUSTOMIZE)
        facts = deploy_facts.collect(self.root)
        self.assertEqual(facts.kustomizations, {"k8s/base", "k8s/overlays/prod"})
        rendered = {
            r.source: [
                (d["metadata"]["name"], d["metadata"].get("labels"))
                for d in r.documents
            ]
            for r in facts.renderings
        }
        self.assertEqual(
            rendered,
            {
                "k8s/base/kustomization.yaml": [("web", None)],
                "k8s/overlays/prod/kustomization.yaml": [
                    ("prod-web", {"tier": "front"})
                ],
            },
        )
        self.assertTrue(facts.tool_input("k8s/base/deployment.yaml"))
        self.assertFalse(facts.tool_input("k8s/other.yaml"))
        # Next to a kustomization that does not list it: its own manifest.
        self.assertFalse(facts.tool_input("k8s/base/job.yaml"))

    def test_unrenderable_descriptions_are_reported(self):
        _write(
            self.root,
            {
                "compose.yaml": "services:\n  a:\n    ports: ['${PORT}:80']\n",
                "chart/Chart.yaml": (
                    "apiVersion: v2\nname: c\nversion: 1.0.0\n"
                    "dependencies:\n  - name: redis\n    version: 1.0.0\n"
                    "    repository: https://charts.example\n"
                ),
            },
        )
        facts = deploy_facts.collect(self.root)
        self.assertEqual(
            sorted((d, tool) for d, tool, _ in facts.errors),
            [("chart", "helm"), ("compose.yaml", "compose")],
        )
        self.assertEqual(facts.compose, [])
        self.assertEqual(facts.renderings, [])

    def test_no_descriptions_needs_no_tool(self):
        (self.root / "app.py").write_text("x = 1\n", encoding="utf-8")
        self.assertEqual(deploy_facts.requests(self.root), [])


class MissingToolTests(unittest.TestCase):
    def test_missing_go_fails_loudly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root, {"compose.yaml": "services:\n  a:\n    image: a\n"})
            with unittest.mock.patch.dict(
                "os.environ", {"DEEPTHOUGHT_GO": "", "PATH": directory}
            ):
                deploy_facts._collect.cache_clear()
                with self.assertRaises(deploy_facts.DeployToolsMissing):
                    deploy_facts.collect(root)


if __name__ == "__main__":
    unittest.main()
