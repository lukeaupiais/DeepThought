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
            (item["element"]["kind"], item["element"]["root"], item["files"])
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
            [item["element"]["name"] for item in go["items"]], ["example.com/svc"]
        )

    async def test_file_components_and_component_files(self):
        found = await self.service.get_file_components(
            "web/nested/a.ts", self.repo, snapshot_id=self.snapshot
        )
        self.assertEqual(
            sorted(item["element"]["root"] for item in found["items"]),
            ["", "web/nested"],
        )
        for item in found["items"]:
            self.assertEqual(item["relationship"]["rule_id"], "components.nearest-root")
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


if __name__ == "__main__":
    unittest.main()
