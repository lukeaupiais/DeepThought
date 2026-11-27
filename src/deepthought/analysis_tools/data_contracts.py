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
from collections import Counter, defaultdict
from dataclasses import replace
import importlib.util
import json
import os
import posixpath
from pathlib import Path
import re
import time

from .._version import __version__
from .. import go_facts as gofacts
from .. import ts_modules
from ..gorm_naming import table_names as gorm_table_names
from ..django_naming import column_names as django_column_names
from ..typeorm_naming import column_names as typeorm_column_names
from ..typeorm_naming import table_names as typeorm_table_names
from ..components import PACKAGE_KINDS, SKIPPED_DIRECTORIES, scan_components
from ..service_contracts.http import PLACEHOLDER
from ..data_contracts import (
    _owner_at,
    _descendants,
    DJANGO_NO_COLUMN,
    django_chain,
    django_columns,
    PRISMA_INPUT_KINDS,
    prisma_columns,
    extract_typeorm_classes,
    typeorm_key_access,
    TYPEORM_METHOD_SYMBOL,
    typeorm_columns as typeorm_call_columns,
    prisma_assignments,
    prisma_input_columns,
    prisma_typed_inputs,
    drizzle_helper_name,
    drizzle_spread_columns,
    statement_columns,
    app_config_label,
    extract_ddl,
    drizzle_foreign_keys,
    extract_column_changes,
    extract_schema_changes,
    extract_foreign_keys,
    is_rollback_migration,
    migration_set,
    replay_foreign_keys,
    replay_tables,
    extract_entity_manager,
    extract_go_sql,
    go_sql_from_facts,
    GORM_MODULES,
    GoTableName,
    extract_gorm,
    django_app,
    django_historical_model,
    extract_django,
    extract_drizzle,
    resolve_script_module,
    resolve_script_alias,
    read_tsconfig_paths,
    extract_java,
    extract_java_sql,
    extract_mybatis_xml,
    extract_prisma_client,
    extract_python_sql,
    extract_script_sql,
    extract_ruby_models,
    extract_php_eloquent,
    resolve_eloquent,
    RAILS_ENGLISH,
    apply_rails_inflections,
    extract_sqlalchemy,
    SESSION_METHOD_SYMBOL,
    annotated_classes,
    sqlmodel_default_mappings,
    extract_typeorm,
    extract_typeorm_calls,
    typeorm_builder,
    typeorm_call_entities,
    _TYPEORM_WRITES,
    extract_sequelize,
    extract_mongodb_collections,
    extract_knex,
    extract_kysely,
    imports_kysely,
    KYSELY_METHODS,
    KYSELY_MODULE,
    script_string_constants,
    KNEX_MODULES,
    _MONGODB_MODULES as MONGODB_MODULES,
    extract_mongoose,
    extract_php_doctrine,
    doctrine_xml,
    doctrine_strategies,
    resolve_doctrine,
    mongoose_majors,
    resolve_mongoose,
    resolve_workspace_module,
    read_package_entries,
    read_tsconfig_out,
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
from ..calls import name_bindings
from ..engine import analyze_repository
from ..prisma_schema import SchemaRead, read_schemas
from ..scip_calls import ScipReferences
from .. import deploy_facts
from ..deployments import compose_databases
from ..service_contracts.spring import SpringConfig, _module_root, java_constants
from ..syntax import make_parser, parse_source, script_source
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
DDL_COLUMN_RULE = register_rule(
    EvidenceRule(
        "data-contracts.sql-column",
        "resolved",
        "sqlglot parses the CREATE TABLE statement that declares this column, "
        "or the ALTER TABLE ... ADD or RENAME COLUMN (MySQL CHANGE) that gives "
        "it this name, and every ALTER TABLE of the table's migration set, "
        "replayed in order: a column a later statement drops or renames is "
        "left out.",
    )
)

TABLE_COLUMN_RULE = register_rule(
    EvidenceRule(
        "data-contracts.table-has-column",
        "resolved",
        "The column belongs to the table in the replayed schema (see "
        "data-contracts.sql-column).",
    )
)

DDL_RULE = register_rule(
    EvidenceRule(
        "data-contracts.sql-create-table",
        "resolved",
        "The SQL file has a CREATE TABLE statement for this table (temporary "
        "tables excluded), or the ALTER TABLE ... RENAME TO that gives an "
        "earlier table this name; a table a later file of the same migration "
        "set drops or renames, or that only a rollback (down) migration "
        "creates, is left out. Tables are told apart by their case-insensitive "
        "name without schema and their namespace: the module's "
        "spring.datasource.url when its Spring configuration fixes one "
        "persistent database, else the one database that the Compose "
        "services building the file's deepest build context name in "
        "connection URLs, else the nearest package.",
    )
)
SQL_FOREIGN_KEY_RULE = register_rule(
    EvidenceRule(
        "data-contracts.sql-foreign-key",
        "resolved",
        "A SQL file's CREATE TABLE or ALTER TABLE statement, parsed by "
        "sqlglot, declares a foreign key of this table (a column's "
        "REFERENCES constraint or a FOREIGN KEY constraint) to the referenced "
        "table. Tables are told apart as data-contracts.sql-create-table "
        "tells them apart.",
    )
)
DRIZZLE_FOREIGN_KEY_RULE = register_rule(
    EvidenceRule(
        "data-contracts.drizzle-foreign-key",
        "resolved",
        "drizzle-kit's snapshot of the schema after the last migration of a "
        "migrations folder (the last entry of meta/_journal.json) holds a "
        "foreign key of this table to the referenced table. The folder's "
        "SQL migrations are not read for keys, so a key a later migration "
        "drops, or one added inside a DO block, follows the snapshot. When "
        "that snapshot cannot be read, the migrations are read as any SQL "
        "file (data-contracts.sql-foreign-key) and a drizzle_snapshot_unread "
        "warning says so.",
    )
)
PRISMA_FOREIGN_KEY_RULE = register_rule(
    EvidenceRule(
        "data-contracts.prisma-foreign-key",
        "resolved",
        "Prisma's own schema engine reads a relation field with fields and "
        "references (@relation(fields: [...], references: [...])) on this "
        "table's model: the model's table holds a foreign key to the related "
        "model's table. Implicit many-to-many join tables are not read.",
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
        "through the file's declarations and its imports (relative, "
        "tsconfig paths aliases and workspace packages), re-exports and "
        "export * included; an import it cannot follow to its end or a "
        "query key resolves to the one table variable of that name in the "
        "caller's namespace, else in the repository. The receiver is not checked to be a "
        "Drizzle database.",
    )
)
DRIZZLE_COLUMN_READ_RULE = register_rule(
    EvidenceRule(
        "data-contracts.drizzle-scip-column-read",
        "resolved",
        "A scip-typescript index resolves a property access (users.email, "
        "or a selected row's email) to the property of a Drizzle table's "
        "columns object, and that property's column (its builder's name "
        "argument, else its key) is a column of the table "
        "(data-contracts.sql-column).",
    )
)
DRIZZLE_COLUMN_KEY_RULE = register_rule(
    EvidenceRule(
        "data-contracts.drizzle-column-key",
        "pattern_matched",
        "A key of the object a Drizzle insert's values, an update's set or "
        "onConflictDoUpdate's set writes, or of a relational query's "
        "columns reads, on a call data-contracts.drizzle-scip resolves to "
        "the table, names one of the table's column properties; Drizzle "
        "types those objects by the column keys, which the index does not "
        "record as references.",
    )
)
DRIZZLE_SCIP_RULE = register_rule(
    EvidenceRule(
        "data-contracts.drizzle-scip",
        "resolved",
        "A scip-typescript index resolves the called method to drizzle-orm's "
        "own query builder (from and the joins read, insert, update and "
        "delete write, a relational query's findMany/findFirst reads) and "
        "the written table name (the argument, the property of an imported "
        "object or the db.query key) to the variable that holds a Drizzle "
        "table (data-contracts.drizzle-table), through imports, re-exports "
        "and export * as the index follows them.",
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
        "data-contracts.django-scip-manager-call",
        "resolved",
        "The code calls Model.objects with a QuerySet method that reads "
        "(filter, get, all, count, ...) or writes (create, update, delete, "
        "bulk_create, ...) somewhere in the chain, and a scip-python index "
        "resolves the written Model to a Django model class. Instance save() "
        "and delete(), other managers and raw SQL are not read.",
    )
)
DJANGO_HISTORICAL_RULE = register_rule(
    EvidenceRule(
        "data-contracts.django-historical-manager-call",
        "pattern_matched",
        "As django-scip-manager-call, but the scip-python index resolves the "
        'written Model to a variable assigned get_model("label", "Model") '
        "(a data migration's historical model): the model with that app label "
        "and class name.",
    )
)


DJANGO_COLUMN_RULE = register_rule(
    EvidenceRule(
        "data-contracts.django-column",
        "resolved",
        "A field of a Django model (or of an abstract model it extends) "
        "assigned a Django field class: its column is the one Django's own "
        "Field.get_attname_column() gives, at the Django release the project "
        "declares (db_column, else the attname: author_id for a ForeignKey "
        "author); the implicit id of a model without a primary key. Declared "
        "for a table whose migrations give no columns.",
    )
)
DJANGO_COLUMN_ACCESS_RULE = register_rule(
    EvidenceRule(
        "data-contracts.django-scip-column-access",
        "resolved",
        "A scip-python index resolves an attribute access to a field of a "
        "Django model (data-contracts.django-column): an assignment to it "
        "writes the column, an augmented assignment reads and writes it, any "
        "other access reads it.",
    )
)
DJANGO_COLUMN_KEY_RULE = register_rule(
    EvidenceRule(
        "data-contracts.django-column-key",
        "pattern_matched",
        "A lookup or field name of a Django query on a model the index "
        "resolves (data-contracts.django-scip-manager-call): keyword lookups "
        "of filter, exclude, get and the like and of Q(), field names of "
        "values, only, order_by and the like and of F() and aggregates read, "
        "followed through relations (author__name reads author_id and the "
        "author's name) and reverse relations (by related_query_name, else "
        "related_name, else the model name); keywords of update and create, "
        "defaults of get_or_create and a Model(...) constructor's keywords "
        "write, and get_or_create/update_or_create lookups read and write. Read "
        "against the model's fields, as scip-python does not resolve keyword "
        "lookups or field-name strings.",
    )
)
SA_COLUMN_RULE = register_rule(
    EvidenceRule(
        "data-contracts.sqlalchemy-column",
        "pattern_matched",
        "An attribute of a SQLAlchemy mapped class (or of a mixin or "
        "single-table base it inherits) assigned Column(...) or "
        "mapped_column(...), annotated Mapped[...] alone, or an annotated "
        "field of a SQLModel table: its column is the name written in the "
        "call, else the attribute's name (SQLAlchemy computes no column "
        "name). Declared for a table whose migrations give no columns.",
    )
)
SA_COLUMN_ACCESS_RULE = register_rule(
    EvidenceRule(
        "data-contracts.sqlalchemy-scip-column-access",
        "resolved",
        "A scip-python index resolves an attribute access to a column "
        "attribute of a mapped class (data-contracts.sqlalchemy-column): an "
        "assignment to it, or a dict key passed to values() or update(), "
        "writes the column; an augmented assignment reads and writes it; "
        "any other access reads it.",
    )
)
SA_COLUMN_KEY_RULE = register_rule(
    EvidenceRule(
        "data-contracts.sqlalchemy-column-key",
        "pattern_matched",
        "A keyword or dict key of a query or statement on one mapped class "
        "the index resolves (filter_by reads; values() of insert or update "
        "and Query.update's dict write) or a keyword of a mapped class's "
        "default constructor (writes), read against the class's column "
        "attributes, as scip-python does not resolve keyword names.",
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

FSA_MODEL_QUERY_RULE = register_rule(
    EvidenceRule(
        "data-contracts.flask-sqlalchemy-model-query",
        "pattern_matched",
        "The code reads Flask-SQLAlchemy's Model.query on (an attribute of) "
        "the one SQLAlchemy mapped class of the repository with that name.",
    )
)
SQLMODEL_DEFAULT_RULE = register_rule(
    EvidenceRule(
        "data-contracts.sqlmodel-default-table",
        "pattern_matched",
        "The class sets table=True, inherits from sqlmodel.SQLModel (its "
        "bases followed through a SCIP index, or by name in the same file) "
        "and sets no __tablename__: SQLModel names its table by the class "
        "name lowercased.",
    )
)
SA_NAME_RULES = {
    "query": SA_QUERY_RULE,
    "statement": SA_STATEMENT_RULE,
    "model_query": FSA_MODEL_QUERY_RULE,
}
SA_SCIP_RULES = {
    how: register_rule(
        EvidenceRule(f"data-contracts.sqlalchemy-scip-{name}", "resolved", text)
    )
    for how, name, text in (
        (
            "query",
            "query",
            "The code calls .query() with an argument whose name a SCIP index "
            "resolves to a SQLAlchemy mapped class; the receiver is not "
            "checked to be a Session.",
        ),
        (
            "statement",
            "statement",
            "The code calls SQLAlchemy's select (READS) or insert, update or "
            "delete (WRITES) on a name a SCIP index resolves to a mapped class.",
        ),
        (
            "model_query",
            "model-query",
            "The code reads .query on a name a SCIP index resolves to a "
            "mapped class (Flask-SQLAlchemy's Model.query).",
        ),
        (
            "instance",
            "instance",
            "A scip-python index resolves the called method to SQLAlchemy's "
            "Session (or AsyncSession, scoped_session) add, delete or merge "
            "(WRITES) or refresh (READS), and documents the type it infers "
            "for the variable passed as a mapped class.",
        ),
        (
            "get",
            "get",
            "The code calls .get() or .select_from() with a first argument "
            "whose name a SCIP index resolves to a mapped class "
            "(Session.get(Model, key)); the receiver is not checked.",
        ),
    )
}

KYSELY_SCIP_RULE = register_rule(
    EvidenceRule(
        "data-contracts.kysely-scip",
        "resolved",
        "A scip-typescript index resolves the called method to Kysely's own "
        "selectFrom, a join, insertInto, replaceInto, updateTable, "
        "deleteFrom or mergeInto (selectFrom and joins READ, the others "
        "WRITE), and its first argument is this table as a string literal "
        "(alias dropped) or an array of them; names the statement declares "
        "with with()/withRecursive() are left out.",
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
            "script-sql",
            "The code passes SQL naming this table (string literals, template "
            "strings and + concatenation, the parts the code does not fix "
            "dropped, or pg's {text} config) to a query, execute, raw, "
            "prepare, exec, run, all, get, each or pg-promise query method, "
            "in a JavaScript or TypeScript file importing a Node SQL driver "
            "(pg, mysql, mysql2, mariadb, sqlite3, better-sqlite3, mssql, "
            "oracledb, ...) or knex; the receiver's type is not checked.",
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
            "go-sql-types",
            "The Go type checker (go/types) resolves the call to a "
            "database/sql, sqlx, pgx, GORM (Raw, Exec) or bun query method, "
            "or to an interface method returning one of their types (sqlc's "
            "DBTX), and computes the SQL argument's text (constants, locals "
            "assigned once, + joins with text the code does not fix); the "
            "SQL names this table.",
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

SQL_RULES["typeorm-sql"] = register_rule(
    EvidenceRule(
        "data-contracts.typeorm-sql",
        "resolved",
        "A scip-typescript index resolves the call to TypeORM's query "
        "method (Repository, EntityManager, DataSource, Connection or "
        "QueryRunner), and the SQL it is passed (string literals, template "
        "strings and + concatenation, the parts the code does not fix "
        "dropped) names this table.",
    )
)

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
        "without a TableName method, and this table is GORM's default name "
        "as GORM's own schema.NamingStrategy{}.TableName computes it, built "
        "at the gorm.io/gorm and jinzhu/inflection versions the model's "
        "module selects (UserModel is user_models). A repository that "
        "configures a NamingStrategy gets no default names; a module whose "
        "GORM cannot be built gets none either, with a gorm_naming_unread "
        "warning.",
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
GORM_TABLE_TYPES_RULE = register_rule(
    EvidenceRule(
        "data-contracts.gorm-table-name-types",
        "resolved",
        "The Go type checker (go/types) finds a TableName() method of the "
        "type, with no parameters and one return of a constant string, in a "
        "module whose go.mod requires GORM: GORM's Tabler interface names "
        "the type's table.",
    )
)
PRISMA_MODEL_RULE = register_rule(
    EvidenceRule(
        "data-contracts.prisma-model",
        "resolved",
        "Prisma's own schema engine (the prisma-schema-wasm build the "
        "project's Prisma pins) reads a model for this table from the "
        "schema: its @@map name, else the model name.",
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
# A scalar field of a model's payload type in the Prisma Client prisma
# generate writes (``Prisma/$UserPayload#typeLiteral10:scalars.`...`:
# typeLiteral11:email.``), or of a groupBy row
# (``Prisma/UserGroupByOutputType#typeLiteral10:email.``).
# The prisma-client-js generator declares these in its namespace Prisma
# (``index.d.ts`/Prisma/``); the prisma-client generator (Prisma 7's
# default) in a file per model (``models/`User.ts`/``).
_PRISMA_CLIENT_SCOPE = r"(?:/Prisma/|/models/`\w+\.ts`/)"
_PRISMA_PAYLOAD = re.compile(
    r"^scip-typescript .*" + _PRISMA_CLIENT_SCOPE + r"(?:\$(\w+)Payload#[^ ]*"
    r":scalars\.[^ ]*|(\w+)GroupByOutputType#[^ ]*):(\w+)\.$"
)
PRISMA_COLUMN_RULE = register_rule(
    EvidenceRule(
        "data-contracts.prisma-column",
        "resolved",
        "Prisma's own schema engine (data-contracts.prisma-model) lists this "
        "scalar field of the model, with its @map name, in the schema's "
        "DMMF; used for a table whose migrations give no columns. The DMMF "
        "leaves out Unsupported and @ignore fields, so the table's columns "
        "are not marked all known.",
    )
)
PRISMA_COLUMN_KEY_RULE = register_rule(
    EvidenceRule(
        "data-contracts.prisma-column-key",
        "pattern_matched",
        "A key of a Prisma Client call's arguments (data-contracts."
        "prisma-client) names a field of the model in Prisma's DMMF: in a "
        "where, cursor, having, select, orderBy, distinct, by or aggregate "
        "it reads the field's column, in a data, create or update it "
        "writes it; a relation key reads (or, written, sets) the foreign "
        "key fields the model holds and its own keys name the related "
        "model's fields. Prisma Client types these objects by the model's "
        "fields, which scip-typescript records as local keys, not "
        "references.",
    )
)
PRISMA_COLUMN_READ_RULE = register_rule(
    EvidenceRule(
        "data-contracts.prisma-scip-column-read",
        "resolved",
        "A scip-typescript index resolves a property access on a row to a "
        "scalar of the model's payload type in the Prisma Client the "
        "project's prisma generate wrote ($<Model>Payload scalars, or a "
        "groupBy row's <Model>GroupByOutputType), and "
        "Prisma's DMMF maps that field to this column of the model's "
        "table: the one such model of the reader's namespace, else of the "
        "repository.",
    )
)
TYPEORM_COLUMN_RULE = register_rule(
    EvidenceRule(
        "data-contracts.typeorm-column",
        "resolved",
        "A property of a TypeORM entity (or of a class the entity extends) "
        "is decorated with one of TypeORM's column decorators (@Column, "
        "@PrimaryGeneratedColumn, @CreateDateColumn, ...), or is a "
        "ManyToOne or a OneToOne with @JoinColumn, which holds a foreign "
        "key. Its column is the name its options give, else the name "
        "TypeORM's DefaultNamingStrategy (columnName, or joinColumnName of "
        "the related entity's primary column) at the release the project "
        "pins gives it; declared for a table whose migrations give no "
        "columns. A project that sets a namingStrategy gets only the names "
        "its options give.",
    )
)
TYPEORM_COLUMN_ACCESS_RULE = register_rule(
    EvidenceRule(
        "data-contracts.typeorm-scip-column-access",
        "resolved",
        "A scip-typescript index resolves a property access to a column "
        "property of a TypeORM entity (data-contracts.typeorm-column): an "
        "assignment to it writes the column; an object key naming it reads "
        "it under a find option (where, order, select...), in an object a "
        "TypeORM find option type is declared for (an annotation, as, "
        "satisfies, or the parameter type of the project function called), "
        "and in the criteria or arguments of a TypeORM repository or entity "
        "manager method that reads; any other object key writes it (values "
        "of save, create, update, or of a project's own method); any other "
        "access reads it.",
    )
)
# scip-typescript symbols of TypeORM's query builder methods and types.
_TYPEORM_BUILDER_METHOD_SYMBOL = re.compile(
    r"scip-typescript npm typeorm \S+ query-builder/.*/\w*QueryBuilder#\w+\(\)\."
)
_TYPEORM_TYPE_SYMBOL = re.compile(r"scip-typescript npm typeorm \S+ .*/(\w+)#")
TYPEORM_COLUMN_KEY_RULE = register_rule(
    EvidenceRule(
        "data-contracts.typeorm-column-key",
        "pattern_matched",
        "A key of a TypeORM repository call's arguments (find options "
        "where, select and order, the criteria of findBy, delete, update "
        "and the like, the values of save, insert, upsert and update) names "
        "a column property of the entity the index resolves the call to "
        "(data-contracts.typeorm-scip): the call reads it, or writes it in "
        "values. Read by key, as scip-typescript does not resolve the keys "
        "of TypeORM's mapped option types to the entity's properties.",
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
        "table is the name TypeORM's own DefaultNamingStrategy.tableName "
        "gives the class, run at the typeorm release the project pins "
        "(installed, locked, else the newest release of the declared "
        "range). A custom naming strategy is not read. An entity whose "
        "release cannot be found or installed gets no table, with a "
        "typeorm_naming_unread warning.",
    )
)
TYPEORM_CHILD_RULE = register_rule(
    EvidenceRule(
        "data-contracts.typeorm-child-entity",
        "pattern_matched",
        "The class is decorated with TypeORM's @ChildEntity, a single-table "
        "inheritance child, and extends by name a class mapped to this "
        "table: the one such entity of the child's namespace, else of the "
        "repository.",
    )
)
TYPEORM_REPOSITORY_RULE = register_rule(
    EvidenceRule(
        "data-contracts.typeorm-repository",
        "pattern_matched",
        "In a file importing typeorm or @nestjs/typeorm, the code calls "
        "getRepository or @InjectRepository with a TypeORM entity class "
        "name: the one such entity of the caller's namespace, else of the "
        "repository. A repository called with a TypeORM read method (find, "
        "count and the like) only reads, one called with a write method "
        "(save, update, delete and the like) only writes, and any other "
        "repository reads and writes.",
    )
)
TYPEORM_WRAPPER_RULE = register_rule(
    EvidenceRule(
        "data-contracts.typeorm-repository-wrapper",
        "pattern_matched",
        "The code calls a project's own getRepository with more arguments "
        "than TypeORM's, or in a file that does not import typeorm, and "
        "exactly one argument is an imported or declared class name that is "
        "a TypeORM entity: the one such entity of the caller's namespace, "
        "else of the repository. Reads and writes follow the chained method "
        "as for data-contracts.typeorm-repository.",
    )
)

TYPEORM_SCIP_RULE = register_rule(
    EvidenceRule(
        "data-contracts.typeorm-scip",
        "resolved",
        "A scip-typescript index resolves the called method to a read "
        "(find, count and the like: READS) or write (save, update, delete "
        "and the like: WRITES) method of TypeORM's Repository, "
        "TreeRepository, MongoRepository or EntityManager, and the entity "
        "class to a TypeORM entity: the receiver's declared type "
        "Repository<E> (a property, parameter or variable, or this in a "
        "class extending it), a TypeORM getRepository(E) call or a variable "
        "holding one, or an entity manager method's first argument; every "
        "name resolved through the index.",
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

DOCTRINE_TABLE_RULE = register_rule(
    EvidenceRule(
        "data-contracts.doctrine-table",
        "pattern_matched",
        "A Doctrine entity (#[ORM\\Entity], an @ORM\\Entity docblock "
        "annotation or an <entity> of a .orm.xml mapping) names this table "
        "with a literal Table name; a single-table subclass takes its root "
        "entity's table. Also a literal JoinTable name.",
    )
)
DOCTRINE_DEFAULT_RULE = register_rule(
    EvidenceRule(
        "data-contracts.doctrine-default-table",
        "name_matched",
        "A Doctrine entity without a Table name: its short class name under "
        "the configured naming strategy (DefaultNamingStrategy keeps it, "
        "UnderscoreNamingStrategy snake-cases it, number aware from ORM 3 or "
        "with underscore_number_aware). Left out when the strategy is custom "
        "or a metadata listener renames tables.",
    )
)
DOCTRINE_CALL_RULE = register_rule(
    EvidenceRule(
        "data-contracts.doctrine-call",
        "pattern_matched",
        "The code reads an entity's table through its repository (the "
        "repository's own createQueryBuilder and finders, finders on a "
        "property or parameter typed with the repository class, "
        "getRepository(X::class) finders, EntityManager find(X::class)) or a "
        "query builder from(X::class), or writes it with update/delete"
        "(X::class) or persist/remove of a variable typed with the entity "
        "class (a parameter, property or new X()). Receivers other than "
        "these types are not type-checked.",
    )
)
DOCTRINE_DQL_RULE = register_rule(
    EvidenceRule(
        "data-contracts.doctrine-dql",
        "pattern_matched",
        "DQL text the code fixes, passed to createQuery or setDQL, names this "
        "entity (fully qualified, through X::class, or as a Bundle:Entity "
        "alias of a bundle class of the repository) in a FROM or JOIN "
        "(READS) or as the target of UPDATE or DELETE (WRITES); association "
        "joins are not followed. The receiver is not type-checked.",
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

MONGODB_COLLECTION_RULE = register_rule(
    EvidenceRule(
        "data-contracts.mongodb-collection",
        "pattern_matched",
        "In a file importing mongodb, mongoose or mongoskin (as a value or in "
        "a typeof import() type), or below a package.json that depends on "
        "one, the code opens "
        "a MongoDB driver collection by its literal name, "
        "<db>.collection('name'), on a database handle: x.db, x.db(...), "
        "x.connection, a || or ?? of handles, or a name the file binds (by "
        "declarations and assignments) only to handles or "
        "types as mongodb's Db. A read method (find, aggregate, ...) on the "
        "collection, directly or through a variable holding it in the same "
        "function, or through this.f anywhere in the file when the "
        "collection is assigned to this.f, "
        "gives READS, a write method (insertOne, updateMany, ensureIndex, "
        "drop, ...) gives WRITES, and any other use gives both. The name is "
        "the collection itself; no pluralizing applies.",
    )
)

KNEX_QUERY_RULE = register_rule(
    EvidenceRule(
        "data-contracts.knex-query",
        "pattern_matched",
        "In a file importing knex or below a package.json depending on it, "
        "a Knex query names this table by a literal: K('t'), K.table('t'), "
        "K.batchInsert('t', ...), or table/from/into/join (and the other "
        "*join methods) in a builder chain rooted at a Knex instance, outside "
        "K.schema. An instance is a name knex or a .knex property, a name "
        "bound to a call of the imported knex factory, a parameter typed "
        "Knex or Knex.Transaction, or the parameter of a callback passed to "
        "an instance's transaction(). Alias (t as x) and schema are dropped. "
        "insert, update, del/delete, truncate, increment, decrement, upsert "
        "or merge on the chain, or on a variable holding the query in the "
        "same function, gives WRITES; otherwise READS. Joined tables are "
        "READS.",
    )
)
KNEX_CREATE_RULE = register_rule(
    EvidenceRule(
        "data-contracts.knex-create-table",
        "pattern_matched",
        "K.schema.createTable('t', ...) or createTableIfNotExists (also after "
        "withSchema) on a Knex instance, as data-contracts.knex-query defines "
        "one, declares this table.",
    )
)

MONGOOSE_REPOSITORY_RULE = register_rule(
    EvidenceRule(
        "data-contracts.mongoose-repository-call",
        "pattern_matched",
        "A Mongoose model held in a field of a repository class: the class's "
        "constructor passes the model to super(), and a base class sets the "
        "field from that constructor parameter. The code calls a query or "
        "write method on this.field in the subclass, or calls a method the "
        "class inherits, on this or on a property whose declared type is "
        "the class, and that inherited method's body (with the methods it "
        "calls on this) makes those calls on the field.",
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
    """Rails, Laravel and Symfony test trees (test/, tests/, Tests/, spec/
    and their nested copies) and vendored third-party code (vendor/, where
    Bundler caches gems and Composer installs packages)."""
    return any(
        part in ("test", "tests", "Tests", "spec", "vendor")
        for part in path.split("/")[:-1]
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


# Engine diagnostics that stop the walk over source files.
SOURCE_LIMITS = {"file_count_limit", "total_byte_limit"}


@register_tool
class DataContractTool(AnalysisTool):
    def __init__(self, repo_path, max_files=10_000):
        super().__init__(repo_path)
        self.last_facts = None
        self.max_files = max_files

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
        self,
        repo_url,
        languages=None,
        file_patterns=None,
        skip_patterns=None,
        scip_indexes=(),
    ):
        """``scip_indexes`` are pairs of an ingested SCIP index (with the
        workspace's file paths) and its receipt digest, as the tree-sitter
        tool takes them: SQLAlchemy model names in the files they cover link
        to the class the index resolves them to."""
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
            max_files=self.max_files,
        )
        root = Path(self.repo_path).resolve()
        # JavaScript and TypeScript imports resolve through the compiler.
        result = ts_modules.with_compiler_modules(result, root)
        scan = scan_components(root, include=include, exclude=exclude)
        packages = {c: c for c in scan.components if c.kind in PACKAGE_KINDS}
        cache = {}

        config = SpringConfig(root)
        datasources = {}

        # Compose build contexts, and the databases each one's services name
        # in connection URLs.
        contexts, compose_keys = set(), {}
        contexts.update(
            c.context
            for c in scan.components
            if c.kind == "compose_service" and c.context is not None
        )
        selected = {c.manifest for c in scan.components if c.kind == "compose_service"}
        projects = deploy_facts.collect(root, skipped=SKIPPED_DIRECTORIES).compose
        for context, key in compose_databases(
            p for p in projects if p.file in selected
        ):
            compose_keys.setdefault(context, set()).add(key)

        def compose_database(path):
            """The one database that the services of the deepest Compose build
            context enclosing the file name in connection URLs, or None."""
            enclosing = [c for c in contexts if c == "" or path.startswith(c + "/")]
            if not enclosing:
                return None
            keys = compose_keys.get(max(enclosing, key=len), set())
            return next(iter(keys)) if len(keys) == 1 else None

        def scope(path):
            """The table namespace of a file: its module's datasource URL when
            the Spring configuration fixes one persistent database, else the
            one Compose database its build context's services connect to, else
            its nearest package."""
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
            database = compose_database(path)
            if database is not None:
                return database
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
        doctrine_mapped = []  # entities of .orm.xml mapping files
        for path in _sql_files(root, include, exclude, ".orm.xml"):
            content = (root / path).read_bytes()
            found = doctrine_xml(path, content)
            if found:
                digests[path] = sha256(content).hexdigest()
                doctrine_mapped.extend(found)
        prisma_texts = {}
        foreign_keys, foreign_keys_unparsed = [], []
        for path in _sql_files(root, include, exclude, ".prisma"):
            content = (root / path).read_bytes()
            digests[path] = sha256(content).hexdigest()
            try:
                prisma_texts[path] = content.decode("utf-8")
            except UnicodeDecodeError:
                unreadable.append(path)
        # Prisma's own schema engine, the build the project's Prisma pins.
        prisma_read = read_schemas(root, prisma_texts) if prisma_texts else SchemaRead()
        prisma.extend(prisma_read.models)
        drizzle_folders, drizzle_keys, drizzle_unread = {}, [], []
        foreign_keys_superseded = {"prisma": 0}

        def prisma_migrations(path):
            """Whether a SQL file is a migration of a Prisma schema the engine
            read: <dir>/migrations/<name>/migration.sql beside the
            migration_lock.toml Prisma Migrate writes, with a read schema
            in <dir>. Prisma's schema is the current state, so its keys
            stand for these migrations, including the ones later dropped."""
            parts = path.split("/")
            if len(parts) < 3 or parts[-3] != "migrations":
                return False
            base = "/".join(parts[:-3])
            if not (root / base / "migrations" / "migration_lock.toml").is_file():
                return False
            prefix = f"{base}/" if base else ""
            return any(f.startswith(prefix) for f in prisma_read.engines)

        migration_sets, table_sets = defaultdict(list), defaultdict(list)
        foreign_keys_dropped, foreign_key_drops_unmatched = 0, []
        rollbacks, changes_of, ddl_read = [], {}, []
        # (migration set, table or None for every table) of an ALTER TABLE
        # no dialect parses: those tables' columns are not known.
        columns_unknown, columns_unparsed = set(), []

        def read_keys(path, content, folder=None):
            """Queue a SQL file's foreign keys and drops for the replay of
            its migration set (``folder`` when given)."""
            keys, unparsed = extract_foreign_keys(path, content)
            changes, changes_unparsed = changes_of[path]
            migration_sets[folder or migration_set(path)].append((path, keys, changes))
            foreign_keys_unparsed.extend(
                (path, line) for line in sorted({*unparsed, *changes_unparsed})
            )

        for path in _sql_files(root, include, exclude):
            content = (root / path).read_bytes()
            digests[path] = sha256(content).hexdigest()
            found = extract_ddl(path, content)
            if found is None:
                unreadable.append(path)
            elif is_rollback_migration(path):
                # A rollback (down) migration is not part of the schema the
                # migrations build: neither its tables nor its keys are read.
                rollbacks.append(path)
                ddl_read.extend(found)
            else:
                ddl_read.extend(found)
                changes_of[path] = extract_schema_changes(path, content)
                column_changes, alters_unparsed = extract_column_changes(path, content)
                folder = posixpath.dirname(path)
                drizzle = (root / folder / "meta" / "_journal.json").is_file()
                key = folder if drizzle else migration_set(path)
                table_sets[key].append(
                    (path, found, changes_of[path][0] + column_changes)
                )
                for line, altered in alters_unparsed:
                    columns_unknown.add((key, altered))
                    columns_unparsed.append((path, line))
                if drizzle:
                    # drizzle-kit's own record of the schema after the last
                    # migration gives the keys (below), not each migration.
                    drizzle_folders.setdefault(folder, []).append((path, content))
                    continue
                if prisma_migrations(path):
                    foreign_keys_superseded["prisma"] += 1
                    continue
                read_keys(path, content)
        for folder, migrations in sorted(drizzle_folders.items()):
            snapshot, keys = drizzle_foreign_keys(root, folder)
            if snapshot is not None:
                drizzle_keys.extend(keys)
                continue
            # Without the last snapshot the state is unknown: each migration
            # is replayed as any migration set is, and the warning says so.
            drizzle_unread.append(keys)
            for path, content in migrations:
                read_keys(path, content, folder)
        tables_dropped = tables_renamed = 0
        for key, files in sorted(table_sets.items()):
            tables, dropped, renamed = replay_tables(files)
            if (key, None) in columns_unknown:
                tables = [replace(t, columns_read=False) for t in tables]
            tables = [
                replace(t, columns_read=False)
                if (key, t.name) in columns_unknown
                else t
                for t in tables
            ]
            ddl.extend(tables)
            tables_dropped += dropped
            tables_renamed += renamed
        for _, files in sorted(migration_sets.items()):
            keys, dropped, unmatched = replay_foreign_keys(files)
            foreign_keys.extend(keys)
            foreign_keys_dropped += dropped
            foreign_key_drops_unmatched.extend(unmatched)
        sources, changed, partial_scripts = [], [], []
        for coverage in result.coverage:
            if (
                coverage.status == "partial"
                and coverage.language in ("javascript", "typescript")
                and not _is_script_test(coverage.file)
            ):
                # Read only where a SCIP index confirms each call (Kysely).
                partial_scripts.append(coverage)
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
            # A Vue component is read as its script blocks.
            sources.append(
                (
                    coverage.file,
                    coverage.language,
                    *script_source(coverage.file, content),
                )
            )
        if changed:
            raise ValueError(
                "Files changed during analysis: " + ", ".join(sorted(changed))
            )
        parsers = {}
        trees, python_trees, script_trees, go_trees, ruby_trees = [], [], [], [], []
        php_trees = []
        for file, language, grammar, content in sources:
            if language in ("ruby", "php") and _is_ruby_test(file):
                continue
            if language == "go" and file.endswith("_test.go"):
                continue
            if language in ("javascript", "typescript") and _is_script_test(file):
                continue
            suffix = ".tsx" if grammar == ".tsx" else ""
            if (language, suffix) not in parsers:
                parsers[language, suffix] = make_parser(language, suffix)
            tree = parse_source(parsers[language, suffix], language, suffix, content)
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
        column_nodes = {}
        # {table qualified name: its columns}, for tables whose columns
        # are all known.
        table_columns = {}

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

        def link(kind, source, target, rule, extra, labels=("DbTable", "Contract")):
            relationships.append(
                RelationshipData(
                    kind,
                    source[0],
                    {"qualified_name": source[1], "repo_url": repo_url},
                    list(labels),
                    {"qualified_name": target, "repo_url": repo_url},
                    {**extra, **_evidence(rule)},
                )
            )

        repository_name = f"{repo_url}::repository"
        for path in sorted(
            {d.file for d in ddl_read}
            | {d.file for d in ddl}
            | {file for d in ddl if d.columns_read for _, file, _ in d.columns}
            | {m.file for m in prisma}
            | {m.file for m in mapper_statements}
            | {c.file for c in doctrine_mapped}
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
            if not item.columns_read:
                continue
            owner = table(item.file, item.name)
            tables[(scope(item.file), item.name)].properties["columns_read"] = True
            table_columns.setdefault(owner, tuple(c for c, _, _ in item.columns))
            for column, file, line in item.columns:
                name = f"{owner}::column::{column}"
                column_nodes.setdefault(
                    name,
                    NodeData(
                        ["DbColumn", "Contract"],
                        {
                            "qualified_name": name,
                            "repo_url": repo_url,
                            "name": f"{item.name}.{column}",
                            "display_name": f"{item.name}.{column}",
                            "table": item.name,
                            "column": column,
                            "protocol": "sql",
                            "scope": scope(item.file),
                        },
                    ),
                )
                relationships.append(
                    RelationshipData(
                        "HAS_COLUMN",
                        ["DbTable", "Contract"],
                        {"qualified_name": owner, "repo_url": repo_url},
                        ["DbColumn", "Contract"],
                        {"qualified_name": name, "repo_url": repo_url},
                        _evidence(TABLE_COLUMN_RULE),
                    )
                )
                relationships.append(
                    RelationshipData(
                        "DECLARES",
                        ["File"],
                        {
                            "qualified_name": f"{repo_url}::{file}::file",
                            "repo_url": repo_url,
                        },
                        ["DbColumn", "Contract"],
                        {"qualified_name": name, "repo_url": repo_url},
                        {"line": line, **_evidence(DDL_COLUMN_RULE)},
                    )
                )

        def foreign_key(file, line, owner, referenced, columns, to, on_delete, rule):
            link(
                "REFERENCES_TABLE",
                (["DbTable", "Contract"], table(file, table_key(owner))),
                table(file, table_key(referenced)),
                rule,
                {
                    "file_path": file,
                    "line": line,
                    "columns": list(columns),
                    "referenced_columns": list(to),
                    **({"on_delete": on_delete} if on_delete else {}),
                },
            )

        for keys, rule in (
            (foreign_keys, SQL_FOREIGN_KEY_RULE),
            (drizzle_keys, DRIZZLE_FOREIGN_KEY_RULE),
        ):
            for key in sorted(set(keys), key=lambda k: (k.file, k.line, k.table)):
                foreign_key(
                    key.file,
                    key.line,
                    key.table,
                    key.references,
                    key.columns,
                    key.referenced_columns,
                    key.on_delete,
                    rule,
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
        references = ScipReferences(result, scip_indexes, root)
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
        django_by_model = {}
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
            django_by_model[model_id] = target
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

        # A manager call's model is the class a scip-python index resolves
        # its written name to; a file no index covers is not read.
        django_trees = {file: (content, tree) for file, content, tree in python_trees}
        django_by_label = {}
        for model_id, target in django_by_model.items():
            model = by_id[model_id]
            label = model.app_label or django_app(model.file, labels)[1]
            django_by_label.setdefault((label, model.name), set()).add(target)

        def column_for(target, name, owner_file, rule, counts, field, line):
            """The DbColumn of table ``target`` an ORM field names ``name``:
            the table's column where migrations give its columns (None, and
            counted, when they have no such column), else one declared by
            the field (``line`` of ``owner_file``; None for an implicit
            column)."""
            known = table_columns.get(target)
            if known is not None:
                column = (
                    name
                    if name in known
                    else next((c for c in known if c.lower() == name.lower()), None)
                )
                if column is None:
                    counts["not_a_table_column"] += 1
                    return None
            else:
                column = name
            column_target = f"{target}::column::{column}"
            if known is None and column_target not in column_nodes:
                table_label = target.rsplit("::", 1)[-1]
                column_nodes[column_target] = NodeData(
                    ["DbColumn", "Contract"],
                    {
                        "qualified_name": column_target,
                        "repo_url": repo_url,
                        "name": f"{table_label}.{column}",
                        "display_name": f"{table_label}.{column}",
                        "table": table_label,
                        "column": column,
                        "protocol": "sql",
                        "scope": scope(owner_file),
                    },
                )
                link(
                    "HAS_COLUMN",
                    (["DbTable", "Contract"], target),
                    column_target,
                    rule,
                    {"field": field},
                    labels=("DbColumn", "Contract"),
                )
                if line is not None:
                    link(
                        "DECLARES",
                        (["File"], f"{repo_url}::{owner_file}::file"),
                        column_target,
                        rule,
                        {"line": line, "field": field},
                        labels=("DbColumn", "Contract"),
                    )
                counts["columns_declared"] += 1
            counts["columns"] += 1
            return column_target

        def link_field_accesses(field_at, field_name, trees, rule, counts):
            """READS_COLUMN/WRITES_COLUMN edges of the attribute accesses the
            index resolves to an ORM field (``field_at``: the field's
            definition site -> {table: DbColumn}). An assignment writes, an
            augmented assignment reads and writes, a key of a dict passed to
            ``values()`` or ``update()`` writes; any other access reads.
            Returns the sites linked."""
            sites = set()
            if not field_at:
                return sites
            owners_by_file = defaultdict(list)
            for d in result.definitions:
                owners_by_file[d.file].append(d)
            for file, line, column, site in references.references_to(field_at):
                holders = field_at[site]
                if len(holders) != 1:
                    counts["accesses_unattributed"] += 1
                    for name in holders.values():
                        if name in column_nodes:
                            column_nodes[name].properties["reads_unattributed"] = True
                    continue
                entry = trees.get(file)
                kinds = ("READS_COLUMN",)
                if entry is not None:
                    node = entry[1].root_node.descendant_for_point_range(
                        (line - 1, column), (line - 1, column)
                    )
                    access = node.parent if node is not None else None
                    holder = access.parent if access is not None else None
                    if (
                        access is not None
                        and access.type == "keyword_argument"
                        and access.child_by_field_name("name") == node
                    ):
                        # A keyword the index resolves to the field (a
                        # synthesized __init__): read as a call key.
                        counts["keyword_sites"] += 1
                        continue
                    if (
                        access is not None
                        and access.type == "attribute"
                        and holder is not None
                        and holder.child_by_field_name("left") == access
                    ):
                        if holder.type == "assignment":
                            kinds = ("WRITES_COLUMN",)
                        elif holder.type == "augmented_assignment":
                            # ``x.f += 1`` reads the value it writes.
                            kinds = ("READS_COLUMN", "WRITES_COLUMN")
                    elif (
                        access is not None
                        and access.type == "attribute"
                        and holder is not None
                        and holder.type == "pair"
                        and holder.child_by_field_name("key") == access
                    ):
                        # ``update(User).values({User.name: x})``.
                        call = holder.parent
                        while call is not None and call.type in (
                            "dictionary",
                            "list",
                            "argument_list",
                        ):
                            call = call.parent
                        function = (
                            call.child_by_field_name("function")
                            if call is not None and call.type == "call"
                            else None
                        )
                        if (
                            function is not None
                            and function.type == "attribute"
                            and function.child_by_field_name("attribute").text
                            in (b"values", b"update")
                        ):
                            kinds = ("WRITES_COLUMN",)
                sites.add((file, line, column))
                tool, receipt = references.tool(file)
                for kind in kinds:
                    counts["writes" if kind == "WRITES_COLUMN" else "reads"] += 1
                    link(
                        kind,
                        code(file, _owner_at(owners_by_file[file], file, line, column)),
                        next(iter(holders.values())),
                        rule,
                        {
                            "line": line,
                            "column": column,
                            "field": field_name[site],
                            "indexer": tool,
                            "receipt": receipt,
                        },
                        labels=("DbColumn", "Contract"),
                    )
            return sites

        # Django model fields: their columns by Django's own naming, the
        # accesses the index resolves to them, and the lookups and field
        # names of manager calls and model constructors.
        django_columns_counts = Counter()
        django_models, django_field_at = {}, defaultdict(dict)
        django_field_name = {}
        models_by_name = defaultdict(list)
        for model in models:
            models_by_name[model.name].append(model)

        def django_model_named(name, file):
            found = models_by_name.get(name, [])
            own = [m for m in found if scope(m.file) == scope(file)]
            found = own or found
            return found[0] if len(found) == 1 else None

        def django_fields(model, seen=()):
            """The fields of a model and of the abstract models it extends,
            the model's own last."""
            found = []
            for base in model.bases:
                parent = django_model_named(base, model.file)
                if (
                    parent is not None
                    and parent.abstract
                    and parent.model_id not in seen
                ):
                    found += django_fields(parent, seen + (model.model_id,))
            return found + [(model, f) for f in model.fields]

        django_planned, django_requests = [], set()
        for model_id, target in sorted(django_by_model.items(), key=str):
            model = by_id[model_id]
            if model.proxy:
                continue
            fields = {}
            for owner, field in django_fields(model):
                fields[field.name] = (owner, field)
            concrete_parent = any(
                (p := django_model_named(b, model.file)) is not None and not p.abstract
                for b in model.bases
            )
            entry = django_models.setdefault(
                target, {"fields": {}, "pk": None, "reverse": {}}
            )
            primary = next((n for n, (_, f) in fields.items() if f.primary), None)
            entry["pk"] = primary or ("id" if not concrete_parent else None)
            for name, (owner, field) in fields.items():
                related = None
                if field.target is not None:
                    other = (
                        model
                        if field.target == "self"
                        else django_model_named(field.target, owner.file)
                    )
                    if other is not None:
                        related = django_by_model.get(other.model_id)
                entry["fields"][name] = (None, related)
                if field.kind in DJANGO_NO_COLUMN:
                    continue
                request = (field.kind, field.name, field.db_column)
                django_requests.add(request)
                django_planned.append((target, owner, field, request))
            if primary is None and not concrete_parent:
                django_requests.add(("AutoField", "id", None))
                django_planned.append((target, model, None, ("AutoField", "id", None)))
        django_names, django_unread = django_column_names(root, django_requests)
        if not django_planned:
            django_unread = None
        if django_unread:
            django_columns_counts["naming_unread"] = len(django_planned)
        for target, owner, field, request in django_planned:
            name = django_names.get(request)
            if name is None:
                continue
            column_target = column_for(
                target,
                name,
                owner.file,
                DJANGO_COLUMN_RULE,
                django_columns_counts,
                request[1],
                field.line if field is not None else None,
            )
            if column_target is None:
                continue
            entry = django_models[target]
            entry["fields"][request[1]] = (
                column_target,
                entry["fields"].get(request[1], (None, None))[1],
            )
            if field is not None:
                site = (owner.file, field.line, field.column)
                django_field_at[site][target] = column_target
                django_field_name[site] = field.name
        # A reverse relation's lookup name: related_query_name, else
        # related_name, else the model's name in lower case.
        django_related_query = {}
        for target, owner, field, request in django_planned:
            if field is not None and field.related_query:
                django_related_query[(target, field.name)] = field.related_query
        for target, entry in django_models.items():
            for name, (column, related) in entry["fields"].items():
                if column is not None and related in django_models:
                    model_name = django_related_query.get((target, name)) or next(
                        (
                            by_id[m].name.lower()
                            for m, t in django_by_model.items()
                            if t == target and not by_id[m].proxy
                        ),
                        None,
                    )
                    if model_name and "%" not in model_name and model_name != "+":
                        django_models[related]["reverse"].setdefault(
                            model_name, (target, name)
                        )
        # Attribute accesses the index resolves to a field.
        django_sites = link_field_accesses(
            django_field_at,
            django_field_name,
            django_trees,
            DJANGO_COLUMN_ACCESS_RULE,
            django_columns_counts,
        )

        def django_keys(file, keyed, method):
            owners = [d for d in result.definitions if d.file == file]
            for key, name, sets, line, column in keyed.uses:
                if (file, line, column) in django_sites:
                    continue
                django_columns_counts["call_keys"] += 1
                link(
                    "WRITES_COLUMN" if sets else "READS_COLUMN",
                    code(file, _owner_at(owners, file, line, column)),
                    django_models[key]["fields"][name][0],
                    DJANGO_COLUMN_KEY_RULE,
                    {"line": line, "column": column, "method": method, "field": name},
                    labels=("DbColumn", "Contract"),
                )

        django_counts = {
            "linked_scip": 0,
            "historical": 0,
            "not_a_model": 0,
            "scip_no_reference": 0,
            "no_scip_index": 0,
        }
        django_unindexed = set()
        for access in accesses:
            declared = references.declarations(
                access.file, access.line, access.column, access.model
            )
            if declared is None:
                if references.covers(access.file):
                    django_counts["scip_no_reference"] += 1
                else:
                    django_counts["no_scip_index"] += 1
                    django_unindexed.add(access.file)
                continue
            targets = sorted(
                {django_by_model[d.id] for d in declared if d.id in django_by_model},
                key=str,
            )
            historical = None
            if not targets:
                historical = django_historical_model(
                    references, django_trees, access.file, access.line, access.column
                )
                targets = sorted(django_by_label.get(historical, ()), key=str)
            if len(targets) != 1:
                django_counts["not_a_model"] += 1
                continue
            django_counts["historical" if historical else "linked_scip"] += 1
            tool, receipt = references.tool(access.file)
            source = code(access.file, access.caller_id)
            properties = {
                "model": access.model,
                "line": access.line,
                "column": access.column,
                "indexer": tool,
                "receipt": receipt,
            }
            entry = django_trees.get(access.file)
            if targets[0] in django_models and entry is not None:
                node = entry[1].root_node.descendant_for_point_range(
                    (access.line - 1, access.column), (access.line - 1, access.column)
                )
                while node is not None and not (
                    node.type == "attribute"
                    and node.child_by_field_name("attribute") is not None
                    and node.child_by_field_name("attribute").text == b"objects"
                ):
                    node = node.parent
                if node is not None:
                    chain = django_chain(node)
                    keyed = django_columns(chain, entry[0], targets[0], django_models)
                    django_keys(
                        access.file, keyed, chain[-1][0] if chain else "objects"
                    )
                    properties["columns_resolved"] = keyed.complete and not keyed.rows
                    django_columns_counts["calls_resolved"] += properties[
                        "columns_resolved"
                    ]
            for kind, used in (("READS", access.reads), ("WRITES", access.writes)):
                if used:
                    link(
                        kind,
                        source,
                        targets[0],
                        DJANGO_HISTORICAL_RULE if historical else DJANGO_ACCESS_RULE,
                        properties,
                    )
        # Model(field=...) constructors the index resolves to a model.
        if django_models:
            model_names = {
                by_id[m].name for m, t in django_by_model.items() if t in django_models
            }
            for file, content, tree in python_trees:
                if not references.covers(file):
                    continue
                for call in _descendants(tree.root_node, "call"):
                    function = call.child_by_field_name("function")
                    if function is None:
                        continue
                    last = function
                    if function.type == "attribute":
                        last = function.child_by_field_name("attribute")
                    if last is None or last.type != "identifier":
                        continue
                    name = last.text.decode("utf-8", "replace")
                    if name not in model_names:
                        continue
                    declared = references.declarations(
                        file, last.start_point.row + 1, last.start_point.column, name
                    )
                    found = {
                        django_by_model[d.id]
                        for d in declared or ()
                        if d.id in django_by_model
                    }
                    if len(found) != 1 or next(iter(found)) not in django_models:
                        continue
                    keyed = django_columns(
                        (), content, next(iter(found)), django_models, call
                    )
                    django_columns_counts["constructors"] += 1
                    django_keys(file, keyed, name)
        # SQLAlchemy mapped classes, Table() declarations and their uses.
        # Where a SCIP index covers a file, a written model name links to the
        # class the index resolves it to; elsewhere to the one mapped class
        # of the repository with that name.
        sa_mappings, sa_tables, sa_accesses, sa_classes = [], [], [], []
        sa_trees = {file: (content, tree) for file, content, tree in python_trees}
        for file, content, tree in python_trees:
            found = extract_sqlalchemy(tree, content, file, result.definitions)
            sa_mappings.extend(found[0])
            sa_tables.extend(found[1])
            sa_accesses.extend(found[2])
            sa_classes.extend(found[3])
        sa_mappings.extend(
            sqlmodel_default_mappings(sa_classes, references, sa_mappings)
        )
        sa_targets, sa_by_model = {}, {}
        for mapping in sa_mappings:
            target = table(mapping.file, mapping.table)
            sa_targets.setdefault(mapping.name, []).append(target)
            sa_by_model[mapping.model_id] = target
            link(
                "MAPS_TO",
                symbol(mapping.model_id),
                target,
                SA_TABLENAME_RULE if mapping.explicit else SQLMODEL_DEFAULT_RULE,
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
        # SQLAlchemy columns: the column attributes of each mapped class,
        # with those of mixins and of single-table bases it inherits.
        sa_columns_counts = Counter()
        sa_class_by_id = {c.model_id: c for c in sa_classes}
        sa_class_by_name = defaultdict(list)
        for c in sa_classes:
            sa_class_by_name[(c.file, c.name)].append(c)

        def sa_bases(c):
            found = []
            for name, line, column, _ in c.bases:
                declared = references.declarations(
                    c.file, line, column, name.rsplit(".", 1)[-1]
                )
                found += (
                    sa_class_by_name.get((c.file, name), [])
                    if declared is None
                    else [
                        sa_class_by_id[d.id] for d in declared if d.id in sa_class_by_id
                    ]
                )
            return found

        def sa_mapped_base(c, seen=()):
            """The table of the nearest mapped class ``c`` inherits."""
            for base in sa_bases(c):
                if base.model_id in seen:
                    continue
                if base.model_id in sa_by_model:
                    return sa_by_model[base.model_id]
                found = sa_mapped_base(base, seen + (c.model_id,))
                if found is not None:
                    return found
            return None

        def sa_fields(c, target, seen=()):
            """(class, field) of ``c`` and of the bases mapped to no other
            table (mixins, single-table bases), the class's own last."""
            found = []
            for base in sa_bases(c):
                if base.model_id in seen:
                    continue
                other = sa_by_model.get(base.model_id)
                if other is not None and other != target:
                    continue  # joined inheritance: the base's table
                found += sa_fields(base, target, seen + (c.model_id,))
            return found + [(c, f) for f in c.fields]

        # A subclass without __tablename__ or a primary key of its own is a
        # single-table subclass: its queries and columns are its base's.
        for c in sa_classes:
            if c.model_id in sa_by_model or c.tablename or c.table:
                continue
            if any(f.primary for f in c.fields):
                continue
            target = sa_mapped_base(c)
            if target is not None:
                sa_by_model[c.model_id] = target
        sa_models = defaultdict(dict)  # table -> {attribute: DbColumn}
        sa_field_at, sa_field_name = defaultdict(dict), {}
        for model_id, target in sorted(sa_by_model.items(), key=str):
            c = sa_class_by_id.get(model_id)
            if c is None:
                continue
            for owner, field in sa_fields(c, target):
                if field.kind == "annotated" and not c.table:
                    continue
                column_target = column_for(
                    target,
                    field.column_name or field.name,
                    owner.file,
                    SA_COLUMN_RULE,
                    sa_columns_counts,
                    field.name,
                    field.line,
                )
                if column_target is None:
                    continue
                sa_models[target][field.name] = column_target
                site = (owner.file, field.line, field.column)
                sa_field_at[site][target] = column_target
                sa_field_name[site] = field.name
        # Each class's attributes, with those of the joined-inheritance bases
        # it inherits (which keep their own table's columns).
        sa_attrs = {}

        def sa_class_attrs(model_id, seen=()):
            if model_id in sa_attrs:
                return sa_attrs[model_id]
            c = sa_class_by_id.get(model_id)
            target = sa_by_model.get(model_id)
            attrs = {}
            if c is not None and target is not None:
                for base in sa_bases(c):
                    if base.model_id in seen or base.model_id == model_id:
                        continue
                    other = sa_by_model.get(base.model_id)
                    if other is not None and other != target:
                        attrs.update(sa_class_attrs(base.model_id, seen + (model_id,)))
                attrs.update(sa_models.get(target, {}))
            sa_attrs[model_id] = attrs
            return attrs

        sa_sites = link_field_accesses(
            sa_field_at,
            sa_field_name,
            sa_trees,
            SA_COLUMN_ACCESS_RULE,
            sa_columns_counts,
        )

        def sa_keys(file, keys, model_id, method):
            owners = [d for d in result.definitions if d.file == file]
            for name, sets, line, column in keys:
                column_target = sa_class_attrs(model_id).get(name)
                if column_target is None or (file, line, column) in sa_sites:
                    if column_target is None:
                        sa_columns_counts["keys_unknown"] += 1
                    continue
                sa_columns_counts["call_keys"] += 1
                link(
                    "WRITES_COLUMN" if sets else "READS_COLUMN",
                    code(file, _owner_at(owners, file, line, column)),
                    column_target,
                    SA_COLUMN_KEY_RULE,
                    {"line": line, "column": column, "method": method, "field": name},
                    labels=("DbColumn", "Contract"),
                )

        sa_counts = {
            "linked": 0,
            "linked_scip": 0,
            "not_a_mapped_class": 0,
            "scip_not_a_mapped_class": 0,
            "instance_no_scip_index": 0,
            "instance_method_unresolved": 0,
            "instance_not_session": 0,
            "instance_type_unknown": 0,
        }
        for access in sa_accesses:
            if access.how == "instance":
                called = references.symbols(
                    access.file, access.method_line, access.method_column
                )
                if called is None:
                    sa_counts["instance_no_scip_index"] += 1
                    continue
                if all(c.startswith("local ") for c in called):
                    # The indexer resolved the method to nothing outside the
                    # file: the session's package was not installed for it.
                    sa_counts["instance_method_unresolved"] += 1
                    continue
                matches = [SESSION_METHOD_SYMBOL.fullmatch(c) for c in called]
                if not any(
                    m is not None and m.group(1) == access.method for m in matches
                ):
                    sa_counts["instance_not_session"] += 1
                    continue
                declared = references.value_classes(
                    access.file, access.model_line, access.model_column
                )
                if declared is None:
                    # A parameter or annotated variable: its declared type.
                    declared = annotated_classes(
                        references,
                        sa_trees,
                        access.file,
                        access.model_line,
                        access.model_column,
                    )
                if declared is None:
                    sa_counts["instance_type_unknown"] += 1
                    continue
                targets = sorted(
                    {sa_by_model[d.id] for d in declared if d.id in sa_by_model},
                    key=str,
                )
                if len(targets) != 1:
                    sa_counts["scip_not_a_mapped_class"] += 1
                    continue
                sa_counts["linked_scip"] += 1
                tool, receipt = references.tool(access.file)
                link(
                    "WRITES" if access.writes else "READS",
                    code(access.file, access.caller_id),
                    targets[0],
                    SA_SCIP_RULES["instance"],
                    {
                        "model": access.model,
                        "method": access.method,
                        "line": access.model_line,
                        "column": access.model_column,
                        "indexer": tool,
                        "receipt": receipt,
                    },
                )
                continue
            declared = references.declarations(
                access.file, access.model_line, access.model_column, access.model
            )
            if declared is not None:
                targets = sorted(
                    {sa_by_model[d.id] for d in declared if d.id in sa_by_model},
                    key=str,
                )
                if len(targets) != 1:
                    sa_counts["scip_not_a_mapped_class"] += 1
                    continue
                sa_counts["linked_scip"] += 1
                model_ids = {d.id for d in declared if d.id in sa_by_model}
                if len(model_ids) == 1:
                    sa_keys(access.file, access.keys, model_ids.pop(), access.how)
                tool, receipt = references.tool(access.file)
                link(
                    "WRITES" if access.writes else "READS",
                    code(access.file, access.caller_id),
                    targets[0],
                    SA_SCIP_RULES[access.how],
                    {
                        "model": access.model,
                        "line": access.model_line,
                        "column": access.model_column,
                        "indexer": tool,
                        "receipt": receipt,
                    },
                )
                continue
            targets = sa_targets.get(access.model, [])
            if access.how == "get" or len(targets) != 1:
                sa_counts["not_a_mapped_class"] += 1
                continue
            sa_counts["linked"] += 1
            link(
                "WRITES" if access.writes else "READS",
                code(access.file, access.caller_id),
                targets[0],
                SA_NAME_RULES[access.how],
                {
                    "model": access.model,
                    "line": access.model_line,
                    "column": access.model_column,
                },
            )
        # Mapped(attribute=...) default constructors the index resolves.
        if sa_models:
            sa_names = {
                c.name for c in sa_classes if sa_by_model.get(c.model_id) in sa_models
            }

            def sa_custom_init(c, seen=()):
                return c.init or any(
                    sa_custom_init(b, seen + (c.model_id,))
                    for b in sa_bases(c)
                    if b.model_id not in seen
                )

            for file, content, tree in python_trees:
                if not references.covers(file):
                    continue
                for call in _descendants(tree.root_node, "call"):
                    function = call.child_by_field_name("function")
                    last = function
                    if function is not None and function.type == "attribute":
                        last = function.child_by_field_name("attribute")
                    if last is None or last.type != "identifier":
                        continue
                    name = last.text.decode("utf-8", "replace")
                    if name not in sa_names:
                        continue
                    declared = references.declarations(
                        file, last.start_point.row + 1, last.start_point.column, name
                    )
                    found = {
                        d.id
                        for d in declared or ()
                        if sa_by_model.get(d.id) in sa_models
                    }
                    if len(found) != 1:
                        continue
                    model_id = next(iter(found))
                    if sa_custom_init(sa_class_by_id[model_id]):
                        sa_columns_counts["constructor_custom_init"] += 1
                        continue
                    arguments = call.child_by_field_name("arguments")
                    keys = []
                    for child in arguments.named_children if arguments else ():
                        if child.type == "keyword_argument":
                            key = child.child_by_field_name("name")
                            keys.append(
                                (
                                    key.text.decode("utf-8", "replace"),
                                    True,
                                    key.start_point.row + 1,
                                    key.start_point.column,
                                )
                            )
                    sa_columns_counts["constructors"] += 1
                    sa_keys(file, keys, model_id, name)
        # SQL and JPQL text that Java code runs.
        entity_tables = {}
        for mapping in mappings:
            if mapping.member is None and mapping.entity_name:
                entity_tables.setdefault(mapping.entity_name, set()).add(
                    mapped.get(mapping.entity)
                )
        sql_counts = {
            "statements": 0,
            "not_fixed": 0,
            "unknown_entity": 0,
            # Statements by reader: sqlglot, or the token reader for SQL with
            # text the code does not fix and JPQL (tokens) or SQL sqlglot
            # rejects in every dialect (tokens_not_parsed).
            "sqlglot": 0,
            "tokens": 0,
            "tokens_not_parsed": 0,
        }
        # Go: the type checker's facts. The tree-sitter rules read SQL calls
        # only in files it did not check and at calls it could not type, and
        # TableName literals in files importing a SQL package everywhere.
        facts = gofacts.collect(
            root,
            [file for file, _, _ in go_trees if file.endswith(".go")],
            [
                c.manifest.rpartition("/")[0]
                for c in scan.components
                if c.kind == "go_module"
            ],
        )
        typed = {(c.file, c.line, c.column) for c in facts.calls}
        go_names = [
            GoTableName(
                item.file,
                item.type_name,
                table_key(item.table.rpartition(".")[2]),
                item.line,
                item.column,
            )
            for item in facts.table_names
            if facts.requires_module(item.file, GORM_MODULES)
        ]
        named = {(n.file, n.line) for n in go_names}
        go_uses = go_sql_from_facts(facts.calls, result.definitions)
        for file, content, tree in go_trees:
            found = extract_go_sql(tree, content, file, result.definitions)
            go_names.extend(n for n in found[0] if (n.file, n.line) not in named)
            if file not in facts.checked or file in facts.partial:
                go_uses.extend(
                    u for u in found[1] if (u.file, u.line, u.column) not in typed
                )
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
            + [
                item
                for file, content, tree in script_trees
                for item in extract_script_sql(
                    tree, content, file, result.definitions, references
                )
            ]
        )
        column_counts = Counter()

        def statement_column_links(file, statement, source, rule, place):
            """READS_COLUMN and WRITES_COLUMN edges of a statement whose
            tables' columns are known, resolved by sqlglot's optimizer;
            returns the tables every column reference of which resolved."""
            schema = {}
            for name in (*statement.reads, *statement.writes):
                owner = table(file, name)
                if owner in table_columns:
                    schema[name] = table_columns[owner]
            if not schema:
                return frozenset()
            found = statement_columns(statement, schema)
            if found is None:
                return frozenset()
            uses, complete = found
            column_counts["statements"] += 1
            column_counts["tables_resolved"] += len(complete)
            column_counts["tables_unresolved"] += len(set(schema) - complete)
            for use in uses:
                column_counts["uses"] += 1
                link(
                    "WRITES_COLUMN" if use.writes else "READS_COLUMN",
                    source,
                    f"{table(file, use.table)}::column::{use.column}",
                    rule,
                    dict(place),
                    labels=("DbColumn", "Contract"),
                )
            return complete

        for item in uses:
            sql_counts["statements"] += 1
            if item.statement is None:
                sql_counts["not_fixed"] += 1
                continue
            sql_counts[item.statement.reader] += 1
            source = code(item.file, item.owner_id)
            place = {"line": item.line, "column": item.column}
            complete = (
                ()
                if item.jpql
                else statement_column_links(
                    item.file, item.statement, source, SQL_RULES[item.how], place
                )
            )
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
                        if name in complete:
                            extra["columns_resolved"] = True
                    link(kind, source, target, SQL_RULES[item.how], {**extra, **place})
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
                GORM_TABLE_TYPES_RULE
                if (item.file, item.line) in named
                else GORM_TABLE_RULE,
                {"line": item.line, "column": item.column},
            )
        # A configured NamingStrategy changes every default name.
        gorm_defaults, pending = 0, {}
        for model in sorted(
            [] if gorm_naming else gorm_models,
            key=lambda m: (m.file, m.line, m.column),
        ):
            key = (model.file.rpartition("/")[0], model.type_name)
            if key in gorm_targets or key in pending:
                continue
            declared = owner(model.file, model.type_name)
            if declared is not None:
                pending[key] = (declared, model.type_name)
        # GORM's own NamingStrategy at the version each module selects.
        by_module = {}
        for key, (declared, type_name) in pending.items():
            module = facts.module_of(declared.file)
            by_module.setdefault(module, []).append((key, declared, type_name))
        gorm_naming_unread = {}
        for module, models in sorted(by_module.items(), key=lambda m: m[0] or ""):
            if module is None:
                gorm_naming_unread["."] = (
                    f"{len(models)} models outside every Go module"
                )
                continue
            names, reason = gorm_table_names(
                root, module, [type_name for _, _, type_name in models]
            )
            if reason is not None:
                gorm_naming_unread[module or "."] = reason
                continue
            for key, declared, type_name in models:
                gorm_defaults += 1
                target = table(declared.file, table_key(names[type_name]))
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
        prisma_targets, prisma_by_accessor = {}, defaultdict(set)
        for model in sorted(prisma, key=lambda m: (m.file, m.line)):
            target = table(model.file, table_key(model.table))
            prisma_targets.setdefault(prisma_accessor(model.model), set()).add(
                (scope(model.file), target)
            )
            prisma_by_accessor[prisma_accessor(model.model)].add(
                (scope(model.file), model)
            )
            link(
                "DECLARES",
                (["File"], f"{repo_url}::{model.file}::file"),
                target,
                PRISMA_MODEL_RULE,
                {"line": model.line, "model": model.model},
            )
            for referenced, columns, to, on_delete in model.foreign_keys:
                foreign_key(
                    model.file,
                    model.line,
                    model.table,
                    referenced,
                    columns,
                    to,
                    on_delete,
                    PRISMA_FOREIGN_KEY_RULE,
                )
        # The column of each Prisma model field: the table's own column
        # from its migrations, else the field's column in the DMMF.
        prisma_models = {(scope(m.file), m.model): m for m in prisma}
        prisma_columns_at = {}
        prisma_counts = Counter()
        for model in sorted(prisma, key=lambda m: (m.file, m.line)):
            owner = table(model.file, table_key(model.table))
            known = table_columns.get(owner)
            by_field = {}
            for field, column in model.columns:
                if known is not None:
                    name = (
                        column
                        if column in known
                        else next(
                            (c for c in known if c.lower() == column.lower()), None
                        )
                    )
                    if name is None:
                        prisma_counts["field_not_a_table_column"] += 1
                        continue
                else:
                    name = column
                    target = f"{owner}::column::{name}"
                    if target not in column_nodes:
                        prisma_counts["columns_declared"] += 1
                        column_nodes[target] = NodeData(
                            ["DbColumn", "Contract"],
                            {
                                "qualified_name": target,
                                "repo_url": repo_url,
                                "name": f"{model.table}.{name}",
                                "display_name": f"{model.table}.{name}",
                                "table": model.table,
                                "column": name,
                                "protocol": "sql",
                                "scope": scope(model.file),
                            },
                        )
                        link(
                            "HAS_COLUMN",
                            (["DbTable", "Contract"], owner),
                            target,
                            PRISMA_COLUMN_RULE,
                            {"field": field},
                            labels=("DbColumn", "Contract"),
                        )
                        link(
                            "DECLARES",
                            (["File"], f"{repo_url}::{model.file}::file"),
                            target,
                            PRISMA_COLUMN_RULE,
                            {"line": model.line, "field": field},
                            labels=("DbColumn", "Contract"),
                        )
                by_field[field] = f"{owner}::column::{name}"
            prisma_columns_at[(scope(model.file), model.model)] = by_field
        orm_counts = {"prisma_linked": 0, "prisma_ambiguous": 0}
        # A caller whose own namespace has a schema the engine did not read
        # is not linked to another namespace's model of the same name.
        unread_scopes = {scope(path) for path in prisma_read.unread}
        if unread_scopes:
            orm_counts["prisma_schema_unread"] = 0
        for file, content, tree in script_trees:
            for use in extract_prisma_client(
                tree, content, file, result.definitions, set(prisma_targets)
            ):
                use_scope = scope(use.file)
                if use_scope in unread_scopes and all(
                    s != use_scope for s, _ in prisma_targets[use.model]
                ):
                    orm_counts["prisma_schema_unread"] += 1
                    continue
                targets = _nearest(prisma_targets[use.model], use_scope)
                if len(targets) != 1:
                    orm_counts["prisma_ambiguous"] += 1
                    continue
                orm_counts["prisma_linked"] += 1
                target = next(iter(targets))
                model = next(
                    m
                    for s_, m in prisma_by_accessor[use.model]
                    if table(m.file, table_key(m.table)) == target
                )
                model_scope = scope(model.file)
                found = prisma_columns(
                    use.call,
                    content,
                    use.method,
                    model,
                    lambda name, at=model_scope: prisma_models.get((at, name)),
                )
                resolved = found.complete
                for name, field, writes, line, column in found.uses:
                    column_target = prisma_columns_at.get((model_scope, name), {}).get(
                        field
                    )
                    if column_target is None:
                        resolved = False
                        prisma_counts["key_not_a_column"] += 1
                        continue
                    prisma_counts["keys"] += 1
                    link(
                        "WRITES_COLUMN" if writes else "READS_COLUMN",
                        code(use.file, use.caller_id),
                        column_target,
                        PRISMA_COLUMN_KEY_RULE,
                        {"field": field, "line": line, "column": column},
                        labels=("DbColumn", "Contract"),
                    )
                for kind, used in (("READS", use.reads), ("WRITES", use.writes)):
                    if used:
                        # A read is resolved when the rows it returns are
                        # only the fields it selects.
                        whole = resolved and (kind == "WRITES" or not found.rows)
                        link(
                            kind,
                            code(use.file, use.caller_id),
                            target,
                            PRISMA_CLIENT_RULE,
                            {
                                "model": use.model,
                                "line": use.line,
                                "column": use.column,
                                **({"columns_resolved": True} if whole else {}),
                            },
                        )
        # Reads of a row's field: the index resolves the property to a
        # scalar of the model's payload type in the generated client.
        if prisma_columns_at:
            owners = defaultdict(list)
            for d in result.definitions:
                owners[d.file].append(d)
            by_model = defaultdict(set)
            for (at, name), model in prisma_models.items():
                by_model[name].add((at, model))
            # Objects written for a Prisma Client input type outside a call
            # (``const where: Prisma.UserWhereInput = {...}``): the index
            # resolves the type to the client's, which names the model.
            for file, content, tree in script_trees:
                typed = prisma_typed_inputs(tree)
                # Later assignments to a typed variable, or to one of its
                # properties, through the index's definition of the name.
                bound = {
                    (file, *binding): (line, column, name)
                    for line, column, name, _, binding in typed
                    if binding is not None
                }
                inputs = [(*t[:4], None) for t in typed if t[3] is not None]
                for line, column, key, key_line, key_column, value in (
                    prisma_assignments(tree, content) if bound else ()
                ):
                    for site in references.definition_sites(file, line, column) or ():
                        if site in bound:
                            inputs.append(
                                (
                                    *bound[site],
                                    value,
                                    None
                                    if key is None
                                    else (key, key_line, key_column),
                                )
                            )
                for line, column, name, value, key in inputs:
                    natives = references.symbols(file, line, column) or ()
                    if not any(
                        re.search(_PRISMA_CLIENT_SCOPE + re.escape(name) + "#$", native)
                        for native in natives
                    ):
                        continue
                    kind = model = None
                    for suffix, what in PRISMA_INPUT_KINDS.items():
                        prefix = name[: -len(suffix)]
                        if name.endswith(suffix) and prefix in by_model:
                            if model is None or len(prefix) > len(model):
                                kind, model = what, prefix
                    candidates = _nearest(by_model.get(model, ()), scope(file))
                    if len(candidates) != 1:
                        prisma_counts["typed_inputs_no_model"] += 1
                        continue
                    model = next(iter(candidates))
                    model_scope = scope(model.file)
                    found = prisma_input_columns(
                        kind,
                        value,
                        content,
                        model,
                        lambda related, at=model_scope: prisma_models.get(
                            (at, related)
                        ),
                        key,
                    )
                    prisma_counts["typed_inputs"] += 1
                    for related, field, writes, at_line, at_column in found.uses:
                        column_target = prisma_columns_at.get(
                            (model_scope, related), {}
                        ).get(field)
                        if column_target is None:
                            prisma_counts["key_not_a_column"] += 1
                            continue
                        prisma_counts["typed_input_keys"] += 1
                        link(
                            "WRITES_COLUMN" if writes else "READS_COLUMN",
                            code(
                                file, _owner_at(owners[file], file, at_line, at_column)
                            ),
                            column_target,
                            PRISMA_COLUMN_KEY_RULE,
                            {
                                "field": field,
                                "line": at_line,
                                "column": at_column,
                                "input_type": name,
                            },
                            labels=("DbColumn", "Contract"),
                        )
            for file, line, column, native in references.references_matching(
                lambda native: _PRISMA_PAYLOAD.search(native) is not None
            ):
                match = _PRISMA_PAYLOAD.search(native)
                model_name = match.group(1) or match.group(2)
                field = match.group(3)
                candidates = _nearest(by_model.get(model_name, ()), scope(file))
                if len(candidates) != 1:
                    prisma_counts[
                        "row_reads_ambiguous" if candidates else "row_reads_no_model"
                    ] += 1
                    continue
                model = next(iter(candidates))
                column_target = prisma_columns_at.get(
                    (scope(model.file), model.model), {}
                ).get(field)
                if column_target is None:
                    prisma_counts["row_reads_not_a_column"] += 1
                    continue
                prisma_counts["row_reads"] += 1
                tool, receipt = references.tool(file)
                link(
                    "READS_COLUMN",
                    code(file, _owner_at(owners[file], file, line, column)),
                    column_target,
                    PRISMA_COLUMN_READ_RULE,
                    {
                        "field": field,
                        "line": line,
                        "column": column,
                        "indexer": tool,
                        "receipt": receipt,
                    },
                    labels=("DbColumn", "Contract"),
                )
        # TypeORM entities and the repositories of them.
        typeorm_mappings, typeorm_uses, typeorm_children = [], [], []
        for file, content, tree in script_trees:
            found = extract_typeorm(tree, content, file, result.definitions)
            typeorm_mappings.extend(found[0])
            typeorm_uses.extend(found[1])
            typeorm_children.extend(found[2])
        # An @Entity without a name: TypeORM's DefaultNamingStrategy at the
        # release the project pins.
        typeorm_names, typeorm_naming_unread = typeorm_table_names(
            root,
            sorted({(m.file, m.name) for m in typeorm_mappings if m.table is None}),
        )
        typeorm_mappings = [
            m
            if m.table is not None
            else replace(m, table=table_key(typeorm_names[m.file, m.name]))
            for m in typeorm_mappings
            if m.table is not None or (m.file, m.name) in typeorm_names
        ]
        typeorm_targets, typeorm_by_model = {}, {}
        for mapping in typeorm_mappings:
            target = table(mapping.file, mapping.table)
            typeorm_by_model[mapping.model_id] = target
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
        # A @ChildEntity is stored in the table of the entity it extends,
        # through any number of child levels.
        pending = list(typeorm_children)
        while pending:
            waiting = []
            for child in pending:
                bases = _nearest(
                    typeorm_targets.get(child.base, set()), scope(child.file)
                )
                if len(bases) != 1:
                    waiting.append(child)
                    continue
                target = next(iter(bases))
                typeorm_targets.setdefault(child.name, set()).add(
                    (scope(child.file), target)
                )
                typeorm_by_model[child.model_id] = target
                link(
                    "MAPS_TO",
                    symbol(child.model_id),
                    target,
                    TYPEORM_CHILD_RULE,
                    {"base": child.base, "line": child.line, "column": child.column},
                )
            if len(waiting) == len(pending):
                break
            pending = waiting
        typeorm_child_counts = {
            "linked": len(typeorm_children) - len(pending),
            "base_not_an_entity": len(pending),
        }
        # The columns of each entity's properties, through the classes it
        # extends; a property a base class declares is shared by the
        # entities that extend it.
        typeorm_columns = Counter()
        typeorm_classes = defaultdict(list)
        for file, content, tree in script_trees:
            for found in extract_typeorm_classes(tree, content, file):
                typeorm_classes[found.name].append(found)
        custom_naming = any(
            b"namingStrategy" in content for _, content, _ in script_trees
        )
        if custom_naming:
            typeorm_columns["naming_strategy_set"] = 1

        def typeorm_class(name, file):
            """The class ``name`` that ``file`` sees: its own, else the one
            of its namespace, else the repository's only one."""
            found = typeorm_classes.get(name, [])
            own = [c for c in found if c.file == file] or [
                c for c in found if scope(c.file) == scope(file)
            ]
            found = own or found
            return found[0] if len(found) == 1 else None

        entity_tables = [
            (m.file, m.name, typeorm_by_model[m.model_id]) for m in typeorm_mappings
        ]
        entity_tables += [
            (c.file, c.name, typeorm_by_model[c.model_id])
            for c in typeorm_children
            if c.model_id in typeorm_by_model
        ]
        # (file, line, column) of a property -> {table: column}
        typeorm_property_at = defaultdict(dict)
        # table -> {property: (column or None, related)}
        typeorm_fields = {}
        typeorm_property_name = {}
        requests, planned = set(), []
        for file, name, owner in entity_tables:
            chain, seen, at = [], set(), typeorm_class(name, file)
            while at is not None and (at.file, at.name) not in seen:
                seen.add((at.file, at.name))
                chain.append(at)
                at = typeorm_class(at.base, at.file) if at.base else None
            if not chain:
                typeorm_columns["entity_class_not_read"] += 1
                continue
            properties = {}
            for cls in reversed(chain):
                for prop in cls.properties:
                    properties[prop.name] = (cls, prop)
            fields = typeorm_fields.setdefault(owner, {})
            for cls, prop in properties.values():
                if prop.kind == "relation":
                    fields[prop.name] = (None, (cls.file, prop.target))
                    if not prop.owning:
                        continue
                    if prop.custom is not None:
                        planned.append((owner, cls, prop, prop.custom, None))
                        continue
                    referenced = prop.referenced
                    if referenced is None:
                        related = (
                            typeorm_class(prop.target, cls.file)
                            if prop.target
                            else None
                        )
                        primaries = []
                        while related is not None and not primaries:
                            primaries = [
                                p for p in related.properties if p.kind == "primary"
                            ]
                            related = (
                                typeorm_class(related.base, related.file)
                                if related.base and not primaries
                                else None
                            )
                        if len(primaries) != 1:
                            typeorm_columns["join_column_unknown"] += 1
                            continue
                        referenced = primaries[0].name
                    request = ("join", prop.name, referenced)
                else:
                    request = ("column", prop.name, prop.custom, ())
                if custom_naming and prop.custom is None:
                    typeorm_columns["named_by_project_strategy"] += 1
                    continue
                requests.add((cls.file, request))
                planned.append((owner, cls, prop, None, request))
        column_names_found, column_naming_unread = typeorm_column_names(
            root, sorted(requests, key=str)
        )
        for path, reason in sorted(column_naming_unread.items()):
            typeorm_naming_unread.setdefault(path, reason)
        for owner, cls, prop, custom, request in planned:
            name = (
                custom
                if custom is not None
                else column_names_found.get((cls.file, request))
            )
            if name is None:
                typeorm_columns["naming_unread"] += 1
                continue
            known = table_columns.get(owner)
            if known is not None:
                column = (
                    name
                    if name in known
                    else next((c for c in known if c.lower() == name.lower()), None)
                )
                if column is None:
                    typeorm_columns["not_a_table_column"] += 1
                    continue
            else:
                column = name
            target = f"{owner}::column::{column}"
            if known is None and target not in column_nodes:
                table_label = owner.rsplit("::", 1)[-1]
                column_nodes[target] = NodeData(
                    ["DbColumn", "Contract"],
                    {
                        "qualified_name": target,
                        "repo_url": repo_url,
                        "name": f"{table_label}.{column}",
                        "display_name": f"{table_label}.{column}",
                        "table": table_label,
                        "column": column,
                        "protocol": "sql",
                        "scope": scope(cls.file),
                    },
                )
                link(
                    "HAS_COLUMN",
                    (["DbTable", "Contract"], owner),
                    target,
                    TYPEORM_COLUMN_RULE,
                    {"property": prop.name},
                    labels=("DbColumn", "Contract"),
                )
                link(
                    "DECLARES",
                    (["File"], f"{repo_url}::{cls.file}::file"),
                    target,
                    TYPEORM_COLUMN_RULE,
                    {"line": prop.line, "property": prop.name},
                    labels=("DbColumn", "Contract"),
                )
                typeorm_columns["columns_declared"] += 1
            typeorm_columns["columns"] += 1
            typeorm_property_at[(cls.file, prop.line, prop.column)][owner] = target
            typeorm_property_name[(cls.file, prop.line, prop.column)] = prop.name
            typeorm_fields[owner][prop.name] = (
                target,
                typeorm_fields[owner].get(prop.name, (None, None))[1],
            )
        # A relation's related entity, as its table.
        owner_of_class = defaultdict(set)
        for file, name, owner in entity_tables:
            owner_of_class[name].add((file, owner))
        for fields in typeorm_fields.values():
            for name, (column, related) in list(fields.items()):
                if related is None:
                    continue
                at, target_name = related
                owners = owner_of_class.get(target_name, set())
                near = [o for f, o in owners if scope(f) == scope(at)] or [
                    o for _, o in owners
                ]
                fields[name] = (column, near[0] if len(set(near)) == 1 else None)
        # Accesses of a column property, through the index.
        typeorm_unattributed, typeorm_sites = set(), set()

        def typeorm_method_owner(file, name):
            """The TypeORM class whose method the index resolves ``name``
            (a called method's name node) to, else None."""
            for symbol in (
                references.symbols(
                    file, name.start_point.row + 1, name.start_point.column
                )
                or ()
            ):
                match = TYPEORM_METHOD_SYMBOL.fullmatch(symbol)
                if match is not None:
                    return match.group(1)
                if _TYPEORM_BUILDER_METHOD_SYMBOL.fullmatch(symbol):
                    return "QueryBuilder"
            return None

        def typeorm_type_kind(file, node):
            """ "read" for a type the index resolves to one of TypeORM's
            find option types, "write" for DeepPartial and
            QueryDeepPartialEntity, else None."""
            for symbol in (
                references.symbols(
                    file, node.start_point.row + 1, node.start_point.column
                )
                or ()
            ):
                match = _TYPEORM_TYPE_SYMBOL.fullmatch(symbol)
                if match is None:
                    continue
                if match.group(1).startswith("Find"):
                    return "read"
                if match.group(1) in ("DeepPartial", "QueryDeepPartialEntity"):
                    return "write"
            return None

        def typeorm_parameter_kind(file, name, index):
            """typeorm_type_kind of the declared type of parameter ``index``
            of the function the index resolves the called ``name`` to."""
            for site in (
                references.definition_sites(
                    file, name.start_point.row + 1, name.start_point.column
                )
                or ()
            ):
                entry = trees_by_file.get(site[0])
                if entry is None:
                    continue
                at = entry.root_node.descendant_for_point_range(
                    (site[1] - 1, site[2]), (site[1] - 1, site[2])
                )
                holder = at.parent if at is not None else None
                parameters = (
                    holder.child_by_field_name("parameters")
                    if holder is not None
                    else None
                )
                if parameters is None:
                    continue
                listed = [
                    p
                    for p in parameters.named_children
                    if p.type in ("required_parameter", "optional_parameter")
                ]
                if index >= len(listed):
                    continue
                annotation = listed[index].child_by_field_name("type")
                if annotation is not None and annotation.named_children:
                    annotation = annotation.named_children[0]
                if annotation is not None and annotation.type == "generic_type":
                    annotation = annotation.child_by_field_name("name")
                if annotation is not None and annotation.type == "type_identifier":
                    kind = typeorm_type_kind(site[0], annotation)
                    if kind is not None:
                        return kind
            return None

        if typeorm_property_at:
            owners_by_file = defaultdict(list)
            for d in result.definitions:
                owners_by_file[d.file].append(d)
            trees_by_file = {file: tree for file, _, tree in script_trees}
            contents_by_file = {file: content for file, content, _ in script_trees}
            for file, line, column, site in references.references_to(
                typeorm_property_at
            ):
                holders = typeorm_property_at[site]
                tree = trees_by_file.get(file)
                at = (
                    tree.root_node.descendant_for_point_range(
                        (line - 1, column), (line - 1, column)
                    )
                    if tree is not None
                    else None
                )
                while at is not None and at.type not in ("type_query", "program"):
                    at = at.parent
                if at is not None and at.type == "type_query":
                    # ``typeof row.kind`` names a type, not a value.
                    continue
                if len(holders) != 1:
                    typeorm_columns["accesses_unattributed"] += 1
                    typeorm_unattributed.update(holders.values())
                    continue
                writes = False
                tree = trees_by_file.get(file)
                keyed = (
                    typeorm_key_access(
                        tree,
                        contents_by_file[file],
                        line,
                        column,
                        lambda name, file=file: typeorm_method_owner(file, name),
                        lambda node, file=file: typeorm_type_kind(file, node),
                        lambda name, index, file=file: typeorm_parameter_kind(
                            file, name, index
                        ),
                    )
                    if tree is not None
                    else None
                )
                if keyed is not None:
                    writes = keyed == "write"
                    typeorm_columns["keys"] += 1
                elif tree is not None:
                    node = tree.root_node.descendant_for_point_range(
                        (line - 1, column), (line - 1, column)
                    )
                    access = node.parent if node is not None else None
                    holder = access.parent if access is not None else None
                    writes = (
                        access is not None
                        and access.type == "member_expression"
                        and holder is not None
                        and holder.type
                        in ("assignment_expression", "augmented_assignment_expression")
                        and holder.child_by_field_name("left") == access
                    )
                typeorm_columns["writes" if writes else "reads"] += 1
                typeorm_sites.add((file, line, column))
                tool, receipt = references.tool(file)
                link(
                    "WRITES_COLUMN" if writes else "READS_COLUMN",
                    code(file, _owner_at(owners_by_file[file], file, line, column)),
                    next(iter(holders.values())),
                    TYPEORM_COLUMN_ACCESS_RULE,
                    {
                        "line": line,
                        "property": typeorm_property_name[site],
                        "column": column,
                        "indexer": tool,
                        "receipt": receipt,
                    },
                    labels=("DbColumn", "Contract"),
                )
        for name in typeorm_unattributed:
            if name in column_nodes:
                column_nodes[name].properties["reads_unattributed"] = True
        orm_counts.update(typeorm_linked=0, typeorm_not_an_entity=0)
        # A wrapper call names no entity more often than not: count those
        # apart so the TypeORM API's own misses stay visible.
        orm_counts.update(typeorm_wrapper_not_an_entity=0)
        # In files a scip-typescript index covers, TypeORM's own repository
        # and entity manager calls are read through the index (below), and
        # the decorator-level name rule is not used.
        # Where the index records no symbol, or only a local one, at the
        # getRepository or InjectRepository name, it could not type the call
        # (its imports did not resolve, as in a package importing its own
        # unbuilt dist): the index read nothing there, and the run counts
        # it (typeorm_index_unresolved).
        orm_counts.update(typeorm_name_rule_replaced=0, typeorm_index_unresolved=0)
        for use in typeorm_uses:
            if not use.wrapper and references.covers(use.file):
                called = references.symbols(
                    use.file, use.callee_line, use.callee_column
                )
                if any(not c.startswith("local ") for c in called or ()):
                    orm_counts["typeorm_name_rule_replaced"] += 1
                    continue
                orm_counts["typeorm_index_unresolved"] += 1
            targets = _nearest(typeorm_targets.get(use.model, set()), scope(use.file))
            if len(targets) != 1:
                orm_counts[
                    "typeorm_wrapper_not_an_entity"
                    if use.wrapper
                    else "typeorm_not_an_entity"
                ] += 1
                continue
            orm_counts["typeorm_linked"] += 1
            source = code(use.file, use.caller_id)
            kinds = ("READS",) * use.reads + ("WRITES",) * use.writes
            for kind in kinds:
                link(
                    kind,
                    source,
                    next(iter(targets)),
                    TYPEORM_WRAPPER_RULE if use.wrapper else TYPEORM_REPOSITORY_RULE,
                    {"entity": use.model, "line": use.line, "column": use.column},
                )
        typeorm_scip_counts = {
            "linked": 0,
            "no_scip_index": 0,
            "not_typeorm": 0,
            "entity_unknown": 0,
            "not_an_entity": 0,
            "builders": 0,
            "relation_builders": 0,
        }

        def typeorm_keys(call, content, node, method, owner, chain, manager=False):
            """Read a repository call's (or query builder's) keys and link
            the columns they name; returns the TypeormColumns."""
            keyed = typeorm_call_columns(
                node, content, method, owner, typeorm_fields, manager, chain
            )
            owners = [d for d in result.definitions if d.file == call.file]
            for ent, name, sets, line, column in keyed.uses:
                if (call.file, line, column) in typeorm_sites:
                    continue
                typeorm_columns["call_keys"] += 1
                link(
                    "WRITES_COLUMN" if sets else "READS_COLUMN",
                    code(call.file, _owner_at(owners, call.file, line, column)),
                    typeorm_fields[ent][name][0],
                    TYPEORM_COLUMN_KEY_RULE,
                    {
                        "line": line,
                        "column": column,
                        "method": call.method
                        if method != "builder"
                        else "createQueryBuilder",
                        "property": name,
                    },
                    labels=("DbColumn", "Contract"),
                )
            return keyed

        if typeorm_mappings or typeorm_children:
            ts_trees = {file: (content, tree) for file, content, tree in script_trees}
            for file, content, tree in script_trees:
                for call in extract_typeorm_calls(
                    tree, content, file, result.definitions
                ):
                    if call.method == "createQueryBuilder":
                        status, builder = typeorm_builder(references, ts_trees, call)
                        if status != "resolved":
                            typeorm_scip_counts[status] += 1
                            continue
                        if builder.table is not None:
                            targets = [table(call.file, table_key(builder.table))]
                        else:
                            targets = sorted(
                                {
                                    typeorm_by_model[e.id]
                                    for e in builder.entities
                                    if e.id in typeorm_by_model
                                },
                                key=str,
                            )
                        if len(targets) != 1:
                            typeorm_scip_counts["not_an_entity"] += 1
                            continue
                        typeorm_scip_counts["linked"] += 1
                        typeorm_scip_counts["builders"] += 1
                        tool, receipt = references.tool(call.file)
                        properties = {
                            "method": call.method,
                            "line": call.line,
                            "column": call.column,
                            "indexer": tool,
                            "receipt": receipt,
                        }
                        if targets[0] in typeorm_fields:
                            keyed = typeorm_keys(
                                call,
                                content,
                                None,
                                "builder",
                                targets[0],
                                builder.chain,
                            )
                            # A reading builder's columns are SQL text, not read here.
                            properties["columns_resolved"] = (
                                keyed.complete and builder.writes
                            )
                        link(
                            "WRITES" if builder.writes else "READS",
                            code(call.file, call.caller_id),
                            targets[0],
                            TYPEORM_SCIP_RULE,
                            properties,
                        )
                        continue
                    status, entities = typeorm_call_entities(references, ts_trees, call)
                    if status != "resolved":
                        typeorm_scip_counts[status] += 1
                        continue
                    targets = sorted(
                        {
                            typeorm_by_model[e.id]
                            for e in entities
                            if e.id in typeorm_by_model
                        },
                        key=str,
                    )
                    if len(targets) != 1:
                        typeorm_scip_counts["not_an_entity"] += 1
                        continue
                    typeorm_scip_counts["linked"] += 1
                    tool, receipt = references.tool(call.file)
                    writes = call.method in _TYPEORM_WRITES
                    properties = {
                        "method": call.method,
                        "line": call.line,
                        "column": call.column,
                        "indexer": tool,
                        "receipt": receipt,
                    }
                    node = tree.root_node.descendant_for_point_range(
                        (call.line - 1, call.column), (call.line - 1, call.column)
                    )
                    while node is not None and node.type != "call_expression":
                        node = node.parent
                    if targets[0] in typeorm_fields and node is not None:
                        manager = any(
                            "/`EntityManager.d.ts`/EntityManager#" in symbol
                            for symbol in references.symbols(
                                call.file, call.line, call.column
                            )
                            or ()
                        )
                        keyed = typeorm_keys(
                            call, content, node, call.method, targets[0], (), manager
                        )
                        properties["columns_resolved"] = keyed.complete and (
                            writes or not keyed.rows
                        )
                        typeorm_columns["call_keys_resolved"] += properties[
                            "columns_resolved"
                        ]
                    link(
                        "WRITES" if writes else "READS",
                        code(call.file, call.caller_id),
                        targets[0],
                        TYPEORM_SCIP_RULE,
                        properties,
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
        # Workspace packages, for imports of a sibling package by name.
        workspace_packages, ts_outputs = [], {}
        for path in _sql_files(root, include, exclude, ".json"):
            name = path.rpartition("/")[2]
            if name != "package.json" and not re.fullmatch(
                r"tsconfig(?:\.[\w-]+)*\.json", name
            ):
                continue
            content = (root / path).read_bytes()
            if name == "package.json":
                found = read_package_entries(path, content)
                if found is not None:
                    workspace_packages.append(found)
                    digests[path] = sha256(content).hexdigest()
                continue
            found = read_tsconfig_out(path, content)
            if found is not None:
                digests[path] = sha256(content).hexdigest()
                ts_outputs.setdefault(path.rpartition("/")[0], []).append(found)
        import_cache = {}

        def resolve_import(file, module):
            """The repository file a script import names: a relative path, a
            tsconfig ``paths`` alias or a workspace package."""
            key = (file, module)
            if key not in import_cache:
                import_cache[key] = (
                    resolve_script_module(file, module, script_files)
                    or resolve_script_alias(file, module, script_files, ts_paths)
                    or resolve_workspace_module(
                        file, module, script_files, workspace_packages, ts_outputs
                    )
                )
            return import_cache[key]

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
            resolve_import,
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
        # {directory of a package.json: (its content, the modules it depends
        # on)}, for gates on a package's dependencies.
        node_manifests = {}
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
            node_manifests[path.rpartition("/")[0]] = (
                path,
                content,
                {
                    module
                    for section in (
                        "dependencies",
                        "devDependencies",
                        "peerDependencies",
                        "optionalDependencies",
                    )
                    if isinstance(manifest.get(section), dict)
                    for module in manifest[section]
                },
            )

        def dependents(modules):
            """Directories of package.json files depending on one of the
            modules; each such manifest becomes evidence of the analysis."""
            found = set()
            for directory, (path, content, used) in node_manifests.items():
                if used & set(modules):
                    digests[path] = sha256(content).hexdigest()
                    found.add(directory)
            return found

        def enclosing(directories, path):
            """The nearest directory of the set enclosing the file (Node
            resolves a dependency from there), or None."""
            directory = path.rpartition("/")[0]
            while True:
                if directory in directories:
                    return directory
                if not directory:
                    return None
                directory = directory.rpartition("/")[0]

        mongodb_packages = dependents(MONGODB_MODULES)
        for file, content, tree in script_trees:
            found = extract_mongoose(tree, content, file, result.definitions)
            if found is not None:
                mongoose_files[file] = found
        mongoose_tables, mongoose_unresolved, mongoose_model = resolve_mongoose(
            mongoose_files,
            mongoose_majors(mongoose_specs),
            resolve_import,
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
        repository_counts = {"linked": 0, "unresolved_model": 0}
        has_models = any(found.models for found in mongoose_files.values())
        for file, found in sorted(mongoose_files.items()):
            for use in found.uses if has_models else ():
                key = mongoose_model(use)
                if use.target[0] in ("field", "typed"):
                    if isinstance(key, tuple):
                        key = [(key, use.reads, use.writes)]
                    if not isinstance(key, list):
                        if key not in ("package", "field"):
                            repository_counts[key] = repository_counts.get(key, 0) + 1
                        continue
                    linked = False
                    for model_key, reads, writes in key:
                        target = mongoose_targets.get(model_key)
                        if target is None:
                            continue
                        linked = True
                        model = mongoose_files[model_key[0]].models[model_key[1]]
                        extra = {
                            "model": model.name,
                            "line": use.line,
                            "column": use.column,
                        }
                        if use.target[0] == "typed":
                            extra["method"] = use.target[2]
                        for kind, used in (("READS", reads), ("WRITES", writes)):
                            if used:
                                link(
                                    kind,
                                    code(use.file, use.caller_id),
                                    target,
                                    MONGOOSE_REPOSITORY_RULE,
                                    extra,
                                )
                    repository_counts["linked" if linked else "unresolved_model"] += 1
                    continue
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
        # MongoDB driver collections opened by name.
        mongodb_counts = {"linked": 0}

        for file, content, tree in script_trees:
            for use in extract_mongodb_collections(
                tree,
                content,
                file,
                result.definitions,
                project=enclosing(mongodb_packages, file) is not None,
            ):
                mongodb_counts["linked"] += 1
                target = table(use.file, table_key(use.collection))
                source = code(use.file, use.caller_id)
                for kind, used in (("READS", use.reads), ("WRITES", use.writes)):
                    if used:
                        link(
                            kind,
                            source,
                            target,
                            MONGODB_COLLECTION_RULE,
                            {
                                "collection": use.collection,
                                "line": use.line,
                                "column": use.column,
                            },
                        )
        # Knex query builder.
        knex_counts = {"queries": 0, "created": 0}
        knex_packages = dependents(KNEX_MODULES)
        knex_constants = _script_module_constants(result, script_trees)
        for file, content, tree in script_trees:
            for use in extract_knex(
                tree,
                content,
                file,
                result.definitions,
                project=enclosing(knex_packages, file) is not None,
                constants=knex_constants.get(file),
            ):
                target = table(use.file, table_key(use.table))
                extra = {"line": use.line, "column": use.column}
                if use.declares:
                    knex_counts["created"] += 1
                    link(
                        "DECLARES",
                        code(use.file, None),
                        target,
                        KNEX_CREATE_RULE,
                        extra,
                    )
                    continue
                knex_counts["queries"] += 1
                source = code(use.file, use.caller_id)
                for kind, used in (("READS", use.reads), ("WRITES", use.writes)):
                    if used:
                        link(kind, source, target, KNEX_QUERY_RULE, extra)
        # Kysely: table methods whose receiver a SCIP index resolves to
        # Kysely's own, or, in files no index covers, files importing kysely.
        kysely_counts = {"calls_scip": 0, "not_kysely": 0, "no_scip_index": 0}
        kysely_unindexed = set()
        partly_parsed = []
        for coverage in partial_scripts:
            if not references.covers(coverage.file):
                continue
            try:
                content = (root / coverage.file).read_bytes()
            except OSError:
                continue
            if sha256(content).hexdigest() != coverage.digest:
                continue
            grammar, content = script_source(coverage.file, content)
            suffix = ".tsx" if grammar == ".tsx" else ""
            if (coverage.language, suffix) not in parsers:
                parsers[coverage.language, suffix] = make_parser(
                    coverage.language, suffix
                )
            partly_parsed.append(
                (
                    coverage.file,
                    content,
                    parse_source(
                        parsers[coverage.language, suffix],
                        coverage.language,
                        suffix,
                        content,
                    ),
                )
            )
        for file, content, tree in [*script_trees, *partly_parsed]:
            calls = extract_kysely(tree, content, file, result.definitions)
            if calls and not references.covers(file):
                # The compiler decides what a call is; without its index the
                # calls are not read, and the run says so.
                if imports_kysely(content):
                    kysely_counts["no_scip_index"] += len(calls)
                    kysely_unindexed.add(file)
                continue
            for call in calls:
                found = references.symbols(file, call.line, call.column)
                if not any(
                    s.startswith(f"scip-typescript npm {KYSELY_MODULE} ")
                    and s.endswith(f"#{call.method}().")
                    for s in found
                ):
                    kysely_counts["not_kysely"] += 1
                    continue
                kysely_counts["calls_scip"] += 1
                tool, receipt = references.tool(file)
                rule = KYSELY_SCIP_RULE
                extra = {"indexer": tool, "receipt": receipt}
                reads, writes = KYSELY_METHODS[call.method]
                source = code(file, call.caller_id)
                for name in call.tables:
                    target = table(file, table_key(name))
                    position = {"line": call.line, "column": call.column, **extra}
                    for kind, used in (("READS", reads), ("WRITES", writes)):
                        if used:
                            link(kind, source, target, rule, position)
        # Doctrine ORM: entities, repositories and EntityManager calls.
        doctrine_classes, doctrine_uses, doctrine_renames = [], [], False
        php_texts = []
        for file, content, tree in php_trees:
            found = extract_php_doctrine(tree, content, file, result.definitions)
            doctrine_classes.extend(found[0])
            doctrine_uses.extend(found[1])
            doctrine_renames = doctrine_renames or found[2]
            if b"NamingStrategy" in content:
                php_texts.append(content.decode("utf-8", "replace"))
        doctrine_classes.extend(doctrine_mapped)

        def doctrine_source(c):
            if c.class_id is None and c.file.endswith(".xml"):
                return ["File"], f"{repo_url}::{c.file}::file"
            return code(c.file, c.class_id)

        configured, doctrine_majors = set(), set()
        if doctrine_classes:
            for suffix in (".yaml", ".yml"):
                for path in _sql_files(root, include, exclude, suffix):
                    if "doctrine" not in path.rpartition("/")[2]:
                        continue
                    content = (root / path).read_bytes()
                    services = re.findall(
                        rb"naming_strategy\s*:\s*['\"]?([\w.\\]+)", content
                    )
                    if services:
                        digests[path] = sha256(content).hexdigest()
                        configured.update(s.decode() for s in services)
            for path in _sql_files(root, include, exclude, "composer.lock"):
                content = (root / path).read_bytes()
                try:
                    locked = json.loads(content).get("packages") or []
                except (ValueError, AttributeError):
                    continue
                for package in locked:
                    if (
                        isinstance(package, dict)
                        and package.get("name") == "doctrine/orm"
                    ):
                        major = re.match(r"v?(\d+)", str(package.get("version", "")))
                        if major:
                            digests[path] = sha256(content).hexdigest()
                            doctrine_majors.add(int(major.group(1)))
            if not doctrine_majors:
                for path in _sql_files(root, include, exclude, "composer.json"):
                    content = (root / path).read_bytes()
                    try:
                        manifest = json.loads(content)
                    except ValueError:
                        continue
                    spec = (
                        (manifest.get("require") or {}).get("doctrine/orm")
                        if isinstance(manifest, dict)
                        else None
                    )
                    if isinstance(spec, str):
                        digests[path] = sha256(content).hexdigest()
                        numbers = re.findall(r"(?:^|[\s|^~>=<v])(\d+)", spec)
                        doctrine_majors.update(int(n) for n in numbers)
        doctrine_tables, doctrine_unresolved, doctrine_repositories = resolve_doctrine(
            doctrine_classes,
            doctrine_strategies(configured, php_texts, doctrine_majors),
            doctrine_renames,
        )
        for path, (mapped, name, how) in sorted(doctrine_tables.items()):
            link(
                "MAPS_TO",
                doctrine_source(mapped),
                table(mapped.file, name),
                DOCTRINE_TABLE_RULE if how == "explicit" else DOCTRINE_DEFAULT_RULE,
                {"entity": path, "line": mapped.line, "column": mapped.column},
            )
        if not doctrine_renames:
            for c in doctrine_classes:
                for name, line in c.join_tables:
                    link(
                        "MAPS_TO",
                        doctrine_source(c),
                        table(c.file, table_key(name)),
                        DOCTRINE_TABLE_RULE,
                        {"entity": c.path, "join_table": True, "line": line},
                    )
        declared_methods = {c.path: set(c.methods) for c in doctrine_classes}
        bundles = {}
        for c in doctrine_classes:
            short = c.path.rpartition("\\")[2]
            if short.endswith("Bundle") and c.class_id is not None:
                bundles.setdefault(short, set()).add(c.path.rpartition("\\")[0])

        def dql_entity(written):
            """The entity a DQL name means: itself, or a Bundle:Entity alias."""
            bundle, colon, name = written.partition(":")
            if not colon:
                return written
            namespaces = bundles.get(bundle, set())
            return (
                f"{next(iter(namespaces))}\\Entity\\{name}"
                if len(namespaces) == 1
                else None
            )

        doctrine_counts = {"linked": 0, "not_an_entity": 0, "unresolved_entity": 0}
        for use in doctrine_uses:
            if (
                use.repository is not None
                and use.repository not in doctrine_repositories
                and not use.repository.endswith("Repository")
            ):
                continue  # a finder-named method of another class
            if use.finder in declared_methods.get(use.repository, ()):
                continue  # the repository's own method, read where it is defined
            entity = use.entity or doctrine_repositories.get(use.repository)
            if use.how == "dql":
                entity = dql_entity(entity)
            mapped = doctrine_tables.get(entity)
            if mapped is None:
                reason = (
                    "unresolved_entity"
                    if entity in doctrine_unresolved
                    else "not_an_entity"
                )
                doctrine_counts[reason] += 1
                continue
            doctrine_counts["linked"] += 1
            source = code(use.file, use.caller_id)
            target = table(mapped[0].file, mapped[1])
            for kind, used in (("READS", use.reads), ("WRITES", use.writes)):
                if used:
                    link(
                        kind,
                        source,
                        target,
                        DOCTRINE_DQL_RULE if use.how == "dql" else DOCTRINE_CALL_RULE,
                        {"entity": entity, "line": use.line, "column": use.column},
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
            re-exports and ``export *``; "?" when the chain reaches a module
            the reader cannot see (a package, or a file it could not
            parse)."""
            if file is None:
                return {"?"}
            if (file, name) in seen:
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
                return {"?"}
            if name in module.re_exports:
                source, original = module.re_exports[name]
                return exported_table(resolve_import(file, source), original, seen)
            found = set()
            for source in module.star_exports:
                found |= exported_table(resolve_import(file, source), name, seen)
            return found - {"?"} if found - {"?"} else found

        # In files a scip-typescript index covers, a use is read through the
        # index: its method must resolve to drizzle-orm and its table name to
        # a table variable's definition. The name rule below runs only where
        # the index records no symbol, or only a local one, at the method
        # (it could not type the call: drizzle_scip index_unresolved).
        table_at = {
            (item.file, item.line, item.column): table(file, item.table)
            for file, variables in drizzle_tables.items()
            for items in variables.values()
            for item in items
            if item.table != PLACEHOLDER
        }
        # The column each property of a table's columns object maps to, by
        # where its key is written, and each table's columns by key.
        keys_at = {}
        drizzle_columns = Counter()
        parsed_scripts = {file: (content, tree) for file, content, tree in script_trees}

        def helper_name(file, callee):
            """The name a helper builder function gives its column, where
            the index resolves the call to its declaration."""
            names = set()
            for site_file, line, column in (
                references.definition_sites(file, *callee) or ()
            ):
                if site_file in parsed_scripts:
                    content, tree = parsed_scripts[site_file]
                    names.add(drizzle_helper_name(tree, content, line, column))
            return next(iter(names)) if len(names) == 1 else None

        # (file, line, column) of a column key -> {table: column}: a key
        # of a shared object spread into several tables maps one column of
        # each.
        shared = defaultdict(dict)

        def spread_columns(file, spreads):
            """(columns as (file, key, written, line, column, callee),
            complete) of the objects ``spreads`` name, through the index."""
            found, complete, pending, seen = (
                [],
                True,
                [(file, s) for s in spreads],
                set(),
            )
            while pending:
                at_file, (line, column) = pending.pop()
                sites = references.definition_sites(at_file, line, column) or ()
                read = False
                for site in sites:
                    if site in seen or site[0] not in parsed_scripts:
                        continue
                    seen.add(site)
                    content, tree = parsed_scripts[site[0]]
                    columns, whole, more = drizzle_spread_columns(
                        tree, content, site[1], site[2]
                    )
                    if not columns and not more:
                        continue
                    read = True
                    complete = complete and whole
                    found.extend((site[0], *c) for c in columns)
                    pending.extend((site[0], m) for m in more)
                complete = complete and read
            return found, complete

        for file, variables in drizzle_tables.items():
            for items in variables.values():
                for item in items:
                    if item.table == PLACEHOLDER:
                        continue
                    owner = table(file, item.table)
                    known = table_columns.get(owner)
                    spread, spread_whole = spread_columns(file, item.spreads)
                    entries = [(file, *c) for c in item.columns] + spread
                    if item.spreads:
                        drizzle_columns["spread_columns"] += len(spread)
                    by_key = {}
                    for at, key, written, line, column, callee in entries:
                        if written is None and callee is not None:
                            written = helper_name(at, callee)
                            if written is not None:
                                drizzle_columns["helper_names"] += 1
                        name = written if written is not None else key
                        if known is None:
                            drizzle_columns["table_columns_unknown"] += 1
                            continue
                        if name not in known:
                            # Named another way (a casing option, a
                            # column the migrations do not create).
                            drizzle_columns["not_a_table_column"] += 1
                            continue
                        drizzle_columns["columns"] += 1
                        target = f"{owner}::column::{name}"
                        shared[(at, line, column)][owner] = target
                        by_key[key] = target
                    keys_at[(item.file, item.line, item.column)] = (
                        by_key,
                        item.columns_complete
                        and spread_whole
                        and known is not None
                        and len(by_key) == len(entries),
                    )
        column_at = dict(shared)
        drizzle_scip = {
            "linked": 0,
            "no_scip_index": 0,
            "index_unresolved": 0,
            "not_drizzle": 0,
            "not_a_table": 0,
        }
        by_name_rule = []
        for use in drizzle_uses:
            called = references.symbols(use.file, use.line, use.column)
            if called is None:
                drizzle_scip["no_scip_index"] += 1
                by_name_rule.append(use)
                continue
            if all(c.startswith("local ") for c in called):
                drizzle_scip["index_unresolved"] += 1
                by_name_rule.append(use)
                continue
            if not any(" npm drizzle-orm " in c for c in called):
                drizzle_scip["not_drizzle"] += 1
                continue
            sites = references.definition_sites(use.file, use.ref_line, use.ref_column)
            targets = {table_at[s] for s in sites if s in table_at}
            if len(targets) != 1:
                drizzle_scip["not_a_table"] += 1
                continue
            drizzle_scip["linked"] += 1
            tool, receipt = references.tool(use.file)
            site = next((s for s in sites if s in table_at), None)
            by_key, whole = keys_at.get(site, ({}, False))
            place = {"line": use.line, "column": use.column}
            resolved = whole and use.keys_complete
            for key, line, column in use.keys:
                target = by_key.get(key)
                if target is None:
                    resolved = False
                    drizzle_columns["key_not_a_column"] += 1
                    continue
                drizzle_columns["keys"] += 1
                link(
                    "WRITES_COLUMN" if use.writes else "READS_COLUMN",
                    code(use.file, use.caller_id),
                    target,
                    DRIZZLE_COLUMN_KEY_RULE,
                    {"key": key, "line": line, "column": column},
                    labels=("DbColumn", "Contract"),
                )
            for kind, used in (("READS", use.reads), ("WRITES", use.writes)):
                if used:
                    link(
                        kind,
                        code(use.file, use.caller_id),
                        next(iter(targets)),
                        DRIZZLE_SCIP_RULE,
                        {
                            "variable": use.ref.name,
                            **place,
                            "indexer": tool,
                            "receipt": receipt,
                            **({"columns_resolved": True} if resolved else {}),
                        },
                    )
        # Reads of a column property, wherever the index resolves one: the
        # table's own property (users.email) or a selected row's.
        unattributed = set()

        def receiver_table(file, line, column):
            """The table whose variable the object of the property access at
            (line, column) of ``file`` resolves to, or None."""
            if file not in parsed_scripts:
                return None
            _, tree = parsed_scripts[file]
            node = tree.root_node.descendant_for_point_range(
                (line - 1, column), (line - 1, column)
            )
            access = node.parent if node is not None else None
            if access is None or access.type != "member_expression":
                return None
            target = access.child_by_field_name("object")
            if target is None or target.type not in ("identifier", "member_expression"):
                return None
            if target.type == "member_expression":
                target = target.child_by_field_name("property")
            sites = (
                references.definition_sites(
                    file, target.start_point.row + 1, target.start_point.column
                )
                or ()
            )
            owners_found = {table_at[s] for s in sites if s in table_at}
            return next(iter(owners_found)) if len(owners_found) == 1 else None

        if column_at:
            owners = defaultdict(list)
            for d in result.definitions:
                owners[d.file].append(d)
            for file, line, column, site in references.references_to(column_at):
                holders = column_at[site]
                if len(holders) == 1:
                    target = next(iter(holders.values()))
                else:
                    # A key shared by several tables' columns: the table is
                    # the receiver's (``agentCronJobs.updatedAt``).
                    target = holders.get(receiver_table(file, line, column))
                if target is None:
                    # A row's shared property: its table is not known, so
                    # the column's readers are not all known either.
                    drizzle_columns["reads_unattributed"] += 1
                    for unknown in holders.values():
                        unattributed.add(unknown)
                    continue
                drizzle_columns["reads"] += 1
                tool, receipt = references.tool(file)
                link(
                    "READS_COLUMN",
                    code(file, _owner_at(owners[file], file, line, column)),
                    target,
                    DRIZZLE_COLUMN_READ_RULE,
                    {
                        "line": line,
                        "column": column,
                        "indexer": tool,
                        "receipt": receipt,
                    },
                    labels=("DbColumn", "Contract"),
                )
        for name in unattributed:
            if name in column_nodes:
                column_nodes[name].properties["reads_unattributed"] = True
        drizzle_access = {"linked": 0, "not_a_table": 0}
        for use in by_name_rule:
            ref = use.ref
            if ref.how == "local":
                targets = exported_table(use.file, ref.name)
            elif ref.how == "import":
                targets = exported_table(resolve_import(use.file, ref.module), ref.name)
            else:
                targets = {"?"}
            if targets == {"?"}:
                # A package, or a module chain the reader cannot see through
                # (a file it could not parse): the one table variable of
                # that name.
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
            + [column_nodes[name] for name in sorted(column_nodes)]
        )
        status = (
            "partial"
            if unreadable
            or kysely_unindexed
            or gorm_naming_unread
            or typeorm_naming_unread
            or django_unread
            or django_unindexed
            or prisma_read.unread
            or result.status != "succeeded"
            else "succeeded"
        )
        warnings = [f"{path}: not UTF-8 text" for path in unreadable]
        warnings += [
            f"gorm_naming_unread: {module}: {reason}, so its models without a "
            "TableName method get no table"
            for module, reason in sorted(gorm_naming_unread.items())
        ]
        warnings += [
            f"typeorm_naming_unread: {path}: {reason}, so its @Entity classes "
            "without a name get no table"
            for path, reason in sorted(typeorm_naming_unread.items())
        ]
        if django_unread:
            warnings.append(
                f"django_naming_unread: {django_unread}, so Django model fields "
                "get no columns"
            )
        warnings += [
            f"foreign_key_not_parsed: {path}:{line}: no sqlglot dialect parses "
            "this statement, so its foreign keys are not read"
            for path, line in sorted(foreign_keys_unparsed)
        ]
        warnings += [
            f"drizzle_snapshot_unread: {reason}, so the foreign keys of each "
            "migration are read instead, including keys later migrations drop"
            for reason in drizzle_unread
        ]
        warnings += [
            f"prisma_schema_unread: {path}: {reason}"
            for path, reason in sorted(prisma_read.unread.items())
        ]
        if kysely_unindexed:
            warnings.append(
                f"no_scip_index: {len(kysely_unindexed)} files importing kysely "
                "have no scip-typescript index, so their Kysely queries are not "
                "read; run deepthought index-scip and pass the index"
            )
        if django_unindexed:
            warnings.append(
                f"no_scip_index: {len(django_unindexed)} files calling Django "
                "managers have no scip-python index, so their queries are not "
                "read; run deepthought index-scip and pass the index"
            )
        # A limit stops the source walk: the files after it are unread, so
        # the tables and edges reported are a lower bound.
        limits = sorted({d.code for d in result.diagnostics if d.code in SOURCE_LIMITS})
        warnings += [
            f"{code}: analysis stopped before reading every source file, "
            "so tables, models and calls in the unread files are missing"
            + (f" (max_files={self.max_files})" if code == "file_count_limit" else "")
            for code in limits
        ]
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
                    "source_limits": limits,
                    "model_tokens": 0,
                    "capabilities": ["data-contracts"],
                    "tables": len(tables),
                    "ddl_statements": len(ddl_read),
                    "ddl_tables_dropped": tables_dropped,
                    "ddl_tables_renamed": tables_renamed,
                    "ddl_columns": sum(
                        len(d.columns) for d in set(ddl) if d.columns_read
                    ),
                    "ddl_tables_columns_unknown": sum(
                        1 for d in set(ddl) if not d.columns_read
                    ),
                    "ddl_alter_unparsed": len(columns_unparsed),
                    "ddl_readers": dict(Counter(d.reader for d in set(ddl))),
                    "entities": len(entities),
                    "association_tables": sum(1 for m in mappings if m.member),
                    "repositories": len(repositories),
                    "repository_resolution": counts,
                    "django_models": len(model_tables),
                    "django_access_resolution": django_counts,
                    "sqlalchemy_mappings": len(sa_mappings),
                    "sqlalchemy_tables": len(sa_tables),
                    "sqlalchemy_access_resolution": sa_counts,
                    "sqlalchemy_columns": dict(sa_columns_counts),
                    "sql_text": sql_counts,
                    "sql_columns": dict(column_counts),
                    "mybatis_mapper_statements": mybatis_counts,
                    "entity_manager_resolution": entity_counts,
                    "go_type_facts": {
                        "modules": dict(Counter(facts.modules.values())),
                        "toolchains": dict(Counter(facts.toolchains.values())),
                        "downloads": dict(
                            Counter(
                                s.partition(":")[0] for s in facts.downloads.values()
                            )
                        ),
                        "files_checked": len(facts.checked) - len(facts.partial),
                        "files_partly_checked": len(facts.partial),
                        "files_not_checked": dict(Counter(facts.uncovered.values())),
                        "calls": len(facts.calls),
                        "table_names": len(facts.table_names),
                        **(
                            {"gopath_packages": gofacts.gopath_report(facts)}
                            if facts.gopath
                            else {}
                        ),
                    },
                    "gorm_table_names": gorm_counts,
                    "gorm_default_table_names": gorm_defaults,
                    "gorm_naming_unread": len(gorm_naming_unread),
                    "gorm_access_resolution": gorm_access,
                    "prisma_models": len(prisma),
                    "foreign_keys": {
                        "sql": len(set(foreign_keys)),
                        "prisma": sum(len(m.foreign_keys) for m in prisma),
                        "sql_not_parsed": len(foreign_keys_unparsed),
                        "sql_dropped": foreign_keys_dropped,
                        "sql_drops_naming_no_key": len(foreign_key_drops_unmatched),
                        "sql_rollback_files": len(rollbacks),
                        "drizzle_snapshot": len(set(drizzle_keys)),
                        "drizzle_snapshot_unread": len(drizzle_unread),
                        "prisma_migration_files_superseded": foreign_keys_superseded[
                            "prisma"
                        ],
                    },
                    "prisma_schema": {
                        "engines": dict(sorted(prisma_read.engines.items())),
                        "unread": len(prisma_read.unread),
                    },
                    "typeorm_entities": len(typeorm_mappings),
                    "typeorm_naming_unread": len(typeorm_naming_unread),
                    "typeorm_child_entities": typeorm_child_counts,
                    "mongodb_collections": mongodb_counts,
                    "knex": knex_counts,
                    "kysely": kysely_counts,
                    "orm_access_resolution": orm_counts,
                    "typeorm_scip": typeorm_scip_counts,
                    "typeorm_columns": dict(typeorm_columns),
                    "django_columns": dict(django_columns_counts),
                    "sequelize_models": len(sequelize_tables),
                    "sequelize_unresolved": len(sequelize_unresolved),
                    "sequelize_access_resolution": sequelize_counts,
                    "doctrine_entities": len(doctrine_tables),
                    "doctrine_unresolved": dict(
                        sorted(Counter(doctrine_unresolved.values()).items())
                    ),
                    "doctrine_access_resolution": doctrine_counts,
                    "mongoose_models": len(mongoose_tables),
                    "mongoose_unresolved": dict(
                        sorted(Counter(mongoose_unresolved.values()).items())
                    ),
                    "mongoose_access_resolution": dict(sorted(mongoose_counts.items())),
                    "mongoose_repository_resolution": dict(
                        sorted(repository_counts.items())
                    ),
                    "eloquent_models": len(eloquent_tables),
                    "eloquent_unresolved": len(eloquent_unresolved),
                    "eloquent_access_resolution": eloquent_counts,
                    "drizzle_tables": drizzle_counts,
                    "drizzle_access_resolution": drizzle_access,
                    "drizzle_scip": drizzle_scip,
                    "drizzle_columns": dict(drizzle_columns),
                    "prisma_columns": dict(prisma_counts),
                    "active_record_models": len(rails_tables),
                    "active_record_unresolved": len(rails_unresolved),
                    "active_record_access_resolution": rails_counts,
                    "scope": "tables from CREATE TABLE, Prisma schemas, "
                    "Rails schema files, Laravel and Sequelize migrations and "
                    "JPA, Django, SQLAlchemy, GORM, TypeORM, Sequelize, "
                    "Eloquent, Doctrine, ActiveRecord and Mongoose mappings, read and "
                    "written by "
                    "repositories, ORM calls and the SQL text code runs; per "
                    "datasource URL, Compose database or nearest package",
                },
            ),
        )

    async def get_symbol_definitions(self):
        return {}


def _script_module_constants(result, script_trees):
    """{file: {name: text}}: the module-level string constants each script
    file declares, and those it imports by name (``import { T } from``)
    from the one project module that declares them."""
    declared = {
        file: script_string_constants(tree.root_node, content)
        for file, content, tree in script_trees
    }
    found = {file: dict(values) for file, values in declared.items() if values}
    for (file, local), entries in name_bindings(result).names.items():
        if file not in declared or local in declared[file] or len(entries) != 1:
            continue
        name, targets = entries[0]
        if name is None or len(targets) != 1:
            continue
        text = declared.get(targets[0], {}).get(name)
        if text is not None:
            found.setdefault(file, {})[local] = text
    return found
