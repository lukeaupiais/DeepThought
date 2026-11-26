"""SCIP AnalysisTool using the existing ingestion and reference composer.

Consumes source-bound indexes: the TypeScript v1 profile from scip_indexing,
and the v2 profiles deepthought.indexers writes for Python, TypeScript,
JavaScript and Go. Several indexes (one per indexer) compose into one
snapshot. Analysis itself neither installs nor invokes tools.
Every edge is ``resolved``: it comes from the indexer's symbol identities and
document occurrences, checked against the bound source files.
"""

from copy import deepcopy
from dataclasses import replace
from fnmatch import fnmatchcase
from hashlib import sha256
import importlib.util
import json
import re
from pathlib import Path
import time

from .._version import __version__
from ..graph import compose_graph
from ..scip import ingest_scip
from ..indexers import INDEXERS, LANGUAGE_SUFFIXES, validate_receipt
from ..scip_indexing import validate_index
from .base import AnalysisTool, ToolCapability, ToolMetadata
from .contracts import (
    NodeData,
    RelationshipData,
    StandardizedOutput,
    ToolExecutionMetadata,
)
from .evidence import EvidenceRule, register_rule
from .registry import register_tool

CONTAINS_RULE = register_rule(
    EvidenceRule(
        "scip.repository-contains-file",
        "resolved",
        "The source-bound SCIP index has a document for this file.",
    )
)
DEFINES_RULE = register_rule(
    EvidenceRule(
        "scip.file-defines-symbol",
        "resolved",
        "The file's SCIP document has a definition occurrence of this symbol.",
    )
)
CONTAINS_REFERENCE_RULE = register_rule(
    EvidenceRule(
        "scip.file-contains-reference",
        "resolved",
        "The file's SCIP document has this non-definition occurrence.",
    )
)
REFERENCES_RULE = register_rule(
    EvidenceRule(
        "scip.native-symbol-reference",
        "resolved",
        "The occurrence names the SCIP symbol that this definition declares; "
        "local symbols match only within their document. A symbol declared at "
        "several sites links to each of them.",
    )
)
DEPENDS_ON_RULE = register_rule(
    EvidenceRule(
        "scip.reference-dependency",
        "resolved",
        "At least one occurrence in the source file names a symbol declared "
        "only in the target file.",
    )
)
IMPLEMENTS_RULE = register_rule(
    EvidenceRule(
        "scip.symbol-implementation",
        "resolved",
        "The indexer records that the source symbol implements the target "
        "symbol: a class's supertypes, including indirect ones, through "
        "extends or implements, and a member's overridden or implemented "
        "member. Both symbols are declared in the analyzed files.",
    )
)


def _implementations(index_path):
    """(document, symbol, related symbol) for each is_implementation
    relationship of an index whose receipt was validated."""
    from .._scip import scip_pb2

    content = Path(index_path).read_bytes()
    index = scip_pb2.Index()
    index.ParseFromString(content)
    return sorted(
        {
            (document.relative_path, info.symbol, relationship.symbol)
            for document in index.documents
            for info in document.symbols
            for relationship in info.relationships
            if relationship.is_implementation and info.symbol and relationship.symbol
        }
    )


def _evidence(rule):
    return {"evidence_type": rule.evidence_type, "rule_id": rule.rule_id}


TYPESCRIPT_V1 = "scip-typescript-v1"
LANGUAGES = ("go", "javascript", "python", "typescript")


def _language(file):
    return LANGUAGE_SUFFIXES.get(Path(file).suffix)


def _receipt_profile(path):
    receipt = Path(str(path) + ".sources.json")
    try:
        return json.loads(receipt.read_text()).get("profile")
    except (OSError, ValueError, AttributeError):
        return None


def _receipt_languages(receipt):
    if receipt.get("profile") == TYPESCRIPT_V1:
        return {"typescript"}
    return set(INDEXERS[receipt["producer"]["name"]].languages)


def _merge(sources, revision):
    """One analysis snapshot from per-indexer results over disjoint files."""
    if len(sources) == 1:
        return replace(sources[0], snapshot_id=revision)
    statuses = {source.status for source in sources}
    status = next(
        s for s in ("failed", "cancelled", "partial", "succeeded") if s in statuses
    )
    return replace(
        sources[0],
        snapshot_id=revision,
        status=status,
        definitions=tuple(d for s in sources for d in s.definitions),
        references=tuple(r for s in sources for r in s.references),
        coverage=tuple(c for s in sources for c in s.coverage),
        diagnostics=tuple(d for s in sources for d in s.diagnostics),
        analyzer_versions=tuple(
            sorted({item for s in sources for item in s.analyzer_versions})
        ),
    )


def _receipt_metrics(receipts):
    def summary(receipt):
        return {
            "source_receipt_sha256": sha256(
                json.dumps(receipt, sort_keys=True).encode()
            ).hexdigest(),
            "index_sha256": receipt["index_sha256"],
            "producer": receipt["producer"],
            "position_encoding_fallback": receipt["position_encoding"],
        }

    if len(receipts) == 1:
        return summary(receipts[0])
    return {"indexes": [summary(receipt) for receipt in receipts]}


@register_tool
class ScipAnalysisTool(AnalysisTool):
    def __init__(
        self,
        repo_path,
        *,
        index_path=None,
        index_paths=None,
    ):
        super().__init__(repo_path)
        if index_path is not None and index_paths is not None:
            raise ValueError("Pass index_path or index_paths, not both")
        self.index_paths = (
            [Path(p) for p in index_paths]
            if index_paths is not None
            else [
                Path(index_path)
                if index_path is not None
                else self.repo_path / "index.scip"
            ]
        )
        if not self.index_paths:
            raise ValueError("At least one SCIP index is required")
        self._definitions = {}

    @property
    def index_path(self):
        return self.index_paths[0]

    def _get_metadata(self):
        return ToolMetadata(
            name="scip",
            version=__version__,
            capabilities={
                ToolCapability.SYMBOL_INDEXING,
                ToolCapability.REFERENCE_INDEXING,
                ToolCapability.DEPENDENCY_ANALYSIS,
            },
            supported_languages=set(LANGUAGES),
            requires_binary=False,
            binary_name=None,
            performance_class="unmeasured",
        )

    async def check_availability(self):
        try:
            protocol = importlib.util.find_spec("google.protobuf") is not None
        except ModuleNotFoundError:
            protocol = False
        return protocol and all(
            path.is_file() and Path(str(path) + ".sources.json").is_file()
            for path in self.index_paths
        )

    def _validate(self, path):
        """The index's receipt, checked against the current inputs."""
        if not path.is_file() or not Path(str(path) + ".sources.json").is_file():
            raise ValueError(f"SCIP index or receipt is missing: {path}")
        if _receipt_profile(path) == TYPESCRIPT_V1:
            return validate_index(self.repo_path, path)
        return validate_receipt(self.repo_path, path)

    async def analyze(
        self, repo_url, languages=None, file_patterns=None, skip_patterns=None
    ):
        self._definitions = {}
        started = time.monotonic()
        wanted = set(LANGUAGES) if languages is None else set(languages)
        if wanted - set(LANGUAGES):
            raise ValueError("The SCIP plugin supports " + ", ".join(LANGUAGES))
        receipts = [self._validate(path) for path in self.index_paths]
        semantic_receipts = [
            {key: value for key, value in receipt.items() if key != "wall_seconds"}
            for receipt in receipts
        ]
        sources, owner = [], {}
        for path, receipt in zip(self.index_paths, receipts):
            # The receipt vouches for the index's digest, so it is read whole:
            # a size cap would refuse a big repository's index (lobe-chat's
            # 80 MB) and read as a producer mismatch.
            source = ingest_scip(
                path,
                repository_id=repo_url,
                default_position_encoding=receipt["position_encoding"],
                max_index_bytes=max(1, path.stat().st_size),
            )
            if source.status == "failed":
                raise ValueError(
                    f"SCIP index {path.name} could not be read: "
                    + "; ".join(f"{d.code}: {d.message}" for d in source.diagnostics)
                )
            producer = receipt["producer"]
            if (
                dict(source.analyzer_versions).get(producer["name"])
                != producer["version"]
            ):
                raise ValueError("SCIP producer differs from the source receipt")
            v1 = receipt.get("profile") == TYPESCRIPT_V1

            def selected(file, v1=v1, receipt=receipt):
                if v1:
                    language = (
                        "typescript"
                        if file.endswith(".ts") or file.endswith(".tsx")
                        else None
                    )
                else:
                    language = _language(file)
                return (
                    language in wanted
                    and language in _receipt_languages(receipt)
                    and (
                        file_patterns is None
                        or any(fnmatchcase(file, pattern) for pattern in file_patterns)
                    )
                    and not any(
                        fnmatchcase(file, pattern) for pattern in (skip_patterns or ())
                    )
                )

            source = replace(
                source,
                definitions=tuple(d for d in source.definitions if selected(d.file)),
                references=tuple(r for r in source.references if selected(r.file)),
                coverage=tuple(c for c in source.coverage if selected(c.file)),
            )
            for coverage in source.coverage:
                if coverage.file in owner:
                    raise ValueError("Two SCIP indexes cover " + coverage.file)
                owner[coverage.file] = receipt
            sources.append(source)

        # Selection is explicit and part of source identity. Local symbols retain
        # the original per-document scope through the SDK's native resolver.
        identity = {
            "composer_version": "2",
            "projection_version": "3",
            "include": file_patterns,
            "exclude": skip_patterns,
        }
        if len(sources) == 1:
            identity.update(
                source=sources[0].snapshot_id, source_receipt=semantic_receipts[0]
            )
        else:
            identity["sources"] = [
                [source.snapshot_id, receipt]
                for source, receipt in zip(sources, semantic_receipts)
            ]
        revision = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        result = _merge(sources, revision)
        if not result.coverage:
            raise ValueError("SCIP selection contains no indexed documents")
        graph = compose_graph([result], composer_version="2")
        nodes, relationships = [], []
        repository = f"{repo_url}::repository"
        nodes.append(
            NodeData(
                ["Repository"],
                {
                    "qualified_name": repository,
                    "repo_url": repo_url,
                    "name": repo_url,
                    "url": repo_url,
                },
            )
        )

        def link(kind, source, target, rule, properties=None):
            relationships.append(
                RelationshipData(
                    kind,
                    ["GraphNode"],
                    {"qualified_name": source, "repo_url": repo_url},
                    ["GraphNode"],
                    {"qualified_name": target, "repo_url": repo_url},
                    {**(properties or {}), **_evidence(rule)},
                )
            )

        files = {}
        for coverage in result.coverage:
            name = f"{repo_url}::{coverage.file}::file"
            files[coverage.file] = name
            nodes.append(
                NodeData(
                    ["File"],
                    {
                        "qualified_name": name,
                        "repo_url": repo_url,
                        "name": coverage.file,
                        "path": coverage.file,
                        "file_path": coverage.file,
                        "language": _language(coverage.file),
                        "coverage_status": coverage.status,
                    },
                )
            )
            link("CONTAINS", repository, name, CONTAINS_RULE)
        targets = {}
        source_lines = {}

        def source_token(record):
            if record.coordinate_encoding == "unspecified":
                return None
            if record.file not in source_lines:
                source_lines[record.file] = re.split(
                    r"\r\n|[\r\n\u2028\u2029]",
                    (self.repo_path / record.file).read_text(),
                )
            lines = source_lines[record.file]
            if record.end_line > len(lines):
                raise ValueError("SCIP occurrence exceeds its bound source file")
            codec, width = {
                "utf-8": ("utf-8", 1),
                "utf-16": ("utf-16-le", 2),
                "utf-32": ("utf-32-le", 4),
            }[record.coordinate_encoding]
            start_line = lines[record.line - 1].encode(codec)
            end_line = lines[record.end_line - 1].encode(codec)
            if record.column * width > len(
                start_line
            ) or record.end_column * width > len(end_line):
                raise ValueError("SCIP occurrence exceeds its bound source line")
            try:
                start_line[: record.column * width].decode(codec)
                end_line[: record.end_column * width].decode(codec)
                if record.line == record.end_line:
                    return start_line[
                        record.column * width : record.end_column * width
                    ].decode(codec)
            except UnicodeDecodeError as exc:
                raise ValueError(
                    "SCIP occurrence splits a source code unit sequence"
                ) from exc
            return None

        for item in graph.definitions:
            d = item.definition
            name = f"{repo_url}::scip-definition::{item.id}"
            targets[item.id] = name
            token = source_token(d)
            display_name = (
                token
                if d.symbol == d.native_id and token and len(token) <= 256
                else d.symbol
            )
            properties = {
                "qualified_name": name,
                "repo_url": repo_url,
                "definition_id": item.id,
                "source_definition_id": d.id,
                "file_sha256": owner[d.file]["files"][d.file],
                "name": d.symbol,
                "display_name": display_name,
                "native_id": d.native_id,
                "kind": d.kind,
                "file_path": d.file,
                "start_line": d.line,
                "column": d.column,
                "end_line": d.end_line,
                "end_column": d.end_column,
                "coordinate_encoding": d.coordinate_encoding,
                "analyzed_by": ["scip"],
            }
            labels = ["Symbol"]
            if d.kind in {
                "Class",
                "Method",
                "Function",
                "Interface",
                "Enum",
                "TypeAlias",
                "Namespace",
            }:
                labels.append(d.kind)
            nodes.append(NodeData(labels, properties))
            link("DEFINES", files[d.file], name, DEFINES_RULE)
            self._definitions[name] = deepcopy(properties)
        for item in graph.references:
            r = item.reference
            source_token(r)
            name = f"{repo_url}::scip-reference::{item.id}"
            nodes.append(
                NodeData(
                    ["Reference"],
                    {
                        "qualified_name": name,
                        "repo_url": repo_url,
                        "reference_id": item.id,
                        "native_id": r.native_id,
                        "file_path": r.file,
                        "start_line": r.line,
                        "column": r.column,
                        "end_line": r.end_line,
                        "end_column": r.end_column,
                        "coordinate_encoding": r.coordinate_encoding,
                        "symbol_roles": r.symbol_roles,
                        "resolution": item.resolution,
                        "declaration_resolution": (
                            "unresolved"
                            if not item.target_ids
                            else "unique"
                            if len(item.target_ids) == 1
                            else "multiple"
                        ),
                        "target_ids": list(item.target_ids),
                    },
                )
            )
            link("CONTAINS_REFERENCE", files[r.file], name, CONTAINS_REFERENCE_RULE)
            for target in item.target_ids:
                link(
                    "REFERENCES",
                    name,
                    targets[target],
                    REFERENCES_RULE,
                    {"resolution": item.resolution},
                )
        for edge in graph.edges:
            link(
                "DEPENDS_ON",
                files[edge.source_file],
                files[edge.target_file],
                DEPENDS_ON_RULE,
                {
                    "relation": "references",
                    "dependency_id": edge.id,
                    "witness_ids": list(edge.witness_ids),
                },
            )
        # A local symbol is unique only within its document.
        sites = {}
        for item in graph.definitions:
            d = item.definition
            key = (
                (d.file, d.native_id)
                if d.native_id.startswith("local ")
                else (d.native_id,)
            )
            sites.setdefault(key, []).append(targets[item.id])
        implementations = {"linked": 0, "external_target": 0}
        # Sites cover only selected files, so unselected sources are skipped and
        # unselected or undeclared targets are counted, never guessed.
        for file, symbol, related in (
            item for path in self.index_paths for item in _implementations(path)
        ):

            def key(native):
                return (file, native) if native.startswith("local ") else (native,)

            sources, supers = sites.get(key(symbol), []), sites.get(key(related), [])
            if not sources:
                continue
            if not supers:
                implementations["external_target"] += 1
                continue
            implementations["linked"] += 1
            for source in sorted(sources):
                for target in sorted(supers):
                    link("IMPLEMENTS", source, target, IMPLEMENTS_RULE)
        # Recheck index/source binding after ingestion and conversion as well.
        if [self._validate(path) for path in self.index_paths] != receipts:
            raise ValueError("SCIP inputs changed during analysis")
        diagnostics = [
            d.to_dict() if hasattr(d, "to_dict") else vars(d)
            for d in result.diagnostics
        ]
        return StandardizedOutput(
            nodes,
            relationships,
            ToolExecutionMetadata(
                tool_name="scip",
                tool_version=__version__,
                execution_time_seconds=time.monotonic() - started,
                languages_processed=sorted(
                    {_language(c.file) for c in result.coverage}
                ),
                files_processed=len(result.coverage),
                errors=[d["message"] for d in diagnostics]
                if result.status == "failed"
                else [],
                warnings=[d["message"] for d in diagnostics]
                if result.status != "failed"
                else [],
                custom_metrics={
                    "snapshot_id": revision,
                    "analysis_status": result.status,
                    "graph_status": graph.status,
                    "composer_version": graph.composer_version,
                    "resolution_semantics": "native_symbol_binding",
                    "model_tokens": 0,
                    "capabilities": [
                        "definitions",
                        "references",
                        "dependencies",
                        "implementations",
                    ],
                    "coverage": [vars(c) for c in result.coverage],
                    "diagnostics": diagnostics,
                    "resolution_counts": graph.resolution_counts,
                    "implementation_relationships": implementations,
                    **_receipt_metrics(semantic_receipts),
                },
            ),
        )

    async def get_symbol_definitions(self):
        return {
            name: properties.copy() for name, properties in self._definitions.items()
        }

    async def cleanup(self):
        self._definitions = {}
