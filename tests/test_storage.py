"""Persistence boundary controls; real database acceptance is in test_storage_integration."""

import asyncio
import unittest
from copy import deepcopy

from deepthought.analysis_tools.contracts import (
    NodeData,
    RelationshipData,
    StandardizedOutput,
    ToolExecutionMetadata,
)
from deepthought.storage.graph import BatchConfig, WriteCoordinator
from deepthought.storage.graph.encoding import decode_properties, encode_properties
from deepthought.storage.graph.writer import _prepare


def fixture(snapshot="s1", repo="https://github.com/example/public"):
    return StandardizedOutput(
        nodes=[
            NodeData(
                ["Function", "Symbol"],
                {
                    "qualified_name": "sample.run",
                    "repo_url": repo,
                    "name": "run",
                    "nested": {"nullable": None},
                    "start_line": 1,
                },
            )
        ],
        relationships=[],
        metadata=ToolExecutionMetadata(
            "fixture",
            "1",
            0,
            custom_metrics={
                "snapshot_id": snapshot,
                "analysis_status": "succeeded",
                "model_tokens": 0,
            },
        ),
    )


class PropertyEncodingTests(unittest.TestCase):
    def test_nested_json_null_empty_heterogeneous_and_large_integer_roundtrip(self):
        props = {
            "text": "raw",
            "count": 5,
            "flag": True,
            "empty": [],
            "null": None,
            "mixed": [1, "1", None],
            "nested": {"x": [None, {"y": 3}]},
            "big": 2**80,
            "array": [1, 2],
            "float": 0.5,
        }
        self.assertEqual(decode_properties(encode_properties(props)), props)
        self.assertEqual(encode_properties(props)["text"], "raw")
        self.assertEqual(encode_properties(props)["array"], [1, 2])
        self.assertEqual(
            decode_properties({"x": 1, "_dt_edge_id": "internal"}), {"x": 1}
        )

    def test_reserved_metadata_and_nonfinite_values_rejected(self):
        for props in (
            {"_dt_json_fields": []},
            {"x": float("nan")},
            {"x": [float("inf")]},
        ):
            with self.subTest(props=props), self.assertRaises(ValueError):
                encode_properties(props)

    def test_property_order_does_not_change_storage_encoding(self):
        a = {"z": {}, "a": None, "b": [None]}
        self.assertEqual(
            encode_properties(a), encode_properties(dict(reversed(list(a.items()))))
        )


class StorageAdmissionTests(unittest.TestCase):
    def test_runtime_metadata_does_not_change_immutable_content(self):
        one = fixture()
        two = fixture()
        two.metadata.execution_time_seconds = 42
        a, ah = _prepare(one, one.nodes[0].properties["repo_url"], "s1")
        b, bh = _prepare(two, two.nodes[0].properties["repo_url"], "s1")
        self.assertEqual(ah, bh)
        self.assertEqual(a, b)
        two.nodes[0].properties["start_line"] = 9
        self.assertNotEqual(
            ah, _prepare(two, two.nodes[0].properties["repo_url"], "s1")[1]
        )

    def test_cross_repository_snapshot_duplicate_and_dangling_rejected(self):
        output = fixture()
        repo = output.nodes[0].properties["repo_url"]
        with self.assertRaisesRegex(ValueError, "repository"):
            _prepare(output, "other", "s1")
        output.nodes[0].properties["snapshot_id"] = "other"
        with self.assertRaisesRegex(ValueError, "snapshot"):
            _prepare(output, repo, "s1")
        output = fixture()
        output.nodes.append(deepcopy(output.nodes[0]))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            _prepare(output, repo, "s1")
        output = fixture()
        output.relationships.append(
            RelationshipData(
                "CALLS",
                ["Symbol"],
                {"qualified_name": "sample.run"},
                ["Symbol"],
                {"qualified_name": "absent"},
            )
        )
        with self.assertRaisesRegex(ValueError, "absent"):
            _prepare(output, repo, "s1")

    def test_explicit_missing_node_contract_creates_endpoint_record(self):
        output = fixture()
        repo = output.nodes[0].properties["repo_url"]
        output.relationships.append(
            RelationshipData(
                "CALLS",
                ["Symbol"],
                {"qualified_name": "sample.run"},
                ["Symbol"],
                {"qualified_name": "external"},
                create_missing_nodes=True,
            )
        )
        data, _ = _prepare(output, repo, "s1")
        self.assertEqual(len(data["nodes"]), 2)
        self.assertEqual(len(data["relationships"]), 1)

    def test_illegal_identifier_and_marker_labels_rejected(self):
        output = fixture()
        repo = output.nodes[0].properties["repo_url"]
        for label in ("Bad`) MATCH (n) DELETE n //", "AnalysisSnapshot"):
            output.nodes[0].labels = [label]
            with self.subTest(label=label), self.assertRaises(ValueError):
                _prepare(output, repo, "s1")

    def test_failed_partial_and_misidentified_tools_never_write(self):
        class Driver:
            def session(self, **kwargs):
                raise AssertionError("Rejected analysis must not touch database")

        for mode in ("failed", "partial", "unknown", "mismatch"):
            output = fixture()
            output.metadata.custom_metrics["analysis_status"] = (
                mode if mode != "mismatch" else "succeeded"
            )
            result = asyncio.run(
                WriteCoordinator(Driver()).write_tool_output(
                    "wrong" if mode == "mismatch" else "fixture",
                    output,
                    output.nodes[0].properties["repo_url"],
                )
            )
            self.assertFalse(result.success)
            self.assertTrue(result.errors)

    def test_driver_error_is_an_unsuccessful_write_not_false_success(self):
        class Driver:
            def session(self, **kwargs):
                raise RuntimeError("database unavailable")

        output = fixture()
        # Without optional driver installed failure is still explicit, never a successful write.
        result = asyncio.run(
            WriteCoordinator(Driver()).write_tool_output(
                "fixture", output, output.nodes[0].properties["repo_url"]
            )
        )
        self.assertFalse(result.success)
        self.assertTrue(result.errors)
        self.assertEqual(result.stats.nodes_created, 0)

    def test_multi_tool_requires_explicit_revision_and_never_silently_skips_tools(self):
        class Driver:
            def session(self, **kwargs):
                raise AssertionError("Rejected batch must not touch database")

        primary = fixture()
        secondary = fixture()
        secondary.metadata.tool_name = "second"
        coordinator = WriteCoordinator(Driver())
        result = asyncio.run(
            coordinator.write_outputs(
                [primary, secondary], primary.nodes[0].properties["repo_url"]
            )
        )
        self.assertFalse(result.success)
        self.assertIn("explicit snapshot_id", result.errors[0])
        secondary.metadata.custom_metrics["analysis_status"] = "partial"
        result = asyncio.run(
            coordinator.write_outputs(
                [primary, secondary],
                primary.nodes[0].properties["repo_url"],
                snapshot_id="graph-v1",
            )
        )
        self.assertFalse(result.success)
        self.assertIn("no contribution was imported", result.errors[0])

    def test_batch_settings_reject_invalid_sizes_and_timeouts(self):
        for kwargs in (
            {"node_batch_size": True},
            {"relationship_batch_size": 0},
            {"transaction_timeout_seconds": float("inf")},
            {"transaction_timeout_seconds": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                BatchConfig(**kwargs)


if __name__ == "__main__":
    unittest.main()
