"""Scoped Neo4j reads ported from DeepThought's GraphQueryService/ContextService.

Original MIT sources: libs/analysis/.../engine/data_access/graph_repository.py
and libs/core/.../context/context_service.py; attribution is retained in NOTICE.
The service owns graph reads; CLI/MCP adapters must not duplicate Cypher logic.
Neo4j is imported only at the database feature boundary.
"""

import json
import math
import re
import time
from hashlib import sha256
from pathlib import Path

from .components import PACKAGE_KINDS
from .source_roles import ROLE_PATTERNS, exclusion_pattern

# A query that is one identifier also finds members named by it.
_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")


# Relations ``get_impact`` and ``get_contract_impact`` follow backwards (from
# the dependent's side) and forwards (from the changed side).
_IMPACT_BACKWARD = (
    "CALLS|INHERITS_FROM|IMPLEMENTS|DISPATCHES_TO|USES_FIXTURE|USES_DEPENDENCY"
    "|REQUESTS|CONSUMES|READS"
    "|REFERENCES_TABLE|CARRIES"
)
_IMPACT_FORWARD = "HANDLES|PUBLISHES|WRITES"
# A contract's messages, for ``contract`` actions: the declarations named
# like protoc's request and response types that the signature of each
# client's stub method names (SIGNATURE_USES, from SCIP), found through the
# call at the line each REQUESTS edge records. They are ``known`` when the
# Go type checker gave the contract its ``message_types``, or when every
# client's stub names one and every provider's signature names one.
_CONTRACT_MESSAGES = (
    "CALL { WITH root "
    "WITH root, [x IN [root.request_message, root.response_message] "
    "WHERE x IS NOT NULL | last(split(x, '.'))] AS names "
    "OPTIONAL MATCH (client)-[r:REQUESTS]->(root) "
    "WHERE client.repo_url=$repo_url AND client.snapshot_id=$snapshot_id "
    "OPTIONAL MATCH (client)-[c:CALLS]->(:Symbol)-[:SIGNATURE_USES]->(m:Symbol) "
    "WHERE r IS NOT NULL AND c.line = r.line "
    "AND c.name = last(split(r.request, '.')) "
    "AND last(split(m.name, '.')) IN names "
    "WITH r, collect(DISTINCT m) AS found "
    "RETURN collect(CASE WHEN r IS NULL THEN null ELSE found END) AS clients } "
    "WITH root, parts, clients, "
    "reduce(acc = [], found IN clients | acc + found) AS messages "
    "WITH root, parts, messages, root.message_types IS NOT NULL OR ("
    "size(messages) > 0 AND all(found IN clients WHERE size(found) > 0) "
    "AND all(p IN parts WHERE any(m IN messages WHERE (p)-[:SIGNATURE_USES]->(m)))"
    ") AS known "
)
# What each dependent must do about a change (``action``), from the change's
# kind: a declaration's ``signature`` (its parameters or results) or
# ``behaviour`` (its body only), or a ``contract`` (its messages).
_IMPACT_ACTIONS = {
    None: "",
    "signature": "CASE nearest WHEN 1 THEN 'must_change' ELSE 'retest' END AS action, ",
    "behaviour": "'retest' AS action, ",
    "contract": "CASE WHEN carried = 1 THEN 'must_change' "
    "WHEN known THEN 'retest' "
    "ELSE 'unknown' END AS action, ",
}
# The labels a Contract node carries beside ``Contract``.
CONTRACT_KINDS = (
    "DbColumn",
    "DbTable",
    "GraphqlField",
    "GrpcMethod",
    "HttpRoute",
    "MessageDestination",
)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


# At most this many distinct files are checked per answer, and listed.
_FRESHNESS_PATHS = 500
_FRESHNESS_LISTED = 20
_PATH_KEYS = frozenset({"file_path", "path", "file"})
# A workspace file whose real path is outside the workspace (a symlink out
# of it) is not read: it is left out of the check.
_OUTSIDE = object()


def _path_key(key):
    """file_path, path or file, also as a raw query's unaliased column
    (``f.path``, ``s.file_path``)."""
    return isinstance(key, str) and key.rsplit(".", 1)[-1] in _PATH_KEYS


def _named_paths(value):
    """Relative file paths an answer names under file_path, path or file
    keys, at any depth."""
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if _path_key(key) and isinstance(item, str):
                if item and not item.startswith("/") and ".." not in item.split("/"):
                    found.add(item)
            else:
                found |= _named_paths(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found |= _named_paths(item)
    return found


def _cypher_code(query):
    """Return query with string literals, quoted names and comments blanked.

    Each blanked character becomes a space, so keyword checks see only the
    query's own code. An unterminated literal or comment is refused."""
    out = []
    i, n = 0, len(query)
    while i < n:
        c = query[i]
        if c in "'\"`":
            j = i + 1
            while j < n and query[j] != c:
                j += 2 if query[j] == "\\" and c != "`" else 1
            if j >= n:
                raise ValueError("Unterminated quoted text in Cypher query")
            out.append(" " * (j + 1 - i))
            i = j + 1
        elif query.startswith("//", i):
            j = query.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif query.startswith("/*", i):
            j = query.find("*/", i + 2)
            if j < 0:
                raise ValueError("Unterminated comment in Cypher query")
            out.append(" " * (j + 2 - i))
            i = j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + " must be a nonempty string")
    return value


def _bounds(limit, max_output_bytes, timeout):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    if type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= 65536:
        raise ValueError("max_output_bytes must be from 1024 to 65536")
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= 30
    ):
        raise ValueError("timeout must be finite, positive and at most 30 seconds")


def _evidence_filter(evidence_types):
    """None keeps every edge; otherwise a nonempty subset of the evidence types."""
    from .analysis_tools.evidence import EVIDENCE_TYPES

    if evidence_types is None:
        return None
    if isinstance(evidence_types, str) or not isinstance(
        evidence_types, (list, tuple, set, frozenset)
    ):
        raise ValueError("evidence_types must be a list of evidence types or None")
    selected = sorted(set(evidence_types))
    if not selected or not set(selected) <= EVIDENCE_TYPES:
        raise ValueError(
            "evidence_types must be a nonempty subset of "
            + ", ".join(sorted(EVIDENCE_TYPES))
        )
    return selected


_SOURCE_DETAIL = (
    "source_code",
    "source_sha256",
    "source_start_line",
    "source_start_column",
    "source_end_line",
    "source_end_column",
    "name_range_utf8",
    "name_range_utf16",
    "name_range_utf32",
)


def _value(value):
    """Preserve actual graph properties and paths rather than discarding scalars."""
    from neo4j.graph import Node, Path, Relationship
    from .storage.graph.encoding import decode_properties

    if isinstance(value, Node):
        properties = decode_properties(dict(value))
        # Declaration text and positions are returned by get_code_context.
        for key in _SOURCE_DETAIL:
            properties.pop(key, None)
        return {
            "element_id": value.element_id,
            "labels": sorted(value.labels),
            "properties": properties,
        }
    if isinstance(value, Relationship):
        return {
            "element_id": value.element_id,
            # Stable content identity of the edge within its snapshot; the
            # element_id is Neo4j-internal and may be reused across databases.
            "edge_id": value.get("_dt_edge_id"),
            "type": value.type,
            "source": value.start_node.element_id,
            "target": value.end_node.element_id,
            "properties": decode_properties(dict(value)),
        }
    if isinstance(value, Path):
        return {
            "nodes": [_value(n) for n in value.nodes],
            "relationships": [_value(r) for r in value.relationships],
        }
    if isinstance(value, dict):
        return {str(k): _value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_value(v) for v in value]
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if hasattr(value, "iso_format"):
        return value.iso_format()
    raise ValueError("Unsupported non-JSON query value: " + type(value).__name__)


# Raw rows a text reply is rendered from; its own budget is on the text.
_RAW_TEXT_BYTES = 4_000_000


def _check_cypher(query, parameters, allow_database_scope):
    """Reject anything but one parameterized read query (defense in depth)."""
    _text(query, "query")
    if allow_database_scope is not True:
        raise ValueError("Raw Cypher requires explicit allow_database_scope=True")
    if not isinstance(parameters, dict) or any(
        not isinstance(k, str) for k in parameters
    ):
        raise ValueError("Cypher parameters must be a string-keyed dictionary")
    _json(parameters)
    if set(parameters) & {"repo_url", "snapshot_id", "fetch_limit"}:
        raise ValueError("Scope and fetch_limit parameters are reserved")
    if len(query.encode()) > 16384:
        raise ValueError("Cypher text exceeds 16384 bytes")
    # Keywords are looked for in the query's code only: a quoted name such as
    # "use", "Start" or "delete.go" is data, not a clause.
    code = _cypher_code(query)
    if not re.match(r"\s*(MATCH|OPTIONAL\s+MATCH|RETURN|WITH|UNWIND)\b", code, re.I):
        raise ValueError("Only a single read query is accepted")
    if re.search(
        r"\b(CALL|LOAD|CSV|CREATE|MERGE|SET|DELETE|REMOVE|DROP|ALTER|GRANT|DENY|REVOKE|FOREACH|SHOW|USE|START|STOP|TERMINATE)\b|;",
        code,
        re.I,
    ):
        raise ValueError(
            "Procedures, updates, administration and multiple statements are disabled"
        )


class GraphQueryService:
    """Single query implementation shared by programmatic and agent callers.

    Typed reads always require an explicitly selected complete repository snapshot.
    Raw Cypher is an opt-in *database-scoped* capability, not automatic tenant
    isolation. It requires an exclusively owned single-snapshot database. Reads
    run in explicit transactions that are always rolled back. The database should
    additionally enforce read-only access; routing mode alone is not security.
    """

    def __init__(self, driver, database="neo4j", *, observer=None, workspace=None):
        self.driver = driver
        self.database = _text(database, "database")
        self.observer = observer
        # The working copy the snapshot was taken from, if the caller names
        # one: each answer then says which of the files it names have
        # changed there since (``freshness``).
        self.workspace = None if workspace is None else Path(workspace)
        self._digests = {}

    @staticmethod
    async def _marker(tx, repo_url, snapshot_id):
        result = await tx.run(
            "MATCH (s:AnalysisSnapshot {repo_url:$repo_url,snapshot_id:$snapshot_id,status:'complete'}) RETURN s.metadata_json AS metadata,s.content_hash AS content_hash",
            repo_url=repo_url,
            snapshot_id=snapshot_id,
        )
        marker = await result.single()
        if marker is None:
            raise ValueError("Selected repository snapshot is missing or incomplete")
        return marker

    async def _read(
        self,
        query,
        parameters,
        repo_url,
        snapshot_id,
        *,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
        database_scope=False,
        repository_scope=False,
        required_capability=None,
        metric=None,
        observe=True,
        raw_output_bytes=None,
    ):
        _text(repo_url, "repo_url")
        if not repository_scope:
            _text(snapshot_id, "snapshot_id")
        _bounds(limit, max_output_bytes, timeout)
        if raw_output_bytes is not None:
            # Text results are budgeted after rendering; the raw rows they are
            # rendered from may be larger than any JSON reply.
            max_output_bytes = raw_output_bytes
        started = time.monotonic()
        parameters = dict(parameters)
        parameters.update(
            repo_url=repo_url, snapshot_id=snapshot_id, fetch_limit=limit + 1
        )
        async with self.driver.session(
            database=self.database,
            default_access_mode="READ",
            fetch_size=min(limit + 1, 101),
        ) as session:
            tx = await session.begin_transaction(timeout=timeout)
            event = None
            try:
                if repository_scope:
                    # Snapshot listing spans the repository's complete snapshots.
                    marker = {"metadata": None, "content_hash": None}
                else:
                    marker = await self._marker(tx, repo_url, snapshot_id)
                metadata = json.loads(marker["metadata"] or "{}")
                if required_capability is not None and required_capability not in (
                    metadata.get("custom_metrics", {}).get("capabilities", [])
                ):
                    raise ValueError(
                        "Selected snapshot does not provide " + required_capability
                    )
                if database_scope:
                    check = await tx.run(
                        "MATCH (n) WHERE NOT (n.repo_url=$repo_url AND n.snapshot_id=$snapshot_id) OR n.repo_url IS NULL OR n.snapshot_id IS NULL RETURN count(n) AS other_nodes",
                        repo_url=repo_url,
                        snapshot_id=snapshot_id,
                    )
                    if (await check.single())["other_nodes"]:
                        raise ValueError(
                            "Raw Cypher requires a dedicated single-snapshot database"
                        )
                    explained = await tx.run("EXPLAIN " + query, parameters)
                    summary = await explained.consume()
                    if summary.query_type != "r":
                        raise ValueError(
                            "Only read-only Cypher query plans are accepted"
                        )
                result = await tx.run(query, parameters)
                metrics = metadata.get("custom_metrics", {})
                response = {
                    "repo_url": repo_url,
                    "snapshot_id": snapshot_id,
                    "status": "succeeded",
                    "scope": "database"
                    if database_scope
                    else ("repository" if repository_scope else "repository_snapshot"),
                    "capabilities": metrics.get("capabilities", []),
                    "analysis_status": metrics.get("analysis_status", "unknown"),
                    "items": [],
                    "returned": 0,
                    "truncated": False,
                }
                if metric is not None:
                    response[metric] = metrics.get(metric)
                if repository_scope:
                    # Several snapshots; their own analysis facts are per item.
                    del response["capabilities"], response["analysis_status"]
                async for record in result:
                    if len(response["items"]) >= limit:
                        response["truncated"] = True
                        break
                    item = {key: _value(value) for key, value in record.items()}
                    response["items"].append(item)
                    response["returned"] = len(response["items"])
                    # Reserve room for the longer `false`/`true` truncation spelling.
                    if len(_json(response).encode()) + 1 > max_output_bytes:
                        response["items"].pop()
                        response["returned"] -= 1
                        response["truncated"] = True
                        break
                if self.workspace is not None and not repository_scope:
                    response["freshness"] = await self._freshness(
                        tx, repo_url, snapshot_id, response["items"]
                    )
                    # The note may push the answer over budget: drop whole
                    # items, as the loop above does.
                    while (
                        response["items"]
                        and len(_json(response).encode()) + 1 > max_output_bytes
                    ):
                        response["items"].pop()
                        response["returned"] -= 1
                        response["truncated"] = True
                if len(_json(response).encode()) > max_output_bytes:
                    raise ValueError("Query metadata exceeds the output byte budget")
                # Optional evidence hook shared by CLI, MCP and embedded callers.
                # Emit only after a real Neo4j read; no raw query or parameters.
                if self.observer is not None and observe:
                    encoded = _json(response).encode()
                    event = {
                        "backend": "neo4j",
                        "interface": "deepthought.graph_queries.GraphQueryService",
                        "database": self.database,
                        "repo_url": repo_url,
                        "snapshot_id": snapshot_id,
                        "content_hash": marker["content_hash"],
                        "query_sha256": sha256(query.encode()).hexdigest(),
                        "response_sha256": sha256(encoded).hexdigest(),
                        "response_bytes": len(encoded),
                        "returned": response["returned"],
                        "truncated": response["truncated"],
                        "status": "succeeded",
                    }
                return response
            finally:
                await tx.rollback()
                if event is not None:
                    event["wall_seconds"] = time.monotonic() - started
                    self.observer(event)

    def _workspace_digest(self, path):
        """sha256 of a workspace file's bytes, as the analysis records it in
        File.file_sha256; None when it is gone, ``_OUTSIDE`` when its real
        path leaves the workspace. Cached by size and mtime, so an edit that
        keeps both (``cp -p``, an archive restore) reads as unchanged."""
        target = self.workspace / path
        try:
            stat = target.stat()
            real = target.resolve(strict=True)
        except OSError:
            return None
        if not real.is_relative_to(self.workspace.resolve()):
            return _OUTSIDE
        key = (path, stat.st_size, stat.st_mtime_ns)
        if key not in self._digests:
            try:
                self._digests[key] = sha256(target.read_bytes()).hexdigest()
            except OSError:
                return None
        return self._digests[key]

    async def _freshness(self, tx, repo_url, snapshot_id, items):
        """Whether the files an answer names still hold the bytes the
        snapshot analyzed, in the workspace this service was given.

        ``status`` is ``current`` (every named file is unchanged),
        ``changed`` (``changed_files`` were edited since and ``missing_files``
        are gone: their lines may have moved) or ``unverified`` (no
        workspace was given, or the answer names no analyzed file)."""
        paths = sorted(_named_paths(items))
        if self.workspace is None or not paths:
            return {"status": "unverified"}
        result = await tx.run(
            "MATCH (f:File {repo_url:$repo_url,snapshot_id:$snapshot_id}) "
            "WHERE f.path IN $paths AND f.file_sha256 IS NOT NULL "
            "RETURN f.path AS path, f.file_sha256 AS digest",
            repo_url=repo_url,
            snapshot_id=snapshot_id,
            paths=paths[:_FRESHNESS_PATHS],
        )
        recorded = {record["path"]: record["digest"] async for record in result}
        if not recorded:
            return {"status": "unverified"}
        changed, missing, checked = [], [], 0
        for path, digest in sorted(recorded.items()):
            current = self._workspace_digest(path)
            if current is _OUTSIDE:
                continue
            checked += 1
            if current is None:
                missing.append(path)
            elif current != digest:
                changed.append(path)
        if not checked:
            return {"status": "unverified"}
        freshness = {
            "status": "changed" if changed or missing else "current",
            "files_checked": checked,
        }
        if changed:
            freshness["changed_files"] = changed[:_FRESHNESS_LISTED]
        if missing:
            freshness["missing_files"] = missing[:_FRESHNESS_LISTED]
        if len(changed) > _FRESHNESS_LISTED or len(missing) > _FRESHNESS_LISTED:
            freshness["files_listed_truncated"] = True
        return freshness

    async def list_snapshots(
        self,
        repo_url,
        *,
        commit=None,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """List a repository's complete snapshots, optionally those of one commit.

        A snapshot lists every commit it was linked to; the same content analyzed
        at several commits is one snapshot. Newest publication first.
        """
        from .revisions import is_commit

        if commit is not None and not is_commit(commit):
            raise ValueError("commit must be a full lowercase hex Git object name")
        response = await self._read(
            """MATCH (s:AnalysisSnapshot {repo_url:$repo_url,status:'complete'})
            WHERE $commit IS NULL OR $commit IN coalesce(s.source_commits,[])
            RETURN s.snapshot_id AS snapshot_id,s.content_hash AS content_hash,
                s.tool_name AS tool_name,s.node_count AS nodes,
                s.relationship_count AS relationships,s.completed_at AS completed_at,
                coalesce(s.source_commits,[]) AS commits
            ORDER BY s.completed_at DESC,s.snapshot_id LIMIT $fetch_limit""",
            {"commit": commit},
            repo_url,
            None,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
            repository_scope=True,
        )
        for item in response["items"]:
            item["commits"] = sorted(item["commits"])
        response["commit"] = commit
        if not response["items"]:
            response["status"] = "not_found"
        response["semantics"] = (
            "A commit is listed only when every analyzed file was tracked and "
            "unchanged from it; unlinked snapshots may still match a commit."
        )
        if len(_json(response).encode()) > max_output_bytes:
            raise ValueError("Query response exceeds output byte budget")
        return response

    _DIFF_CATEGORIES = (
        "nodes.added",
        "nodes.removed",
        "nodes.changed",
        "edges.added",
        "edges.removed",
    )
    # A node in both snapshots changed when its labels or any stored property
    # (other than its snapshot) differ. Stored properties are never null.
    _NODE_CHANGED = (
        "(size(labels(n))<>size(labels(o)) OR any(l IN labels(n) WHERE NOT l IN labels(o)) "
        "OR size(keys(n))<>size(keys(o)) OR any(k IN keys(n) WHERE k<>'snapshot_id' "
        "AND (NOT k IN keys(o) OR n[k]<>o[k])))"
    )
    _NODE = (
        "n {.qualified_name,.name,.kind,.file_path,.path,.start_line,.end_line,"
        "labels:labels(n)}"
    )

    async def diff_snapshots(
        self,
        repo_url,
        *,
        base_snapshot_id,
        head_snapshot_id,
        changes=None,
        limit=20,
        max_output_bytes=16000,
        timeout=10,
    ):
        """Compare two complete snapshots of one repository.

        Nodes are matched by qualified name, which does not depend on source
        positions. Edges are matched by edge_id, the content hash of their type,
        endpoints and properties, so an edge whose properties change (for example
        a call moved to another line) is reported removed and added. ``changes``
        selects which categories list items; every category is always counted.
        """
        _text(base_snapshot_id, "base_snapshot_id")
        _text(head_snapshot_id, "head_snapshot_id")
        if changes is None:
            changes = self._DIFF_CATEGORIES
        if (
            isinstance(changes, str)
            or not isinstance(changes, (list, tuple, set, frozenset))
            or not changes
            or not set(changes) <= set(self._DIFF_CATEGORIES)
        ):
            raise ValueError(
                "changes must be a nonempty subset of "
                + ", ".join(self._DIFF_CATEGORIES)
            )
        bounds = dict(max_output_bytes=max_output_bytes, timeout=timeout)
        counts = []
        for scope, other in (
            (head_snapshot_id, base_snapshot_id),
            (base_snapshot_id, head_snapshot_id),
        ):
            # Reading in each scope also checks that both snapshots are complete.
            row = {}
            for statement in (
                """MATCH (n:GraphNode {repo_url:$repo_url,snapshot_id:$snapshot_id})
                OPTIONAL MATCH (o:GraphNode {repo_url:$repo_url,snapshot_id:$other,
                    qualified_name:n.qualified_name})
                RETURN count(n) AS nodes,count(n)-count(o) AS nodes_only,
                    sum(CASE WHEN o IS NOT NULL AND """
                + self._NODE_CHANGED
                + " THEN 1 ELSE 0 END) AS nodes_changed",
                """MATCH (s:GraphNode {repo_url:$repo_url,snapshot_id:$snapshot_id})-[r]->
                    (:GraphNode {repo_url:$repo_url,snapshot_id:$snapshot_id})
                OPTIONAL MATCH (os:GraphNode {repo_url:$repo_url,snapshot_id:$other,
                    qualified_name:s.qualified_name})
                RETURN count(r) AS edges,sum(CASE WHEN os IS NOT NULL AND EXISTS {
                    MATCH (os)-[r2]->() WHERE r2._dt_edge_id=r._dt_edge_id
                } THEN 0 ELSE 1 END) AS edges_only""",
            ):
                result = await self._read(
                    statement, {"other": other}, repo_url, scope, limit=1, **bounds
                )
                row.update(result["items"][0])
            counts.append(row)
        head, base = counts
        summary = {
            "nodes": {
                "added": head["nodes_only"],
                "removed": base["nodes_only"],
                "changed": head["nodes_changed"],
                "unchanged": head["nodes"] - head["nodes_only"] - head["nodes_changed"],
            },
            "edges": {
                "added": head["edges_only"],
                "removed": base["edges_only"],
                "unchanged": head["edges"] - head["edges_only"],
            },
        }
        listed = {}
        for category in self._DIFF_CATEGORIES:
            if category not in changes:
                continue
            element, change = category.split(".")
            scope, other = (
                (base_snapshot_id, head_snapshot_id)
                if change == "removed"
                else (head_snapshot_id, base_snapshot_id)
            )
            if element == "nodes":
                statement = (
                    "MATCH (n:GraphNode {repo_url:$repo_url,snapshot_id:$snapshot_id}) "
                    "OPTIONAL MATCH (o:GraphNode {repo_url:$repo_url,snapshot_id:$other,"
                    "qualified_name:n.qualified_name}) "
                    + (
                        "WITH n,o WHERE o IS NULL RETURN " + self._NODE + " AS node "
                        if change != "changed"
                        else "WITH n,o WHERE o IS NOT NULL AND "
                        + self._NODE_CHANGED
                        + " RETURN "
                        + self._NODE
                        + " AS node,"
                        "[k IN keys(n) WHERE NOT k IN ['snapshot_id','source_code'] "
                        "AND NOT k STARTS WITH '_dt_' "
                        "AND (NOT k IN keys(o) OR n[k]<>o[k]) | {key:k,base:o[k],head:n[k]}]"
                        "+[k IN keys(o) WHERE NOT k IN keys(n) AND k<>'source_code' "
                        "AND NOT k STARTS WITH '_dt_' "
                        "| {key:k,base:o[k],head:null}] AS properties,"
                        "CASE WHEN size(labels(n))<>size(labels(o)) OR any(l IN labels(n) "
                        "WHERE NOT l IN labels(o)) THEN {base:labels(o),head:labels(n)} "
                        "END AS labels "
                    )
                    + "ORDER BY n.qualified_name LIMIT $fetch_limit"
                )
            else:
                statement = (
                    "MATCH (s:GraphNode {repo_url:$repo_url,snapshot_id:$snapshot_id})-[r]->"
                    "(t:GraphNode {repo_url:$repo_url,snapshot_id:$snapshot_id}) "
                    "OPTIONAL MATCH (os:GraphNode {repo_url:$repo_url,snapshot_id:$other,"
                    "qualified_name:s.qualified_name}) "
                    "WITH s,r,t,os WHERE os IS NULL OR NOT EXISTS { "
                    "MATCH (os)-[r2]->() WHERE r2._dt_edge_id=r._dt_edge_id } "
                    "RETURN r._dt_edge_id AS edge_id,type(r) AS type,"
                    "s.qualified_name AS source,t.qualified_name AS target,"
                    "r.evidence_type AS evidence_type,r.rule_id AS rule_id,r.line AS line "
                    "ORDER BY type,source,target,edge_id LIMIT $fetch_limit"
                )
            rows = await self._read(
                statement, {"other": other}, repo_url, scope, limit=limit, **bounds
            )
            listed[category] = {"items": rows["items"], "truncated": rows["truncated"]}
        response = {
            "repo_url": repo_url,
            "base_snapshot_id": base_snapshot_id,
            "head_snapshot_id": head_snapshot_id,
            "status": "succeeded",
            "scope": "repository_snapshots",
            "summary": summary,
            "changes": listed,
            "truncated": any(item["truncated"] for item in listed.values()),
            "semantics": (
                "Nodes match by qualified name and edges by content; an edge whose "
                "properties changed is listed as removed and added."
            ),
        }
        # Trim the longest listings first until the response fits its budget.
        while len(_json(response).encode()) > max_output_bytes:
            longest = max(listed.values(), key=lambda item: len(item["items"]))
            if not longest["items"]:
                raise ValueError("Query metadata exceeds the output byte budget")
            longest["items"].pop()
            longest["truncated"] = response["truncated"] = True
        return response

    async def search_elements(
        self,
        query,
        repo_url,
        *,
        snapshot_id,
        file_path=None,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Search original code-element names in one repository snapshot."""
        if not isinstance(query, str):
            raise ValueError("query must be a string; empty selects all declarations")
        if file_path is not None:
            _text(file_path, "file_path")
        if query:
            # The index keeps a member's dotted name (`Command.Flags`) as one
            # token, so a bare identifier also matches declarations whose
            # name is it or ends with `.` and it; those name matches come
            # first, and a declaration found only that way has score 0.
            statement = """CALL db.index.fulltext.queryNodes('code_search',$query) YIELD node,score
                WHERE node.repo_url=$repo_url AND node.snapshot_id=$snapshot_id
                  AND score > 0.1
                WITH collect({node:node,score:score}) AS hits
                OPTIONAL MATCH (named:Symbol {repo_url:$repo_url,snapshot_id:$snapshot_id})
                WHERE $member IS NOT NULL
                  AND (named.name = $query OR named.name ENDS WITH $member)
                WITH hits,collect({node:named,score:0.0}) AS named
                UNWIND hits + named AS hit
                WITH hit.node AS node,max(hit.score) AS score
                WHERE node IS NOT NULL
                  AND ($file_path IS NULL OR node.file_path=$file_path)
                WITH node,score,$member IS NOT NULL
                    AND (node.name = $query OR node.name ENDS WITH $member) AS name_match
                RETURN node {.qualified_name,.definition_id,.name,.display_name,.kind,
                    .file_path,.start_line,.end_line,.column,.end_column,.native_id} AS element,
                    score,name_match
                ORDER BY name_match DESC,score DESC,element.file_path,element.start_line,
                    element.qualified_name
                LIMIT $fetch_limit"""
        else:
            statement = """MATCH (node:Symbol {repo_url:$repo_url,snapshot_id:$snapshot_id})
                WHERE $file_path IS NULL OR node.file_path=$file_path
                RETURN node {.qualified_name,.definition_id,.name,.display_name,.kind,
                    .file_path,.start_line,.end_line,.column,.end_column,.native_id} AS element
                ORDER BY element.file_path,element.start_line,element.qualified_name
                LIMIT $fetch_limit"""
        return await self._read(
            statement,
            {
                "query": query,
                "file_path": file_path,
                "member": "." + query if _IDENTIFIER.fullmatch(query) else None,
            },
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
        )

    async def get_code_context(
        self,
        file_path,
        repo_url,
        *,
        snapshot_id,
        line_number=None,
        definition_id=None,
        native_symbol=None,
        source_offset=0,
        max_source_characters=1200,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
        include_relationships=True,
    ):
        """Port File -> DEFINES context lookup; innermost containing scope first.

        Each declaration carries a page of its stored source text:
        ``max_source_characters`` Unicode code points from ``source_offset``,
        with ``source_characters`` giving the full length to page through.
        An indexer (SCIP) symbol bound to a parsed declaration is returned as
        that declaration, listing the symbols in ``native_ids``;
        ``native_symbol`` selects by indexer symbol.
        """
        _text(file_path, "file_path")
        if line_number is not None and (
            type(line_number) is not int or line_number < 1
        ):
            raise ValueError("line_number must be a positive integer or None")
        for value, label in (
            (definition_id, "definition_id"),
            (native_symbol, "native_symbol"),
        ):
            if value is not None:
                _text(value, label)
        if type(source_offset) is not int or not 0 <= source_offset <= 2_000_000:
            raise ValueError("source_offset must be from 0 to 2000000")
        if (
            type(max_source_characters) is not int
            or not 0 <= max_source_characters <= 8192
        ):
            raise ValueError("max_source_characters must be from 0 to 8192")
        response = await self._read(
            """MATCH (f:File {repo_url:$repo_url,snapshot_id:$snapshot_id,path:$file_path})
            -[:DEFINES]->(n:Symbol {repo_url:$repo_url,snapshot_id:$snapshot_id})
            OPTIONAL MATCH (n)-[:HAS_AST_DECLARATION]->
                (ast:Symbol {repo_url:$repo_url,snapshot_id:$snapshot_id})
            WITH n,coalesce(ast,n) AS elem
            WHERE ($line_number IS NULL OR (elem.start_line <= $line_number AND elem.end_line >= $line_number))
              AND ($definition_id IS NULL OR elem.definition_id = $definition_id
                   OR n.definition_id = $definition_id)
              AND ($native_symbol IS NULL OR n.native_id = $native_symbol)
            WITH elem,[x IN collect(n.native_id) WHERE x IS NOT NULL] AS native_ids
            RETURN elem {.qualified_name,.definition_id,.name,.display_name,.kind,
                .file_path,.start_line,.end_line,.column,.end_column,.native_id,
                native_ids:native_ids,
                .source_sha256,.source_start_line,.source_start_column,
                .source_end_line,.source_end_column,
                .name_range_utf8,.name_range_utf16,.name_range_utf32,
                source_characters:size(elem.source_code),
                source_offset:CASE WHEN elem.source_code IS NULL THEN null ELSE $source_offset END,
                source_code:substring(elem.source_code,$source_offset,$max_source_characters)} AS element
            ORDER BY CASE WHEN $line_number IS NULL THEN elem.start_line ELSE elem.end_line-elem.start_line END,
                elem.start_line,elem.qualified_name LIMIT $fetch_limit""",
            {
                "file_path": file_path,
                "line_number": line_number,
                "definition_id": definition_id,
                "native_symbol": native_symbol,
                "source_offset": source_offset,
                "max_source_characters": max_source_characters,
            },
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
        )

        if type(include_relationships) is not bool:
            raise ValueError("include_relationships must be boolean")
        if include_relationships:
            qnames = [row["element"]["qualified_name"] for row in response["items"]]
            neighbours = await self._read(
                """MATCH (source:GraphNode {repo_url:$repo_url,snapshot_id:$snapshot_id})
                -[r]->(target:GraphNode {repo_url:$repo_url,snapshot_id:$snapshot_id})
                WHERE source.qualified_name IN $qnames OR target.qualified_name IN $qnames
                   OR (source:File AND source.path=$file_path AND type(r) IN ['IMPORTS','EXPORTS'])
                RETURN source AS source,r AS relationship,target AS target
                ORDER BY type(r),source.qualified_name,target.qualified_name LIMIT $fetch_limit""",
                {"qnames": qnames, "file_path": file_path},
                repo_url,
                snapshot_id,
                limit=limit,
                max_output_bytes=max_output_bytes,
                timeout=timeout,
            )
            response["relationships"] = neighbours["items"]
            response["truncated"] = response["truncated"] or neighbours["truncated"]
            while len(_json(response).encode()) > max_output_bytes:
                response["truncated"] = True
                if response["relationships"]:
                    response["relationships"].pop()
                elif response["items"]:
                    response["items"].pop()
                    response["returned"] -= 1
                else:
                    raise ValueError("Query metadata exceeds the output byte budget")
        return response

    async def get_file_structure(
        self,
        file_path,
        repo_url,
        *,
        snapshot_id,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Port original file-prefix navigation without unbounded collect(elem)."""
        if not isinstance(file_path, str):
            raise ValueError("file_path must be a string")
        return await self._read(
            """MATCH (f:File {repo_url:$repo_url,snapshot_id:$snapshot_id})
            WHERE $file_path='' OR f.path=$file_path OR f.path STARTS WITH $prefix
            OPTIONAL MATCH (f)-[:DEFINES]->(elem:Symbol {repo_url:$repo_url,snapshot_id:$snapshot_id})
            RETURN f.path AS path,f.language AS language,count(elem) AS declarations
            ORDER BY path LIMIT $fetch_limit""",
            {"file_path": file_path, "prefix": file_path.rstrip("/") + "/"},
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
        )

    async def get_function_calls(
        self,
        function_qname,
        repo_url,
        *,
        snapshot_id,
        evidence_types=None,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        return await self._calls(
            function_qname,
            repo_url,
            snapshot_id,
            False,
            evidence_types,
            limit,
            max_output_bytes,
            timeout,
        )

    async def get_function_callers(
        self,
        function_qname,
        repo_url,
        *,
        snapshot_id,
        evidence_types=None,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        return await self._calls(
            function_qname,
            repo_url,
            snapshot_id,
            True,
            evidence_types,
            limit,
            max_output_bytes,
            timeout,
        )

    async def _calls(
        self, qname, repo_url, snapshot_id, incoming, evidence, limit, budget, timeout
    ):
        _text(qname, "function_qname")
        evidence = _evidence_filter(evidence)
        pattern = (
            "(other)-[r:CALLS]->(func)" if incoming else "(func)-[r:CALLS]->(other)"
        )
        response = await self._read(
            "MATCH (func:Symbol {repo_url:$repo_url,snapshot_id:$snapshot_id,qualified_name:$qname}) "
            "MATCH "
            + pattern
            + " WHERE other.repo_url=$repo_url AND other.snapshot_id=$snapshot_id "
            "AND ($evidence IS NULL OR r.evidence_type IN $evidence) "
            "RETURN other {.qualified_name,.name,.kind,.file_path,.start_line} AS element,r AS relationship "
            "ORDER BY other.qualified_name,r.line,r.column,r._dt_edge_id LIMIT $fetch_limit",
            {"qname": qname, "evidence": evidence},
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=budget,
            timeout=timeout,
        )
        if not response["items"] and "calls" not in response["capabilities"]:
            response["status"] = "unsupported_or_unobserved"
        response["evidence_types"] = evidence
        response["semantics"] = (
            "Observed CALLS edges only; absence does not prove that no calls exist."
        )
        if len(_json(response).encode()) > budget:
            raise ValueError("Query response exceeds output byte budget")
        return response

    async def get_call_sites(
        self,
        name,
        repo_url,
        *,
        snapshot_id,
        file_path=None,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Every call site that calls ``name`` (the last name of its callee,
        ``param`` for ``c.req.param()``): linked ones by their CALLS edge and
        target, the others with the reason no rule linked them, and how many
        there are of each."""
        _text(name, "name")
        if file_path is not None:
            _text(file_path, "file_path")
        _bounds(limit, max_output_bytes, timeout)
        parameters = {"name": name, "file_path": file_path}
        linked = """MATCH (caller)-[r:CALLS {name:$name}]->(target)
            WHERE caller.repo_url=$repo_url AND caller.snapshot_id=$snapshot_id
              AND target.repo_url=$repo_url AND target.snapshot_id=$snapshot_id
              AND ($file_path IS NULL OR caller.file_path=$file_path)"""
        unlinked = """MATCH (call:UnresolvedCall {repo_url:$repo_url,snapshot_id:$snapshot_id,name:$name})
            WHERE $file_path IS NULL OR call.file_path=$file_path"""
        counts = await self._read(
            "CALL () { "
            + linked
            + " RETURN 'linked' AS status,count(r) AS calls UNION ALL "
            + unlinked
            + " RETURN call.status AS status,count(call) AS calls } "
            "RETURN status,sum(calls) AS calls ORDER BY status",
            parameters,
            repo_url,
            snapshot_id,
            limit=3,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
        )
        response = await self._read(
            "CALL () { "
            + linked
            + " RETURN 'linked' AS status,r.callee AS callee,caller.file_path AS file_path,"
            "r.line AS line,r.column AS column,null AS reason,null AS resolver,"
            "target.qualified_name AS target,[] AS candidates,r AS relationship UNION ALL "
            + unlinked
            + " RETURN call.status AS status,call.callee AS callee,call.file_path AS file_path,"
            "call.line AS line,call.column AS column,call.reason AS reason,"
            "call.resolver AS resolver,null AS target,"
            "coalesce(call.candidates,[]) AS candidates,null AS relationship } "
            "RETURN status,callee,file_path,line,column,reason,resolver,target,candidates,"
            "relationship "
            "ORDER BY file_path,line,column,callee LIMIT $fetch_limit",
            parameters,
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
            metric="unresolved_call_sites",
        )
        recorded = response.pop("unresolved_call_sites")
        response["counts"] = {
            status: 0 for status in ("linked", "ambiguous", "unresolved")
        } | {item["status"]: item["calls"] for item in counts["items"]}
        if recorded is not True:
            # Written before unlinked calls were stored.
            response["status"] = "unsupported_or_unobserved"
        response["semantics"] = (
            "Call sites whose callee's last name is the name given, as the "
            "parser reads them. Linked calls carry the rule that linked them; "
            "unresolved and ambiguous ones carry the reason. Calls made "
            "through other names (aliases, callbacks, dynamic dispatch) are "
            "not counted."
        )
        if len(_json(response).encode()) > max_output_bytes:
            raise ValueError("Query response exceeds output byte budget")
        return response

    async def get_impact(
        self,
        qname,
        repo_url,
        *,
        snapshot_id,
        max_depth=3,
        evidence_types=None,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
        offset=0,
        exclude_roles=None,
        change="signature",
    ):
        """Declarations and files that depend on a declaration, transitively.

        The change set is the declaration and every declaration it CONTAINS.
        Dependents are reached backwards over CALLS, INHERITS_FROM,
        IMPLEMENTS, DISPATCHES_TO (a Go interface method to each method that
        a call through it can run, so a changed implementation reaches the
        interface's callers), USES_FIXTURE (a test to its pytest fixtures),
        USES_DEPENDENCY (an endpoint to its FastAPI dependencies), REQUESTS,
        CONSUMES, READS, REFERENCES_TABLE (a table with a foreign key to a
        changed table) and CARRIES (a route or message destination whose
        request, response or message is a changed type, so its clients must
        change with it) edges, and forwards over HANDLES, PUBLISHES and
        WRITES: a handler's route (a publisher's
        destination, a writer's table) depends on it, and the callers
        requesting that route (the consumers of that destination, the
        readers of that table, the tables referencing it) depend on the route. At most ``max_depth`` hops. Each dependent is listed
        once, with its strongest path: the path whose weakest edge evidence is
        strongest, then the shortest, then the first by edge identities.

        Each dependent is ``direct`` (depth 1: it uses the change itself, so
        it may need an edit) or ``transitive`` (deeper: its behaviour may
        change, so it needs re-testing), and lists its ``source_roles``
        (``test``, ``example``; see ``source_roles``). ``exclude_roles``
        leaves dependents with any of those roles out.

        Each dependent also has an ``action`` for the kind of ``change``:
        ``signature`` (default; the declaration's parameters or results:
        ``must_change`` at one hop, ``retest`` deeper) or ``behaviour`` (its
        body only: every dependent is ``retest``).
        """
        if change not in ("signature", "behaviour"):
            raise ValueError("change must be signature or behaviour")
        return await self._impact(
            "MATCH (root:Symbol {repo_url:$repo_url,snapshot_id:$snapshot_id,qualified_name:$subject}) "
            "OPTIONAL MATCH (root)-[:CONTAINS*1..10]->(part:Symbol) "
            "WHERE part.repo_url=$repo_url AND part.snapshot_id=$snapshot_id "
            "WITH root, collect(DISTINCT part) AS parts ",
            _IMPACT_BACKWARD,
            qname,
            repo_url,
            snapshot_id,
            max_depth,
            evidence_types,
            limit,
            max_output_bytes,
            timeout,
            offset=offset,
            reverse=_IMPACT_FORWARD,
            exclude_roles=exclude_roles,
            action=change,
        )

    async def search_contracts(
        self,
        query,
        repo_url,
        *,
        snapshot_id,
        kind=None,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Contracts whose name, display name, path or qualified name
        contains ``query`` (case-insensitive; empty selects all): HTTP routes
        (``GET /api/x``), gRPC methods, GraphQL fields, message destinations
        and database tables, optionally of one ``kind`` (a label such as
        ``HttpRoute`` or ``DbTable``), with how many providers and clients
        each has. Their qualified names seed ``get_contract_impact``."""
        if not isinstance(query, str):
            raise ValueError("query must be a string; empty selects all contracts")
        if kind is not None and kind not in CONTRACT_KINDS:
            raise ValueError("kind must be one of " + ", ".join(CONTRACT_KINDS))
        return await self._read(
            "MATCH (c:Contract {repo_url:$repo_url,snapshot_id:$snapshot_id}) "
            "WHERE ($kind IS NULL OR $kind IN labels(c)) AND ($query = '' OR any("
            "text IN [c.name, c.display_name, c.path, c.qualified_name] "
            "WHERE toLower(coalesce(text, '')) CONTAINS toLower($query))) "
            "OPTIONAL MATCH (provider)-[:HANDLES|PUBLISHES|WRITES|MAPS_TO|DECLARES]->(c) "
            "WITH c, count(DISTINCT provider) AS providers "
            "OPTIONAL MATCH (client)-[:REQUESTS|CONSUMES|READS]->(c) "
            "WITH c, providers, count(DISTINCT client) AS clients "
            "RETURN c.qualified_name AS qualified_name, "
            "[label IN labels(c) WHERE label IN $kinds][0] AS kind, "
            "coalesce(c.display_name, c.name) AS name, c.file_path AS file_path, "
            "providers, clients, "
            # gRPC: protoc's message types, and the Go types (see impact).
            "c.request_message AS request_message, "
            "c.response_message AS response_message, "
            "c.message_types AS message_types "
            "ORDER BY kind, name, qualified_name LIMIT $fetch_limit",
            {"query": query, "kind": kind, "kinds": list(CONTRACT_KINDS)},
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
        )

    async def get_contract_impact(
        self,
        qualified_name,
        repo_url,
        *,
        snapshot_id,
        max_depth=3,
        evidence_types=None,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
        offset=0,
        exclude_roles=None,
    ):
        """What depends on a contract: an HTTP route, gRPC method, GraphQL
        field, message destination or database table.

        The change set is the contract and its providers: the declarations
        (or files) that HANDLE the route, PUBLISH to the destination, WRITE,
        map (MAPS_TO) or DECLARE the table, listed under ``providers``.
        Dependents are then found as ``get_impact`` finds them: the callers
        that REQUEST the route, the consumers and readers, the tables whose
        foreign keys reference the table, and their callers in turn, also
        across services. ``offset``, ``exclude_roles`` and the
        output are as for ``get_impact``; ``max_output_bytes`` is at least
        2048.

        Each dependent has an ``action`` for a change of the contract's
        messages: ``must_change`` when it uses the contract itself (one hop:
        a client, or a provider's caller) or reaches it only through
        declarations whose parameter or result types carry the contract's
        messages (CARRIES_MESSAGE edges, from the Go type checker for gRPC
        methods); otherwise ``retest`` when those types are known for the
        contract (its ``message_types``), and ``unknown`` when they are not.
        A caller of a wrapper that hides the messages only re-tests.
        """
        _text(qualified_name, "qualified_name")
        _bounds(limit, max_output_bytes, timeout)
        if max_output_bytes < 2048:
            # Providers and dependents each need room of their own.
            raise ValueError("contract impact needs max_output_bytes of 2048 or more")
        found = await self._read(
            "MATCH (root:Contract {repo_url:$repo_url,snapshot_id:$snapshot_id,"
            "qualified_name:$subject}) "
            "MATCH (provider)-[r:HANDLES|PUBLISHES|WRITES|MAPS_TO|DECLARES]->(root) "
            "WHERE provider.repo_url=$repo_url AND provider.snapshot_id=$snapshot_id "
            "WITH provider, collect(DISTINCT type(r)) AS relations "
            "RETURN provider.qualified_name AS qualified_name, "
            "coalesce(provider.name, provider.path) AS name, "
            "coalesce(provider.file_path, provider.path) AS file_path, "
            "provider.start_line AS start_line, relations "
            "ORDER BY file_path, start_line, qualified_name",
            {"subject": qualified_name},
            repo_url,
            snapshot_id,
            limit=10,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
        )
        providers = found["items"]
        # The providers share the output budget with the dependents.
        while providers and max_output_bytes - len(_json(providers).encode()) < 1200:
            providers.pop()
        response = await self._impact(
            "MATCH (root:Contract {repo_url:$repo_url,snapshot_id:$snapshot_id,"
            "qualified_name:$subject}) "
            "OPTIONAL MATCH (provider)-[:HANDLES|PUBLISHES|WRITES|MAPS_TO|DECLARES]->(root) "
            "WHERE provider.repo_url=$repo_url AND provider.snapshot_id=$snapshot_id "
            "WITH root, collect(DISTINCT provider) AS parts ",
            _IMPACT_BACKWARD,
            qualified_name,
            repo_url,
            snapshot_id,
            max_depth,
            evidence_types,
            limit,
            max_output_bytes - len(_json(providers).encode()) - 64,
            timeout,
            offset=offset,
            reverse=_IMPACT_FORWARD,
            exclude_roles=exclude_roles,
            action="contract",
        )
        response["providers"] = providers
        response["providers_truncated"] = found["truncated"] or len(providers) < len(
            found["items"]
        )
        return response

    async def get_file_impact(
        self,
        file_path,
        repo_url,
        *,
        snapshot_id,
        max_depth=3,
        evidence_types=None,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
        offset=0,
        exclude_roles=None,
    ):
        """Files that depend on a file, transitively, over IMPORTS and
        DEPENDS_ON; ``direct`` and ``transitive`` dependents and
        ``exclude_roles`` as for ``get_impact``."""
        return await self._impact(
            "MATCH (root:File {repo_url:$repo_url,snapshot_id:$snapshot_id,path:$subject}) "
            "WITH root, [] AS parts ",
            "IMPORTS|DEPENDS_ON",
            file_path,
            repo_url,
            snapshot_id,
            max_depth,
            evidence_types,
            limit,
            max_output_bytes,
            timeout,
            offset=offset,
            exclude_roles=exclude_roles,
        )

    async def get_component_impact(
        self,
        qualified_name,
        repo_url,
        *,
        snapshot_id,
        max_depth=3,
        evidence_types=None,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
        offset=0,
    ):
        """Components that depend on a component, transitively.

        Follows REQUIRES (package and Compose depends_on dependencies),
        DEPLOYS (the Compose services and Kubernetes workloads that build or
        run a package) and SELECTS (the Kubernetes Services that route to a
        workload) and USES_CONTRACT_OF (packages whose code requests a route
        or consumes a message destination another package provides)
        backwards, so a package change reaches what deploys and exposes it
        and the packages that use its contracts.
        """
        return await self._impact(
            "MATCH (root:Component {repo_url:$repo_url,snapshot_id:$snapshot_id,"
            "qualified_name:$subject}) WITH root, [] AS parts ",
            "REQUIRES|DEPLOYS|SELECTS|USES_CONTRACT_OF",
            qualified_name,
            repo_url,
            snapshot_id,
            max_depth,
            evidence_types,
            limit,
            max_output_bytes,
            timeout,
            offset=offset,
        )

    async def _impact(
        self,
        seeds,
        relations,
        subject,
        repo_url,
        snapshot_id,
        max_depth,
        evidence,
        limit,
        budget,
        timeout,
        *,
        offset=0,
        reverse=None,
        exclude_roles=None,
        action=None,
    ):
        _text(subject, "subject")
        exclude = exclusion_pattern(exclude_roles)
        if type(offset) is not int or not 0 <= offset <= 100_000:
            raise ValueError("offset must be an integer from 0 to 100000")
        if type(max_depth) is not int or not 1 <= max_depth <= 5:
            raise ValueError("max_depth must be an integer from 1 to 5")
        evidence = _evidence_filter(evidence)
        step = "-[:RELATIONS*1..DEPTH]->"
        if reverse is not None:
            # A quantified path pattern (Neo4j 5.9+) checks each hop's
            # direction while expanding: RELATIONS edges point from the
            # dependent's side, REVERSE edges from the changed side.
            step = (
                " ((near)-[hop:RELATIONS|REVERSE]-(far) WHERE startNode(hop) = "
                "CASE WHEN type(hop) IN $reverse THEN far ELSE near END)"
                "{1,DEPTH} "
            )
        statement = (
            (
                seeds
                + (
                    _CONTRACT_MESSAGES
                    if action == "contract"
                    else "WITH root, parts, [] AS messages, false AS known "
                )
                + "WITH root, messages, known, [root] + parts AS changed "
                "UNWIND changed AS target "
                "MATCH path=(dependent)" + step + "(target) "
                "WHERE NOT dependent IN changed "
                "AND ($exclude IS NULL OR NOT "
                "coalesce(dependent.file_path, dependent.path, '') =~ $exclude) "
                "AND all(n IN nodes(path) WHERE n.repo_url=$repo_url AND n.snapshot_id=$snapshot_id) "
                "AND all(r IN relationships(path) WHERE $evidence IS NULL OR r.evidence_type IN $evidence) "
                "WITH root, messages, known, dependent, target, path, "
                "[r IN relationships(path) | r.evidence_type] AS kinds "
                "WITH root, messages, known, dependent, target, path, CASE "
                "WHEN 'name_matched' IN kinds THEN 2 "
                "WHEN 'pattern_matched' IN kinds THEN 1 ELSE 0 END AS weakness, "
                # A path through declarations that each carry the changed
                # contract's messages (CARRIES_MESSAGE) hands the dependent
                # those messages: it builds or reads them.
                "all(n IN nodes(path)[1..] WHERE n = root OR "
                "EXISTS { (n)-[:CARRIES_MESSAGE]->(root) } OR "
                "any(m IN messages WHERE (n)-[:SIGNATURE_USES]->(m))) AS carried "
                "ORDER BY dependent.qualified_name, weakness, length(path), "
                "[r IN relationships(path) | r._dt_edge_id] "
                "WITH root, known, dependent, collect({target:target, path:path, weakness:weakness})[0] AS best, "
                "min(length(path)) AS nearest, max(CASE WHEN carried THEN 1 ELSE 0 END) AS carried "
                "RETURN dependent {.qualified_name,.name,.display_name,.kind,.path,"
                ".file_path,.start_line,"
                "labels:labels(dependent)} AS element, "
                "length(best.path) AS depth, "
                # Any one-hop path means the dependent names the change
                # itself, even when its strongest path is longer.
                "CASE nearest WHEN 1 THEN 'direct' ELSE 'transitive' END AS impact, "
                "ACTION"
                "[role IN $roles WHERE coalesce(dependent.file_path, dependent.path, '') "
                "=~ role.pattern | role.name] AS source_roles, "
                "['resolved','pattern_matched','name_matched'][best.weakness] AS evidence_type, "
                "best.target.qualified_name AS changed, "
                "[r IN relationships(best.path) | {type:type(r),edge_id:r._dt_edge_id,"
                "evidence_type:r.evidence_type,rule_id:r.rule_id}] AS edges "
                "ORDER BY CASE nearest WHEN 1 THEN 0 ELSE 1 END, depth, best.weakness, "
                "dependent.qualified_name "
                "SKIP $offset LIMIT $fetch_limit"
            )
            .replace("ACTION", _IMPACT_ACTIONS[action])
            .replace("REVERSE", reverse or "")
            .replace("RELATIONS", relations)
            .replace("DEPTH", str(max_depth))
        )
        response = await self._read(
            statement,
            {
                "subject": subject,
                "offset": offset,
                "evidence": evidence,
                "exclude": exclude,
                "roles": [
                    {"name": name, "pattern": ROLE_PATTERNS[name]}
                    for name in sorted(ROLE_PATTERNS)
                ],
                "reverse": (reverse or "").split("|") if reverse else [],
            },
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=budget,
            timeout=timeout,
        )
        if not response["items"]:
            response["status"] = "unsupported_or_unobserved"
        if exclude_roles is not None:
            response["exclude_roles"] = sorted(set(exclude_roles))
        response["max_depth"] = max_depth
        response["evidence_types"] = evidence
        response["relations"] = relations.split("|")
        if reverse:
            response["forward_relations"] = reverse.split("|")
        response["semantics"] = (
            # The backward relations are listed in `relations`.
            "Dependents reachable backwards over observed `relations`"
            + (
                " edges and forwards over " + ", ".join(reverse.split("|"))
                if reverse
                else ""
            )
            + " edges; each path's evidence is its weakest edge. Direct (one "
            "hop): may need edits; transitive: re-test. " + "Absence does "
            "not prove that nothing else is affected: dynamic dispatch, "
            "reflection, unanalyzed files and unrecorded relations are not seen."
        )
        # Results are ordered and a snapshot does not change, so a truncated
        # list continues at `next_offset`.
        response["offset"] = offset
        while True:
            response["next_offset"] = (
                offset + response["returned"]
                if response["truncated"] and response["returned"]
                else None
            )
            # The description fields count toward the budget too; drop
            # whole items.
            if not response["items"] or len(_json(response).encode()) <= budget:
                break
            response["items"].pop()
            response["returned"] = len(response["items"])
            response["truncated"] = True
        if len(_json(response).encode()) > budget:
            raise ValueError("Query response exceeds output byte budget")
        return response

    async def get_members(
        self,
        qname,
        repo_url,
        *,
        snapshot_id,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Declarations directly nested in this one (methods, nested functions)."""
        return await self._nesting(
            qname, repo_url, snapshot_id, False, limit, max_output_bytes, timeout
        )

    async def get_owner(
        self,
        qname,
        repo_url,
        *,
        snapshot_id,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """The declaration this one is directly nested in, if any."""
        return await self._nesting(
            qname, repo_url, snapshot_id, True, limit, max_output_bytes, timeout
        )

    async def _nesting(
        self, qname, repo_url, snapshot_id, incoming, limit, budget, timeout
    ):
        _text(qname, "qualified_name")
        pattern = (
            "(other:Symbol)-[r:CONTAINS]->(d)"
            if incoming
            else "(d)-[r:CONTAINS]->(other:Symbol)"
        )
        response = await self._read(
            "MATCH (d:Symbol {repo_url:$repo_url,snapshot_id:$snapshot_id,qualified_name:$qname}) "
            "MATCH "
            + pattern
            + " WHERE other.repo_url=$repo_url AND other.snapshot_id=$snapshot_id "
            "RETURN other {.qualified_name,.name,.kind,.file_path,.start_line} AS element,r AS relationship "
            "ORDER BY other.start_line,other.column,other.qualified_name LIMIT $fetch_limit",
            {"qname": qname},
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=budget,
            timeout=timeout,
        )
        return self._observed(response, "declaration CONTAINS", budget)

    async def get_base_types(
        self,
        type_qname,
        repo_url,
        *,
        snapshot_id,
        evidence_types=None,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Classes and interfaces a declaration names as its direct bases."""
        return await self._bases(
            type_qname,
            repo_url,
            snapshot_id,
            False,
            evidence_types,
            limit,
            max_output_bytes,
            timeout,
        )

    async def get_subtypes(
        self,
        type_qname,
        repo_url,
        *,
        snapshot_id,
        evidence_types=None,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Classes and interfaces that name this declaration as a direct base."""
        return await self._bases(
            type_qname,
            repo_url,
            snapshot_id,
            True,
            evidence_types,
            limit,
            max_output_bytes,
            timeout,
        )

    async def _bases(
        self, qname, repo_url, snapshot_id, incoming, evidence, limit, budget, timeout
    ):
        _text(qname, "type_qname")
        evidence = _evidence_filter(evidence)
        pattern = (
            "(other)-[r:INHERITS_FROM]->(t)"
            if incoming
            else "(t)-[r:INHERITS_FROM]->(other)"
        )
        response = await self._read(
            "MATCH (t:Symbol {repo_url:$repo_url,snapshot_id:$snapshot_id,qualified_name:$qname}) "
            "MATCH "
            + pattern
            + " WHERE other.repo_url=$repo_url AND other.snapshot_id=$snapshot_id "
            "AND ($evidence IS NULL OR r.evidence_type IN $evidence) "
            "RETURN other {.qualified_name,.name,.kind,.file_path,.start_line} AS element,r AS relationship "
            "ORDER BY other.qualified_name,r.line,r.column,r._dt_edge_id LIMIT $fetch_limit",
            {"qname": qname, "evidence": evidence},
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=budget,
            timeout=timeout,
        )
        if not response["items"] and "supertypes" not in response["capabilities"]:
            response["status"] = "unsupported_or_unobserved"
        response["evidence_types"] = evidence
        response["semantics"] = (
            "Observed INHERITS_FROM edges for bases named in declarations only; "
            "absence does not prove that no subtype relation exists."
        )
        if len(_json(response).encode()) > budget:
            raise ValueError("Query response exceeds output byte budget")
        return response

    async def get_file_imports(
        self,
        file_path,
        repo_url,
        *,
        snapshot_id,
        evidence_types=None,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Files that this file's import statements were linked to."""
        return await self._file_links(
            "IMPORTS",
            file_path,
            repo_url,
            snapshot_id,
            limit,
            max_output_bytes,
            timeout,
            evidence=evidence_types,
        )

    async def get_file_importers(
        self,
        file_path,
        repo_url,
        *,
        snapshot_id,
        evidence_types=None,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Files whose import statements were linked to this file."""
        return await self._file_links(
            "IMPORTS",
            file_path,
            repo_url,
            snapshot_id,
            limit,
            max_output_bytes,
            timeout,
            evidence=evidence_types,
            incoming=True,
        )

    async def get_references(
        self,
        native_symbol,
        repo_url,
        *,
        snapshot_id,
        file_path=None,
        after_reference_id="",
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """SCIP occurrences of one native symbol, in reference_id order.

        Local symbols are unique only within a document, so they need file_path.
        Continue a truncated listing with the last returned reference_id.
        """
        _text(native_symbol, "native_symbol")
        if file_path is not None:
            _text(file_path, "file_path")
        if native_symbol.startswith("local ") and file_path is None:
            raise ValueError("Local SCIP symbols require file_path document scope")
        if not isinstance(after_reference_id, str):
            raise ValueError("Reference cursor must be a string")
        return await self._read(
            """MATCH (r:Reference {repo_url:$repo_url,snapshot_id:$snapshot_id,native_id:$native_symbol})
            WHERE ($file_path IS NULL OR r.file_path=$file_path) AND r.reference_id > $after
            OPTIONAL MATCH (r)-[e:REFERENCES]->(d:Symbol {repo_url:$repo_url,snapshot_id:$snapshot_id})
            WITH r,e,d ORDER BY r.reference_id,d.definition_id
            WITH r,collect(CASE WHEN d IS NULL THEN NULL ELSE {definition_id:d.definition_id,
                qualified_name:d.qualified_name,file_path:d.file_path,start_line:d.start_line,
                edge_id:e._dt_edge_id} END) AS targets
            ORDER BY r.reference_id
            RETURN r {.reference_id,.native_id,.file_path,.start_line,.column,.end_line,
                .end_column,.coordinate_encoding,.symbol_roles,.resolution,
                .declaration_resolution} AS reference,targets
            LIMIT $fetch_limit""",
            {
                "native_symbol": native_symbol,
                "file_path": file_path,
                "after": after_reference_id,
            },
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
            required_capability="references",
        )

    async def _dependencies(
        self,
        file_path,
        repo_url,
        snapshot_id,
        limit,
        max_output_bytes,
        timeout,
        incoming,
    ):
        _text(file_path, "file_path")
        other = "(other:File {repo_url:$repo_url,snapshot_id:$snapshot_id})"
        pattern = (
            f"{other}-[r:DEPENDS_ON]->(f)"
            if incoming
            else f"(f)-[r:DEPENDS_ON]->{other}"
        )
        return await self._read(
            f"""MATCH (f:File {{repo_url:$repo_url,snapshot_id:$snapshot_id,path:$file_path}})
            MATCH {pattern}
            WHERE r.relation='references'
            RETURN other.path AS file,r.dependency_id AS dependency_id,
                r._dt_edge_id AS edge_id,r.evidence_type AS evidence_type,
                r.rule_id AS rule_id,size(r.witness_ids) AS witness_count,
                r.witness_ids[0..20] AS witness_ids
            ORDER BY other.path,r.dependency_id LIMIT $fetch_limit""",
            {"file_path": file_path},
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
            required_capability="dependencies",
        )

    async def get_file_dependencies(
        self,
        file_path,
        repo_url,
        *,
        snapshot_id,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Files declaring symbols this file references, with up to 20 witnesses."""
        return await self._dependencies(
            file_path, repo_url, snapshot_id, limit, max_output_bytes, timeout, False
        )

    async def get_file_dependents(
        self,
        file_path,
        repo_url,
        *,
        snapshot_id,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Files that reference symbols declared in this file."""
        return await self._dependencies(
            file_path, repo_url, snapshot_id, limit, max_output_bytes, timeout, True
        )

    async def _implementations(
        self,
        native_symbol,
        repo_url,
        snapshot_id,
        file_path,
        limit,
        max_output_bytes,
        timeout,
        incoming,
    ):
        _text(native_symbol, "native_symbol")
        if file_path is not None:
            _text(file_path, "file_path")
        if native_symbol.startswith("local ") and file_path is None:
            raise ValueError("Local SCIP symbols require file_path document scope")
        pattern = (
            "(other)-[r:IMPLEMENTS]->(s)" if incoming else "(s)-[r:IMPLEMENTS]->(other)"
        )
        return await self._read(
            f"""MATCH (s:Symbol {{repo_url:$repo_url,snapshot_id:$snapshot_id,native_id:$native_symbol}})
            WHERE $file_path IS NULL OR s.file_path=$file_path
            MATCH {pattern}
            WHERE other.repo_url=$repo_url AND other.snapshot_id=$snapshot_id
            RETURN other {{.qualified_name,.native_id,.display_name,.kind,.file_path,
                .start_line,.column}} AS symbol,r._dt_edge_id AS edge_id,
                r.evidence_type AS evidence_type,r.rule_id AS rule_id
            ORDER BY other.file_path,other.start_line,other.qualified_name,
                s.qualified_name
            LIMIT $fetch_limit""",
            {"native_symbol": native_symbol, "file_path": file_path},
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
            required_capability="implementations",
        )

    async def get_implementations(
        self,
        native_symbol,
        repo_url,
        *,
        snapshot_id,
        file_path=None,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Symbols the index records as implementing, extending or overriding it."""
        return await self._implementations(
            native_symbol,
            repo_url,
            snapshot_id,
            file_path,
            limit,
            max_output_bytes,
            timeout,
            True,
        )

    async def get_supertypes(
        self,
        native_symbol,
        repo_url,
        *,
        snapshot_id,
        file_path=None,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Symbols this symbol implements, extends or overrides, per the index."""
        return await self._implementations(
            native_symbol,
            repo_url,
            snapshot_id,
            file_path,
            limit,
            max_output_bytes,
            timeout,
            False,
        )

    async def get_file_exports(
        self,
        file_path,
        repo_url,
        *,
        snapshot_id,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        return await self._file_links(
            "EXPORTS",
            file_path,
            repo_url,
            snapshot_id,
            limit,
            max_output_bytes,
            timeout,
        )

    async def list_components(
        self,
        repo_url,
        *,
        snapshot_id,
        kind=None,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Components that manifests declare, with their files' count."""
        if kind is not None:
            _text(kind, "kind")
        response = await self._read(
            "MATCH (c:Component {repo_url:$repo_url,snapshot_id:$snapshot_id}) "
            "WHERE $kind IS NULL OR c.kind = $kind "
            "OPTIONAL MATCH (f:File {repo_url:$repo_url,snapshot_id:$snapshot_id})"
            "-[:PART_OF]->(c) "
            "WITH c, count(f) AS files "
            "RETURN c AS element, files "
            "ORDER BY c.root, c.kind, c.qualified_name LIMIT $fetch_limit",
            {"kind": kind},
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
            required_capability="components",
        )
        response["semantics"] = (
            "Components declared by build manifests (pyproject.toml, package.json, "
            "go.mod, Cargo.toml, pom.xml) and Compose files in the analyzed "
            "paths; units that no manifest declares are not listed."
        )
        return response

    async def get_file_components(
        self,
        file_path,
        repo_url,
        *,
        snapshot_id,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """The nearest enclosing component of each kind for this file."""
        return await self._file_links(
            "PART_OF",
            file_path,
            repo_url,
            snapshot_id,
            limit,
            max_output_bytes,
            timeout,
        )

    async def get_component_dependencies(
        self,
        qualified_name,
        repo_url,
        *,
        snapshot_id,
        incoming=False,
        evidence_types=None,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Components this one's manifest requires, or (``incoming``) those
        whose manifests require it, with the declaration and rule."""
        _text(qualified_name, "qualified_name")
        evidence = _evidence_filter(evidence_types)
        pattern = "<-[r:REQUIRES]-" if incoming else "-[r:REQUIRES]->"
        response = await self._read(
            "MATCH (c:Component {qualified_name:$name,repo_url:$repo_url,"
            "snapshot_id:$snapshot_id})"
            + pattern
            + "(other:Component {repo_url:$repo_url,snapshot_id:$snapshot_id}) "
            "WHERE $evidence IS NULL OR r.evidence_type IN $evidence "
            "RETURN other AS element, r AS relationship "
            "ORDER BY other.root, other.qualified_name, r.section, r._dt_edge_id "
            "LIMIT $fetch_limit",
            {"name": qualified_name, "evidence": evidence},
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
            required_capability="components",
        )
        response["evidence_types"] = evidence
        return self._observed(
            response, ("incoming " if incoming else "") + "REQUIRES", max_output_bytes
        )

    async def get_shared_tables(
        self,
        repo_url,
        *,
        snapshot_id,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Database tables that code in more than one package declares, maps,
        reads or writes: shared-database coupling, which no API contract
        shows. Each table lists every package touching it (the nearest
        package of the code, as for USES_CONTRACT_OF) with its roles; tables
        written from several packages come first."""
        response = await self._read(
            "MATCH (t:DbTable {repo_url:$repo_url,snapshot_id:$snapshot_id})"
            "<-[r:DECLARES|MAPS_TO|READS|WRITES]-(code) "
            "WHERE code.repo_url=$repo_url AND code.snapshot_id=$snapshot_id "
            "WITH t, r, coalesce(code.path, code.file_path) AS path "
            "MATCH (f:File {repo_url:$repo_url,snapshot_id:$snapshot_id,path:path})"
            "-[:PART_OF]->(c:Component {repo_url:$repo_url,snapshot_id:$snapshot_id}) "
            "WHERE c.kind IN $package_kinds "
            # The nearest package: the deepest of the roots enclosing the file.
            "WITH t, r, path, c ORDER BY size(coalesce(c.root, '')) DESC "
            "WITH t, r, path, collect(c) AS enclosing "
            "UNWIND [c IN enclosing WHERE size(coalesce(c.root, '')) = "
            "size(coalesce(enclosing[0].root, ''))] AS c "
            "WITH t, c, type(r) AS role, count(r) AS uses "
            "ORDER BY role "
            "WITH t, c, collect({role:role, count:uses}) AS roles "
            "ORDER BY c.qualified_name "
            "WITH t, collect({qualified_name:c.qualified_name, kind:c.kind, "
            "name:c.display_name, roles:roles}) AS packages "
            "WHERE size(packages) > 1 "
            "WITH t, packages, size([p IN packages WHERE "
            "any(x IN p.roles WHERE x.role = 'WRITES')]) AS writers "
            "RETURN t {.qualified_name,.name,.scope} AS element, packages, "
            "writers "
            "ORDER BY writers DESC, size(packages) DESC, t.qualified_name "
            "LIMIT $fetch_limit",
            {"package_kinds": sorted(PACKAGE_KINDS)},
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
            required_capability="data-contracts",
        )
        response["semantics"] = (
            "Tables (DbTable contracts) that code in two or more packages "
            "DECLARES, MAPS_TO, READS or WRITES, with each package's roles and "
            "edge counts. Tables are the same only within one namespace (a "
            "shared persistent spring.datasource.url, else one package), so "
            "packages sharing a database without a common datasource URL are "
            "not joined. Requires the components tool in the same snapshot."
        )
        return response

    async def get_component_files(
        self,
        qualified_name,
        repo_url,
        *,
        snapshot_id,
        after_path="",
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Files under a component's root, by path, after ``after_path``."""
        _text(qualified_name, "qualified_name")
        if not isinstance(after_path, str):
            raise ValueError("after_path must be a string")
        response = await self._read(
            "MATCH (c:Component {qualified_name:$name,repo_url:$repo_url,"
            "snapshot_id:$snapshot_id})"
            "<-[r:PART_OF]-(f:File {repo_url:$repo_url,snapshot_id:$snapshot_id}) "
            "WHERE f.path > $after "
            "RETURN f.path AS path, f.language AS language, r AS relationship "
            "ORDER BY f.path LIMIT $fetch_limit",
            {"name": qualified_name, "after": after_path},
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
            required_capability="components",
        )
        if response["items"]:
            response["next_after_path"] = response["items"][-1]["path"]
        return self._observed(response, "incoming PART_OF", max_output_bytes)

    async def _file_links(
        self,
        relation,
        path,
        repo_url,
        snapshot_id,
        limit,
        budget,
        timeout,
        *,
        evidence=None,
        incoming=False,
    ):
        _text(path, "file_path")
        evidence = _evidence_filter(evidence)
        pattern = "<-[r:" + relation + "]-" if incoming else "-[r:" + relation + "]->"
        response = await self._read(
            "MATCH (f:File {path:$path,repo_url:$repo_url,snapshot_id:$snapshot_id})"
            + pattern
            + "(other {repo_url:$repo_url,snapshot_id:$snapshot_id}) "
            "WHERE $evidence IS NULL OR r.evidence_type IN $evidence "
            "RETURN other AS element,r AS relationship "
            "ORDER BY other.qualified_name,r.line,r._dt_edge_id LIMIT $fetch_limit",
            {"path": path, "evidence": evidence},
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=budget,
            timeout=timeout,
        )
        response["evidence_types"] = evidence
        return self._observed(
            response, ("incoming " if incoming else "") + relation, budget
        )

    @staticmethod
    def _observed(response, relation, budget):
        if not response["items"]:
            response["status"] = "unsupported_or_unobserved"
        response["semantics"] = (
            "Observed "
            + relation
            + " evidence only; absence does not establish completeness."
        )
        if len(_json(response).encode()) > budget:
            raise ValueError("Query response exceeds output byte budget")
        return response

    async def explain_edge(
        self,
        edge_id,
        repo_url,
        *,
        snapshot_id,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Explain one stored edge: its endpoints, location, evidence and rule.

        edge_id is the stable ``edge_id`` that edge query results return. The
        stored edge carries its evidence type and rule ID; the rule's description
        comes from the rules registered by this installed version.
        """
        if not isinstance(edge_id, str) or not re.fullmatch(r"[0-9a-f]{64}", edge_id):
            raise ValueError("edge_id must be a 64-character lowercase hex identity")
        # Importing the tools registers their rules in this process.
        from . import analysis_tools  # noqa: F401
        from .analysis_tools.evidence import EVIDENCE_MEANINGS, registered_rules

        summary = (
            "{.qualified_name,.name,.kind,.path,.file_path,.start_line,.end_line,"
            "labels:labels(NODE)}"
        )
        response = await self._read(
            "MATCH (source:GraphNode {repo_url:$repo_url,snapshot_id:$snapshot_id})"
            "-[r]->(target:GraphNode {repo_url:$repo_url,snapshot_id:$snapshot_id}) "
            "WHERE r._dt_edge_id=$edge_id "
            "RETURN source " + summary.replace("NODE", "source") + " AS source,"
            "r AS relationship,"
            "target " + summary.replace("NODE", "target") + " AS target "
            "LIMIT $fetch_limit",
            {"edge_id": edge_id},
            repo_url,
            snapshot_id,
            limit=1,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
        )
        rules = registered_rules()
        items = []
        for row in response["items"]:
            source = {k: v for k, v in row["source"].items() if v is not None}
            target = {k: v for k, v in row["target"].items() if v is not None}
            relationship = row["relationship"]
            properties = relationship["properties"]
            evidence_type = properties.get("evidence_type")
            rule_id = properties.get("rule_id")
            rule = rules.get(rule_id)
            if rule is None:
                rule_status = "unregistered"
            elif rule.evidence_type != evidence_type:
                rule_status = "conflicts_with_registry"
            else:
                rule_status = "registered"
            location = {
                "file": source.get("file_path")
                or source.get("path")
                or target.get("file_path")
                or target.get("path"),
                "line": properties.get(
                    "line",
                    target.get("start_line")
                    if relationship["type"] == "DEFINES"
                    else None,
                ),
                "column": properties.get("column"),
            }
            place = location["file"] or ""
            if location["line"] is not None:
                place += ":" + str(location["line"])
                if location["column"] is not None:
                    place += ":" + str(location["column"])
            statement = (
                f"{relationship['type']} from "
                f"{source.get('name') or source['qualified_name']} to "
                f"{target.get('name') or target['qualified_name']}"
                + (f" at {place}" if place else "")
                + f". Evidence: {evidence_type}, by rule {rule_id}"
                + (f": {rule.description}" if rule_status == "registered" else "")
                + "."
            )
            items.append(
                {
                    "edge_id": relationship["edge_id"],
                    "type": relationship["type"],
                    "source": source,
                    "target": target,
                    "properties": properties,
                    "location": location,
                    "evidence": {
                        "evidence_type": evidence_type,
                        "meaning": EVIDENCE_MEANINGS.get(evidence_type),
                        "rule_id": rule_id,
                        "rule_description": rule.description if rule else None,
                        "rule_status": rule_status,
                    },
                    "statement": statement,
                }
            )
        response["items"] = items
        if not items:
            response["status"] = "not_found"
        response["semantics"] = (
            "Rule descriptions come from the rules registered by this installed "
            "version; the stored edge records only its evidence type and rule ID."
        )
        if len(_json(response).encode()) > max_output_bytes:
            raise ValueError("Query response exceeds output byte budget")
        return response

    async def get_class_hierarchy(
        self,
        class_qname,
        repo_url,
        *,
        snapshot_id,
        max_depth=5,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Port INHERITS_FROM ancestry/descendancy with a finite depth bound.

        Classes and interfaces both take part, so TypeScript ``implements`` and
        interface ``extends`` edges are followed too.
        """
        _text(class_qname, "class_qname")
        if type(max_depth) is not int or not 1 <= max_depth <= 10:
            raise ValueError("max_depth must be an integer from 1 to 10")
        statement = """MATCH (c:Symbol {qualified_name:$qname,repo_url:$repo_url,snapshot_id:$snapshot_id})
            WHERE c:Class OR c:Interface
            CALL {
                WITH c MATCH p=(c)-[:INHERITS_FROM*1..DEPTH]->(other)
                WHERE other:Class OR other:Interface
                RETURN p,other,'ancestor' AS direction
                UNION ALL
                WITH c MATCH p=(c)<-[:INHERITS_FROM*1..DEPTH]-(other)
                WHERE other:Class OR other:Interface
                RETURN p,other,'descendant' AS direction
            }
            WITH p,other,direction
            WHERE all(n IN nodes(p) WHERE n.repo_url=$repo_url AND n.snapshot_id=$snapshot_id)
            RETURN other AS element,p AS path,direction
            ORDER BY direction,length(p),other.qualified_name LIMIT $fetch_limit""".replace(
            "DEPTH", str(max_depth)
        )
        response = await self._read(
            statement,
            {"qname": class_qname},
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
        )
        return self._observed(response, "INHERITS_FROM", max_output_bytes)

    async def trace_execution_paths(
        self,
        start_function,
        end_function,
        repo_url,
        *,
        snapshot_id,
        max_depth=5,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Port original UNIFIED_EDGE shortest-path query, without inventing flows."""
        _text(start_function, "start_function")
        _text(end_function, "end_function")
        if type(max_depth) is not int or not 1 <= max_depth <= 10:
            raise ValueError("max_depth must be an integer from 1 to 10")
        statement = """MATCH p=shortestPath(
            (start:Symbol {qualified_name:$start,repo_url:$repo_url,snapshot_id:$snapshot_id})
            -[:UNIFIED_EDGE*1..DEPTH]->
            (end:Symbol {qualified_name:$end,repo_url:$repo_url,snapshot_id:$snapshot_id}))
            WHERE all(n IN nodes(p) WHERE n.repo_url=$repo_url AND n.snapshot_id=$snapshot_id)
              AND all(r IN relationships(p) WHERE r.dimension IN ['CFG','CALL'])
            RETURN p AS path,length(p) AS depth
            ORDER BY depth LIMIT $fetch_limit""".replace("DEPTH", str(max_depth))
        response = await self._read(
            statement,
            {"start": start_function, "end": end_function},
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
        )
        return self._observed(response, "UNIFIED_EDGE CFG/CALL", max_output_bytes)

    async def get_repository_stats(
        self, repo_url, *, snapshot_id, max_output_bytes=16000, timeout=5
    ):
        return await self._read(
            """MATCH (n:GraphNode {repo_url:$repo_url,snapshot_id:$snapshot_id})
            RETURN count(n) AS nodes,
                sum(CASE WHEN n:File THEN 1 ELSE 0 END) AS files,
                sum(CASE WHEN n:Function OR n:Method THEN 1 ELSE 0 END) AS functions,
                sum(CASE WHEN n:Class THEN 1 ELSE 0 END) AS classes,
                sum(CASE WHEN n:Variable THEN 1 ELSE 0 END) AS variables""",
            {},
            repo_url,
            snapshot_id,
            limit=1,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
        )

    async def get_unknown_symbols(
        self, repo_url, *, snapshot_id, limit=20, max_output_bytes=16000, timeout=5
    ):
        return await self._read(
            """MATCH (n:UnknownSymbol {repo_url:$repo_url,snapshot_id:$snapshot_id})
            RETURN n AS element ORDER BY n.qualified_name LIMIT $fetch_limit""",
            {},
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
        )

    async def find_symbol_by_name(
        self,
        symbol_name,
        repo_url,
        *,
        snapshot_id,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        _text(symbol_name, "symbol_name")
        return await self._read(
            """MATCH (n:Symbol {repo_url:$repo_url,snapshot_id:$snapshot_id})
            WHERE n.name=$name OR n.display_name=$name
            RETURN n AS element ORDER BY n.file_path,n.start_line,n.qualified_name LIMIT $fetch_limit""",
            {"name": symbol_name},
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
        )

    async def node_exists(
        self,
        qualified_name,
        repo_url,
        *,
        snapshot_id,
        node_type=None,
        max_output_bytes=16000,
        timeout=5,
    ):
        _text(qualified_name, "qualified_name")
        if node_type is not None:
            _text(node_type, "node_type")
        return await self._read(
            """MATCH (n:GraphNode {qualified_name:$qname,repo_url:$repo_url,snapshot_id:$snapshot_id})
            WHERE $node_type IS NULL OR $node_type IN labels(n) RETURN count(n)>0 AS exists""",
            {"qname": qualified_name, "node_type": node_type},
            repo_url,
            snapshot_id,
            limit=1,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
        )

    async def query_cypher(
        self,
        query,
        parameters,
        repo_url,
        *,
        snapshot_id,
        allow_database_scope=False,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """Execute bounded parameterized reads in a dedicated snapshot database.

        This is deliberately explicit opt-in: arbitrary Cypher is database-scoped,
        not automatically rewritten for repository/tenant isolation. Database read
        privileges/read-only configuration remain required for untrusted callers.
        The conservative lexical restrictions are defense in depth; the server's
        EXPLAIN query type and unconditional rollback provide additional checks.
        """
        _check_cypher(query, parameters, allow_database_scope)
        return await self._read(
            query,
            parameters,
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
            database_scope=True,
        )

    async def query_cypher_text(
        self,
        query,
        parameters,
        repo_url,
        *,
        snapshot_id,
        allow_database_scope=False,
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        """``query_cypher`` written as short sentences (see ``result_text``).

        The same rows are read; links whose end items the query did not return
        have those items looked up, and the snapshot's EdgeCoverage records
        give the closing coverage line. Neither lookup is journaled as a query.
        The byte budget applies to the text: whole lines past it are cut and
        the reply says so.
        """
        from .result_text import render

        _check_cypher(query, parameters, allow_database_scope)
        response = await self._read(
            query,
            parameters,
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
            database_scope=True,
            raw_output_bytes=_RAW_TEXT_BYTES,
        )
        known, wanted = set(), set()

        def visit(value):
            if isinstance(value, dict):
                if "labels" in value and "element_id" in value:
                    known.add(value["element_id"])
                if {"type", "source", "target"} <= set(value):
                    wanted.update((value["source"], value["target"]))
                for v in value.values():
                    visit(v)
            elif isinstance(value, list):
                for v in value:
                    visit(v)

        visit(response["items"])
        missing = sorted(wanted - known)
        nodes = {}
        for offset in range(0, len(missing), 100):
            page = await self._read(
                "MATCH (n) WHERE elementId(n) IN $ids RETURN n",
                {"ids": missing[offset : offset + 100]},
                repo_url,
                snapshot_id,
                limit=100,
                timeout=timeout,
                database_scope=True,
                observe=False,
                raw_output_bytes=_RAW_TEXT_BYTES,
            )
            nodes.update((r["n"]["element_id"], r["n"]) for r in page["items"])
        coverage = []
        for page_number in range(10):
            page = await self._read(
                "MATCH (e:EdgeCoverage) RETURN e.family AS family, e.protocol AS protocol,"
                " e.language AS language, e.status AS status, e.reason AS reason,"
                " e.unresolved_reasons AS unresolved_reasons"
                " ORDER BY family, protocol, language SKIP $skip LIMIT $fetch_limit",
                {"skip": page_number * 100},
                repo_url,
                snapshot_id,
                limit=100,
                timeout=timeout,
                database_scope=True,
                observe=False,
                raw_output_bytes=_RAW_TEXT_BYTES,
            )
            coverage.extend(page["items"])
            if not page["truncated"]:
                break
        text = render(
            response, nodes=nodes, coverage=coverage, query=query, limit=limit
        )
        if len(text.encode()) > max_output_bytes:
            lines = text.split("\n")
            kept, size = [], 0
            note = "... {} more lines cut at the {}-byte budget: narrow the query."
            for line in lines:
                size += len(line.encode()) + 1
                if size + len(note.encode()) + 16 > max_output_bytes:
                    break
                kept.append(line)
            kept.append(note.format(len(lines) - len(kept), max_output_bytes))
            text = "\n".join(kept)
        return text
