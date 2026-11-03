"""Verified Neo4j handoff and query evidence; no agent/provider launcher.

The actual registered AnalysisTool, shared writer and production query service
remain the only extraction/storage/query implementations. Receipts bind observed
reads to the prepared graph; they are not signed remote attestations.
"""

import asyncio
from copy import deepcopy
from datetime import datetime
import json
import math
import re
from pathlib import Path
from threading import Lock
import time
from uuid import uuid4

from . import _digest, prepare


INTERFACE = "deepthought.graph_queries.GraphQueryService"


def _output(data):
    from ..analysis_tools import (
        NodeData,
        RelationshipData,
        StandardizedOutput,
        ToolExecutionMetadata,
    )

    metadata = deepcopy(data["metadata"])
    metadata["timestamp"] = datetime.fromisoformat(metadata["timestamp"])
    return StandardizedOutput(
        [NodeData(**row) for row in data["nodes"]],
        [RelationshipData(**row) for row in data["relationships"]],
        ToolExecutionMetadata(**metadata),
    )


def prepare_neo4j(
    scenario, workspace, *, tool_access=None, settings=None, registry=None
):
    """Extract once, import through the shared coordinator and verify DB readback.

    settings contains explicit connection configuration or comes from the normal
    DEEPTHOUGHT_NEO4J_* environment. Credentials never enter the handoff/receipt.
    Caller supplies the actual agent transport and isolates the private oracle.
    """
    from ..graph_cli import connection_settings

    settings = dict(connection_settings() if settings is None else settings)
    if settings.get("database", "neo4j") == "system":
        raise ValueError("The system database is not an evaluation graph")
    access = (
        tool_access
        if tool_access is not None
        else {
            "command": "deepthought-mcp",
            "help": "Use the configured DeepThought MCP tools searchCodeElements, "
            "getCodeContext and getFileStructure with the supplied repository_id and "
            "snapshot_id. queryGraph is optional for a dedicated snapshot database.",
        }
    )
    prepared = prepare(scenario, workspace, registry=registry, tool_access=access)
    output = _output(prepared["private_tool_output"])
    repo = prepared["scenario"]["source"].get(
        "repository", "synthetic://" + prepared["scenario"]["id"]
    )
    snapshot = output.metadata.custom_metrics.get("snapshot_id")

    async def store_and_verify():
        from ..storage.graph import (
            Neo4jConnectionManager,
            WriteCoordinator,
            setup_schema,
        )
        from ..storage.graph.writer import _prepare

        started = time.monotonic()
        expected, fingerprint = _prepare(output, repo, snapshot)
        manager = Neo4jConnectionManager(**settings)
        database = manager.database
        async with manager:
            async with manager.driver.session(database=database) as session:
                rows = await session.run(
                    "MATCH (n) WHERE n.repo_url IS NULL OR n.snapshot_id IS NULL "
                    "OR n.repo_url<>$repo_url OR n.snapshot_id<>$snapshot_id "
                    "RETURN count(n) AS other_nodes",
                    repo_url=repo,
                    snapshot_id=snapshot,
                )
                if (await rows.single(strict=True))["other_nodes"]:
                    raise ValueError(
                        "Neo4j evaluations require a dedicated repository/snapshot database"
                    )
            await setup_schema(manager.driver, database=database)
            result = await WriteCoordinator(
                manager.driver, database=database
            ).write_tool_output(output.metadata.tool_name, output, repo)
            if not result.success:
                raise ValueError(
                    "Neo4j evaluation import failed: " + "; ".join(result.errors)
                )
            scope = {"repo_url": repo, "snapshot_id": snapshot}
            async with manager.driver.session(database=database) as session:
                tx = await session.begin_transaction(timeout=30)
                try:
                    rows = await tx.run(
                        "MATCH (s:AnalysisSnapshot {repo_url:$repo_url,snapshot_id:$snapshot_id,status:'complete'}) RETURN s.content_hash AS hash",
                        scope,
                    )
                    marker = await rows.single(strict=True)
                    if marker["hash"] != fingerprint:
                        raise ValueError(
                            "Neo4j publication does not match extracted facts"
                        )
                    rows = await tx.run(
                        "MATCH (n:GraphNode {repo_url:$repo_url,snapshot_id:$snapshot_id}) RETURN labels(n) AS labels, properties(n) AS properties",
                        scope,
                    )
                    actual_nodes = [
                        {
                            "labels": sorted(row["labels"]),
                            "properties": dict(row["properties"]),
                        }
                        async for row in rows
                    ]
                    rows = await tx.run(
                        "MATCH (a:GraphNode {repo_url:$repo_url,snapshot_id:$snapshot_id})-[r]->(b) RETURN a.qualified_name AS source,b.qualified_name AS target,type(r) AS type,properties(r) AS properties,b.repo_url AS target_repo,b.snapshot_id AS target_snapshot",
                        scope,
                    )
                    actual_edges = [dict(row) async for row in rows]
                    for row in actual_edges:
                        if (
                            row.pop("target_repo") != repo
                            or row.pop("target_snapshot") != snapshot
                        ):
                            raise ValueError(
                                "Neo4j relationship escapes the prepared graph"
                            )
                finally:
                    await tx.rollback()
            expected_edges = [
                {
                    "source": row["source"],
                    "target": row["target"],
                    "type": row["type"],
                    "properties": {**row["properties"], "_dt_edge_id": row["edge_id"]},
                }
                for row in expected["relationships"]
            ]
            if sorted(map(_digest, actual_nodes)) != sorted(
                map(_digest, expected["nodes"])
            ):
                raise ValueError("Neo4j node readback differs from extracted facts")
            if sorted(map(_digest, actual_edges)) != sorted(
                map(_digest, expected_edges)
            ):
                raise ValueError(
                    "Neo4j relationship readback differs from extracted facts"
                )
        return {
            "backend": "neo4j",
            "interface": INTERFACE,
            "database": database,
            "repo_url": repo,
            "snapshot_id": snapshot,
            "content_hash": fingerprint,
            "nodes": len(actual_nodes),
            "relationships": len(actual_edges),
            "verified": True,
            "wall_seconds": time.monotonic() - started,
        }

    receipt = asyncio.run(store_and_verify())
    prepared["neo4j"] = receipt
    access = prepared["contexts"]["treatment"]["tool_access"]
    access.update(
        {
            "backend": "neo4j",
            "interface": INTERFACE,
            "database": receipt["database"],
            "repository_id": repo,
            "snapshot_id": snapshot,
        }
    )
    provenance = prepared["provenance"]
    provenance["evaluation_interface"] = "neo4j"
    provenance["neo4j_sha256"] = _digest(receipt)
    provenance["tool_access_sha256"] = _digest(access)
    provenance["actor_context_sha256"]["treatment"] = _digest(
        prepared["contexts"]["treatment"]
    )
    prepared["input_id"] = _digest(provenance)
    prepared["preparation"]["neo4j_wall_seconds"] = receipt["wall_seconds"]
    return prepared


class QueryRecorder:
    """Observer for the real GraphQueryService, configured outside actor control.

    Use a fresh run_id and an evaluator-owned output path per session. Store this
    file outside the actor namespace. JSON hashes detect changes, not forgery by
    a privileged caller; independent native/server-log review remains necessary.
    """

    def __init__(self, input_id, run_id, path=None):
        if not all(isinstance(v, str) and v.strip() for v in (input_id, run_id)):
            raise ValueError("Query evidence needs preparation and run identities")
        self.input_id, self.run_id = input_id, run_id
        self.path = Path(path) if path is not None else None
        self.events = []
        self.lock = Lock()
        if self.path is not None:
            # Refuse reuse: receipts from a previous actor cannot silently accumulate.
            with self.path.open("x"):
                pass

    def __call__(self, event):
        event = deepcopy(event)
        event.update(
            input_id=self.input_id,
            run_id=self.run_id,
            schema_version=1,
            call_id=uuid4().hex,
        )
        event["event_id"] = _digest(event)
        with self.lock:
            if self.path is not None:
                with self.path.open("a") as stream:
                    stream.write(
                        json.dumps(event, sort_keys=True, allow_nan=False) + "\n"
                    )
            self.events.append(event)


def query_recorder_from_environment():
    """Opt-in instrumentation shared by the existing CLI and MCP adapters."""
    import os

    keys = (
        "DEEPTHOUGHT_EVAL_INPUT_ID",
        "DEEPTHOUGHT_EVAL_RUN_ID",
        "DEEPTHOUGHT_EVAL_QUERY_LOG",
    )
    values = [os.environ.get(key) for key in keys]
    if not any(values):
        return None
    if not all(values):
        raise ValueError("Set all three DEEPTHOUGHT_EVAL_* query audit settings")
    # Separate CLI processes may serve one actor. The parent owns/creates the log;
    # an observer instance never imports any engine facts or grading labels.
    recorder = QueryRecorder(values[0], values[1])
    recorder.path = Path(values[2])
    if not recorder.path.is_file() or recorder.path.is_symlink():
        raise ValueError("Evaluator must create a regular query log before agent use")
    return recorder


def assess_graph_evidence(record):
    """Recompute interface eligibility; never trust a caller's acceptance flag."""
    profile = record["provenance"].get("evaluation_interface")
    if profile != "neo4j":
        return {
            "status": "unverified_graph_interface"
            if record["provenance"].get("delivery_mode") == "tool_access"
            else "raw_context_control",
            "successful_calls": 0,
        }
    graph = record.get("neo4j")
    if not isinstance(graph, dict) or _digest(graph) != record["provenance"].get(
        "neo4j_sha256"
    ):
        raise ValueError("Neo4j readback identity mismatch")
    if (
        graph.get("verified") is not True
        or graph.get("backend") != "neo4j"
        or graph.get("interface") != INTERFACE
    ):
        raise ValueError("A verified Neo4j graph preparation is required")
    run_id = record.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        return {"status": "unverified_graph_run", "successful_calls": 0}
    events = record.get("graph_queries")
    if events is None or events == []:
        return {"status": "graph_not_used_or_unverified", "successful_calls": 0}
    if not isinstance(events, list):
        raise ValueError("Graph query evidence must be a list or unknown")
    seen = set()
    for original in events:
        if not isinstance(original, dict):
            raise ValueError("Invalid graph query receipt")
        event = deepcopy(original)
        claimed = event.pop("event_id", None)
        if claimed != _digest(event):
            raise ValueError("Graph query receipt identity mismatch")
        if claimed in seen:
            return {"status": "replayed_graph_evidence", "successful_calls": 0}
        seen.add(claimed)
        if event.get("input_id") != record["input_id"] or event.get("run_id") != run_id:
            return {"status": "wrong_graph_run", "successful_calls": 0}
        if any(
            event.get(k) != graph.get(k)
            for k in (
                "backend",
                "interface",
                "database",
                "repo_url",
                "snapshot_id",
                "content_hash",
            )
        ):
            return {"status": "wrong_graph", "successful_calls": 0}
        if (
            type(event.get("schema_version")) is not int
            or event["schema_version"] != 1
            or event.get("status") != "succeeded"
        ):
            return {"status": "graph_query_failed", "successful_calls": 0}
        for key in ("response_bytes", "returned"):
            if type(event.get(key)) is not int or event[key] < 0:
                raise ValueError("Graph query counts must be nonnegative integers")
        if type(event.get("truncated")) is not bool or event["response_bytes"] == 0:
            raise ValueError(
                "Graph response evidence needs nonzero bytes and a boolean truncation flag"
            )
        for key in ("query_sha256", "response_sha256", "call_id"):
            if not isinstance(event.get(key), str) or not re.fullmatch(
                r"[a-f0-9]{32,64}", event[key]
            ):
                raise ValueError(
                    "Graph query evidence needs valid query/response/call identities"
                )
        wall = event.get("wall_seconds")
        if type(wall) not in (int, float) or not math.isfinite(wall) or wall < 0:
            raise ValueError("Graph query time must be finite and nonnegative")
    return {
        "status": "verified_neo4j_access",
        "successful_calls": len(events),
        "response_bytes": sum(e["response_bytes"] for e in events),
        "query_wall_seconds": sum(e["wall_seconds"] for e in events),
        "receipt_provenance": "observed_server_events_not_signed_attestations",
    }
