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


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


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


def _value(value):
    """Preserve actual graph properties and paths rather than discarding scalars."""
    from neo4j.graph import Node, Path, Relationship
    from .storage.graph.encoding import decode_properties

    if isinstance(value, Node):
        return {
            "element_id": value.element_id,
            "labels": sorted(value.labels),
            "properties": decode_properties(dict(value)),
        }
    if isinstance(value, Relationship):
        return {
            "element_id": value.element_id,
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


class GraphQueryService:
    """Single query implementation shared by programmatic and agent callers.

    Typed reads always require an explicitly selected complete repository snapshot.
    Raw Cypher is an opt-in *database-scoped* capability, not automatic tenant
    isolation. It requires an exclusively owned single-snapshot database. Reads
    run in explicit transactions that are always rolled back. The database should
    additionally enforce read-only access; routing mode alone is not security.
    """

    def __init__(self, driver, database="neo4j", *, observer=None):
        self.driver = driver
        self.database = _text(database, "database")
        self.observer = observer

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
    ):
        _text(repo_url, "repo_url")
        _text(snapshot_id, "snapshot_id")
        _bounds(limit, max_output_bytes, timeout)
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
                marker_result = await tx.run(
                    "MATCH (s:AnalysisSnapshot {repo_url:$repo_url,snapshot_id:$snapshot_id,status:'complete'}) RETURN s.metadata_json AS metadata,s.content_hash AS content_hash",
                    repo_url=repo_url,
                    snapshot_id=snapshot_id,
                )
                marker = await marker_result.single()
                if marker is None:
                    raise ValueError(
                        "Selected repository snapshot is missing or incomplete"
                    )
                metadata = json.loads(marker["metadata"] or "{}")
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
                    "scope": "database" if database_scope else "repository_snapshot",
                    "capabilities": metrics.get("capabilities", []),
                    "analysis_status": metrics.get("analysis_status", "unknown"),
                    "items": [],
                    "returned": 0,
                    "truncated": False,
                }
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
                if len(_json(response).encode()) > max_output_bytes:
                    raise ValueError("Query metadata exceeds the output byte budget")
                # Optional evidence hook shared by CLI, MCP and embedded callers.
                # Emit only after a real Neo4j read; no raw query or parameters.
                if self.observer is not None:
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
            statement = """CALL db.index.fulltext.queryNodes('code_search',$query) YIELD node,score
                WHERE node.repo_url=$repo_url AND node.snapshot_id=$snapshot_id
                  AND score > 0.1 AND ($file_path IS NULL OR node.file_path=$file_path)
                RETURN node {.qualified_name,.definition_id,.name,.display_name,.kind,
                    .file_path,.start_line,.end_line,.column,.end_column} AS element,score
                ORDER BY score DESC,element.file_path,element.start_line,element.qualified_name
                LIMIT $fetch_limit"""
        else:
            statement = """MATCH (node:Symbol {repo_url:$repo_url,snapshot_id:$snapshot_id})
                WHERE $file_path IS NULL OR node.file_path=$file_path
                RETURN node {.qualified_name,.definition_id,.name,.display_name,.kind,
                    .file_path,.start_line,.end_line,.column,.end_column} AS element
                ORDER BY element.file_path,element.start_line,element.qualified_name
                LIMIT $fetch_limit"""
        return await self._read(
            statement,
            {"query": query, "file_path": file_path},
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
        limit=20,
        max_output_bytes=16000,
        timeout=5,
        include_relationships=True,
    ):
        """Port File -> DEFINES context lookup; innermost containing scope first."""
        _text(file_path, "file_path")
        if line_number is not None and (
            type(line_number) is not int or line_number < 1
        ):
            raise ValueError("line_number must be a positive integer or None")
        response = await self._read(
            """MATCH (f:File {repo_url:$repo_url,snapshot_id:$snapshot_id,path:$file_path})
            -[:DEFINES]->(elem:Symbol {repo_url:$repo_url,snapshot_id:$snapshot_id})
            WHERE $line_number IS NULL OR (elem.start_line <= $line_number AND elem.end_line >= $line_number)
            RETURN elem {.qualified_name,.definition_id,.name,.display_name,.kind,
                .file_path,.start_line,.end_line,.column,.end_column} AS element
            ORDER BY CASE WHEN $line_number IS NULL THEN elem.start_line ELSE elem.end_line-elem.start_line END,
                elem.start_line,elem.qualified_name LIMIT $fetch_limit""",
            {"file_path": file_path, "line_number": line_number},
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
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        return await self._calls(
            function_qname,
            repo_url,
            snapshot_id,
            False,
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
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        return await self._calls(
            function_qname,
            repo_url,
            snapshot_id,
            True,
            limit,
            max_output_bytes,
            timeout,
        )

    async def _calls(
        self, qname, repo_url, snapshot_id, incoming, limit, budget, timeout
    ):
        _text(qname, "function_qname")
        pattern = (
            "(other)-[r:CALLS]->(func)" if incoming else "(func)-[r:CALLS]->(other)"
        )
        response = await self._read(
            "MATCH (func:Symbol {repo_url:$repo_url,snapshot_id:$snapshot_id,qualified_name:$qname}) "
            "MATCH "
            + pattern
            + " WHERE other.repo_url=$repo_url AND other.snapshot_id=$snapshot_id "
            "RETURN other {.qualified_name,.name,.kind,.file_path,.start_line} AS element,r AS relationship "
            "ORDER BY other.qualified_name LIMIT $fetch_limit",
            {"qname": qname},
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=budget,
            timeout=timeout,
        )
        if not response["items"] and "calls" not in response["capabilities"]:
            response["status"] = "unsupported_or_unobserved"
        response["semantics"] = (
            "Observed CALLS edges only; absence does not prove that no calls exist."
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
        limit=20,
        max_output_bytes=16000,
        timeout=5,
    ):
        return await self._file_links(
            "IMPORTS",
            file_path,
            repo_url,
            snapshot_id,
            limit,
            max_output_bytes,
            timeout,
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

    async def _file_links(
        self, relation, path, repo_url, snapshot_id, limit, budget, timeout
    ):
        _text(path, "file_path")
        response = await self._read(
            "MATCH (f:File {path:$path,repo_url:$repo_url,snapshot_id:$snapshot_id}) "
            "-[r:"
            + relation
            + "]->(other {repo_url:$repo_url,snapshot_id:$snapshot_id}) "
            "RETURN other AS element,r AS relationship ORDER BY other.qualified_name LIMIT $fetch_limit",
            {"path": path},
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=budget,
            timeout=timeout,
        )
        return self._observed(response, relation, budget)

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
        """Port INHERITS_FROM ancestry/descendancy with a finite depth bound."""
        _text(class_qname, "class_qname")
        if type(max_depth) is not int or not 1 <= max_depth <= 10:
            raise ValueError("max_depth must be an integer from 1 to 10")
        statement = """MATCH (c:Class {qualified_name:$qname,repo_url:$repo_url,snapshot_id:$snapshot_id})
            CALL {
                WITH c MATCH p=(c)-[:INHERITS_FROM*1..DEPTH]->(other:Class)
                RETURN p,other,'ancestor' AS direction
                UNION ALL
                WITH c MATCH p=(c)<-[:INHERITS_FROM*1..DEPTH]-(other:Class)
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
        if not re.match(
            r"\s*(MATCH|OPTIONAL\s+MATCH|RETURN|WITH|UNWIND)\b", query, re.I
        ):
            raise ValueError("Only a single read query is accepted")
        if re.search(
            r"\b(CALL|LOAD|CSV|CREATE|MERGE|SET|DELETE|REMOVE|DROP|ALTER|GRANT|DENY|REVOKE|FOREACH|SHOW|USE|START|STOP|TERMINATE)\b|;",
            query,
            re.I,
        ):
            raise ValueError(
                "Procedures, updates, administration and multiple statements are disabled"
            )
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
