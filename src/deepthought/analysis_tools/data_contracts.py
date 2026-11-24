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
from collections import Counter
import importlib.util
import json
import os
from pathlib import Path
import re
import time

from .._version import __version__
from ..components import PACKAGE_KINDS, SKIPPED_DIRECTORIES, scan_components
from ..service_contracts.http import PLACEHOLDER
from ..data_contracts import (
    app_config_label,
    extract_ddl,
    extract_entity_manager,
    extract_go_sql,
    extract_gorm,
    gorm_table_name,
    extract_django,
    extract_drizzle,
    resolve_script_module,
    resolve_script_alias,
    read_tsconfig_paths,
    extract_java,
    extract_java_sql,
    extract_mybatis_xml,
    extract_prisma_client,
    extract_prisma_schema,
    extract_python_sql,
    extract_ruby_models,
    extract_php_eloquent,
    resolve_eloquent,
    RAILS_ENGLISH,
    apply_rails_inflections,
    extract_sqlalchemy,
    extract_typeorm,
    extract_sequelize,
    extract_mongoose,
    mongoose_majors,
    resolve_mongoose,
    resolve_sequelize,
    merge_sequelize_defines,
    sequelize_define_options,
    UNKNOWN as SEQUELIZE_UNKNOWN,
    prisma_accessor,
    table_key,
    resolve_active_record,
    resolve_django,
    resolve_ruby_constant,
    ruby_namespaces,
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
        "The repository has this SQL file (or Prisma schema) at this path.",
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


LARAVEL_MIGRATION_RULE = register_rule(
    EvidenceRule(
        "data-contracts.laravel-migration-table",
        "pattern_matched",
        "The PHP file calls Laravel's Schema::create with this literal table "
        "name, or Schema::rename with it as the new name.",
    )
)
ELOQUENT_TABLE_RULE = register_rule(
    EvidenceRule(
        "data-contracts.eloquent-table-name",
        "pattern_matched",
        "The class is an Eloquent model (its superclass chain reaches "
        "Illuminate's Model, Authenticatable or a pivot through classes of "
        "the repository, names resolved through namespaces and use "
        "statements) whose own $table, or the nearest inherited one, is "
        "this literal.",
    )
)
ELOQUENT_DEFAULT_RULE = register_rule(
    EvidenceRule(
        "data-contracts.eloquent-default-table-name",
        "name_matched",
        "The class is a concrete Eloquent model without $table, and this "
        "table is Laravel's default getTable(): Str::snake of "
        "Str::pluralStudly of the class name, the last word pluralized by "
        "doctrine/inflector 2.0's English rules (UserProfile is "
        "user_profiles).",
    )
)
ELOQUENT_CALL_RULE = register_rule(
    EvidenceRule(
        "data-contracts.eloquent-call",
        "pattern_matched",
        "The code calls a query or persistence method statically on an "
        "Eloquent model (Model::where, ::find, ::create, ...): it reads, and "
        "it writes when the call or the chain after it creates, updates or "
        "deletes. Instance methods ($model->save()) and relations are not "
        "read.",
    )
)
LARAVEL_DB_RULE = register_rule(
    EvidenceRule(
        "data-contracts.laravel-db-table",
        "pattern_matched",
        "The code calls DB::table with this literal table: it reads, and it "
        "writes when the chain inserts, updates, upserts or deletes.",
    )
)
DRIZZLE_TABLE_RULE = register_rule(
    EvidenceRule(
        "data-contracts.drizzle-table",
        "pattern_matched",
        "A variable holds a Drizzle table: pgTable, mysqlTable, sqliteTable "
        "or singlestoreTable imported from drizzle-orm's core module, a "
        "schema's .table, or a *TableCreator whose function wraps the name "
        "in fixed text, called with this literal name.",
    )
)
DRIZZLE_QUERY_RULE = register_rule(
    EvidenceRule(
        "data-contracts.drizzle-query",
        "pattern_matched",
        "A query-builder call names the variable of a Drizzle table: from "
        "and the joins read, insert, update and delete write; "
        "db.query.<name>.findMany/findFirst reads. The variable is found "
        "through the file's declarations and relative imports, re-exports "
        "and export * included; a package import or a query key resolves "
        "to the one table variable of that name in the caller's namespace, "
        "else in the repository. The receiver is not checked to be a "
        "Drizzle database.",
    )
)
RAILS_SCHEMA_RULE = register_rule(
    EvidenceRule(
        "data-contracts.rails-schema-table",
        "resolved",
        "The Rails schema file (db/schema.rb or db/<name>_schema.rb) has a "
        "create_table call, or a Scenic create_view call, for this table.",
    )
)
RAILS_TABLE_RULE = register_rule(
    EvidenceRule(
        "data-contracts.active-record-table-name",
        "pattern_matched",
        "The class is an ActiveRecord model (its superclass chain reaches "
        "ActiveRecord::Base through classes of the repository, found by Ruby "
        "constant lookup) whose own self.table_name, or its STI base "
        "class's, is this literal.",
    )
)
RAILS_DEFAULT_RULE = register_rule(
    EvidenceRule(
        "data-contracts.active-record-default-table-name",
        "name_matched",
        "The class is a concrete ActiveRecord model without self.table_name, "
        "and this table is ActiveRecord's default name: the class name "
        "underscored and pluralized with ActiveSupport's English rules, "
        "prefixed with an enclosing module's table_name_prefix or with the "
        "singular table name of an enclosing model; an STI subclass shares "
        "its base class's table. Models with naming settings or under an "
        "application-wide naming configuration are left out.",
    )
)
RAILS_ACCESS_RULE = register_rule(
    EvidenceRule(
        "data-contracts.active-record-call",
        "pattern_matched",
        "The code calls a query method (where, find, pluck, ...) or a "
        "persistence method (create, update_all, destroy_all, ...) on a "
        "constant that Ruby's lexical lookup resolves to an ActiveRecord "
        "model of the repository. Instance methods and associations are not "
        "read.",
    )
)


SUPPORTED_LANGUAGES = (
    "go",
    "java",
    "javascript",
    "php",
    "python",
    "ruby",
    "typescript",
)
# Rails configuration that changes every default table name.
RAILS_NAMING_CONFIG = re.compile(
    rb"active_record\.(?:table_name_prefix|table_name_suffix|pluralize_table_names)"
    rb"\s*="
)
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
            "go-sql",
            "The code passes SQL naming this table (string literals, joined "
            "with + to text the code does not fix) to a database/sql, sqlx, "
            "pgx, GORM (Raw, Exec) or bun query method, in a file importing "
            "one of those packages or a SQL driver; the receiver's type is "
            "not checked.",
        ),
        (
            "mybatis-annotation",
            "The mapper method has a MyBatis @Select, @Insert, @Update or "
            "@Delete (org.apache.ibatis.annotations) whose SQL names this "
            "table; array strings are joined with spaces, a <script> value is "
            "read as mapper XML, #{} is a bind marker and ${} text the code "
            "does not fix.",
        ),
        (
            "mybatis-mapper-xml",
            "A MyBatis mapper XML statement (<select>, <insert>, <update>, "
            "<delete>) names this table, with every dynamic branch kept and "
            "<sql> fragments included; it belongs to the method of the "
            "mapper interface named by the namespace and id, else to the XML "
            "file.",
        ),
        (
            "php-sql",
            "The code passes SQL naming this table (literals, heredocs, . "
            "joins, sprintf templates and local variables built from them, "
            "what they interpolate unfixed) to a PDO or mysqli query, prepare "
            "or exec method, a Doctrine DBAL execute or fetch method, a "
            "mysqli_* or pg_* query function, or Laravel's DB::select, "
            "DB::statement and the like; the receiver's type is not checked.",
        ),
        (
            "jdbc-insert-table",
            "The code calls withTableName with this table (SimpleJdbcInsert), "
            "in a file importing org.springframework.jdbc.",
        ),
    )
}

GORM_TABLE_RULE = register_rule(
    EvidenceRule(
        "data-contracts.gorm-table-name",
        "pattern_matched",
        "The type, in a file importing a Go SQL package, has a TableName() "
        "method that returns this table name as a string literal (GORM's "
        "Tabler interface).",
    )
)
GORM_DEFAULT_RULE = register_rule(
    EvidenceRule(
        "data-contracts.gorm-default-table-name",
        "name_matched",
        "The Go struct is a GORM model (it embeds gorm.Model, or is passed "
        "as &T{} to AutoMigrate or Model in a file importing gorm.io/gorm) "
        "without a TableName method, and this table is GORM's default name: "
        "NamingStrategy's snake case of the type name, pluralized by "
        "jinzhu/inflection 1.0.0 (UserModel is user_models). A repository "
        "that configures a NamingStrategy gets no default names.",
    )
)
GORM_CALL_RULE = register_rule(
    EvidenceRule(
        "data-contracts.gorm-call",
        "pattern_matched",
        "A First, Last, Take, Find, FirstOrInit (READS), Create, Save, "
        "Delete (WRITES) or FirstOrCreate "
        "call, or a Model call chained with a reading (Find, Count, Pluck, "
        "Scan, ...) or writing (Update, Updates, Delete, ...) method before any "
        "Association, whose "
        "model argument is a GORM model: &T{}, T{}, []T{}, new(T), "
        "make([]T, n) or a parameter or local of the function declared with "
        "such a type. The receiver is not checked to be a *gorm.DB.",
    )
)
PRISMA_MODEL_RULE = register_rule(
    EvidenceRule(
        "data-contracts.prisma-model",
        "resolved",
        "The Prisma schema declares a model for this table: its @@map name, "
        "else the model name.",
    )
)
PRISMA_CLIENT_RULE = register_rule(
    EvidenceRule(
        "data-contracts.prisma-client",
        "pattern_matched",
        "The code calls a reading (findMany, findUnique, count, ...) or "
        "writing (create, update, upsert, delete, ...) Prisma Client method "
        "on a property named like a Prisma model's client name (the model "
        "name, first letter lower case): the one such model of the caller's "
        "namespace, else of the repository. The receiver is not checked to "
        "be a PrismaClient.",
    )
)
TYPEORM_TABLE_RULE = register_rule(
    EvidenceRule(
        "data-contracts.typeorm-entity-name",
        "pattern_matched",
        "The class is decorated with TypeORM's @Entity (imported from "
        "typeorm) naming this table as a string or as the name option.",
    )
)
TYPEORM_DEFAULT_RULE = register_rule(
    EvidenceRule(
        "data-contracts.typeorm-default-table-name",
        "name_matched",
        "The class is decorated with TypeORM's @Entity without a name; the "
        "table is its DefaultNamingStrategy name, the snake case of the "
        "class name. A custom naming strategy is not read.",
    )
)
TYPEORM_REPOSITORY_RULE = register_rule(
    EvidenceRule(
        "data-contracts.typeorm-repository",
        "pattern_matched",
        "In a file importing typeorm or @nestjs/typeorm, the code calls "
        "getRepository or @InjectRepository with a TypeORM entity class "
        "name: the one such entity of the caller's namespace, else of the "
        "repository. A repository reads and writes; which of its methods are "
        "called is not checked.",
    )
)

SEQUELIZE_MIGRATION_RULE = register_rule(
    EvidenceRule(
        "data-contracts.sequelize-migration-table",
        "pattern_matched",
        "The code calls createTable (or renameTable, for its new name) with "
        "this literal table on queryInterface or getQueryInterface(), as "
        "Sequelize migrations do. The receiver is not type-checked.",
    )
)
SEQUELIZE_TABLE_RULE = register_rule(
    EvidenceRule(
        "data-contracts.sequelize-table-name",
        "pattern_matched",
        "A Sequelize model (a class extending Model from sequelize or "
        "sequelize-typescript, initialized with init or decorated with "
        "@Table, or a define call) names this table with a literal "
        "tableName option.",
    )
)
SEQUELIZE_DEFAULT_RULE = register_rule(
    EvidenceRule(
        "data-contracts.sequelize-default-table-name",
        "name_matched",
        "A Sequelize model without tableName: Sequelize 6's default name, the "
        "model name pluralized by the inflection package 1.13 (underscored "
        "with underscored, kept with freezeTableName), the options of the "
        "model over the define options given to new Sequelize. beforeDefine "
        "hooks are not read.",
    )
)
SEQUELIZE_CALL_RULE = register_rule(
    EvidenceRule(
        "data-contracts.sequelize-call",
        "pattern_matched",
        "The code calls a static query (findAll, findOne, count, ...) or "
        "persistence (create, update, destroy, bulkCreate, ...) method on a "
        "Sequelize model named by an imported or local binding (User, "
        "db.User, sequelize.models.User): the one model of that name in the "
        "caller's namespace, else in the repository. Instance methods and "
        "associations are not read.",
    )
)

MONGOOSE_COLLECTION_RULE = register_rule(
    EvidenceRule(
        "data-contracts.mongoose-collection",
        "pattern_matched",
        "A Mongoose model (model(name, schema) on mongoose, a connection or "
        "the model import of mongoose) names this collection with a literal "
        "collection argument or its schema's literal collection option. A "
        "discriminator maps to its base model's collection.",
    )
)
MONGOOSE_DEFAULT_RULE = register_rule(
    EvidenceRule(
        "data-contracts.mongoose-default-collection",
        "name_matched",
        "A Mongoose model without a collection: the model name pluralized "
        "by mongoose's default pluralize, the same under every mongoose "
        "major the package manifests allow (5, 6 and 7 or later differ). "
        "Left out when the code sets its own pluralize function.",
    )
)
MONGOOSE_CALL_RULE = register_rule(
    EvidenceRule(
        "data-contracts.mongoose-call",
        "pattern_matched",
        "The code calls a static query (find, findOne, countDocuments, "
        "aggregate, ...) or write (create, updateOne, deleteMany, ...) "
        "method on a Mongoose model: a binding of the model in the file, an "
        "import that resolves to a file exporting it, mongoose.model(name) "
        "or mongoose.models.name. Instance methods (doc.save()) and query "
        "chains held in variables are not read.",
    )
)


def _nearest(candidates, scope):
    """The tables of (scope, table) candidates in the user's own namespace
    when it has any, else all of them."""
    own = {t for s, t in candidates if s == scope}
    return own or {t for _, t in candidates}


def _evidence(rule):
    return {"evidence_type": rule.evidence_type, "rule_id": rule.rule_id}


def _is_test_source(path):
    return path.startswith("src/test/") or "/src/test/" in path


def _is_ruby_test(path):
    """Rails and Laravel test trees (test/, tests/, spec/ and their nested
    copies) and vendored third-party code (vendor/, where Bundler caches
    gems and Composer installs packages)."""
    return any(
        part in ("test", "tests", "spec", "vendor") for part in path.split("/")[:-1]
    )


def _is_script_test(path):
    """JavaScript and TypeScript test sources: test trees (test/, tests/,
    __tests__/, spec/) and ``*.test.*`` / ``*.spec.*`` files."""
    parts = path.split("/")
    return any(p in ("test", "tests", "__tests__", "spec") for p in parts[:-1]) or bool(
        re.search(r"\.(?:test|spec)\.[cm]?[jt]sx?$", parts[-1])
    )


def table_name(repo_url, scope, name):
    return f"{repo_url}::{scope or '.'}::db-table::{name}"


def _sql_files(root, include, exclude, suffix=".sql"):
    from fnmatch import fnmatch

    found = []
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRECTORIES)
        for name in sorted(names):
            if not name.lower().endswith(suffix):
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

        ddl, unreadable, digests, prisma, mapper_statements = [], [], {}, [], []
        for path in _sql_files(root, include, exclude, ".xml"):
            content = (root / path).read_bytes()
            if b"<mapper" not in content:
                continue
            found = extract_mybatis_xml(path, content)
            if found is not None:
                digests[path] = sha256(content).hexdigest()
                mapper_statements.extend(found)
        for path in _sql_files(root, include, exclude, ".prisma"):
            content = (root / path).read_bytes()
            digests[path] = sha256(content).hexdigest()
            found = extract_prisma_schema(path, content)
            if found is None:
                unreadable.append(path)
            else:
                prisma.extend(found)
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
        trees, python_trees, script_trees, go_trees, ruby_trees = [], [], [], [], []
        php_trees = []
        for file, language, content in sources:
            if language in ("ruby", "php") and _is_ruby_test(file):
                continue
            if language == "go" and file.endswith("_test.go"):
                continue
            suffix = ".tsx" if file.endswith(".tsx") else ""
            if (language, suffix) not in parsers:
                parsers[language, suffix] = make_parser(language, suffix)
            tree = parsers[language, suffix].parse(content)
            {
                "java": trees,
                "python": python_trees,
                "go": go_trees,
                "ruby": ruby_trees,
                "php": php_trees,
            }.get(language, script_trees).append((file, content, tree))
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
        for path in sorted(
            {d.file for d in ddl}
            | {m.file for m in prisma}
            | {m.file for m in mapper_statements}
        ):
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
                        "language": "prisma"
                        if path.endswith(".prisma")
                        else "xml"
                        if path.endswith(".xml")
                        else "sql",
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
        go_names, go_uses = [], []
        for file, content, tree in go_trees:
            found = extract_go_sql(tree, content, file, result.definitions)
            go_names.extend(found[0])
            go_uses.extend(found[1])
        php_found = [
            (file, extract_php_eloquent(tree, content, file, result.definitions))
            for file, content, tree in php_trees
        ]
        uses = (
            go_uses
            + [item for _, found in php_found for item in found[3]]
            + [
                item
                for file, content, tree in trees
                for item in extract_java_sql(
                    tree, content, file, result.definitions, constants
                )
            ]
            + [
                item
                for file, content, tree in python_trees
                for item in extract_python_sql(tree, content, file, result.definitions)
            ]
        )
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
        # MyBatis mapper XML: the namespace names the mapper interface.
        mapper_methods = {}
        for d in result.definitions:
            if d.file.endswith(".java") and "." in d.symbol:
                owner, _, method = d.symbol.rpartition(".")
                mapper_methods.setdefault((owner, method), []).append(d)
        mybatis_counts = {"statements": 0, "not_fixed": 0, "method_found": 0}
        for item in mapper_statements:
            mybatis_counts["statements"] += 1
            owner = item.namespace.rpartition(".")[2]
            suffix = item.namespace.replace(".", "/")
            methods = [
                d
                for d in mapper_methods.get((owner, item.id), ())
                if d.file[: -len(".java")] == suffix
                or d.file[: -len(".java")].endswith("/" + suffix)
            ]
            if len(methods) == 1:
                mybatis_counts["method_found"] += 1
                source = symbol(methods[0].id)
            else:
                source = (["File"], f"{repo_url}::{item.file}::file")
            if item.statement is None:
                mybatis_counts["not_fixed"] += 1
                continue
            for kind, names in (
                ("READS", item.statement.reads),
                ("WRITES", item.statement.writes),
            ):
                for name in names:
                    link(
                        kind,
                        source,
                        table(item.file, name),
                        SQL_RULES["mybatis-mapper-xml"],
                        {"statement": f"{item.namespace}.{item.id}", "line": item.line},
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
        # GORM models: TableName methods, default names and the calls.
        gorm_models, gorm_uses, gorm_naming = [], [], False
        for file, content, tree in go_trees:
            found = extract_gorm(tree, content, file, result.definitions)
            gorm_models.extend(found[0])
            gorm_uses.extend(found[1])
            gorm_naming = gorm_naming or found[2]
        go_types = {}
        for d in result.definitions:
            if d.file.endswith(".go"):
                key = (d.file.rpartition("/")[0], d.symbol)
                go_types.setdefault(key, []).append(d)

        def owner(file, type_name):
            """The one declaration of the type in the file's package."""
            found = go_types.get((file.rpartition("/")[0], type_name), [])
            return found[0] if len(found) == 1 else None

        gorm_counts = {"linked": 0, "type_not_found": 0}
        gorm_targets = {}
        for item in go_names:
            declared = owner(item.file, item.type_name)
            if declared is None:
                gorm_counts["type_not_found"] += 1
                continue
            gorm_counts["linked"] += 1
            target = table(item.file, item.table)
            gorm_targets[(item.file.rpartition("/")[0], item.type_name)] = target
            link(
                "MAPS_TO",
                symbol(declared.id),
                target,
                GORM_TABLE_RULE,
                {"line": item.line, "column": item.column},
            )
        # A configured NamingStrategy changes every default name.
        gorm_defaults = 0
        for model in sorted(
            [] if gorm_naming else gorm_models,
            key=lambda m: (m.file, m.line, m.column),
        ):
            key = (model.file.rpartition("/")[0], model.type_name)
            if key in gorm_targets:
                continue
            declared = owner(model.file, model.type_name)
            if declared is None:
                continue
            gorm_defaults += 1
            target = table(declared.file, gorm_table_name(model.type_name))
            gorm_targets[key] = target
            link(
                "MAPS_TO",
                symbol(declared.id),
                target,
                GORM_DEFAULT_RULE,
                {"line": declared.line, "column": declared.column},
            )
        modules = sorted(
            (
                (c.name, c.manifest.rpartition("/")[0])
                for c in scan.components
                if c.kind == "go_module" and c.name
            ),
            key=lambda m: -len(m[0]),
        )

        def package_directory(import_path):
            for module, directory in modules:
                if import_path == module or import_path.startswith(module + "/"):
                    rest = import_path[len(module) :].strip("/")
                    return "/".join(p for p in (directory, rest) if p)
            return None

        gorm_access = {"linked": 0, "not_a_model": 0}
        for use in gorm_uses:
            directory = (
                use.file.rpartition("/")[0]
                if use.model.package is None
                else package_directory(use.model.package)
            )
            target = gorm_targets.get((directory, use.model.name))
            if target is None:
                gorm_access["not_a_model"] += 1
                continue
            gorm_access["linked"] += 1
            source = code(use.file, use.caller_id)
            for kind, used in (("READS", use.reads), ("WRITES", use.writes)):
                if used:
                    link(
                        kind,
                        source,
                        target,
                        GORM_CALL_RULE,
                        {
                            "model": use.model.name,
                            "line": use.line,
                            "column": use.column,
                        },
                    )
        # Prisma models and the client calls that use them.
        prisma_targets = {}
        for model in sorted(prisma, key=lambda m: (m.file, m.line)):
            target = table(model.file, table_key(model.table))
            prisma_targets.setdefault(prisma_accessor(model.model), set()).add(
                (scope(model.file), target)
            )
            link(
                "DECLARES",
                (["File"], f"{repo_url}::{model.file}::file"),
                target,
                PRISMA_MODEL_RULE,
                {"line": model.line, "model": model.model},
            )
        orm_counts = {"prisma_linked": 0, "prisma_ambiguous": 0}
        for file, content, tree in script_trees:
            for use in extract_prisma_client(
                tree, content, file, result.definitions, set(prisma_targets)
            ):
                targets = _nearest(prisma_targets[use.model], scope(use.file))
                if len(targets) != 1:
                    orm_counts["prisma_ambiguous"] += 1
                    continue
                orm_counts["prisma_linked"] += 1
                for kind, used in (("READS", use.reads), ("WRITES", use.writes)):
                    if used:
                        link(
                            kind,
                            code(use.file, use.caller_id),
                            next(iter(targets)),
                            PRISMA_CLIENT_RULE,
                            {
                                "model": use.model,
                                "line": use.line,
                                "column": use.column,
                            },
                        )
        # TypeORM entities and the repositories of them.
        typeorm_mappings, typeorm_uses = [], []
        for file, content, tree in script_trees:
            found = extract_typeorm(tree, content, file, result.definitions)
            typeorm_mappings.extend(found[0])
            typeorm_uses.extend(found[1])
        typeorm_targets = {}
        for mapping in typeorm_mappings:
            target = table(mapping.file, mapping.table)
            typeorm_targets.setdefault(mapping.name, set()).add(
                (scope(mapping.file), target)
            )
            link(
                "MAPS_TO",
                symbol(mapping.model_id),
                target,
                TYPEORM_TABLE_RULE if mapping.explicit else TYPEORM_DEFAULT_RULE,
                {"line": mapping.line, "column": mapping.column},
            )
        orm_counts.update(typeorm_linked=0, typeorm_not_an_entity=0)
        for use in typeorm_uses:
            targets = _nearest(typeorm_targets.get(use.model, set()), scope(use.file))
            if len(targets) != 1:
                orm_counts["typeorm_not_an_entity"] += 1
                continue
            orm_counts["typeorm_linked"] += 1
            source = code(use.file, use.caller_id)
            for kind in ("READS", "WRITES"):
                link(
                    kind,
                    source,
                    next(iter(targets)),
                    TYPEORM_REPOSITORY_RULE,
                    {"entity": use.model, "line": use.line, "column": use.column},
                )
        # Sequelize: migrations, models and their static calls.
        sequelize_models, sequelize_inits, sequelize_uses, defines = [], {}, [], []
        for path in _sql_files(root, include, exclude, ".json"):
            if not path.endswith("config/config.json"):
                continue
            content = (root / path).read_bytes()
            try:
                config = json.loads(content)
            except ValueError:
                continue
            stack = [config]
            while stack:
                value = stack.pop()
                if isinstance(value, dict):
                    for key, item in sorted(value.items()):
                        if key == "define":
                            digests[path] = sha256(content).hexdigest()
                            defines.append(
                                tuple(
                                    sorted(
                                        (
                                            k,
                                            v
                                            if isinstance(v, bool)
                                            else SEQUELIZE_UNKNOWN,
                                        )
                                        for k, v in item.items()
                                        if k in ("freezeTableName", "underscored")
                                    )
                                )
                                if isinstance(item, dict)
                                else None
                            )
                        else:
                            stack.append(item)
                elif isinstance(value, list):
                    stack.extend(value)
        script_files = {c.file for c in result.coverage}
        ts_paths = {}
        for path in _sql_files(root, include, exclude, "tsconfig.json"):
            content = (root / path).read_bytes()
            found = read_tsconfig_paths(path, content)
            if found is not None:
                digests[path] = sha256(content).hexdigest()
                ts_paths[path.rpartition("/")[0]] = found
        for file, content, tree in script_trees:
            models, inits, created, uses = extract_sequelize(
                tree, content, file, result.definitions
            )
            sequelize_models.extend(models)
            for name, options in inits.items():
                sequelize_inits[file, name] = options
            sequelize_uses.extend(uses)
            defines.extend(sequelize_define_options(tree.root_node, content))
            for name, line in created:
                link(
                    "DECLARES",
                    code(file, None),
                    table(file, table_key(name)),
                    SEQUELIZE_MIGRATION_RULE,
                    {"line": line},
                )
        sequelize_tables, sequelize_unresolved = resolve_sequelize(
            sequelize_models,
            sequelize_inits,
            merge_sequelize_defines(defines),
            scope,
            lambda file, module: (
                resolve_script_module(file, module, script_files)
                or resolve_script_alias(file, module, script_files, ts_paths)
            ),
        )
        sequelize_targets = {}
        for index, (name, how, names) in sorted(sequelize_tables.items()):
            model = sequelize_models[index]
            target = table(model.file, name)
            for alias in names:
                sequelize_targets.setdefault(alias, set()).add(
                    (scope(model.file), target)
                )
            link(
                "MAPS_TO",
                code(model.file, model.model_id),
                target,
                SEQUELIZE_TABLE_RULE if how == "explicit" else SEQUELIZE_DEFAULT_RULE,
                {"model": model.name, "line": model.line, "column": model.column},
            )
        sequelize_counts = {"linked": 0, "not_a_model": 0}
        for use in sequelize_uses:
            targets = _nearest(sequelize_targets.get(use.model, set()), scope(use.file))
            if len(targets) != 1:
                sequelize_counts["not_a_model"] += 1
                continue
            sequelize_counts["linked"] += 1
            source = code(use.file, use.caller_id)
            for kind, used in (("READS", use.reads), ("WRITES", use.writes)):
                if used:
                    link(
                        kind,
                        source,
                        next(iter(targets)),
                        SEQUELIZE_CALL_RULE,
                        {"model": use.model, "line": use.line, "column": use.column},
                    )
        # Mongoose: models, their collections and static calls.
        mongoose_files, mongoose_specs = {}, []
        for path in _sql_files(root, include, exclude, "package.json"):
            if not path.endswith("package.json"):
                continue
            content = (root / path).read_bytes()
            try:
                manifest = json.loads(content)
            except ValueError:
                continue
            if not isinstance(manifest, dict):
                continue
            for section in ("dependencies", "devDependencies", "peerDependencies"):
                spec = (manifest.get(section) or {}).get("mongoose")
                if isinstance(spec, str):
                    digests[path] = sha256(content).hexdigest()
                    mongoose_specs.append(spec)
        for file, content, tree in script_trees:
            if _is_script_test(file):
                continue
            found = extract_mongoose(tree, content, file, result.definitions)
            if found is not None:
                mongoose_files[file] = found
        mongoose_tables, mongoose_unresolved, mongoose_model = resolve_mongoose(
            mongoose_files,
            mongoose_majors(mongoose_specs),
            lambda file, module: (
                resolve_script_module(file, module, script_files)
                or resolve_script_alias(file, module, script_files, ts_paths)
            ),
            scope,
        )
        mongoose_targets = {}
        for (file, index), (name, how, base) in sorted(mongoose_tables.items()):
            model = mongoose_files[file].models[index]
            target = table(file, table_key(name))
            mongoose_targets[file, index] = target
            extra = {"model": model.name, "line": model.line, "column": model.column}
            if base is not None:
                extra["discriminator_of"] = mongoose_files[base[0]].models[base[1]].name
            link(
                "MAPS_TO",
                code(file, model.model_id),
                target,
                MONGOOSE_DEFAULT_RULE if how == "default" else MONGOOSE_COLLECTION_RULE,
                extra,
            )
        mongoose_counts = {"linked": 0, "unresolved_model": 0}
        has_models = any(found.models for found in mongoose_files.values())
        for file, found in sorted(mongoose_files.items()):
            for use in found.uses if has_models else ():
                key = mongoose_model(use)
                if not isinstance(key, tuple):
                    if key != "package":
                        mongoose_counts[key] = mongoose_counts.get(key, 0) + 1
                    continue
                target = mongoose_targets.get(key)
                if target is None:
                    mongoose_counts["unresolved_model"] += 1
                    continue
                mongoose_counts["linked"] += 1
                model = mongoose_files[key[0]].models[key[1]]
                source = code(use.file, use.caller_id)
                for kind, used in (("READS", use.reads), ("WRITES", use.writes)):
                    if used:
                        link(
                            kind,
                            source,
                            target,
                            MONGOOSE_CALL_RULE,
                            {
                                "model": model.name,
                                "line": use.line,
                                "column": use.column,
                            },
                        )
        # Laravel: migrations, Eloquent models and their calls.
        php_classes, php_accesses = [], []
        for file, found in php_found:
            php_classes.extend(found[0])
            php_accesses.extend(found[1])
            for name, line in found[2]:
                link(
                    "DECLARES",
                    code(file, None),
                    table(file, name),
                    LARAVEL_MIGRATION_RULE,
                    {"line": line},
                )
        eloquent_tables, eloquent_unresolved = resolve_eloquent(php_classes)
        php_by_path = {c.path: c for c in php_classes}
        eloquent_targets = {}
        for path, (name, how) in eloquent_tables.items():
            model = php_by_path[path]
            target = table(model.file, name)
            eloquent_targets[path] = target
            if model.class_id is not None:
                link(
                    "MAPS_TO",
                    symbol(model.class_id),
                    target,
                    ELOQUENT_TABLE_RULE if how == "explicit" else ELOQUENT_DEFAULT_RULE,
                    {"line": model.line, "column": model.column},
                )
        eloquent_counts = {"linked": 0, "not_a_model": 0, "not_fixed": 0}
        for use in php_accesses:
            if use.model is None:
                if use.table == PLACEHOLDER:
                    eloquent_counts["not_fixed"] += 1
                    continue
                target, rule, extra = table(use.file, use.table), LARAVEL_DB_RULE, {}
            elif use.model in eloquent_targets:
                target, rule = eloquent_targets[use.model], ELOQUENT_CALL_RULE
                extra = {"model": use.model}
            else:
                eloquent_counts["not_a_model"] += 1
                continue
            eloquent_counts["linked"] += 1
            source = code(use.file, use.caller_id)
            for kind, used in (("READS", use.reads), ("WRITES", use.writes)):
                if used:
                    link(
                        kind,
                        source,
                        target,
                        rule,
                        {**extra, "line": use.line, "column": use.column},
                    )
        # Drizzle tables and the query-builder calls that name them.
        drizzle_tables, drizzle_uses, script_modules = {}, [], {}
        for file, content, tree in script_trees:
            found = extract_drizzle(tree, content, file, result.definitions)
            for item in found[0]:
                drizzle_tables.setdefault(file, {}).setdefault(
                    item.variable, []
                ).append(item)
            drizzle_uses.extend(found[1])
            script_modules[file] = found[2]
        source_files = {c.file for c in result.coverage}
        by_variable = {}
        drizzle_counts = {"tables": 0, "not_fixed": 0}
        for file in sorted(drizzle_tables):
            for variable, items in sorted(drizzle_tables[file].items()):
                for item in items:
                    if item.table == PLACEHOLDER:
                        drizzle_counts["not_fixed"] += 1
                        continue
                    drizzle_counts["tables"] += 1
                    target = table(file, item.table)
                    link(
                        "DECLARES",
                        code(file, None),
                        target,
                        DRIZZLE_TABLE_RULE,
                        {
                            "line": item.line,
                            "column": item.column,
                            "variable": variable,
                        },
                    )
                if len(items) == 1 and items[0].table != PLACEHOLDER:
                    by_variable.setdefault(variable, set()).add(
                        (scope(file), table(file, items[0].table))
                    )

        def exported_table(file, name, seen=()):
            """The tables a module exports under a name, following
            re-exports and ``export *``."""
            if file is None or (file, name) in seen:
                return set()
            seen = (*seen, (file, name))
            items = drizzle_tables.get(file, {}).get(name)
            if items:
                return (
                    {table(file, items[0].table)}
                    if len(items) == 1 and items[0].table != PLACEHOLDER
                    else {None}
                )
            module = script_modules.get(file)
            if module is None:
                return set()
            if name in module.re_exports:
                source, original = module.re_exports[name]
                return exported_table(
                    resolve_script_module(file, source, source_files), original, seen
                )
            found = set()
            for source in module.star_exports:
                found |= exported_table(
                    resolve_script_module(file, source, source_files), name, seen
                )
            return found

        drizzle_access = {"linked": 0, "not_a_table": 0}
        for use in drizzle_uses:
            ref = use.ref
            if ref.how == "local":
                targets = exported_table(use.file, ref.name)
            elif ref.how == "import" and ref.module.startswith("."):
                targets = exported_table(
                    resolve_script_module(use.file, ref.module, source_files), ref.name
                )
            else:
                targets = _nearest(by_variable.get(ref.name, set()), scope(use.file))
            if len(targets) != 1 or None in targets:
                drizzle_access["not_a_table"] += 1
                continue
            drizzle_access["linked"] += 1
            source = code(use.file, use.caller_id)
            for kind, used in (("READS", use.reads), ("WRITES", use.writes)):
                if used:
                    link(
                        kind,
                        source,
                        next(iter(targets)),
                        DRIZZLE_QUERY_RULE,
                        {"variable": ref.name, "line": use.line, "column": use.column},
                    )
        # Rails: db/schema.rb tables, ActiveRecord models and their calls.
        ruby_classes, ruby_prefixes, ruby_accesses = [], {}, []
        rails_overridden = False
        inflections = RAILS_ENGLISH.copy()
        for file, content, tree in ruby_trees:
            found = extract_ruby_models(tree, content, file, result.definitions)
            ruby_classes.extend(found[0])
            ruby_prefixes.update(found[1])
            ruby_accesses.extend(found[2])
            for name, line in found[3]:
                link(
                    "DECLARES",
                    code(file, None),
                    table(file, name),
                    RAILS_SCHEMA_RULE,
                    {"line": line},
                )
            if file.startswith("config/") or "/config/" in file:
                if RAILS_NAMING_CONFIG.search(content) or not (
                    apply_rails_inflections(tree, content, inflections)
                ):
                    rails_overridden = True
        rails_tables, rails_unresolved = resolve_active_record(
            ruby_classes, ruby_prefixes, rails_overridden, inflections
        )
        ruby_by_path = {}
        for c in ruby_classes:
            ruby_by_path.setdefault(c.path, []).append(c)
        rails_targets = {}
        for path, (name, how) in rails_tables.items():
            model = next(
                (c for c in ruby_by_path[path] if c.superclass), ruby_by_path[path][0]
            )
            target = table(model.file, name)
            rails_targets[path] = target
            if model.class_id is not None:
                link(
                    "MAPS_TO",
                    symbol(model.class_id),
                    target,
                    RAILS_TABLE_RULE if how == "explicit" else RAILS_DEFAULT_RULE,
                    {"line": model.line, "column": model.column},
                )
        ruby_names = ruby_namespaces(ruby_by_path)
        rails_counts = {"linked": 0, "not_a_model": 0}
        for access in ruby_accesses:
            path = resolve_ruby_constant(
                access.constant, access.nesting, ruby_by_path, ruby_names
            )
            if path not in rails_targets:
                rails_counts["not_a_model"] += 1
                continue
            rails_counts["linked"] += 1
            source = code(access.file, access.caller_id)
            for kind, used in (("READS", access.reads), ("WRITES", access.writes)):
                if used:
                    link(
                        kind,
                        source,
                        rails_targets[path],
                        RAILS_ACCESS_RULE,
                        {
                            "model": path,
                            "line": access.line,
                            "column": access.column,
                        },
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
                    "mybatis_mapper_statements": mybatis_counts,
                    "entity_manager_resolution": entity_counts,
                    "gorm_table_names": gorm_counts,
                    "gorm_default_table_names": gorm_defaults,
                    "gorm_access_resolution": gorm_access,
                    "prisma_models": len(prisma),
                    "typeorm_entities": len(typeorm_mappings),
                    "orm_access_resolution": orm_counts,
                    "sequelize_models": len(sequelize_tables),
                    "sequelize_unresolved": len(sequelize_unresolved),
                    "sequelize_access_resolution": sequelize_counts,
                    "mongoose_models": len(mongoose_tables),
                    "mongoose_unresolved": dict(
                        sorted(Counter(mongoose_unresolved.values()).items())
                    ),
                    "mongoose_access_resolution": dict(sorted(mongoose_counts.items())),
                    "eloquent_models": len(eloquent_tables),
                    "eloquent_unresolved": len(eloquent_unresolved),
                    "eloquent_access_resolution": eloquent_counts,
                    "drizzle_tables": drizzle_counts,
                    "drizzle_access_resolution": drizzle_access,
                    "active_record_models": len(rails_tables),
                    "active_record_unresolved": len(rails_unresolved),
                    "active_record_access_resolution": rails_counts,
                    "scope": "tables from CREATE TABLE, Prisma schemas, "
                    "Rails schema files, Laravel and Sequelize migrations and "
                    "JPA, Django, SQLAlchemy, GORM, TypeORM, Sequelize, "
                    "Eloquent, ActiveRecord and Mongoose mappings, read and "
                    "written by "
                    "repositories, ORM calls and the SQL text code runs; per "
                    "datasource URL or nearest package",
                },
            ),
        )

    async def get_symbol_definitions(self):
        return {}
