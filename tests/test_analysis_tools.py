"""Public interface/conformance tests; no private project inputs or model calls."""

import asyncio
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deepthought import analyze_repository
from deepthought.analysis_tools import (
    AnalysisTool,
    NodeData,
    RelationshipData,
    StandardizedOutput,
    ToolCapability,
    ToolExecutionMetadata,
    ToolRegistry,
    TreeSitterTool,
    tool_registry,
)

GRAMMARS = all(
    importlib.util.find_spec(name) is not None
    for name in ("tree_sitter", "tree_sitter_python", "tree_sitter_typescript")
)


class RegistryTests(unittest.TestCase):
    def test_builtin_is_discoverable_with_truthful_capabilities(self):
        metadata = tool_registry.list_tools()
        self.assertEqual([m.name for m in metadata], ["tree-sitter"])
        self.assertEqual(metadata[0].supported_languages, {"python", "typescript"})
        self.assertEqual(
            metadata[0].capabilities,
            {ToolCapability.AST_EXTRACTION, ToolCapability.SYMBOL_INDEXING},
        )
        self.assertFalse(tool_registry.get_tools_for_language("go", Path(".")))
        self.assertFalse(
            tool_registry.get_tools_for_capability(ToolCapability.CALL_GRAPH, Path("."))
        )

    def test_registration_unknown_duplicate_and_abstract_tool(self):
        registry = ToolRegistry()
        self.assertIsNone(registry.get_tool("missing", Path(".")))
        self.assertIs(registry.register(TreeSitterTool), TreeSitterTool)
        with self.assertRaises(ValueError):
            registry.register(TreeSitterTool)
        with self.assertRaises(TypeError):
            registry.register(object)
        with self.assertRaises(TypeError):
            registry.register(AnalysisTool)
        first = registry.get_tool("tree-sitter", Path("a"))
        second = registry.get_tool("tree-sitter", Path("b"))
        self.assertIsNot(first, second)
        self.assertEqual(first.repo_path, Path("a"))
        self.assertEqual(second.repo_path, Path("b"))
        self.assertTrue(first.supports_language("Python"))
        self.assertEqual(len(registry.get_tools_for_language("python", Path("a"))), 1)
        self.assertEqual(
            len(
                registry.get_tools_for_capability(
                    ToolCapability.SYMBOL_INDEXING, Path("a")
                )
            ),
            1,
        )

    def test_import_does_not_load_optional_analyzer_or_storage_dependencies(self):
        code = """
import sys
import deepthought.analysis_tools as tools
import deepthought.storage.graph
import deepthought.graph_queries
import deepthought.graph_cli
import deepthought.graph_mcp
assert [m.name for m in tools.tool_registry.list_tools()] == ['tree-sitter']
for module in ('tree_sitter', 'tree_sitter_python', 'tree_sitter_typescript', 'neo4j', 'mcp', 'torch'):
    assert module not in sys.modules, module
"""
        result = subprocess.run(
            [sys.executable, "-c", code], check=False, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_performance_estimate_stays_unknown_and_availability_does_not_install(self):
        tool = TreeSitterTool(Path("."))
        estimate = tool.get_performance_estimate(1000)
        self.assertIsNone(estimate["time_estimate_seconds"])
        self.assertIsNone(estimate["memory_estimate_mb"])
        with self.assertRaises(ValueError):
            tool.get_performance_estimate(-1)
        with patch(
            "deepthought.analysis_tools.tree_sitter.importlib.util.find_spec",
            return_value=None,
        ):
            self.assertFalse(asyncio.run(tool.check_availability()))


class ContractTests(unittest.TestCase):
    def test_records_roundtrip_and_validate_qualified_identity(self):
        node = NodeData(
            ["Function"], {"qualified_name": "repo::a.py::f", "repo_url": "repo"}
        )
        edge = RelationshipData(
            "DECLARES",
            ["File"],
            {"qualified_name": "repo::a.py"},
            ["Function"],
            {"qualified_name": "repo::a.py::f"},
        )
        output = StandardizedOutput(
            [node], [edge], ToolExecutionMetadata("test", "1", 0)
        )
        self.assertEqual(json.loads(output.to_json()), output.to_dict())
        self.assertEqual(output.get_statistics()["total_nodes"], 1)
        self.assertEqual(output.get_statistics()["relationship_types"], {"DECLARES": 1})
        self.assertEqual(node.to_cypher_map()["properties"], node.properties)
        with self.assertRaises(ValueError):
            NodeData(["Function"], {"name": "f", "repo_url": "repo"})
        with self.assertRaises(ValueError):
            RelationshipData(
                "CALLS", ["Function"], {"name": "f"}, ["Function"], {"name": "g"}
            )
        with self.assertRaises(ValueError):
            StandardizedOutput([node, node], [], output.metadata)
        with self.assertRaises(ValueError):
            ToolExecutionMetadata("test", "1", float("nan"))
        with self.assertRaises(ValueError):
            NodeData(
                ["Function"],
                {
                    "qualified_name": "repo::a.py::f",
                    "repo_url": "repo",
                    "bad": float("inf"),
                },
            )

    def test_missing_grammar_is_explicit_and_not_empty_success(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "sample.py").write_text("class Available: pass\n")
            tool = TreeSitterTool(Path(root))
            with patch("deepthought.engine.make_parser", side_effect=ImportError):
                result = asyncio.run(tool.analyze("fixture", {"python"}))
            self.assertEqual(
                result.metadata.custom_metrics["analysis_status"], "failed"
            )
            self.assertFalse([n for n in result.nodes if "Symbol" in n.labels])
            self.assertEqual(
                result.metadata.custom_metrics["coverage"][0]["status"], "unavailable"
            )
            self.assertEqual(
                result.metadata.custom_metrics["diagnostics"][0]["code"],
                "grammar_unavailable",
            )
            self.assertTrue(result.metadata.errors)
            self.assertEqual(asyncio.run(tool.get_symbol_definitions()), {})


@unittest.skipUnless(GRAMMARS, "Provision optional Python/TypeScript grammars")
class TreeSitterTests(unittest.TestCase):
    def test_sdk_parity_and_occurrence_identity(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "sample.py").write_text(
                "def repeat(): pass\ndef repeat(): pass\n"
            )
            sdk = analyze_repository(
                root, repository_id="fixture", languages=("python",)
            )
            tool = TreeSitterTool(Path(root))
            result = asyncio.run(tool.analyze("fixture", {"python"}))
            properties = [n.properties for n in result.nodes if "Symbol" in n.labels]
            self.assertEqual(
                [r.type for r in result.relationships],
                ["CONTAINS", "DEFINES", "DEFINES"],
            )
            by_name = {n.properties["qualified_name"]: n for n in result.nodes}
            for edge in result.relationships:
                self.assertIn(edge.source_match["qualified_name"], by_name)
                self.assertIn(edge.target_match["qualified_name"], by_name)
            file = next(n for n in result.nodes if "File" in n.labels)
            self.assertEqual(file.properties["path"], "sample.py")
            self.assertEqual(result.metadata.custom_metrics["model_tokens"], 0)
            self.assertEqual(
                [p["definition_id"] for p in properties],
                [d.id for d in sdk.definitions],
            )
            self.assertEqual(
                [p["start_line"] for p in properties], [d.line for d in sdk.definitions]
            )
            self.assertEqual(len({p["qualified_name"] for p in properties}), 2)
            self.assertEqual(
                result.metadata.custom_metrics["snapshot_id"], sdk.snapshot_id
            )
            self.assertEqual(
                result.metadata.custom_metrics["coverage"], sdk.to_dict()["coverage"]
            )
            definitions = asyncio.run(tool.get_symbol_definitions())
            definitions.clear()
            self.assertEqual(len(asyncio.run(tool.get_symbol_definitions())), 2)
            result.nodes[0].properties["name"] = "modified output"
            self.assertNotEqual(
                next(iter(asyncio.run(tool.get_symbol_definitions()).values()))["name"],
                "modified output",
            )

    def test_filters_typescript_and_partial_coverage(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "a.tsx").write_text("export function View() { return <p/>; }\n")
            (path / "skip.ts").write_text("export class Skip {}\n")
            (path / "bad.py").write_text("class Visible:\n    pass\nvalue =\n")
            tool = TreeSitterTool(path)
            filtered = asyncio.run(
                tool.analyze("fixture", {"typescript"}, ["*.tsx", "*.ts"], ["skip.ts"])
            )
            self.assertEqual(
                [n.properties["name"] for n in filtered.nodes if "Symbol" in n.labels],
                ["View"],
            )
            self.assertEqual(filtered.metadata.files_processed, 1)
            partial = asyncio.run(tool.analyze("fixture", {"python"}))
            self.assertEqual(
                partial.metadata.custom_metrics["analysis_status"], "partial"
            )
            self.assertTrue(partial.metadata.warnings)
            self.assertEqual(
                partial.metadata.custom_metrics["coverage"][0]["status"], "partial"
            )

    def test_failing_or_empty_reanalysis_clears_owned_definitions(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "a.py").write_text("class First: pass\n")
            tool = TreeSitterTool(path)
            asyncio.run(tool.analyze("fixture", {"python"}))
            self.assertTrue(asyncio.run(tool.get_symbol_definitions()))
            with self.assertRaises(ValueError):
                asyncio.run(tool.analyze("fixture", {"go"}))
            self.assertEqual(asyncio.run(tool.get_symbol_definitions()), {})
            asyncio.run(tool.analyze("fixture", {"python"}))
            (path / "a.py").unlink()
            empty = asyncio.run(tool.analyze("fixture", {"python"}))
            self.assertFalse([n for n in empty.nodes if "Symbol" in n.labels])
            self.assertEqual(asyncio.run(tool.get_symbol_definitions()), {})
            asyncio.run(tool.cleanup())
            self.assertEqual(asyncio.run(tool.get_symbol_definitions()), {})

    def test_source_failures_remain_visible_and_clear_previous_symbols(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            source = path / "sample.py"
            source.write_text("class PreviouslyVisible: pass\n")
            tool = TreeSitterTool(path)
            asyncio.run(tool.analyze("fixture", {"python"}))
            source.write_bytes(b"\xff\xfe")
            failed = asyncio.run(tool.analyze("fixture", {"python"}))
            self.assertEqual(
                failed.metadata.custom_metrics["analysis_status"], "failed"
            )
            self.assertFalse([n for n in failed.nodes if "Symbol" in n.labels])
            self.assertEqual(asyncio.run(tool.get_symbol_definitions()), {})
            self.assertEqual(
                failed.metadata.custom_metrics["diagnostics"][0]["code"],
                "source_unreadable",
            )
            source.write_text("class Current: pass\n")
            (path / "linked.py").symlink_to(source)
            partial = asyncio.run(tool.analyze("fixture", {"python"}))
            self.assertEqual(
                partial.metadata.custom_metrics["analysis_status"], "partial"
            )
            self.assertEqual(
                [n.properties["name"] for n in partial.nodes if "Symbol" in n.labels],
                ["Current"],
            )
            self.assertTrue(partial.metadata.warnings)
            self.assertIn(
                "failed",
                {c["status"] for c in partial.metadata.custom_metrics["coverage"]},
            )

    def test_registry_instances_do_not_leak_cross_repository_state(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            (Path(a) / "sample.py").write_text("class First: pass\n")
            (Path(b) / "sample.py").write_text("class Second: pass\n")
            first = tool_registry.get_tool("tree-sitter", Path(a))
            second = tool_registry.get_tool("tree-sitter", Path(b))
            asyncio.run(first.analyze("first", {"python"}))
            asyncio.run(second.analyze("second", {"python"}))
            self.assertEqual(
                [
                    p["name"]
                    for p in asyncio.run(first.get_symbol_definitions()).values()
                ],
                ["First"],
            )
            self.assertEqual(
                [
                    p["name"]
                    for p in asyncio.run(second.get_symbol_definitions()).values()
                ],
                ["Second"],
            )


if __name__ == "__main__":
    unittest.main()
