"""Scoped Neo4j reads ported from DeepThought's GraphQueryService/ContextService.

Original MIT sources: libs/analysis/.../engine/data_access/graph_repository.py
and libs/core/.../context/context_service.py; attribution is retained in NOTICE.
The service owns graph reads; CLI/MCP adapters must not duplicate Cypher logic.
Neo4j is imported only at the database feature boundary.
"""

import json
import asyncio
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


def _compact_references(response):
    """Factor repeated evidence while retaining every occurrence property."""
    references = [item["reference"] for item in response["items"]]
    shared = {}
    if references:
        shared = {
            key: value
            for key, value in references[0].items()
            if key
            in {
                "native_id",
                "coordinate_encoding",
                "symbol_roles",
                "resolution",
                "declaration_resolution",
                "target_ids",
            }
            and all(key in ref and ref[key] == value for ref in references)
        }
    return {
        **response,
        "response_format": "references-v2",
        "reference_common": shared,
        "items": [
            {
                "reference": {
                    key: value for key, value in ref.items() if key not in shared
                }
            }
            for ref in references
        ],
        "next_cursor": references[-1]["reference_id"]
        if response["truncated"] and references
        else None,
    }


def _source_references(response, scope, parameters, limit, budget):
    from .typescript_reference_evidence import digest

    if not response["items"]:
        return {
            **response,
            "source_verification": {
                "status": "unavailable",
                "reason": "No indexed symbol evidence",
            },
            "next_cursor": None,
        }
    if len(response["items"]) > 1 or (
        parameters.get("export_name") is not None and response["truncated"]
    ):
        return {
            **response,
            "items": [
                {
                    "native_id": item["evidence"]["native_id"],
                    "exports": item["evidence"]["exports"],
                }
                for item in response["items"]
            ],
            "source_verification": {
                "status": "ambiguous",
                "reason": "Qualify the exported name with export_file",
            },
            "next_cursor": None,
        }
    evidence = response["items"][0]["evidence"]
    references = evidence["references"]
    if digest(references) != evidence["references_sha256"]:
        raise ValueError("Compiler reference evidence integrity mismatch")
    matches = [
        ref
        for ref in references
        if ref["reference_id"] > parameters["after"]
        and (
            parameters["file_path"] is None
            or ref["file_path"] == parameters["file_path"]
        )
    ]
    rows = matches[:limit]
    verified = evidence["verification_status"] == "checked"
    output = {
        **response,
        "response_format": "source-references-v1",
        "reference_common": {
            "native_id": evidence["native_id"],
            "coordinate_encoding": "utf-16",
            "resolution": "resolved" if verified else "unverified",
            "target_ids": evidence["target_ids"],
        },
        "source_verification": {
            "status": "prepared" if verified else "unavailable",
            "freshness": "unverified_at_query",
            "profile": scope["profile"],
            "compiler_version": scope["compiler_version"],
            "diagnostics": 0,
            "selected_files": len(scope["files"]),
            "files_sha256": scope["files_sha256"],
            "source_sha256": scope["source_sha256"],
            "compiler_sha256": scope["compiler_sha256"],
            "scope_definition": scope["scope_definition"],
            "total_references": len(matches),
            "comparison": {
                key: evidence[key]
                for key in ["indexed_count", "missing_in_scip", "extra_in_scip"]
            },
            "reference_set_sha256": evidence["references_sha256"],
        },
    }
    while True:
        output.update(
            items=[{"reference": ref} for ref in rows],
            returned=len(rows),
            truncated=len(rows) < len(matches),
            next_cursor=rows[-1]["reference_id"]
            if rows and len(rows) < len(matches)
            else None,
        )
        if len(_json(output).encode()) + 512 <= budget:
            return output
        if not rows:
            raise ValueError("Source verification metadata exceeds output budget")
        rows.pop()
        if not rows and matches:
            raise ValueError("First verified reference exceeds output budget")


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
        result = {str(k): _value(v) for k, v in value.items()}
        if "_dt_json_fields" in result or "_dt_null_fields" in result:
            for marker in ("_dt_json_fields", "_dt_null_fields"):
                result[marker] = [
                    key for key in (result.get(marker) or []) if key in result
                ]
            return decode_properties(result)
        return result
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

    def __init__(
        self, driver, database="neo4j", *, observer=None, reference_validator=None
    ):
        self.driver = driver
        self.database = _text(database, "database")
        self.observer = observer
        self.reference_validator = reference_validator

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
        required_capability=None,
        format_response=None,
        response_format=None,
        evidence_query=None,
        postprocess=None,
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
                metrics = metadata.get("custom_metrics", {})
                if evidence_query is not None and "verified_references" in metrics.get(
                    "capabilities", []
                ):
                    sources = metrics.get("tool_outputs", [metadata])
                    scope = next(
                        item["custom_metrics"]["reference_verification"]
                        for item in sources
                        if item["tool_name"] == "scip"
                    )
                    query = evidence_query

                    def format_response(value):
                        return _source_references(
                            value, scope, parameters, limit, max_output_bytes
                        )

                if (
                    required_capability is not None
                    and required_capability not in metrics.get("capabilities", [])
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
                    "scope": "database" if database_scope else "repository_snapshot",
                    "capabilities": metrics.get("capabilities", []),
                    "analysis_status": metrics.get("analysis_status", "unknown"),
                    "items": [],
                    "returned": 0,
                    "truncated": False,
                }
                if response_format is not None:
                    response["response_format"] = response_format
                if required_capability is not None:
                    sources = metrics.get("tool_outputs", [metadata])
                    response["reference_coverage"] = [
                        {
                            "tool": item.get("tool_name"),
                            "graph_status": item.get("custom_metrics", {}).get(
                                "graph_status", "unknown"
                            ),
                            "resolution_counts": item.get("custom_metrics", {}).get(
                                "resolution_counts", {}
                            ),
                            "resolution_semantics": item.get("custom_metrics", {}).get(
                                "resolution_semantics", "declaration_candidates_v1"
                            ),
                            "indexed_files": item.get("files_processed"),
                            "source_binding": {
                                "receipt_sha256": item.get("custom_metrics", {}).get(
                                    "source_receipt_sha256"
                                ),
                                "checked_at": item.get("timestamp"),
                                "workspace_freshness": "unverified_at_query",
                            },
                        }
                        for item in sources
                        if "references"
                        in item.get("custom_metrics", {}).get("capabilities", [])
                    ]
                async for record in result:
                    if len(response["items"]) >= limit:
                        response["truncated"] = True
                        break
                    item = {key: _value(value) for key, value in record.items()}
                    response["items"].append(item)
                    response["returned"] = len(response["items"])
                    # Budget the actual response, including a possible continuation cursor.
                    budget_response = (
                        format_response({**response, "truncated": True})
                        if format_response is not None
                        else response
                    )
                    if len(_json(budget_response).encode()) + 1 > max_output_bytes:
                        response["items"].pop()
                        response["returned"] -= 1
                        response["truncated"] = True
                        break
                if format_response is not None:
                    if response["truncated"] and not response["items"]:
                        raise ValueError("First result exceeds the output byte budget")
                    response = format_response(response)
                if len(_json(response).encode()) > max_output_bytes:
                    raise ValueError("Query metadata exceeds the output byte budget")
            finally:
                await tx.rollback()
            if postprocess is not None:
                response = await postprocess(response, metadata)
            if len(_json(response).encode()) > max_output_bytes:
                raise ValueError("Query metadata exceeds the output byte budget")
            if self.observer is not None:
                encoded = _json(response).encode()
                self.observer(
                    {
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
                        "wall_seconds": time.monotonic() - started,
                    }
                )
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
            statement = """CALL db.index.fulltext.queryNodes('code_search',$query) YIELD node,score
                WHERE node.repo_url=$repo_url AND node.snapshot_id=$snapshot_id
                  AND score > 0.1 AND ($file_path IS NULL OR node.file_path=$file_path)
                RETURN node {.qualified_name,.definition_id,.name,.display_name,.kind,
                .file_path,.start_line,.end_line,.column,.end_column,.native_id} AS element,score
                ORDER BY score DESC,element.file_path,element.start_line,element.qualified_name
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
            {"query": query, "file_path": file_path},
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
        )

    async def _source_check(self, repo_url):
        before, failure = None, None
        if self.reference_validator is not None:
            try:
                before = await asyncio.to_thread(
                    self.reference_validator.capture, repo_url
                )
            except (ValueError, OSError) as exc:
                failure = str(exc)[:300]

        async def verify_source(response, metadata):
            verification = response.get("source_verification")
            if verification is None:
                return response
            try:
                if failure is not None or self.reference_validator is None:
                    raise ValueError(
                        failure or "No trusted source workspace configured"
                    )
                await asyncio.to_thread(
                    self.reference_validator.validate, before, metadata, repo_url
                )
                verification["freshness"] = "checked_before_and_after_query"
                for coverage in response.get("reference_coverage", []):
                    if coverage["tool"] == "scip":
                        coverage["source_binding"]["workspace_freshness"] = (
                            "checked_before_and_after_query"
                        )
                if verification["status"] == "prepared":
                    verification["status"] = "verified"
            except (ValueError, OSError, KeyError, StopIteration) as exc:
                verification.update(status="unverified", reason=str(exc)[:300])
            if (
                verification["status"] == "verified"
                and response.get("response_format") == "symbol-context-v1"
            ):
                await asyncio.to_thread(
                    self.reference_validator.validate_context, before, response["items"]
                )
            return response

        return verify_source

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
        compact=False,
        export_name=None,
        export_file=None,
    ):
        """Exact native SCIP occurrences; local symbols require document scope."""
        if export_name is not None:
            _text(export_name, "export_name")
            if native_symbol is not None or not compact:
                raise ValueError(
                    "Export lookup requires compact source evidence and no native_symbol"
                )
            if export_file is not None:
                _text(export_file, "export_file")
        else:
            _text(native_symbol, "native_symbol")
            if export_file is not None:
                raise ValueError("export_file requires export_name")
        if type(compact) is not bool:
            raise ValueError("compact must be a boolean")
        if not isinstance(after_reference_id, str):
            raise ValueError("Reference cursor must be a string")
        if file_path is not None:
            _text(file_path, "file_path")
        if (
            native_symbol is not None
            and native_symbol.startswith("local ")
            and file_path is None
        ):
            raise ValueError("Local SCIP symbols require file_path document scope")
        _bounds(limit, max_output_bytes, timeout)
        verify_source = await self._source_check(repo_url) if compact else None

        return await self._read(
            """MATCH (r:Reference {repo_url:$repo_url,snapshot_id:$snapshot_id,native_id:$native_symbol})
            WHERE ($file_path IS NULL OR r.file_path=$file_path) AND r.reference_id > $after
            RETURN r {.reference_id,.native_id,.file_path,.start_line,.column,.end_line,.end_column,
                .coordinate_encoding,.symbol_roles,.resolution,.declaration_resolution,
                .target_ids,._dt_json_fields,._dt_null_fields} AS reference
            ORDER BY r.reference_id LIMIT $fetch_limit""",
            {
                "native_symbol": native_symbol,
                "file_path": file_path,
                "after": after_reference_id,
                "export_name": export_name,
                "export_file": export_file,
                "export_key": json.dumps(
                    [export_file, export_name], ensure_ascii=False
                ),
            },
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
            required_capability="verified_references"
            if export_name is not None
            else "references",
            format_response=_compact_references if compact else None,
            evidence_query=(
                """MATCH (f:File {repo_url:$repo_url,snapshot_id:$snapshot_id})
                -[x:EXPORTS_REFERENCE_SYMBOL {name:$export_name,producer:'typescript-6.0.3'}]->
                (e:ReferenceEvidence {repo_url:$repo_url,snapshot_id:$snapshot_id})
                WHERE $export_file IS NULL OR f.path=$export_file
                WITH DISTINCT e ORDER BY e.native_id,e.file_scope
                WITH e.compiler_symbol_id AS root,collect(DISTINCT e) AS nodes
                WITH root,head(nodes) AS e
                RETURN root,e {.native_id,.verification_status,.references,.references_sha256,.exports,
                    .indexed_count,.missing_in_scip,.extra_in_scip,.target_ids,
                    ._dt_json_fields,._dt_null_fields} AS evidence ORDER BY root LIMIT $fetch_limit"""
                if export_name is not None
                else """MATCH (e:ReferenceEvidence {repo_url:$repo_url,snapshot_id:$snapshot_id,native_id:$native_symbol})
                WHERE e.file_scope='' OR e.file_scope=$file_path
                RETURN e {.native_id,.verification_status,.references,.references_sha256,.exports,
                    .indexed_count,.missing_in_scip,.extra_in_scip,.target_ids,
                    ._dt_json_fields,._dt_null_fields} AS evidence LIMIT 1"""
            )
            if compact
            else None,
            postprocess=verify_source if compact else None,
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
        """Cross-file reference dependencies with bounded occurrence witnesses."""
        _text(file_path, "file_path")
        return await self._read(
            """MATCH (f:File {repo_url:$repo_url,snapshot_id:$snapshot_id,path:$file_path})
            -[r:DEPENDS_ON]->(t:File {repo_url:$repo_url,snapshot_id:$snapshot_id})
            WHERE r.relation='references'
            RETURN t.path AS target_file, r.dependency_id AS dependency_id,
                r.witness_ids[0..20] AS witness_ids, size(r.witness_ids) AS witness_count,
                size(r.witness_ids)>20 AS witnesses_truncated
            ORDER BY t.path,r.dependency_id LIMIT $fetch_limit""",
            {"file_path": file_path},
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
            required_capability="dependencies",
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
        """Snapshot declaration bodies, AST ownership and exact semantic bindings.

        Source offsets count Unicode code points. Expanded bodies remain paged.
        Historical snapshots report current-input verification explicitly.
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
            or not 1 <= max_source_characters <= 8192
        ):
            raise ValueError("max_source_characters must be from 1 to 8192")
        if type(include_relationships) is not bool:
            raise ValueError("include_relationships must be boolean")
        _bounds(limit, max_output_bytes, timeout)
        verify_source = await self._source_check(repo_url)

        def format_context(response):
            relationships = {}
            for row in response["items"]:
                for relationship in row["relationships"]:
                    for endpoint in ("source", "target"):
                        relationship[endpoint]["labels"].sort()
                    relationships[_json(relationship)] = relationship
            value = {
                **response,
                "response_format": "symbol-context-v1",
                "source_scope": "stored_snapshot",
                "source_offset_encoding": "unicode_codepoints",
                "source_verification": {"status": "prepared"},
                "items": [{"element": row["element"]} for row in response["items"]],
                "relationships": list(relationships.values()),
            }
            if len(value["relationships"]) > limit:
                value["relationships"] = value["relationships"][:limit]
                value["truncated"] = True
            while (
                value["relationships"]
                and len(_json(value).encode()) + 512 > max_output_bytes
            ):
                value["relationships"].pop()
                value["truncated"] = True
            return value

        return await self._read(
            """MATCH (f:File {repo_url:$repo_url,snapshot_id:$snapshot_id,path:$file_path})
            -[:DEFINES]->(n:Symbol {repo_url:$repo_url,snapshot_id:$snapshot_id})
            OPTIONAL MATCH (n)-[:HAS_AST_DECLARATION]->(ast:Symbol {repo_url:$repo_url,snapshot_id:$snapshot_id})
            WITH n,coalesce(ast,n) AS elem
            WHERE ($definition_id IS NULL OR n.definition_id=$definition_id OR elem.definition_id=$definition_id)
              AND ($native_symbol IS NULL OR n.native_id=$native_symbol)
              AND ($line_number IS NULL OR (coalesce(elem.source_start_line,elem.start_line)<=$line_number
                   AND coalesce(elem.source_end_line,elem.end_line)>=$line_number))
            WITH DISTINCT elem
            ORDER BY CASE WHEN elem.source_code IS NULL THEN 1 ELSE 0 END,
                CASE WHEN $line_number IS NULL THEN elem.start_line ELSE elem.end_line-elem.start_line END,
                elem.start_line,elem.qualified_name LIMIT $fetch_limit
            CALL {
                WITH elem
                OPTIONAL MATCH (source:GraphNode {repo_url:$repo_url,snapshot_id:$snapshot_id})
                -[r]->(target:GraphNode {repo_url:$repo_url,snapshot_id:$snapshot_id})
                WHERE $include_relationships AND (source=elem OR target=elem
                    OR (source:File AND source.path=$file_path AND type(r) IN ['IMPORTS','EXPORTS']))
                WITH source,r,target ORDER BY type(r),source.qualified_name,target.qualified_name LIMIT $fetch_limit
                RETURN collect(CASE WHEN r IS NULL THEN null ELSE {
                    source: {element_id:elementId(source),labels:labels(source),properties:source {.*,source_code:null}},
                    relationship:r,
                    target: {element_id:elementId(target),labels:labels(target),properties:target {.*,source_code:null}}
                } END) AS relationships
            }
            RETURN elem {.qualified_name,.definition_id,.name,.display_name,.kind,
                .file_path,.start_line,.end_line,.column,.end_column,.coordinate_encoding,.parent_definition_id,.lod,
                .source_start_line,.source_start_column,.source_end_line,.source_end_column,
                .source_sha256,.file_sha256,.source_context_schema,.ast_binding_status,
                source_code: substring(elem.source_code,$source_offset,$source_limit),
                source_offset:$source_offset, source_total_characters:size(elem.source_code),
                source_available:elem.source_code IS NOT NULL,
                source_truncated:coalesce(size(elem.source_code)>$source_offset+$source_limit,false),
                source_next_offset:CASE WHEN size(elem.source_code)>$source_offset+$source_limit
                    THEN $source_offset+$source_limit ELSE null END
            } AS element,relationships
            ORDER BY CASE WHEN elem.source_code IS NULL THEN 1 ELSE 0 END,
                CASE WHEN $line_number IS NULL THEN elem.start_line ELSE elem.end_line-elem.start_line END,
                elem.start_line,elem.qualified_name""",
            {
                "file_path": file_path,
                "line_number": line_number,
                "definition_id": definition_id,
                "native_symbol": native_symbol,
                "source_offset": source_offset,
                "source_limit": max_source_characters,
                "include_relationships": include_relationships,
            },
            repo_url,
            snapshot_id,
            limit=limit,
            max_output_bytes=max_output_bytes,
            timeout=timeout,
            format_response=format_context,
            postprocess=verify_source,
        )

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
        compact=False,
    ):
        _text(symbol_name, "symbol_name")
        if type(compact) is not bool:
            raise ValueError("compact must be a boolean")
        if compact:
            return await self._read(
                """MATCH (n:Symbol {repo_url:$repo_url,snapshot_id:$snapshot_id})
                WHERE n.name=$name OR n.display_name=$name OR n.native_id=$name
                WITH n ORDER BY n.file_path,n.start_line,n.definition_id
                WITH CASE WHEN n.native_id STARTS WITH 'local '
                    THEN n.file_path+'::'+n.native_id
                    ELSE coalesce(n.native_id,n.qualified_name) END AS symbol_id,
                    collect(n) AS nodes
                RETURN symbol_id,head(nodes).native_id AS native_id,
                    coalesce(head(nodes).display_name,head(nodes).name) AS name,
                    [n IN nodes | n {.definition_id,.kind,.file_path,.start_line,.column,
                        .end_line,.end_column,.coordinate_encoding,.analyzed_by,
                        .parent_definition_id,.lod,.ast_binding_status,.ast_candidate_ids,
                        source_available:n.source_code IS NOT NULL OR coalesce(n.ast_binding_status='resolved',false)}] AS declarations
                ORDER BY symbol_id LIMIT $fetch_limit""",
                {"name": symbol_name},
                repo_url,
                snapshot_id,
                limit=limit,
                max_output_bytes=max_output_bytes,
                timeout=timeout,
                response_format="definitions-v2",
            )
        return await self._read(
            """MATCH (n:Symbol {repo_url:$repo_url,snapshot_id:$snapshot_id})
            WHERE n.name=$name OR n.display_name=$name OR n.native_id=$name
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
