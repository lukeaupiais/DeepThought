"""TreeSitterTool backed by the existing standalone declaration extractor.

This bounded port supports Python and TypeScript/TSX declarations and static
file imports. It does not port the legacy heuristic CALLS/CFG/data-flow output
or claim additional languages.
"""

import importlib.util
import time
from copy import deepcopy

from .._version import __version__
from .. import imports as import_rules
from ..engine import analyze_repository
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
        "tree-sitter.repository-contains-file",
        "resolved",
        "The file was selected and analyzed under the repository root.",
    )
)
DEFINES_RULE = register_rule(
    EvidenceRule(
        "tree-sitter.file-defines-declaration",
        "resolved",
        "The parse tree of the file contains this named declaration.",
    )
)

IMPORT_RULES = {
    rule.rule_id: rule
    for rule in (
        register_rule(
            EvidenceRule(
                import_rules.PYTHON_RELATIVE_MODULE,
                "resolved",
                "A relative import names a package or module file analyzed at "
                "that path; a package directory takes precedence over a module.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.PYTHON_ABSOLUTE_MODULE,
                "name_matched",
                "The dotted module name equals the path of an analyzed file under "
                "the repository root or src/; the runtime import path is not checked.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.PYTHON_FROM_IMPORT_SUBMODULE,
                "name_matched",
                "A name imported from a package equals an analyzed submodule; the "
                "package itself may define that name instead.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.TYPESCRIPT_RELATIVE_MODULE,
                "resolved",
                "A relative specifier resolves to an analyzed file in TypeScript's "
                "extension and index lookup order; package.json files are not read.",
            )
        ),
    )
}


def _evidence(rule):
    return {"evidence_type": rule.evidence_type, "rule_id": rule.rule_id}


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
        imports = {item.id: item for item in result.imports}
        resolutions = import_rules.resolve_imports(result)
        undisclosed = {}
        for resolution in resolutions:
            if resolution.status != "linked":
                item = imports[resolution.import_id]
                undisclosed.setdefault(item.file, {}).setdefault(
                    resolution.status + "_imports", set()
                ).add(item.specifier)
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
                        **{
                            key: sorted(values)
                            for key, values in sorted(
                                undisclosed.get(coverage.file, {}).items()
                            )
                        },
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
                    _evidence(CONTAINS_RULE),
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
                    _evidence(DEFINES_RULE),
                )
            )
        for resolution in resolutions:
            item = imports[resolution.import_id]
            for link in resolution.links:
                relationships.append(
                    RelationshipData(
                        "IMPORTS",
                        ["File"],
                        {"qualified_name": files[item.file], "repo_url": repo_url},
                        ["File"],
                        {
                            "qualified_name": files[link.target_file],
                            "repo_url": repo_url,
                        },
                        {
                            "import_id": item.id,
                            "specifier": item.specifier,
                            "import_kind": item.kind,
                            "names": list(item.names),
                            "line": item.line,
                            **_evidence(IMPORT_RULES[link.rule_id]),
                        },
                    )
                )
        status_counts = dict.fromkeys(("linked", "ambiguous", "unresolved"), 0)
        for resolution in resolutions:
            status_counts[resolution.status] += 1
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
                "import_resolution": status_counts,
                "scope": "named declarations and static file imports; "
                "no call, control or data-flow inference",
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
