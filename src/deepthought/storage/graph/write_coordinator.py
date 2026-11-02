"""Port of WriteCoordinator: shared multi-tool graph and truthful write results.

Ordered primary/supplemental contributions retain the original intelligent merge
rules. One explicit graph revision is published atomically, adapting the legacy
incremental write lifecycle without inventing isolated per-tool graphs. The caller
attests that contributions belong together by selecting a graph revision; their
original source snapshots and semantic metadata remain recorded separately.
"""

from copy import deepcopy
from dataclasses import dataclass, field
import json

from ...analysis_tools.contracts import (
    NodeData,
    StandardizedOutput,
    ToolExecutionMetadata,
)
from .writer import BatchConfig, GraphWriter, WriteStats


@dataclass
class WriteCoordinatorConfig:
    batch_config: BatchConfig | None = None
    array_properties: tuple[str, ...] = (
        "analyzed_by",
        "tools_processed",
        "tags",
        "annotations",
    )
    prefer_longer_properties: tuple[str, ...] = (
        "description",
        "docstring",
        "comment",
        "documentation",
    )


@dataclass
class WriteResult:
    tool_name: str
    success: bool
    stats: WriteStats
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    snapshot_id: str | None = None
    contributions: list[dict] = field(default_factory=list)


def _semantic(metadata):
    value = metadata.to_dict()
    value.pop("timestamp", None)
    value.pop("execution_time_seconds", None)
    return value


def _unique(values):
    result = []
    seen = set()
    for value in values:
        key = json.dumps(value, sort_keys=True, allow_nan=False)
        if key not in seen:
            seen.add(key)
            result.append(deepcopy(value))
    return result


def _merge_properties(previous, incoming, config):
    """Original intelligent arrays/longer documentation/type and overwrite rules."""
    merged = deepcopy(previous)
    for key, value in incoming.items():
        if key in config.array_properties and isinstance(value, list):
            existing = merged.get(key)
            merged[key] = _unique(
                (existing if isinstance(existing, list) else []) + value
            )
        elif key in config.prefer_longer_properties:
            existing = merged.get(key)
            if isinstance(value, str) and len(value) > len(
                existing if isinstance(existing, str) else ""
            ):
                merged[key] = value
            elif key not in merged:
                merged[key] = deepcopy(value)
        elif key in {"type_annotation", "return_type"}:
            if value is not None or key not in merged:
                merged[key] = deepcopy(value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _combine(outputs, repo_url, snapshot_id, config):
    if not outputs:
        raise ValueError("At least one AnalysisTool output is required")
    names = [output.metadata.tool_name for output in outputs]
    if len(names) != len(set(names)):
        raise ValueError("Each tool may contribute once per graph revision")
    for output in outputs:
        if (
            output.metadata.custom_metrics.get("analysis_status") != "succeeded"
            or output.metadata.errors
        ):
            raise ValueError(
                "Only successful complete analysis may be published; no contribution was imported"
            )
        source = output.metadata.custom_metrics.get("snapshot_id")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("Each tool contribution needs its original snapshot_id")
    if len(outputs) > 1 and snapshot_id is None:
        raise ValueError(
            "Multi-tool graph publication requires an explicit snapshot_id graph revision"
        )
    revision = (
        snapshot_id
        if snapshot_id is not None
        else outputs[0].metadata.custom_metrics["snapshot_id"]
    )
    if not isinstance(revision, str) or not revision.strip():
        raise ValueError("Graph revision snapshot_id must be nonempty")
    # Single output follows the same batch admission path without changing its semantic fingerprint.
    if len(outputs) == 1 and snapshot_id is None:
        return deepcopy(outputs[0]), revision
    nodes, edges = {}, {}
    for output in outputs:
        source_snapshot = output.metadata.custom_metrics["snapshot_id"]
        for node in output.nodes:
            props = deepcopy(node.properties)
            if props.get("repo_url", repo_url) != repo_url:
                raise ValueError("Node repository differs from selected import")
            if props.get("snapshot_id", source_snapshot) not in {
                source_snapshot,
                revision,
            }:
                raise ValueError("Node snapshot differs from its tool contribution")
            props.update(repo_url=repo_url, snapshot_id=revision)
            qname = props["qualified_name"]
            if qname in nodes:
                previous = nodes[qname]
                nodes[qname] = NodeData(
                    sorted(set(previous.labels) | set(node.labels)),
                    _merge_properties(previous.properties, props, config),
                )
            else:
                nodes[qname] = NodeData(deepcopy(node.labels), props)
        for relationship in output.relationships:
            rel = deepcopy(relationship)
            for match in (rel.source_match, rel.target_match):
                if match.get("repo_url", repo_url) != repo_url:
                    raise ValueError(
                        "Relationship repository differs from selected import"
                    )
                if match.get("snapshot_id", source_snapshot) not in {
                    source_snapshot,
                    revision,
                }:
                    raise ValueError(
                        "Relationship snapshot differs from its tool contribution"
                    )
                match.update(repo_url=repo_url, snapshot_id=revision)
            # Exact cross-tool duplicate relationships are one shared fact, not parallel edges.
            key = json.dumps(
                [
                    rel.type,
                    rel.source_match["qualified_name"],
                    rel.target_match["qualified_name"],
                    rel.properties,
                ],
                sort_keys=True,
                allow_nan=False,
            )
            if key in edges:
                previous = edges[key]
                for earlier, later in (
                    (previous.source_match, rel.source_match),
                    (previous.target_match, rel.target_match),
                ):
                    for field, value in later.items():
                        if field in earlier and earlier[field] != value:
                            raise ValueError(
                                "Conflicting relationship endpoint constraints"
                            )
                        earlier[field] = deepcopy(value)
                previous.source_labels = sorted(
                    set(previous.source_labels) | set(rel.source_labels)
                )
                previous.target_labels = sorted(
                    set(previous.target_labels) | set(rel.target_labels)
                )
                previous.create_missing_nodes = (
                    previous.create_missing_nodes and rel.create_missing_nodes
                )
            else:
                edges[key] = rel
    contributions = [_semantic(output.metadata) for output in outputs]
    capabilities = sorted(
        {
            capability
            for output in outputs
            for capability in output.metadata.custom_metrics.get("capabilities", [])
        }
    )
    metadata = ToolExecutionMetadata(
        tool_name="+".join(names),
        tool_version="multi-tool-graph-v1",
        execution_time_seconds=sum(
            output.metadata.execution_time_seconds for output in outputs
        ),
        languages_processed=sorted(
            {lang for output in outputs for lang in output.metadata.languages_processed}
        ),
        files_processed=sum("File" in node.labels for node in nodes.values()),
        warnings=[
            warning for output in outputs for warning in output.metadata.warnings
        ],
        custom_metrics={
            "snapshot_id": revision,
            "analysis_status": "succeeded",
            "capabilities": capabilities,
            "tool_outputs": contributions,
            "source_binding": "caller_selected_graph_revision",
            "model_tokens": sum(
                output.metadata.custom_metrics["model_tokens"] for output in outputs
            )
            if all(
                type(output.metadata.custom_metrics.get("model_tokens")) is int
                and output.metadata.custom_metrics["model_tokens"] >= 0
                for output in outputs
            )
            else None,
        },
    )
    return StandardizedOutput(
        list(nodes.values()), list(edges.values()), metadata
    ), revision


class WriteCoordinator:
    def __init__(self, neo4j_driver, repo_path=None, config=None, *, database="neo4j"):
        self.neo4j_driver = neo4j_driver
        self.repo_path = repo_path
        self.config = config or WriteCoordinatorConfig()
        self.graph_writer = GraphWriter(
            neo4j_driver, self.config.batch_config, database=database
        )
        self._write_history = []

    async def write_tool_output(self, tool_name, output, repo_url):
        """Compatibility entry point; single contributions use batch publication."""
        if output.metadata.tool_name != tool_name:
            stats = WriteStats(
                errors=["Tool identity differs from output metadata"],
                snapshot_id=output.metadata.custom_metrics.get("snapshot_id"),
            )
            result = WriteResult(
                tool_name,
                False,
                stats,
                list(stats.errors),
                snapshot_id=stats.snapshot_id,
            )
            self._write_history.append(result)
            return result
        return await self.write_outputs([output], repo_url)

    async def write_outputs(self, outputs, repo_url, *, snapshot_id=None):
        """Atomically publish ordered primary/supplemental outputs as one graph.

        Multi-tool callers must provide a graph revision; per-tool source snapshots
        remain metadata, not separate graphs. Any failed/partial contribution or
        unresolved endpoint rejects the whole publication. No tools are skipped.
        """
        outputs = list(outputs)
        revision = snapshot_id
        name = "+".join(output.metadata.tool_name for output in outputs)
        try:
            merged, revision = _combine(outputs, repo_url, snapshot_id, self.config)
            stats = await self.graph_writer.write(
                merged, repo_url=repo_url, snapshot_id=revision
            )
        except (ValueError, TypeError, KeyError) as exc:
            stats = WriteStats(errors=[str(exc)], snapshot_id=revision)
        result = WriteResult(
            name,
            not stats.errors,
            stats,
            list(stats.errors),
            [warning for output in outputs for warning in output.metadata.warnings],
            revision,
            [output.metadata.to_dict() for output in outputs],
        )
        self._write_history.append(result)
        return result

    def get_total_stats(self):
        return {
            "nodes_created": sum(
                r.stats.nodes_created for r in self._write_history if r.success
            ),
            "relationships_created": sum(
                r.stats.relationships_created for r in self._write_history if r.success
            ),
            "successful_imports": sum(r.success for r in self._write_history),
            "failed_imports": sum(not r.success for r in self._write_history),
        }
