"""DataContractTool: database tables and the code that maps and accesses them.

Tables come from SQL ``CREATE TABLE`` statements and JPA entity mappings, and
Spring Data repositories read and write their entities' tables (see
``deepthought.data_contracts``). Each table is a DbTable node, a Contract
like an HTTP route: writers affect readers the way publishers affect
consumers. Entities and repositories are the tree-sitter declarations of the
same files, with the same identities, so a revision that also contains
tree-sitter output joins them.
"""

from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import time

from .._version import __version__
from ..components import PACKAGE_KINDS, SKIPPED_DIRECTORIES, scan_components
from ..data_contracts import extract_ddl, extract_java
from ..engine import analyze_repository
from ..service_contracts.spring import java_constants
from ..syntax import make_parser
from .base import AnalysisTool, ToolCapability, ToolMetadata
from .components import _nearest_packages
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
        "data-contracts.repository-contains-sql",
        "resolved",
        "The repository has this SQL file at this path.",
    )
)
DDL_RULE = register_rule(
    EvidenceRule(
        "data-contracts.sql-create-table",
        "resolved",
        "The SQL file has a CREATE TABLE statement for this table (temporary "
        "tables excluded). Tables are told apart by their nearest package and "
        "their case-insensitive name without schema.",
    )
)
TABLE_RULE = register_rule(
    EvidenceRule(
        "data-contracts.jpa-table-annotation",
        "pattern_matched",
        "The class is a JPA @Entity (jakarta or javax.persistence) whose "
        "@Table names this table; the datasource and any naming strategy "
        "that rewrites explicit names are not read.",
    )
)
DEFAULT_RULE = register_rule(
    EvidenceRule(
        "data-contracts.jpa-default-table-name",
        "name_matched",
        "The class is a JPA @Entity without a @Table name, and this table's "
        "name is the entity name in Spring Boot's default physical naming "
        "(CamelCaseToUnderscoresNamingStrategy). Another naming strategy "
        "gives another name.",
    )
)
REPOSITORY_RULE = register_rule(
    EvidenceRule(
        "data-contracts.spring-data-repository",
        "pattern_matched",
        "The interface extends a Spring Data CRUD (READS and WRITES) or "
        "paging-and-sorting (READS) repository whose entity type argument is "
        "the JPA entity mapped to this table. Which inherited methods are "
        "called is not checked.",
    )
)


def _evidence(rule):
    return {"evidence_type": rule.evidence_type, "rule_id": rule.rule_id}


def _is_test_source(path):
    return path.startswith("src/test/") or "/src/test/" in path


def table_name(repo_url, scope, name):
    return f"{repo_url}::{scope or '.'}::db-table::{name}"


def _sql_files(root, include, exclude):
    from fnmatch import fnmatch

    found = []
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRECTORIES)
        for name in sorted(names):
            if not name.lower().endswith(".sql"):
                continue
            path = os.path.relpath(os.path.join(directory, name), root).replace(
                os.sep, "/"
            )
            if (
                any(fnmatch(path, p) for p in include)
                and not any(fnmatch(path, p) for p in exclude)
                and not _is_test_source(path)
            ):
                found.append(path)
    return found


@register_tool
class DataContractTool(AnalysisTool):
    def __init__(self, repo_path):
        super().__init__(repo_path)
        self.last_facts = None

    def _get_metadata(self):
        return ToolMetadata(
            name="data-contracts",
            version=__version__,
            capabilities={ToolCapability.DEPENDENCY_ANALYSIS},
            supported_languages={"java"},
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
        if languages is not None and "java" not in set(languages):
            raise ValueError("data-contracts reads java (and SQL files) only")
        include = ("*",) if file_patterns is None else tuple(file_patterns)
        exclude = () if skip_patterns is None else tuple(skip_patterns)
        result = analyze_repository(
            self.repo_path,
            repository_id=repo_url,
            languages=("java",),
            include=include,
            exclude=exclude,
        )
        root = Path(self.repo_path).resolve()
        scan = scan_components(root, include=include, exclude=exclude)
        packages = {c: c for c in scan.components if c.kind in PACKAGE_KINDS}
        cache = {}

        def scope(path):
            nearest = _nearest_packages(path, packages, cache)
            return min((c.manifest for c in nearest), default="")

        ddl, unreadable, digests = [], [], {}
        for path in _sql_files(root, include, exclude):
            content = (root / path).read_bytes()
            digests[path] = sha256(content).hexdigest()
            found = extract_ddl(path, content)
            if found is None:
                unreadable.append(path)
            else:
                ddl.extend(found)
        sources, changed = [], []
        for coverage in result.coverage:
            if coverage.status != "analyzed" or _is_test_source(coverage.file):
                continue
            try:
                content = (root / coverage.file).read_bytes()
            except OSError:
                changed.append(coverage.file)
                continue
            if sha256(content).hexdigest() != coverage.digest:
                changed.append(coverage.file)
                continue
            sources.append((coverage.file, content))
        if changed:
            raise ValueError(
                "Files changed during analysis: " + ", ".join(sorted(changed))
            )
        parser = make_parser("java", ".java") if sources else None
        trees = [(f, c, parser.parse(c)) for f, c in sources]
        constants, seen = {}, set()
        for _, content, tree in trees:
            for qualified, fields in java_constants(tree, content).items():
                if qualified in seen:
                    constants.pop(qualified, None)
                else:
                    seen.add(qualified)
                    constants[qualified] = fields
        mappings, repositories = [], []
        for file, content, tree in trees:
            found = extract_java(tree, content, file, result.definitions, constants)
            mappings.extend(found[0])
            repositories.extend(found[1])
        # A class name mapped in two files gives no entity.
        entities = {}
        for mapping in mappings:
            entities.setdefault(mapping.entity, []).append(mapping)
        resolved, counts = [], {"linked": 0, "not_an_entity": 0}
        for repository in repositories:
            target = next(
                (
                    entities[c][0]
                    for c in repository.candidates
                    if len(entities.get(c, ())) == 1
                ),
                None,
            )
            if target is None:
                counts["not_an_entity"] += 1
            else:
                counts["linked"] += 1
                resolved.append((repository, target))
        self.last_facts = (result, ddl, mappings, repositories, resolved)

        definitions = {d.id: d for d in result.definitions}
        tables, nodes, relationships, symbols = {}, [], [], {}

        def table(path, name):
            key = (scope(path), name)
            if key not in tables:
                tables[key] = NodeData(
                    ["DbTable", "Contract"],
                    {
                        "qualified_name": table_name(repo_url, *key),
                        "repo_url": repo_url,
                        "name": name,
                        "display_name": name,
                        "protocol": "sql",
                        "scope": key[0],
                    },
                )
            return tables[key].properties["qualified_name"]

        def symbol(definition_id):
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

        def link(kind, source, target, rule, extra):
            relationships.append(
                RelationshipData(
                    kind,
                    source[0],
                    {"qualified_name": source[1], "repo_url": repo_url},
                    ["DbTable", "Contract"],
                    {"qualified_name": target, "repo_url": repo_url},
                    {**extra, **_evidence(rule)},
                )
            )

        repository_name = f"{repo_url}::repository"
        files = []
        for path in sorted({d.file for d in ddl}):
            file_name = f"{repo_url}::{path}::file"
            files.append(
                NodeData(
                    ["File"],
                    {
                        "qualified_name": file_name,
                        "repo_url": repo_url,
                        "path": path,
                        "file_path": path,
                        "name": path,
                        "language": "sql",
                        "file_sha256": digests[path],
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
        for item in sorted(set(ddl), key=lambda d: (d.file, d.line, d.name)):
            link(
                "DECLARES",
                (["File"], f"{repo_url}::{item.file}::file"),
                table(item.file, item.name),
                DDL_RULE,
                {"line": item.line, **({"schema": item.schema} if item.schema else {})},
            )
        mapped = {}
        for mapping in mappings:
            if len(entities[mapping.entity]) != 1:
                continue
            target = table(mapping.file, mapping.name)
            mapped[mapping.entity] = target
            link(
                "MAPS_TO",
                symbol(mapping.entity_id),
                target,
                TABLE_RULE if mapping.explicit else DEFAULT_RULE,
                {"line": mapping.line, "column": mapping.column},
            )
        for repository, entity in resolved:
            source = symbol(repository.repository_id)
            for kind in ("READS", "WRITES") if repository.writes else ("READS",):
                link(
                    kind,
                    source,
                    mapped[entity.entity],
                    REPOSITORY_RULE,
                    {
                        "entity": entity.entity,
                        "line": repository.line,
                        "column": repository.column,
                    },
                )
        nodes = (
            [
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
            + files
            + sorted(symbols.values(), key=lambda n: n.properties["qualified_name"])
            + sorted(tables.values(), key=lambda n: n.properties["qualified_name"])
        )
        status = (
            "partial" if unreadable or result.status != "succeeded" else "succeeded"
        )
        warnings = [f"{path}: not UTF-8 text" for path in unreadable]
        identity = {
            "analyzer": "data-contracts",
            "analyzer_version": __version__,
            "java_snapshot": result.snapshot_id,
            "sql": sorted(digests.items()),
            "manifests": scan.manifests,
            "status": status,
        }
        snapshot_id = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return StandardizedOutput(
            nodes,
            relationships,
            ToolExecutionMetadata(
                tool_name="data-contracts",
                tool_version=__version__,
                execution_time_seconds=time.monotonic() - started,
                languages_processed=["java"] + (["sql"] if digests else []),
                files_processed=len(result.coverage) + len(digests),
                warnings=warnings,
                custom_metrics={
                    "snapshot_id": snapshot_id,
                    "analysis_status": status,
                    "model_tokens": 0,
                    "capabilities": ["data-contracts"],
                    "tables": len(tables),
                    "ddl_statements": len(ddl),
                    "entities": len(mappings),
                    "repositories": len(repositories),
                    "repository_resolution": counts,
                    "scope": "tables from CREATE TABLE and JPA entity mappings, "
                    "read and written by Spring Data repositories; per nearest "
                    "package, without datasource configuration or SQL strings",
                },
            ),
        )

    async def get_symbol_definitions(self):
        return {}
