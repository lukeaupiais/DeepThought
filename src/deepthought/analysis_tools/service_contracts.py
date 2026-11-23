"""ServiceContractTool: HTTP routes servers declare and the requests clients make.

Routes and requests are read from Java/Spring sources (see
``deepthought.service_contracts``). Each route is an HttpRoute node linked
from its handler by HANDLES; each request that one rule ties to exactly one
route links its calling declaration to it by REQUESTS. The handlers and
callers are the tree-sitter declarations of the same files, with the same
identities, so a revision that also contains tree-sitter output joins them.
"""

from hashlib import sha256
import importlib.util
import time

from .._version import __version__
from ..engine import analyze_repository
from ..service_contracts import resolve as contract_rules
from ..service_contracts.http import PLACEHOLDER
from ..service_contracts.spring import SpringConfig, extract_spring
from ..syntax import make_parser
from .base import AnalysisTool, ToolCapability, ToolMetadata
from .contracts import (
    NodeData,
    RelationshipData,
    StandardizedOutput,
    ToolExecutionMetadata,
)
from .evidence import EvidenceRule, register_rule
from .registry import register_tool

SUPPORTED_LANGUAGES = ("java",)

ROUTE_RULE = register_rule(
    EvidenceRule(
        "service-contracts.spring-route-mapping",
        "pattern_matched",
        "A method of a Spring controller (or an interface) carries a mapping "
        "annotation imported from org.springframework.web.bind.annotation whose "
        "method and literal path, joined with the type's @RequestMapping path, "
        "give this route. The servlet context path, gateways and routes "
        "registered in code are not read.",
    )
)
REQUEST_RULES = {
    contract_rules.HTTP_SERVICE_PATH: register_rule(
        EvidenceRule(
            contract_rules.HTTP_SERVICE_PATH,
            "pattern_matched",
            "The request names a service (a Feign client's name, or the URL's "
            "literal host) equal to exactly one service's spring.application.name, "
            "and its method and path fit this route of that service more "
            "specifically than any other; the value of unknown URL parts is not "
            "traced.",
        )
    ),
    contract_rules.HTTP_PATH: register_rule(
        EvidenceRule(
            contract_rules.HTTP_PATH,
            "pattern_matched",
            "The request names no known service, and its method and path fit "
            "this route more specifically than any other route in the "
            "repository; the host, base URL and gateway are not checked.",
        )
    ),
}


def _is_test_source(path):
    """A file of a Maven or Gradle test source set (``src/test/``)."""
    return path.startswith("src/test/") or "/src/test/" in path


def _evidence(rule):
    return {"evidence_type": rule.evidence_type, "rule_id": rule.rule_id}


@register_tool
class ServiceContractTool(AnalysisTool):
    def __init__(self, repo_path):
        super().__init__(repo_path)
        self.last_facts = None

    def _get_metadata(self):
        return ToolMetadata(
            name="service-contracts",
            version=__version__,
            capabilities={ToolCapability.DEPENDENCY_ANALYSIS},
            supported_languages=set(SUPPORTED_LANGUAGES),
            requires_binary=False,
            binary_name=None,
            performance_class="unmeasured",
        )

    async def check_availability(self):
        return (
            importlib.util.find_spec("tree_sitter") is not None
            and importlib.util.find_spec("tree_sitter_java") is not None
        )

    async def analyze(
        self, repo_url, languages=None, file_patterns=None, skip_patterns=None
    ):
        started = time.monotonic()
        selected = tuple(
            sorted(
                set(SUPPORTED_LANGUAGES)
                & set(SUPPORTED_LANGUAGES if languages is None else languages)
            )
        )
        if not selected:
            raise ValueError(
                "service-contracts reads " + ", ".join(SUPPORTED_LANGUAGES) + " only"
            )
        result = analyze_repository(
            self.repo_path,
            repository_id=repo_url,
            languages=selected,
            include=("*",) if file_patterns is None else tuple(file_patterns),
            exclude=() if skip_patterns is None else tuple(skip_patterns),
        )
        root = self.repo_path.resolve()
        routes, requests, unread = [], [], []
        changed = []
        config = SpringConfig(root)
        parser = None
        test_files = 0
        for coverage in result.coverage:
            if coverage.status != "analyzed":
                continue
            if _is_test_source(coverage.file):
                test_files += 1
                continue
            try:
                content = (root / coverage.file).read_bytes()
            except OSError:
                changed.append(coverage.file)
                continue
            if sha256(content).hexdigest() != coverage.digest:
                changed.append(coverage.file)
                continue
            if parser is None:
                parser = make_parser("java", ".java")
            service, context_path = config.service(coverage.file)
            found = extract_spring(
                parser.parse(content),
                content,
                repo_url,
                coverage.file,
                result.definitions,
                service=service,
                context_path=context_path,
            )
            routes.extend(found[0])
            requests.extend(found[1])
            unread.extend(found[2])
        if changed:
            raise ValueError(
                "Files changed during analysis: " + ", ".join(sorted(changed))
            )
        resolutions = contract_rules.resolve_requests(routes, requests)
        self.last_facts = (result, routes, requests, unread, resolutions)

        definitions = {d.id: d for d in result.definitions}
        languages_by_file = {c.file: c for c in result.coverage}
        files, symbols, nodes, relationships = {}, {}, [], []

        def file_node(path):
            if path not in files:
                coverage = languages_by_file[path]
                files[path] = NodeData(
                    ["File"],
                    {
                        "qualified_name": f"{repo_url}::{path}::file",
                        "repo_url": repo_url,
                        "path": path,
                        "file_path": path,
                        "name": path,
                        "language": coverage.language,
                        "coverage_status": coverage.status,
                        "file_sha256": coverage.digest,
                    },
                )
            return ["File"], files[path].properties["qualified_name"]

        def symbol_node(definition_id):
            d = definitions[definition_id]
            name = f"{repo_url}::{d.file}::{d.symbol}::{d.id}"
            if definition_id not in symbols:
                symbols[definition_id] = NodeData(
                    [d.kind, "Symbol"],
                    {
                        "qualified_name": name,
                        "repo_url": repo_url,
                        "definition_id": d.id,
                        "file_path": d.file,
                        "name": d.symbol,
                        "display_name": d.symbol,
                        "kind": d.kind,
                        "start_line": d.line,
                        "end_line": d.end_line,
                        "column": d.column,
                        "end_column": d.end_column,
                        "coordinate_encoding": d.coordinate_encoding,
                    },
                )
            return [d.kind, "Symbol"], name

        route_names = {}
        for route in routes:
            name = f"{repo_url}::{route.file}::http-route::{route.id}"
            route_names[route.id] = name
            nodes.append(
                NodeData(
                    ["HttpRoute", "Contract"],
                    {
                        "qualified_name": name,
                        "repo_url": repo_url,
                        "route_id": route.id,
                        "protocol": "http",
                        "name": f"{' '.join(route.methods)} {route.path}",
                        "display_name": f"{' '.join(route.methods)} {route.path}",
                        "methods": list(route.methods),
                        "path": route.path,
                        "framework": route.framework,
                        "file_path": route.file,
                        "line": route.line,
                        **({"service": route.service} if route.service else {}),
                    },
                )
            )
            labels, handler = symbol_node(route.handler_id)
            relationships.append(
                RelationshipData(
                    "HANDLES",
                    labels,
                    {"qualified_name": handler, "repo_url": repo_url},
                    ["HttpRoute", "Contract"],
                    {"qualified_name": name, "repo_url": repo_url},
                    {
                        "route_id": route.id,
                        "line": route.line,
                        "column": route.column,
                        **_evidence(ROUTE_RULE),
                    },
                )
            )
        by_id = {r.id: r for r in requests}
        counts = {}
        undisclosed = {}
        for resolution in resolutions:
            key = resolution.status + (
                ":" + resolution.reason if resolution.reason else ""
            )
            counts[key] = counts.get(key, 0) + 1
            request = by_id[resolution.request_id]
            written = (
                (request.method or "?")
                + " "
                + (request.url.replace(PLACEHOLDER, "{?}"))
            )
            if resolution.status != "linked":
                undisclosed.setdefault(request.file, {}).setdefault(
                    resolution.status + "_http_requests", set()
                ).add(written)
                continue
            if request.caller_id is None:
                source = file_node(request.file)
            else:
                source = symbol_node(request.caller_id)
            relationships.append(
                RelationshipData(
                    "REQUESTS",
                    source[0],
                    {"qualified_name": source[1], "repo_url": repo_url},
                    ["HttpRoute", "Contract"],
                    {
                        "qualified_name": route_names[resolution.route_id],
                        "repo_url": repo_url,
                    },
                    {
                        "request_id": request.id,
                        "client": request.client,
                        "request": written,
                        "line": request.line,
                        "column": request.column,
                        **(
                            {"url_alternatives": request.alternatives}
                            if request.alternatives > 1
                            else {}
                        ),
                        **(
                            {"target_service": resolution.target_service}
                            if resolution.target_service
                            else {}
                        ),
                        **_evidence(REQUEST_RULES[resolution.rule_id]),
                    },
                )
            )
        for path, values in undisclosed.items():
            file_node(path)
            files[path].properties.update(
                {key: sorted(items) for key, items in sorted(values.items())}
            )
        unread_counts = {}
        for item in unread:
            key = f"{item.kind}:{item.reason}"
            unread_counts[key] = unread_counts.get(key, 0) + 1
        nodes = (
            sorted(files.values(), key=lambda n: n.properties["qualified_name"])
            + sorted(symbols.values(), key=lambda n: n.properties["qualified_name"])
            + nodes
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
                "http_routes": len(routes),
                "http_requests": len(requests),
                "http_request_resolution": dict(sorted(counts.items())),
                "unread": dict(sorted(unread_counts.items())),
                "test_source_files_skipped": test_files,
                "scope": "Spring MVC routes, Feign clients and RestTemplate "
                "requests with folded paths outside test source sets; requests linked to routes by "
                "service name and path template, never by traced values",
            },
        )
        return StandardizedOutput(
            nodes=nodes, relationships=relationships, metadata=metadata
        )

    async def get_symbol_definitions(self):
        return {}
