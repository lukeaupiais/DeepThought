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
from ..components import MANIFESTS, Component, enclosing_components, scan_components
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
        "directive, Cargo.toml [package] or the pom.xml project artifactId.",
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


def _evidence(rule):
    return {"evidence_type": rule.evidence_type, "rule_id": rule.rule_id}


def component_name(repo_url, component):
    return f"{repo_url}::{component.manifest}::{component.kind}::component"


def file_name(repo_url, path):
    return f"{repo_url}::{path}::file"


def _component_node(repo_url, component):
    properties = {
        "qualified_name": component_name(repo_url, component),
        "repo_url": repo_url,
        "kind": component.kind,
        "root": component.root,
        "manifest": component.manifest,
        "file_path": component.manifest,
        "display_name": component.name or component.root or ".",
    }
    if component.name is not None:
        properties["name"] = component.name
    if component.line is not None:
        properties["start_line"] = component.line
    return NodeData(["Component"], properties)


def bind_components(nodes, repo_url):
    """PART_OF edges from every File node to its enclosing components.

    Run when tool outputs are combined, so files from every tool are linked.
    Returns (relationships, counts).
    """
    components = []
    for node in nodes:
        p = node.properties
        if "Component" in node.labels and p.get("kind") in MANIFESTS.values():
            components.append(
                (Component(p["kind"], p["manifest"], p.get("name")), node)
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
                [c.kind, c.manifest, c.name, c.line] for c in scan.components
            ],
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
            node = _component_node(repo_url, component)
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
                    "scope": "components that build manifests declare and the "
                    "nearest enclosing root of each kind; no deployment, "
                    "network or call-graph inference",
                },
            ),
        )

    async def get_symbol_definitions(self):
        return {}
