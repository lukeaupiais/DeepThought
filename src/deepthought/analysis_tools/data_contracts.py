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
from ..data_contracts import (
    app_config_label,
    extract_ddl,
    extract_entity_manager,
    extract_django,
    extract_java,
    extract_java_sql,
    extract_python_sql,
    extract_sqlalchemy,
    resolve_django,
)
from ..engine import analyze_repository
from ..service_contracts.spring import SpringConfig, _module_root, java_constants
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
        "tables excluded). Tables are told apart by their case-insensitive "
        "name without schema and their namespace: the module's "
        "spring.datasource.url when its Spring configuration fixes one "
        "persistent database, else the nearest package.",
    )
)
TABLE_RULE = register_rule(
    EvidenceRule(
        "data-contracts.jpa-table-annotation",
        "pattern_matched",
        "The class is a JPA @Entity (jakarta or javax.persistence) whose "
        "@Table names this table; a naming strategy that rewrites explicit "
        "names is not read.",
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
JOIN_RULE = register_rule(
    EvidenceRule(
        "data-contracts.jpa-join-table",
        "pattern_matched",
        "A field or getter of the JPA @Entity carries @JoinTable or "
        "@CollectionTable (jakarta or javax.persistence) naming this table, "
        "which holds the entity's association or element collection.",
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


SUPPORTED_LANGUAGES = ("java", "python")
# JDBC URL markers of databases that live and die with one process.
IN_MEMORY = (":mem:", ":memory:", "jdbc:derby:memory")


DJANGO_TABLE_RULE = register_rule(
    EvidenceRule(
        "data-contracts.django-db-table",
        "pattern_matched",
        "The class is a Django model (a django.db.models.Model base, directly "
        "or through models of the repository named by unique class name) "
        "whose own Meta sets db_table to this table.",
    )
)
DJANGO_DEFAULT_RULE = register_rule(
    EvidenceRule(
        "data-contracts.django-default-table-name",
        "pattern_matched",
        "The class is a concrete Django model without Meta.db_table, and this "
        "table is Django's default name: the app label, an underscore and "
        "the class name in lower case. The app label is Meta.app_label, else "
        "the literal label of the app's AppConfig in apps.py, else the name "
        "of the package holding the models module.",
    )
)
DJANGO_M2M_RULE = register_rule(
    EvidenceRule(
        "data-contracts.django-many-to-many-table",
        "pattern_matched",
        "A ManyToManyField of the model (or of an abstract model it extends) "
        "without a through model: its db_table, else the model's table, an "
        "underscore and the field name, as Django names the join table.",
    )
)
DJANGO_ACCESS_RULE = register_rule(
    EvidenceRule(
        "data-contracts.django-manager-call",
        "pattern_matched",
        "The code calls Model.objects with a QuerySet method that reads "
        "(filter, get, all, count, ...) or writes (create, update, delete, "
        "bulk_create, ...) somewhere in the chain, and Model is the one "
        "Django model of the repository with that class name. Instance "
        "save() and delete(), other managers and raw SQL are not read.",
    )
)


SA_TABLENAME_RULE = register_rule(
    EvidenceRule(
        "data-contracts.sqlalchemy-tablename",
        "pattern_matched",
        "The class, in a module that imports SQLAlchemy (or Flask-SQLAlchemy "
        "or SQLModel), sets __tablename__ to this table as a plain string.",
    )
)
SA_TABLE_RULE = register_rule(
    EvidenceRule(
        "data-contracts.sqlalchemy-table",
        "pattern_matched",
        "The code calls SQLAlchemy's Table (imported from sqlalchemy, or "
        "through an imported sqlalchemy module) with this table name as its "
        "first argument.",
    )
)
SA_QUERY_RULE = register_rule(
    EvidenceRule(
        "data-contracts.sqlalchemy-session-query",
        "pattern_matched",
        "The code calls .query() with an argument naming (or an attribute "
        "of) the one SQLAlchemy mapped class of the repository with that "
        "class name; the receiver is not checked to be a Session.",
    )
)
SA_STATEMENT_RULE = register_rule(
    EvidenceRule(
        "data-contracts.sqlalchemy-statement",
        "pattern_matched",
        "The code calls SQLAlchemy's select (READS) or insert, update or "
        "delete (WRITES) on the one mapped class of the repository with that "
        "class name.",
    )
)

SQL_RULES = {
    how: register_rule(EvidenceRule(f"data-contracts.{how}", "pattern_matched", text))
    for how, text in (
        (
            "spring-data-query",
            "The method has a Spring Data @Query whose text (JPQL, or SQL "
            "with nativeQuery = true) names this table after FROM, JOIN, "
            "INTO, UPDATE or DELETE FROM; JPQL entity names resolve to the "
            "one entity of the repository with that name.",
        ),
        (
            "jpa-query",
            "The code passes JPQL (createQuery) or SQL (createNativeQuery) "
            "naming this table or its entity, in a file importing "
            "jakarta.persistence or javax.persistence; the receiver is not "
            "checked to be an EntityManager.",
        ),
        (
            "jdbc-sql",
            "The code passes SQL naming this table to JdbcClient.sql or a "
            "JdbcTemplate query, update, batchUpdate or execute method, in a "
            "file importing org.springframework.jdbc; the receiver's type is "
            "not checked.",
        ),
        (
            "python-sql",
            "The code passes SQL naming this table (a literal, an f-string or "
            "a %/format template whose table names are fixed, or SQLAlchemy "
            "text()) to an execute, executemany, executescript, "
            "exec_driver_sql, fetch, fetchrow, fetchval or raw call, in a "
            "module importing a database driver, SQLAlchemy or django.db; "
            "the receiver's type is not checked.",
        ),
        (
            "jpa-entity-manager",
            "The code calls persist, merge or remove (WRITES) on a variable or "
            "field declared as a jakarta or javax EntityManager, passing a "
            "parameter, local or field declared as this table's entity, or "
            "find or getReference (READS) with the entity's class literal.",
        ),
        (
            "jdbc-insert-table",
            "The code calls withTableName with this table (SimpleJdbcInsert), "
            "in a file importing org.springframework.jdbc.",
        ),
    )
}


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
            supported_languages=set(SUPPORTED_LANGUAGES),
            requires_binary=False,
            binary_name=None,
            performance_class="unmeasured",
        )

    async def check_availability(self):
        return importlib.util.find_spec("tree_sitter") is not None and any(
            importlib.util.find_spec(f"tree_sitter_{language}") is not None
            for language in SUPPORTED_LANGUAGES
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
                "data-contracts reads "
                + ", ".join(SUPPORTED_LANGUAGES)
                + " (and SQL files) only"
            )
        include = ("*",) if file_patterns is None else tuple(file_patterns)
        exclude = () if skip_patterns is None else tuple(skip_patterns)
        result = analyze_repository(
            self.repo_path,
            repository_id=repo_url,
            languages=selected,
            include=include,
            exclude=exclude,
        )
        root = Path(self.repo_path).resolve()
        scan = scan_components(root, include=include, exclude=exclude)
        packages = {c: c for c in scan.components if c.kind in PACKAGE_KINDS}
        cache = {}

        config = SpringConfig(root)
        datasources = {}

        def scope(path):
            """The table namespace of a file: its module's datasource URL when
            the Spring configuration fixes one persistent database, else its
            nearest package."""
            module = _module_root(root, path)
            if module is not None:
                if module not in datasources:
                    urls = config.values(path, "spring.datasource.url") or []
                    url = urls[0] if len(urls) == 1 else None
                    datasources[module] = (
                        url
                        if url
                        and "${" not in url
                        and not any(m in url.lower() for m in IN_MEMORY)
                        else None
                    )
                if datasources[module]:
                    return "datasource:" + datasources[module]
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
            sources.append((coverage.file, coverage.language, content))
        if changed:
            raise ValueError(
                "Files changed during analysis: " + ", ".join(sorted(changed))
            )
        parsers = {}
        trees, python_trees = [], []
        for file, language, content in sources:
            if language not in parsers:
                parsers[language] = make_parser(
                    language, ".java" if language == "java" else ".py"
                )
            tree = parsers[language].parse(content)
            (trees if language == "java" else python_trees).append(
                (file, content, tree)
            )
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
            if mapping.member is None:
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
        files = []

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
            if len(entities.get(mapping.entity, ())) != 1:
                continue
            target = table(mapping.file, mapping.name)
            if mapping.member is None:
                mapped[mapping.entity] = target
            link(
                "MAPS_TO",
                symbol(mapping.entity_id),
                target,
                JOIN_RULE
                if mapping.member is not None
                else TABLE_RULE
                if mapping.explicit
                else DEFAULT_RULE,
                {
                    "line": mapping.line,
                    "column": mapping.column,
                    **({"member": mapping.member} if mapping.member else {}),
                },
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
        # Django models, their tables and the manager calls that use them.
        labels = {}
        for file, content, tree in python_trees:
            if file.rpartition("/")[2] == "apps.py":
                label = app_config_label(tree, content)
                if label:
                    labels[file.rpartition("/")[0]] = label
        models, accesses = [], []
        for file, content, tree in python_trees:
            found = extract_django(tree, content, file, result.definitions)
            models.extend(found[0])
            accesses.extend(found[1])
        model_tables, m2m = resolve_django(models, labels)
        by_id = {m.model_id: m for m in models}
        django_targets = {}
        for model_id, (name, how) in sorted(model_tables.items()):
            model = by_id[model_id]
            target = table(model.file, name)
            if not model.proxy:
                link(
                    "MAPS_TO",
                    symbol(model_id),
                    target,
                    DJANGO_TABLE_RULE if how == "explicit" else DJANGO_DEFAULT_RULE,
                    {"line": model.line, "column": model.column},
                )
            django_targets.setdefault(model.name, []).append(target)
        for model_id, field, name in m2m:
            model = by_id[model_id]
            link(
                "MAPS_TO",
                symbol(model_id),
                table(model.file, name),
                DJANGO_M2M_RULE,
                {"line": model.line, "column": model.column, "member": field},
            )
        coverage_by_file = {c.file: c for c in result.coverage}
        file_nodes = {}

        def code(file, caller_id):
            """The declaration that holds a use, else its file."""
            if caller_id is not None:
                return symbol(caller_id)
            name = f"{repo_url}::{file}::file"
            if file not in file_nodes:
                coverage = coverage_by_file[file]
                file_nodes[file] = NodeData(
                    ["File"],
                    {
                        "qualified_name": name,
                        "repo_url": repo_url,
                        "path": file,
                        "file_path": file,
                        "name": file,
                        "language": coverage.language,
                        "file_sha256": coverage.digest,
                    },
                )
            return ["File"], name

        django_counts = {"linked": 0, "not_a_model": 0}
        for access in accesses:
            targets = django_targets.get(access.model, [])
            if len(targets) != 1:
                django_counts["not_a_model"] += 1
                continue
            django_counts["linked"] += 1
            source = code(access.file, access.caller_id)
            for kind, used in (("READS", access.reads), ("WRITES", access.writes)):
                if used:
                    link(
                        kind,
                        source,
                        targets[0],
                        DJANGO_ACCESS_RULE,
                        {
                            "model": access.model,
                            "line": access.line,
                            "column": access.column,
                        },
                    )
        # SQLAlchemy mapped classes, Table() declarations and their uses.
        sa_mappings, sa_tables, sa_accesses = [], [], []
        for file, content, tree in python_trees:
            found = extract_sqlalchemy(tree, content, file, result.definitions)
            sa_mappings.extend(found[0])
            sa_tables.extend(found[1])
            sa_accesses.extend(found[2])
        sa_targets = {}
        for mapping in sa_mappings:
            target = table(mapping.file, mapping.table)
            sa_targets.setdefault(mapping.name, []).append(target)
            link(
                "MAPS_TO",
                symbol(mapping.model_id),
                target,
                SA_TABLENAME_RULE,
                {"line": mapping.line, "column": mapping.column},
            )
        for item in sa_tables:
            link(
                "DECLARES",
                code(item.file, item.owner_id),
                table(item.file, item.table),
                SA_TABLE_RULE,
                {"line": item.line, "column": item.column},
            )
        sa_counts = {"linked": 0, "not_a_mapped_class": 0}
        for access in sa_accesses:
            targets = sa_targets.get(access.model, [])
            if len(targets) != 1:
                sa_counts["not_a_mapped_class"] += 1
                continue
            sa_counts["linked"] += 1
            link(
                "WRITES" if access.writes else "READS",
                code(access.file, access.caller_id),
                targets[0],
                SA_QUERY_RULE if access.how == "query" else SA_STATEMENT_RULE,
                {"model": access.model, "line": access.line, "column": access.column},
            )
        # SQL and JPQL text that Java code runs.
        entity_tables = {}
        for mapping in mappings:
            if mapping.member is None and mapping.entity_name:
                entity_tables.setdefault(mapping.entity_name, set()).add(
                    mapped.get(mapping.entity)
                )
        sql_counts = {"statements": 0, "not_fixed": 0, "unknown_entity": 0}
        uses = [
            item
            for file, content, tree in trees
            for item in extract_java_sql(
                tree, content, file, result.definitions, constants
            )
        ] + [
            item
            for file, content, tree in python_trees
            for item in extract_python_sql(tree, content, file, result.definitions)
        ]
        for item in uses:
            sql_counts["statements"] += 1
            if item.statement is None:
                sql_counts["not_fixed"] += 1
                continue
            for kind, names in (
                ("READS", item.statement.reads),
                ("WRITES", item.statement.writes),
            ):
                for name in names:
                    if item.jpql:
                        targets = entity_tables.get(name, set())
                        if len(targets) != 1 or None in targets:
                            sql_counts["unknown_entity"] += 1
                            continue
                        target, extra = next(iter(targets)), {"entity": name}
                    else:
                        target, extra = table(item.file, name), {}
                    link(
                        kind,
                        code(item.file, item.owner_id),
                        target,
                        SQL_RULES[item.how],
                        {**extra, "line": item.line, "column": item.column},
                    )
        entity_counts = {"linked": 0, "not_an_entity": 0}
        for file, content, tree in trees:
            for use in extract_entity_manager(tree, content, file, result.definitions):
                entity = next(
                    (
                        c
                        for c in use.candidates
                        if len(entities.get(c, ())) == 1 and c in mapped
                    ),
                    None,
                )
                if entity is None:
                    entity_counts["not_an_entity"] += 1
                    continue
                entity_counts["linked"] += 1
                link(
                    "WRITES" if use.writes else "READS",
                    code(use.file, use.owner_id),
                    mapped[entity],
                    SQL_RULES["jpa-entity-manager"],
                    {"entity": entity, "line": use.line, "column": use.column},
                )
        files.extend(file_nodes[f] for f in sorted(file_nodes))
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
                    "entities": len(entities),
                    "association_tables": sum(1 for m in mappings if m.member),
                    "repositories": len(repositories),
                    "repository_resolution": counts,
                    "django_models": len(model_tables),
                    "django_access_resolution": django_counts,
                    "sqlalchemy_mappings": len(sa_mappings),
                    "sqlalchemy_tables": len(sa_tables),
                    "sqlalchemy_access_resolution": sa_counts,
                    "sql_text": sql_counts,
                    "entity_manager_resolution": entity_counts,
                    "scope": "tables from CREATE TABLE, JPA, Django and "
                    "SQLAlchemy mappings, read and written by repositories, "
                    "manager and session calls and the SQL text Java code runs; "
                    "per datasource URL or nearest package",
                },
            ),
        )

    async def get_symbol_definitions(self):
        return {}
