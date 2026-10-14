"""Local graph composition from supplied facts, without name-based inference."""

from collections import defaultdict
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Iterable

from .models import AnalysisResult, Definition, Reference
from .syntax import identifier

# Changes to graph semantics require a new explicit contract, not a package bump.
COMPOSER_VERSION = "1"


@dataclass(frozen=True)
class GraphDefinition:
    id: str
    repository_id: str
    source_snapshot_id: str
    definition: Definition


@dataclass(frozen=True)
class GraphReference:
    id: str
    repository_id: str
    source_snapshot_id: str
    reference: Reference
    resolution: str
    target_ids: tuple[str, ...]


@dataclass(frozen=True)
class DependencyEdge:
    id: str
    source_repository_id: str
    source_file: str
    target_repository_id: str
    target_file: str
    witness_ids: tuple[str, ...]
    relation: str = "references"


@dataclass(frozen=True)
class GraphSnapshot:
    snapshot_id: str
    status: str
    sources: tuple[AnalysisResult, ...]
    definitions: tuple[GraphDefinition, ...]
    references: tuple[GraphReference, ...]
    edges: tuple[DependencyEdge, ...]

    @property
    def resolution_counts(self) -> dict[str, int]:
        counts = dict.fromkeys(("resolved", "unresolved", "ambiguous"), 0)
        for reference in self.references:
            counts[reference.resolution] += 1
        return counts

    def lookup_definitions(
        self, symbol: str, *, repository_id: str | None = None
    ) -> tuple[GraphDefinition, ...]:
        """Exact display/native symbol lookup; multiple matches remain separate."""
        return tuple(
            node
            for node in self.definitions
            if (repository_id is None or node.repository_id == repository_id)
            and symbol in (node.definition.symbol, node.definition.native_id)
        )

    def references_to(self, definition_id: str) -> tuple[GraphReference, ...]:
        """References including explicit ambiguous candidates, never inferred calls."""
        if not any(node.id == definition_id for node in self.definitions):
            raise ValueError("Unknown graph definition identity")
        return tuple(ref for ref in self.references if definition_id in ref.target_ids)

    def dependencies(self, repository_id: str, file: str) -> tuple[DependencyEdge, ...]:
        """Cross-file edges witnessed only by uniquely resolved references."""
        return tuple(
            edge
            for edge in self.edges
            if (edge.source_repository_id, edge.source_file) == (repository_id, file)
        )

    def to_dict(self) -> dict:
        # Retain source artifacts so every resolution and projection is auditable.
        return json.loads(
            json.dumps(
                {
                    "artifact_type": "graph",
                    "schema_version": "1",
                    "composer_version": COMPOSER_VERSION,
                    **asdict(self),
                }
            )
        )

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, ensure_ascii=False, allow_nan=False
        )

    @classmethod
    def from_json(cls, value: str) -> "GraphSnapshot":
        data = json.loads(value)
        if (
            not isinstance(data, dict)
            or data.get("artifact_type") != "graph"
            or data.get("schema_version") != "1"
            or data.get("composer_version") != COMPOSER_VERSION
        ):
            raise ValueError("Unsupported graph artifact schema")
        try:
            graph = compose_graph(
                AnalysisResult.from_json(json.dumps(item)) for item in data["sources"]
            )
            if graph.to_dict() != data:
                raise ValueError("Graph does not agree with its source facts")
            return graph
        except (TypeError, KeyError, AttributeError) as exc:
            raise ValueError("Invalid graph artifact") from exc


def _symbol_key(repository_id: str, file: str, native: str) -> tuple[str, ...]:
    # SCIP local symbols MUST be document-scoped, even across repositories.
    return (repository_id, file, native) if native.startswith("local ") else (native,)


def compose_graph(results: Iterable[AnalysisResult]) -> GraphSnapshot:
    """Compose one selected full analysis snapshot per repository.

    Full native symbols are the sole resolution evidence. Definitions without
    native IDs remain queryable but cannot resolve compiler-backed references.
    """
    sources = tuple(results)
    if any(not isinstance(item, AnalysisResult) for item in sources):
        raise ValueError("Graph sources must be analysis artifacts")
    sources = tuple(sorted(sources, key=lambda item: item.repository_id))
    if not sources or len({item.repository_id for item in sources}) != len(sources):
        raise ValueError("Select exactly one analysis snapshot per repository")
    definitions, references = [], []
    symbols = defaultdict(list)
    for source in sources:
        for definition in sorted(source.definitions, key=lambda item: item.id):
            node = GraphDefinition(
                identifier(
                    source.repository_id,
                    definition.file,
                    definition.id,
                    "GraphDefinition",
                    0,
                ),
                source.repository_id,
                source.snapshot_id,
                definition,
            )
            definitions.append(node)
            if definition.native_id is not None:
                symbols[
                    _symbol_key(
                        source.repository_id, definition.file, definition.native_id
                    )
                ].append(node)
    witnesses = defaultdict(list)
    for source in sources:
        for reference in sorted(source.references, key=lambda item: item.id):
            targets = symbols.get(
                _symbol_key(source.repository_id, reference.file, reference.native_id),
                [],
            )
            target_ids = tuple(sorted(node.id for node in targets))
            resolution = (
                "unresolved"
                if not targets
                else "resolved"
                if len(targets) == 1
                else "ambiguous"
            )
            ref = GraphReference(
                identifier(
                    source.repository_id,
                    reference.file,
                    reference.id,
                    "GraphReference",
                    0,
                ),
                source.repository_id,
                source.snapshot_id,
                reference,
                resolution,
                target_ids,
            )
            references.append(ref)
            if resolution == "resolved":
                target = targets[0]
                if (source.repository_id, reference.file) != (
                    target.repository_id,
                    target.definition.file,
                ):
                    witnesses[
                        (
                            source.repository_id,
                            reference.file,
                            target.repository_id,
                            target.definition.file,
                        )
                    ].append(ref.id)
    edges = tuple(
        DependencyEdge(
            sha256(json.dumps(key).encode()).hexdigest(), *key, tuple(sorted(ids))
        )
        for key, ids in sorted(witnesses.items())
    )
    complete = all(
        source.status == "succeeded" and "references" in source.capabilities
        for source in sources
    )
    complete = complete and all(ref.resolution == "resolved" for ref in references)
    status = (
        "succeeded"
        if complete
        else "partial"
        if definitions or references
        else "failed"
    )
    manifest = {
        "sources": [source.to_dict() for source in sources],
        "composer_version": COMPOSER_VERSION,
    }
    snapshot_id = sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return GraphSnapshot(
        snapshot_id, status, sources, tuple(definitions), tuple(references), edges
    )
