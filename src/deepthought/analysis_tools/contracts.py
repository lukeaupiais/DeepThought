"""Portable AnalysisTool records, adapted from the original MIT contracts.

See licenses/deepthought-original-MIT.txt. No database driver or graph writer is
required. Source locations and coverage belong to the producing tool's metadata.
"""

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _text(value, field_name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a nonempty string")


def _json(value):
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("Records must contain finite JSON-compatible data") from exc


def _labels(values):
    if not isinstance(values, list) or not values:
        raise ValueError("At least one label is required")
    for value in values:
        _text(value, "label")


@dataclass
class NodeData:
    labels: list[str]
    properties: dict[str, Any]
    match_keys: list[str] = field(
        default_factory=lambda: ["qualified_name", "repo_url"]
    )

    def __post_init__(self):
        _labels(self.labels)
        if not isinstance(self.properties, dict):
            raise TypeError("Node properties must be a dictionary")
        _text(self.properties.get("qualified_name"), "qualified_name")
        if "repo_url" in self.properties:
            _text(self.properties["repo_url"], "repo_url")
        if (
            not isinstance(self.match_keys, list)
            or "qualified_name" not in self.match_keys
        ):
            raise ValueError("Node match keys must include qualified_name")
        for key in self.match_keys:
            _text(key, "match key")
            if key not in self.properties:
                raise ValueError("Every match key requires a property")
        _json(self.properties)

    def to_cypher_map(self):
        """Legacy data-shape compatibility; does not execute a database query."""
        return asdict(self)


@dataclass
class RelationshipData:
    type: str
    source_labels: list[str]
    source_match: dict[str, Any]
    target_labels: list[str]
    target_match: dict[str, Any]
    properties: dict[str, Any] = field(default_factory=dict)
    create_missing_nodes: bool = False

    def __post_init__(self):
        _text(self.type, "relationship type")
        _labels(self.source_labels)
        _labels(self.target_labels)
        for match in (self.source_match, self.target_match):
            if not isinstance(match, dict):
                raise TypeError("Relationship matches must be dictionaries")
            _text(match.get("qualified_name"), "endpoint qualified_name")
            _json(match)
        if not isinstance(self.properties, dict):
            raise TypeError("Relationship properties must be a dictionary")
        if type(self.create_missing_nodes) is not bool:
            raise ValueError("create_missing_nodes must be boolean")
        _json(self.properties)


@dataclass
class ToolExecutionMetadata:
    tool_name: str
    tool_version: str
    execution_time_seconds: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    languages_processed: list[str] = field(default_factory=list)
    files_processed: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    custom_metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        _text(self.tool_name, "tool_name")
        _text(self.tool_version, "tool_version")
        if (
            type(self.execution_time_seconds) not in (int, float)
            or not math.isfinite(self.execution_time_seconds)
            or self.execution_time_seconds < 0
        ):
            raise ValueError("Execution time must be finite and nonnegative")
        if type(self.files_processed) is not int or self.files_processed < 0:
            raise ValueError("File count must be a nonnegative integer")
        if not isinstance(self.timestamp, datetime):
            raise TypeError("Timestamp must be a datetime")
        for values in (self.languages_processed, self.errors, self.warnings):
            if not isinstance(values, list) or any(
                not isinstance(v, str) for v in values
            ):
                raise ValueError("Metadata lists must contain strings")
        if not isinstance(self.custom_metrics, dict):
            raise TypeError("Custom metrics must be a dictionary")
        _json(self.custom_metrics)

    def to_dict(self):
        result = asdict(self)
        result["timestamp"] = self.timestamp.isoformat()
        return result


@dataclass
class StandardizedOutput:
    nodes: list[NodeData]
    relationships: list[RelationshipData]
    metadata: ToolExecutionMetadata

    def __post_init__(self):
        if not isinstance(self.nodes, list) or any(
            not isinstance(n, NodeData) for n in self.nodes
        ):
            raise ValueError("nodes must be NodeData records")
        if not isinstance(self.relationships, list) or any(
            not isinstance(r, RelationshipData) for r in self.relationships
        ):
            raise ValueError("relationships must be RelationshipData records")
        if not isinstance(self.metadata, ToolExecutionMetadata):
            raise TypeError("metadata must be ToolExecutionMetadata")
        keys = [
            (n.properties.get("repo_url"), n.properties["qualified_name"])
            for n in self.nodes
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate qualified node identities")

    def to_dict(self):
        return {
            "nodes": [asdict(n) for n in self.nodes],
            "relationships": [asdict(r) for r in self.relationships],
            "metadata": self.metadata.to_dict(),
        }

    def to_json(self):
        return json.dumps(
            self.to_dict(), sort_keys=True, ensure_ascii=False, allow_nan=False
        )

    def get_statistics(self):
        node_types, relationship_types = {}, {}
        for node in self.nodes:
            for label in node.labels:
                node_types[label] = node_types.get(label, 0) + 1
        for relationship in self.relationships:
            relationship_types[relationship.type] = (
                relationship_types.get(relationship.type, 0) + 1
            )
        return {
            "total_nodes": len(self.nodes),
            "total_relationships": len(self.relationships),
            "node_types": node_types,
            "relationship_types": relationship_types,
            "errors": len(self.metadata.errors),
            "warnings": len(self.metadata.warnings),
        }
