"""Services named by their deployments, and Locust's --host."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.service_contracts.deployed import (
    Deployed,
    Deployments,
    aspire_units,
    command_line,
    deployed_units,
    dotnet_project,
    locust_host,
)

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec("yaml")

SKAFFOLD = """
apiVersion: skaffold/v3
kind: Config
build:
  artifacts:
  - image: frontend
    context: src/frontend
  - image: admin
    context: src/admin
  - image: loadgenerator
    context: src/loadgenerator
"""

MANIFESTS = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: frontend
spec:
  template:
    metadata:
      labels:
        app: frontend
    spec:
      containers:
      - name: server
        image: frontend
---
apiVersion: v1
kind: Service
metadata:
  name: frontend
spec:
  selector:
    app: frontend
---
apiVersion: v1
kind: Service
metadata:
  name: frontend-external
spec:
  selector:
    app: frontend
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: admin
spec:
  template:
    metadata:
      labels:
        app: admin
    spec:
      containers:
      - name: server
        image: admin:1.0
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: loadgenerator
spec:
  template:
    spec:
      containers:
      - name: main
        image: loadgenerator
        env:
        - name: FRONTEND_ADDR
          value: "frontend:80"
"""

FLASK = """
from flask import Flask
app = Flask(__name__)

@app.route("/")
def index():
    return "home"

@app.route("/cart")
def cart():
    return "cart"
"""

LOCUST = """
from locust import HttpUser, task

class Shopper(HttpUser):
    @task
    def index(self):
        self.client.get("/")

    @task
    def cart(self):
        self.client.get("/cart")
"""

DOCKERFILE = """
FROM python:3.12
COPY . .
ENTRYPOINT locust --host="http://${FRONTEND_ADDR}" --headless -u "${USERS:-10}"
"""

FILES = {
    "skaffold.yaml": SKAFFOLD,
    "kubernetes-manifests/app.yaml": MANIFESTS,
    "src/frontend/app.py": FLASK,
    "src/admin/app.py": FLASK,
    "src/loadgenerator/locustfile.py": LOCUST,
    "src/loadgenerator/Dockerfile": DOCKERFILE,
}


def _write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@unittest.skipUnless(READY, "tree-sitter or PyYAML is not installed")
class DeployedServiceTests(unittest.TestCase):
    def analyze(self, files):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root, files)
            units = deployed_units(root)
            output = asyncio.run(ServiceContractTool(root).analyze("r"))
        return units, output

    def test_units_and_route_services(self):
        units, output = self.analyze(FILES)
        self.assertEqual(
            sorted((u.label, u.context, u.hosts) for u in units),
            [
                ("kubernetes-manifests/app.yaml:Deployment/admin", "src/admin", ()),
                (
                    "kubernetes-manifests/app.yaml:Deployment/frontend",
                    "src/frontend",
                    ("frontend", "frontend-external"),
                ),
                (
                    "kubernetes-manifests/app.yaml:Deployment/loadgenerator",
                    "src/loadgenerator",
                    (),
                ),
            ],
        )
        services = sorted(
            (n.properties["file_path"], n.properties["path"], n.properties["service"])
            for n in output.nodes
            if "HttpRoute" in n.labels
        )
        self.assertEqual(
            services,
            [
                ("src/admin/app.py", "/", "admin"),
                ("src/admin/app.py", "/cart", "admin"),
                ("src/frontend/app.py", "/", "frontend"),
                ("src/frontend/app.py", "/cart", "frontend"),
            ],
        )

    def test_locust_host_picks_the_frontend(self):
        _, output = self.analyze(FILES)
        requests = sorted(
            (
                r.properties["request"],
                r.target_match["qualified_name"].split("::")[1],
                r.properties["rule_id"],
                tuple(r.properties["host_from"]),
            )
            for r in output.relationships
            if r.type == "REQUESTS"
        )
        unit = ("kubernetes-manifests/app.yaml:Deployment/loadgenerator",)
        self.assertEqual(
            requests,
            [
                (
                    "GET http://frontend:80/",
                    "src/frontend/app.py",
                    "service-contracts.locust-host-service-path",
                    unit,
                ),
                (
                    "GET http://frontend:80/cart",
                    "src/frontend/app.py",
                    "service-contracts.locust-host-service-path",
                    unit,
                ),
            ],
        )

    def test_without_a_host_both_services_fit(self):
        files = dict(FILES)
        files["src/loadgenerator/Dockerfile"] = "FROM python\nENTRYPOINT locust\n"
        _, output = self.analyze(files)
        self.assertFalse([r for r in output.relationships if r.type == "REQUESTS"])
        self.assertEqual(
            output.metadata.custom_metrics["http_request_resolution"],
            {"ambiguous:several_routes": 2},
        )


class CommandLineTests(unittest.TestCase):
    def unit(self, **values):
        defaults = dict(
            label="x",
            name="load",
            context="load",
            hosts=(),
            environment=(("HOST", "web:8080"), ("UNSET", None)),
        )
        return Deployed(**{**defaults, **values})

    def test_locust_host(self):
        unit = self.unit()
        self.assertEqual(
            locust_host('locust --host="http://${HOST}" -u 5', unit), "http://web:8080"
        )
        self.assertEqual(
            locust_host("locust -H http://$HOST/", unit), "http://web:8080/"
        )
        self.assertEqual(
            locust_host("/usr/bin/locust --host=http://a", unit), "http://a"
        )
        self.assertIsNone(locust_host("locust --host=http://${UNSET}", unit))
        self.assertIsNone(locust_host("locust --host=http://${MISSING}", unit))
        self.assertIsNone(locust_host("locust --headless", unit))
        self.assertIsNone(locust_host("python run.py --host=http://a", unit))

    def test_kubernetes_command_and_args_replace_the_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(
                root,
                {
                    "load/Dockerfile": 'ENTRYPOINT ["locust"]\nCMD ["--host", "http://a"]\n'
                },
            )
            self.assertEqual(command_line(root, self.unit()), "locust --host http://a")
            self.assertEqual(
                command_line(root, self.unit(args=("--host=http://b",))),
                "locust --host=http://b",
            )
            self.assertEqual(
                command_line(root, self.unit(command=("sh", "-c", "run.sh"))),
                "sh -c run.sh",
            )
            self.assertIsNone(command_line(root, self.unit(command=())))

    def test_a_host_claimed_by_two_services_names_neither(self):
        deployments = Deployments(
            [
                self.unit(label="a", name="a", context="a", hosts=("a", "shared")),
                self.unit(label="b", name="b", context="b", hosts=("b", "shared")),
                self.unit(label="c1", name="c", context="c", hosts=("c",)),
                self.unit(label="c2", name="other", context="c", hosts=("c2",)),
            ]
        )
        self.assertEqual(deployments.hosts(), {"a": "a", "b": "b"})
        self.assertEqual(deployments.service("a/x/y.py"), "a")
        self.assertIsNone(deployments.service("c/y.py"))
        self.assertIsNone(deployments.service("d/y.py"))


VS_DOCKERFILE = """#See https://aka.ms/containerfastmode
FROM mcr.microsoft.com/dotnet/aspnet:8.0 AS base
WORKDIR /app

FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY ["src/Catalog.API/Catalog.API.csproj", "src/Catalog.API/"]
RUN dotnet restore "src/Catalog.API/Catalog.API.csproj"
COPY . .
WORKDIR "/src/src/Catalog.API"
RUN dotnet build "Catalog.API.csproj" -c Release -o /app/build

FROM build AS publish
RUN dotnet publish "Catalog.API.csproj" -c Release \\
    -o /app/publish /p:UseAppHost=false

FROM base AS final
WORKDIR /app
COPY --from=publish /app/publish .
ENTRYPOINT ["dotnet", "Catalog.API.dll"]
"""

SUBDIRECTORY_DOCKERFILE = """FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src/orders
COPY src/Ordering.API .
RUN dotnet publish -c Release -o out
"""

PATTERN_DOCKERFILE = """FROM mcr.microsoft.com/dotnet/sdk:8.0
WORKDIR /app
COPY . ./
RUN dotnet restore && dotnet publish src/Basket.API/*.csproj -c Release -o out
"""


class DotnetProjectTests(unittest.TestCase):
    def test_the_published_project_runs(self):
        files = {
            "src/Catalog.API/Catalog.API.csproj": "<Project />",
            "src/Catalog.API/Dockerfile": VS_DOCKERFILE,
            "src/Ordering.API/Ordering.API.csproj": "<Project />",
            "src/Ordering.API/Dockerfile": SUBDIRECTORY_DOCKERFILE,
            "src/Basket.API/Basket.API.csproj": "<Project />",
            "src/Basket.API/Dockerfile": PATTERN_DOCKERFILE,
            # Two different projects published: none is chosen.
            "src/Two/Dockerfile": (
                "FROM sdk\nWORKDIR /s\nCOPY . .\n"
                "RUN dotnet publish src/Catalog.API/Catalog.API.csproj\n"
                "RUN dotnet publish src/Ordering.API/Ordering.API.csproj\n"
            ),
            # The project is copied from another image, not the context.
            "src/Other/Dockerfile": (
                "FROM sdk\nWORKDIR /s\nCOPY --from=base . .\n"
                "RUN dotnet publish x.csproj\n"
            ),
            # Published from a directory with no project.
            "src/Empty/Dockerfile": (
                "FROM sdk\nWORKDIR /s\nCOPY . .\nWORKDIR /s/docs\nRUN dotnet publish\n"
            ),
            "docker-compose.yml": (
                "services:\n"
                "  catalog-api:\n"
                "    build:\n"
                "      context: .\n"
                "      dockerfile: src/Catalog.API/Dockerfile\n"
                "  ordering-api:\n"
                "    build: {context: ., dockerfile: src/Ordering.API/Dockerfile}\n"
                "  basket-api:\n"
                "    build: {context: ., dockerfile: src/Basket.API/Dockerfile}\n"
                "  two:\n"
                "    build: {context: ., dockerfile: src/Two/Dockerfile}\n"
                "  other:\n"
                "    build: {context: ., dockerfile: src/Other/Dockerfile}\n"
                "  empty:\n"
                "    build: {context: ., dockerfile: src/Empty/Dockerfile}\n"
                "  node:\n"
                "    build: src/web\n"
            ),
            "src/web/Dockerfile": "FROM node\nCOPY . .\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root, files)
            units = sorted((u.name, u.context) for u in deployed_units(root))
            self.assertIsNone(dotnet_project(root, "", None))
            self.assertIsNone(dotnet_project(root, "", "missing/Dockerfile"))
        self.assertEqual(
            units,
            [
                ("basket-api", "src/Basket.API"),
                ("catalog-api", "src/Catalog.API"),
                ("empty", ""),
                ("node", "src/web"),
                ("ordering-api", "src/Ordering.API"),
                ("other", ""),
                ("two", ""),
            ],
        )


APPHOST = """
var builder = DistributedApplication.CreateBuilder(args);
var catalog = builder.AddProject<Projects.Catalog_API>("catalog-api");
builder.AddProject("ordering-api", "../Ordering.API/Ordering.API.csproj");
builder.AddDotnetProject("webapp", "../WebApp")
    .WithReference(catalog);
// builder.AddProject("commented", "../WebApp");
builder.AddProject<Projects.Missing>("missing");
builder.AddDotnetProject("dynamic", Paths.Of("WebApp"));
builder.Build().Run();
"""


class AspireTests(unittest.TestCase):
    def test_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(
                root,
                {
                    "src/AppHost/AppHost.cs": APPHOST,
                    "src/AppHost/AppHost.csproj": "<Project />",
                    "src/Catalog.API/Catalog.API.csproj": "<Project />",
                    "src/Ordering.API/Ordering.API.csproj": "<Project />",
                    "src/WebApp/WebApp.csproj": "<Project />",
                    # Not an AppHost: no CreateBuilder call.
                    "tests/Fixture.cs": 'x.AddProject("other", "../src/WebApp");',
                },
            )
            units = sorted((u.name, u.context, u.hosts) for u in aspire_units(root))
        self.assertEqual(
            units,
            [
                ("catalog-api", "src/Catalog.API", ("catalog-api",)),
                ("ordering-api", "src/Ordering.API", ("ordering-api",)),
                ("webapp", "src/WebApp", ("webapp",)),
            ],
        )


if __name__ == "__main__":
    unittest.main()
