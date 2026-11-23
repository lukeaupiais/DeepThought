"""TreeSitterTool backed by the existing standalone declaration extractor.

This bounded port supports Python, TypeScript/TSX and (opt-in) JavaScript/JSX
and Go declarations, static file imports, and name-matched calls and declared bases.
It does not port the legacy confidence-scored CALLS output, CFG/data-flow
output or claim additional languages.
"""

from hashlib import sha256
import importlib.util
from pathlib import Path
import time
from copy import deepcopy

from .._version import __version__
from .. import calls as call_rules
from .. import imports as import_rules
from .. import inheritance as base_rules
from ..engine import analyze_repository
from ..syntax import DEFAULT_LANGUAGES, LANGUAGES, declaration_sources, make_parser
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
NESTS_RULE = register_rule(
    EvidenceRule(
        "tree-sitter.declaration-contains-declaration",
        "resolved",
        "The parse tree places the inner declaration within the outer one; the "
        "outer is the innermost declaration whose span encloses it.",
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
                "extension and index lookup order, then to JavaScript sources as "
                "with allowJs; package.json files are not read.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.JAVASCRIPT_RELATIVE_FILE,
                "resolved",
                "A relative specifier with a JavaScript extension names an "
                "analyzed file at exactly that path.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.JAVASCRIPT_EXTENSIONLESS_MODULE,
                "pattern_matched",
                "An extensionless relative specifier matches an analyzed file by "
                "the bundler/CommonJS lookup (.js, .jsx, .mjs, .cjs, then index); "
                "Node ES modules would not resolve it, and bundler config is not read.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.GO_MODULE_PACKAGE,
                "resolved",
                "A Go import path inside the importing file's own module (the "
                "nearest go.mod) names the directory at that path below the "
                "module root; the link reaches every non-test file of its "
                "package. vendor/, replace directives and go.work are not read.",
            )
        ),
    )
}


def _declaration_sources(root, result):
    """Declaration source text from the analyzed bytes, and files that changed.

    Each analyzed file is read again; it contributes only if its digest equals
    the one the analysis recorded, so stored text always matches the facts.
    """
    sources, changed, parsers = {}, [], {}
    for item in result.coverage:
        suffix = Path(item.file).suffix
        if (
            item.status not in {"analyzed", "partial"}
            or item.digest is None
            or suffix not in LANGUAGES.get(item.language, ((),))[0]
        ):
            continue
        try:
            content = (Path(root) / item.file).read_bytes()
        except OSError:
            content = None
        if content is None or sha256(content).hexdigest() != item.digest:
            changed.append(item.file)
            continue
        key = (item.language, suffix)
        if key not in parsers:
            parsers[key] = make_parser(item.language, suffix)
        sources.update(
            declaration_sources(
                parsers[key].parse(content), content, result.repository_id, item.file
            )
        )
    return sources, sorted(changed)


_CALL_RULE_TEXT = {
    call_rules.CALL_LEXICAL_NAME: "A bare callee name equals a function or class "
    "declared in an enclosing lexical scope of the same file.",
    call_rules.CALL_IMPORTED_NAME: "A bare callee name is bound by an import "
    "whose module file declares that name at top level.",
    call_rules.CALL_MODULE_ATTRIBUTE: "A callee 'module.name' uses a module "
    "import binding whose file declares that name at top level; a Go import "
    "without an alias binds the imported package's declared name.",
    call_rules.CALL_SELF_METHOD: "A self.name or this.name callee equals a "
    "method declared in the enclosing class body; inheritance is not followed.",
    call_rules.CALL_PACKAGE_NAME: "A bare callee name equals a function declared "
    "at top level by another file in the same directory with the same package "
    "clause; build constraints are not evaluated.",
}
CALL_RULES = {
    rule_id: register_rule(EvidenceRule(rule_id, "name_matched", text))
    for rule_id, text in _CALL_RULE_TEXT.items()
}

_BASE_RULE_TEXT = {
    base_rules.BASE_LEXICAL_NAME: "A base named in a class or interface "
    "declaration equals a class or interface declared in an enclosing lexical "
    "scope of the same file.",
    base_rules.BASE_IMPORTED_NAME: "A base name is bound by an import whose "
    "module file declares a class or interface of that name at top level.",
    base_rules.BASE_MODULE_ATTRIBUTE: "A base 'module.Name' uses a module import "
    "binding whose file declares a class or interface of that name at top level.",
}
BASE_RULES = {
    rule_id: register_rule(EvidenceRule(rule_id, "name_matched", text))
    for rule_id, text in _BASE_RULE_TEXT.items()
}


def _evidence(rule):
    return {"evidence_type": rule.evidence_type, "rule_id": rule.rule_id}


@register_tool
class TreeSitterTool(AnalysisTool):
    def __init__(self, repo_path):
        super().__init__(repo_path)
        self._definitions: dict[str, dict] = {}
        self.last_analysis = None
        self.last_reuse = {}

    def _get_metadata(self):
        return ToolMetadata(
            name="tree-sitter",
            version=__version__,
            capabilities={
                ToolCapability.AST_EXTRACTION,
                ToolCapability.SYMBOL_INDEXING,
            },
            supported_languages={"go", "javascript", "python", "typescript"},
            requires_binary=False,
            binary_name=None,
            performance_class="unmeasured",
        )

    async def check_availability(self):
        return importlib.util.find_spec("tree_sitter") is not None and any(
            importlib.util.find_spec(name) is not None
            for name in (
                "tree_sitter_go",
                "tree_sitter_javascript",
                "tree_sitter_python",
                "tree_sitter_typescript",
            )
        )

    async def analyze(
        self,
        repo_url,
        languages=None,
        file_patterns=None,
        skip_patterns=None,
        previous=None,
    ):
        """Analyze the workspace; ``previous`` is an earlier AnalysisResult.

        Unchanged files reuse ``previous`` facts (see analyze_repository); the
        output equals a full run. The analysis and reuse counts are kept on
        ``last_analysis`` and ``last_reuse``, outside the published metadata.
        """
        self._definitions = {}
        started = time.monotonic()
        self.last_reuse = {}
        result = analyze_repository(
            self.repo_path,
            repository_id=repo_url,
            languages=tuple(
                sorted(DEFAULT_LANGUAGES if languages is None else languages)
            ),
            include=("*",) if file_patterns is None else tuple(file_patterns),
            exclude=() if skip_patterns is None else tuple(skip_patterns),
            previous=previous,
            reuse=self.last_reuse,
        )
        self.last_analysis = result
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
        declared_packages = {
            item.file: {item.kind: item.name} for item in result.packages
        }
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
                        **(
                            {"file_sha256": coverage.digest}
                            if coverage.digest is not None
                            else {}
                        ),
                        **declared_packages.get(coverage.file, {}),
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
        sources, changed = _declaration_sources(self.repo_path, result)
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
                **sources.get(definition.id, {}),
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
        # Each declaration's owner is the innermost other declaration whose span
        # strictly encloses it in the same file.
        spans = {}
        for d in result.definitions:
            spans.setdefault(d.file, []).append(
                ((d.line, d.column), (d.end_line, d.end_column), d)
            )
        for file, items in spans.items():
            # Parse-tree spans nest, so a stack of open spans gives each owner.
            items.sort(key=lambda item: (item[0], tuple(-v for v in item[1])))
            open_spans = []
            for start, end, inner in items:
                while open_spans and open_spans[-1][1] < end:
                    open_spans.pop()
                owner = next(
                    (
                        outer
                        for _, outer_end, outer in reversed(open_spans)
                        if (outer_end, outer.line, outer.column) != (end, *start)
                    ),
                    None,
                )
                open_spans.append((start, end, inner))
                if owner is None:
                    continue
                relationships.append(
                    RelationshipData(
                        "CONTAINS",
                        [owner.kind, "Symbol"],
                        {
                            "qualified_name": f"{repo_url}::{owner.file}::{owner.symbol}::{owner.id}",
                            "repo_url": repo_url,
                        },
                        [inner.kind, "Symbol"],
                        {
                            "qualified_name": f"{repo_url}::{inner.file}::{inner.symbol}::{inner.id}",
                            "repo_url": repo_url,
                        },
                        {"line": inner.line, **_evidence(NESTS_RULE)},
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
        qualified = {
            d.id: f"{repo_url}::{d.file}::{d.symbol}::{d.id}"
            for d in result.definitions
        }
        kinds = {d.id: d.kind for d in result.definitions}
        sites = {site.id: site for site in result.call_sites}
        call_counts = {}
        for resolution in call_rules.resolve_calls(result):
            key = resolution.status + (
                ":" + resolution.reason if resolution.reason else ""
            )
            call_counts[key] = call_counts.get(key, 0) + 1
            if resolution.status != "linked":
                continue
            site = sites[resolution.call_id]
            if site.caller_id is None:
                source = (["File"], files[site.file])
            else:
                source = (
                    [kinds[site.caller_id], "Symbol"],
                    qualified[site.caller_id],
                )
            relationships.append(
                RelationshipData(
                    "CALLS",
                    source[0],
                    {"qualified_name": source[1], "repo_url": repo_url},
                    [kinds[resolution.target_id], "Symbol"],
                    {
                        "qualified_name": qualified[resolution.target_id],
                        "repo_url": repo_url,
                    },
                    {
                        "call_id": site.id,
                        "callee": site.callee,
                        "call_kind": site.kind,
                        "line": site.line,
                        "column": site.column,
                        **_evidence(CALL_RULES[resolution.rule_id]),
                    },
                )
            )
        bases = {item.id: item for item in result.supertypes}
        base_counts = {}
        for resolution in base_rules.resolve_supertypes(result):
            key = resolution.status + (
                ":" + resolution.reason if resolution.reason else ""
            )
            base_counts[key] = base_counts.get(key, 0) + 1
            if resolution.status != "linked":
                continue
            item = bases[resolution.call_id]
            relationships.append(
                RelationshipData(
                    "INHERITS_FROM",
                    [kinds[item.owner_id], "Symbol"],
                    {"qualified_name": qualified[item.owner_id], "repo_url": repo_url},
                    [kinds[resolution.target_id], "Symbol"],
                    {
                        "qualified_name": qualified[resolution.target_id],
                        "repo_url": repo_url,
                    },
                    {
                        "supertype_id": item.id,
                        "base": item.name,
                        "heritage": item.kind,
                        "line": item.line,
                        "column": item.column,
                        **_evidence(BASE_RULES[resolution.rule_id]),
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
                "call_resolution": dict(sorted(call_counts.items())),
                "supertype_resolution": dict(sorted(base_counts.items())),
                "source_context": {
                    "declarations": len(sources),
                    "files_changed_since_analysis": changed,
                },
                "scope": "named declarations, static file imports, and "
                "name-matched calls and declared bases; no control or data-flow "
                "inference",
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
