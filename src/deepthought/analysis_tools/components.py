"""Components AnalysisTool: build-manifest units, replacing weighted detection.

The original service detector added weighted points for Dockerfiles, exposed
ports, serialization calls and call-graph modularity, and reported a
confidence. This tool records only what manifests declare (see
deepthought.components); each edge names the rule that made it.
"""

from hashlib import sha256
import json
import time

from .._version import __version__
from .. import component_dependencies as deps
from .. import deployments
from ..components import (
    COMPONENT_KINDS,
    Component,
    enclosing_components,
    scan_components,
)
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
        "components.repository-contains-manifest",
        "resolved",
        "The repository has this manifest file at this path.",
    )
)
DECLARES_RULE = register_rule(
    EvidenceRule(
        "components.manifest-declares",
        "resolved",
        "The manifest declares the component: pyproject.toml [project] or "
        "[tool.poetry] name, a package.json object, the go.mod module "
        "directive, Cargo.toml [package], the pom.xml project artifactId, a "
        "Compose file's services entry, or a Kubernetes workload or Service "
        "object.",
    )
)
PART_OF_RULE = register_rule(
    EvidenceRule(
        "components.nearest-root",
        "resolved",
        "The file is under the component's root directory and no other "
        "component of the same kind has a root nearer to it. This is the "
        "directory containment build tools use to find a file's manifest; "
        "it does not show that the build includes the file (for example "
        "files a manifest excludes, or Python modules outside its package "
        "discovery).",
    )
)

_REQUIRES_TEXT = {
    deps.PATH["cargo_package"]: "A Cargo.toml dependency's path is the "
    "directory of this Cargo package.",
    deps.PATH["go_module"]: "A go.mod require of a module that a replace "
    "directive points at this go.mod's directory.",
    deps.PATH["node_package"]: "A package.json dependency's file: or link: "
    "path is the directory of this package.",
    deps.PATH["python_project"]: "A pyproject.toml dependency whose "
    "[tool.uv.sources] or Poetry path is the directory of this project.",
    deps.WORKSPACE["cargo_package"]: "A Cargo.toml dependency with workspace = "
    "true, whose entry in the nearest enclosing [workspace.dependencies] has "
    "a path to this package's directory.",
    deps.PATCH: "A Cargo.toml dependency names a package that a [patch] table "
    "in an enclosing manifest replaces with a path to this package's "
    "directory.",
    deps.WORKSPACE["node_package"]: "A package.json dependency using the "
    "workspace: protocol names this package, the only one in the repository "
    "with that name.",
    deps.WORKSPACE["python_project"]: "A pyproject.toml dependency with a "
    "workspace = true uv source names this project, the only one in the "
    "repository with that (normalized) name.",
}
REQUIRES_RULES = {
    rule: register_rule(EvidenceRule("components." + rule, "resolved", text))
    for rule, text in _REQUIRES_TEXT.items()
}
REQUIRES_RULES[deps.NAME] = register_rule(
    EvidenceRule(
        "components." + deps.NAME,
        "name_matched",
        "A declared dependency's name (Maven groupId:artifactId, Go module path, "
        "normalized Python name, npm or Cargo package name) equals the name of "
        "exactly one component of the same kind in the repository. The build "
        "may take that name from a registry instead, at another version.",
    )
)

REQUIRES_RULES[deps.COMPOSE_DEPENDS_ON] = register_rule(
    EvidenceRule(
        "components." + deps.COMPOSE_DEPENDS_ON,
        "resolved",
        "A Compose service's depends_on names this service of the same file.",
    )
)
DEPLOYS_RULES = {
    deployments.BUILD_CONTEXT: register_rule(
        EvidenceRule(
            "components." + deployments.BUILD_CONTEXT,
            "resolved",
            "The Compose service's build context is this component's root directory.",
        )
    ),
    deployments.IMAGE_NAME: register_rule(
        EvidenceRule(
            "components." + deployments.IMAGE_NAME,
            "name_matched",
            "The Compose service builds nothing; the last path segment of its "
            "image name, without tag or digest, equals the name (or the last "
            "/-segment of the name) of exactly one package component. The "
            "image may be built from other sources.",
        )
    ),
}

DEPLOYS_RULES[deployments.SKAFFOLD_CONTEXT] = register_rule(
    EvidenceRule(
        "components." + deployments.SKAFFOLD_CONTEXT,
        "resolved",
        "The workload's container images name Skaffold build artifacts with one "
        "build context, and that context is this component's root directory.",
    )
)
DEPLOYS_RULES[deployments.K8S_IMAGE_NAME] = register_rule(
    EvidenceRule(
        "components." + deployments.K8S_IMAGE_NAME,
        "name_matched",
        "The workload has one container image and no Skaffold context; the "
        "image's last path segment, without tag or digest, equals the name (or "
        "the last /-segment of the name) of exactly one package component. The "
        "image may be built from other sources.",
    )
)
SELECTS_RULE = register_rule(
    EvidenceRule(
        "components." + deployments.SERVICE_SELECTOR,
        "resolved",
        "The Kubernetes Service's nonempty spec.selector is a subset of the "
        "workload's pod template labels, both declare the same namespace (or "
        "neither does), and both are declared in the same directory. This is "
        "the selection Kubernetes applies when that directory's objects are "
        "applied as written; overlays or charts that change them are not "
        "rendered, and objects in other directories are not matched.",
    )
)


def _evidence(rule):
    return {"evidence_type": rule.evidence_type, "rule_id": rule.rule_id}


def component_name(repo_url, component):
    if component.kind in {"compose_service", "k8s_workload", "k8s_service"}:
        # These files declare several units.
        return (
            f"{repo_url}::{component.manifest}::{component.kind}::"
            f"{component.name}::component"
        )
    return f"{repo_url}::{component.manifest}::{component.kind}::component"


def file_name(repo_url, path):
    return f"{repo_url}::{path}::file"


def _component_node(repo_url, component, coordinates=None):
    properties = {
        "qualified_name": component_name(repo_url, component),
        "repo_url": repo_url,
        "kind": component.kind,
        "manifest": component.manifest,
        "file_path": component.manifest,
        "display_name": component.name or component.root or ".",
    }
    if component.root is not None:
        properties["root"] = component.root
    if component.image is not None:
        properties["image"] = component.image
    if component.kind in {"k8s_workload", "k8s_service"}:
        if component.namespace is not None:
            properties["namespace"] = component.namespace
        for key in ("labels", "selector"):
            if getattr(component, key):
                properties[key] = [k + "=" + v for k, v in getattr(component, key)]
        if component.images:
            properties["images"] = list(component.images)
    if component.name is not None:
        properties["name"] = component.name
    if component.line is not None:
        properties["start_line"] = component.line
    if coordinates is not None:
        properties["coordinates"] = coordinates
    return NodeData(["Component"], properties)


def bind_components(nodes, repo_url):
    """PART_OF edges from every File node to its enclosing components.

    Run when tool outputs are combined, so files from every tool are linked.
    Returns (relationships, counts).
    """
    components = []
    for node in nodes:
        p = node.properties
        if "Component" in node.labels and p.get("kind") in COMPONENT_KINDS:
            compose = p["kind"] in {"compose_service", "k8s_workload"}
            components.append(
                (
                    Component(
                        p["kind"],
                        p["manifest"],
                        p.get("name"),
                        context=p.get("root") if compose else None,
                    ),
                    node,
                )
            )
    relationships, linked, unlinked = [], 0, 0
    if not components:
        return relationships, {"files_linked": 0, "files_outside": 0}
    by_component = {c: node for c, node in components}
    for node in sorted(nodes, key=lambda n: n.properties["qualified_name"]):
        if "File" not in node.labels or not node.properties.get("path"):
            continue
        found = enclosing_components(node.properties["path"], by_component)
        if not found:
            unlinked += 1
            continue
        linked += 1
        for component in found:
            target = by_component[component]
            relationships.append(
                RelationshipData(
                    "PART_OF",
                    ["File"],
                    {
                        "qualified_name": node.properties["qualified_name"],
                        "repo_url": repo_url,
                    },
                    ["Component"],
                    {
                        "qualified_name": target.properties["qualified_name"],
                        "repo_url": repo_url,
                    },
                    _evidence(PART_OF_RULE),
                )
            )
    return relationships, {"files_linked": linked, "files_outside": unlinked}


@register_tool
class ComponentsTool(AnalysisTool):
    def __init__(self, repo_path):
        super().__init__(repo_path)
        self.last_scan = None

    def _get_metadata(self):
        return ToolMetadata(
            name="components",
            version=__version__,
            capabilities={ToolCapability.DEPENDENCY_ANALYSIS},
            # Languages whose build manifests it reads.
            supported_languages={
                "go",
                "java",
                "javascript",
                "python",
                "rust",
                "typescript",
            },
            requires_binary=False,
            binary_name=None,
            performance_class="fast",
        )

    async def check_availability(self):
        return self.repo_path.is_dir()

    async def analyze(
        self, repo_url, languages=None, file_patterns=None, skip_patterns=None
    ):
        """Components the selected manifests declare; ``languages`` is ignored
        because manifests belong to no source language."""
        started = time.monotonic()
        include = ("*",) if file_patterns is None else tuple(file_patterns)
        exclude = () if skip_patterns is None else tuple(skip_patterns)
        scan = scan_components(self.repo_path, include=include, exclude=exclude)
        self.last_scan = scan
        status = "succeeded" if not scan.diagnostics else "partial"
        identity = {
            "repository_id": repo_url,
            "analyzer": "components",
            "analyzer_version": __version__,
            "status": status,
            "include": include,
            "exclude": exclude,
            "manifests": scan.manifests,
            "diagnostics": scan.diagnostics,
            "components": [
                [
                    c.kind,
                    c.manifest,
                    c.name,
                    c.line,
                    c.context,
                    c.image,
                    c.namespace,
                    c.labels,
                    c.selector,
                    c.images,
                ]
                for c in scan.components
            ],
            "dependencies": [
                [d.manifest, d.section, d.name, d.path, d.workspace]
                for d in scan.dependencies
            ],
            "coordinates": scan.coordinates,
            "artifacts": scan.artifacts,
            "kubernetes": scan.kubernetes,
        }
        snapshot_id = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        repository = f"{repo_url}::repository"
        nodes = [
            NodeData(
                ["Repository"],
                {
                    "qualified_name": repository,
                    "repo_url": repo_url,
                    "name": repo_url,
                    "url": repo_url,
                },
            )
        ]
        relationships = []
        digests = dict(scan.manifests)
        for path in sorted({c.manifest for c in scan.components}):
            nodes.append(
                NodeData(
                    ["File"],
                    {
                        "qualified_name": file_name(repo_url, path),
                        "repo_url": repo_url,
                        "path": path,
                        "file_path": path,
                        "name": path,
                        "file_sha256": digests[path],
                    },
                )
            )
            relationships.append(
                RelationshipData(
                    "CONTAINS",
                    ["Repository"],
                    {"qualified_name": repository, "repo_url": repo_url},
                    ["File"],
                    {"qualified_name": file_name(repo_url, path), "repo_url": repo_url},
                    _evidence(CONTAINS_RULE),
                )
            )
        counts = {}
        for component in scan.components:
            counts[component.kind] = counts.get(component.kind, 0) + 1
            node = _component_node(
                repo_url, component, dict(scan.coordinates).get(component.manifest)
            )
            nodes.append(node)
            relationships.append(
                RelationshipData(
                    "DECLARES",
                    ["File"],
                    {
                        "qualified_name": file_name(repo_url, component.manifest),
                        "repo_url": repo_url,
                    },
                    ["Component"],
                    {
                        "qualified_name": node.properties["qualified_name"],
                        "repo_url": repo_url,
                    },
                    _evidence(DECLARES_RULE),
                )
            )
        links, dependency_counts = deps.resolve_dependencies(
            scan.components, scan.dependencies, dict(scan.coordinates)
        )
        for link in links:
            relationships.append(
                RelationshipData(
                    "REQUIRES",
                    ["Component"],
                    {
                        "qualified_name": component_name(repo_url, link.source),
                        "repo_url": repo_url,
                    },
                    ["Component"],
                    {
                        "qualified_name": component_name(repo_url, link.target),
                        "repo_url": repo_url,
                    },
                    {
                        **_evidence(REQUIRES_RULES[link.rule]),
                        "section": link.dependency.section,
                        "declared_name": link.dependency.name,
                        **(
                            {"declared_path": link.dependency.path}
                            if link.dependency.path is not None
                            else {}
                        ),
                    },
                )
            )
        for service, target, rule in deployments.resolve_deployments(scan.components):
            relationships.append(
                RelationshipData(
                    "DEPLOYS",
                    ["Component"],
                    {
                        "qualified_name": component_name(repo_url, service),
                        "repo_url": repo_url,
                    },
                    ["Component"],
                    {
                        "qualified_name": component_name(repo_url, target),
                        "repo_url": repo_url,
                    },
                    _evidence(DEPLOYS_RULES[rule]),
                )
            )
        for service, workload in deployments.resolve_selectors(scan.components):
            relationships.append(
                RelationshipData(
                    "SELECTS",
                    ["Component"],
                    {
                        "qualified_name": component_name(repo_url, service),
                        "repo_url": repo_url,
                    },
                    ["Component"],
                    {
                        "qualified_name": component_name(repo_url, workload),
                        "repo_url": repo_url,
                    },
                    _evidence(SELECTS_RULE),
                )
            )
        for workload, target, rule in deployments.resolve_workload_deployments(
            scan.components
        ):
            relationships.append(
                RelationshipData(
                    "DEPLOYS",
                    ["Component"],
                    {
                        "qualified_name": component_name(repo_url, workload),
                        "repo_url": repo_url,
                    },
                    ["Component"],
                    {
                        "qualified_name": component_name(repo_url, target),
                        "repo_url": repo_url,
                    },
                    _evidence(DEPLOYS_RULES[rule]),
                )
            )
        part_of, membership = bind_components(nodes, repo_url)
        relationships.extend(part_of)
        messages = [
            f"{path}: {message}" if path else message
            for path, message in scan.diagnostics
        ]
        return StandardizedOutput(
            nodes,
            relationships,
            ToolExecutionMetadata(
                tool_name="components",
                tool_version=__version__,
                execution_time_seconds=time.monotonic() - started,
                languages_processed=[],
                files_processed=len(scan.manifests),
                warnings=messages,
                custom_metrics={
                    "snapshot_id": snapshot_id,
                    "analysis_status": status,
                    "model_tokens": 0,
                    "capabilities": ["components"],
                    "manifests": [list(item) for item in scan.manifests],
                    "diagnostics": [list(item) for item in scan.diagnostics],
                    "component_counts": dict(sorted(counts.items())),
                    "membership": membership,
                    "dependency_resolution": dependency_counts,
                    "scope": "components that build manifests declare and the "
                    "nearest enclosing root of each kind; no deployment, "
                    "network or call-graph inference",
                },
            ),
        )

    async def get_symbol_definitions(self):
        return {}
