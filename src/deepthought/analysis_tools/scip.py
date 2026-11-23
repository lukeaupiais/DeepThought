"""SCIP AnalysisTool using the existing ingestion and reference composer.

The initial validated profile consumes source-bound TypeScript indexes produced
explicitly by scip_indexing. Analysis itself neither installs nor invokes tools.
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
from ..scip_indexing import validate_index
from .base import AnalysisTool, ToolCapability, ToolMetadata
from .contracts import (
    NodeData,
    RelationshipData,
    StandardizedOutput,
    ToolExecutionMetadata,
)
from .registry import register_tool


@register_tool
class ScipAnalysisTool(AnalysisTool):
    def __init__(
        self,
        repo_path,
        *,
        index_path=None,
        reference_node=None,
        reference_typescript=None,
    ):
        if (reference_node is None) != (reference_typescript is None):
            raise ValueError(
                "Provide both the reference Node runtime and TypeScript package"
            )
        self.reference_node, self.reference_typescript = (
            reference_node,
            reference_typescript,
        )
        super().__init__(repo_path)
        self.index_path = (
            Path(index_path)
            if index_path is not None
            else self.repo_path / "index.scip"
        )
        self._definitions = {}

    def _get_metadata(self):
        return ToolMetadata(
            name="scip",
            version=__version__,
            capabilities={
                ToolCapability.SYMBOL_INDEXING,
                ToolCapability.REFERENCE_INDEXING,
                ToolCapability.DEPENDENCY_ANALYSIS,
            },
            supported_languages={"typescript"},
            requires_binary=self.reference_node is not None,
            binary_name="node" if self.reference_node is not None else None,
            performance_class="unmeasured",
        )

    async def check_availability(self):
        try:
            protocol = importlib.util.find_spec("google.protobuf") is not None
        except ModuleNotFoundError:
            protocol = False
        return (
            protocol
            and self.index_path.is_file()
            and Path(str(self.index_path) + ".sources.json").is_file()
            and (
                self.reference_node is None
                or (
                    Path(self.reference_node).is_file()
                    and (
                        Path(self.reference_typescript) / "lib/typescript.js"
                    ).is_file()
                )
            )
        )

    async def analyze(
        self, repo_url, languages=None, file_patterns=None, skip_patterns=None
    ):
        self._definitions = {}
        started = time.monotonic()
        if languages is not None and set(languages) != {"typescript"}:
            raise ValueError(
                "The source-bound SCIP plugin currently supports TypeScript"
            )
        receipt = validate_index(self.repo_path, self.index_path)
        verification_scope, reference_evidence = None, []
        semantic_receipt = {
            key: value for key, value in receipt.items() if key != "wall_seconds"
        }
        result = ingest_scip(
            self.index_path,
            repository_id=repo_url,
            default_position_encoding=receipt["position_encoding"],
        )
        if (
            dict(result.analyzer_versions).get("scip-typescript")
            != receipt["producer"]["version"]
        ):
            raise ValueError("SCIP producer differs from the source receipt")

        def selected(file):
            return (
                (file.endswith(".ts") or file.endswith(".tsx"))
                and (
                    file_patterns is None
                    or any(fnmatchcase(file, pattern) for pattern in file_patterns)
                )
                and not any(
                    fnmatchcase(file, pattern) for pattern in (skip_patterns or ())
                )
            )

        # Selection is explicit and part of source identity. Local symbols retain
        # the original per-document scope through the SDK's native resolver.
        revision = sha256(
            json.dumps(
                {
                    "source": result.snapshot_id,
                    "source_receipt": semantic_receipt,
                    "composer_version": "2",
                    "projection_version": "3",
                    "include": file_patterns,
                    "exclude": skip_patterns,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        result = replace(
            result,
            snapshot_id=revision,
            definitions=tuple(d for d in result.definitions if selected(d.file)),
            references=tuple(r for r in result.references if selected(r.file)),
            coverage=tuple(c for c in result.coverage if selected(c.file)),
        )
        if not result.coverage:
            raise ValueError("SCIP selection contains no indexed TypeScript documents")
        if self.reference_node is not None:
            from ..typescript_reference_evidence import build_reference_evidence

            verification_scope, reference_evidence = build_reference_evidence(
                result,
                receipt,
                self.repo_path,
                self.reference_node,
                self.reference_typescript,
            )
            revision = sha256(
                json.dumps(
                    {
                        "source": revision,
                        "reference_evidence": {
                            k: v
                            for k, v in verification_scope.items()
                            if k != "preparation_wall_seconds"
                        },
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            result = replace(result, snapshot_id=revision)
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

        def link(kind, source, target, properties=None):
            relationships.append(
                RelationshipData(
                    kind,
                    ["GraphNode"],
                    {"qualified_name": source, "repo_url": repo_url},
                    ["GraphNode"],
                    {"qualified_name": target, "repo_url": repo_url},
                    properties or {},
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
                        "language": "typescript",
                        "coverage_status": coverage.status,
                    },
                )
            )
            link("CONTAINS", repository, name)
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
                "file_sha256": receipt["files"][d.file],
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
            link("DEFINES", files[d.file], name)
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
            link("CONTAINS_REFERENCE", files[r.file], name)
            for target in item.target_ids:
                link(
                    "REFERENCES", name, targets[target], {"resolution": item.resolution}
                )
        for edge in graph.edges:
            link(
                "DEPENDS_ON",
                files[edge.source_file],
                files[edge.target_file],
                {
                    "relation": "references",
                    "dependency_id": edge.id,
                    "witness_ids": list(edge.witness_ids),
                },
            )
        # Recheck index/source binding after ingestion and conversion as well.
        native_targets = {}
        if reference_evidence:
            from ..typescript_reference_evidence import symbol_key

            for item in graph.definitions:
                key = symbol_key(item.definition.native_id, item.definition.file)
                native_targets.setdefault(key, []).append(item.id)
        for evidence in reference_evidence:
            evidence["target_ids"] = native_targets.get(evidence["symbol_key"], [])
            evidence_name = f"{repo_url}::reference-evidence::{sha256(evidence['symbol_key'].encode()).hexdigest()}"
            nodes.append(
                NodeData(
                    ["ReferenceEvidence"],
                    {
                        **evidence,
                        "qualified_name": evidence_name,
                        "repo_url": repo_url,
                    },
                )
            )
            for exported in evidence["exports"]:
                link(
                    "EXPORTS_REFERENCE_SYMBOL",
                    files[exported["file"]],
                    evidence_name,
                    {"name": exported["name"], "producer": "typescript-6.0.3"},
                )
        if validate_index(self.repo_path, self.index_path) != receipt:
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
                languages_processed=["typescript"],
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
                    "capabilities": ["definitions", "references", "dependencies"]
                    + (["verified_references"] if verification_scope else []),
                    **(
                        {"reference_verification": verification_scope}
                        if verification_scope
                        else {}
                    ),
                    "coverage": [vars(c) for c in result.coverage],
                    "diagnostics": diagnostics,
                    "resolution_counts": graph.resolution_counts,
                    "source_receipt_sha256": sha256(
                        json.dumps(semantic_receipt, sort_keys=True).encode()
                    ).hexdigest(),
                    "index_sha256": receipt["index_sha256"],
                    "producer": receipt["producer"],
                    "position_encoding_fallback": receipt["position_encoding"],
                },
            ),
        )

    async def get_symbol_definitions(self):
        return {
            name: properties.copy() for name, properties in self._definitions.items()
        }

    async def cleanup(self):
        self._definitions = {}
