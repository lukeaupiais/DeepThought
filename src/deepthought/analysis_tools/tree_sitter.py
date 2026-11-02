"""TreeSitterTool backed by the existing standalone declaration extractor.

This bounded port supports Python and TypeScript/TSX. It does not port the
legacy heuristic CALLS/CFG/data-flow output or claim additional languages.
"""

import importlib.util
import time
from copy import deepcopy

from .._version import __version__
from ..engine import analyze_repository
from .base import AnalysisTool, ToolCapability, ToolMetadata
from .contracts import (
    NodeData,
    RelationshipData,
    StandardizedOutput,
    ToolExecutionMetadata,
)
from .registry import register_tool


@register_tool
class TreeSitterTool(AnalysisTool):
    def __init__(self, repo_path):
        super().__init__(repo_path)
        self._definitions: dict[str, dict] = {}

    def _get_metadata(self):
        return ToolMetadata(
            name="tree-sitter",
            version=__version__,
            capabilities={
                ToolCapability.AST_EXTRACTION,
                ToolCapability.SYMBOL_INDEXING,
            },
            supported_languages={"python", "typescript"},
            requires_binary=False,
            binary_name=None,
            performance_class="unmeasured",
        )

    async def check_availability(self):
        return importlib.util.find_spec("tree_sitter") is not None and any(
            importlib.util.find_spec(name) is not None
            for name in ("tree_sitter_python", "tree_sitter_typescript")
        )

    async def analyze(
        self, repo_url, languages=None, file_patterns=None, skip_patterns=None
    ):
        self._definitions = {}
        started = time.monotonic()
        result = analyze_repository(
            self.repo_path,
            repository_id=repo_url,
            languages=tuple(
                sorted(
                    self.metadata.supported_languages
                    if languages is None
                    else languages
                )
            ),
            include=("*",) if file_patterns is None else tuple(file_patterns),
            exclude=() if skip_patterns is None else tuple(skip_patterns),
        )
        # Preserve the original engine's Repository -> File -> declaration
        # projection. Extraction still belongs to the unchanged SDK parser.
        repository_name = f"{repo_url}::repository"
        nodes = [
            NodeData(
                ["Repository"],
                {
                    "qualified_name": repository_name,
                    "repo_url": repo_url,
                    "name": repo_url,
                    "url": repo_url,
                },
            )
        ]
        relationships = []
        files = {}
        for coverage in result.coverage:
            file_name = f"{repo_url}::{coverage.file}::file"
            files[coverage.file] = file_name
            nodes.append(
                NodeData(
                    ["File"],
                    {
                        "qualified_name": file_name,
                        "repo_url": repo_url,
                        "path": coverage.file,
                        "file_path": coverage.file,
                        "name": coverage.file,
                        "language": coverage.language,
                        "coverage_status": coverage.status,
                    },
                )
            )
            relationships.append(
                RelationshipData(
                    "CONTAINS",
                    ["Repository"],
                    {"qualified_name": repository_name, "repo_url": repo_url},
                    ["File"],
                    {"qualified_name": file_name, "repo_url": repo_url},
                )
            )
        for definition in result.definitions:
            qualified_name = (
                f"{repo_url}::{definition.file}::{definition.symbol}::{definition.id}"
            )
            properties = {
                "qualified_name": qualified_name,
                "repo_url": repo_url,
                "definition_id": definition.id,
                "file_path": definition.file,
                "name": definition.symbol,
                "display_name": definition.symbol,
                "kind": definition.kind,
                "start_line": definition.line,
                "end_line": definition.end_line,
                "column": definition.column,
                "end_column": definition.end_column,
                "coordinate_encoding": definition.coordinate_encoding,
            }
            nodes.append(
                NodeData(labels=[definition.kind, "Symbol"], properties=properties)
            )
            relationships.append(
                RelationshipData(
                    "DEFINES",
                    ["File"],
                    {"qualified_name": files[definition.file], "repo_url": repo_url},
                    [definition.kind, "Symbol"],
                    {"qualified_name": qualified_name, "repo_url": repo_url},
                )
            )
        artifact = result.to_dict()
        diagnostics = artifact["diagnostics"]
        metadata = ToolExecutionMetadata(
            tool_name=self.metadata.name,
            tool_version=self.metadata.version,
            execution_time_seconds=time.monotonic() - started,
            languages_processed=sorted({c.language for c in result.coverage}),
            files_processed=len(result.coverage),
            errors=[d["message"] for d in diagnostics]
            if result.status == "failed"
            else [],
            warnings=[d["message"] for d in diagnostics]
            if result.status != "failed"
            else [],
            custom_metrics={
                "analysis_status": result.status,
                "model_tokens": 0,
                "snapshot_id": result.snapshot_id,
                "repository_id": result.repository_id,
                "coverage": artifact["coverage"],
                "diagnostics": diagnostics,
                "analyzer_versions": artifact["analyzer_versions"],
                "capabilities": artifact["capabilities"],
                "source_facts_schema_version": result.schema_version,
                "scope": "named declarations only; no call, control or data-flow inference",
            },
        )
        output = StandardizedOutput(
            nodes=nodes, relationships=relationships, metadata=metadata
        )
        self._definitions = {
            n.properties["qualified_name"]: deepcopy(n.properties)
            for n in nodes
            if "Symbol" in n.labels
        }
        return output

    async def get_symbol_definitions(self):
        return deepcopy(self._definitions)

    async def cleanup(self):
        self._definitions = {}
