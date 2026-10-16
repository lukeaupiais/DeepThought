"""Bounded, evidence-preserving queries over a selected portable graph."""

from collections import Counter
from dataclasses import asdict, dataclass, replace
import json

from .graph import GraphSnapshot
from .models import is_relative_file


@dataclass(frozen=True)
class QueryRequest:
    operation: str
    repository_id: str
    symbol: str | None = None
    file: str | None = None
    limit: int = 20
    max_output_bytes: int = 32_768

    def __post_init__(self):
        if not isinstance(self.operation, str) or self.operation not in {
            "definitions",
            "references",
            "dependencies",
        }:
            raise ValueError("Unsupported query operation")
        if not isinstance(self.repository_id, str) or not self.repository_id.strip():
            raise ValueError("Query repository identity must be nonempty")
        if type(self.limit) is not int or not 1 <= self.limit <= 100:
            raise ValueError("Query limit must be an integer from 1 to 100")
        if (
            type(self.max_output_bytes) is not int
            or not 1024 <= self.max_output_bytes <= 32_768
        ):
            raise ValueError("Query output budget must be from 1024 to 32768 bytes")
        if self.symbol is not None and (
            not isinstance(self.symbol, str) or not self.symbol.strip()
        ):
            raise ValueError("Query symbol must be a nonempty string")
        if self.operation == "dependencies":
            if not is_relative_file(self.file) or self.symbol is not None:
                raise ValueError(
                    "Dependency queries require a relative file, not a symbol"
                )
        elif self.file is not None:
            raise ValueError("Only dependency queries accept a file")
        if self.operation == "references" and self.symbol is None:
            raise ValueError("Reference queries require an exact symbol")


@dataclass(frozen=True)
class QueryResult:
    operation: str
    repository_id: str
    snapshot_id: str
    graph_snapshot_id: str
    status: str
    analysis_status: str
    graph_status: str
    capabilities: tuple[str, ...]
    coverage: dict[str, int]
    diagnostic_count: int
    diagnostics: tuple[dict, ...]
    diagnostics_truncated: bool
    resolution_counts: dict[str, int]
    total_matches: int
    matches: tuple[dict, ...]
    truncated: bool
    max_output_bytes: int

    def to_json(self) -> str:
        data = asdict(self)
        data.pop("max_output_bytes")
        data.update(artifact_type="query", schema_version="1")
        content = json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if len(content.encode("utf-8")) + 1 > self.max_output_bytes:
            raise ValueError("Query response exceeds max_output_bytes")
        return content

    def to_dict(self) -> dict:
        return json.loads(self.to_json())


def query_graph(graph: GraphSnapshot, request: QueryRequest) -> QueryResult:
    """Query facts without I/O. Bounds serialized results, not graph scan time.

    repository_id scopes the origin of returned facts. Reference targets resolve
    across the supplied graph, using native identities rather than display names.
    Results follow canonical graph order. Counts require scanning matching facts;
    only the bounded response prefix is projected into dictionaries.
    """
    if not isinstance(graph, GraphSnapshot) or not isinstance(request, QueryRequest):
        raise ValueError("A graph snapshot and QueryRequest are required")
    sources = [s for s in graph.sources if s.repository_id == request.repository_id]
    if len(sources) != 1:
        raise ValueError("Select a repository present exactly once in the graph")
    source = sources[0]
    counts = dict.fromkeys(("resolved", "unresolved", "ambiguous"), 0)
    for ref in graph.references:
        if ref.repository_id == request.repository_id:
            counts[ref.resolution] += 1
    capability = "definitions" if request.operation == "definitions" else "references"
    available = capability in source.capabilities
    status = source.status if available else "unavailable"
    if (
        available
        and request.operation != "definitions"
        and status == "succeeded"
        and (counts["unresolved"] or counts["ambiguous"])
    ):
        status = "partial"
    targets = (
        {node.id for node in graph.lookup_definitions(request.symbol)}
        if available and request.operation == "references"
        else set()
    )

    def matching_nodes():
        if not available:
            return
        if request.operation == "definitions":
            for node in graph.definitions:
                if node.repository_id == request.repository_id and (
                    request.symbol is None
                    or request.symbol
                    in (node.definition.symbol, node.definition.native_id)
                ):
                    yield node
        elif request.operation == "references":
            for node in graph.references:
                if node.repository_id == request.repository_id and (
                    targets.intersection(node.target_ids)
                    or (not targets and node.reference.native_id == request.symbol)
                ):
                    yield node
        else:
            for node in graph.edges:
                if (node.source_repository_id, node.source_file) == (
                    request.repository_id,
                    request.file,
                ):
                    yield node

    total = sum(1 for _ in matching_nodes())
    result = QueryResult(
        operation=request.operation,
        repository_id=source.repository_id,
        snapshot_id=source.snapshot_id,
        graph_snapshot_id=graph.snapshot_id,
        status=status,
        analysis_status=source.status,
        graph_status=graph.status,
        capabilities=source.capabilities,
        coverage=dict(Counter(item.status for item in source.coverage)),
        diagnostic_count=len(source.diagnostics),
        diagnostics=tuple(asdict(item) for item in source.diagnostics[:10]),
        diagnostics_truncated=len(source.diagnostics) > 10,
        resolution_counts=counts,
        total_matches=total,
        matches=(),
        truncated=total > 0,
        max_output_bytes=request.max_output_bytes,
    )
    # Metadata must fit even for empty/unavailable results; never silently omit it.
    result.to_json()
    for node in matching_nodes():
        if len(result.matches) == request.limit:
            break
        if request.operation == "references":
            row = asdict(replace(node, target_ids=node.target_ids[:20]))
            row.update(
                target_count=len(node.target_ids),
                targets_truncated=len(node.target_ids) > 20,
            )
        elif request.operation == "dependencies":
            row = asdict(replace(node, witness_ids=node.witness_ids[:20]))
            row.update(
                witness_count=len(node.witness_ids),
                witnesses_truncated=len(node.witness_ids) > 20,
            )
        else:
            row = asdict(node)
        matches = (*result.matches, row)
        candidate = replace(result, matches=matches, truncated=len(matches) < total)
        try:
            candidate.to_json()
        except ValueError:
            if not result.matches:
                raise ValueError("First query match exceeds max_output_bytes") from None
            break
        result = candidate
    return result
