"""ServiceContractTool: HTTP routes servers declare and the requests clients make.

Routes and requests are read from Java/Spring, Python, JavaScript/TypeScript
and Go sources (see ``deepthought.service_contracts``). Each route is an
HttpRoute node linked from its handler by HANDLES (from its file when the
handler is not a declaration); each request that one rule ties to exactly one
route links its calling declaration to it by REQUESTS. The handlers and
callers are the tree-sitter declarations of the same files, with the same
identities, so a revision that also contains tree-sitter output joins them.
"""

from hashlib import sha256
import importlib.util
import re
import time

from .._version import __version__
from ..engine import analyze_repository
from ..service_contracts import grpc as grpc_contracts
from ..service_contracts import resolve as contract_rules
from ..service_contracts.http import PLACEHOLDER
from ..service_contracts.messaging import extract_messaging
from ..service_contracts.spring import (
    SpringConfig,
    _module_root,
    extract_spring,
    java_constants,
)
from ..service_contracts.web import extract_web
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

SUPPORTED_LANGUAGES = ("go", "java", "javascript", "python", "typescript")

ROUTE_RULES = {
    "spring-mvc": register_rule(
        EvidenceRule(
            "service-contracts.spring-route-mapping",
            "pattern_matched",
            "A method of a Spring controller (or an interface) carries a mapping "
            "annotation imported from org.springframework.web.bind.annotation whose "
            "method and literal path, joined with the type's @RequestMapping path, "
            "give this route. The servlet context path, gateways and routes "
            "registered in code are not read.",
        )
    ),
    "flask": register_rule(
        EvidenceRule(
            "service-contracts.flask-route",
            "pattern_matched",
            "A function carries a route decorator of a Flask app or Blueprint "
            "bound once in the file to flask.Flask or flask.Blueprint; the "
            "Blueprint's url_prefix is joined, but not the prefix given when it "
            "is registered.",
        )
    ),
    "fastapi": register_rule(
        EvidenceRule(
            "service-contracts.fastapi-route",
            "pattern_matched",
            "A function carries a route decorator of a FastAPI app or APIRouter "
            "bound once in the file; the APIRouter's own prefix is joined, but not "
            "the prefix given to include_router.",
        )
    ),
    "express": register_rule(
        EvidenceRule(
            "service-contracts.express-route",
            "pattern_matched",
            "A method call (get, post, ..., all) on an Express app or Router bound "
            "once in the file from the express module declares this path; a "
            "prefix given to app.use is not read.",
        )
    ),
    "go-net-http": register_rule(
        EvidenceRule(
            "service-contracts.go-handle-func",
            "pattern_matched",
            "A HandleFunc or Handle call in a file importing net/http or "
            "gorilla/mux declares this path, with methods from a chained "
            ".Methods(...) or a Go 1.22 'METHOD /path' pattern; a base path "
            "variable before the path is assumed to add no segments.",
        )
    ),
}


def _message_rule(rule_id, description):
    return register_rule(EvidenceRule(rule_id, "pattern_matched", description))


_SAME_NAME = (
    " Publishers and consumers meet at the destination node with the same "
    "broker and name; consumer groups, brokers and clusters are not checked."
)
MESSAGE_RULES = {
    ("spring-kafka", "publish"): _message_rule(
        "service-contracts.spring-kafka-send",
        "KafkaTemplate.send names this topic as a literal or folded value, or "
        "as the first argument of a ProducerRecord, on a name declared only "
        "with the Spring KafkaTemplate type." + _SAME_NAME,
    ),
    ("spring-kafka", "consume"): _message_rule(
        "service-contracts.spring-kafka-listener",
        "A method carries @KafkaListener from org.springframework.kafka whose "
        "topics name this topic." + _SAME_NAME,
    ),
    ("spring-amqp", "publish"): _message_rule(
        "service-contracts.spring-amqp-send",
        "RabbitTemplate.convertAndSend or send with an exchange and routing key "
        "names this exchange/key destination." + _SAME_NAME,
    ),
    ("spring-amqp", "consume"): _message_rule(
        "service-contracts.spring-rabbit-listener",
        "A method carries @RabbitListener whose queues include this queue; it "
        "receives the default exchange's messages keyed with the queue name."
        + _SAME_NAME,
    ),
    ("kafka-python", "publish"): _message_rule(
        "service-contracts.kafka-python-send",
        "send on a name bound once to kafka.KafkaProducer names this topic."
        + _SAME_NAME,
    ),
    ("kafka-python", "consume"): _message_rule(
        "service-contracts.kafka-python-consumer",
        "kafka.KafkaConsumer is constructed with this topic." + _SAME_NAME,
    ),
    ("pika", "publish"): _message_rule(
        "service-contracts.pika-publish",
        "basic_publish in a file importing pika names this exchange and routing "
        "key." + _SAME_NAME,
    ),
    ("pika", "consume"): _message_rule(
        "service-contracts.pika-consume",
        "basic_consume in a file importing pika consumes a queue that receives "
        "this destination: the default exchange keyed with the queue name, or "
        "a queue_bind of that queue in the same file." + _SAME_NAME,
    ),
    ("go-amqp", "publish"): _message_rule(
        "service-contracts.go-amqp-publish",
        "Publish in a file importing an amqp client package names this exchange "
        "and routing key." + _SAME_NAME,
    ),
    ("go-amqp", "consume"): _message_rule(
        "service-contracts.go-amqp-consume",
        "Consume in a file importing an amqp client package consumes a queue "
        "that receives this destination: the default exchange keyed with the "
        "queue name, or a QueueBind of that queue in the same file." + _SAME_NAME,
    ),
}

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
GRPC_SERVER_RULES = {
    rule_id: register_rule(EvidenceRule(rule_id, "pattern_matched", description))
    for rule_id, description in (
        (
            grpc_contracts.GO_REGISTER,
            "pb.RegisterXServer(server, impl) registers a value of type T "
            "(&T{}, new(T), or a name bound once to one of those) for service X, "
            "and T declares, in the same Go package directory, a method named "
            "as this rpc.",
        ),
        (
            grpc_contracts.PYTHON_SERVICER,
            "A class inheriting XServicer (or a class of the same file "
            "inheriting such a class) defines a method named as this rpc of "
            "service X.",
        ),
        (
            grpc_contracts.JAVA_IMPL_BASE,
            "A class extending XGrpc.XImplBase declares a method named as this "
            "rpc of service X with a lowercase first letter.",
        ),
        (
            grpc_contracts.NODE_ADD_SERVICE,
            "server.addService(pkg.X.service, {key: handler}) maps the rpc "
            "named by key to a function or method declared in the same file.",
        ),
    )
}
GRPC_CALL_RULE = register_rule(
    EvidenceRule(
        grpc_contracts.STUB_CALL,
        "pattern_matched",
        "A method is called on a stub of service X built by its generated "
        "factory (NewXClient, XStub, XGrpc.newStub/newBlockingStub/"
        "newFutureStub, new pkg.X), directly or through a name bound in the "
        "same file only to stubs of X; X is declared by exactly one proto "
        "package and the method names one of its rpcs. The channel's target "
        "address is not read.",
    )
)


def _is_test_source(path):
    """A file of a Maven or Gradle test source set (``src/test/``), or a test
    file by its language's naming convention."""
    name = path.rpartition("/")[2]
    return (
        path.startswith("src/test/")
        or "/src/test/" in path
        or name.endswith("_test.go")
        or (name.startswith("test_") and name.endswith(".py"))
        or name.endswith("_test.py")
        or re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", name) is not None
    )


def _go_packages(definitions):
    """Go directory -> (functions, methods) by name, for handler anchoring."""
    packages = {}
    for d in definitions:
        if not d.file.endswith(".go") or d.kind not in ("Function", "Method"):
            continue
        functions, methods = packages.setdefault(d.file.rpartition("/")[0], ({}, {}))
        if d.kind == "Function":
            functions.setdefault(d.symbol, []).append(d.id)
        else:
            methods.setdefault(d.symbol.rpartition(".")[2], []).append(d.id)
    return packages


def _evidence(rule):
    return {"evidence_type": rule.evidence_type, "rule_id": rule.rule_id}


@register_tool
class ServiceContractTool(AnalysisTool):
    def __init__(self, repo_path):
        super().__init__(repo_path)
        self.last_facts = None
        self.last_grpc = None

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
        return importlib.util.find_spec("tree_sitter") is not None and any(
            importlib.util.find_spec(f"tree_sitter_{name}") is not None
            for name in ("go", "java", "javascript", "python", "typescript")
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
        endpoints, messaging_unread = [], []
        changed = []
        config = SpringConfig(root)
        parsers = {}
        packages = _go_packages(result.definitions)
        test_files = 0
        grpc_sources = []
        sources = []
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
            grpc_sources.append((coverage.file, coverage.language, content))
            suffix = "." + coverage.file.rsplit(".", 1)[-1]
            key = (coverage.language, suffix if suffix == ".tsx" else "")
            if key not in parsers:
                parsers[key] = make_parser(coverage.language, suffix)
            sources.append((coverage, content, parsers[key].parse(content)))
        # Java constants other classes name (Queues.NAME), by qualified class;
        # a class declared in two files gives none.
        constants, seen = {}, set()
        for coverage, content, tree in sources:
            if coverage.language != "java":
                continue
            for qualified, fields in java_constants(tree, content).items():
                if qualified in seen:
                    constants.pop(qualified, None)
                else:
                    seen.add(qualified)
                    constants[qualified] = fields
        # A module whose Java code calls setExchange has no fixed template
        # exchange; otherwise it is spring.rabbitmq.template.exchange, or the
        # default exchange "" when that key is not set.
        exchange_set = {
            _module_root(root, coverage.file)
            for coverage, content, _ in sources
            if coverage.language == "java" and b"setExchange(" in content
        }

        def template_exchange(file):
            module = _module_root(root, file)
            if module is None or module in exchange_set:
                return None
            configured = config.values(file, "spring.rabbitmq.template.exchange")
            if not configured:
                return ""
            if len(configured) == 1 and "${" not in configured[0]:
                return configured[0]
            return None

        for coverage, content, tree in sources:
            if coverage.language == "java":
                service, context_path = config.service(coverage.file)
                found = extract_spring(
                    tree,
                    content,
                    repo_url,
                    coverage.file,
                    result.definitions,
                    service=service,
                    context_path=context_path,
                    constants=constants,
                )
            else:
                found = extract_web(
                    tree,
                    content,
                    repo_url,
                    coverage.file,
                    result.definitions,
                    coverage.language,
                    package=packages.get(coverage.file.rpartition("/")[0]),
                )
            routes.extend(found[0])
            requests.extend(found[1])
            unread.extend(found[2])
            if coverage.language in ("go", "java", "python"):
                found = extract_messaging(
                    tree,
                    content,
                    repo_url,
                    coverage.file,
                    result.definitions,
                    coverage.language,
                    constants=constants,
                    template_exchange=(
                        template_exchange(coverage.file)
                        if coverage.language == "java"
                        else None
                    ),
                )
                endpoints.extend(found[0])
                messaging_unread.extend(found[1])
        if changed:
            raise ValueError(
                "Files changed during analysis: " + ", ".join(sorted(changed))
            )
        resolutions = contract_rules.resolve_requests(routes, requests)
        self.last_facts = (result, routes, requests, unread, resolutions)
        grpc = grpc_contracts.collect(
            root,
            grpc_sources,
            result.definitions,
            repo_url,
            grpc_contracts.proto_files(
                root,
                ("*",) if file_patterns is None else tuple(file_patterns),
                () if skip_patterns is None else tuple(skip_patterns),
            ),
        )
        self.last_grpc = grpc
        self.last_messages = (endpoints, messaging_unread)

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
                        **({"base_unknown": True} if route.base_unknown else {}),
                    },
                )
            )
            if route.handler_id is None:
                labels, handler = file_node(route.file)
            else:
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
                        **_evidence(ROUTE_RULES[route.framework]),
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
        grpc_names = {}
        for (service, method), declared in sorted(grpc.index.methods.items()):
            full = f"{service}/{method}"
            name = f"{repo_url}::grpc::{full}"
            grpc_names[(service, method)] = name
            nodes.append(
                NodeData(
                    ["GrpcMethod", "Contract"],
                    {
                        "qualified_name": name,
                        "repo_url": repo_url,
                        "protocol": "grpc",
                        "name": full,
                        "display_name": full,
                        "service": service,
                        "method": method,
                        "file_path": declared[0].file,
                        "line": declared[0].line,
                        "declared_in": [f"{m.file}:{m.line}" for m in declared],
                    },
                )
            )
        for handler in grpc.handlers:
            labels, source = symbol_node(handler.handler_id)
            relationships.append(
                RelationshipData(
                    "HANDLES",
                    labels,
                    {"qualified_name": source, "repo_url": repo_url},
                    ["GrpcMethod", "Contract"],
                    {
                        "qualified_name": grpc_names[(handler.service, handler.method)],
                        "repo_url": repo_url,
                    },
                    {
                        "line": handler.line,
                        **_evidence(GRPC_SERVER_RULES[handler.rule]),
                    },
                )
            )
        grpc_counts = {}
        grpc_calls = {c.id: c for c in grpc.calls}
        for resolution in grpc.resolutions:
            key = resolution.status + (
                ":" + resolution.reason if resolution.reason else ""
            )
            grpc_counts[key] = grpc_counts.get(key, 0) + 1
            call = grpc_calls[resolution.call_id]
            written = f"{call.service}.{call.method}"
            if resolution.status != "linked":
                undisclosed.setdefault(call.file, {}).setdefault(
                    resolution.status + "_grpc_calls", set()
                ).add(written)
                continue
            if call.caller_id is None:
                source = file_node(call.file)
            else:
                source = symbol_node(call.caller_id)
            relationships.append(
                RelationshipData(
                    "REQUESTS",
                    source[0],
                    {"qualified_name": source[1], "repo_url": repo_url},
                    ["GrpcMethod", "Contract"],
                    {
                        "qualified_name": grpc_names[
                            (resolution.service, resolution.method)
                        ],
                        "repo_url": repo_url,
                    },
                    {
                        "request_id": call.id,
                        "request": written,
                        "line": call.line,
                        "column": call.column,
                        **_evidence(GRPC_CALL_RULE),
                    },
                )
            )
        for path, values in undisclosed.items():
            file_node(path)
            files[path].properties.update(
                {key: sorted(items) for key, items in sorted(values.items())}
            )
        topics = {}
        for endpoint in endpoints:
            name = (
                f"{repo_url}::{endpoint.broker}::message-destination::"
                f"{endpoint.destination}"
            )
            topics.setdefault(
                name,
                NodeData(
                    ["MessageDestination", "Contract"],
                    {
                        "qualified_name": name,
                        "repo_url": repo_url,
                        "protocol": endpoint.broker,
                        "name": f"{endpoint.broker} {endpoint.destination}",
                        "display_name": f"{endpoint.broker} {endpoint.destination}",
                        "destination": endpoint.destination,
                    },
                ),
            )
            if endpoint.declaration_id is None:
                source = file_node(endpoint.file)
            else:
                source = symbol_node(endpoint.declaration_id)
            relationships.append(
                RelationshipData(
                    "PUBLISHES" if endpoint.role == "publish" else "CONSUMES",
                    source[0],
                    {"qualified_name": source[1], "repo_url": repo_url},
                    ["MessageDestination", "Contract"],
                    {"qualified_name": name, "repo_url": repo_url},
                    {
                        "endpoint_id": endpoint.id,
                        "client": endpoint.client,
                        "line": endpoint.line,
                        "column": endpoint.column,
                        **_evidence(MESSAGE_RULES[(endpoint.client, endpoint.role)]),
                    },
                )
            )
        nodes.extend(topics[name] for name in sorted(topics))
        unread_counts = {}
        for item in unread:
            key = f"{item.kind}:{item.reason}"
            unread_counts[key] = unread_counts.get(key, 0) + 1
        for _, _, kind, reason in grpc.unread:
            key = f"grpc_{kind}:{reason}"
            unread_counts[key] = unread_counts.get(key, 0) + 1
        for item in messaging_unread:
            key = f"{item.role}:{item.reason}"
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
                "message_endpoints": len(endpoints),
                "message_destinations": len(topics),
                "test_source_files_skipped": test_files,
                "grpc_methods": len(grpc.index.methods),
                "grpc_handlers": len(grpc.handlers),
                "grpc_calls": len(grpc.calls),
                "grpc_call_resolution": dict(sorted(grpc_counts.items())),
                "proto_files": dict(sorted(grpc.proto_files.items())),
                "scope": "Spring MVC, Flask, FastAPI, Express and Go net/http "
                "routes; Feign, RestTemplate, WebClient, requests, httpx, Locust, "
                "fetch, axios, request and Go net/http requests with folded URLs, "
                "outside test source sets; requests linked to routes by service "
                "name and path template, never by traced values; gRPC "
                "methods from .proto files, their Go, Python, Java and Node "
                "servers and stub calls, matched by proto service and rpc name",
            },
        )
        return StandardizedOutput(
            nodes=nodes, relationships=relationships, metadata=metadata
        )

    async def get_symbol_definitions(self):
        return {}
