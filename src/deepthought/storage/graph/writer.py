"""Port of the original GraphWriter batch persistence and standardized records.

Retained: tool-agnostic node/relationship batches, parameterized UNWIND writes,
label grouping, endpoint identity checks and WriteStats. Adapted: snapshot-scoped
identity replaces global internal-ID caches; one managed transaction publishes a
complete immutable snapshot or rolls back. Cross-analysis property guessing,
APOC merge policies and create-only duplication are deliberately not reproduced.
"""

import json
import re
import time
from dataclasses import dataclass, field
from hashlib import sha256

from .encoding import encode_properties


@dataclass
class WriteStats:
    nodes_created: int = 0
    nodes_updated: int = 0
    relationships_created: int = 0
    relationships_updated: int = 0
    errors: list[str] = field(default_factory=list)
    batch_count: int = 0
    total_time_seconds: float = 0.0
    snapshot_id: str | None = None
    reused: bool = False


@dataclass
class BatchConfig:
    node_batch_size: int = 5000
    relationship_batch_size: int = 10000
    transaction_timeout_seconds: float = 60

    def __post_init__(self):
        for value in (self.node_batch_size, self.relationship_batch_size):
            if type(value) is not int or not 1 <= value <= 100_000:
                raise ValueError("Batch sizes must be integers from 1 to 100000")
        if type(self.transaction_timeout_seconds) not in (int, float) or not (
            0 < self.transaction_timeout_seconds <= 3600
        ):
            raise ValueError("Transaction timeout must be positive and <=3600 seconds")


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("Graph labels/types must be simple Cypher identifiers")
    return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _prepare(output, repo_url, snapshot_id):
    """Validate the complete import before any writes; never guess missing edges."""
    if not isinstance(repo_url, str) or not repo_url.strip():
        raise ValueError("Repository identity is required")
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ValueError("Tool metadata must include a nonempty snapshot_id")
    nodes = {}
    for node in output.nodes:
        labels = sorted({identifier(label) for label in node.labels} | {"GraphNode"})
        if "AnalysisSnapshot" in labels:
            raise ValueError(
                "AnalysisSnapshot is reserved for storage publication markers"
            )
        props = dict(node.properties)
        if props.get("repo_url", repo_url) != repo_url:
            raise ValueError("Node repository differs from selected import")
        if props.get("snapshot_id", snapshot_id) != snapshot_id:
            raise ValueError("Node snapshot differs from selected import")
        props.update(repo_url=repo_url, snapshot_id=snapshot_id)
        qname = props["qualified_name"]
        if qname in nodes:
            raise ValueError("Duplicate qualified_name within one snapshot")
        nodes[qname] = {"labels": labels, "properties": props}
    relationships = []
    seen = set()
    for rel in output.relationships:
        kind = identifier(rel.type)
        endpoints = []
        for match, labels in (
            (rel.source_match, rel.source_labels),
            (rel.target_match, rel.target_labels),
        ):
            qname = match["qualified_name"]
            if (
                match.get("repo_url", repo_url) != repo_url
                or match.get("snapshot_id", snapshot_id) != snapshot_id
            ):
                raise ValueError(
                    "Relationship endpoints must remain inside selected snapshot"
                )
            if qname not in nodes:
                if not rel.create_missing_nodes:
                    raise ValueError("Relationship endpoint is absent: " + qname)
                nodes[qname] = {
                    "labels": sorted({identifier(x) for x in labels} | {"GraphNode"}),
                    "properties": {
                        **match,
                        "repo_url": repo_url,
                        "snapshot_id": snapshot_id,
                    },
                }
            target = nodes[qname]
            if not set(labels) <= set(target["labels"]) or any(
                target["properties"].get(k) != v for k, v in match.items()
            ):
                raise ValueError("Relationship endpoint match does not match its node")
            endpoints.append(qname)
        props = dict(rel.properties)
        encoded = encode_properties(props)
        identity = _canonical([kind, endpoints, props])
        if identity in seen:
            raise ValueError("Duplicate relationship within snapshot")
        seen.add(identity)
        relationships.append(
            {
                "type": kind,
                "source": endpoints[0],
                "target": endpoints[1],
                "properties": encoded,
                "edge_id": sha256(identity.encode()).hexdigest(),
            }
        )
    metadata = output.metadata.to_dict()
    # Runtime measurements/timestamps do not alter the immutable semantic content.
    metadata.pop("timestamp", None)
    metadata.pop("execution_time_seconds", None)
    payload = {
        "nodes": sorted(
            nodes.values(), key=lambda x: x["properties"]["qualified_name"]
        ),
        "relationships": sorted(relationships, key=lambda x: x["edge_id"]),
        "metadata": metadata,
    }
    fingerprint = sha256(_canonical(payload).encode()).hexdigest()
    for node in payload["nodes"]:
        node["properties"] = encode_properties(node["properties"])
    return payload, fingerprint


class GraphWriter:
    def __init__(self, driver, config=None, *, database="neo4j"):
        self.driver = driver
        self.config = config or BatchConfig()
        self.database = database

    async def write(self, output, *, repo_url=None, snapshot_id=None):
        """Atomically store one snapshot; recorded errors always mean failure.

        The caller should use WriteCoordinator for repository/tool admission.
        Schema must be installed first with setup_schema().
        """
        started = time.monotonic()
        snapshot_id = snapshot_id or output.metadata.custom_metrics.get("snapshot_id")
        stats = WriteStats(snapshot_id=snapshot_id)
        try:
            if (
                output.metadata.custom_metrics.get("analysis_status") != "succeeded"
                or output.metadata.errors
            ):
                raise ValueError("Only successful complete analysis may be published")
            if repo_url is None:
                repos = {n.properties.get("repo_url") for n in output.nodes}
                if len(repos) != 1:
                    raise ValueError("Select exactly one repository per import")
                repo_url = repos.pop()
            data, fingerprint = _prepare(output, repo_url, snapshot_id)
            from neo4j import unit_of_work

            @unit_of_work(timeout=self.config.transaction_timeout_seconds)
            async def transaction(tx):
                # Return stats from the final successful retry only.
                attempt = WriteStats(snapshot_id=snapshot_id)
                result = await tx.run(
                    "MERGE (s:AnalysisSnapshot {repo_url:$repo_url,snapshot_id:$snapshot_id}) "
                    "ON CREATE SET s.status='writing',s.content_hash=$fingerprint "
                    "RETURN s.status AS status,s.content_hash AS content_hash",
                    repo_url=repo_url,
                    snapshot_id=snapshot_id,
                    fingerprint=fingerprint,
                )
                marker = await result.single(strict=True)
                if marker["content_hash"] != fingerprint:
                    raise ValueError(
                        "Snapshot identity already contains different analysis facts"
                    )
                if marker["status"] == "complete":
                    attempt.reused = True
                    return attempt
                if marker["status"] != "writing":
                    raise ValueError("Snapshot marker has invalid state")
                await self._write_nodes(tx, data["nodes"], attempt)
                await self._write_relationships(
                    tx, data["relationships"], repo_url, snapshot_id, attempt
                )
                result = await tx.run(
                    "MATCH (s:AnalysisSnapshot {repo_url:$repo_url,snapshot_id:$snapshot_id}) "
                    "SET s.status='complete',s.tool_name=$tool_name,s.metadata_json=$metadata_json,"
                    "s.node_count=$node_count,s.relationship_count=$relationship_count,s.completed_at=timestamp()",
                    repo_url=repo_url,
                    snapshot_id=snapshot_id,
                    tool_name=output.metadata.tool_name,
                    metadata_json=_canonical(data["metadata"]),
                    node_count=len(data["nodes"]),
                    relationship_count=len(data["relationships"]),
                )
                await result.consume()
                return attempt

            async with self.driver.session(database=self.database) as session:
                stats = await session.execute_write(transaction)
        except Exception as exc:  # noqa: BLE001 -- legacy WriteStats is the explicit failure result
            stats.errors.append(str(exc))
        stats.total_time_seconds = time.monotonic() - started
        return stats

    async def _write_nodes(self, tx, nodes, stats):
        groups = {}
        for node in nodes:
            groups.setdefault(tuple(node["labels"]), []).append(node)
        for labels, group in groups.items():
            for offset in range(0, len(group), self.config.node_batch_size):
                batch = group[offset : offset + self.config.node_batch_size]
                # Snapshot publication is atomic; existing complete content never enters this path.
                result = await tx.run(
                    "UNWIND $nodes AS node CREATE (n:"
                    + ":".join(labels)
                    + ") SET n=node.properties",
                    nodes=batch,
                )
                summary = await result.consume()
                stats.nodes_created += summary.counters.nodes_created
                stats.batch_count += 1

    async def _write_relationships(
        self, tx, relationships, repo_url, snapshot_id, stats
    ):
        groups = {}
        for rel in relationships:
            groups.setdefault(rel["type"], []).append(rel)
        for kind, group in groups.items():
            for offset in range(0, len(group), self.config.relationship_batch_size):
                batch = group[offset : offset + self.config.relationship_batch_size]
                result = await tx.run(
                    "UNWIND $relationships AS rel "
                    "MATCH (source:GraphNode {repo_url:$repo_url,snapshot_id:$snapshot_id,qualified_name:rel.source}) "
                    "MATCH (target:GraphNode {repo_url:$repo_url,snapshot_id:$snapshot_id,qualified_name:rel.target}) "
                    "CREATE (source)-[r:" + kind + "]->(target) SET r=rel.properties "
                    "SET r._dt_edge_id=rel.edge_id RETURN count(r) AS written",
                    relationships=batch,
                    repo_url=repo_url,
                    snapshot_id=snapshot_id,
                )
                count = (await result.single(strict=True))["written"]
                if count != len(batch):
                    raise ValueError(
                        "Relationship write did not resolve every endpoint"
                    )
                await result.consume()
                stats.relationships_created += count
                stats.batch_count += 1
