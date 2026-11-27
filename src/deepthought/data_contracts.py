"""Database tables that SQL migrations declare and Java code maps and accesses.

Three kinds of facts, each read from one file:

- ``CREATE TABLE`` statements in ``.sql`` files (``Ddl``).
- JPA entities: a class annotated ``@Entity`` from ``jakarta.persistence`` or
  ``javax.persistence``, mapped to the table its ``@Table(name = ...)`` names,
  or, without one, to the Spring Boot default physical name of the entity
  name (``PetType`` is ``pet_type``) (``Mapping``).
- Spring Data repositories: an interface extending a CRUD repository
  interface of ``org.springframework.data`` with an entity type argument
  (``Repository``).

A table belongs to the nearest package (see ``deepthought.components``) of
the file that declares or maps it: one service's ``owners`` table is not
another's. Names are compared case-insensitively, without schema or quotes.
SQL and JPQL that Java code runs (``@Query``, ``createQuery``, JdbcTemplate
and JdbcClient) name the tables they read and write (``SqlUse``).
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field as _field, replace
import json
import logging
import posixpath
from pathlib import Path
import re

from .service_contracts.http import PLACEHOLDER
from .service_contracts.spring import (
    _Imports,
    _Values,
    _annotations,
    _arguments,
    _package,
    _text,
)

# sqlglot warns on every statement it passes through as text; those are
# counted in the run's metrics (sql_text, ddl_readers) instead.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

_PERSISTENCE = ("jakarta.persistence", "javax.persistence")
_SPRING_DATA = "org.springframework.data"
# Repository interfaces whose inherited methods read (and write) entities.
READ_WRITE_REPOSITORIES = {
    "CrudRepository",
    "ListCrudRepository",
    "JpaRepository",
    "ReactiveCrudRepository",
    "R2dbcRepository",
}
READ_ONLY_REPOSITORIES = {
    "PagingAndSortingRepository",
    "ListPagingAndSortingRepository",
    "ReactiveSortingRepository",
}

_COMMENT_OR_STRING = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'", re.DOTALL)


@dataclass(frozen=True)
class Ddl:
    file: str
    line: int
    name: str
    schema: str | None = None
    # "sqlglot"; "postgres" for a CREATE TABLE no sqlglot dialect parses and
    # PostgreSQL's parser does; "tokens_not_parsed" when neither parses it.
    reader: str = "sqlglot"
    # The table's columns, each (name, file, line) of the statement that
    # declares it; complete only when ``columns_read`` (a parser read the
    # CREATE TABLE and every ALTER TABLE of its migration set).
    columns: tuple[tuple[str, str, int], ...] = ()
    columns_read: bool = False


@dataclass(frozen=True)
class Mapping:
    file: str
    entity_id: str
    entity: str  # qualified class name
    name: str
    explicit: bool
    line: int
    column: int
    # The member annotated @JoinTable or @CollectionTable, for a table the
    # entity's associations use rather than the entity's own table.
    member: str | None = None
    # The JPA entity name (JPQL names entities by it), for the own table.
    entity_name: str | None = None


@dataclass(frozen=True)
class Repository:
    file: str
    repository_id: str
    entity: str  # the type argument as written
    candidates: tuple[str, ...]  # qualified names it may denote
    writes: bool
    line: int
    column: int


def table_key(name):
    """The comparison form of a table name: unquoted, lower case."""
    if name[:1] in '"`[' and len(name) >= 2:
        name = name[1:-1]
    return name.lower()


def column_key(node):
    """A column's name as the database keeps it: a quoted identifier as
    written, an unquoted one folded to lower case."""
    from sqlglot import exp

    identifier = node if isinstance(node, exp.Identifier) else node.this
    if isinstance(identifier, exp.Identifier) and identifier.quoted:
        return identifier.name
    return node.name.lower()


def _blank(match):
    return re.sub(r"[^\n]", " ", match.group(0))


# Dialects sqlglot tries for schema files, in order. ClickHouse is read
# here only: its DDL (engines, codecs) is in migrations, its queries are not.
DDL_DIALECTS = ("postgres", "mysql", "sqlite", "tsql", "oracle", "clickhouse", None)


def extract_ddl(file, content: bytes):
    """The permanent tables a SQL file's CREATE TABLE statements declare.

    The file is split into statements by sqlglot's tokenizer (so semicolons
    in strings, comments and dollar-quoted bodies do not split it) and each
    statement holding CREATE and TABLE tokens is parsed by sqlglot in the
    first of DDL_DIALECTS that accepts it. A CREATE TABLE statement no
    dialect parses (syntax sqlglot does not know yet, or invalid SQL) is
    read from its tokens with ``reader="tokens_not_parsed"``. Temporary
    tables are not part of the schema. None when the file is not UTF-8 or
    no dialect tokenizes it."""
    from sqlglot.tokens import TokenType

    split = _ddl_statements(content)
    if split is None:
        return None
    text, statements = split
    found = []
    for statement in statements:
        types = {t.token_type for t in statement}
        if TokenType.CREATE not in types or TokenType.TABLE not in types:
            continue
        start, end = statement[0].start, statement[-1].end + 1
        offset = text.count("\n", 0, start)
        parsed = _sqlglot_ddl(text[start:end])
        reader = "sqlglot"
        if parsed is None:
            parsed, reader = _postgres_ddl(text[start:end]), "postgres"
        if parsed is None:
            table = _token_ddl(statement)
            if table is not None:
                name, schema, line = table
                found.append(Ddl(file, line, name, schema, "tokens_not_parsed"))
            continue
        found.extend(
            Ddl(
                file,
                line + offset,
                name,
                schema,
                reader,
                columns=tuple((column, file, at + offset) for column, at in columns),
                columns_read=complete,
            )
            for line, name, schema, columns, complete in parsed
        )
    return found


def _postgres_ddl(text):
    """(line, name, schema, columns, complete) of the permanent table a
    CREATE TABLE statement declares, read by PostgreSQL's own parser
    (libpg_query, through pglast), columns as in _sqlglot_ddl; complete is
    False for a table that takes columns from elsewhere (LIKE, INHERITS,
    PARTITION OF, OF type). [] for a temporary table, None when the parser
    does not read ``text`` as one CREATE TABLE."""
    from pglast import parse_sql
    from pglast.parser import ParseError

    try:
        parsed = parse_sql(text)
    except ParseError:
        return None
    if len(parsed) != 1 or type(parsed[0].stmt).__name__ != "CreateStmt":
        return None
    statement = parsed[0].stmt
    relation = statement.relation
    if relation.relpersistence == "t":
        return []

    def line(location):
        return text.count("\n", 0, max(location or 0, 0)) + 1

    columns, complete = (
        [],
        not (statement.inhRelations or statement.partbound or statement.ofTypename),
    )
    for element in statement.tableElts or ():
        kind = type(element).__name__
        if kind == "ColumnDef":
            columns.append((element.colname, line(element.location)))
        elif kind == "TableLikeClause":
            complete = False
    return [
        (
            line(relation.location),
            relation.relname.lower(),
            relation.schemaname.lower() if relation.schemaname else None,
            tuple(columns),
            complete,
        )
    ]


def _ddl_statements(content: bytes):
    """(text, statements) of a SQL file, each statement a list of sqlglot
    tokens split at semicolons, or None when the file is not UTF-8 or no
    dialect of DDL_DIALECTS tokenizes it."""
    import sqlglot
    from sqlglot.errors import SqlglotError
    from sqlglot.tokens import TokenType

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    tokens = None
    for dialect in DDL_DIALECTS:
        try:
            tokens = sqlglot.Dialect.get_or_raise(dialect).tokenize(text)
        except (SqlglotError, ValueError):
            continue
        break
    if tokens is None:
        return None
    statements, current = [], []
    for token in tokens:
        if token.token_type == TokenType.SEMICOLON:
            statements.append(current)
            current = []
        else:
            current.append(token)
    statements.append(current)
    return text, statements


@dataclass(frozen=True)
class ForeignKey:
    """A foreign key a schema declares: ``table`` references ``references``."""

    file: str
    line: int
    table: str
    references: str
    columns: tuple[str, ...] = ()
    referenced_columns: tuple[str, ...] = ()
    on_delete: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class ForeignKeyDrop:
    """A statement that removes foreign keys: ALTER TABLE ``table`` DROP
    CONSTRAINT / DROP FOREIGN KEY ``constraint``, or DROP TABLE ``table``
    (``constraint`` None), which takes the keys of and to the table."""

    file: str
    line: int
    table: str
    constraint: str | None = None


@dataclass(frozen=True)
class TableRename:
    """ALTER TABLE ``table`` RENAME TO ``new_name``."""

    file: str
    line: int
    table: str
    new_name: str


@dataclass(frozen=True)
class ColumnChange:
    """ALTER TABLE ``table`` ADD ``column`` (``action`` "add"), DROP COLUMN
    (``"drop"``), or RENAME COLUMN / MySQL's CHANGE (``"rename"``, to
    ``new_name``)."""

    file: str
    line: int
    table: str
    action: str
    column: str
    new_name: str | None = None


_COLUMN_WORDS = frozenset({"ADD", "DROP", "RENAME", "CHANGE", "MODIFY", "COLUMN"})


def _runs_plpgsql(statement):
    """Whether a statement's tokens begin a DO block or CREATE [OR
    REPLACE] FUNCTION or PROCEDURE."""
    words = [t.text.upper() for t in statement[:4]]
    return words[0] == "DO" or (
        words[0] == "CREATE" and bool({"FUNCTION", "PROCEDURE"} & set(words))
    )


def _altered_table(statement):
    """The table an ALTER TABLE statement's tokens name (ALTER TABLE, an
    optional IF EXISTS and ONLY, then a dotted name), or None."""
    words = [t.text for t in statement[2:]]
    while words and words[0].upper() in ("IF", "EXISTS", "ONLY"):
        words.pop(0)
    if not words:
        return None
    name = words[0]
    while len(words) >= 3 and words[1] == ".":
        name = words[2]
        words = words[2:]
    return table_key(name)


_ALTER_TABLE = re.compile(r"\bALTER\s+TABLE\b", re.IGNORECASE)
_ALTER = re.compile(r"\bALTER\b", re.IGNORECASE)


def _plpgsql_statements(text):
    """Whether a DO block's or a PL/pgSQL function's statement runs when
    the migration does, and (line, SQL, dynamic) of each statement its
    body runs, read by libpg_query (pglast); lines are 1-based in
    ``text``, and for a dynamic EXECUTE the SQL is the expression that
    builds the statement. None when ``text`` is not one such statement that
    pglast parses."""
    from pglast import parse_plpgsql, parse_sql
    from pglast.parser import ParseError

    try:
        parsed = parse_sql(text)
    except ParseError:
        return None
    if len(parsed) != 1:
        return None
    statement = parsed[0].stmt
    kind = type(statement).__name__
    if kind == "DoStmt":
        options = {o.defname: o for o in statement.args or ()}
        language = options.get("language")
        if language is not None and language.arg.sval.lower() != "plpgsql":
            return None
        body = options.get("as")
        if body is None:
            return None
        # The body opens after its dollar quote; PL/pgSQL numbers the
        # lines of the wrapped function from that line.
        quote = text.index("$", body.arg_location) if "$" in text else -1
        base = text.count("\n", 0, max(quote, 0))
        function = (
            "CREATE FUNCTION __do() RETURNS void AS $__do$"
            + body.arg.sval
            + "$__do$ LANGUAGE plpgsql"
        )
        runs = True
    elif kind == "CreateFunctionStmt":
        options = {o.defname: o for o in statement.options or ()}
        language = options.get("language")
        if language is None or language.arg.sval.lower() != "plpgsql":
            return None
        body = options.get("as")
        if body is None:
            return None
        quote = text.index("$", body.arg_location) if "$" in text else -1
        base = text.count("\n", 0, max(quote, 0))
        function, runs = text, False
    else:
        return None
    try:
        tree = parse_plpgsql(function)
    except ParseError:
        return None
    found = []
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(reversed(node))
            continue
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if key == "PLpgSQL_stmt_execsql":
                query = value["sqlstmt"]["PLpgSQL_expr"]["query"]
                found.append((base + value.get("lineno", 1) - 1, query, False))
            elif key == "PLpgSQL_stmt_dynexecute":
                query = value["query"]["PLpgSQL_expr"]["query"]
                found.append((base + value.get("lineno", 1) - 1, query, True))
            stack.append(value)
    return runs, found


def _postgres_column_changes(text):
    """(table, action, column, new name) of each column an ALTER TABLE
    statement adds, drops or renames, read by PostgreSQL's own parser
    (libpg_query, through pglast), which keeps quoted names' case and
    folds unquoted ones; None when it does not parse ``text`` as one ALTER
    TABLE or column rename."""
    from pglast import parse_sql
    from pglast.enums import AlterTableType, ObjectType
    from pglast.parser import ParseError

    try:
        parsed = parse_sql(text)
    except ParseError:
        return None
    if len(parsed) != 1:
        return None
    statement = parsed[0].stmt
    kind = type(statement).__name__
    relation = getattr(statement, "relation", None)
    if relation is None or kind not in ("AlterTableStmt", "RenameStmt"):
        return None
    table = relation.relname.lower()
    if kind == "RenameStmt":
        if statement.renameType != ObjectType.OBJECT_COLUMN:
            return []
        return [(table, "rename", statement.subname, statement.newname)]
    found = []
    for command in statement.cmds or ():
        if command.subtype == AlterTableType.AT_AddColumn:
            found.append((table, "add", command.def_.colname, None))
        elif command.subtype == AlterTableType.AT_DropColumn:
            found.append((table, "drop", command.name, None))
    return found


def extract_column_changes(file, content: bytes):
    """(changes, statements not parsed) of a SQL file's ALTER TABLE
    statements that add, drop or rename columns, read by sqlglot in the
    first of DDL_DIALECTS that parses each, else by PostgreSQL's parser.
    The statements a DO block runs are read the same way, from libpg_query's
    PL/pgSQL parse. An ALTER TABLE statement neither parses whose words may
    change columns (ADD, DROP, RENAME, CHANGE, MODIFY, COLUMN), a column
    change in a function body (it runs only if called) and a dynamic
    EXECUTE whose expression holds ALTER are not read; their (line, table)
    is returned, table from the tokens or None, since that table's columns
    are then unknown. None when the file is not UTF-8 or no dialect
    tokenizes it."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import SqlglotError
    from sqlglot.tokens import TokenType

    split = _ddl_statements(content)
    if split is None:
        return None
    text, statements = split
    found, unparsed = [], []
    for statement in statements:
        if statement and _runs_plpgsql(statement):
            start, end = statement[0].start, statement[-1].end + 1
            line = statement[0].line
            inner = _plpgsql_statements(text[start:end])
            if inner is None:
                if _ALTER_TABLE.search(text, start, end):
                    unparsed.append((line, None))
                continue
            runs, inner = inner
            for at, query, dynamic in inner:
                at += line
                if dynamic:
                    # The statement is built when it runs; one whose
                    # expression holds ALTER may change any table's columns.
                    if _ALTER.search(query):
                        unparsed.append((at, None))
                    continue
                read = extract_column_changes(file, query.encode())
                if read is None:
                    continue
                changes, missed = read
                if runs:
                    found.extend(replace(c, line=at) for c in changes)
                else:
                    # A function body runs only if something calls it.
                    unparsed.extend((at, c.table) for c in changes)
                unparsed.extend((at, table) for _, table in missed)
            continue
        if not statement or statement[0].token_type != TokenType.ALTER:
            continue
        if TokenType.TABLE not in {t.token_type for t in statement[:3]}:
            continue
        start, end = statement[0].start, statement[-1].end + 1
        line = statement[0].line
        for dialect in DDL_DIALECTS:
            try:
                trees = [
                    t
                    for t in sqlglot.parse(text[start:end], read=dialect)
                    if t is not None
                ]
            except (SqlglotError, ValueError, RecursionError):
                continue
            if trees and all(isinstance(t, exp.Alter) for t in trees):
                break
        else:
            # Syntax sqlglot does not know (ADD CONSTRAINT ... USING INDEX,
            # ALTER COLUMN ... SET STATISTICS): PostgreSQL's parser reads it.
            read = _postgres_column_changes(text[start:end])
            if read is not None:
                found.extend(
                    ColumnChange(file, line, table, action, column, new_name)
                    for table, action, column, new_name in read
                )
                continue
            words = {t.text.upper() for t in statement}
            if words & _COLUMN_WORDS:
                unparsed.append((line, _altered_table(statement)))
            continue
        for tree in trees:
            owner = tree.this
            if not isinstance(owner, exp.Table) or not owner.name:
                continue
            table = owner.name.lower()
            for action in tree.args.get("actions") or ():
                if isinstance(action, exp.ColumnDef) and action.name:
                    found.append(
                        ColumnChange(file, line, table, "add", column_key(action))
                    )
                elif (
                    isinstance(action, exp.Drop)
                    and (action.args.get("kind") or "").upper() == "COLUMN"
                ):
                    found.extend(
                        ColumnChange(file, line, table, "drop", column_key(c))
                        for c in action.args.get("tables") or ()
                        if c.name
                    )
                elif isinstance(action, exp.RenameColumn):
                    old, new = action.this, action.args.get("to")
                    if old is not None and new is not None:
                        found.append(
                            ColumnChange(
                                file,
                                line,
                                table,
                                "rename",
                                column_key(old),
                                column_key(new),
                            )
                        )
                elif isinstance(action, exp.ModifyColumn):
                    renamed = action.args.get("rename_from")
                    if renamed is not None and action.this is not None:
                        found.append(
                            ColumnChange(
                                file,
                                line,
                                table,
                                "rename",
                                column_key(renamed),
                                column_key(action.this),
                            )
                        )
    return found, unparsed


def extract_foreign_keys(file, content: bytes):
    """(foreign keys, lines not parsed) of a SQL file's CREATE TABLE and
    ALTER TABLE statements, read by sqlglot: a column's REFERENCES
    constraint and a table's FOREIGN KEY constraint, inline or added by
    ALTER TABLE ... ADD [CONSTRAINT] FOREIGN KEY. A statement with a
    REFERENCES token that no dialect parses is not read; its line is
    returned instead. Temporary tables are left out. None when the file is
    not UTF-8 or no dialect tokenizes it."""
    from sqlglot.tokens import TokenType

    split = _ddl_statements(content)
    if split is None:
        return None
    text, statements = split
    found, unparsed = [], []
    for statement in statements:
        types = {t.token_type for t in statement}
        if (
            TokenType.TABLE not in types
            or TokenType.REFERENCES not in types
            or not types & {TokenType.CREATE, TokenType.ALTER}
        ):
            continue
        start, end = statement[0].start, statement[-1].end + 1
        offset = text.count("\n", 0, start)
        keys = _sqlglot_foreign_keys(text[start:end])
        if keys is None:
            unparsed.append(statement[0].line)
            continue
        found.extend(ForeignKey(file, line + offset, *rest) for line, *rest in keys)
    return found, unparsed


def drizzle_foreign_keys(root, folder):
    """The foreign keys of the schema state drizzle-kit records for a
    migrations ``folder`` (relative to ``root``): the snapshot of the last
    entry of ``meta/_journal.json``, which drizzle-kit writes as the schema
    after every migration, so keys later migrations drop are not in it.
    Returns (snapshot path, keys), or (None, reason) when the journal or its
    last snapshot cannot be read."""
    meta = f"{folder}/meta" if folder else "meta"
    try:
        journal = json.loads((Path(root) / meta / "_journal.json").read_text("utf-8"))
        last = journal["entries"][-1]["idx"]
        snapshot = f"{meta}/{last:04d}_snapshot.json"
        text = (Path(root) / snapshot).read_text("utf-8")
        tables = json.loads(text)["tables"]
        keys = []
        for table in tables.values():
            for name, key in sorted((table.get("foreignKeys") or {}).items()):
                position = text.find(json.dumps(name))
                keys.append(
                    ForeignKey(
                        snapshot,
                        text.count("\n", 0, position) + 1 if position >= 0 else 1,
                        key["tableFrom"].lower(),
                        key["tableTo"].lower(),
                        tuple(c.lower() for c in key.get("columnsFrom") or ()),
                        tuple(c.lower() for c in key.get("columnsTo") or ()),
                        (key.get("onDelete") or "").upper() or None,
                    )
                )
    except (
        OSError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        AttributeError,
    ) as error:
        return None, f"drizzle-kit snapshot of {folder or '.'}: {error!r}"
    return snapshot, keys


def _sqlglot_foreign_keys(text):
    """(line, table, references, columns, referenced columns, on delete) of
    each foreign key a statement declares, or None when no dialect parses
    it."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import SqlglotError

    def name(table):
        if isinstance(table, exp.Schema):
            table = table.this
        if not isinstance(table, exp.Table) or not isinstance(
            table.this, exp.Identifier
        ):
            return None
        return table.name.lower()

    for dialect in DDL_DIALECTS:
        try:
            trees = [t for t in sqlglot.parse(text, read=dialect) if t is not None]
        except (SqlglotError, ValueError, RecursionError):
            continue
        if not trees or any(isinstance(t, exp.Command) for t in trees):
            continue  # A statement sqlglot only passes through as text.
        found = []
        for tree in trees:
            if isinstance(tree, exp.Create):
                if (tree.args.get("kind") or "").upper() != "TABLE":
                    continue
                properties = tree.args.get("properties")
                if properties and any(
                    isinstance(p, exp.TemporaryProperty) for p in properties.expressions
                ):
                    continue
                owner = tree.this
            elif isinstance(tree, exp.Alter):
                if (tree.args.get("kind") or "").upper() != "TABLE":
                    continue
                owner = tree.this
            else:
                continue
            table = name(owner)
            if table is None:
                continue
            for reference in tree.find_all(exp.Reference):
                target = name(reference.this)
                if target is None:
                    continue
                referenced = tuple(
                    e.name.lower()
                    for e in getattr(reference.this, "expressions", None) or ()
                    if isinstance(reference.this, exp.Schema)
                )
                holder = reference.parent
                if isinstance(holder, exp.ForeignKey):
                    columns = tuple(e.name.lower() for e in holder.expressions)
                else:
                    column = reference.find_ancestor(exp.ColumnDef)
                    columns = (column.name.lower(),) if column else ()
                on_delete = next(
                    (
                        str(o)[len("ON DELETE ") :].upper()
                        for o in reference.args.get("options") or ()
                        if str(o).upper().startswith("ON DELETE ")
                    ),
                    None,
                )
                line = (
                    reference.this.this.this.meta.get("line", 1)
                    if isinstance(reference.this, exp.Schema)
                    else reference.this.this.meta.get("line", 1)
                )
                named = reference.parent
                if isinstance(named, exp.ForeignKey):
                    named = named.parent
                constraint = (
                    named.this.name.lower()
                    if isinstance(named, (exp.Constraint, exp.ColumnConstraint))
                    and isinstance(named.this, exp.Identifier)
                    else None
                )
                found.append(
                    (line, table, target, columns, referenced, on_delete, constraint)
                )
        return found
    return None


def extract_schema_changes(file, content: bytes):
    """(changes, lines not parsed) of a SQL file's statements that drop or
    rename what earlier statements declared, read by sqlglot: ALTER TABLE
    ... DROP CONSTRAINT, ALTER TABLE ... DROP FOREIGN KEY (MySQL) and DROP
    TABLE (ForeignKeyDrop), and ALTER TABLE ... RENAME TO (TableRename). A
    statement with a DROP or RENAME token that no dialect parses is not
    read; its line is returned. None when the file is not UTF-8 or no
    dialect tokenizes it."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import SqlglotError
    from sqlglot.tokens import TokenType

    split = _ddl_statements(content)
    if split is None:
        return None
    text, statements = split
    found, unparsed = [], []
    for statement in statements:
        types = {t.token_type for t in statement}
        # sqlglot tokenizes RENAME as a command: the rest of the statement
        # (MySQL's RENAME TABLE a TO b) is one string token.
        if not statement:
            continue
        renames = statement[0].token_type == TokenType.RENAME
        if not renames and (
            TokenType.TABLE not in types
            or not types & {TokenType.DROP, TokenType.RENAME}
        ):
            continue
        start, end = statement[0].start, statement[-1].end + 1
        line = statement[0].line
        for dialect in DDL_DIALECTS:
            try:
                trees = [
                    t
                    for t in sqlglot.parse(text[start:end], read=dialect)
                    if t is not None
                ]
            except (SqlglotError, ValueError, RecursionError):
                continue
            if not trees or any(isinstance(t, exp.Command) for t in trees):
                continue
            break
        else:
            unparsed.append(line)
            continue
        for tree in trees:
            if isinstance(tree, exp.Drop):
                if (tree.args.get("kind") or "").upper() != "TABLE":
                    continue
                found.extend(
                    ForeignKeyDrop(file, line, table.name.lower())
                    for table in tree.args.get("tables") or ()
                    if isinstance(table, exp.Table)
                )
            elif isinstance(tree, exp.Alter):
                owner = tree.this
                if (tree.args.get("kind") or "").upper() != "TABLE" or not (
                    isinstance(owner, exp.Table)
                ):
                    continue
                for action in tree.args.get("actions") or ():
                    if isinstance(action, exp.AlterRename) and isinstance(
                        action.this, exp.Table
                    ):
                        found.append(
                            TableRename(
                                file,
                                line,
                                owner.name.lower(),
                                action.this.name.lower(),
                            )
                        )
                        continue
                    if not isinstance(action, exp.Drop) or (
                        (action.args.get("kind") or "").upper()
                        not in ("CONSTRAINT", "FOREIGN KEY")
                    ):
                        continue
                    found.extend(
                        ForeignKeyDrop(
                            file, line, owner.name.lower(), named.name.lower()
                        )
                        for named in action.args.get("tables") or ()
                        if isinstance(named, exp.Table)
                    )
    return found, unparsed


_ROLLBACK_MIGRATION = re.compile(r"(?:^|[._-])down\.sql$", re.I)


def is_rollback_migration(path):
    """Whether a SQL file undoes a migration: golang-migrate and sqlx write
    <version>_<name>.down.sql, Diesel <version>_<name>/down.sql. A rollback
    is not part of the schema the migrations build."""
    return bool(_ROLLBACK_MIGRATION.search(posixpath.basename(path)))


_MIGRATION_FOLDERS = {"migrations", "migration", "migrate"}


def migration_set(path):
    """The folder whose SQL files apply one after another, in name order:
    the nearest folder of the path named migrations, migration or migrate
    (Prisma, sqlx, golang-migrate, Flyway's db/migration, Rails' db/migrate),
    or the file itself when it is in none."""
    parts = path.split("/")[:-1]
    for index in range(len(parts) - 1, -1, -1):
        if parts[index].lower() in _MIGRATION_FOLDERS:
            return "/".join(parts[: index + 1])
    return path


def _natural(path):
    return [
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in re.split(r"(\d+)", path)
    ]


def _postgres_key_name(key):
    """The name PostgreSQL gives an unnamed foreign key:
    <table>_<columns>_fkey, cut to 63 bytes (ChooseConstraintName)."""
    if not key.columns:
        return None
    label = f"{key.table}_{'_'.join(key.columns)}"
    return f"{label[: 63 - len('_fkey')]}_fkey"


def replay_foreign_keys(files):
    """The foreign keys left once each file's statements run in order.

    ``files`` holds (path, keys, changes) of the SQL files of one migration
    set (``migration_set``); they apply in natural name order (V2 before
    V10) and, within a file, in line order. A drop names a key by its
    constraint name, or by the name PostgreSQL gives an unnamed key; DROP
    TABLE takes the keys of and to the table, and RENAME TO moves them. Returns (keys, dropped count,
    drops that name no key read before them: DROP CONSTRAINT also drops
    primary, unique and check constraints). Running the migrations against
    a database would run the project's code, so their statements are
    replayed from sqlglot's parse instead."""
    keys, dropped, unmatched = [], 0, []
    for _, found, drops in sorted(files, key=lambda f: _natural(f[0])):
        events = [(k.line, 1, k) for k in found] + [(d.line, 0, d) for d in drops]
        for _, kind, item in sorted(events, key=lambda e: (e[0], e[1])):
            if kind:
                keys.append(item)
                continue
            if isinstance(item, TableRename):
                keys = [
                    replace(
                        k,
                        table=item.new_name if k.table == item.table else k.table,
                        references=item.new_name
                        if k.references == item.table
                        else k.references,
                    )
                    for k in keys
                ]
                continue
            if item.constraint is None:
                left = [k for k in keys if item.table not in (k.table, k.references)]
            else:
                left = [
                    k
                    for k in keys
                    if k.table != item.table
                    or item.constraint
                    not in (k.name, None if k.name else _postgres_key_name(k))
                ]
                if len(left) == len(keys):
                    unmatched.append(item)
            dropped += len(keys) - len(left)
            keys = left
    return keys, dropped, unmatched


def replay_tables(files):
    """The tables left once a migration set's files run in order, as
    ``replay_foreign_keys`` orders them: ``files`` holds (path, tables,
    changes). DROP TABLE removes a table; RENAME TO moves it, declared
    where it is renamed; ColumnChange adds, drops and renames its columns
    (a renamed column is declared where it is renamed). A table created
    twice keeps its first declaration.
    Returns (tables, dropped count, renamed count)."""
    state, dropped, renamed = {}, 0, 0
    for _, found, changes in sorted(files, key=lambda f: _natural(f[0])):
        events = [(t.line, 1, t) for t in found] + [(c.line, 0, c) for c in changes]
        for _, kind, item in sorted(events, key=lambda e: (e[0], e[1])):
            if kind:
                state.setdefault(item.name.lower(), item)
            elif isinstance(item, ColumnChange):
                table = state.get(item.table)
                if table is None:
                    continue
                names = [c[0] for c in table.columns]
                target = item.column
                if target not in names:
                    target = next(
                        (n for n in names if n.lower() == target.lower()), target
                    )
                columns = [c for c in table.columns if c[0] != target]
                if item.action == "add":
                    columns = [
                        *table.columns,
                        *(
                            ()
                            if len(columns) < len(table.columns)
                            else ((item.column, item.file, item.line),)
                        ),
                    ]
                elif item.action == "rename":
                    columns = [
                        (item.new_name, item.file, item.line) if c[0] == target else c
                        for c in table.columns
                    ]
                state[item.table] = replace(table, columns=tuple(columns))
            elif isinstance(item, TableRename):
                if item.table in state:
                    state[item.new_name] = replace(
                        state.pop(item.table),
                        file=item.file,
                        line=item.line,
                        name=item.new_name,
                    )
                    renamed += 1
            elif item.constraint is None and state.pop(item.table, None):
                dropped += 1
    return list(state.values()), dropped, renamed


def _sqlglot_ddl(text):
    """(line, name, schema, columns, complete) of each permanent table a
    statement creates, columns as (name, line), complete False when it takes
    columns from elsewhere; None when no dialect parses it."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import SqlglotError

    for dialect in DDL_DIALECTS:
        try:
            trees = [t for t in sqlglot.parse(text, read=dialect) if t is not None]
        except (SqlglotError, ValueError, RecursionError):
            continue
        if not trees or any(isinstance(t, exp.Command) for t in trees):
            continue  # A statement sqlglot only passes through as text.
        found = []
        for tree in trees:
            for create in tree.find_all(exp.Create):
                if (create.args.get("kind") or "").upper() != "TABLE":
                    continue
                properties = create.args.get("properties")
                if properties and any(
                    isinstance(p, exp.TemporaryProperty) for p in properties.expressions
                ):
                    continue
                table = create.this
                if isinstance(table, exp.Schema):
                    table = table.this
                if not isinstance(table, exp.Table) or not isinstance(
                    table.this, exp.Identifier
                ):
                    continue
                schema = create.this
                columns = (
                    tuple(
                        (
                            column_key(column),
                            column.this.meta.get("line", 1),
                        )
                        for column in schema.expressions
                        if isinstance(column, exp.ColumnDef) and column.name
                    )
                    if isinstance(schema, exp.Schema)
                    else ()
                )
                # Columns taken from elsewhere (AS SELECT, LIKE, INHERITS,
                # PARTITION OF) are not listed.
                complete = (
                    isinstance(schema, exp.Schema)
                    and create.expression is None
                    and not any(
                        isinstance(
                            node,
                            (
                                exp.LikeProperty,
                                exp.InheritsProperty,
                                exp.PartitionedOfProperty,
                            ),
                        )
                        for node in create.walk()
                    )
                )
                found.append(
                    (
                        table.this.meta.get("line", 1),
                        table.name.lower(),
                        table.db.lower() or None,
                        columns,
                        complete,
                    )
                )
        return found
    return None


def _token_ddl(statement):
    """(name, schema, line) of the table a CREATE TABLE statement no dialect
    parses names, read from sqlglot's tokens: CREATE, modifiers, TABLE, an
    optional IF NOT EXISTS, then a dotted name. None for a temporary table
    or another statement."""
    from sqlglot.tokens import TokenType

    words = iter(statement)
    if next(words).token_type != TokenType.CREATE:
        return None
    for token in words:
        if token.token_type == TokenType.TABLE:
            break
        if token.text.upper() in ("TEMP", "TEMPORARY"):
            return None
        if token.text.upper() not in ("OR", "REPLACE", "GLOBAL", "LOCAL", "UNLOGGED"):
            return None
    else:
        return None
    rest = list(words)
    if [t.text.upper() for t in rest[:3]] == ["IF", "NOT", "EXISTS"]:
        rest = rest[3:]
    parts = []
    for token in rest:
        if parts and len(parts) % 2 == 1:
            if token.token_type != TokenType.DOT:
                break
            parts.append(token)
        elif token.token_type in (TokenType.VAR, TokenType.IDENTIFIER):
            parts.append(token)
        else:
            break
    names = parts[::2]
    if not names:
        return None
    return (
        names[-1].text.lower(),
        names[-2].text.lower() if len(names) > 1 else None,
        names[-1].line,
    )


# Words that end a table reference list or cannot be a table or an alias.
_SQL_KEYWORDS = {
    "where",
    "join",
    "inner",
    "left",
    "right",
    "full",
    "outer",
    "cross",
    "natural",
    "on",
    "using",
    "group",
    "order",
    "having",
    "limit",
    "offset",
    "fetch",
    "union",
    "intersect",
    "except",
    "minus",
    "set",
    "values",
    "select",
    "from",
    "into",
    "as",
    "lateral",
    "only",
    "window",
    "for",
    "returning",
    "when",
    "then",
    "default",
    "output",
    "with",
    "start",
    "connect",
    "pivot",
    "unpivot",
    "tablesample",
    "qualify",
    "in",
    "and",
    "or",
    "not",
    "is",
    "by",
    "straight_join",
    "apply",
    "partition",
}
_TOKEN = re.compile(r'"[^"]*"|`[^`]*`|\[[^\]]*\]|[A-Za-z_\x00][\w$#\x00]*|[(),.;]|\S')


@dataclass(frozen=True)
class Statement:
    """Tables (or, in JPQL, entity names) one statement reads and writes."""

    reads: tuple[str, ...]
    writes: tuple[str, ...]
    # Who read it: "sqlglot"; "tokens" for SQL with text the code does not
    # fix, or JPQL, which no SQL parser reads; "tokens_not_parsed" for SQL
    # sqlglot rejects in every dialect.
    reader: str = "sqlglot"
    # The text sqlglot read and the dialect that read it, for its columns.
    text: str = _field(default="", compare=False, repr=False)
    dialect: str | None = _field(default=None, compare=False, repr=False)
    # For code that may run any of several statements (one per path), each.
    alternatives: tuple["Statement", ...] = _field(
        default=(), compare=False, repr=False
    )


def _word(token):
    return token.lower() if token[:1].isalpha() or token[:1] == "_" else None


def _is_name(token):
    return token[:1] in '"`[' or (
        (token[:1].isalpha() or token[:1] in "_\x00")
        and token.lower() not in _SQL_KEYWORDS
    )


# Database system catalogs: their names are the database's, not the
# application's. Schemas: the SQL standard's information_schema, PostgreSQL's
# pg_catalog, SQL Server's and MySQL's sys, MySQL's performance_schema.
# PostgreSQL names its catalogs pg_* and SQLite reserves sqlite_* (both
# documented reserved prefixes); Oracle's data dictionary views are listed.
CATALOG_SCHEMAS = frozenset(
    {"information_schema", "pg_catalog", "sys", "performance_schema"}
)
ORACLE_DICTIONARY = frozenset(
    f"{scope}_{view}"
    for scope in ("all", "user", "dba")
    for view in (
        "users",
        "tables",
        "tab_columns",
        "tab_cols",
        "tab_identity_cols",
        "json_columns",
        "mviews",
        "tab_comments",
        "col_comments",
        "constraints",
        "cons_columns",
        "indexes",
        "ind_columns",
        "views",
        "sequences",
        "objects",
        "synonyms",
        "triggers",
    )
)


def is_catalog(parts):
    """Whether a (lower case, possibly schema-qualified) name is a database
    system catalog."""
    if len(parts) > 1 and parts[-2] in CATALOG_SCHEMAS:
        return True
    name = parts[-1]
    return name.startswith(("pg_", "sqlite_")) or (
        len(parts) == 1 and name in ORACLE_DICTIONARY
    )


# Dialects sqlglot tries, in order; the first that parses the text reads it.
SQL_DIALECTS = ("postgres", "mysql", "sqlite", "tsql", "oracle", None)


def statement_tables(text, jpql=False):
    """The tables a SQL statement reads and writes, from sqlglot.

    SQL that holds text the code does not fix (PLACEHOLDER), which no
    parser accepts, and JPQL, which is not SQL, are read by the token
    reader (``_token_statement_tables``). Other SQL is parsed by sqlglot in
    the first of SQL_DIALECTS that accepts it; SQL no dialect accepts is
    read by the token reader with ``reader="tokens_not_parsed"``.

    INSERT/REPLACE/MERGE targets, UPDATE and DELETE targets (an alias in a
    multi-table DELETE resolved to its table) and TRUNCATE are written;
    every other table is read. Common table expression names, ``dual`` and
    system catalogs (is_catalog) are left out; ``schema.table`` gives
    ``table``."""
    if jpql or PLACEHOLDER in text:
        return replace(_token_statement_tables(text, jpql), reader="tokens")
    # JDBC and JPA positional parameters (?1), which no dialect reads, are
    # bind markers like ``?``.
    masked = _COMMENT_OR_STRING.sub(_blank, text)
    # So are named parameters: sqlc's @name, SQL Server's @p, MySQL's @var
    # (never a table or column; PostgreSQL would read @ as its absolute
    # value operator). @@ system variables stay.
    for match in reversed(list(re.finditer(r"(?<![@\w])@[A-Za-z_]\w*", masked))):
        text = (
            text[: match.start()]
            + "?"
            + " " * (len(match.group()) - 1)
            + text[match.end() :]
        )
    masked = _COMMENT_OR_STRING.sub(_blank, text)
    for match in reversed(list(re.finditer(r"\?\d+", masked))):
        text = (
            text[: match.start()]
            + "?"
            + " " * (len(match.group()) - 1)
            + text[match.end() :]
        )
    found = _sqlglot_tables(text)
    if found is None:
        # Syntax sqlglot does not know yet (PostgreSQL 18's RETURNING WITH
        # (OLD AS o), SQLite's VARYING CHARACTER): read by the token
        # reader and counted as such.
        return replace(_token_statement_tables(text), reader="tokens_not_parsed")
    reads, writes, dialect = found
    return Statement(reads, writes, text=text, dialect=dialect)


def _sqlglot_tables(text):
    """(reads, writes, dialect) of SQL text by sqlglot, or None."""
    # sqlglot is a required dependency, imported when SQL is first read so
    # that importing the package stays free of it (a core-only install).
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import SqlglotError

    for dialect in SQL_DIALECTS:
        try:
            trees = [t for t in sqlglot.parse(text, read=dialect) if t is not None]
        except (SqlglotError, ValueError, RecursionError):
            continue
        if not trees or any(isinstance(t, exp.Command) for t in trees):
            continue  # A statement sqlglot only passes through as text.
        reads, writes = [], []
        for tree in trees:
            aliases = {
                table_key(t.alias): t
                for t in tree.find_all(exp.Table)
                if t.alias and t.name
            }
            written = set()
            for node in tree.walk():
                targets = []
                if isinstance(node, (exp.Insert, exp.Update, exp.Merge)):
                    targets = [node.this]
                elif isinstance(node, exp.Delete):
                    targets = [node.this, *(node.args.get("tables") or ())]
                elif isinstance(node, exp.TruncateTable):
                    targets = list(node.expressions)
                for target in targets:
                    if isinstance(target, exp.Schema):
                        target = target.this
                    if not isinstance(target, exp.Table) or not target.name:
                        continue
                    written.add(id(target))
                    if not target.db and table_key(target.name) in aliases:
                        target = aliases[table_key(target.name)]
                        written.add(id(target))
                    name = _sqlglot_name(target)
                    if name is not None:
                        writes.append(name)
            for table in tree.find_all(exp.Table):
                if id(table) in written or table.find_ancestor(exp.Lock):
                    continue  # FOR UPDATE OF t names a table already read.
                name = _sqlglot_name(table)
                if name is not None:
                    reads.append(name)
        return tuple(dict.fromkeys(reads)), tuple(dict.fromkeys(writes)), dialect
    return None


@dataclass(frozen=True)
class ColumnUse:
    """A column a statement reads (names in a read position) or writes
    (assigns, inserts or sets on conflict)."""

    table: str  # table key
    column: str
    writes: bool


def statement_columns(statement, schema):
    """(uses, complete tables) of a statement sqlglot read: the ColumnUse of
    each column it names, resolved by sqlglot's optimizer (qualify) against
    ``schema`` ({table key: column names}, the tables whose columns are all
    known), and the tables of the statement every column reference of which
    resolved to one of their columns (a ``*`` expands to all of them). A
    reference that names no column of a known table, or that sqlglot cannot
    attribute to one table, leaves the tables it could belong to incomplete.
    None for a statement sqlglot did not read."""
    import sqlglot
    from sqlglot.errors import SqlglotError

    if statement is not None and statement.alternatives:
        # Code that may run any of them uses every one's columns; a table
        # is complete when it is in each one that names it.
        found = [statement_columns(s, schema) for s in statement.alternatives]
        if any(f is None for f in found):
            return None
        uses = tuple(dict.fromkeys(u for f in found for u in f[0]))
        named = [{*s.reads, *s.writes} & set(schema) for s in statement.alternatives]
        complete = frozenset(
            t
            for t in set().union(*named)
            if all(t in f[1] for f, n in zip(found, named) if t in n)
        )
        return uses, complete
    if statement is None or statement.reader != "sqlglot" or not statement.text:
        return None
    try:
        trees = [
            t
            for t in sqlglot.parse(statement.text, read=statement.dialect)
            if t is not None
        ]
    except (SqlglotError, ValueError, RecursionError):
        return None
    state = _Columns(schema, statement.dialect)
    for tree in trees:
        state.statement(tree)
    complete = frozenset(t for t in state.seen - state.unresolved if t in schema)
    return tuple(dict.fromkeys(state.uses)), complete


class _Columns:
    """Column uses of statements, gathered by statement_columns."""

    def __init__(self, schema, dialect):
        from sqlglot.schema import MappingSchema

        self.schema = schema
        self.dialect = dialect
        # Names are compared as the engine keeps them (column_key), so the
        # schema is not normalized again.
        self.mapping = MappingSchema(
            {t: {c: "UNKNOWN" for c in columns} for t, columns in schema.items()},
            dialect=dialect,
            normalize=False,
        )
        self.uses, self.seen, self.unresolved = [], set(), set()

    def _key(self, table):
        return table_key(table.name) if table.name else None

    def _use(self, table, column, writes):
        """Record a column of ``table`` (a key), or mark it unresolved."""
        self.seen.add(table)
        if column in self.schema.get(table, ()):
            self.uses.append(ColumnUse(table, column, writes))
        else:
            self.unresolved.add(table)

    def _all_tables(self, tree):
        from sqlglot import exp

        found = {self._key(t) for t in tree.find_all(exp.Table)} - {None}
        self.seen |= found
        self.unresolved |= found

    def select(self, tree):
        """Columns a query reads, by sqlglot's scopes after qualify."""
        from sqlglot import exp
        from sqlglot.errors import SqlglotError
        from sqlglot.optimizer.qualify import qualify
        from sqlglot.optimizer.scope import traverse_scope

        try:
            qualified = qualify(
                tree.copy(),
                schema=self.mapping,
                dialect=self.dialect,
                validate_qualify_columns=False,
                quote_identifiers=False,
                identify=False,
            )
            scopes = traverse_scope(qualified)
        except (SqlglotError, ValueError, KeyError, RecursionError):
            self._all_tables(tree)
            return
        for scope in scopes:
            tables = {
                alias: self._key(source)
                for alias, source in scope.sources.items()
                if isinstance(source, exp.Table) and self._key(source)
            }
            self.seen |= set(tables.values())
            projections = (
                scope.expression.expressions
                if isinstance(scope.expression, exp.Select)
                else ()
            )
            if any(
                isinstance(p, exp.Star)
                or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star))
                for p in projections
            ):
                # A * qualify could not expand: a table's columns unknown.
                self.unresolved |= set(tables.values())
            for column in scope.columns:
                if isinstance(column.this, exp.Star):
                    continue
                source = scope.sources.get(column.table) if column.table else None
                if source is None:
                    if column.table or len(tables) != 1:
                        # Not attributed to one source (an output alias is
                        # resolved by qualify, not left here).
                        if not self._is_output_alias(scope, column):
                            self.unresolved |= set(tables.values())
                        continue
                    table = next(iter(tables.values()))
                    self._use(table, column.name, False)
                elif isinstance(source, exp.Table):
                    table = self._key(source)
                    if table is not None:
                        self._use(table, column.name, False)

    @staticmethod
    def _is_output_alias(scope, column):
        from sqlglot import exp

        if column.table or not isinstance(scope.expression, exp.Select):
            return False
        return column.name in {p.alias for p in scope.expression.expressions if p.alias}

    def _query(self, target, extra_sources, expressions, where, with_):
        """Columns read by ``expressions`` and ``where`` over ``target`` and
        ``extra_sources``, as the SELECT that reads them."""
        from sqlglot import exp

        select = exp.Select(
            expressions=[e.copy() for e in expressions] or [exp.Literal.number(1)]
        ).from_(target.copy())
        for source in extra_sources:
            select = select.join(source.copy())
        if where is not None:
            select.set("where", where.copy())
        if with_ is not None:
            select.set("with_", with_.copy())
        self.select(select)

    def _target(self, node):
        from sqlglot import exp

        target = node.this
        if isinstance(target, exp.Schema):
            target = target.this
        return target if isinstance(target, exp.Table) else None

    def _assigned(self, target, key, assignments, writes=True):
        """Columns that SET-style ``a = b`` pairs assign on ``target``."""
        from sqlglot import exp

        names = {target.name, target.alias_or_name}
        values = []
        for pair in assignments:
            if not isinstance(pair, exp.EQ):
                continue
            left = pair.this
            if isinstance(left, exp.Column) and (not left.table or left.table in names):
                self._use(key, column_key(left), writes)
            else:
                self.unresolved.add(key)
            values.append(pair.expression)
        return values

    def statement(self, tree):
        from sqlglot import exp

        dml = (exp.Update, exp.Insert, exp.Delete)
        if any(isinstance(c.this, dml) for c in tree.find_all(exp.CTE)):
            # A data-modifying CTE is a statement of its own; the query
            # around it reads what it RETURNs, from its target.
            tree = tree.copy()
            for cte in list(tree.find_all(exp.CTE)):
                if not isinstance(cte.this, dml):
                    continue
                inner = cte.this
                self.statement(inner)
                target = self._target(inner)
                returning = inner.args.get("returning")
                stand_in = exp.Select(
                    expressions=[e.copy() for e in returning.expressions]
                    if returning
                    else [exp.Literal.number(1)]
                )
                if target is not None:
                    stand_in = stand_in.from_(target.copy())
                cte.set("this", stand_in)
        if isinstance(tree, (exp.Select, exp.SetOperation)):
            self.select(tree)
            return
        with_ = tree.args.get("with_")
        returning = tree.args.get("returning")
        returned = list(returning.expressions) if returning else []
        if isinstance(tree, exp.Update):
            target = self._target(tree)
            key = target and self._key(target)
            if key is None:
                self._all_tables(tree)
                return
            values = self._assigned(target, key, tree.expressions)
            sources = []
            from_ = tree.args.get("from")
            if from_ is not None:
                sources.append(from_.this)
            sources.extend(j.this for j in tree.args.get("joins") or ())
            self._query(
                target, sources, values + returned, tree.args.get("where"), with_
            )
        elif isinstance(tree, exp.Delete):
            target = self._target(tree)
            key = target and self._key(target)
            if key is None:
                self._all_tables(tree)
                return
            self.seen.add(key)
            sources = list(tree.args.get("using") or ())
            self._query(target, sources, returned, tree.args.get("where"), with_)
        elif isinstance(tree, exp.Insert):
            target = self._target(tree)
            key = target and self._key(target)
            if key is None:
                self._all_tables(tree)
                return
            if isinstance(tree.this, exp.Schema) and tree.this.expressions:
                for column in tree.this.expressions:
                    self._use(key, column_key(column), True)
            elif key in self.schema:
                for column in self.schema[key]:
                    self._use(key, column, True)
            else:
                self.seen.add(key)
                self.unresolved.add(key)
            source = tree.expression
            if isinstance(source, (exp.Select, exp.SetOperation)):
                if with_ is not None and not source.args.get("with_"):
                    source = source.copy()
                    source.set("with_", with_.copy())
                self.select(source)
            conflict = tree.args.get("conflict")
            values = []
            if conflict is not None:
                for column in conflict.args.get("conflict_keys") or ():
                    self._use(key, column_key(column), False)
                values = self._assigned(target, key, conflict.expressions or ())
                # EXCLUDED.c is the row being inserted, not a read.
                values = [
                    v
                    for v in values
                    if not (isinstance(v, exp.Column) and v.table.lower() == "excluded")
                ]
            if values or returned:
                self._query(target, [], values + returned, None, None)
        else:
            self._all_tables(tree)


def _visible_ctes(table):
    """The names of the common table expressions a table reference can
    name: those of every enclosing WITH, except that inside the body of one
    of them only the earlier ones are visible (and itself, under
    RECURSIVE). sqlglot's scope builder does not build scopes for
    data-modifying CTEs (``WITH t AS (UPDATE t ... RETURNING *)``), so the
    SQL visibility rule is applied here."""
    from sqlglot import exp

    names, node = set(), table
    while node is not None:
        parent = node.parent
        if isinstance(parent, exp.With):
            # ``node`` is one CTE of this WITH: the earlier ones are visible.
            ctes = list(parent.expressions)
            index = next((i for i, c in enumerate(ctes) if c is node), len(ctes))
            if parent.args.get("recursive"):
                index += 1
            names |= {table_key(c.alias_or_name) for c in ctes[:index]}
        elif parent is not None:
            # The WITH of an enclosing statement (arg "with_" in sqlglot 30,
            # "with" before).
            clause = next(
                (
                    v
                    for k, v in parent.args.items()
                    if k in ("with", "with_") and isinstance(v, exp.With)
                ),
                None,
            )
            if clause is not None and clause is not node:
                names |= {table_key(c.alias_or_name) for c in clause.expressions}
        node = parent
    return names


def _sqlglot_name(table, ctes=None):
    """The table key of a sqlglot Table, or None for a CTE it can name,
    ``dual``, a catalog or a table-valued function."""
    from sqlglot import exp

    if not table.name or not isinstance(table.this, exp.Identifier):
        return None
    parts = [table_key(p) for p in (table.catalog, table.db, table.name) if p]
    if len(parts) == 1 and parts[0] in _visible_ctes(table):
        return None
    if parts[-1] == "dual" or is_catalog(parts):
        return None
    return parts[-1]


def _token_statement_tables(text, jpql=False):
    """The tables a SQL statement (or entities a JPQL one) names after FROM,
    JOIN, USING, INTO, UPDATE and TRUNCATE.

    INSERT/MERGE/REPLACE INTO, UPDATE, DELETE FROM and TRUNCATE targets are
    written; every other table is read. Comments, string literals, common
    table expression names, subqueries' own parentheses, function calls in
    FROM (a name followed by ``(``) and FROM inside function arguments
    (``EXTRACT(YEAR FROM d)``) are skipped. A name holding text the code
    does not fix (PLACEHOLDER) is dropped, and so is a system catalog
    (``information_schema.tables``, ``pg_class``, ``sqlite_master``; see
    is_catalog). In SQL ``schema.table`` gives ``table``; in JPQL a dotted
    name is a path (``o.pets``), not an entity.
    """
    text = _COMMENT_OR_STRING.sub(_blank, text)
    tokens = _TOKEN.findall(text)
    words = [_word(t) for t in tokens]
    # Common table expressions: name [(columns)] AS (
    ctes = set()
    for i, word in enumerate(words):
        if word == "as" and i + 1 < len(tokens) and tokens[i + 1] == "(" and i:
            j = i - 1
            if tokens[j] == ")":
                depth = 0
                while j >= 0:
                    depth += {")": 1, "(": -1}.get(tokens[j], 0)
                    if depth == 0:
                        break
                    j -= 1
                j -= 1
            if j >= 0 and _is_name(tokens[j]):
                ctes.add(table_key(tokens[j]))
    reads, writes = [], []
    # Per parenthesis level: whether a statement verb opened it, so FROM is a
    # clause and not part of a function's arguments.
    levels = [False]
    i = 0

    def name_at(k, columns=False):
        """(name or None, next index) of a possibly dotted name at k; with
        ``columns`` a ``(`` after it opens a column list, not a call."""
        if k >= len(tokens) or not _is_name(tokens[k]):
            return None, k
        parts = [tokens[k]]
        k += 1
        while k + 1 < len(tokens) and tokens[k] == "." and _is_name(tokens[k + 1]):
            parts.append(tokens[k + 1])
            k += 2
        if k < len(tokens) and tokens[k] == "(" and not columns:
            return None, k  # A function call.
        if any("\x00" in p for p in parts):
            return None, k
        if jpql:
            return (parts[0] if len(parts) == 1 else None), k
        if is_catalog([table_key(p) for p in parts]):
            return None, k
        return table_key(parts[-1]), k

    def take(k, into, columns=False):
        name, k = name_at(k, columns)
        if name is not None and table_key(name) not in ctes and name != "dual":
            into.append(name)
        return k

    while i < len(tokens):
        token, word = tokens[i], words[i]
        previous = words[i - 1] if i else None
        if token == "(":
            levels.append(False)
        elif token == ")":
            if len(levels) > 1:
                levels.pop()
        elif word in ("select", "delete", "update", "insert", "merge", "with"):
            levels[-1] = True
        # JPQL may leave out SELECT: FROM Owner o WHERE ...
        if word == "from" and (levels[-1] or previous is None):
            target = writes if previous == "delete" else reads
            k = i + 1
            while k < len(tokens) and words[k] in ("only", "lateral"):
                k += 1
            k = take(k, target)
            # A comma-separated list: FROM a x, b AS y
            while True:
                while (
                    k < len(tokens)
                    and tokens[k] not in (",", "(", ")", ";")
                    and (
                        words[k] is None
                        or words[k] not in _SQL_KEYWORDS
                        or words[k] == "as"
                    )
                ):
                    k += 1
                if k < len(tokens) and tokens[k] == "," and levels[-1]:
                    k = take(k + 1, reads)
                    continue
                break
        elif word == "join":
            take(i + 1, reads)
        elif word == "using" and levels[-1]:
            take(i + 1, reads)
        elif word == "into" and previous in ("insert", "merge", "replace", "ignore"):
            take(i + 1, writes, columns=True)
        elif word == "update" and (previous is None or tokens[i - 1] in ";()"):
            take(i + 1, writes)
        elif word == "truncate":
            k = i + 1
            if k < len(tokens) and words[k] == "table":
                k += 1
            take(k, writes)
        i += 1
    return Statement(tuple(dict.fromkeys(reads)), tuple(dict.fromkeys(writes)))


def default_table_name(entity):
    """Spring Boot's default physical name (Hibernate's
    CamelCaseToUnderscoresNamingStrategy): dots become underscores, an
    underscore goes before an upper-case letter that has a lower-case letter
    or digit on both sides, then lower case."""
    text = entity.replace(".", "_")

    def lower(ch):
        return ch.islower() or ch.isdigit()

    out = []
    for i, ch in enumerate(text):
        if (
            0 < i < len(text) - 1
            and lower(text[i - 1])
            and ch.isupper()
            and lower(text[i + 1])
        ):
            out.append("_")
        out.append(ch)
    return "".join(out).lower()


def _from(imports, written, packages):
    """Whether an annotation or type written in the file comes from ``packages``."""
    package, _, name = written.rpartition(".")
    if package:
        return any(package == p or package.startswith(p + ".") for p in packages)
    imported = imports.names.get(name, set())
    if imported:
        return any(i == p or i.startswith(p + ".") for i in imported for p in packages)
    return any(
        w == p or w.startswith(p + ".") for w in imports.wildcards for p in packages
    )


def _declarations(root):
    stack = [(root, ())]
    while stack:
        node, outer = stack.pop()
        if node.type in (
            "class_declaration",
            "interface_declaration",
            "record_declaration",
        ):
            yield node, outer
            name = node.child_by_field_name("name")
            outer = (*outer, name.text.decode("utf-8")) if name else outer
        stack.extend((c, outer) for c in reversed(node.named_children))


def extract_java(tree, content, file, definitions, constants=None):
    """(mappings, repositories) of one Java file."""
    root = tree.root_node
    imports = _Imports(root, content)
    values = _Values(root, content, constants, imports)
    package = _package(root, content)
    spans = {
        (d.line, d.column, d.end_line, d.end_column): d
        for d in definitions
        if d.file == file
    }
    mappings, repositories = [], []
    for node, outer in _declarations(root):
        name_node = node.child_by_field_name("name")
        definition = spans.get(
            (
                node.start_point.row + 1,
                node.start_point.column,
                node.end_point.row + 1,
                node.end_point.column,
            )
        )
        if name_node is None or definition is None:
            continue
        simple = _text(name_node, content)
        qualified = ".".join(p for p in (package, *outer, simple) if p)
        if node.type == "class_declaration":
            annotations = {
                w.rpartition(".")[2]: a
                for w, a in _annotations(node, content)
                if _from(imports, w, _PERSISTENCE)
            }
            if "Entity" not in annotations:
                continue
            explicit = None
            if "Table" in annotations:
                written = _arguments(annotations["Table"]).get("name")
                if written is not None:
                    explicit = values.evaluate(written)
                    if PLACEHOLDER in explicit or not explicit:
                        continue  # A name the file does not fix.
            entity = simple
            entity_name = _arguments(annotations["Entity"]).get("name")
            if entity_name is not None:
                entity = values.evaluate(entity_name)
                if PLACEHOLDER in entity or not entity:
                    entity = simple
            mappings.append(
                Mapping(
                    file,
                    definition.id,
                    qualified,
                    table_key(explicit) if explicit else default_table_name(entity),
                    explicit is not None,
                    node.start_point.row + 1,
                    node.start_point.column,
                    entity_name=entity,
                )
            )
            mappings.extend(
                _association_tables(
                    node, content, file, definition.id, qualified, imports, values
                )
            )
        elif node.type == "interface_declaration":
            extends = next(
                (c for c in node.named_children if c.type == "extends_interfaces"),
                None,
            )
            if extends is None:
                continue
            for written_type in _generic_types(extends):
                base = _type_name(written_type.named_children[0], content)
                short = base.rpartition(".")[2] if base else None
                if short not in READ_WRITE_REPOSITORIES | READ_ONLY_REPOSITORIES:
                    continue
                if not _from(imports, base, (_SPRING_DATA,)):
                    continue
                arguments = next(
                    (
                        c
                        for c in written_type.named_children
                        if c.type == "type_arguments"
                    ),
                    None,
                )
                first = arguments.named_children[0] if arguments else None
                written = _type_name(first, content)
                if not written:
                    continue
                repositories.append(
                    Repository(
                        file,
                        definition.id,
                        written,
                        _candidates(written, imports, package),
                        short in READ_WRITE_REPOSITORIES,
                        node.start_point.row + 1,
                        node.start_point.column,
                    )
                )
    return mappings, repositories


def _association_tables(entity, content, file, entity_id, qualified, imports, values):
    """Mappings for the @JoinTable and @CollectionTable names of an entity's
    fields and getters (not of nested classes)."""
    body = entity.child_by_field_name("body")
    found = []
    for member in body.named_children if body is not None else ():
        if member.type not in ("field_declaration", "method_declaration"):
            continue
        for written, annotation in _annotations(member, content):
            if written.rpartition(".")[2] not in (
                "JoinTable",
                "CollectionTable",
            ) or not _from(imports, written, _PERSISTENCE):
                continue
            name = _arguments(annotation).get("name")
            text = values.evaluate(name) if name is not None else PLACEHOLDER
            if PLACEHOLDER in text or not text:
                continue  # Default join table names are not derived.
            if member.type == "field_declaration":
                declarator = member.child_by_field_name("declarator")
                label = declarator.child_by_field_name("name") if declarator else None
            else:
                label = member.child_by_field_name("name")
            found.append(
                Mapping(
                    file,
                    entity_id,
                    qualified,
                    table_key(text),
                    True,
                    annotation.start_point.row + 1,
                    annotation.start_point.column,
                    _text(label, content) if label is not None else "?",
                )
            )
    return found


def _type_name(node, content):
    """A written class type without type arguments, or None."""
    if node is None or node.type not in ("type_identifier", "scoped_type_identifier"):
        return None
    return re.sub(r"\s+", "", _text(node, content))


def _generic_types(extends):
    stack = list(extends.named_children)
    while stack:
        node = stack.pop(0)
        if node.type == "generic_type":
            yield node
        elif node.type == "type_list":
            stack[:0] = node.named_children


def _candidates(written, imports, package):
    """The qualified class names a written type may denote, best first: as
    written when qualified, else the single-type import, else the same
    package, then each wildcard import."""
    head, _, name = written.partition(".")
    if "." in written and head[:1].islower():
        return (written,)
    imported = sorted(imports.names.get(head, ()))
    if imported:
        return tuple(f"{p}.{written}" for p in imported)
    own = f"{package}.{written}" if package else written
    return (own, *(f"{w}.{written}" for w in sorted(imports.wildcards)))


# EntityManager methods that write (persist, merge, remove) or read (find,
# getReference) an entity's table.
ENTITY_MANAGER_WRITES = {"persist", "merge", "remove"}
ENTITY_MANAGER_READS = {"find", "getReference"}
_JAVA_SCOPES = ("method_declaration", "constructor_declaration", "lambda_expression")


@dataclass(frozen=True)
class EntityUse:
    """An EntityManager call on an entity whose type the code declares."""

    file: str
    owner_id: str | None
    entity: str  # the type as written
    candidates: tuple[str, ...]
    writes: bool
    line: int
    column: int


def _declared_types(scope, content):
    """{name: written type} of the parameters and locals declared once in a
    method, constructor or lambda (nested lambdas excluded)."""
    found = {}
    stack = [scope]
    while stack:
        node = stack.pop()
        if node.type in ("formal_parameter", "local_variable_declaration"):
            written = _type_name(node.child_by_field_name("type"), content)
            names = (
                [node.child_by_field_name("name")]
                if node.type == "formal_parameter"
                else [
                    d.child_by_field_name("name")
                    for d in node.children_by_field_name("declarator")
                ]
            )
            for name in names:
                if name is not None:
                    found.setdefault(_text(name, content), []).append(written)
        if node is not scope and node.type in _JAVA_SCOPES:
            continue
        stack.extend(node.named_children)
    # A name declared twice is known to be local but of no single type.
    return {n: t[0] if len(t) == 1 else None for n, t in found.items()}


def _field_types(body, content):
    """{name: written type} of the fields a class body declares once."""
    found = {}
    for node in body.named_children if body is not None else ():
        if node.type != "field_declaration":
            continue
        written = _type_name(node.child_by_field_name("type"), content)
        for declarator in node.children_by_field_name("declarator"):
            name = _text(declarator.child_by_field_name("name"), content)
            found.setdefault(name, []).append(written)
    return {n: t[0] for n, t in found.items() if len(t) == 1 and t[0]}


def _variable_type(node, scope, body, content):
    """The declared type of ``name`` (a parameter or local of the scope, else
    a field) or ``this.name`` (a field), or None."""
    if node is None:
        return None
    if node.type == "identifier":
        variable = _text(node, content)
        local = _declared_types(scope, content) if scope is not None else {}
        if variable in local:
            return local[variable]
        return _field_types(body, content).get(variable)
    if (
        node.type == "field_access"
        and _text(node.child_by_field_name("object"), content) == "this"
    ):
        return _field_types(body, content).get(
            _text(node.child_by_field_name("field"), content)
        )
    return None


def extract_entity_manager(tree, content, file, definitions):
    """EntityManager ``persist``/``merge``/``remove`` (WRITES) of a parameter,
    local or field whose declared type is a class, and ``find``/
    ``getReference`` (READS) of a class literal, in a file importing
    jakarta.persistence or javax.persistence."""
    root = tree.root_node
    imports = _Imports(root, content)
    if not any(_imports_package(imports, p) for p in _PERSISTENCE):
        return []
    package = _package(root, content)
    found = []
    # (node, enclosing scope, enclosing class body)
    stack = [(root, None, None)]
    while stack:
        node, scope, body = stack.pop()
        if node.type in _JAVA_SCOPES:
            scope = node
        elif node.type in (
            "class_declaration",
            "enum_declaration",
            "record_declaration",
        ):
            body = node.child_by_field_name("body")
        stack.extend((c, scope, body) for c in reversed(node.named_children))
        if node.type != "method_invocation":
            continue
        name = _text(node.child_by_field_name("name"), content)
        if name not in ENTITY_MANAGER_WRITES | ENTITY_MANAGER_READS:
            continue
        first = _first_argument(node)
        if first is None or _variable_type(
            node.child_by_field_name("object"), scope, body, content
        ) not in ("EntityManager", *(f"{p}.EntityManager" for p in _PERSISTENCE)):
            continue
        written = None
        if name in ENTITY_MANAGER_READS:
            if first.type == "class_literal":
                written = _type_name(first.named_children[0], content)
        else:
            written = _variable_type(first, scope, body, content)
        if not written:
            continue
        found.append(
            EntityUse(
                file,
                _innermost(definitions, file, node),
                written,
                _candidates(written, imports, package),
                name in ENTITY_MANAGER_WRITES,
                node.start_point.row + 1,
                node.start_point.column,
            )
        )
    return found


# Django ---------------------------------------------------------------------

_SPRING_JDBC = "org.springframework.jdbc"
# JdbcTemplate / NamedParameterJdbcTemplate / JdbcOperations methods whose
# first argument is SQL text.
JDBC_METHODS = {
    "query",
    "queryForObject",
    "queryForList",
    "queryForMap",
    "queryForRowSet",
    "queryForStream",
    "queryForInt",
    "queryForLong",
    "update",
    "batchUpdate",
    "execute",
}


@dataclass(frozen=True)
class SqlUse:
    """SQL (or JPQL) text that code runs, and the tables it names."""

    file: str
    owner_id: str | None  # the innermost declaration holding it
    how: str  # spring-data-query | jpa-query | jdbc-sql | jdbc-insert-table | mybatis-annotation
    jpql: bool
    statement: Statement | None  # None: text the file does not fix
    line: int
    column: int


def _imports_package(imports, prefix):
    return any(
        p == prefix or p.startswith(prefix + ".")
        for packages in (*imports.names.values(), imports.wildcards)
        for p in packages
    )


def _first_argument(call):
    arguments = call.child_by_field_name("arguments")
    named = [
        c
        for c in (arguments.named_children if arguments is not None else ())
        if c.type not in ("line_comment", "block_comment")
    ]
    return named[0] if named else None


def extract_java_sql(tree, content, file, definitions, constants=None):
    """SQL and JPQL that one Java file runs: Spring Data ``@Query`` methods,
    ``EntityManager.createQuery``/``createNativeQuery``, JdbcTemplate and
    JdbcClient SQL, and ``SimpleJdbcInsert.withTableName``."""
    root = tree.root_node
    imports = _Imports(root, content)
    persistence = any(_imports_package(imports, p) for p in _PERSISTENCE)
    jdbc = _imports_package(imports, _SPRING_JDBC)
    mybatis = _imports_package(imports, _MYBATIS)
    if not (persistence or jdbc or mybatis or _imports_package(imports, _SPRING_DATA)):
        return []
    values = _Values(root, content, constants, imports)
    found = []

    def use(node, how, jpql, text):
        statement = (
            None
            if text is None or text.replace(PLACEHOLDER, "").strip() == ""
            else statement_tables(text, jpql=jpql)
        )
        if statement and not (statement.reads or statement.writes):
            if PLACEHOLDER in text:
                statement = None  # Its tables are in text the file does not fix.
        found.append(
            SqlUse(
                file,
                _innermost(definitions, file, node),
                how,
                jpql,
                statement,
                node.start_point.row + 1,
                node.start_point.column,
            )
        )

    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.named_children))
        if node.type == "method_declaration":
            for written, annotation in _annotations(node, content):
                kind = written.rpartition(".")[2]
                if (
                    mybatis
                    and kind in ("Select", "Insert", "Update", "Delete")
                    and _from(imports, written, (_MYBATIS,))
                ):
                    # MyBatis joins the strings of an array with spaces.
                    strings = values.strings(_arguments(annotation).get("value"))
                    text = (
                        None
                        if not strings
                        else mybatis_script(
                            PLACEHOLDER if PLACEHOLDER in strings else " ".join(strings)
                        )
                    )
                    use(node, "mybatis-annotation", False, text)
                    continue
                if kind != "Query" or not _from(imports, written, (_SPRING_DATA,)):
                    continue
                arguments = _arguments(annotation)
                value = arguments.get("value")
                native = arguments.get("nativeQuery")
                use(
                    node,
                    "spring-data-query",
                    not (native is not None and _text(native, content) == "true"),
                    None if value is None else values.evaluate(value),
                )
        elif node.type == "method_invocation":
            name = _text(node.child_by_field_name("name"), content)
            receiver = node.child_by_field_name("object")
            first = _first_argument(node)
            if first is None:
                continue
            if persistence and name in ("createQuery", "createNativeQuery"):
                text = values.evaluate(first)
                if first.type in ("string_literal", "binary_expression") or (
                    text != PLACEHOLDER
                ):
                    use(node, "jpa-query", name == "createQuery", text)
            elif jdbc and name == "sql":
                use(node, "jdbc-sql", False, values.evaluate(first))
            elif jdbc and name == "withTableName":
                text = values.evaluate(first)
                statement = (
                    None
                    if PLACEHOLDER in text or not text
                    else Statement((), (table_key(text.rpartition(".")[2]),))
                )
                found.append(
                    SqlUse(
                        file,
                        _innermost(definitions, file, node),
                        "jdbc-insert-table",
                        False,
                        statement,
                        node.start_point.row + 1,
                        node.start_point.column,
                    )
                )
            elif (
                jdbc
                and name in JDBC_METHODS
                and receiver is not None
                and receiver.type != "method_invocation"
            ):
                # Fluent JdbcClient calls (.sql(...).query(mapper)) are read
                # at .sql(); a JdbcTemplate is a field or local.
                text = values.evaluate(first)
                if first.type in ("string_literal", "binary_expression") or (
                    text != PLACEHOLDER
                ):
                    use(node, "jdbc-sql", False, text)
    return found


DJANGO_READS = {
    "aggregate",
    "all",
    "annotate",
    "count",
    "earliest",
    "exclude",
    "exists",
    "filter",
    "first",
    "get",
    "in_bulk",
    "iterator",
    "last",
    "latest",
    "values",
    "values_list",
    # Methods that start a chain as often as they continue one.
    "order_by",
    "only",
    "defer",
    "select_related",
    "prefetch_related",
    "distinct",
    "reverse",
    "select_for_update",
    "dates",
    "datetimes",
}
DJANGO_WRITES = {
    "bulk_create",
    "bulk_update",
    "create",
    "delete",
    "update",
}
DJANGO_READ_WRITES = {"get_or_create", "update_or_create"}


@dataclass(frozen=True)
class DjangoModel:
    file: str
    model_id: str
    name: str
    bases: tuple[str, ...]  # written base names, last dotted part
    django_base: bool  # one base is django.db.models.Model
    abstract: bool
    proxy: bool
    db_table: str | None
    app_label: str | None  # Meta.app_label
    m2m: tuple[tuple[str, str | None, bool], ...]  # (field, db_table, through)
    line: int
    column: int
    fields: tuple = ()  # DjangoField, in class order


# Django's relation fields: those holding a foreign key column, and those
# whose column is on another table (or a join table).
DJANGO_FOREIGN_KEYS = frozenset({"ForeignKey", "OneToOneField"})
DJANGO_NO_COLUMN = frozenset(
    {"ManyToManyField", "GenericForeignKey", "GenericRelation", "ForeignObject"}
)


@dataclass(frozen=True)
class DjangoField:
    """A field assigned in a Django model class body: ``kind`` is the Django
    field class (django.db.models or django.contrib), or ``Field`` for a
    project's own field class; ``target`` the related model's written name
    (last dotted part, ``self`` for the model itself) for a relation."""

    name: str
    kind: str
    db_column: str | None
    target: str | None
    primary: bool
    line: int
    column: int
    related_query: str | None = None  # related_query_name, else related_name


@dataclass(frozen=True)
class DjangoAccess:
    file: str
    caller_id: str | None
    model: str  # the class name before .objects
    reads: bool
    writes: bool
    line: int
    column: int


def _string(node, content):
    """A plain string literal's text, or None."""
    if node is None or node.type != "string":
        return None
    parts = [c for c in node.named_children if c.type == "string_content"]
    if any(c.type == "interpolation" for c in node.named_children):
        return None
    prefix = _text(node, content).split("'")[0].split('"')[0].lower()
    if "f" in prefix or "b" in prefix:
        return None
    return "".join(_text(c, content) for c in parts)


def _python_imports(root, content):
    """{local name: dotted origin} of a module's top-level from-imports."""
    names = {}
    for node in root.named_children:
        if node.type != "import_from_statement":
            continue
        module = node.child_by_field_name("module_name")
        if module is None:
            continue
        origin = _text(module, content)
        for child in node.children_by_field_name("name"):
            if child.type == "aliased_import":
                name = _text(child.child_by_field_name("name"), content)
                local = _text(child.child_by_field_name("alias"), content)
            else:
                name = local = _text(child, content)
            names[local] = f"{origin}.{name}"
    return names


def _assignments(block, content):
    """(name, value node) of simple assignments directly in a block."""
    for statement in block.named_children if block is not None else ():
        if statement.type != "expression_statement" or not statement.named_children:
            continue
        node = statement.named_children[0]
        if node.type == "assignment":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is not None and left.type == "identifier" and right is not None:
                yield _text(left, content), right


def _keyword_arguments(call, content):
    arguments = call.child_by_field_name("arguments")
    found = {}
    for child in arguments.named_children if arguments is not None else ():
        if child.type == "keyword_argument":
            found[_text(child.child_by_field_name("name"), content)] = (
                child.child_by_field_name("value")
            )
    return found


def _innermost(definitions, file, node):
    start = (node.start_point.row + 1, node.start_point.column)
    end = (node.end_point.row + 1, node.end_point.column)
    best = None
    for d in definitions:
        if d.file != file:
            continue
        if (d.line, d.column) <= start and end <= (d.end_line, d.end_column):
            size = (d.end_line - d.line, d.end_column - d.column)
            if best is None or size < best[0]:
                best = (size, d.id)
    return best[1] if best else None


def extract_django(tree, content, file, definitions):
    """(models, accesses) of one Python file."""
    root = tree.root_node
    imports = _python_imports(root, content)
    spans = {
        (d.line, d.column, d.end_line, d.end_column): d
        for d in definitions
        if d.file == file
    }

    def is_django(written):
        """Whether a written base or field type names django.db.models.X."""
        head, _, tail = written.partition(".")
        origin = imports.get(head)
        if tail:
            return origin == "django.db.models" and tail
        if origin and origin.startswith("django.db.models."):
            return origin.rpartition(".")[2]
        return None

    models, accesses = [], []
    for node in root.named_children:
        if node.type == "decorated_definition":
            node = node.child_by_field_name("definition")
        if node is None or node.type != "class_definition":
            continue
        definition = spans.get(
            (
                node.start_point.row + 1,
                node.start_point.column,
                node.end_point.row + 1,
                node.end_point.column,
            )
        )
        superclasses = node.child_by_field_name("superclasses")
        if definition is None or superclasses is None:
            continue
        written = [
            re.sub(r"\s+", "", _text(c, content))
            for c in superclasses.named_children
            if c.type in ("identifier", "attribute")
        ]
        body = node.child_by_field_name("body")
        meta, m2m = {}, []
        for statement in body.named_children if body is not None else ():
            if statement.type == "class_definition":
                name = statement.child_by_field_name("name")
                if name is not None and _text(name, content) == "Meta":
                    meta = dict(
                        _assignments(statement.child_by_field_name("body"), content)
                    )
        fields = []
        for statement in body.named_children if body is not None else ():
            if statement.type != "expression_statement" or not statement.named_children:
                continue
            assignment = statement.named_children[0]
            left = assignment.child_by_field_name("left")
            value = assignment.child_by_field_name("right")
            if (
                assignment.type != "assignment"
                or left is None
                or left.type != "identifier"
                or value is None
                or value.type != "call"
            ):
                continue
            callee = re.sub(
                r"\s+", "", _text(value.child_by_field_name("function"), content)
            )
            head = callee.partition(".")[0]
            last = callee.rpartition(".")[2]
            origin = imports.get(head) or ""
            if origin.startswith("django.") or origin == "django":
                if not (
                    last.endswith("Field")
                    or last in DJANGO_FOREIGN_KEYS
                    or last in DJANGO_NO_COLUMN
                ):
                    continue
                kind = last
            elif last.endswith("Field"):
                kind = "Field"
            else:
                continue
            keywords = _keyword_arguments(value, content)
            target = keywords.get("to")
            if target is None:
                arguments = value.child_by_field_name("arguments")
                positional = [
                    a
                    for a in (arguments.named_children if arguments else ())
                    if a.type not in ("keyword_argument", "comment")
                ]
                target = positional[0] if positional else None
            related = None
            if kind in DJANGO_FOREIGN_KEYS or kind in DJANGO_NO_COLUMN:
                if target is not None and target.type == "string":
                    related = (_string(target, content) or "").rpartition(".")[2]
                elif target is not None and target.type in ("identifier", "attribute"):
                    related = _text(target, content).rpartition(".")[2]
            primary = keywords.get("primary_key")
            related_query = _string(
                keywords.get("related_query_name"), content
            ) or _string(keywords.get("related_name"), content)
            fields.append(
                DjangoField(
                    _text(left, content),
                    kind,
                    _string(keywords.get("db_column"), content),
                    related or None,
                    primary is not None and _text(primary, content) == "True",
                    left.start_point.row + 1,
                    left.start_point.column,
                    related_query,
                )
            )
        for field, value in _assignments(body, content):
            if value.type != "call":
                continue
            function = value.child_by_field_name("function")
            if is_django(re.sub(r"\s+", "", _text(function, content))) != (
                "ManyToManyField"
            ):
                continue
            keywords = _keyword_arguments(value, content)
            m2m.append(
                (
                    field,
                    _string(keywords.get("db_table"), content),
                    "through" in keywords,
                )
            )

        def flag(key):
            value = meta.get(key)
            return value is not None and _text(value, content) == "True"

        models.append(
            DjangoModel(
                file,
                definition.id,
                _text(node.child_by_field_name("name"), content),
                tuple(w.rpartition(".")[2] for w in written),
                any(is_django(w) == "Model" for w in written),
                flag("abstract"),
                flag("proxy"),
                _string(meta.get("db_table"), content),
                _string(meta.get("app_label"), content),
                tuple(m2m),
                node.start_point.row + 1,
                node.start_point.column,
                tuple(fields),
            )
        )
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type != "attribute":
            continue
        target = node.child_by_field_name("object")
        name = node.child_by_field_name("attribute")
        if name is None or _text(name, content) != "objects":
            continue
        # ``Model.objects`` or a module-qualified ``models.Model.objects``:
        # the model is the last name, where the index resolves it.
        while target is not None and target.type == "attribute":
            target = target.child_by_field_name("attribute")
        if target is None or target.type != "identifier":
            continue
        methods, current = [], node
        while (
            current.parent is not None
            and current.parent.type == "attribute"
            and current.parent.child_by_field_name("object") == current
        ):
            current = current.parent
            methods.append(_text(current.child_by_field_name("attribute"), content))
            if current.parent is not None and current.parent.type == "call":
                current = current.parent
        reads = any(m in DJANGO_READS | DJANGO_READ_WRITES for m in methods)
        writes = any(m in DJANGO_WRITES | DJANGO_READ_WRITES for m in methods)
        if not (reads or writes):
            continue
        accesses.append(
            DjangoAccess(
                file,
                _innermost(definitions, file, node),
                _text(target, content),
                reads,
                writes,
                target.start_point.row + 1,
                target.start_point.column,
            )
        )
    return models, accesses


def django_app(file, labels):
    """(app directory, label) of a models module: the package holding
    ``models.py`` or the ``models`` package, labelled by its ``apps.py``
    AppConfig ``label`` when ``labels`` has one, else its directory name."""
    parts = file.split("/")
    packages = parts[:-1]
    if "models" in packages:
        parts = packages[: len(packages) - 1 - packages[::-1].index("models")]
    else:
        parts = packages
    directory = "/".join(parts)
    return directory, labels.get(directory) or (parts[-1] if parts else None)


def app_config_label(tree, content):
    """The literal ``label`` of the only AppConfig subclass in an apps.py."""
    found = []
    for node in tree.root_node.named_children:
        if node.type != "class_definition":
            continue
        superclasses = node.child_by_field_name("superclasses")
        if superclasses is None or "AppConfig" not in _text(superclasses, content):
            continue
        values = dict(_assignments(node.child_by_field_name("body"), content))
        found.append(_string(values.get("label"), content))
    return found[0] if len(found) == 1 else None


def resolve_django(models, labels):
    """{model id: (table, rule)} and [(model id, field, table)] m2m tables.

    A model is a class with a django.db.models.Model base, or a base that is
    a model by unique class name in the repository. Abstract models have no
    table; a proxy uses its first concrete base's table. Default names are
    ``{app_label}_{class name lower case}`` and ``{table}_{field}``.
    """
    by_name = {}
    for model in models:
        by_name.setdefault(model.name, []).append(model)

    def parents(model):
        return [
            by_name[b][0]
            for b in model.bases
            if len(by_name.get(b, ())) == 1 and by_name[b][0] is not model
        ]

    is_model = {}

    def model_like(model, seen=()):
        if model.model_id not in is_model:
            if model in seen:
                return False
            is_model[model.model_id] = model.django_base or any(
                model_like(p, (*seen, model)) for p in parents(model)
            )
        return is_model[model.model_id]

    tables, m2m = {}, []

    def table(model, seen=()):
        if model.abstract or not model_like(model) or model in seen:
            return None
        if model.proxy:
            for parent in parents(model):
                found = table(parent, (*seen, model))
                if found:
                    return found
            return None
        if model.db_table:
            return model.db_table.lower(), "explicit"
        label = model.app_label or django_app(model.file, labels)[1]
        if not label:
            return None
        return f"{label}_{model.name.lower()}", "default"

    def inherited_m2m(model, seen=()):
        fields = list(model.m2m)
        for parent in parents(model):
            if parent.abstract and parent not in seen:
                fields += inherited_m2m(parent, (*seen, model))
        return fields

    for model in models:
        found = table(model)
        if found is None:
            continue
        tables[model.model_id] = found
        if model.proxy:
            continue
        for field, db_table, through in inherited_m2m(model):
            if through:
                continue
            m2m.append(
                (
                    model.model_id,
                    field,
                    db_table.lower() if db_table else f"{found[0]}_{field}",
                )
            )
    return tables, m2m


# SQLAlchemy -----------------------------------------------------------------

_SQLALCHEMY = ("sqlalchemy", "flask_sqlalchemy", "sqlmodel")
SQLALCHEMY_READS = {"select"}
SQLALCHEMY_WRITES = {"insert", "update", "delete"}


@dataclass(frozen=True)
class SaMapping:
    file: str
    model_id: str
    name: str
    table: str
    line: int
    column: int
    explicit: bool = True  # False: a default naming strategy's name


@dataclass(frozen=True)
class SaTable:
    file: str
    owner_id: str | None  # the enclosing declaration, else the file
    table: str
    line: int
    column: int


@dataclass(frozen=True)
class SaAccess:
    file: str
    caller_id: str | None
    model: str
    writes: bool
    how: str  # "query", "model_query", "get", "statement" or "instance"
    line: int
    column: int
    model_line: int = 0  # where the model's name is written (byte column)
    model_column: int = 0
    # "instance": the Session method called (add, delete, merge, refresh)
    # and where its name is written; ``model`` is the variable passed.
    method: str = ""
    method_line: int = 0
    method_column: int = 0
    # Column names the chain names by keyword or dict key: (name, writes,
    # line, byte column), for a query or statement on one mapped class.
    keys: tuple = ()


@dataclass(frozen=True)
class SaField:
    """An attribute of a class body that may map a column: ``Column(...)``
    or ``mapped_column(...)`` ("column"), ``x: Mapped[T]`` alone
    ("mapped"), or any other annotated attribute ("annotated", a column
    when the class is a SQLModel table)."""

    name: str
    column_name: str | None  # the name written in the call, else None
    kind: str
    primary: bool
    line: int
    column: int


@dataclass(frozen=True)
class SaClass:
    """A class of a module importing SQLAlchemy, Flask-SQLAlchemy or
    SQLModel, with what its declared bases are written as."""

    file: str
    model_id: str
    name: str
    line: int
    column: int
    # (written name, line, byte column of its last name, imported origin)
    bases: tuple[tuple[str, int, int, str | None], ...]
    table: bool  # SQLModel's ``table=True`` class keyword
    tablename: str | None
    fields: tuple = ()
    init: bool = False  # the class defines __init__


SQLMODEL_BASE = "sqlmodel.SQLModel"
# Session methods taking a mapped instance, and whether they write it.
SESSION_INSTANCE_METHODS = {
    "add": True,
    "delete": True,
    "merge": True,
    "refresh": False,
}
# The scip-python symbols of those methods on SQLAlchemy's sessions.
SESSION_METHOD_SYMBOL = re.compile(
    r"scip-python python \S+ \S+ `sqlalchemy\.(?:orm\.session|orm\.scoping|"
    r"ext\.asyncio\.session|ext\.asyncio\.scoping)`/"
    r"(?:Session|scoped_session|AsyncSession|async_scoped_session)#"
    r"(add|delete|merge|refresh)\(\)\."
)


def _module_aliases(root, content):
    """{local name: module} of a module's top-level ``import x [as y]``."""
    aliases = {}
    for node in root.named_children:
        if node.type != "import_statement":
            continue
        for child in node.children_by_field_name("name"):
            if child.type == "aliased_import":
                module = _text(child.child_by_field_name("name"), content)
                aliases[_text(child.child_by_field_name("alias"), content)] = module
            else:
                module = _text(child, content)
                aliases[module.split(".")[0]] = module.split(".")[0]
    return aliases


# Class-body calls that map something other than a column of the table.
_SA_NOT_COLUMNS = {
    "relationship",
    "Relationship",
    "relation",
    "backref",
    "column_property",
    "synonym",
    "association_proxy",
    "composite",
    "deferred",
    "query_expression",
    "dynamic_loader",
}
_SA_MAPPED = re.compile(r"(?:\w+\.)*Mapped\[")


def _sa_fields(body, content):
    """The SaFields of a class body."""
    found = []
    for statement in body.named_children if body is not None else ():
        if statement.type != "expression_statement" or not statement.named_children:
            continue
        node = statement.named_children[0]
        if node.type != "assignment":
            continue
        left = node.child_by_field_name("left")
        if left is None or left.type != "identifier":
            continue
        name = _text(left, content)
        # Dunder names are SQLAlchemy's own settings; an underscored name is
        # a column like any other (``_password = Column("password")``).
        if (name.startswith("__") and name.endswith("__")) or name == "model_config":
            continue
        annotation = node.child_by_field_name("type")
        written = _text(annotation, content) if annotation is not None else ""
        if "ClassVar" in written:
            continue
        right = node.child_by_field_name("right")
        kind, column_name, primary = None, None, False
        if right is not None and right.type == "call":
            function = right.child_by_field_name("function")
            last = function
            if function is not None and function.type == "attribute":
                last = function.child_by_field_name("attribute")
            called = _text(last, content) if last is not None else ""
            keywords = _keyword_arguments(right, content)
            if called in ("column_property", "deferred"):
                # column_property(Column(...), ...): the attribute maps the
                # column it wraps.
                arguments = right.child_by_field_name("arguments")
                inner = next(
                    (
                        c
                        for c in arguments.named_children
                        if c.type not in ("keyword_argument", "comment")
                    ),
                    None,
                )
                function = (
                    inner.child_by_field_name("function")
                    if (inner is not None and inner.type == "call")
                    else None
                )
                last = function
                if function is not None and function.type == "attribute":
                    last = function.child_by_field_name("attribute")
                if last is None or _text(last, content) not in (
                    "Column",
                    "mapped_column",
                ):
                    continue
                right, called = inner, _text(last, content)
                keywords = _keyword_arguments(right, content)
            if called in ("Column", "mapped_column"):
                kind = "column"
                arguments = right.child_by_field_name("arguments")
                first = next(
                    (
                        c
                        for c in arguments.named_children
                        if c.type not in ("keyword_argument", "comment")
                    ),
                    None,
                )
                column_name = _string(first, content) or _string(
                    keywords.get("name"), content
                )
            elif called in _SA_NOT_COLUMNS or _SA_MAPPED.match(written):
                continue
            elif annotation is not None:
                kind = "annotated"
                column = keywords.get("sa_column")
                if column is not None and column.type == "call":
                    inner = _keyword_arguments(column, content)
                    arguments = column.child_by_field_name("arguments")
                    first = next(
                        (
                            c
                            for c in arguments.named_children
                            if c.type not in ("keyword_argument", "comment")
                        ),
                        None,
                    )
                    column_name = _string(first, content) or _string(
                        inner.get("name"), content
                    )
                    keywords = {**keywords, **inner}
            value = keywords.get("primary_key")
            primary = value is not None and value.type == "true"
        elif annotation is not None:
            kind = "mapped" if _SA_MAPPED.match(written) else "annotated"
        if kind is None or (kind == "annotated" and name.startswith("_")):
            # A SQLModel (Pydantic) private attribute is no field.
            continue
        found.append(
            SaField(
                name,
                column_name,
                kind,
                primary,
                left.start_point.row + 1,
                left.start_point.column,
            )
        )
    return tuple(found)


def _sa_chain_keys(node, writes, how, content):
    """The column names a query or statement chain names after ``node``:
    keywords of filter_by (read); keywords and dict keys of values (written
    by insert and update); string keys of Query.update's dict (written). It
    stops at a join, after which filter_by names another entity."""
    keys = []

    def strings(argument, sets):
        dicts = [argument]
        if argument.type == "list":
            dicts = list(argument.named_children)
        for item in dicts:
            if item.type != "dictionary":
                continue
            for pair in item.named_children:
                key = pair.child_by_field_name("key") if pair.type == "pair" else None
                text = _string(key, content)
                if text:
                    keys.append(
                        (text, sets, key.start_point.row + 1, key.start_point.column)
                    )

    current = node
    while True:
        parent = current.parent
        if (
            parent is None
            or parent.type != "attribute"
            or parent.child_by_field_name("object") != current
        ):
            break
        call = parent.parent
        if (
            call is None
            or call.type != "call"
            or call.child_by_field_name("function") != parent
        ):
            break
        method = _text(parent.child_by_field_name("attribute"), content)
        if method in ("join", "outerjoin", "join_from", "select_from"):
            break
        arguments = call.child_by_field_name("arguments")
        children = arguments.named_children if arguments is not None else ()
        if method in ("filter_by", "values") or (
            method == "update" and how in ("query", "model_query")
        ):
            sets = method != "filter_by" and (writes or method == "update")
            for child in children:
                if child.type == "keyword_argument" and method != "update":
                    name = child.child_by_field_name("name")
                    keys.append(
                        (
                            _text(name, content),
                            sets,
                            name.start_point.row + 1,
                            name.start_point.column,
                        )
                    )
                elif method != "filter_by":
                    strings(child, sets)
        current = call
    return tuple(keys)


def _from_sqlalchemy(origin):
    return origin is not None and any(
        origin == p or origin.startswith(p + ".") for p in _SQLALCHEMY
    )


def extract_sqlalchemy(tree, content, file, definitions):
    """(mappings, tables, accesses, classes) of one Python file that imports
    SQLAlchemy."""
    root = tree.root_node
    imports = _python_imports(root, content)
    aliases = _module_aliases(root, content)
    # Mappings are read only from files that import SQLAlchemy; Session.query
    # calls are read anywhere, and count only for a mapped class's name.
    importing = any(_from_sqlalchemy(o) for o in (*imports.values(), *aliases.values()))
    spans = {
        (d.line, d.column, d.end_line, d.end_column): d
        for d in definitions
        if d.file == file
    }

    def sqlalchemy_name(function):
        """The SQLAlchemy callable a written function names, or None."""
        if function.type == "identifier":
            origin = imports.get(_text(function, content))
            return origin.rpartition(".")[2] if _from_sqlalchemy(origin) else None
        if function.type == "attribute":
            target = function.child_by_field_name("object")
            if target is not None and target.type == "identifier":
                module = aliases.get(_text(target, content))
                if module is not None and _from_sqlalchemy(module):
                    return _text(function.child_by_field_name("attribute"), content)
        return None

    mappings, tables, accesses, classes = [], [], [], []
    for node in root.named_children:
        if node.type == "decorated_definition":
            node = node.child_by_field_name("definition")
        if node is None or node.type != "class_definition":
            continue
        definition = spans.get(
            (
                node.start_point.row + 1,
                node.start_point.column,
                node.end_point.row + 1,
                node.end_point.column,
            )
        )
        values = dict(_assignments(node.child_by_field_name("body"), content))
        name = _string(values.get("__tablename__"), content)
        if importing and definition is not None:
            bases, table = [], False
            superclasses = node.child_by_field_name("superclasses")
            for base in superclasses.named_children if superclasses else ():
                if base.type == "keyword_argument":
                    value = base.child_by_field_name("value")
                    table = table or (
                        _text(base.child_by_field_name("name"), content) == "table"
                        and value is not None
                        and value.type == "true"
                    )
                    continue
                last = base
                if base.type == "attribute":
                    last = base.child_by_field_name("attribute")
                elif base.type != "identifier":
                    continue
                written = _text(base, content)
                bases.append(
                    (
                        written,
                        last.start_point.row + 1,
                        last.start_point.column,
                        imports.get(written),
                    )
                )
            classes.append(
                SaClass(
                    file,
                    definition.id,
                    _text(node.child_by_field_name("name"), content),
                    node.start_point.row + 1,
                    node.start_point.column,
                    tuple(bases),
                    table,
                    name.lower() if name else None,
                    _sa_fields(node.child_by_field_name("body"), content),
                    any(
                        (
                            c.child_by_field_name("definition")
                            if c.type == "decorated_definition"
                            else c
                        ).type
                        == "function_definition"
                        and _text(
                            (
                                c.child_by_field_name("definition")
                                if c.type == "decorated_definition"
                                else c
                            ).child_by_field_name("name"),
                            content,
                        )
                        == "__init__"
                        for c in node.child_by_field_name("body").named_children
                    ),
                )
            )
        if importing and definition is not None and name:
            mappings.append(
                SaMapping(
                    file,
                    definition.id,
                    _text(node.child_by_field_name("name"), content),
                    name.lower(),
                    node.start_point.row + 1,
                    node.start_point.column,
                )
            )

    def access(node, model, writes, how, method=None, keys=False):
        accesses.append(
            SaAccess(
                file,
                _innermost(definitions, file, node),
                _text(model, content),
                writes,
                how,
                node.start_point.row + 1,
                node.start_point.column,
                model.start_point.row + 1,
                model.start_point.column,
                *(
                    (
                        _text(method, content),
                        method.start_point.row + 1,
                        method.start_point.column,
                    )
                    if method is not None
                    else ("", 0, 0)
                ),
                _sa_chain_keys(node, writes, how, content) if keys else (),
            )
        )

    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type == "attribute":
            # Flask-SQLAlchemy's ``Model.query``, other than a call of it.
            target = node.child_by_field_name("object")
            parent = node.parent
            if (
                target is not None
                and target.type == "identifier"
                and _text(node.child_by_field_name("attribute"), content) == "query"
                and not (
                    parent is not None
                    and parent.type == "call"
                    and parent.child_by_field_name("function") == node
                )
            ):
                access(node, target, False, "model_query", keys=True)
            continue
        if node.type != "call":
            continue
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if function is None or arguments is None:
            continue
        positional = [
            c
            for c in arguments.named_children
            if c.type not in ("keyword_argument", "comment")
        ]
        called = sqlalchemy_name(function)
        if called == "Table":
            name = _string(positional[0], content) if positional else None
            if name:
                tables.append(
                    SaTable(
                        file,
                        _innermost(definitions, file, node),
                        name.lower(),
                        node.start_point.row + 1,
                        node.start_point.column,
                    )
                )
            continue
        method = (
            _text(function.child_by_field_name("attribute"), content)
            if function.type == "attribute"
            else None
        )
        if called in SQLALCHEMY_READS | SQLALCHEMY_WRITES:
            how, writes, considered = (
                "statement",
                called in SQLALCHEMY_WRITES,
                positional[:1] if called in SQLALCHEMY_WRITES else positional,
            )
        elif method == "query":
            how, writes, considered = "query", False, positional
        elif method in SESSION_INSTANCE_METHODS:
            # session.add(user): linked only where a SCIP index resolves the
            # method to SQLAlchemy's Session and infers the variable's type.
            if len(positional) == 1 and positional[0].type == "identifier":
                access(
                    node,
                    positional[0],
                    SESSION_INSTANCE_METHODS[method],
                    "instance",
                    function.child_by_field_name("attribute"),
                )
            continue
        elif method in ("get", "select_from"):
            # Session.get(Model, key) and select_from(Model): linked only
            # where a SCIP index resolves the name to a mapped class.
            how, writes, considered = "get", False, positional[:1]
        else:
            continue
        for argument in considered:
            while argument.type == "attribute":
                argument = argument.child_by_field_name("object")
            if argument.type == "identifier":
                # Keys are read for a chain on one class: filter_by names
                # the last entity, so several are ambiguous.
                access(node, argument, writes, how, keys=len(considered) == 1)
    return mappings, tables, accesses, classes


# typing's forms that wrap one type: Annotated[T, ...] and Optional[T].
_TYPING_WRAPPER = re.compile(
    r"scip-python python \S+ \S+ (?:typing|typing_extensions)/(Annotated|Optional)\."
)


def annotated_classes(references, trees, file, line, column, depth=4):
    """The parsed classes a variable referenced at ``line`` and UTF-8 byte
    ``column`` of ``file`` is declared as, read from the annotation where a
    SCIP index defines it: a parameter or annotated assignment. Every name
    in the annotation is resolved through the index (``references``, a
    ``ScipReferences``); ``Annotated[T, ...]``, ``Optional[T]`` and
    ``T | None`` give T, and a name the index resolves to a module-level
    ``Alias = <annotation>`` gives the alias's annotation. ``trees`` maps a
    file to its (content, tree). None when no index covers the file or
    nothing is declared."""
    sites = references.definition_sites(file, line, column)
    if not sites:
        return None
    found = set()
    for site_file, site_line, site_column in sites:
        node = _python_node_at(trees, site_file, site_line, site_column)
        if node is None:
            continue
        parent = node.parent
        annotation = None
        if parent is not None and parent.type in (
            "typed_parameter",
            "typed_default_parameter",
        ):
            annotation = parent.child_by_field_name("type")
        elif (
            parent is not None
            and parent.type == "assignment"
            and parent.child_by_field_name("left") == node
        ):
            annotation = parent.child_by_field_name("type")
            if annotation is None:
                found |= _call_result_classes(
                    references,
                    trees,
                    site_file,
                    parent.child_by_field_name("right"),
                    depth,
                )
        if annotation is not None:
            found |= _annotation_classes(
                references, trees, site_file, annotation, depth
            )
    return tuple(sorted(found, key=lambda d: d.id)) if found else None


# SQLAlchemy's Session.get(Model, key), which returns a Model or None.
_SESSION_GET = re.compile(
    r"scip-python python \S+ \S+ `sqlalchemy\.(?:orm\.session|orm\.scoping|"
    r"ext\.asyncio\.session|ext\.asyncio\.scoping)`/"
    r"(?:Session|scoped_session|AsyncSession|async_scoped_session)#get\(\)\."
)


def _call_result_classes(references, trees, file, node, depth):
    """Parsed classes the value of ``x = f(...)`` is declared as: the return
    annotation of the function the index resolves ``f`` to, or for
    SQLAlchemy's ``Session.get(Model, key)`` (``await`` too) the class
    ``Model`` names."""
    if node is not None and node.type == "await" and node.named_child_count == 1:
        node = node.named_children[0]
    if depth <= 0 or node is None or node.type != "call":
        return set()
    function = node.child_by_field_name("function")
    last = function
    if function is not None and function.type == "attribute":
        last = function.child_by_field_name("attribute")
    if last is None or last.type != "identifier":
        return set()
    line, column = last.start_point.row + 1, last.start_point.column
    called = references.symbols(file, line, column) or ()
    if any(_SESSION_GET.fullmatch(c) for c in called):
        arguments = node.child_by_field_name("arguments")
        positional = [
            c
            for c in (arguments.named_children if arguments else ())
            if c.type not in ("keyword_argument", "comment")
        ]
        if not positional:
            return set()
        return _annotation_classes(references, trees, file, positional[0], depth - 1)
    found = set()
    for site in references.definition_sites(file, line, column) or ():
        target = _python_node_at(trees, *site)
        parent = target.parent if target is not None else None
        if parent is not None and parent.type == "function_definition":
            found |= _annotation_classes(
                references,
                trees,
                site[0],
                parent.child_by_field_name("return_type"),
                depth - 1,
            )
    return found


@dataclass
class DjangoColumns:
    """The model fields a Django query names: ``uses`` are (model key,
    field, writes, line, column); ``complete`` is False where an argument
    is not read here (a variable, ``**kwargs``, an unknown method or
    lookup); ``rows`` is True when the query returns model rows whose
    columns are not all named (no values(), values_list(), only(), count()
    and the like)."""

    uses: list = _field(default_factory=list)
    complete: bool = True
    rows: bool = False


# QuerySet methods by what their arguments name.
_DJANGO_LOOKUPS = frozenset("filter exclude get get_or_create update_or_create".split())
_DJANGO_FIELD_LISTS = frozenset(
    "values values_list only defer order_by distinct select_related earliest "
    "latest".split()
)
_DJANGO_SETS = frozenset("update create".split())
_DJANGO_EXPRESSIONS = frozenset("annotate aggregate alias".split())
_DJANGO_PLAIN = frozenset(
    "all none count exists first last delete iterator reverse using "
    "select_for_update prefetch_related in_bulk bulk_create".split()
)
_DJANGO_NOT_ROWS = frozenset(
    "values values_list only count exists aggregate update delete in_bulk create "
    "bulk_create bulk_update".split()
)
_DJANGO_AGGREGATES = frozenset(
    "F Count Sum Avg Max Min StdDev Variance ArrayAgg StringAgg Coalesce "
    "Lower Upper Length Trunc TruncDate TruncDay ExtractYear Cast".split()
)


def django_chain(node):
    """(method, call node) of the QuerySet methods chained on ``node``
    (a ``Model.objects`` attribute), in order."""
    chain, current = [], node
    while (
        current.parent is not None
        and current.parent.type == "attribute"
        and current.parent.child_by_field_name("object") == current
        and current.parent.parent is not None
        and current.parent.parent.type == "call"
    ):
        name = current.parent.child_by_field_name("attribute")
        current = current.parent.parent
        chain.append((name.text.decode("utf-8", "replace"), current))
    return chain


def django_columns(chain, content, model, models, constructor=None):
    """The ``DjangoColumns`` of a QuerySet ``chain`` (django_chain) on
    ``model``, or of a ``constructor`` call ``Model(field=...)``.
    ``models`` maps a model key to {"fields": {field: (column or None,
    related model key or None)}, "pk": primary key field, "reverse":
    {lookup name: (related model key, its foreign key field)}}. Django
    takes lookups as keyword arguments and field paths as strings, which
    scip-python does not resolve to the model's fields, so they are read
    against the model's own fields."""
    found = DjangoColumns()

    def use(key, field, writes, node):
        column, _ = models.get(key, {}).get("fields", {}).get(field, (None, None))
        if column is not None:
            found.uses.append(
                (key, field, writes, node.start_point.row + 1, node.start_point.column)
            )

    def path(text, node, writes=False):
        key, parts = model, text.split("__")
        for index, part in enumerate(parts):
            entry = models.get(key, {})
            fields = entry.get("fields", {})
            if part == "pk":
                part = entry.get("pk") or part
            if part in fields:
                use(key, part, writes, node)
                related = fields[part][1]
                if writes or related is None or index + 1 == len(parts):
                    return
                key = related
                continue
            if part.endswith("_id") and fields.get(part[:-3], (None, None))[1]:
                use(key, part[:-3], writes, node)
                return
            if part in entry.get("reverse", {}) and not writes:
                key, foreign = entry["reverse"][part]
                use(key, foreign, False, node)
                continue
            if index == 0:
                found.complete = False
            return

    def keywords(arguments, writes=False):
        for argument in arguments.named_children:
            if argument.type == "keyword_argument":
                name = argument.child_by_field_name("name")
                path(name.text.decode("utf-8", "replace"), name, writes)
                expression(argument.child_by_field_name("value"))
            elif argument.type == "dictionary_splat":
                found.complete = False

    def expression(node):
        if node is None:
            return
        if node.type == "call":
            function = node.child_by_field_name("function")
            name = (function.text or b"").decode("utf-8", "replace").rpartition(".")[2]
            arguments = node.child_by_field_name("arguments")
            if arguments is None:
                return
            if name == "Q":
                keywords(arguments)
                for argument in arguments.named_children:
                    if argument.type not in ("keyword_argument", "comment"):
                        expression(argument)
                return
            if name in _DJANGO_AGGREGATES:
                first = next(
                    (a for a in arguments.named_children if a.type != "comment"),
                    None,
                )
                if first is not None and first.type == "string":
                    text = _string(first, content)
                    if text is not None:
                        path(text, first)
                elif first is not None:
                    expression(first)
                for argument in arguments.named_children:
                    if argument.type == "keyword_argument":
                        expression(argument.child_by_field_name("value"))
                return
            if name == "OuterRef":
                # A field of the outer query's model.
                found.complete = False
                return
            return
        if node.type in (
            "binary_operator",
            "boolean_operator",
            "unary_operator",
            "parenthesized_expression",
            "not_operator",
        ):
            for child in node.named_children:
                expression(child)

    def strings(arguments, writes=False):
        for argument in arguments.named_children:
            if argument.type == "string":
                text = _string(argument, content)
                if text is None:
                    found.complete = False
                    continue
                text = text.lstrip("-")
                if text and text != "?":
                    path(text, argument, writes)
            elif argument.type == "keyword_argument":
                continue
            elif argument.type != "comment":
                expression(argument)
                if argument.type != "call":
                    found.complete = False

    if constructor is not None:
        arguments = constructor.child_by_field_name("arguments")
        if arguments is not None:
            keywords(arguments, True)
            if any(
                a.type not in ("keyword_argument", "comment")
                for a in arguments.named_children
            ):
                found.complete = False
        return found
    rows = True
    for method, call in chain:
        arguments = call.child_by_field_name("arguments")
        if arguments is None:
            continue
        if method in _DJANGO_NOT_ROWS:
            rows = False
        if method in _DJANGO_LOOKUPS:
            for argument in arguments.named_children:
                if argument.type == "keyword_argument":
                    name = argument.child_by_field_name("name")
                    text = name.text.decode("utf-8", "replace")
                    value = argument.child_by_field_name("value")
                    if text in ("defaults", "create_defaults"):
                        if value is not None and value.type == "dictionary":
                            for pair in value.named_children:
                                key = pair.child_by_field_name("key")
                                key_text = _string(key, content) if key else None
                                if pair.type != "pair" or key_text is None:
                                    found.complete = False
                                    continue
                                path(key_text, key, True)
                                expression(pair.child_by_field_name("value"))
                        else:
                            found.complete = False
                        continue
                    path(text, name)
                    if method in ("get_or_create", "update_or_create"):
                        # Not found, the lookups are written with the row.
                        path(text, name, True)
                    expression(value)
                elif argument.type == "dictionary_splat":
                    found.complete = False
                elif argument.type != "comment":
                    expression(argument)
                    if argument.type not in (
                        "call",
                        "binary_operator",
                        "unary_operator",
                        "parenthesized_expression",
                    ):
                        found.complete = False
        elif method in _DJANGO_SETS:
            keywords(arguments, True)
        elif method in _DJANGO_FIELD_LISTS:
            strings(arguments)
        elif method in _DJANGO_EXPRESSIONS:
            for argument in arguments.named_children:
                if argument.type == "keyword_argument":
                    expression(argument.child_by_field_name("value"))
                elif argument.type != "comment":
                    expression(argument)
        elif method == "bulk_update":
            positional = [a for a in arguments.named_children if a.type != "comment"]
            if len(positional) > 1 and positional[1].type in ("list", "tuple"):
                for element in positional[1].named_children:
                    text = (
                        _string(element, content) if element.type == "string" else None
                    )
                    if text is None:
                        found.complete = False
                    else:
                        path(text, element, True)
            else:
                found.complete = False
        elif method not in _DJANGO_PLAIN:
            found.complete = False
    found.rows = rows
    return found


def django_historical_model(references, trees, file, line, column):
    """(app label, model name) of a historical model: the name written at
    ``line`` and ``column`` of ``file`` that a scip-python index resolves to
    a variable assigned ``<registry>.get_model("label", "Model")`` or
    ``get_model("label.Model")``, as data migrations take their models from
    the migration state's app registry. None otherwise.

    Why not a tool: the registry is the migration state Django builds at
    run time, which no index types; get_model's arguments name the model."""
    found = set()
    for site in references.definition_sites(file, line, column) or ():
        target = _python_node_at(trees, *site)
        assignment = target.parent if target is not None else None
        if (
            assignment is None
            or assignment.type != "assignment"
            or assignment.child_by_field_name("left") != target
        ):
            return None
        call = assignment.child_by_field_name("right")
        function = call.child_by_field_name("function") if call else None
        if call is None or call.type != "call" or function is None:
            return None
        content = trees[site[0]][0]
        name = (
            function.child_by_field_name("attribute")
            if function.type == "attribute"
            else function
        )
        if name is None or _text(name, content) != "get_model":
            return None
        arguments = call.child_by_field_name("arguments")
        values = [
            _string(c, content)
            for c in (arguments.named_children if arguments else ())
            if c.type != "comment"
        ]
        if len(values) == 1 and values[0] and values[0].count(".") == 1:
            values = values[0].split(".")
        if len(values) != 2 or not all(values):
            return None
        found.add((values[0], values[1]))
    return found.pop() if len(found) == 1 else None


def _python_node_at(trees, file, line, column):
    entry = trees.get(file)
    if entry is None:
        return None
    point = (line - 1, column)
    node = entry[1].root_node.descendant_for_point_range(point, point)
    return node if node is not None and node.type == "identifier" else None


def _annotation_classes(references, trees, file, node, depth):
    """Parsed classes an annotation expression names (see annotated_classes)."""
    if depth <= 0 or node is None:
        return set()
    content = trees[file][0]
    if node.type == "type" and node.named_child_count == 1:
        node = node.named_children[0]
    if node.type in ("identifier", "attribute"):
        last = (
            node if node.type == "identifier" else node.child_by_field_name("attribute")
        )
        line, column = last.start_point.row + 1, last.start_point.column
        declared = references.declarations(file, line, column, _text(last, content))
        classes = {d for d in declared or () if d.kind == "Class"}
        if classes:
            return classes
        # An alias: ``CurrentUser = Annotated[User, Depends(...)]``.
        found = set()
        for site in references.definition_sites(file, line, column) or ():
            target = _python_node_at(trees, *site)
            parent = target.parent if target is not None else None
            if (
                parent is not None
                and parent.type == "assignment"
                and parent.child_by_field_name("left") == target
                and parent.parent is not None
                and parent.parent.type == "expression_statement"
                and parent.parent.parent is not None
                and parent.parent.parent.type == "module"
            ):
                found |= _annotation_classes(
                    references,
                    trees,
                    site[0],
                    parent.child_by_field_name("right"),
                    depth - 1,
                )
        return found
    if node.type == "subscript":
        value = node.child_by_field_name("value")
        last = value
        if value is not None and value.type == "attribute":
            last = value.child_by_field_name("attribute")
        if last is None or last.type != "identifier":
            return set()
        wrapped = any(
            _TYPING_WRAPPER.fullmatch(s)
            for s in references.symbols(
                file, last.start_point.row + 1, last.start_point.column
            )
            or ()
        )
        subscripts = node.children_by_field_name("subscript")
        if not wrapped or not subscripts:
            return set()
        return _annotation_classes(references, trees, file, subscripts[0], depth - 1)
    if (
        node.type == "binary_operator"
        and _text(node.child_by_field_name("operator"), content) == "|"
    ):
        operands = [
            node.child_by_field_name("left"),
            node.child_by_field_name("right"),
        ]
        operands = [o for o in operands if o is not None and o.type != "none"]
        if len(operands) == 1:
            return _annotation_classes(references, trees, file, operands[0], depth - 1)
    return set()


def sqlmodel_default_mappings(classes, references, mappings):
    """SaMappings, by SQLModel's default name (the class name lowercased),
    of the ``table=True`` classes without ``__tablename__`` that inherit
    from ``sqlmodel.SQLModel``. A base is followed to the class a SCIP index
    (``references``, a ``ScipReferences``) resolves it to, or, in a file no
    index covers, to the class of that name in the same file."""
    by_id = {c.model_id: c for c in classes}
    by_name = defaultdict(list)
    for c in classes:
        by_name[(c.file, c.name)].append(c)
    explicit = {m.model_id for m in mappings}
    known = {}

    def derived(c, seen):
        if c.model_id in known:
            return known[c.model_id]
        if c.model_id in seen:
            return False
        seen.add(c.model_id)
        found = False
        for name, line, column, origin in c.bases:
            if origin == SQLMODEL_BASE:
                found = True
                break
            declared = references.declarations(
                c.file, line, column, name.rsplit(".", 1)[-1]
            )
            bases = (
                by_name.get((c.file, name), ())
                if declared is None
                else [by_id[d.id] for d in declared if d.id in by_id]
            )
            if any(derived(b, seen) for b in bases):
                found = True
                break
        known[c.model_id] = found
        return found

    return [
        SaMapping(c.file, c.model_id, c.name, c.name.lower(), c.line, c.column, False)
        for c in classes
        if c.table
        and c.tablename is None
        and c.model_id not in explicit
        and derived(c, set())
    ]


# Modules whose import marks a Python module as one that runs SQL.
PYTHON_SQL_MODULES = (
    "sqlite3",
    "psycopg2",
    "psycopg",
    "pymysql",
    "MySQLdb",
    "mysql.connector",
    "cx_Oracle",
    "oracledb",
    "pyodbc",
    "pg8000",
    "asyncpg",
    "aiosqlite",
    "aiomysql",
    "aiopg",
    "duckdb",
    "databases",
    "sqlalchemy",
    "flask_sqlalchemy",
    "sqlmodel",
    "django.db",
)
# DB-API, asyncpg, SQLAlchemy and Django methods whose first argument is SQL.
PYTHON_SQL_METHODS = {
    "execute",
    "executemany",
    "executescript",
    "exec_driver_sql",
    "fetch",
    "fetchrow",
    "fetchval",
    "raw",
}
SQL_VERBS = {
    "select",
    "with",
    "insert",
    "update",
    "delete",
    "merge",
    "replace",
    "truncate",
}


def _python_sql_text(node, content, imports, aliases):
    """The SQL text of an expression: string literals, implicit
    concatenation, f-strings and ``%``/``format`` templates (what they
    interpolate becomes PLACEHOLDER), and SQLAlchemy ``text(...)``; else None."""
    if node is None:
        return None
    if node.type == "string":
        prefix = _text(node, content).split("'")[0].split('"')[0].lower()
        if "b" in prefix:
            return None
        parts = []
        for child in node.named_children:
            if child.type == "string_content":
                parts.append(_text(child, content))
            elif child.type == "interpolation":
                if "f" not in prefix:
                    return None
                parts.append(PLACEHOLDER)
        return "".join(parts)
    if node.type == "concatenated_string":
        parts = [
            _python_sql_text(c, content, imports, aliases) for c in node.named_children
        ]
        return None if None in parts else "".join(parts)
    if node.type == "parenthesized_expression" and node.named_children:
        return _python_sql_text(node.named_children[0], content, imports, aliases)
    if node.type == "binary_operator":
        operator = node.child_by_field_name("operator")
        left = _python_sql_text(
            node.child_by_field_name("left"), content, imports, aliases
        )
        if left is None or operator is None:
            return None
        if _text(operator, content) == "%":
            return re.sub(
                r"%(?:\([^)]*\))?[-#0 +]*\d*(?:\.\d+)?[sdirf]", PLACEHOLDER, left
            )
        if _text(operator, content) == "+":
            right = _python_sql_text(
                node.child_by_field_name("right"), content, imports, aliases
            )
            return left + (PLACEHOLDER if right is None else right)
        return None
    if node.type == "call":
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        first = (
            next((c for c in arguments.named_children if c.type != "comment"), None)
            if arguments is not None
            else None
        )
        if function is not None and function.type == "attribute":
            name = _text(function.child_by_field_name("attribute"), content)
            owner = function.child_by_field_name("object")
            if name == "format":
                template = _python_sql_text(owner, content, imports, aliases)
                return (
                    None
                    if template is None
                    else re.sub(r"\{[^{}]*\}", PLACEHOLDER, template)
                )
            if name == "text" and owner.type == "identifier":
                module = aliases.get(_text(owner, content)) or imports.get(
                    _text(owner, content)
                )
                if _from_sqlalchemy(module):
                    return _python_sql_text(first, content, imports, aliases)
            return None
        if function is not None and function.type == "identifier":
            if _text(function, content) == "text" and _from_sqlalchemy(
                imports.get("text")
            ):
                return _python_sql_text(first, content, imports, aliases)
    return None


def _imports_any(imports, aliases, modules):
    origins = list(imports.values()) + list(aliases.values())
    return any(o == m or o.startswith(m + ".") for o in origins for m in modules)


def extract_python_sql(tree, content, file, definitions):
    """SQL that one Python module passes to an execute-style call, in a
    module that imports a database driver, SQLAlchemy or ``django.db``."""
    root = tree.root_node
    imports = _python_imports(root, content)
    aliases = _module_aliases(root, content)
    if not _imports_any(imports, aliases, PYTHON_SQL_MODULES):
        return []
    found = []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.named_children))
        if node.type != "call":
            continue
        function = node.child_by_field_name("function")
        if function is None or function.type != "attribute":
            continue
        if _text(function.child_by_field_name("attribute"), content) not in (
            PYTHON_SQL_METHODS
        ):
            continue
        arguments = node.child_by_field_name("arguments")
        first = (
            next((c for c in arguments.named_children if c.type != "comment"), None)
            if arguments is not None
            else None
        )
        if first is None or first.type == "keyword_argument":
            continue
        text = _python_sql_text(first, content, imports, aliases)
        if text is None:
            continue
        words = re.findall(r"[A-Za-z_]+", _COMMENT_OR_STRING.sub(_blank, text))
        if not words or words[0].lower() not in SQL_VERBS:
            continue  # Not a statement that reads or writes tables.
        # Driver parameter markers (%s, %(name)s) never name a table.
        statement = statement_tables(re.sub(r"%(?:\([^)]*\))?s", "?", text))
        if not (statement.reads or statement.writes) and PLACEHOLDER in text:
            statement = None
        found.append(
            SqlUse(
                file,
                _innermost(definitions, file, node),
                "python-sql",
                False,
                statement,
                node.start_point.row + 1,
                node.start_point.column,
            )
        )
    return found


# TypeScript and JavaScript: Prisma and TypeORM -----------------------------

PRISMA_READS = {
    "findUnique",
    "findUniqueOrThrow",
    "findFirst",
    "findFirstOrThrow",
    "findMany",
    "count",
    "aggregate",
    "groupBy",
}
PRISMA_WRITES = {
    "create",
    "createMany",
    "createManyAndReturn",
    "update",
    "updateMany",
    "updateManyAndReturn",
    "upsert",
    "delete",
    "deleteMany",
}
_TYPEORM = ("typeorm", "@nestjs/typeorm")


@dataclass(frozen=True)
class PrismaModel:
    file: str
    model: str
    table: str
    explicit: bool  # named by @@map
    line: int
    # Foreign keys this model holds: (referenced table, columns, referenced
    # columns, onDelete), column names as @map gives them.
    foreign_keys: tuple = ()
    # (field, column) of each scalar field the engine's DMMF lists (it
    # leaves out Unsupported and @ignore fields), column as @map gives it.
    columns: tuple = ()
    # (field, related model, fields of this model holding its foreign key)
    # of each relation field.
    links: tuple = ()
    # (name, fields) of compound unique and primary keys.
    compounds: tuple = ()


@dataclass(frozen=True)
class ModelUse:
    """A Prisma client call or TypeORM repository on a model by name."""

    file: str
    caller_id: str | None
    model: str  # Prisma client accessor, or TypeORM entity class name
    reads: bool
    writes: bool
    line: int
    column: int
    wrapper: bool = False  # TypeORM: through a project's getRepository
    # TypeORM: where getRepository or InjectRepository is written (byte
    # column).
    callee_line: int = 0
    callee_column: int = 0
    # Prisma: the client method, and its call expression.
    method: str = ""
    call: object = _field(default=None, compare=False, repr=False)


def prisma_accessor(model):
    """The Prisma Client property of a model: its name, first letter lower."""
    return model[:1].lower() + model[1:]


def _script_string(node, content):
    """A plain (not template) string literal's text, or None."""
    if node is None or node.type != "string":
        return None
    return "".join(
        _text(c, content)
        for c in node.named_children
        if c.type in ("string_fragment", "escape_sequence")
    )


def _script_imports(root, content):
    """{local name: (module, imported name)} of top-level named imports."""
    found = {}
    for node in root.named_children:
        if node.type != "import_statement":
            continue
        module = _script_string(node.child_by_field_name("source"), content)
        if module is None:
            continue
        for clause in node.named_children:
            if clause.type != "import_clause":
                continue
            for named in clause.named_children:
                if named.type != "named_imports":
                    continue
                for spec in named.named_children:
                    if spec.type != "import_specifier":
                        continue
                    name = _text(spec.child_by_field_name("name"), content)
                    alias = spec.child_by_field_name("alias")
                    local = _text(alias, content) if alias is not None else name
                    found[local] = (module, name)
    return found


def _call_function(call):
    """The callee expression of a call. tree-sitter-typescript reads
    ``await x.f<T>(...)`` as a call of ``await x.f``; its callee is
    ``x.f``."""
    function = call.child_by_field_name("function")
    if (
        function is not None
        and function.type == "await_expression"
        and call.child_by_field_name("type_arguments") is not None
        and function.named_children
    ):
        return function.named_children[0]
    return function


def _call_parts(call, content):
    """(callee name, receiver node or None, arguments) of a call."""
    function = _call_function(call)
    arguments = call.child_by_field_name("arguments")
    args = [
        a
        for a in (arguments.named_children if arguments else ())
        if a.type != "comment"
    ]
    if function is None:
        return None, None, args
    if function.type == "identifier":
        return _text(function, content), None, args
    if function.type == "member_expression":
        prop = function.child_by_field_name("property")
        return _text(prop, content), function.child_by_field_name("object"), args
    return None, None, args


def extract_typeorm(tree, content, file, definitions):
    """(mappings, uses, children) of one TypeScript or JavaScript file:
    classes decorated with TypeORM's ``@Entity``, the entity classes passed
    to ``getRepository`` or ``@InjectRepository``, and ``@ChildEntity``
    classes."""
    root = tree.root_node
    imports = _script_imports(root, content)
    typeorm = {
        local: name
        for local, (module, name) in imports.items()
        # A deep import (``typeorm/decorator/entity/Entity.js``) exports the
        # same decorator.
        if module in _TYPEORM or module.startswith("typeorm/")
    }
    # A class name a getRepository argument may name: an imported binding
    # or a class this file declares, never a local variable or parameter.
    if not typeorm and b"getRepository" not in content:
        return [], [], []
    # Without a typeorm import, only a project's own getRepository wrapper
    # is read, as vendure's ``connection.getRepository(ctx, Product)``.
    bindings = set(imports) | {
        _text(c.child_by_field_name("name"), content)
        for kind in ("class_declaration", "abstract_class_declaration")
        for c in _descendants(root, kind)
        if c.child_by_field_name("name") is not None
    }
    mappings, uses, children = [], [], []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.named_children))
        if node.type in ("class_declaration", "abstract_class_declaration"):
            decorators = [c for c in node.named_children if c.type == "decorator"]
            if node.parent is not None and node.parent.type == "export_statement":
                decorators += [
                    c for c in node.parent.named_children if c.type == "decorator"
                ]
            for decorator in decorators:
                call = decorator.named_children[0] if decorator.named_children else None
                if call is None:
                    continue
                if call.type == "identifier":
                    callee, args = _text(call, content), []
                elif call.type == "call_expression":
                    callee, receiver, args = _call_parts(call, content)
                    if receiver is not None:
                        continue
                else:
                    continue
                if typeorm.get(callee) == "ChildEntity":
                    base = _script_base_class(node, content)
                    model_id = _innermost(
                        definitions, file, node.child_by_field_name("name")
                    )
                    if base is not None and model_id is not None:
                        children.append(
                            TypeormChild(
                                file,
                                model_id,
                                _text(node.child_by_field_name("name"), content),
                                base,
                                node.start_point.row + 1,
                                node.start_point.column,
                            )
                        )
                    break
                if typeorm.get(callee) != "Entity":
                    continue
                name = None
                if args:
                    name = _script_string(args[0], content)
                    if args[0].type == "object":
                        for pair in args[0].named_children:
                            if pair.type == "pair" and _text(
                                pair.child_by_field_name("key"), content
                            ) in ("name", '"name"', "'name'"):
                                name = _script_string(
                                    pair.child_by_field_name("value"), content
                                )
                                if name is None:
                                    name = PLACEHOLDER
                    elif name is None:
                        name = PLACEHOLDER
                if name == PLACEHOLDER or name == "":
                    break  # A table name the file does not fix.
                entity = _text(node.child_by_field_name("name"), content)
                model_id = _innermost(
                    definitions, file, node.child_by_field_name("name")
                )
                if model_id is None:
                    break
                mappings.append(
                    SaMapping(
                        file,
                        model_id,
                        entity,
                        # Without a name, TypeORM's own naming strategy
                        # gives it (deepthought.typeorm_naming).
                        table_key(name) if name else None,
                        node.start_point.row + 1,
                        node.start_point.column,
                        explicit=bool(name),
                    )
                )
                break
        elif node.type == "call_expression":
            callee, receiver, args = _call_parts(node, content)
            injected = callee == "InjectRepository" and typeorm.get(callee) == callee
            if not (injected or callee == "getRepository") or not args:
                continue
            # TypeORM's own getRepository takes the entity alone, and
            # @InjectRepository takes it first.
            if (
                typeorm
                and args[0].type == "identifier"
                and (injected or len(args) == 1)
            ):
                written, wrapper = args[0], False
            else:
                named = [
                    a
                    for a in args
                    if a.type == "identifier" and _text(a, content) in bindings
                ]
                if len(named) != 1 or injected:
                    continue
                written, wrapper = named[0], True
            entity = _text(written, content)
            callee_name = _call_function(node)
            if callee_name is not None and callee_name.type == "member_expression":
                callee_name = callee_name.child_by_field_name("property")
            method = _chained_method(node, content)
            reads = method not in _TYPEORM_WRITES
            writes = method not in _TYPEORM_READS
            uses.append(
                ModelUse(
                    file,
                    _innermost(definitions, file, node),
                    entity,
                    reads,
                    writes,
                    node.start_point.row + 1,
                    node.start_point.column,
                    wrapper,
                    callee_name.start_point.row + 1,
                    callee_name.start_point.column,
                )
            )
    return mappings, uses, children


# scip-typescript symbols of TypeORM's repository and entity manager
# methods: (class, method).
TYPEORM_METHOD_SYMBOL = re.compile(
    r"scip-typescript npm typeorm \S+ .*/"
    r"(Repository|TreeRepository|MongoRepository|EntityManager|MongoEntityManager)"
    r"#(\w+)\(\)\."
)


@dataclass(frozen=True)
class TypeormCall:
    """A method called on a receiver in a TypeScript or JavaScript file:
    ``receiver.method(args)``, a candidate TypeORM repository call."""

    file: str
    caller_id: str | None
    method: str
    line: int  # where the method's name is written (byte column)
    column: int


def extract_typeorm_calls(tree, content, file, definitions):
    """Every ``receiver.method(...)`` call of one file whose method is one
    of TypeORM's that read or write (_TYPEORM_READS, _TYPEORM_WRITES): the
    candidates a scip-typescript index resolves (typeorm_call_entities)."""
    found = []
    for node in _descendants(tree.root_node, "call_expression"):
        function = node.child_by_field_name("function")
        if function is None or function.type != "member_expression":
            continue
        name = function.child_by_field_name("property")
        if name is None:
            continue
        method = _text(name, content)
        if (
            method not in _TYPEORM_READS
            and method not in _TYPEORM_WRITES
            and method != "createQueryBuilder"
        ):
            continue
        found.append(
            TypeormCall(
                file,
                _innermost(definitions, file, node),
                method,
                name.start_point.row + 1,
                name.start_point.column,
            )
        )
    return found


def typeorm_call_entities(references, trees, call, receiver_entities):
    """(status, parsed entity classes) of a candidate call: status is
    ``no_scip_index``, ``not_typeorm`` (the index resolves the method to no
    TypeORM repository or entity manager method), ``entity_unknown`` or
    ``resolved``. For a repository method the entities are
    ``receiver_entities(call)``: the classes the TypeScript checker gives
    as the receiver type's type arguments (``Repository<E>``, through any
    wrapper's generics). For an entity manager method it is the class the
    first argument names, through the index."""
    called = references.symbols(call.file, call.line, call.column)
    if called is None:
        return "no_scip_index", ()
    owners = {
        m.group(1)
        for m in (TYPEORM_METHOD_SYMBOL.fullmatch(c) for c in called)
        if m is not None and m.group(2) == call.method
    }
    if not owners:
        return "not_typeorm", ()
    content, tree = trees[call.file]
    point = (call.line - 1, call.column)
    name = tree.root_node.descendant_for_point_range(point, point)
    member = name.parent if name is not None else None
    if member is None or member.type != "member_expression":
        return "entity_unknown", ()
    found = set()
    if owners & {"EntityManager", "MongoEntityManager"}:
        found |= _typeorm_argument_entity(references, trees, call.file, member.parent)
    if owners & {"Repository", "TreeRepository", "MongoRepository"}:
        found |= set(receiver_entities(call))
    entities = tuple(sorted(found, key=lambda d: d.id))
    return ("resolved" if entities else "entity_unknown"), entities


# scip-typescript symbols of the methods that start a TypeORM query builder,
# and of the query builder's own methods: (class, method).
_TYPEORM_BUILDER_START = re.compile(
    r"scip-typescript npm typeorm \S+ .*/"
    r"(Repository|TreeRepository|MongoRepository|EntityManager|MongoEntityManager"
    r"|DataSource|Connection|QueryRunner)#createQueryBuilder\(\)\."
)
_TYPEORM_BUILDER_METHOD = re.compile(
    r"scip-typescript npm typeorm \S+ query-builder/.*/(\w*QueryBuilder)#(\w+)\(\)\."
)
# Builder methods that make it a write, and those that name its root table.
_TYPEORM_BUILDER_WRITES = frozenset("insert update delete softDelete restore".split())
_TYPEORM_BUILDER_ROOT = frozenset("from into update".split())


@dataclass(frozen=True)
class TypeormBuilder:
    """A TypeORM query builder resolved through the index: its root table as
    entity classes or a literal table name, and whether it writes."""

    entities: tuple
    table: str | None
    writes: bool
    chain: tuple = ()  # (method, call node) of TypeORM's builder methods


def typeorm_builder(references, trees, call, receiver_entities):
    """(status, TypeormBuilder or None) of a ``createQueryBuilder`` call, as
    typeorm_call_entities: the index must resolve the call to TypeORM's
    createQueryBuilder. The root table is the repository's entity (its
    receiver, as for repository calls), the entity argument of an entity
    manager's or data source's builder, else the entity class or literal
    table name the chain's first ``from``, ``into`` or ``update`` names.
    The builder writes when the chain calls TypeORM's ``insert``,
    ``update``, ``delete``, ``softDelete`` or ``restore``,
    every method resolved through the index; otherwise it reads."""
    called = references.symbols(call.file, call.line, call.column)
    if called is None:
        return "no_scip_index", None
    owners = {
        m.group(1)
        for m in (_TYPEORM_BUILDER_START.fullmatch(c) for c in called)
        if m is not None
    }
    if not owners:
        return "not_typeorm", None
    content, tree = trees[call.file]
    point = (call.line - 1, call.column)
    name = tree.root_node.descendant_for_point_range(point, point)
    member = name.parent if name is not None else None
    if member is None or member.type != "member_expression":
        return "entity_unknown", None
    start = member.parent
    # The chain of builder methods called on the builder, in order.
    chain, node = [], start
    while (
        node.parent is not None
        and node.parent.type == "member_expression"
        and node.parent.child_by_field_name("object") == node
        and node.parent.parent is not None
        and node.parent.parent.type == "call_expression"
    ):
        prop = node.parent.child_by_field_name("property")
        node = node.parent.parent
        if prop is None:
            break
        method = _text(prop, content)
        symbols = references.symbols(
            call.file, prop.start_point.row + 1, prop.start_point.column
        )
        if any(
            (m := _TYPEORM_BUILDER_METHOD.fullmatch(c)) and m.group(2) == method
            for c in symbols or ()
        ):
            chain.append((method, node))
    if any(method == "relation" for method, _ in chain):
        # A relation builder writes the related side (its foreign key, or a
        # many-to-many junction table), not the root: left unread
        # (relation_builders).
        return "relation_builders", None
    writes = any(method in _TYPEORM_BUILDER_WRITES for method, _ in chain)
    found = set()
    if owners & {"Repository", "TreeRepository", "MongoRepository"}:
        found |= set(receiver_entities(call))
    if not found and owners & {
        "EntityManager",
        "MongoEntityManager",
        "DataSource",
        "Connection",
    }:
        found |= _typeorm_argument_entity(references, trees, call.file, start)
    table = None
    if not found:
        root = next((c for m, c in chain if m in _TYPEORM_BUILDER_ROOT), None)
        if root is not None:
            found |= _typeorm_argument_entity(references, trees, call.file, root)
            if not found:
                table = _typeorm_literal_table(root, content)
        # A name the chain declares as a common table expression is no
        # table (``addCommonTableExpression(qb, "_variants")``).
        declared = {
            _typeorm_literal_table(c, content, 1)
            for m, c in chain
            if m == "addCommonTableExpression"
        }
        if table in declared:
            table = None
    entities = tuple(sorted(found, key=lambda d: d.id))
    if not entities and table is None:
        return "entity_unknown", None
    return "resolved", TypeormBuilder(entities, table, writes, tuple(chain))


def _typeorm_literal_table(call, content, position=0):
    """The table name a builder method's argument at ``position`` writes as a
    string literal (``from("albums_assets_assets", "a")``), else None."""
    arguments = call.child_by_field_name("arguments")
    given = [
        a
        for a in (arguments.named_children if arguments else ())
        if a.type != "comment"
    ]
    first = given[position] if len(given) > position else None
    if first is None or first.type != "string":
        return None
    return _script_string(first, content)


def _typeorm_argument_entity(references, trees, file, call):
    """Classes the first argument of a call names, through the index."""
    arguments = call.child_by_field_name("arguments") if call is not None else None
    first = next(
        (
            a
            for a in (arguments.named_children if arguments else ())
            if a.type != "comment"
        ),
        None,
    )
    if first is None or first.type != "identifier":
        return set()
    content = trees[file][0]
    declared = references.declarations(
        file, first.start_point.row + 1, first.start_point.column, _text(first, content)
    )
    return {d for d in declared or () if d.kind == "Class"}


@dataclass(frozen=True)
class TypeormChild:
    """A class decorated with TypeORM's ``@ChildEntity``: a single-table
    inheritance child stored in its root entity's table."""

    file: str
    model_id: str
    name: str
    base: str  # the class it extends, by name
    line: int
    column: int


# TypeORM's property decorators that map a property to a column of its
# entity's table, and the relations that hold a foreign key column.
_TYPEORM_COLUMNS = frozenset(
    {
        "Column",
        "PrimaryColumn",
        "PrimaryGeneratedColumn",
        "CreateDateColumn",
        "UpdateDateColumn",
        "DeleteDateColumn",
        "VersionColumn",
        "ObjectIdColumn",
        "ViewColumn",
    }
)
_TYPEORM_PRIMARY = frozenset(
    {"PrimaryColumn", "PrimaryGeneratedColumn", "ObjectIdColumn"}
)
_TYPEORM_OWNING = frozenset({"ManyToOne", "OneToOne"})
_TYPEORM_RELATIONS = frozenset({"ManyToOne", "OneToOne", "OneToMany", "ManyToMany"})


@dataclass(frozen=True)
class TypeormProperty:
    """A property of a class in a file importing typeorm, by its
    decorators: a column (``kind`` "column" or "primary", ``custom`` the
    name its options give), or a relation (``kind`` "relation", ``target``
    the related class's name, ``custom`` and ``referenced`` its
    @JoinColumn's name and referencedColumnName; ``owning`` when it holds a
    foreign key: a ManyToOne, or a OneToOne with a @JoinColumn)."""

    name: str
    kind: str
    custom: str | None
    line: int
    column: int
    target: str | None = None
    owning: bool = False
    referenced: str | None = None


@dataclass(frozen=True)
class TypeormClass:
    file: str
    name: str
    base: str | None
    properties: tuple
    # False where a column's name or an embedded column is not fixed in
    # the file (a name from a variable, @Column(() => Embedded)).
    complete: bool
    line: int


def _typeorm_options(args, content):
    """The object literal of a decorator's options: its first object
    argument (``@Column("varchar", { name })`` or ``@Column({ name })``)."""
    return next((a for a in args if a.type == "object"), None)


def _typeorm_option(options, key, content):
    """(found, value): a string option of a decorator's options object;
    found True with value None when it is written but not a string."""
    if options is None:
        return False, None
    for pair in options.named_children:
        if pair.type != "pair":
            continue
        name = pair.child_by_field_name("key")
        if name is None or _text(name, content).strip("\"'") != key:
            continue
        return True, _script_string(pair.child_by_field_name("value"), content)
    return False, None


def extract_typeorm_classes(tree, content, file):
    """The ``TypeormClass`` of each class of a file importing typeorm that
    has a property decorated with one of its column or relation
    decorators: entities, and the base classes entities extend."""
    root = tree.root_node
    imports = _script_imports(root, content)
    typeorm = {
        local: name
        for local, (module, name) in imports.items()
        if module in _TYPEORM or module.startswith("typeorm/")
    }
    if not typeorm:
        return []
    found = []
    for node in [
        *_descendants(root, "class_declaration"),
        *_descendants(root, "abstract_class_declaration"),
    ]:
        body = node.child_by_field_name("body")
        name = node.child_by_field_name("name")
        if body is None or name is None:
            continue
        properties, complete = [], True
        for member in body.named_children:
            if member.type != "public_field_definition":
                continue
            key = member.child_by_field_name("name")
            if key is None or key.type != "property_identifier":
                continue
            decorators = {}
            for decorator in member.named_children:
                if decorator.type != "decorator" or not decorator.named_children:
                    continue
                call = decorator.named_children[0]
                if call.type == "identifier":
                    callee, args = _text(call, content), []
                elif call.type == "call_expression":
                    callee, receiver, args = _call_parts(call, content)
                    if receiver is not None:
                        continue
                else:
                    continue
                if callee in typeorm:
                    decorators[typeorm[callee]] = args
            columns = _TYPEORM_COLUMNS & set(decorators)
            relations = _TYPEORM_RELATIONS & set(decorators)
            at = (key.start_point.row + 1, key.start_point.column)
            prop = _text(key, content)
            if columns:
                kind = next(iter(columns))
                args = decorators[kind]
                if args and args[0].type in ("arrow_function", "function_expression"):
                    # An embedded entity's columns: not read here.
                    complete = False
                    continue
                written, custom = _typeorm_option(
                    _typeorm_options(args, content), "name", content
                )
                if written and custom is None:
                    complete = False
                    continue
                properties.append(
                    TypeormProperty(
                        prop,
                        "primary" if kind in _TYPEORM_PRIMARY else "column",
                        custom,
                        *at,
                    )
                )
            elif relations:
                kind = next(iter(relations))
                args = decorators[kind]
                target = None
                if args and args[0].type == "arrow_function":
                    body_node = args[0].child_by_field_name("body")
                    if body_node is not None and body_node.type == "identifier":
                        target = _text(body_node, content)
                elif args and args[0].type == "string":
                    target = _script_string(args[0], content)
                join = decorators.get("JoinColumn")
                owning = kind == "ManyToOne" or (
                    kind == "OneToOne" and "JoinColumn" in decorators
                )
                custom = referenced = None
                if join is not None:
                    options = _typeorm_options(join, content)
                    written, custom = _typeorm_option(options, "name", content)
                    if written and custom is None:
                        complete = False
                    written, referenced = _typeorm_option(
                        options, "referencedColumnName", content
                    )
                    if written and referenced is None:
                        complete = False
                    if join and join[0].type == "array":
                        # Several join columns: not read here.
                        complete = False
                        continue
                properties.append(
                    TypeormProperty(
                        prop,
                        "relation",
                        custom,
                        *at,
                        target=target,
                        owning=owning,
                        referenced=referenced,
                    )
                )
        if properties or _script_base_class(node, content):
            found.append(
                TypeormClass(
                    file,
                    _text(name, content),
                    _script_base_class(node, content),
                    tuple(properties),
                    complete,
                    node.start_point.row + 1,
                )
            )
    return found


def _script_base_class(node, content):
    """The name a class declaration extends, when it is a plain name."""
    for heritage in node.named_children:
        if heritage.type != "class_heritage":
            continue
        for clause in heritage.named_children:
            if clause.type != "extends_clause":
                continue
            value = clause.child_by_field_name("value")
            if value is not None and value.type == "identifier":
                return _text(value, content)
    return None


# TypeORM 0.3 Repository methods that only read or only write. Any other
# method, or a repository that is not called directly, reads and writes.
_TYPEORM_READS = frozenset(
    "find findBy findOne findOneBy findOneOrFail findOneByOrFail findAndCount "
    "findAndCountBy findByIds count countBy exist exists existsBy sum average "
    "minimum maximum".split()
)
_TYPEORM_WRITES = frozenset(
    "save insert update upsert delete remove softDelete softRemove restore "
    "recover increment decrement clear".split()
)


# Keys of TypeORM's find options whose objects name columns read, not set.
_TYPEORM_READ_OPTIONS = frozenset(
    "where order select relations relationLoadStrategy loadRelationIds "
    "lock withDeleted cache comment".split()
)
_TYPEORM_SETTERS = frozenset("create save insert upsert merge preload update".split())
_KEY_NODES = frozenset(
    {"property_identifier", "shorthand_property_identifier", "string_fragment"}
)


def typeorm_key_access(
    tree, content, line, column, typeorm_method=None, annotation=None, parameter=None
):
    """Whether the object key or destructured name at 1-based ``line`` and
    0-based ``column`` sets ("write") or reads ("read") the property it
    names, or None when the site is not such a key.

    A key reads in a destructuring pattern, under one of TypeORM's find
    options (``where``, ``order``, ``select``...), in an object a type
    annotation, ``as`` or ``satisfies`` gives one of TypeORM's read option
    types, and in the argument of a project function whose parameter has
    such a type. In the arguments of a TypeORM repository or entity
    manager method, it reads unless the method sets values (``save``,
    ``create``, ``update``'s second argument...). Any other object key
    sets the property of the value it builds.

    The callbacks are the index's: ``typeorm_method(name node)`` the TypeORM
    class (Repository, EntityManager...) a called method is of, else None; ``annotation(type node)`` "read", "write"
    or None for a type TypeORM gives (FindOptions* and Find*Options read,
    DeepPartial and QueryDeepPartialEntity write); ``parameter(name node,
    index)`` the same for the declared type of that parameter of the
    function called."""
    node = tree.root_node.descendant_for_point_range(
        (line - 1, column), (line - 1, column)
    )
    if node is None:
        return None
    if node.type == "shorthand_property_identifier_pattern":
        return "read"
    if node.type == "string_fragment":
        node = node.parent
    if node is None or node.type not in (_KEY_NODES | {"string"}):
        return None
    parent = node.parent
    if node.type == "shorthand_property_identifier" and parent.type == "object":
        current = parent
    elif (
        parent is not None
        and parent.type == "pair"
        and parent.child_by_field_name("key") == node
    ):
        current = parent.parent
    elif parent is not None and parent.type == "pair_pattern":
        return "read"
    else:
        return None

    def typed(type_node):
        if annotation is None or type_node is None:
            return None
        if type_node.type == "type_annotation" and type_node.named_children:
            type_node = type_node.named_children[0]
        if type_node.type == "generic_type":
            type_node = type_node.child_by_field_name("name")
        if type_node is not None and type_node.type == "nested_type_identifier":
            type_node = type_node.named_children[-1]
        if type_node is None or type_node.type != "type_identifier":
            return None
        return annotation(type_node)

    while current is not None:
        up = current.parent
        if up is None:
            break
        if up.type == "pair":
            key = up.child_by_field_name("key")
            name = (
                content[key.start_byte : key.end_byte]
                .decode("utf-8", "replace")
                .strip("'\"`")
            )
            if name in _TYPEORM_READ_OPTIONS:
                return "read"
            current = up.parent
            continue
        if up.type in ("as_expression", "satisfies_expression"):
            kind = typed(up.named_children[-1] if up.named_children else None)
            if kind is not None:
                return kind
            current = up
            continue
        if up.type in ("array", "parenthesized_expression", "spread_element", "object"):
            current = up
            continue
        if up.type == "variable_declarator":
            kind = typed(up.child_by_field_name("type"))
            if kind is not None:
                return kind
            break
        if up.type == "arguments":
            call = up.parent
            function = call.child_by_field_name("function") if call else None
            index = [c for c in up.named_children if c.type != "comment"].index(current)
            name = function
            if function is not None and function.type == "member_expression":
                name = function.child_by_field_name("property")
            if name is None:
                break
            method = content[name.start_byte : name.end_byte].decode("utf-8", "replace")
            owner = "Repository" if typeorm_method is None else typeorm_method(name)
            if owner:
                if owner in ("EntityManager", "MongoEntityManager"):
                    index -= 1  # the entity comes first
                if method == "update" and index == 0:
                    return "read"
                if method in _TYPEORM_SETTERS:
                    return "write"
                if method in _TYPEORM_READS or method in _TYPEORM_WRITES:
                    return "read"
                break
            kind = parameter(name, index) if parameter is not None else None
            if kind is not None:
                return kind
            break
        break
    return "write"


def _chained_method(call, content):
    """The method called on a call's result, as ``save`` in
    ``getRepository(X).save(y)`` (through an ``await``), else None."""
    parent = call.parent
    if parent is None or parent.type != "member_expression":
        return None
    outer = parent.parent
    if outer is None or outer.type != "call_expression":
        return None
    if outer.child_by_field_name("function") != parent:
        return None
    name = parent.child_by_field_name("property")
    return _text(name, content) if name is not None else None


def extract_prisma_client(tree, content, file, definitions, accessors):
    """Prisma Client calls ``<client>.<model>.<method>(...)`` whose model
    property is one of ``accessors`` and whose method reads or writes."""
    found = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.named_children))
        if node.type != "call_expression":
            continue
        method, receiver, _ = _call_parts(node, content)
        if (
            receiver is None
            or receiver.type != "member_expression"
            or method not in PRISMA_READS | PRISMA_WRITES
        ):
            continue
        accessor = _text(receiver.child_by_field_name("property"), content)
        if accessor not in accessors:
            continue
        found.append(
            ModelUse(
                file,
                _innermost(definitions, file, node),
                accessor,
                method in PRISMA_READS,
                method in PRISMA_WRITES,
                node.start_point.row + 1,
                node.start_point.column,
                method=method,
                call=node,
            )
        )
    return found


# The query argument keys Prisma Client takes, by what their values are.
_PRISMA_FILTERS = {"where", "having", "cursor"}
_PRISMA_WRITES = {"data", "create", "update"}
_PRISMA_FIELD_LISTS = {"distinct", "by"}
_PRISMA_AGGREGATES = {"_count", "_avg", "_sum", "_min", "_max"}
_PRISMA_IGNORED = {"take", "skip", "skipDuplicates", "relationLoadStrategy"}
_PRISMA_LOGICAL = {"AND", "OR", "NOT"}
_PRISMA_RELATION_FILTERS = {"some", "every", "none", "is", "isNot"}
# Methods whose result is not rows of the model: a count, or an aggregate.
_PRISMA_NO_ROWS = {
    "count",
    "aggregate",
    "groupBy",
    "createMany",
    "updateMany",
    "deleteMany",
}


@dataclass
class PrismaColumns:
    """The fields a Prisma Client call names: ``uses`` are (model, field,
    writes, line, column); ``complete`` is False where an argument is not an
    object literal, or names a key that is neither Prisma Client's nor a
    field; ``rows`` is True when the call returns rows of the model whose
    fields are not all named (no ``select``, or an ``include``)."""

    uses: list = _field(default_factory=list)
    complete: bool = True
    rows: bool = False


def prisma_columns(call, content, method, model, models):
    """The ``PrismaColumns`` of a Prisma Client call on ``model`` (a
    ``PrismaModel``); ``models`` gives a related model by name. Prisma
    Client types every argument by the model's fields (its WhereInput,
    Select, CreateInput, ...), which scip-typescript records as local
    object-literal keys, not as references; the keys are read against the
    model's fields in Prisma's own DMMF."""
    found = PrismaColumns()
    arguments = _call_parts(call, content)[2]
    if not arguments:
        found.rows = method not in _PRISMA_NO_ROWS
        return found
    _prisma_arguments(arguments[0], content, model, models, found, True, method)
    return found


def _prisma_object(node):
    while node is not None and node.type in (
        "parenthesized_expression",
        "as_expression",
        "satisfies_expression",
    ):
        node = node.named_children[0] if node.named_children else None
    return node


def _prisma_alternatives(node):
    """The values an argument may be: both branches of a conditional, none
    for undefined or null, else the node itself."""
    node = _prisma_object(node)
    if node is None or node.type in ("undefined", "null"):
        return []
    if node.type == "identifier" and node.text == b"undefined":
        return []
    if node.type == "ternary_expression":
        return _prisma_alternatives(
            node.child_by_field_name("consequence")
        ) + _prisma_alternatives(node.child_by_field_name("alternative"))
    if (
        node.type == "binary_expression"
        and node.child_by_field_name("operator") is not None
        and node.child_by_field_name("operator").type == "&&"
    ):
        # ``...(cond && { key })``: the object, or nothing.
        return _prisma_alternatives(node.child_by_field_name("right"))
    return [node]


def _prisma_pairs(node, content, found):
    """(key, line, column, value node) of the properties of an object
    literal (of each, for a conditional), spread object literals included;
    marks ``found`` incomplete for anything else."""
    pairs = []
    for node in _prisma_alternatives(node):
        if node.type != "object":
            found.complete = False
            continue
        for item in node.named_children:
            if item.type == "comment":
                continue
            if item.type == "spread_element":
                spread = item.named_children[0] if item.named_children else None
                pairs.extend(_prisma_pairs(spread, content, found))
                continue
            key = item.child_by_field_name("key") if item.type == "pair" else None
            if item.type == "shorthand_property_identifier":
                key = item
            if key is None or key.type not in (
                "property_identifier",
                "shorthand_property_identifier",
                "string",
            ):
                found.complete = False
                continue
            name = (
                _script_string(key, content)
                if key.type == "string"
                else _text(key, content)
            )
            if name is None:
                found.complete = False
                continue
            # A shorthand property's value is the variable it names, which
            # is not an object literal read here.
            value = item.child_by_field_name("value") if item.type == "pair" else item
            pairs.append((name, key.start_point.row + 1, key.start_point.column, value))
    return pairs


def _prisma_returned(function):
    """The values an arrow or function expression returns: its expression
    body, or its block's return statements (not nested functions')."""
    body = function.child_by_field_name("body")
    if body is None:
        return []
    if body.type != "statement_block":
        return [body]
    found, stack = [], list(body.named_children)
    while stack:
        node = stack.pop()
        if node.type in _FUNCTION_NODES:
            continue
        if node.type == "return_statement":
            found.extend(node.named_children[:1])
            continue
        stack.extend(node.named_children)
    return found


_FUNCTION_NODES = {
    "arrow_function",
    "function_expression",
    "function_declaration",
    "method_definition",
}


def _prisma_items(node, found):
    """The elements of an array literal, the objects a ``.map`` callback
    returns, or the node itself (each, for a conditional)."""
    items = []
    for node in _prisma_alternatives(node):
        if node.type == "array":
            for element in node.named_children:
                if element.type == "comment":
                    continue
                if element.type == "spread_element":
                    found.complete = False
                    continue
                items.append(element)
            continue
        if node.type == "call_expression":
            function = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            callback = (
                arguments.named_children[0]
                if arguments is not None and arguments.named_children
                else None
            )
            if (
                function is not None
                and function.type == "member_expression"
                and (function.child_by_field_name("property").text or b"")
                in (b"map", b"flatMap")
                and callback is not None
                and callback.type in ("arrow_function", "function_expression")
            ):
                items.extend(_prisma_returned(callback))
                continue
        items.append(node)
    return items


@dataclass
class TypeormColumns:
    """The entity properties a TypeORM repository call's arguments name:
    ``uses`` are (entity, property, writes, line, column); ``complete`` is
    False where an argument is not an object literal or names a key that is
    neither one of TypeORM's find options nor a property; ``rows`` is True
    when the call returns rows of the entity without a ``select``."""

    uses: list = _field(default_factory=list)
    complete: bool = True
    rows: bool = False


# Find options that name no column.
_TYPEORM_OPTIONS_IGNORED = frozenset(
    "relations take skip cache withDeleted lock loadEagerRelations "
    "relationLoadStrategy comment transaction loadRelationIds".split()
)
_TYPEORM_OPTION_METHODS = frozenset(
    "find findOne findOneOrFail findAndCount count exist exists".split()
)
_TYPEORM_WHERE_METHODS = frozenset(
    "findBy findOneBy findOneByOrFail findAndCountBy countBy existsBy exist "
    "delete softDelete restore".split()
)
_TYPEORM_ROW_METHODS = frozenset(
    "find findOne findOneOrFail findAndCount findBy findOneBy findOneByOrFail "
    "findAndCountBy".split()
)
_TYPEORM_FIELD_METHODS = frozenset("sum average minimum maximum".split())


def typeorm_columns(call, content, method, entity, entities, manager=False, chain=()):
    """The ``TypeormColumns`` of a TypeORM repository ``call`` (a
    call_expression) of ``method`` on ``entity``; ``entities`` maps an
    entity to {property: (column or None, related entity or None)}, a
    relation's column being its foreign key's when it holds one.
    scip-typescript does not resolve the keys of TypeORM's mapped option
    types (FindOptionsWhere, FindOptionsSelect, FindOptionsOrder) to the
    entity's properties, so they are read against the entity's own
    metadata, by the argument shapes of TypeORM's Repository API; an
    EntityManager call (``manager``) takes the entity first."""
    found = TypeormColumns()
    arguments = call.child_by_field_name("arguments") if call is not None else None
    args = [
        a
        for a in (arguments.named_children if arguments else [])
        if a.type != "comment"
    ]
    if manager:
        args = args[1:]

    def use(ent, name, writes, line, column):
        column_of, _ = entities.get(ent, {}).get(name, (None, None))
        if column_of is not None:
            found.uses.append((ent, name, writes, line, column))

    def where(node, ent):
        for item in _prisma_items(node, found):
            for name, line, column, value in _prisma_pairs(item, content, found):
                fields = entities.get(ent, {})
                if name not in fields:
                    found.complete = False
                    continue
                use(ent, name, False, line, column)
                related = fields[name][1]
                value = _prisma_object(value)
                if related is not None and value is not None and value.type == "object":
                    where(value, related)

    def select(node, ent):
        for item in _prisma_alternatives(node):
            if item.type == "array":
                for element in item.named_children:
                    name = (
                        _script_string(element, content)
                        if element.type == "string"
                        else None
                    )
                    if name is None or name not in entities.get(ent, {}):
                        found.complete = False
                        continue
                    use(
                        ent,
                        name,
                        False,
                        element.start_point.row + 1,
                        element.start_point.column + 1,
                    )
                continue
            for name, line, column, value in _prisma_pairs(item, content, found):
                fields = entities.get(ent, {})
                if name not in fields:
                    found.complete = False
                    continue
                use(ent, name, False, line, column)
                related = fields[name][1]
                value = _prisma_object(value)
                if related is not None:
                    if value is not None and value.type == "object":
                        select(value, related)
                    else:
                        found.complete = False

    def order(node, ent):
        for name, line, column, value in _prisma_pairs(node, content, found):
            fields = entities.get(ent, {})
            if name not in fields:
                found.complete = False
                continue
            use(ent, name, False, line, column)
            related = fields[name][1]
            value = _prisma_object(value)
            if related is not None and value is not None and value.type == "object":
                order(value, related)

    def values(node, ent):
        for item in _prisma_items(node, found):
            item = _prisma_object(item)
            if (
                item is not None
                and item.type == "call_expression"
                and (item.child_by_field_name("function") or item).type
                == "member_expression"
                and item.child_by_field_name("function")
                .child_by_field_name("property")
                .text
                == b"create"
            ):
                inner = item.child_by_field_name("arguments")
                inner = [a for a in inner.named_children if a.type != "comment"]
                if len(inner) == 1:
                    values(inner[0], ent)
                else:
                    found.complete = False
                continue
            for name, line, column, _ in _prisma_pairs(item, content, found):
                if name not in entities.get(ent, {}):
                    found.complete = False
                    continue
                # A relation sets the foreign key it holds; what its object
                # gives the related entity is written only by a cascade.
                use(ent, name, True, line, column)

    def relations(node, ent):
        # Joining a relation reads the foreign key the entity holds; a
        # string array names relations too.
        for item in _prisma_alternatives(node):
            if item.type == "array":
                continue
            for name, line, column, value in _prisma_pairs(item, content, found):
                fields = entities.get(ent, {})
                if name not in fields or fields[name][1] is None:
                    continue
                use(ent, name, False, line, column)
                value = _prisma_object(value)
                if value is not None and value.type == "object":
                    relations(value, fields[name][1])

    def options(node):
        selected = False
        for name, line, column, value in _prisma_pairs(node, content, found):
            if name == "where":
                where(value, entity)
            elif name == "select":
                selected = True
                select(value, entity)
            elif name == "order":
                order(value, entity)
            elif name == "relations":
                relations(value, entity)
            elif name not in _TYPEORM_OPTIONS_IGNORED:
                found.complete = False
        return selected

    def field(node, writes):
        name = _script_string(node, content) if node.type == "string" else None
        if name is None or name not in entities.get(entity, {}):
            found.complete = False
            return
        use(entity, name, writes, node.start_point.row + 1, node.start_point.column + 1)

    selected = False
    if method == "builder":
        # A query builder's values() and set() objects write; an object
        # given to where() and its kin reads; SQL text is not read here.
        for name, node in chain:
            inner = node.child_by_field_name("arguments")
            first = next(
                (
                    a
                    for a in (inner.named_children if inner else ())
                    if a.type != "comment"
                ),
                None,
            )
            if first is None:
                continue
            if name in ("values", "set"):
                values(first, entity)
            elif name in ("where", "andWhere", "orWhere"):
                alternatives = _prisma_alternatives(first)
                if alternatives and all(
                    a.type in ("object", "array") for a in alternatives
                ):
                    where(first, entity)
                else:
                    found.complete = False
        found.rows = False
        return found
    if method in _TYPEORM_OPTION_METHODS and method not in ("exist",):
        if args:
            selected = options(args[0])
    elif method in _TYPEORM_WHERE_METHODS:
        if args:
            where(args[0], entity)
        else:
            found.complete = False
    elif method in _TYPEORM_FIELD_METHODS:
        if args:
            field(args[0], False)
        if len(args) > 1:
            where(args[1], entity)
    elif method in ("increment", "decrement"):
        if len(args) >= 2:
            where(args[0], entity)
            field(args[1], True)
        else:
            found.complete = False
    elif method == "update":
        if len(args) == 2:
            where(args[0], entity)
            values(args[1], entity)
        else:
            found.complete = False
    elif method in ("save", "insert", "upsert"):
        if args:
            values(args[0], entity)
        if method == "upsert" and len(args) > 1:
            paths = _prisma_object(args[1])
            if paths is not None and paths.type == "array":
                for element in paths.named_children:
                    field(element, False)
            else:
                found.complete = False
        elif method != "upsert" and len(args) > 1:
            pass  # SaveOptions / no further argument names a column
    else:
        found.complete = False
    found.rows = method in _TYPEORM_ROW_METHODS and not selected
    return found


def prisma_typed_inputs(tree):
    """(line, column, name, value node, binding) of each object a Prisma
    Client input type is written for outside a call: ``const where:
    Prisma.UserWhereInput = {...}``, what a function declared to return one
    returns, ``{...} satisfies Prisma.UserWhereInput`` and
    ``Prisma.validator<Prisma.UserWhereInput>()({...})``. Line and column
    are the type name's (``UserWhereInput``), for the index to resolve;
    binding is (line, column) of a declared variable's name, else None."""
    found = []

    def named(node):
        if node is None:
            return []
        if node.type == "nested_type_identifier":
            name = node.named_children[-1]
            return [name] if name.type == "type_identifier" else []
        if node.type in (
            "type_annotation",
            "union_type",
            "type_arguments",
            "array_type",
        ):
            return [n for c in node.named_children for n in named(c)]
        return []

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        pairs = []
        binding = None
        if node.type == "variable_declarator":
            pairs = [
                (name, node.child_by_field_name("value"))
                for name in named(node.child_by_field_name("type"))
            ]
            target = node.child_by_field_name("name")
            if target is not None and target.type == "identifier":
                binding = (target.start_point.row + 1, target.start_point.column)
        elif node.type in _FUNCTION_NODES:
            # A function declared to return an input type: what it returns.
            pairs = [
                (name, value)
                for name in named(node.child_by_field_name("return_type"))
                for value in _prisma_returned(node)
            ]
        elif node.type == "satisfies_expression" and len(node.named_children) == 2:
            pairs = [
                (name, node.named_children[0]) for name in named(node.named_children[1])
            ]
        elif node.type == "call_expression":
            inner = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if (
                inner is not None
                and inner.type == "call_expression"
                and arguments is not None
                and arguments.named_children
            ):
                function = inner.child_by_field_name("function")
                if (
                    function is not None
                    and function.type == "member_expression"
                    and function.child_by_field_name("property").text == b"validator"
                ):
                    pairs = [
                        (name, arguments.named_children[0])
                        for name in named(inner.child_by_field_name("type_arguments"))
                    ]
        for name, value in pairs:
            found.append(
                (
                    name.start_point.row + 1,
                    name.start_point.column,
                    name.text.decode(),
                    value,
                    binding,
                )
            )
    return found


def prisma_assignments(tree, content):
    """(variable line, column, key or None, key line, column, value node)
    of each assignment to a variable (``where = {...}``) or to one of its
    properties (``where.name = {...}``)."""
    found = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type != "assignment_expression":
            continue
        left = node.child_by_field_name("left")
        value = node.child_by_field_name("right")
        if left is None or value is None:
            continue
        if left.type == "identifier":
            found.append(
                (left.start_point.row + 1, left.start_point.column, None, 0, 0, value)
            )
        elif left.type == "member_expression":
            target = left.child_by_field_name("object")
            key = left.child_by_field_name("property")
            if (
                target is not None
                and target.type == "identifier"
                and key is not None
                and key.type == "property_identifier"
            ):
                found.append(
                    (
                        target.start_point.row + 1,
                        target.start_point.column,
                        _text(key, content),
                        key.start_point.row + 1,
                        key.start_point.column,
                        value,
                    )
                )
    return found


# What a Prisma Client input type's object holds, by its name's suffix.
PRISMA_INPUT_KINDS = {
    "WhereInput": "where",
    "WhereUniqueInput": "where",
    "Select": "select",
    "Include": "include",
    "OrderByWithRelationInput": "order",
    "CreateInput": "write",
    "UncheckedCreateInput": "write",
    "UpdateInput": "write",
    "UncheckedUpdateInput": "write",
    "CreateManyInput": "write",
    "UpdateManyMutationInput": "write",
    "UncheckedUpdateManyInput": "write",
}


def prisma_input_columns(kind, value, content, model, models, key=None):
    """The ``PrismaColumns`` of an object written for one of Prisma
    Client's input types (``PRISMA_INPUT_KINDS``) of ``model``; with
    ``key`` ((name, line, column)), ``value`` is that one property's."""
    found = PrismaColumns()
    if key is not None:
        pairs = [(*key, value)]
        if kind == "where":
            _prisma_filter(None, content, model, models, found, pairs)
        elif kind == "write":
            _prisma_write(None, content, model, models, found, pairs)
        else:
            found.complete = False
        return found
    for item in _prisma_items(value, found):
        if kind == "where":
            _prisma_filter(item, content, model, models, found)
        elif kind == "select":
            _prisma_select(item, content, model, models, found, True)
        elif kind == "include":
            _prisma_select(item, content, model, models, found, True, True)
        elif kind == "order":
            _prisma_order(item, content, model, models, found)
        else:
            _prisma_write(item, content, model, models, found)
    return found


def _prisma_arguments(node, content, model, models, found, root, method=""):
    """A query argument object (the call's, or a relation's in a select or
    include)."""
    selected = False
    for key, line, column, value in _prisma_pairs(node, content, found):
        if key in _PRISMA_FILTERS:
            _prisma_filter(value, content, model, models, found)
        elif key in _PRISMA_WRITES:
            for item in _prisma_items(value, found):
                _prisma_write(item, content, model, models, found)
        elif key == "select":
            selected = True
            _prisma_select(value, content, model, models, found, root)
        elif key == "include":
            if root:
                found.rows = True
            _prisma_select(value, content, model, models, found, root, True)
        elif key == "omit":
            pass
        elif key == "orderBy":
            for item in _prisma_items(value, found):
                _prisma_order(item, content, model, models, found)
        elif key in _PRISMA_FIELD_LISTS:
            for item in _prisma_items(value, found):
                name = _script_string(item, content) if item is not None else None
                if name is None or not _prisma_field(model, name, found):
                    found.complete = False
                    continue
                found.uses.append(
                    (
                        model.model,
                        name,
                        False,
                        item.start_point.row + 1,
                        item.start_point.column,
                    )
                )
        elif key in _PRISMA_AGGREGATES:
            value = _prisma_object(value)
            if value is not None and value.type == "true":
                continue
            for name, at_line, at_column, _ in _prisma_pairs(value, content, found):
                if name == "_all":
                    continue
                if not _prisma_field(model, name, found):
                    found.complete = False
                    continue
                found.uses.append((model.model, name, False, at_line, at_column))
        elif key not in _PRISMA_IGNORED:
            found.complete = False
    if root and not selected and method not in _PRISMA_NO_ROWS:
        found.rows = True


def _prisma_field(model, name, found):
    return any(field == name for field, _ in model.columns)


def _prisma_link(model, name):
    return next((link for link in model.links if link[0] == name), None)


def _prisma_filter(node, content, model, models, found, pairs=None):
    """A WhereInput: fields, compound keys, AND/OR/NOT and relation filters."""
    if pairs is None:
        pairs = _prisma_pairs(node, content, found)
    for key, line, column, value in pairs:
        if key in _PRISMA_LOGICAL:
            for item in _prisma_items(value, found):
                _prisma_filter(item, content, model, models, found)
        elif _prisma_field(model, key, found):
            found.uses.append((model.model, key, False, line, column))
        elif (compound := dict(model.compounds).get(key)) is not None:
            inner = {
                k: (at_line, at_column)
                for k, at_line, at_column, _ in (
                    _prisma_pairs(value, content, PrismaColumns())
                )
            }
            for name in compound:
                at_line, at_column = inner.get(name, (line, column))
                found.uses.append((model.model, name, False, at_line, at_column))
        elif (link := _prisma_link(model, key)) is not None:
            related = models(link[1])
            value = _prisma_object(value)
            if related is None:
                found.complete = False
            elif value is None or value.type == "null":
                # A to-one relation that is null: its foreign key is read.
                for name in link[2]:
                    found.uses.append((model.model, name, False, line, column))
            else:
                pairs = _prisma_pairs(value, content, PrismaColumns())
                if pairs and all(k in _PRISMA_RELATION_FILTERS for k, *_ in pairs):
                    for _, _, _, inner in pairs:
                        if _prisma_object(inner) is not None and (
                            _prisma_object(inner).type == "null"
                        ):
                            continue
                        _prisma_filter(inner, content, related, models, found)
                else:
                    _prisma_filter(value, content, related, models, found)
        else:
            found.complete = False


def _prisma_select(node, content, model, models, found, root, include=False):
    """A Select or Include: fields read, and relations with their own
    query arguments."""
    for key, line, column, value in _prisma_pairs(node, content, found):
        value_node = _prisma_object(value)
        if _prisma_field(model, key, found) and not include:
            if value_node is not None and value_node.type == "false":
                continue
            found.uses.append((model.model, key, False, line, column))
        elif (link := _prisma_link(model, key)) is not None:
            related = models(link[1])
            if related is None:
                found.complete = False
                continue
            # The relation's own foreign key fields are read to join it.
            for name in link[2]:
                found.uses.append((model.model, name, False, line, column))
            if value_node is not None and value_node.type == "true":
                # Every field of the related rows comes back.
                found.rows = True
            elif value_node is not None and value_node.type != "false":
                inner = PrismaColumns()
                _prisma_arguments(value_node, content, related, models, inner, False)
                found.uses.extend(inner.uses)
                found.complete = found.complete and inner.complete
                selects = {k for k, *_ in _prisma_pairs(value_node, content, inner)}
                if "select" not in selects:
                    found.rows = True
        elif key == "_count":
            continue
        else:
            found.complete = False


def _prisma_order(node, content, model, models, found):
    """An OrderByInput: a field, or a relation's own order."""
    for key, line, column, value in _prisma_pairs(node, content, found):
        if _prisma_field(model, key, found):
            found.uses.append((model.model, key, False, line, column))
        elif (link := _prisma_link(model, key)) is not None:
            related = models(link[1])
            if related is None:
                found.complete = False
                continue
            _prisma_order(value, content, related, models, found)
        elif key not in ("_count", "_relevance"):
            found.complete = False


def _prisma_write(node, content, model, models, found, pairs=None):
    """A CreateInput or UpdateInput: fields written. A relation key writes
    the foreign key fields this model holds, and its nested writes
    (create, connect, update, ...) name the related model's fields."""
    if pairs is None:
        pairs = _prisma_pairs(node, content, found)
    for key, line, column, value in pairs:
        if _prisma_field(model, key, found):
            found.uses.append((model.model, key, True, line, column))
        elif (link := _prisma_link(model, key)) is not None:
            for name in link[2]:
                found.uses.append((model.model, name, True, line, column))
            related = models(link[1])
            if related is None:
                found.complete = False
                continue
            _prisma_nested(value, content, related, models, found)
        else:
            found.complete = False


def _prisma_nested(node, content, model, models, found):
    """A relation's nested writes in a data object, on its related model."""

    def write(value):
        for item in _prisma_items(value, found):
            _prisma_write(item, content, model, models, found)

    def where(value):
        for item in _prisma_items(value, found):
            _prisma_filter(item, content, model, models, found)

    def parts(value, *names):
        """Each element's values of ``names``, as a dict of lists."""
        for item in _prisma_items(value, found):
            pairs = _prisma_pairs(item, content, found)
            if any(k not in names for k, *_ in pairs):
                found.complete = False
            yield {k: v for k, _, _, v in pairs}

    for key, _, _, value in _prisma_pairs(node, content, found):
        flag = _prisma_object(value)
        if flag is not None and flag.type in ("true", "false"):
            continue
        if key == "create":
            write(value)
        elif key == "createMany":
            for item in parts(value, "data", "skipDuplicates"):
                write(item.get("data"))
        elif key == "connectOrCreate":
            for item in parts(value, "where", "create"):
                where(item.get("where"))
                write(item.get("create"))
        elif key in ("connect", "delete", "deleteMany", "disconnect", "set"):
            where(value)
        elif key == "upsert":
            for item in parts(value, "where", "create", "update"):
                if "where" in item:
                    where(item["where"])
                write(item.get("create"))
                write(item.get("update"))
        elif key in ("update", "updateMany"):
            for item in _prisma_items(value, found):
                keys = {k for k, *_ in _prisma_pairs(item, content, PrismaColumns())}
                if "data" in keys and keys <= {"where", "data"}:
                    for k, _, _, v in _prisma_pairs(item, content, found):
                        if k == "where":
                            where(v)
                        else:
                            write(v)
                else:
                    _prisma_write(item, content, model, models, found)
        else:
            found.complete = False


# Go: database/sql, sqlx, pgx and GORM ---------------------------------------

GO_SQL_PACKAGES = (
    "database/sql",
    "github.com/jmoiron/sqlx",
    "github.com/jackc/pgx",
    "github.com/lib/pq",
    "github.com/go-sql-driver/mysql",
    "github.com/mattn/go-sqlite3",
    "modernc.org/sqlite",
    "gorm.io/gorm",
    "github.com/jinzhu/gorm",
    "github.com/uptrace/bun",
)
# Methods whose arguments hold SQL text (database/sql, sqlx, pgx, GORM Raw
# and Exec, bun's NewRaw).
GO_SQL_METHODS = {
    "Query",
    "QueryRow",
    "QueryContext",
    "QueryRowContext",
    "Exec",
    "ExecContext",
    "Prepare",
    "PrepareContext",
    "Queryx",
    "QueryRowx",
    "QueryxContext",
    "QueryRowxContext",
    "Select",
    "SelectContext",
    "Get",
    "GetContext",
    "MustExec",
    "MustExecContext",
    "NamedExec",
    "NamedExecContext",
    "NamedQuery",
    "NamedQueryContext",
    "Preparex",
    "PreparexContext",
    "Raw",
    "NewRaw",
}


def _go_string(node, content):
    """The text of Go string literals joined with ``+`` (other operands are
    PLACEHOLDER), or None when the expression holds no literal."""
    if node is None:
        return None
    if node.type == "raw_string_literal":
        return "".join(
            _text(c, content)
            for c in node.named_children
            if c.type == "raw_string_literal_content"
        )
    if node.type == "interpreted_string_literal":
        return "".join(
            _text(c, content)
            for c in node.named_children
            if c.type in ("interpreted_string_literal_content", "escape_sequence")
        )
    if node.type == "parenthesized_expression" and node.named_children:
        return _go_string(node.named_children[0], content)
    if node.type == "call_expression":
        # sqlx's Rebind(query) only rewrites bind markers.
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if (
            function is not None
            and function.type == "selector_expression"
            and _text(function.child_by_field_name("field"), content) == "Rebind"
            and arguments is not None
            and len(arguments.named_children) == 1
        ):
            return _go_string(arguments.named_children[0], content)
        return None
    if (
        node.type == "binary_expression"
        and _text(node.child_by_field_name("operator"), content) == "+"
    ):
        left = _go_string(node.child_by_field_name("left"), content)
        right = _go_string(node.child_by_field_name("right"), content)
        if left is None and right is None:
            return None
        return (PLACEHOLDER if left is None else left) + (
            PLACEHOLDER if right is None else right
        )
    return None


def _go_imports(root, content):
    paths = []
    for node in root.named_children:
        if node.type != "import_declaration":
            continue
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type == "import_spec":
                path = _go_string(current.child_by_field_name("path"), content)
                if path:
                    paths.append(path)
            stack.extend(current.named_children)
    return paths


@dataclass(frozen=True)
class GoTableName:
    """A ``TableName() string`` method returning a literal (GORM)."""

    file: str
    type_name: str
    table: str
    line: int
    column: int


def extract_go_sql(tree, content, file, definitions):
    """(table names, SQL uses) of one Go file importing a SQL package:
    GORM ``TableName`` methods returning a literal, and SQL text passed to
    database/sql, sqlx, pgx, GORM or bun query methods."""
    root = tree.root_node
    if not any(
        p == g or p.startswith(g + "/")
        for p in _go_imports(root, content)
        for g in GO_SQL_PACKAGES
    ):
        return [], []
    names, uses = [], []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.named_children))
        if node.type == "method_declaration":
            if _text(node.child_by_field_name("name"), content) != "TableName":
                continue
            receiver = node.child_by_field_name("receiver")
            types = [
                c
                for c in (receiver.named_children if receiver else ())
                if c.type == "parameter_declaration"
            ]
            written = types[0].child_by_field_name("type") if types else None
            if written is not None and written.type == "pointer_type":
                written = written.named_children[0] if written.named_children else None
            body = node.child_by_field_name("body")
            statements = [
                s
                for s in (body.named_children if body is not None else ())
                for s in (s.named_children if s.type == "statement_list" else [s])
                if s.type != "comment"
            ]
            if (
                written is None
                or written.type != "type_identifier"
                or len(statements) != 1
                or statements[0].type != "return_statement"
            ):
                continue
            values = [
                v
                for e in statements[0].named_children
                for v in (e.named_children if e.type == "expression_list" else [e])
            ]
            table = _go_string(values[0], content) if len(values) == 1 else None
            if not table or PLACEHOLDER in table:
                continue
            names.append(
                GoTableName(
                    file,
                    _text(written, content),
                    table_key(table.rpartition(".")[2]),
                    node.start_point.row + 1,
                    node.start_point.column,
                )
            )
        elif node.type == "call_expression":
            function = node.child_by_field_name("function")
            if function is None or function.type != "selector_expression":
                continue
            if _text(function.child_by_field_name("field"), content) not in (
                GO_SQL_METHODS
            ):
                continue
            arguments = node.child_by_field_name("arguments")
            text = next(
                (
                    t
                    for a in (arguments.named_children if arguments else ())[:3]
                    for t in [_go_string(a, content)]
                    if t is not None
                ),
                None,
            )
            if text is None:
                continue
            words = re.findall(r"[A-Za-z_]+", _COMMENT_OR_STRING.sub(_blank, text))
            if not words or words[0].lower() not in SQL_VERBS:
                continue
            statement = statement_tables(text)
            if not (statement.reads or statement.writes) and PLACEHOLDER in text:
                statement = None
            uses.append(
                SqlUse(
                    file,
                    _innermost(definitions, file, node),
                    "go-sql",
                    False,
                    statement,
                    node.start_point.row + 1,
                    node.start_point.column,
                )
            )
    return names, uses


GORM_MODULES = ("gorm.io/gorm", "github.com/jinzhu/gorm")


def _sql_package(path):
    return any(path == g or path.startswith(g + "/") for g in GO_SQL_PACKAGES)


def _owner_at(definitions, file, line, column):
    """The innermost declaration of ``file`` holding (line, column)."""
    best = None
    for d in definitions:
        if d.file == file and (d.line, d.column) <= (line, column) <= (
            d.end_line,
            d.end_column,
        ):
            size = (d.end_line - d.line, d.end_column - d.column)
            if best is None or size < best[0]:
                best = (size, d.id)
    return best[1] if best else None


def go_sql_from_facts(calls, definitions):
    """SQL uses of Go calls the type checker resolved (``go_facts``): a
    method of GO_SQL_METHODS declared in a Go SQL package, or returning one
    of its types (an interface such as sqlc's DBTX), passed SQL text that
    gofacts computes: constants, ``+`` joins and the texts a local variable
    may hold there, one per path; the call may run any of them."""
    uses = []
    for call in calls:
        if call.method not in GO_SQL_METHODS or not (
            _sql_package(call.declared) or any(map(_sql_package, call.results))
        ):
            continue
        texts = next((t for t in call.texts[:3] if t is not None), None)
        if texts is None:
            continue
        statements = []
        for text in texts:
            words = re.findall(r"[A-Za-z_]+", _COMMENT_OR_STRING.sub(_blank, text))
            if words and words[0].lower() in SQL_VERBS:
                statements.append((text, statement_tables(text)))
        if not statements:
            continue
        # The tables of every path that reaches the call: it may run any.
        statement = (
            statements[0][1]
            if len(statements) == 1
            else Statement(
                tuple(dict.fromkeys(t for _, s in statements for t in s.reads)),
                tuple(dict.fromkeys(t for _, s in statements for t in s.writes)),
                # The weakest reader of any path.
                next(
                    (s.reader for _, s in statements if s.reader != "sqlglot"),
                    "sqlglot",
                ),
                alternatives=tuple(s for _, s in statements),
            )
        )
        if not (statement.reads or statement.writes) and any(
            PLACEHOLDER in text for text, _ in statements
        ):
            statement = None
        uses.append(
            SqlUse(
                call.file,
                _owner_at(definitions, call.file, call.line, call.column),
                "go-sql-types",
                False,
                statement,
                call.line,
                call.column,
            )
        )
    return uses


# Ruby on Rails: ActiveRecord models and db/schema.rb -------------------------


class RailsInflections:
    """ActiveSupport::Inflector::Inflections (activesupport 8.0): rules
    defined later are tried first, the first that matches replaces once, and
    uncountable words stay as they are. Acronyms change ``underscore``."""

    def __init__(self):
        self.plurals, self.singulars, self.uncountables = [], [], []
        self.acronyms = {}

    def copy(self):
        other = RailsInflections()
        other.plurals = list(self.plurals)
        other.singulars = list(self.singulars)
        other.uncountables = list(self.uncountables)
        other.acronyms = dict(self.acronyms)
        return other

    def plural(self, pattern, replacement, flags=re.IGNORECASE):
        self.plurals.insert(0, (re.compile(pattern, flags), replacement))

    def singular(self, pattern, replacement, flags=re.IGNORECASE):
        self.singulars.insert(0, (re.compile(pattern, flags), replacement))

    def rule(self, kind, string, replacement):
        """``inflect.plural "string", "replacement"`` (or singular): a
        literal rule, which also makes both words countable."""
        self.uncountables = [
            w for w in self.uncountables if w not in (string, replacement)
        ]
        rules = self.plurals if kind == "plural" else self.singulars
        rules.insert(
            0, (re.compile(re.escape(string)), replacement.replace("\\", "\\\\"))
        )

    def uncountable(self, *words):
        self.uncountables.extend(w.lower() for w in words)

    def acronym(self, word):
        self.acronyms[word.lower()] = word

    def irregular(self, singular, plural):
        self.uncountables = [
            w for w in self.uncountables if w not in (singular, plural)
        ]
        s0, srest, p0, prest = singular[0], singular[1:], plural[0], plural[1:]
        e = re.escape
        if s0.upper() == p0.upper():
            self.plural(f"({e(s0)}){e(srest)}$", "\\1" + prest)
            self.plural(f"({e(p0)}){e(prest)}$", "\\1" + prest)
            self.singular(f"({e(s0)}){e(srest)}$", "\\1" + srest)
            self.singular(f"({e(p0)}){e(prest)}$", "\\1" + srest)
        else:
            su, sl, pu, pl = (
                e(s0.upper()),
                e(s0.lower()),
                e(p0.upper()),
                e(p0.lower()),
            )
            self.plural(f"{su}(?i:{e(srest)})$", p0.upper() + prest, 0)
            self.plural(f"{sl}(?i:{e(srest)})$", p0.lower() + prest, 0)
            self.plural(f"{pu}(?i:{e(prest)})$", p0.upper() + prest, 0)
            self.plural(f"{pl}(?i:{e(prest)})$", p0.lower() + prest, 0)
            self.singular(f"{su}(?i:{e(srest)})$", s0.upper() + srest, 0)
            self.singular(f"{sl}(?i:{e(srest)})$", s0.lower() + srest, 0)
            self.singular(f"{pu}(?i:{e(prest)})$", s0.upper() + srest, 0)
            self.singular(f"{pl}(?i:{e(prest)})$", s0.lower() + srest, 0)

    def _apply(self, word, rules):
        if not word or any(
            re.search(rf"\b{re.escape(u)}\Z", word, re.IGNORECASE)
            for u in self.uncountables
        ):
            return word
        for rule, replacement in rules:
            result, count = rule.subn(replacement, word, count=1)
            if count:
                return result
        return word

    def pluralize(self, word):
        return self._apply(word, self.plurals)

    def singularize(self, word):
        return self._apply(word, self.singulars)

    def underscore(self, name):
        """``AdminUser`` is ``admin_user``, ``HTTPLog`` is ``http_log``,
        ``A::B`` is ``a/b``; with the acronym GitHub, ``GitHubUser`` is
        ``github_user``."""
        if not re.search(r"[A-Z-]|::", name):
            return name
        word = name.replace("::", "/")
        if self.acronyms:
            acronym = "|".join(self.acronyms.values())
            word = re.sub(
                rf"(?:(?<=([A-Za-z\d]))|\b)({acronym})(?=\b|[^a-z])",
                lambda m: ("_" if m.group(1) is not None else "") + m.group(2).lower(),
                word,
            )
        word = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])|(?<=[a-z\d])(?=[A-Z])", "_", word)
        return word.replace("-", "_").lower()

    def table_name(self, class_name):
        """ActiveRecord's default table name of a base class: the
        demodulized class name, underscored and pluralized
        (``Admin::LineItem`` is ``line_items``), without prefixes."""
        return self.pluralize(self.underscore(class_name.rpartition("::")[2]))


def _english():
    english = RailsInflections()
    english.plural(r"$", "s", 0)
    english.plural(r"s$", "s")
    english.plural(r"^(ax|test)is$", r"\1es")
    english.plural(r"(octop|vir)us$", r"\1i")
    english.plural(r"(octop|vir)i$", r"\1i")
    english.plural(r"(alias|status)$", r"\1es")
    english.plural(r"(bu)s$", r"\1ses")
    english.plural(r"(buffal|tomat)o$", r"\1oes")
    english.plural(r"([ti])um$", r"\1a")
    english.plural(r"([ti])a$", r"\1a")
    english.plural(r"sis$", "ses")
    english.plural(r"(?:([^f])fe|([lr])f)$", r"\1\2ves")
    english.plural(r"(hive)$", r"\1s")
    english.plural(r"([^aeiouy]|qu)y$", r"\1ies")
    english.plural(r"(x|ch|ss|sh)$", r"\1es")
    english.plural(r"(matr|vert|ind)(?:ix|ex)$", r"\1ices")
    english.plural(r"^(m|l)ouse$", r"\1ice")
    english.plural(r"^(m|l)ice$", r"\1ice")
    english.plural(r"^(ox)$", r"\1en")
    english.plural(r"^(oxen)$", r"\1")
    english.plural(r"(quiz)$", r"\1zes")
    english.singular(r"s$", "")
    english.singular(r"(ss)$", r"\1")
    english.singular(r"(n)ews$", r"\1ews")
    english.singular(r"([ti])a$", r"\1um")
    english.singular(
        r"((a)naly|(b)a|(d)iagno|(p)arenthe|(p)rogno|(s)ynop|(t)he)(sis|ses)$",
        r"\1sis",
    )
    english.singular(r"(^analy)(sis|ses)$", r"\1sis")
    english.singular(r"([^f])ves$", r"\1fe")
    english.singular(r"(hive)s$", r"\1")
    english.singular(r"(tive)s$", r"\1")
    english.singular(r"([lr])ves$", r"\1f")
    english.singular(r"([^aeiouy]|qu)ies$", r"\1y")
    english.singular(r"(s)eries$", r"\1eries")
    english.singular(r"(m)ovies$", r"\1ovie")
    english.singular(r"(x|ch|ss|sh)es$", r"\1")
    english.singular(r"^(m|l)ice$", r"\1ouse")
    english.singular(r"(bus)(es)?$", r"\1")
    english.singular(r"(o)es$", r"\1")
    english.singular(r"(shoe)s$", r"\1")
    english.singular(r"(cris|test)(is|es)$", r"\1is")
    english.singular(r"^(a)x[ie]s$", r"\1xis")
    english.singular(r"(octop|vir)(us|i)$", r"\1us")
    english.singular(r"(alias|status)(es)?$", r"\1")
    english.singular(r"^(ox)en", r"\1")
    english.singular(r"(vert|ind)ices$", r"\1ex")
    english.singular(r"(matr)ices$", r"\1ix")
    english.singular(r"(quiz)zes$", r"\1")
    english.singular(r"(database)s$", r"\1")
    for pair in (
        ("person", "people"),
        ("man", "men"),
        ("child", "children"),
        ("sex", "sexes"),
        ("move", "moves"),
        ("zombie", "zombies"),
    ):
        english.irregular(*pair)
    english.uncountable(
        "equipment",
        "information",
        "rice",
        "money",
        "species",
        "series",
        "fish",
        "sheep",
        "jeans",
        "police",
    )
    return english


# The default English rules of lib/active_support/inflections.rb.
RAILS_ENGLISH = _english()


def rails_pluralize(word):
    """ActiveSupport's ``"word".pluralize`` with the default English rules."""
    return RAILS_ENGLISH.pluralize(word)


def rails_singularize(word):
    """ActiveSupport's ``"word".singularize`` with the default English rules."""
    return RAILS_ENGLISH.singularize(word)


def rails_underscore(name):
    """ActiveSupport's ``underscore`` without acronyms."""
    return RAILS_ENGLISH.underscore(name)


def rails_table_name(class_name):
    """ActiveRecord's default table name with the default English rules."""
    return RAILS_ENGLISH.table_name(class_name)


@dataclass(frozen=True)
class RubyClass:
    """A Ruby class: its constant path, written superclass and the
    ActiveRecord settings of its body."""

    file: str
    class_id: str | None
    path: str  # A::B::C
    superclass: str | None  # as written, e.g. ApplicationRecord, ::Base
    table_name: str | None  # self.table_name = literal
    abstract: bool
    naming: bool  # assigns table_name_prefix, _suffix or pluralize_table_names
    nesting: tuple[str, ...]  # Module.nesting outside the class, outermost first
    line: int
    column: int


@dataclass(frozen=True)
class RubyAccess:
    file: str
    caller_id: str | None
    constant: str  # as written: User, Admin::User
    nesting: tuple[str, ...]  # Module.nesting at the call, outermost first
    reads: bool
    writes: bool
    line: int
    column: int


ACTIVE_RECORD_READS = {
    "all",
    "find",
    "find_by",
    "find_by!",
    "find_by_sql",
    "where",
    "first",
    "first!",
    "last",
    "last!",
    "take",
    "take!",
    "count",
    "exists?",
    "pluck",
    "pick",
    "ids",
    "order",
    "limit",
    "offset",
    "includes",
    "eager_load",
    "preload",
    "joins",
    "left_joins",
    "left_outer_joins",
    "select",
    "sum",
    "average",
    "minimum",
    "maximum",
    "find_each",
    "find_in_batches",
    "in_batches",
    "distinct",
    "group",
    "having",
    "none",
    "count_by_sql",
}
ACTIVE_RECORD_WRITES = {
    "create",
    "create!",
    "insert",
    "insert!",
    "insert_all",
    "insert_all!",
    "upsert",
    "upsert_all",
    "update",
    "update!",
    "update_all",
    "delete",
    "delete_all",
    "delete_by",
    "destroy",
    "destroy!",
    "destroy_all",
    "destroy_by",
    "update_counters",
    "increment_counter",
    "decrement_counter",
}
ACTIVE_RECORD_READ_WRITES = {
    "find_or_create_by",
    "find_or_create_by!",
    "create_or_find_by",
    "create_or_find_by!",
    "find_or_initialize_by",
}


def _ruby_literal(node, content):
    """The text of a plain string (no interpolation) or simple symbol."""
    if node is None:
        return None
    if node.type == "simple_symbol":
        return _text(node, content)[1:]
    if node.type == "string":
        if any(c.type == "interpolation" for c in node.named_children):
            return None
        return "".join(
            _text(c, content) for c in node.named_children if c.type == "string_content"
        )
    return None


def _ruby_path(node, content):
    if node is None:
        return None
    if node.type == "constant":
        return _text(node, content)
    if node.type == "scope_resolution":
        scope = node.child_by_field_name("scope")
        name = _text(node.child_by_field_name("name"), content)
        if scope is None:
            return "::" + name
        head = _ruby_path(scope, content)
        return None if head is None else f"{head}::{name}"
    return None


def extract_ruby_models(tree, content, file, definitions):
    """(classes, table prefixes, accesses, schema tables) of one Ruby file:
    each class with its superclass and ActiveRecord settings, modules and
    classes whose ``self.table_name_prefix`` returns a literal,
    ``Constant.method`` calls of ActiveRecord query and persistence methods,
    and ``create_table`` and Scenic ``create_view`` calls of a file named
    ``*schema.rb`` under ``db/``."""
    classes, prefixes, accesses, tables = [], {}, [], []
    schema = file.startswith("db/") or "/db/" in file
    schema = schema and file.rpartition("/")[2].endswith("schema.rb")

    def walk(node, nesting):
        for child in node.named_children:
            if child.type in ("class", "module"):
                written = _ruby_path(child.child_by_field_name("name"), content)
                if written is None:
                    continue
                if written.startswith("::"):
                    path = written[2:]
                else:
                    path = f"{nesting[-1]}::{written}" if nesting else written
                body = next(
                    (c for c in child.named_children if c.type == "body_statement"),
                    None,
                )
                prefix = _table_name_prefix(body, content)
                if prefix is not None:
                    prefixes[path] = prefix
                if child.type == "module":
                    if body is not None:
                        walk(body, (*nesting, path))
                    continue
                superclass = child.child_by_field_name("superclass")
                table, abstract, naming = None, False, False
                for statement in body.named_children if body is not None else ():
                    if statement.type == "assignment":
                        left = statement.child_by_field_name("left")
                        if (
                            left is not None
                            and left.type == "call"
                            and _text(left.child_by_field_name("receiver"), content)
                            == "self"
                        ):
                            setting = _text(left.child_by_field_name("method"), content)
                            right = statement.child_by_field_name("right")
                            if setting == "table_name":
                                table = _ruby_literal(right, content) or PLACEHOLDER
                            elif setting == "abstract_class":
                                abstract = _text(right, content) == "true"
                            elif (
                                setting == "table_name_prefix"
                                and _ruby_literal(right, content) is not None
                            ):
                                prefixes[path] = _ruby_literal(right, content)
                            elif setting in NAMING_SETTINGS:
                                naming = True
                    elif (
                        statement.type in ("identifier", "call")
                        and _text(statement, content) == "primary_abstract_class"
                    ):
                        abstract = True
                classes.append(
                    RubyClass(
                        file,
                        _innermost(
                            definitions, file, child.child_by_field_name("name")
                        ),
                        path,
                        _ruby_path(superclass.named_children[0], content)
                        if superclass is not None and superclass.named_children
                        else None,
                        table,
                        abstract,
                        naming,
                        nesting,
                        child.start_point.row + 1,
                        child.start_point.column,
                    )
                )
                if body is not None:
                    walk(body, (*nesting, path))
            else:
                visit(child, nesting)
                walk(child, nesting)

    def visit(node, nesting):
        if node.type != "call" or node.child_by_field_name("method") is None:
            return  # ``x.(args)`` calls ``call`` and names no method.
        method = _text(node.child_by_field_name("method"), content)
        receiver = node.child_by_field_name("receiver")
        if schema and receiver is None and method in ("create_table", "create_view"):
            arguments = node.child_by_field_name("arguments")
            first = arguments.named_children[0] if arguments is not None else None
            name = _ruby_literal(first, content)
            if name:
                tables.append(
                    (table_key(name.rpartition(".")[2]), node.start_point.row + 1)
                )
            return
        if receiver is None or receiver.type not in ("constant", "scope_resolution"):
            return
        reads = method in ACTIVE_RECORD_READS | ACTIVE_RECORD_READ_WRITES
        writes = method in ACTIVE_RECORD_WRITES | ACTIVE_RECORD_READ_WRITES
        constant = _ruby_path(receiver, content)
        if (reads or writes) and constant:
            accesses.append(
                RubyAccess(
                    file,
                    _innermost(definitions, file, node),
                    constant,
                    nesting,
                    reads,
                    writes,
                    node.start_point.row + 1,
                    node.start_point.column,
                )
            )

    walk(tree.root_node, ())
    return classes, prefixes, accesses, tables


def _table_name_prefix(body, content):
    for node in body.named_children if body is not None else ():
        if (
            node.type == "singleton_method"
            and _text(node.child_by_field_name("name"), content) == "table_name_prefix"
        ):
            inner = next(
                (c for c in node.named_children if c.type == "body_statement"), None
            )
            values = [
                c
                for c in (inner.named_children if inner else ())
                if c.type != "comment"
            ]
            if len(values) == 1:
                return _ruby_literal(values[0], content)
    return None


NAMING_SETTINGS = {"table_name_prefix", "table_name_suffix", "pluralize_table_names"}


def resolve_active_record(classes, prefixes, overridden=False, inflections=None):
    """({class path: (table, how)}, unresolved paths) of the concrete
    ActiveRecord models, how
    being "explicit" (``self.table_name``), "default" or "inherited" (an STI
    subclass sharing its base class's table), and the models whose name
    the code does not fix: a table_name that is not a literal, or naming
    settings (``table_name_prefix``, ``table_name_suffix``,
    ``pluralize_table_names``) on the model's chain or, with ``overridden``,
    in the application's configuration. A superclass resolves to a
    class of the repository by Ruby's lexical constant lookup
    (``resolve_ruby_constant``); models are the classes whose chain reaches
    ``ActiveRecord::Base``."""
    inflections = inflections or RAILS_ENGLISH
    by_path = {}
    for c in classes:
        by_path.setdefault(c.path, []).append(c)

    namespaces = ruby_namespaces(by_path)

    def parent(c):
        written = c.superclass
        if written is None or written.lstrip(":") == "ActiveRecord::Base":
            return None
        return resolve_ruby_constant(written, c.nesting, by_path, namespaces)

    # A class may be reopened in several files: its declarations agree on
    # the superclass and table name, or the class is left out.
    single = {}
    for p, cs in by_path.items():
        written = {c.superclass for c in cs if c.superclass}
        names = {c.table_name for c in cs if c.table_name}
        if len(written) > 1 or len(names) > 1:
            continue
        first = next((c for c in cs if c.superclass), cs[0])
        single[p] = replace(
            first,
            table_name=next(iter(names), None),
            abstract=any(c.abstract for c in cs),
            naming=any(c.naming for c in cs),
        )
    chain_cache = {}

    def chain(path, seen=()):
        """The model's ancestors up to ActiveRecord::Base, or None."""
        if path in chain_cache:
            return chain_cache[path]
        c = single.get(path)
        result = None
        if c is not None and path not in seen:
            if c.superclass and c.superclass.lstrip(":") == "ActiveRecord::Base":
                result = [path]
            else:
                up = parent(c)
                if up is not None:
                    rest = chain(up, (*seen, path))
                    result = None if rest is None else [path, *rest]
        chain_cache[path] = result
        return result

    def is_model(path):
        return path in single and chain(path) is not None

    def class_prefix(path):
        """A model's table_name_prefix: its own or the nearest
        superclass's (a class attribute, or a ``self.table_name_prefix``
        method), else none."""
        return next((prefixes[a] for a in chain(path) if a in prefixes), "")

    tables, skipped = {}, []
    for path, c in sorted(single.items()):
        ancestors = chain(path)
        if ancestors is None or c.abstract:
            continue
        # ActiveRecord's base_class: the first class up the chain whose
        # superclass is ActiveRecord::Base or abstract.
        base = next(
            a
            for i, a in enumerate(ancestors)
            if i + 1 == len(ancestors) or single[ancestors[i + 1]].abstract
        )
        if c.table_name and c.table_name != PLACEHOLDER:
            tables[path] = (table_key(c.table_name.rpartition(".")[2]), "explicit")
            continue
        if base != path:
            tables[path] = (base, "inherited")
            continue
        if (
            c.table_name == PLACEHOLDER
            or overridden
            or any(single[a].naming for a in ancestors)
        ):
            skipped.append(path)
            continue
        name = inflections.table_name(path)
        # compute_table_name: a class nested in a concrete model is prefixed
        # with that model's singular table name.
        module = path.split("::")[:-1]
        outer = "::".join(module)
        if module and is_model(outer) and not single[outer].abstract:
            tables[path] = ((outer, name), "nested")
            continue
        # full_table_name_prefix: the table_name_prefix of the innermost
        # enclosing module or class that responds to it (a module defining
        # it, or an ActiveRecord class), else the class's own, which a
        # class inherits from its superclasses.
        prefix = None
        while module:
            enclosing = "::".join(module)
            if is_model(enclosing):
                prefix = class_prefix(enclosing)
                break
            if enclosing in prefixes:
                prefix = prefixes[enclosing]
                break
            module.pop()
        if prefix is None:
            prefix = class_prefix(path)
        tables[path] = (table_key(prefix + name), "default")
    # Inherited and nested names refer to other models' tables.
    resolved = {}

    def table_of(path, depth=0):
        if path in resolved or depth > 20:
            return resolved.get(path)
        entry = tables.get(path)
        if entry is None:
            return None
        value, how = entry
        if how == "inherited":
            inner = table_of(value, depth + 1)
            result = None if inner is None else (inner[0], how)
        elif how == "nested":
            outer, name = value
            inner = table_of(outer, depth + 1)
            singular = inflections.singularize(inner[0]) if inner else None
            result = None if inner is None else (f"{singular}_{name}", "default")
        else:
            result = entry
        resolved[path] = result
        return result

    resolved_tables = {p: r for p in sorted(tables) if (r := table_of(p)) is not None}
    return resolved_tables, sorted(set(skipped) | (set(tables) - set(resolved_tables)))


def ruby_namespaces(paths):
    """Every class path and each of its enclosing constant paths."""
    found = set()
    for path in paths:
        parts = path.split("::")
        found.update("::".join(parts[: i + 1]) for i in range(len(parts)))
    return found


def resolve_ruby_constant(written, nesting, paths, namespaces=None):
    """The class path a constant names where it is written, as Ruby's
    lexical lookup finds it among ``paths``: under each scope of
    ``Module.nesting`` from the innermost out, then at the top level.
    ``User`` in ``module Admin`` is ``Admin::User`` when that class exists,
    else ``User``; in ``class Admin::Panel`` (one scope, Admin::Panel) it is
    ``Admin::Panel::User`` or ``User``, never ``Admin::User``; ``::User`` is
    top-level. For ``A::B`` the first segment is looked up and the rest
    follows it. Ancestors of the enclosing classes are not searched."""
    if written.startswith("::"):
        return written[2:] if written[2:] in paths else None
    if namespaces is None:
        namespaces = ruby_namespaces(paths)
    head, _, rest = written.partition("::")
    for cref in (*reversed(nesting), ""):
        start = f"{cref}::{head}" if cref else head
        if start in namespaces:
            candidate = f"{start}::{rest}" if rest else start
            return candidate if candidate in paths else None
    return None


def _descendants(node, kind):
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == kind:
            yield current
        stack.extend(reversed(current.named_children))


def _ruby_words(node, content):
    """The words of literal string, symbol and ``%w()`` arguments, or None
    when one is not literal."""
    words = []
    for argument in node.named_children if node is not None else ():
        if argument.type in ("string_array", "symbol_array", "array"):
            for item in argument.named_children:
                if item.type == "bare_string":
                    words.append(_text(item, content))
                else:
                    value = _ruby_literal(item, content)
                    if value is None:
                        return None
                    words.append(value)
            continue
        value = _ruby_literal(argument, content)
        if value is None:
            return None
        words.append(value)
    return words


def apply_rails_inflections(tree, content, inflections):
    """Applies the English ``ActiveSupport::Inflector.inflections`` blocks
    of one Ruby file to ``inflections``: ``acronym``, ``irregular`` and
    ``uncountable`` with literal words, and ``plural`` and ``singular``
    with a literal string rule (replaced where it first occurs, as Ruby's
    ``sub!`` does). Returns False when a block changes inflection in a way
    that is not read (regular expression rules, ``clear``, words that are
    not literal), True otherwise."""
    supported = True

    def visit(node):
        nonlocal supported
        if (
            node.type == "call"
            and _text(node.child_by_field_name("method"), content) == "inflections"
            and _ruby_path(node.child_by_field_name("receiver"), content)
            in ("ActiveSupport::Inflector", "::ActiveSupport::Inflector")
        ):
            arguments = _ruby_words(node.child_by_field_name("arguments"), content)
            block = node.child_by_field_name("block")
            if block is None or arguments not in ([], ["en"]):
                return
            parameters = block.child_by_field_name("parameters")
            names = [
                _text(c, content)
                for c in (parameters.named_children if parameters else ())
                if c.type == "identifier"
            ]
            if len(names) != 1:
                supported = False
                return
            for call in _descendants(block, "call"):
                if _text(call.child_by_field_name("receiver"), content) != names[0]:
                    continue
                method = _text(call.child_by_field_name("method"), content)
                words = _ruby_words(call.child_by_field_name("arguments"), content)
                if method in ("human",):
                    continue
                if words is None or method not in (
                    "acronym",
                    "irregular",
                    "uncountable",
                    "plural",
                    "singular",
                ):
                    supported = False
                elif method in ("plural", "singular") and len(words) == 2:
                    if "\\" in words[1]:
                        supported = False
                    else:
                        inflections.rule(method, *words)
                elif method == "acronym" and len(words) == 1:
                    inflections.acronym(words[0])
                elif method == "irregular" and len(words) == 2:
                    inflections.irregular(*words)
                elif method == "uncountable":
                    inflections.uncountable(*words)
                else:
                    supported = False
            return
        for child in node.named_children:
            visit(child)

    visit(tree.root_node)
    return supported


# GORM default table names come from GORM's own code (deepthought.gorm_naming).


@dataclass(frozen=True)
class GoType:
    """A Go type as written: its package (the import path of a qualified
    name, None for the file's own package) and name, without pointers,
    slices and arrays."""

    package: str | None
    name: str


@dataclass(frozen=True)
class GormModel:
    file: str
    type_name: str
    how: str  # "embeds" (gorm.Model) or "migrated" (AutoMigrate) or "model"
    line: int
    column: int
    # The import path of a type named from another package (models.User).
    package: str | None = None


@dataclass(frozen=True)
class GormUse:
    file: str
    caller_id: str | None
    model: GoType
    reads: bool
    writes: bool
    line: int
    column: int


GORM_READS = {"First", "Last", "Take", "Find", "FindInBatches", "FirstOrInit"}
GORM_WRITES = {"Create", "CreateInBatches", "Save", "Delete"}
GORM_READ_WRITES = {"FirstOrCreate"}
# Methods after Model(&T{}) in a chain that read or write T's table.
GORM_CHAIN_READS = GORM_READS | {"Count", "Pluck", "Scan", "Row", "Rows"}
GORM_CHAIN_WRITES = GORM_WRITES | {
    "Update",
    "Updates",
    "UpdateColumn",
    "UpdateColumns",
}


def _go_import_names(root, content):
    """{name used in the file: import path} of the file's imports."""
    names = {}
    for spec in _descendants(root, "import_spec"):
        if spec.parent is None or not any(
            a.type == "import_declaration" for a in _ancestors(spec)
        ):
            continue
        path = _go_string(spec.child_by_field_name("path"), content)
        if not path:
            continue
        alias = spec.child_by_field_name("name")
        if alias is not None:
            name = _text(alias, content)
            if name in ("_", "."):
                continue
        else:
            name = path.rstrip("/").rpartition("/")[2]
            # gopkg.in/yaml.v3 and example.com/x/v2 are used by their name.
            if re.fullmatch(r"v\d+", name) and "/" in path:
                name = path.rstrip("/").rsplit("/", 2)[-2]
            name = re.sub(r"\.v\d+$", "", name)
        names[name] = path
    return names


def _ancestors(node):
    node = node.parent
    while node is not None:
        yield node
        node = node.parent


def _go_type(node, content, imports):
    while node is not None and node.type in (
        "pointer_type",
        "slice_type",
        "array_type",
        "parenthesized_type",
    ):
        node = node.child_by_field_name("element") or (
            node.named_children[-1] if node.named_children else None
        )
    if node is None:
        return None
    if node.type == "type_identifier":
        return GoType(None, _text(node, content))
    if node.type == "qualified_type":
        package = imports.get(_text(node.child_by_field_name("package"), content))
        name = node.child_by_field_name("name")
        if package is None or name is None:
            return None
        return GoType(package, _text(name, content))
    return None


def _go_value_type(node, content, imports, variables):
    """The type of ``&T{}``, ``T{}``, ``[]T{}``, ``new(T)``,
    ``make([]T, n)`` or a variable of the function declared with one."""
    if node is None:
        return None
    if node.type == "unary_expression" and _text(
        node.child_by_field_name("operator"), content
    ) in ("&", "*"):
        return _go_value_type(
            node.child_by_field_name("operand"), content, imports, variables
        )
    if node.type == "parenthesized_expression" and node.named_children:
        return _go_value_type(node.named_children[0], content, imports, variables)
    if node.type == "composite_literal":
        return _go_type(node.child_by_field_name("type"), content, imports)
    if node.type == "call_expression":
        function = _text(node.child_by_field_name("function"), content)
        arguments = node.child_by_field_name("arguments")
        first = (
            arguments.named_children[0]
            if arguments and arguments.named_children
            else None
        )
        if function in ("new", "make") and first is not None:
            return _go_type(first, content, imports)
        return None
    if node.type == "identifier":
        return variables.get(_text(node, content))
    return None


def _go_variables(function, content, imports):
    """{name: type} of a function's parameters and of the locals it
    declares with a type or a typed value; a name declared twice with
    different types gives none."""
    found = {}

    def add(name, kind):
        if kind is None:
            return
        if name in found and found[name] != kind:
            found[name] = None
        else:
            found.setdefault(name, kind)

    for node in _descendants(function, "parameter_declaration"):
        kind = _go_type(node.child_by_field_name("type"), content, imports)
        for child in node.named_children:
            if child.type == "identifier":
                add(_text(child, content), kind)
    for node in _descendants(function, "var_spec"):
        written = node.child_by_field_name("type")
        values = node.child_by_field_name("value")
        names = [c for c in node.named_children if c.type == "identifier"]
        if written is not None:
            kind = _go_type(written, content, imports)
            for name in names:
                add(_text(name, content), kind)
        elif values is not None and len(values.named_children) == len(names):
            for name, value in zip(names, values.named_children):
                add(_text(name, content), _go_value_type(value, content, imports, {}))
    for node in _descendants(function, "short_var_declaration"):
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None:
            continue
        if len(left.named_children) == len(right.named_children):
            for name, value in zip(left.named_children, right.named_children):
                if name.type == "identifier":
                    add(
                        _text(name, content),
                        _go_value_type(value, content, imports, {}),
                    )
    return {k: v for k, v in found.items() if v is not None}


# NamingStrategy settings that change GORM's column names (ColumnName's
# toDBName): table settings (SingularTable, TablePrefix) do not.
GORM_COLUMN_NAMING = {"NoLowerCase", "NameReplacer"}


def gorm_column_naming(tree, content):
    """Whether a file configures a NamingStrategy that may rename columns:
    one that is not a ``schema.NamingStrategy{...}`` literal, or one setting
    NoLowerCase or NameReplacer."""
    for element in _descendants(tree.root_node, "keyed_element"):
        children = element.named_children
        if len(children) < 2 or _text(children[0], content) != "NamingStrategy":
            continue
        value = children[1]
        if value.type == "literal_element" and value.named_children:
            value = value.named_children[0]
        written = (
            value.child_by_field_name("type")
            if value.type == "composite_literal"
            else None
        )
        if written is None or _text(written, content).rpartition(".")[2] != (
            "NamingStrategy"
        ):
            return True
        body = value.child_by_field_name("body")
        for keyed in _descendants(body, "keyed_element") if body else ():
            if keyed.named_children and _text(keyed.named_children[0], content) in (
                GORM_COLUMN_NAMING
            ):
                return True
    return False


def extract_gorm(tree, content, file, definitions):
    """(models, uses, naming) of one Go file. In a file importing
    gorm.io/gorm: structs that embed ``gorm.Model``, types passed to
    ``AutoMigrate`` or ``Model``, and whether the file configures a
    ``NamingStrategy``. In any file: the GORM-named calls whose model
    argument has a known type (the caller keeps those of GORM models)."""
    root = tree.root_node
    imports = _go_import_names(root, content)
    gorm = {n for n, p in imports.items() if p == "gorm.io/gorm"}
    models, uses = [], []
    naming = any(
        _text(k, content) == "NamingStrategy"
        for k in _descendants(root, "keyed_element")
        for k in k.named_children[:1]
    )
    for spec in _descendants(root, "type_spec"):
        body = spec.child_by_field_name("type")
        if body is None or body.type != "struct_type":
            continue
        for field in _descendants(body, "field_declaration"):
            if field.parent is None or field.parent.parent != body:
                continue
            written = field.child_by_field_name("type")
            if (
                field.child_by_field_name("name") is None
                and written is not None
                and written.type == "qualified_type"
                and _text(written.child_by_field_name("package"), content) in gorm
                and _text(written.child_by_field_name("name"), content) == "Model"
            ):
                name = spec.child_by_field_name("name")
                models.append(
                    GormModel(
                        file,
                        _text(name, content),
                        "embeds",
                        name.start_point.row + 1,
                        name.start_point.column,
                    )
                )
    scopes = {}

    def variables(node):
        function = next(
            (
                a
                for a in _ancestors(node)
                if a.type
                in ("function_declaration", "method_declaration", "func_literal")
            ),
            None,
        )
        if function is None:
            return {}
        if function.id not in scopes:
            # A closure sees its enclosing functions' variables too.
            outer = variables(function)
            scopes[function.id] = {**outer, **_go_variables(function, content, imports)}
        return scopes[function.id]

    for call in _descendants(root, "call_expression"):
        function = call.child_by_field_name("function")
        if function is None or function.type != "selector_expression":
            continue
        method = _text(function.child_by_field_name("field"), content)
        arguments = call.child_by_field_name("arguments")
        values = arguments.named_children if arguments is not None else []
        if method == "AutoMigrate" and gorm:
            for value in values:
                kind = _go_value_type(value, content, imports, {})
                if kind is not None:
                    models.append(
                        GormModel(
                            file,
                            kind.name,
                            "migrated",
                            value.start_point.row + 1,
                            value.start_point.column,
                            kind.package,
                        )
                    )
            continue
        if method == "Model":
            kind = (
                _go_value_type(values[0], content, imports, variables(call))
                if values
                else None
            )
            if kind is None:
                continue
            chain = []
            node = call
            while (
                node.parent is not None
                and node.parent.type == "selector_expression"
                and node.parent.parent is not None
                and node.parent.parent.type == "call_expression"
            ):
                link = _text(node.parent.child_by_field_name("field"), content)
                if link == "Association":
                    break
                chain.append(link)
                node = node.parent.parent
            reads = any(m in GORM_CHAIN_READS | GORM_READ_WRITES for m in chain)
            writes = any(m in GORM_CHAIN_WRITES | GORM_READ_WRITES for m in chain)
            if gorm:
                models.append(
                    GormModel(
                        file,
                        kind.name,
                        "model",
                        call.start_point.row + 1,
                        call.start_point.column,
                        kind.package,
                    )
                )
        elif method in GORM_READS | GORM_WRITES | GORM_READ_WRITES:
            kind = (
                _go_value_type(values[0], content, imports, variables(call))
                if values
                else None
            )
            if kind is None:
                continue
            reads = method in GORM_READS | GORM_READ_WRITES
            writes = method in GORM_WRITES | GORM_READ_WRITES
        else:
            continue
        if reads or writes:
            uses.append(
                GormUse(
                    file,
                    _innermost(definitions, file, call),
                    kind,
                    reads,
                    writes,
                    call.start_point.row + 1,
                    call.start_point.column,
                )
            )
    return models, uses, naming


# Drizzle ORM (TypeScript and JavaScript) -------------------------------------

DRIZZLE_CORES = {
    "drizzle-orm/pg-core": ("pgTable", "pgTableCreator", "pgSchema"),
    "drizzle-orm/mysql-core": ("mysqlTable", "mysqlTableCreator", "mysqlSchema"),
    "drizzle-orm/sqlite-core": ("sqliteTable", "sqliteTableCreator", None),
    "drizzle-orm/singlestore-core": (
        "singlestoreTable",
        "singlestoreTableCreator",
        "singlestoreSchema",
    ),
}
DRIZZLE_READS = {"from", "innerJoin", "leftJoin", "rightJoin", "fullJoin", "crossJoin"}
DRIZZLE_WRITES = {"insert", "update", "delete"}
DRIZZLE_QUERY_READS = {"findMany", "findFirst"}


@dataclass(frozen=True)
class DrizzleTable:
    """A table a Drizzle constructor declares and the variable that holds
    it; ``table`` is PLACEHOLDER when the name is not fixed."""

    file: str
    variable: str
    table: str
    exported: bool
    line: int
    column: int
    # Each column property: (key, column name as written or None, line,
    # column of the key, (line, column) of the builder function's name when
    # the name is not written at the property); complete when every property
    # is one.
    columns: tuple = ()
    columns_complete: bool = False
    # (line, column) of each spread object's (or function's) name.
    spreads: tuple = ()


@dataclass(frozen=True)
class DrizzleRef:
    """How a use names a table: a variable of the file ("local"), an import
    (``module`` and the exported ``name``), a property of an imported object
    (``schema.users``, "member") or a relational query key
    (``db.query.<name>``, "query")."""

    how: str  # "local", "import", "member" or "query"
    name: str
    module: str | None


@dataclass(frozen=True)
class DrizzleUse:
    file: str
    caller_id: str | None
    ref: DrizzleRef
    reads: bool
    writes: bool
    line: int
    column: int
    # Where the table's name is written: the argument, the property of
    # ``schema.users`` or the query key of ``db.query.users``.
    ref_line: int = 0
    ref_column: int = 0
    # The column keys the call's objects name: (key, line, column) of each
    # key of ``values``/``set`` objects (writes) or of a relational query's
    # ``columns`` (reads); keys_complete when every column it names is one
    # (no spread, no object built elsewhere).
    keys: tuple[tuple[str, int, int], ...] = ()
    keys_complete: bool = False


@dataclass(frozen=True)
class ScriptModule:
    """A file's module links: named imports {local: (module, name)},
    namespace imports {local: module}, ``export * from`` modules and named
    re-exports {exported: (module, name)}."""

    imports: dict
    namespaces: dict
    star_exports: tuple
    re_exports: dict


def script_module(root, content):
    imports, namespaces, stars, re_exports = {}, {}, [], {}
    for node in root.named_children:
        source = _script_string(node.child_by_field_name("source"), content)
        if source is None:
            continue
        if node.type == "import_statement":
            for clause in node.named_children:
                if clause.type != "import_clause":
                    continue
                for part in clause.named_children:
                    if part.type == "namespace_import":
                        names = [
                            c for c in part.named_children if c.type == "identifier"
                        ]
                        if names:
                            namespaces[_text(names[0], content)] = source
                    elif part.type == "named_imports":
                        for spec in part.named_children:
                            if spec.type != "import_specifier":
                                continue
                            name = _text(spec.child_by_field_name("name"), content)
                            alias = spec.child_by_field_name("alias")
                            local = _text(alias, content) if alias is not None else name
                            imports[local] = (source, name)
        elif node.type == "export_statement":
            clause = next(
                (c for c in node.named_children if c.type == "export_clause"), None
            )
            if clause is None:
                if not any(c.type == "namespace_export" for c in node.named_children):
                    stars.append(source)
                continue
            for spec in clause.named_children:
                if spec.type != "export_specifier":
                    continue
                name = _text(spec.child_by_field_name("name"), content)
                alias = spec.child_by_field_name("alias")
                re_exports[_text(alias, content) if alias is not None else name] = (
                    source,
                    name,
                )
    return ScriptModule(imports, namespaces, tuple(stars), re_exports)


def _drizzle_constructors(module, content, root):
    """{local callee: ("table", None) | ("creator", None) | ("schema", None)}
    for the Drizzle core functions the file imports."""
    found = {}
    for local, (source, name) in module.imports.items():
        names = DRIZZLE_CORES.get(source)
        if names is None:
            continue
        for kind, expected in zip(("table", "creator", "schema"), names):
            if expected is not None and name == expected:
                found[local] = kind
    return found


def _drizzle_creator(node, content):
    """The (prefix, suffix) of ``pgTableCreator((name) => `p_${name}`)``,
    or None when the arrow function does more than wrap its parameter."""
    if node is None or node.type != "arrow_function":
        return None
    parameter = node.child_by_field_name("parameter")
    if parameter is None:
        parameters = node.child_by_field_name("parameters")
        names = [
            c
            for c in (parameters.named_children if parameters else ())
            if c.type in ("identifier", "required_parameter")
        ]
        if len(names) != 1:
            return None
        parameter = names[0]
        if parameter.type == "required_parameter":
            parameter = parameter.child_by_field_name("pattern")
    name = _text(parameter, content)
    body = node.child_by_field_name("body")
    if body is None or body.type != "template_string":
        return None
    parts, seen = [], 0
    for child in body.named_children:
        if child.type == "string_fragment":
            parts.append(_text(child, content))
        elif child.type == "template_substitution":
            inner = child.named_children
            if len(inner) != 1 or _text(inner[0], content) != name or seen:
                return None
            seen += 1
            parts.append(None)
        else:
            return None
    if seen != 1:
        return None
    index = parts.index(None)
    return "".join(parts[:index]), "".join(parts[index + 1 :])


def extract_drizzle(tree, content, file, definitions):
    """(tables, uses) of one TypeScript or JavaScript file: variables
    holding a Drizzle table (``pgTable("name", ...)``, a schema's
    ``.table``, a ``*TableCreator`` that wraps the name in fixed text), and
    query-builder calls (``from``, joins, ``insert``, ``update``,
    ``delete``) or relational queries (``db.query.x.findMany``) naming a
    table by a variable."""
    root = tree.root_node
    module = script_module(root, content)
    constructors = _drizzle_constructors(module, content, root)
    tables, uses = [], []
    creators, schemas = {}, {}
    declarators = list(_descendants(root, "variable_declarator"))
    for declarator in declarators:
        value = declarator.child_by_field_name("value")
        name_node = declarator.child_by_field_name("name")
        if value is None or name_node is None or value.type != "call_expression":
            continue
        callee, receiver, args = _call_parts(value, content)
        if receiver is None and constructors.get(callee) == "creator":
            wrap = _drizzle_creator(args[0] if args else None, content)
            creators[_text(name_node, content)] = wrap
        elif receiver is None and constructors.get(callee) == "schema":
            schemas[_text(name_node, content)] = True
    for declarator in declarators:
        value = declarator.child_by_field_name("value")
        name_node = declarator.child_by_field_name("name")
        if value is None or name_node is None or name_node.type != "identifier":
            continue
        if value.type != "call_expression":
            continue
        callee, receiver, args = _call_parts(value, content)
        if receiver is None and constructors.get(callee) == "table":
            wrap = ("", "")
        elif receiver is None and callee in creators:
            wrap = creators[callee]
        elif (
            receiver is not None
            and callee == "table"
            and (
                (receiver.type == "identifier" and _text(receiver, content) in schemas)
                or (
                    receiver.type == "call_expression"
                    and constructors.get(_call_parts(receiver, content)[0]) == "schema"
                )
            )
        ):
            wrap = ("", "")
        else:
            continue
        written = _script_string(args[0], content) if args else None
        table = (
            PLACEHOLDER
            if written is None or wrap is None
            else table_key(wrap[0] + written + wrap[1])
        )
        statement = next(
            (
                a
                for a in _ancestors(declarator)
                if a.type in ("lexical_declaration", "variable_declaration")
            ),
            None,
        )
        exported = (
            statement is not None
            and statement.parent is not None
            and (statement.parent.type == "export_statement")
        )
        columns, complete, spreads = _drizzle_columns(
            args[1] if len(args) > 1 else None, content
        )
        tables.append(
            DrizzleTable(
                file,
                _text(name_node, content),
                table,
                exported,
                name_node.start_point.row + 1,
                name_node.start_point.column,
                columns,
                complete,
                spreads,
            )
        )
    local = {t.variable for t in tables}

    def reference(node):
        if node is None:
            return None
        if node.type == "identifier":
            name = _text(node, content)
            if name in local:
                return DrizzleRef("local", name, None)
            if name in module.imports:
                source, imported = module.imports[name]
                return DrizzleRef("import", imported, source)
            return None
        if node.type == "member_expression":
            target = node.child_by_field_name("object")
            if target is not None and target.type == "identifier":
                prop = _text(node.child_by_field_name("property"), content)
                source = module.namespaces.get(_text(target, content))
                if source is not None:
                    return DrizzleRef("import", prop, source)
                if _text(target, content) in module.imports:
                    # An imported object of tables (``export * as schema``).
                    return DrizzleRef("member", prop, None)
        return None

    for call in _descendants(root, "call_expression"):
        method, receiver, args = _call_parts(call, content)
        if receiver is None:
            continue
        if method in DRIZZLE_READS | DRIZZLE_WRITES:
            argument = args[0] if args else None
            ref = reference(argument)
            if ref is None:
                continue
            reads, writes = method in DRIZZLE_READS, method in DRIZZLE_WRITES
            written = (
                argument.child_by_field_name("property")
                if argument.type == "member_expression"
                else argument
            )
        elif method in DRIZZLE_QUERY_READS and receiver.type == "member_expression":
            query = receiver.child_by_field_name("object")
            if (
                query is None
                or query.type != "member_expression"
                or _text(query.child_by_field_name("property"), content) != "query"
            ):
                continue
            written = receiver.child_by_field_name("property")
            ref = DrizzleRef("query", _text(written, content), None)
            reads, writes = True, False
        else:
            continue
        where = _call_function(call).child_by_field_name("property")
        if writes:
            keys, keys_complete = _drizzle_written_keys(call, method, content)
        elif method in DRIZZLE_QUERY_READS:
            keys, keys_complete = _drizzle_query_keys(args, content)
        else:
            # Columns a select names are read through the table's
            # properties (``users.email``), which the index resolves.
            keys, keys_complete = (), True
        uses.append(
            DrizzleUse(
                file,
                _innermost(definitions, file, call),
                ref,
                reads,
                writes,
                # The method's own line: a chain starts lines earlier.
                where.start_point.row + 1,
                where.start_point.column,
                written.start_point.row + 1,
                written.start_point.column,
                keys,
                keys_complete,
            )
        )
    return tables, uses, module


def _object_keys(node, content):
    """(keys, complete) of an object literal: (key, line, column) of each
    property named by an identifier or string; complete False for a spread,
    a computed key or a node that is not an object literal."""
    if node is None or node.type != "object":
        return (), False
    keys, complete = [], True
    for item in node.named_children:
        if item.type == "comment":
            continue
        key = None
        if item.type == "shorthand_property_identifier":
            key = item
        elif item.type in ("pair", "method_definition"):
            key = item.child_by_field_name("key")
        if key is None or key.type not in (
            "property_identifier",
            "shorthand_property_identifier",
            "string",
        ):
            complete = False
            continue
        name = (
            _script_string(key, content)
            if key.type == "string"
            else _text(key, content)
        )
        if name is None:
            complete = False
            continue
        keys.append((name, key.start_point.row + 1, key.start_point.column))
    return tuple(keys), complete


def _drizzle_columns(node, content):
    """(columns, complete) of a Drizzle table's columns argument: an
    object literal, or an arrow function returning one (``(t) => ({...})``).
    Each column is (key, the name its builder is given or None, line,
    column of the key): the first argument of the call the property's
    value chain starts with (``text("email").notNull()``) when it is a
    string."""
    if node is not None and node.type == "arrow_function":
        body = node.child_by_field_name("body")
        while body is not None and body.type == "parenthesized_expression":
            body = body.named_children[0] if body.named_children else None
        node = body
    if node is None or node.type != "object":
        return (), False, ()
    columns, complete, spreads = [], True, []
    for item in node.named_children:
        if item.type == "comment":
            continue
        if item.type == "spread_element":
            # ``...timestamps`` or ``...softDeleteColumns()``: the columns
            # of an object declared elsewhere, found through the index.
            spread = item.named_children[0] if item.named_children else None
            if spread is not None and spread.type == "call_expression":
                spread = _call_function(spread)
            if spread is not None and spread.type == "identifier":
                spreads.append((spread.start_point.row + 1, spread.start_point.column))
            else:
                complete = False
            continue
        if item.type != "pair":
            complete = False
            continue
        key = item.child_by_field_name("key")
        if key is None or key.type not in ("property_identifier", "string"):
            complete = False
            continue
        name = (
            _script_string(key, content)
            if key.type == "string"
            else _text(key, content)
        )
        value = item.child_by_field_name("value")
        # The builder call a chain starts with.
        builder = value
        while builder is not None and builder.type == "call_expression":
            function = _call_function(builder)
            if function is None or function.type != "member_expression":
                break
            inner = function.child_by_field_name("object")
            if inner is None or inner.type != "call_expression":
                break
            builder = inner
        if name is None or builder is None or builder.type != "call_expression":
            complete = False
            continue
        written = _builder_name(builder, content)
        function = _call_function(builder)
        callee = (
            (function.start_point.row + 1, function.start_point.column)
            if written is None
            and function is not None
            and function.type == "identifier"
            else None
        )
        columns.append(
            (name, written, key.start_point.row + 1, key.start_point.column, callee)
        )
    return tuple(columns), complete, tuple(spreads)


def _declared_value(tree, line, column):
    """The value a variable or function declared with its name at (line,
    column) gives: the variable's initializer, or what the function (or
    an arrow function initializer) returns; None when there is not one."""
    for kind in ("variable_declarator", "function_declaration"):
        for node in _descendants(tree.root_node, kind):
            name = node.child_by_field_name("name")
            if name is None or (
                name.start_point.row + 1,
                name.start_point.column,
            ) != (line, column):
                continue
            value = (
                node.child_by_field_name("value")
                if node.type == "variable_declarator"
                else node
            )
            if value is not None and value.type in (
                "arrow_function",
                "function_declaration",
                "function_expression",
            ):
                body = value.child_by_field_name("body")
                if body is not None and body.type == "statement_block":
                    returns = [
                        c for c in body.named_children if c.type == "return_statement"
                    ]
                    body = (
                        returns[0].named_children[0]
                        if len(returns) == 1 and returns[0].named_children
                        else None
                    )
                while body is not None and body.type == "parenthesized_expression":
                    body = body.named_children[0] if body.named_children else None
                return body
            return value
    return None


def drizzle_spread_columns(tree, content, line, column):
    """(columns, complete, spreads) of the object of Drizzle columns a
    variable or function declared at (line, column) holds or returns, as
    _drizzle_columns gives them."""
    return _drizzle_columns(_declared_value(tree, line, column), content)


def _builder_name(builder, content):
    """The string a column builder call is given first, or None."""
    _, _, args = _call_parts(builder, content)
    if args and args[0].type in ("string", "template_string"):
        return _script_string(args[0], content)
    return None


def drizzle_helper_name(tree, content, line, column):
    """The column name a helper function declared at (line, column) gives
    its column: the function (``const createdAt = () =>
    timestamptz("created_at").notNull()``) returns a builder chain whose
    first call is given the name as a string; None otherwise."""
    body = _declared_value(tree, line, column)
    while body is not None and body.type == "call_expression":
        inner = _call_function(body)
        if inner is None or inner.type != "member_expression":
            break
        receiver = inner.child_by_field_name("object")
        if receiver is None or receiver.type != "call_expression":
            break
        body = receiver
    if body is None or body.type != "call_expression":
        return None
    return _builder_name(body, content)


def _chain_after(call, content):
    """(method, arguments) of each call chained after ``call``
    (``db.insert(t).values(...).onConflictDoUpdate(...)``)."""
    node = call
    while (
        node.parent is not None
        and node.parent.type == "member_expression"
        and node.parent.parent is not None
        and node.parent.parent.type == "call_expression"
    ):
        node = node.parent.parent
        method, _, args = _call_parts(node, content)
        yield method, args


def _drizzle_written_keys(call, method, content):
    """(keys, complete) of the columns an insert or update writes: the keys
    of its ``values`` objects (one, or an array of them), ``set`` object
    and ``onConflictDoUpdate``'s ``set``. A delete writes no column."""
    if method == "delete":
        return (), True
    keys, complete, seen = [], True, False
    for chained, args in _chain_after(call, content):
        objects = []
        if chained in ("values", "set"):
            argument = args[0] if args else None
            if argument is not None and argument.type == "array":
                objects = list(argument.named_children)
            else:
                objects = [argument]
            seen = True
        elif chained in ("onConflictDoUpdate", "onDuplicateKeyUpdate"):
            options = args[0] if args else None
            setting = [
                item.child_by_field_name("value")
                for item in (options.named_children if options else ())
                if item.type == "pair"
                and _text(item.child_by_field_name("key"), content) == "set"
            ]
            objects = setting
        for node in objects:
            found, whole = _object_keys(node, content)
            keys.extend(found)
            complete = complete and whole
    return tuple(keys), complete and seen


def _drizzle_query_keys(args, content):
    """(keys, complete) of the columns a relational query names by key:
    its ``columns`` object. A query that names columns another way (an
    object ``where`` or ``orderBy``, ``extras``, or options built
    elsewhere) is not complete; ``with`` relations are read by their own
    queries' tables."""
    if not args:
        return (), True
    found, complete = _object_keys(args[0], content)
    if not complete:
        return (), False
    keys = []
    for item in args[0].named_children:
        if item.type != "pair":
            continue
        name = _text(item.child_by_field_name("key"), content)
        value = item.child_by_field_name("value")
        if name == "columns":
            columns, whole = _object_keys(value, content)
            keys.extend(columns)
            complete = complete and whole
        elif name in ("where", "orderBy") and value.type not in (
            "arrow_function",
            "function_expression",
        ):
            complete = False
        elif name == "extras":
            complete = False
    return tuple(keys), complete


_SCRIPT_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


def resolve_script_module(file, source, files):
    """The repository file a relative module specifier names from ``file``:
    the path itself, with a script suffix, a ``.js`` written for its
    ``.ts`` source, or its directory's index file; None for packages."""
    if not source.startswith("."):
        return None
    base = file.rpartition("/")[0]
    parts = [p for p in base.split("/") if p]
    for part in source.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return _script_file("/".join(parts), files)


def _script_file(path, files):
    """The repository file a module path names: the path itself, with a
    script suffix, a ``.js`` written for its ``.ts`` source, or its
    directory's index file."""
    stem = re.sub(r"\.(?:m|c)?js$|\.jsx$", "", path)
    candidates = [path, *(path + s for s in _SCRIPT_SUFFIXES)]
    candidates += [stem + s for s in _SCRIPT_SUFFIXES]
    candidates += [f"{path}/index{s}" for s in _SCRIPT_SUFFIXES]
    return next((c for c in candidates if c in files), None)


def _join(directory, relative):
    parts = [p for p in directory.split("/") if p]
    for part in relative.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def _strip_jsonc(text):
    """JSON text of a JSON-with-comments file (tsconfig.json): comments and
    trailing commas outside strings removed."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def read_tsconfig_paths(path, content):
    """(base directory, {pattern: [targets]}) of a tsconfig.json's
    ``compilerOptions.paths``, relative to ``baseUrl`` when it is set, else
    to the file's directory; None without paths. ``extends`` is not read."""
    import json

    try:
        config = json.loads(_strip_jsonc(content.decode("utf-8", "replace")))
    except ValueError:
        return None
    options = config.get("compilerOptions") if isinstance(config, dict) else None
    paths = options.get("paths") if isinstance(options, dict) else None
    if not isinstance(paths, dict):
        return None
    directory = path.rpartition("/")[0]
    base = options.get("baseUrl")
    base = _join(directory, base) if isinstance(base, str) else directory
    if base is None:
        return None
    return base, {
        k: [t for t in v if isinstance(t, str)]
        for k, v in paths.items()
        if isinstance(v, list)
    }


def resolve_script_alias(file, source, files, configs):
    """The repository file a ``paths`` alias (``@server/models/x``) names,
    by the nearest enclosing tsconfig.json with paths: {config directory:
    (base directory, paths)}. The longest matching prefix wins, as in
    TypeScript."""
    directory = file.rpartition("/")[0]
    while True:
        if directory in configs:
            break
        if not directory:
            return None
        directory = directory.rpartition("/")[0]
    base, paths = configs[directory]
    best = None
    for pattern, targets in paths.items():
        if "*" in pattern:
            prefix, _, suffix = pattern.partition("*")
            if (
                source.startswith(prefix)
                and source.endswith(suffix)
                and len(source) >= len(prefix) + len(suffix)
            ):
                rest = source[len(prefix) : len(source) - len(suffix)]
                key = len(prefix)
            else:
                continue
        elif pattern == source:
            rest, key = "", len(pattern) + 1
        else:
            continue
        if best is None or key > best[0]:
            best = (key, rest, targets)
    if best is None:
        return None
    for target in best[2]:
        joined = _join(base, target.replace("*", best[1]))
        found = joined is not None and _script_file(joined, files)
        if found:
            return found
    return None


def read_package_entries(path, content):
    """(name, directory, entry paths, {dependency: spec}, subpaths) of a
    package.json: the entry points its ``exports["."]`` (every condition),
    ``types``, ``typings``, ``main`` and ``module`` give, and the targets of
    each other ``exports`` key ({"src/*": ["./src/*.ts"]}), or None when it
    has no exports map; None without a name."""
    import json

    try:
        manifest = json.loads(content)
    except ValueError:
        return None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("name"), str):
        return None
    entries, subpaths = [], {}
    exported = manifest.get("exports")
    if isinstance(exported, dict) and any(k.startswith(".") for k in exported):
        for key, value in exported.items():
            if key.startswith("./"):
                found, stack = [], [value]
                while stack:
                    item = stack.pop()
                    if isinstance(item, str):
                        found.append(item)
                    elif isinstance(item, dict):
                        stack.extend(item.values())
                    elif isinstance(item, list):
                        stack.extend(item)
                subpaths[key[2:]] = found
        exported = exported.get(".")
    stack = [exported]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            entries.append(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    for key in ("types", "typings", "main", "module"):
        if isinstance(manifest.get(key), str):
            entries.append(manifest[key])
    dependencies = {}
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        for name, spec in (manifest.get(section) or {}).items():
            if isinstance(spec, str):
                dependencies[name] = spec
    if not isinstance(manifest.get("exports"), dict) or not any(
        k.startswith(".") for k in manifest["exports"]
    ):
        subpaths = None  # no exports map: every file of the package is open
    return manifest["name"], path.rpartition("/")[0], entries, dependencies, subpaths


def read_tsconfig_out(path, content):
    """(outDir, rootDir) of a tsconfig file's own ``compilerOptions``,
    relative to the repository; None unless it sets both."""
    import json

    try:
        config = json.loads(_strip_jsonc(content.decode("utf-8", "replace")))
    except ValueError:
        return None
    options = config.get("compilerOptions") if isinstance(config, dict) else None
    if not isinstance(options, dict):
        return None
    out, source = options.get("outDir"), options.get("rootDir")
    if not (isinstance(out, str) and isinstance(source, str)):
        return None
    directory = path.rpartition("/")[0]
    out, source = _join(directory, out), _join(directory, source)
    return None if out is None or source is None else (out, source)


def resolve_workspace_module(file, source, files, packages, outputs):
    """The repository file a workspace package import (``@scope/name``)
    names. The importing file's nearest package.json must depend on the
    name through the ``workspace:`` protocol, and exactly one package.json
    of the repository must carry it. An entry point that is a repository
    file is that file; one under a tsconfig's ``outDir`` in the package's
    directory is the source the compiler writes it from, under ``rootDir``
    (``dist/index.js`` from ``src/index.ts``). The entries must agree on one
    source. A subpath (``@scope/name/src/schema``) goes through the
    package's ``exports`` map when it has one, else to that path in the
    package. ``packages`` is [(name, directory, entries, dependencies,
    subpaths)];
    ``outputs`` is {tsconfig directory: [(outDir, rootDir)]}."""
    if source.startswith(".") or source.startswith("/"):
        return None
    parts = source.split("/")
    name = "/".join(parts[:2]) if source.startswith("@") else parts[0]
    rest = "/".join(parts[2:] if source.startswith("@") else parts[1:])
    owner = None
    for package in packages:
        directory = package[1]
        if (directory == "" or file.startswith(directory + "/")) and (
            owner is None or len(directory) > len(owner[1])
        ):
            owner = package
    if owner is None or not owner[3].get(name, "").startswith("workspace:"):
        return None
    members = [p for p in packages if p[0] == name]
    if len(members) != 1:
        return None
    _, directory, entries, _, subpaths = members[0]
    if rest and subpaths is not None:
        # Node's exports map: an exact subpath, else the longest pattern.
        if rest in subpaths:
            entries = subpaths[rest]
        else:
            best = None
            for key, targets in subpaths.items():
                prefix, star, suffix = key.partition("*")
                if (
                    star
                    and rest.startswith(prefix)
                    and rest.endswith(suffix)
                    and len(rest) >= len(prefix) + len(suffix)
                    and (best is None or len(prefix) > len(best[0]))
                ):
                    middle = rest[len(prefix) : len(rest) - len(suffix)]
                    best = (prefix, [t.replace("*", middle) for t in targets])
            if best is None:
                return None
            entries = best[1]
        rest = ""
    found = set()
    for entry in [rest] if rest else entries:
        path = _join(directory, entry)
        if path is None:
            continue
        direct = _script_file(path, files)
        if direct:
            found.add(direct)
            continue
        for out, root in outputs.get(directory, ()):
            if path.startswith(out + "/"):
                stem = re.sub(r"\.d\.ts$|\.(?:m|c)?js$", "", path[len(out) + 1 :])
                written = _script_file(f"{root}/{stem}" if root else stem, files)
                if written:
                    found.add(written)
    return found.pop() if len(found) == 1 else None


# MyBatis: mapper XML and annotations -----------------------------------------

_MYBATIS = "org.apache.ibatis.annotations"
MYBATIS_STATEMENTS = ("select", "insert", "update", "delete")


@dataclass
class _XmlElement:
    tag: str
    attributes: dict
    children: list  # str or _XmlElement
    line: int


def _parse_xml(content):
    """The root element of an XML document, with line numbers, or None.
    External entities and DTDs are not loaded."""
    import xml.parsers.expat

    parser = xml.parsers.expat.ParserCreate()
    root, stack = [], []

    def start(tag, attributes):
        element = _XmlElement(tag, attributes, [], parser.CurrentLineNumber)
        (stack[-1].children if stack else root).append(element)
        stack.append(element)

    def end(tag):
        stack.pop()

    def text(data):
        if stack:
            stack[-1].children.append(data)

    def entity(*_):
        raise ValueError("entity declarations are not read")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    parser.EntityDeclHandler = entity
    try:
        parser.Parse(content, True)
    except (xml.parsers.expat.ExpatError, ValueError):
        return None
    return root[0] if root else None


_MYBATIS_PARAMETER = re.compile(r"#\{[^}]*\}")
_MYBATIS_SUBSTITUTION = re.compile(r"\$\{[^}]*\}")


def _mybatis_text(element, fragments, depth=0):
    """The SQL of a statement element with every dynamic branch kept:
    ``<if>``/``<choose>``/``<foreach>`` bodies, ``<where>`` and ``<set>``
    keywords, ``<trim>`` prefixes and suffixes, and ``<include>`` of a
    ``<sql>`` fragment; ``<selectKey>`` and ``<bind>`` are left out, and an
    include that is not found leaves unfixed text."""
    parts = []
    for child in element.children:
        if isinstance(child, str):
            parts.append(child)
            continue
        tag = child.tag
        if tag in ("selectKey", "bind"):
            continue
        if tag == "include":
            fragment = fragments.get(child.attributes.get("refid", ""))
            parts.append(
                PLACEHOLDER
                if fragment is None or depth > 10
                else _mybatis_text(fragment, fragments, depth + 1)
            )
            continue
        inner = _mybatis_text(child, fragments, depth)
        if tag == "where":
            inner = f" WHERE {inner} "
        elif tag == "set":
            inner = f" SET {inner} "
        elif tag == "trim":
            a = child.attributes
            inner = f" {a.get('prefix', '')} {inner} {a.get('suffix', '')} "
        elif tag == "foreach":
            a = child.attributes
            inner = f" {a.get('open', '')} {inner} {a.get('close', '')} "
        parts.append(f" {inner} ")
    return "".join(parts)


def mybatis_sql(text):
    """SQL with MyBatis parameters: ``#{x}`` is a bind marker, ``${x}`` text
    the code does not fix."""
    return _MYBATIS_SUBSTITUTION.sub(PLACEHOLDER, _MYBATIS_PARAMETER.sub("?", text))


def _sql_statement(text):
    if text is None or text.replace(PLACEHOLDER, "").strip() == "":
        return None
    statement = statement_tables(text)
    if not (statement.reads or statement.writes) and PLACEHOLDER in text:
        return None
    return statement


@dataclass(frozen=True)
class MapperStatement:
    file: str
    namespace: str
    id: str
    statement: Statement | None
    line: int


def extract_mybatis_xml(file, content):
    """The statements of a MyBatis mapper XML file (root ``<mapper
    namespace>``), or None when the file is not one."""
    root = _parse_xml(content)
    if root is None or root.tag != "mapper" or not root.attributes.get("namespace"):
        return None
    namespace = root.attributes["namespace"]
    fragments = {}
    for child in root.children:
        if (
            isinstance(child, _XmlElement)
            and child.tag == "sql"
            and "id" in child.attributes
        ):
            fragments[child.attributes["id"]] = child
            fragments[f"{namespace}.{child.attributes['id']}"] = child
    found = []
    for child in root.children:
        if not isinstance(child, _XmlElement) or child.tag not in MYBATIS_STATEMENTS:
            continue
        if "id" not in child.attributes:
            continue
        text = mybatis_sql(_mybatis_text(child, fragments))
        found.append(
            MapperStatement(
                file,
                namespace,
                child.attributes["id"],
                _sql_statement(text),
                child.line,
            )
        )
    return found


def mybatis_script(text):
    """The SQL of an annotation value: a ``<script>`` element is read as
    mapper XML."""
    if text is not None and text.lstrip().startswith("<script>"):
        root = _parse_xml(text.encode("utf-8"))
        text = PLACEHOLDER if root is None else _mybatis_text(root, {})
    return None if text is None else mybatis_sql(text)


# Laravel Eloquent default table names ---------------------------------------

# Generated from doctrine/inflector 2.0.10 lib/Doctrine/Inflector/Rules/English
_DOCTRINE_PLURALS = (
    ("(s)tatus$", "\\1\\2tatuses"),
    ("(quiz)$", "\\1zes"),
    ("^(ox)$", "\\1\\2en"),
    ("([m|l])ouse$", "\\1ice"),
    ("(matr|vert|ind)(ix|ex)$", "\\1ices"),
    ("(x|ch|ss|sh)$", "\\1es"),
    ("([^aeiouy]|qu)y$", "\\1ies"),
    ("(hive|gulf)$", "\\1s"),
    ("(?:([^f])fe|([lr])f)$", "\\1\\2ves"),
    ("sis$", "ses"),
    ("([ti])um$", "\\1a"),
    ("(tax)on$", "\\1a"),
    ("(c)riterion$", "\\1riteria"),
    ("(p)erson$", "\\1eople"),
    ("(m)an$", "\\1en"),
    ("(c)hild$", "\\1hildren"),
    ("(f)oot$", "\\1eet"),
    ("(buffal|her|potat|tomat|volcan)o$", "\\1\\2oes"),
    ("(alumn|bacill|cact|foc|fung|nucle|radi|stimul|syllab|termin|vir)us$", "\\1i"),
    ("us$", "uses"),
    ("(alias)$", "\\1es"),
    ("(analys|ax|cris|test|thes)is$", "\\1es"),
    ("s$", "s"),
    ("^$", ""),
    ("$", "s"),
)
_DOCTRINE_UNINFLECTED_PLURAL = (
    "\\w+media",
    "advice",
    "aircraft",
    "amoyese",
    "art",
    "audio",
    "baggage",
    "bison",
    "borghese",
    "bream",
    "breeches",
    "britches",
    "buffalo",
    "butter",
    "cantus",
    "carp",
    "cattle",
    "chassis",
    "clippers",
    "clothing",
    "coal",
    "cod",
    "coitus",
    "compensation",
    "congoese",
    "contretemps",
    "coreopsis",
    "corps",
    "cotton",
    "data",
    "debris",
    "deer",
    "diabetes",
    "djinn",
    "education",
    "eland",
    "elk",
    "emoji",
    "equipment",
    "evidence",
    "faroese",
    "feedback",
    "fish",
    "flounder",
    "flour",
    "foochowese",
    "food",
    "furniture",
    "gallows",
    "genevese",
    "genoese",
    "gilbertese",
    "gold",
    "headquarters",
    "herpes",
    "hijinks",
    "homework",
    "hottentotese",
    "impatience",
    "information",
    "innings",
    "jackanapes",
    "jeans",
    "jedi",
    "kin",
    "kiplingese",
    "knowledge",
    "kongoese",
    "leather",
    "love",
    "lucchese",
    "luggage",
    "mackerel",
    "Maltese",
    "management",
    "metadata",
    "mews",
    "money",
    "moose",
    "mumps",
    "music",
    "nankingese",
    "news",
    "nexus",
    "niasese",
    "nutrition",
    "offspring",
    "oil",
    "patience",
    "pekingese",
    "piedmontese",
    "pincers",
    "pistoiese",
    "plankton",
    "pliers",
    "pokemon",
    "police",
    "polish",
    "portuguese",
    "proceedings",
    "progress",
    "rabies",
    "rain",
    "research",
    "rhinoceros",
    "rice",
    "salmon",
    "sand",
    "sarawakese",
    "scissors",
    "sea[- ]bass",
    "series",
    "shavese",
    "shears",
    "sheep",
    "siemens",
    "silk",
    "sms",
    "soap",
    "social media",
    "spam",
    "species",
    "staff",
    "sugar",
    "swine",
    "talent",
    "toothpaste",
    "traffic",
    "travel",
    "trousers",
    "trout",
    "tuna",
    "us",
    "vermontese",
    "vinegar",
    "weather",
    "wenchowese",
    "wheat",
    "whiting",
    "wildebeest",
    "wood",
    "wool",
    "yengeese",
    "people",
    "trivia",
    "\\w+ware$",
    "media",
)
_DOCTRINE_IRREGULAR = (
    ("atlas", "atlases"),
    ("axis", "axes"),
    ("axe", "axes"),
    ("beef", "beefs"),
    ("blouse", "blouses"),
    ("brother", "brothers"),
    ("cafe", "cafes"),
    ("cave", "caves"),
    ("chateau", "chateaux"),
    ("niveau", "niveaux"),
    ("child", "children"),
    ("canvas", "canvases"),
    ("cookie", "cookies"),
    ("brownie", "brownies"),
    ("corpus", "corpuses"),
    ("cow", "cows"),
    ("criterion", "criteria"),
    ("curriculum", "curricula"),
    ("demo", "demos"),
    ("domino", "dominoes"),
    ("echo", "echoes"),
    ("epoch", "epochs"),
    ("foot", "feet"),
    ("fungus", "fungi"),
    ("ganglion", "ganglions"),
    ("gas", "gases"),
    ("genie", "genies"),
    ("genus", "genera"),
    ("goose", "geese"),
    ("graffito", "graffiti"),
    ("hippopotamus", "hippopotami"),
    ("hoof", "hoofs"),
    ("human", "humans"),
    ("iris", "irises"),
    ("larva", "larvae"),
    ("leaf", "leaves"),
    ("lens", "lenses"),
    ("loaf", "loaves"),
    ("man", "men"),
    ("medium", "media"),
    ("memorandum", "memoranda"),
    ("money", "monies"),
    ("mongoose", "mongooses"),
    ("motto", "mottoes"),
    ("move", "moves"),
    ("mythos", "mythoi"),
    ("niche", "niches"),
    ("nucleus", "nuclei"),
    ("numen", "numina"),
    ("occiput", "occiputs"),
    ("octopus", "octopuses"),
    ("opus", "opuses"),
    ("ox", "oxen"),
    ("passerby", "passersby"),
    ("penis", "penises"),
    ("person", "people"),
    ("plateau", "plateaux"),
    ("runner-up", "runners-up"),
    ("safe", "safes"),
    ("sex", "sexes"),
    ("sieve", "sieves"),
    ("soliloquy", "soliloquies"),
    ("son-in-law", "sons-in-law"),
    ("syllabus", "syllabi"),
    ("testis", "testes"),
    ("thief", "thieves"),
    ("tooth", "teeth"),
    ("tornado", "tornadoes"),
    ("trilby", "trilbys"),
    ("turf", "turfs"),
    ("valve", "valves"),
    ("volcano", "volcanoes"),
    ("abuse", "abuses"),
    ("avalanche", "avalanches"),
    ("cache", "caches"),
    ("criterion", "criteria"),
    ("curve", "curves"),
    ("emphasis", "emphases"),
    ("foe", "foes"),
    ("grave", "graves"),
    ("hoax", "hoaxes"),
    ("medium", "media"),
    ("neurosis", "neuroses"),
    ("save", "saves"),
    ("wave", "waves"),
    ("oasis", "oases"),
    ("valve", "valves"),
    ("zombie", "zombies"),
)


def _php_replacement(template, groups):
    """A preg_replace template for re.sub: ``\\N`` of a group the pattern
    does not have is empty, as in PHP."""
    return re.sub(
        r"\\(\d+)",
        lambda m: f"\\g<{m.group(1)}>" if int(m.group(1)) <= groups else "",
        template.replace("\\\\", "\x01"),
    ).replace("\x01", "\\\\")


_DOCTRINE_PLURAL_RULES = tuple(
    (rule, _php_replacement(template, rule.groups))
    for pattern, template in _DOCTRINE_PLURALS
    for rule in [re.compile(pattern, re.IGNORECASE | re.ASCII)]
)
_DOCTRINE_UNINFLECTED = re.compile(
    "^(?:" + "|".join(_DOCTRINE_UNINFLECTED_PLURAL) + ")$", re.IGNORECASE | re.ASCII
)
# Substitutions keys by the lower-case word; a later pair replaces an
# earlier one, as PHP array assignment does.
_DOCTRINE_IRREGULAR_PLURAL = {}
for _singular, _plural_word in _DOCTRINE_IRREGULAR:
    _DOCTRINE_IRREGULAR_PLURAL[_singular] = _plural_word


def doctrine_pluralize(word):
    """doctrine/inflector 2.0's English ``Inflector::pluralize``: words
    left uninflected, then irregular words (keeping an upper-case first
    letter), then the first matching transformation."""
    if word == "":
        return ""
    if _DOCTRINE_UNINFLECTED.search(word):
        return word
    lower = word.lower()
    if lower in _DOCTRINE_IRREGULAR_PLURAL:
        to = _DOCTRINE_IRREGULAR_PLURAL[lower]
        return to[0].upper() + to[1:] if lower[0] != word[0] else to
    for rule, replacement in _DOCTRINE_PLURAL_RULES:
        if rule.search(word):
            return rule.sub(replacement, word)
    return word


_LARAVEL_UNCOUNTABLE = ("recommended", "related")


def _php_ucwords(text):
    return re.sub(
        r"(^|[ \t\r\n\f\v])([a-z])", lambda m: m.group(1) + m.group(2).upper(), text
    )


def laravel_plural(value):
    """Laravel's ``Pluralizer::plural`` (count 2): Doctrine's plural with
    the case of the original word."""
    if value.lower() in _LARAVEL_UNCOUNTABLE or not re.search(
        r"[A-Za-z0-9\u0080-\uffff]$", value
    ):
        return value
    plural = doctrine_pluralize(value)
    for function in (
        str.lower,
        str.upper,
        lambda t: t[:1].upper() + t[1:],
        _php_ucwords,
    ):
        if function(value) == value:
            return function(plural)
    return plural


def laravel_table_name(class_name):
    """Eloquent's default ``getTable()``: ``Str::snake(Str::pluralStudly(
    class_basename))`` (``UserProfile`` is ``user_profiles``,
    ``HTTPLog`` is ``h_t_t_p_logs``)."""
    value = class_name.rpartition("\\")[2]
    parts = re.split(r"(.)(?=[A-Z])", value)
    plural = "".join(parts[:-1]) + laravel_plural(parts[-1])
    if re.fullmatch(r"[a-z]+", plural):
        return plural
    plural = re.sub(r"\s+", "", _php_ucwords(plural))
    return re.sub(r"(.)(?=[A-Z])", r"\1_", plural).lower()


ELOQUENT_BASES = {
    "Illuminate\\Database\\Eloquent\\Model",
    "Illuminate\\Foundation\\Auth\\User",
}
ELOQUENT_PIVOTS = {
    "Illuminate\\Database\\Eloquent\\Relations\\Pivot",
    "Illuminate\\Database\\Eloquent\\Relations\\MorphPivot",
}
LARAVEL_SCHEMA = {"Illuminate\\Support\\Facades\\Schema", "Schema"}
LARAVEL_DB = {"Illuminate\\Support\\Facades\\DB", "DB"}
# Query builder methods: reading, and writing (anywhere in a chain).
ELOQUENT_READS = {
    "all", "find", "findOrFail", "findMany", "findOr", "first", "firstOrFail",
    "firstWhere", "sole", "where", "whereIn", "whereNotIn", "whereKey",
    "whereNull", "whereNotNull", "whereHas", "whereBetween", "orWhere", "query",
    "get", "count", "exists", "doesntExist", "pluck", "value", "paginate",
    "simplePaginate", "cursorPaginate", "cursor", "chunk", "chunkById", "each",
    "lazy", "latest", "oldest", "orderBy", "orderByDesc", "with", "withCount",
    "has", "select", "sum", "max", "min", "avg", "distinct", "groupBy", "limit",
    "take", "skip", "firstOrNew", "join", "leftJoin",
}  # fmt: skip
ELOQUENT_WRITES = {
    "create", "forceCreate", "insert", "insertGetId", "insertOrIgnore",
    "upsert", "update", "updateOrInsert", "delete", "forceDelete", "destroy",
    "truncate", "increment", "decrement", "incrementEach", "decrementEach",
    "firstOrCreate", "updateOrCreate", "createMany", "restore",
}  # fmt: skip
ELOQUENT_READ_WRITES = {"firstOrCreate", "updateOrCreate", "updateOrInsert"}


@dataclass(frozen=True)
class PhpClass:
    file: str
    class_id: str | None
    path: str  # fully qualified, without a leading backslash
    superclass: str | None  # fully qualified
    table: str | None  # protected $table = literal; PLACEHOLDER if not literal
    abstract: bool
    line: int
    column: int


@dataclass(frozen=True)
class PhpAccess:
    file: str
    caller_id: str | None
    model: str | None  # fully qualified class, or None for DB::table
    table: str | None  # DB::table's literal table
    reads: bool
    writes: bool
    line: int
    column: int


def _php_literal(node, content):
    if node is None:
        return None
    if node.type in ("string", "encapsed_string"):
        parts = []
        for child in node.named_children:
            if child.type == "string_content":
                parts.append(_text(child, content))
            else:
                return None
        return "".join(parts)
    return None


def _php_arguments(node):
    arguments = node.child_by_field_name("arguments")
    return [
        a.named_children[0] if a.type == "argument" and a.named_children else a
        for a in (arguments.named_children if arguments is not None else ())
        if a.type != "comment"
    ]


def extract_php_eloquent(tree, content, file, definitions):
    """(classes, accesses, tables) of one PHP file: classes with their
    resolved superclass and ``$table``, static Eloquent calls
    (``Model::where(...)``) and ``DB::table('t')`` chains, and the tables
    ``Schema::create`` (or ``Schema::rename``'s target) makes."""
    classes, accesses, tables, sql_uses = [], [], [], []
    namespace, uses = "", {}

    def resolve(written):
        if written is None:
            return None
        if written.startswith("\\"):
            return written[1:]
        head, _, rest = written.partition("\\")
        if head in uses:
            return uses[head] + ("\\" + rest if rest else "")
        if written in ("self", "static", "parent"):
            return None
        return f"{namespace}\\{written}" if namespace else written

    def use_clauses(declaration):
        prefix = ""
        for child in declaration.named_children:
            if child.type == "namespace_name":
                prefix = _text(child, content) + "\\"
            elif child.type == "namespace_use_clause":
                add_clause(child, "")
            elif child.type == "namespace_use_group":
                for clause in child.named_children:
                    if clause.type == "namespace_use_clause":
                        add_clause(clause, prefix)

    def add_clause(clause, prefix):
        if any(c.type in ("function", "const") for c in clause.children):
            return
        alias = clause.child_by_field_name("alias")
        target = next(
            (c for c in clause.named_children if c.type in ("qualified_name", "name")),
            None,
        )
        if target is None:
            return
        path = (prefix + _text(target, content)).lstrip("\\")
        uses[
            _text(alias, content) if alias is not None else path.rpartition("\\")[2]
        ] = path

    def chain_methods(call):
        names, node = [], call
        while node.parent is not None and node.parent.type == "member_call_expression":
            if node.parent.child_by_field_name("object") != node:
                break
            node = node.parent
            names.append(_text(node.child_by_field_name("name"), content))
        return names

    def access(call, model, table, method):
        chain = [method, *chain_methods(call)]
        writes = any(m in ELOQUENT_WRITES for m in chain)
        reads = any(m in ELOQUENT_READS | ELOQUENT_READ_WRITES for m in chain) or (
            not writes
        )
        accesses.append(
            PhpAccess(
                file,
                _innermost(definitions, file, call),
                model,
                table,
                reads,
                writes,
                call.start_point.row + 1,
                call.start_point.column,
            )
        )

    def walk(node):
        nonlocal namespace
        for child in node.named_children:
            if child.type == "namespace_definition":
                name = child.child_by_field_name("name")
                namespace = _text(name, content) if name is not None else ""
                uses.clear()
                body = child.child_by_field_name("body")
                if body is not None:
                    walk(body)
                continue
            if child.type == "namespace_use_declaration":
                use_clauses(child)
                continue
            if child.type == "class_declaration":
                name = child.child_by_field_name("name")
                base = next(
                    (c for c in child.named_children if c.type == "base_clause"), None
                )
                written = (
                    _text(base.named_children[0], content)
                    if base is not None and base.named_children
                    else None
                )
                table = None
                body = child.child_by_field_name("body")
                for member in body.named_children if body is not None else ():
                    if member.type != "property_declaration":
                        continue
                    if any(c.type == "static_modifier" for c in member.named_children):
                        continue
                    for element in member.named_children:
                        if element.type != "property_element":
                            continue
                        variable = element.child_by_field_name("name")
                        if variable is None or _text(variable, content) != "$table":
                            continue
                        values = [c for c in element.named_children if c != variable]
                        table = (
                            (_php_literal(values[-1], content) or PLACEHOLDER)
                            if values
                            else None
                        )
                path = (
                    f"{namespace}\\{_text(name, content)}"
                    if namespace
                    else _text(name, content)
                )
                classes.append(
                    PhpClass(
                        file,
                        _innermost(definitions, file, name),
                        path,
                        resolve(written),
                        table,
                        any(
                            c.type == "abstract_modifier" for c in child.named_children
                        ),
                        name.start_point.row + 1,
                        name.start_point.column,
                    )
                )
            sql = _php_sql_argument(child, content, resolve)
            if sql is not None:
                text = _php_sql_text(sql, content)
                words = (
                    re.findall(r"[A-Za-z_]+", _COMMENT_OR_STRING.sub(_blank, text))
                    if text is not None
                    else ()
                )
                if words and words[0].lower() in SQL_VERBS:
                    statement = statement_tables(text)
                    if (
                        not (statement.reads or statement.writes)
                        and PLACEHOLDER in text
                    ):
                        statement = None
                    sql_uses.append(
                        SqlUse(
                            file,
                            _innermost(definitions, file, child),
                            "php-sql",
                            False,
                            statement,
                            child.start_point.row + 1,
                            child.start_point.column,
                        )
                    )
            if child.type == "scoped_call_expression":
                scope = resolve(_text(child.child_by_field_name("scope"), content))
                method = _text(child.child_by_field_name("name"), content)
                arguments = _php_arguments(child)
                first = _php_literal(arguments[0], content) if arguments else None
                if scope in LARAVEL_SCHEMA and method == "create" and first:
                    tables.append((table_key(first), child.start_point.row + 1))
                elif (
                    scope in LARAVEL_SCHEMA
                    and method == "rename"
                    and len(arguments) > 1
                ):
                    target = _php_literal(arguments[1], content)
                    if target:
                        tables.append((table_key(target), child.start_point.row + 1))
                elif scope in LARAVEL_DB and method == "table" and arguments:
                    # DB::table('schema.users as u'): the table is users.
                    name = re.split(r"\s+as\s+", first or "", flags=re.I)[0].strip()
                    access(
                        child,
                        None,
                        table_key(name.rpartition(".")[2]) if name else PLACEHOLDER,
                        method,
                    )
                elif scope is not None and method in ELOQUENT_READS | ELOQUENT_WRITES:
                    access(child, scope, None, method)
            walk(child)

    walk(tree.root_node)
    return classes, accesses, tables, sql_uses


# PHP: SQL text passed to PDO, mysqli, pg_*, Doctrine DBAL and DB:: ---------

LARAVEL_SQL_METHODS = {
    "select", "selectOne", "selectResultSets", "scalar", "cursor", "insert",
    "update", "delete", "statement", "affectingStatement", "unprepared",
}  # fmt: skip
PHP_SQL_METHODS = {
    # PDO and mysqli
    "query", "prepare", "exec", "real_query", "multi_query",
    # Doctrine DBAL
    "executeQuery", "executeStatement", "executeUpdate", "executeCacheQuery",
    "fetchAllAssociative", "fetchAssociative", "fetchAllNumeric",
    "fetchNumeric", "fetchOne", "fetchFirstColumn", "fetchAllKeyValue",
    "fetchAllAssociativeIndexed", "iterateAssociative", "iterateNumeric",
    "iterateColumn", "iterateKeyValue",
}  # fmt: skip
# Functions taking SQL: {name: (argument index with 2 arguments or fewer, else)}.
PHP_SQL_FUNCTIONS = {
    "mysqli_query": (1, 1),
    "mysqli_real_query": (1, 1),
    "mysqli_multi_query": (1, 1),
    "mysqli_prepare": (1, 1),
    "mysqli_execute_query": (1, 1),
    "pg_query": (-1, 1),
    "pg_send_query": (1, 1),
    "pg_query_params": (0, 1),
    "pg_send_query_params": (1, 1),
    "pg_prepare": (-1, 2),
    "pg_send_prepare": (2, 2),
}
_PHP_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "v": "\v", "f": "\f", "0": " "}
_PHP_BLOCKS = ("compound_statement", "program")
_PHP_FUNCTIONS = (
    "function_definition",
    "method_declaration",
    "anonymous_function",
    "arrow_function",
)


def _php_sql_argument(node, content, resolve):
    """The argument node that holds SQL in a PHP call, or None."""
    if node.type in ("member_call_expression", "nullsafe_member_call_expression"):
        method = _text(node.child_by_field_name("name"), content)
        target = node.child_by_field_name("object")
        on_connection = (
            target is not None
            and target.type == "scoped_call_expression"
            and resolve(_text(target.child_by_field_name("scope"), content))
            in LARAVEL_DB
            and _text(target.child_by_field_name("name"), content) == "connection"
        )
        if method not in PHP_SQL_METHODS and not (
            on_connection and method in LARAVEL_SQL_METHODS
        ):
            return None
        index = 0
    elif node.type == "scoped_call_expression":
        scope = resolve(_text(node.child_by_field_name("scope"), content))
        if scope not in LARAVEL_DB or (
            _text(node.child_by_field_name("name"), content) not in LARAVEL_SQL_METHODS
        ):
            return None
        index = 0
    elif node.type == "function_call_expression":
        name = _text(node.child_by_field_name("function"), content).lstrip("\\")
        if name.lower() not in PHP_SQL_FUNCTIONS:
            return None
        arguments = _php_arguments(node)
        short, long = PHP_SQL_FUNCTIONS[name.lower()]
        index = short if len(arguments) <= 2 else long
        if not arguments or index >= len(arguments) or -index > len(arguments):
            return None
        return arguments[index]
    else:
        return None
    arguments = _php_arguments(node)
    return arguments[index] if len(arguments) > index else None


def _php_assignments(variable, content):
    """The texts ``variable`` may hold where it is used, from ``$x = ...``
    and ``$x .= ...`` assignments before it in the same function: a list of
    alternatives, each a list of the right sides joined in order; None when
    it is assigned some other way."""
    name = _text(variable, content)
    scope = next((a for a in _ancestors(variable) if a.type in _PHP_FUNCTIONS), None)
    if scope is None:
        scope = variable
        while scope.parent is not None:
            scope = scope.parent
    found = []
    stack = [scope]
    while stack:
        node = stack.pop()
        if node is not scope and node.type in _PHP_FUNCTIONS:
            continue
        stack.extend(reversed(node.named_children))
        if node.type not in (
            "assignment_expression",
            "augmented_assignment_expression",
        ):
            continue
        if node.end_byte > variable.start_byte:
            continue
        left = node.child_by_field_name("left")
        if left is None or _text(left, content) != name:
            continue
        operator = node.child_by_field_name("operator")
        written = (
            "="
            if node.type == "assignment_expression"
            else (_text(operator, content) if operator is not None else "")
        )
        found.append(
            (node.start_byte, written, node.child_by_field_name("right"), node)
        )
    found.sort(key=lambda f: f[0])
    if any(f[1] not in ("=", ".=") for f in found):
        return None
    # The last assignment in a block enclosing the use always runs before
    # it and replaces what came earlier; later assignments inside nested
    # branches may run instead, so each is an alternative text.
    around = {a.id for a in _ancestors(variable)}

    def dominates(assignment):
        block = next((a for a in _ancestors(assignment) if a.type in _PHP_BLOCKS), None)
        return block is not None and block.id in around

    def branches(statement):
        body = statement.child_by_field_name("body")
        clauses = [c for c in statement.named_children if c.type.startswith("else")]
        if body is None or not any(c.type == "else_clause" for c in clauses):
            return None  # Without an else no branch need run.
        return [body, *clauses]

    starts = [i for i, f in enumerate(found) if f[1] == "="]
    kill = max((found[i][0] for i in starts if dominates(found[i][3])), default=-1)
    # An if/else whose every branch assigns, in a block enclosing the use,
    # replaces what came before it too.
    for i in starts:
        for statement in _ancestors(found[i][3]):
            if statement.type != "if_statement" or not dominates(statement):
                continue
            parts = branches(statement)
            if parts and all(
                any(
                    found[j][1] == "="
                    and part.start_byte <= found[j][0] < part.end_byte
                    for j in starts
                )
                for part in parts
            ):
                kill = max(kill, statement.start_byte)
    starts = [i for i in starts if found[i][0] >= kill]
    if not starts:
        return None
    # Each start, with the appends that follow it (all of them, as any
    # branch may run them), is one text the statement may run.
    return [
        [found[i][2]] + [f[2] for f in found[i + 1 :] if f[1] == ".="] for i in starts
    ]


def _php_sql_text(node, content, depth=0, classes=None):
    """The text of a PHP string expression: literals, heredocs and nowdocs,
    ``.`` joins, ``sprintf`` templates and local variables built from them;
    what the code does not fix becomes PLACEHOLDER. None when the
    expression is not text. ``classes(written)``, when given, resolves the
    class an ``X::class`` part names, for DQL."""
    if node is None or depth > 4:
        return None
    if node.type == "class_constant_access_expression" and classes is not None:
        parts = node.named_children
        if len(parts) == 2 and _text(parts[1], content) == "class":
            return classes(_text(parts[0], content))
        return None
    if node.type in ("string", "encapsed_string", "heredoc", "nowdoc"):
        parts = []
        body = node
        for child in node.named_children:
            if child.type in ("heredoc_body", "nowdoc_body"):
                body = child
        for child in body.named_children:
            if child.type in ("string_content", "nowdoc_string"):
                parts.append(_text(child, content))
            elif child.type == "escape_sequence":
                escaped = _text(child, content)[1:]
                parts.append(_PHP_ESCAPES.get(escaped, escaped[:1]))
            elif child.type in ("heredoc_start", "heredoc_end"):
                continue
            else:
                parts.append(PLACEHOLDER)
        return "".join(parts)
    if node.type == "parenthesized_expression" and node.named_children:
        return _php_sql_text(node.named_children[0], content, depth, classes)
    if node.type == "binary_expression":
        operator = node.child_by_field_name("operator")
        if operator is None or _text(operator, content) != ".":
            return None
        sides = [
            _php_sql_text(node.child_by_field_name(side), content, depth, classes)
            for side in ("left", "right")
        ]
        return "".join(PLACEHOLDER if p is None else p for p in sides)
    if node.type == "function_call_expression":
        name = _text(node.child_by_field_name("function"), content).lstrip("\\")
        arguments = _php_arguments(node)
        if name.lower() == "sprintf" and arguments:
            template = _php_sql_text(arguments[0], content, depth, classes)
            if template is not None:
                return re.sub(
                    r"%(?:\d+\$)?[-+ 0]*(?:'.)?\d*(?:\.\d+)?[bcdeEfFgGosuxX]",
                    PLACEHOLDER,
                    template.replace("%%", "\x01"),
                ).replace("\x01", "%")
        return None
    if node.type == "variable_name":
        texts = []
        for values in _php_assignments(node, content) or ():
            parts = [_php_sql_text(v, content, depth + 1, classes) for v in values]
            if parts[0] is None:
                return None
            texts.append("".join(PLACEHOLDER if p is None else p for p in parts))
        # Alternatives as one text: the tables of any branch.
        return " ;\n".join(texts) if texts else None
    return None


def resolve_eloquent(classes):
    """({class path: (table, how)}, unresolved paths) of the concrete
    Eloquent models: classes whose superclass chain, through classes of the
    repository, reaches Eloquent's Model or Authenticatable (or a pivot).
    The table is the nearest ``$table`` up the chain ("explicit"), else the
    default name of the class itself ("default"); a pivot without
    ``$table`` (Laravel singularizes its name) and a ``$table`` that is not
    a literal are unresolved."""
    by_path = {}
    for c in classes:
        by_path.setdefault(c.path, []).append(c)
    single = {p: cs[0] for p, cs in by_path.items() if len(cs) == 1}

    def chain(path, seen=()):
        c = single.get(path)
        if c is None or path in seen or c.superclass is None:
            return None
        if c.superclass in ELOQUENT_BASES | ELOQUENT_PIVOTS:
            return [c], c.superclass in ELOQUENT_PIVOTS
        found = chain(c.superclass, (*seen, path))
        return None if found is None else ([c, *found[0]], found[1])

    tables, unresolved = {}, []
    for path, c in sorted(single.items()):
        found = chain(path)
        if found is None or c.abstract:
            continue
        ancestors, pivot = found
        written = next((a.table for a in ancestors if a.table is not None), None)
        if written == PLACEHOLDER or (written is None and pivot):
            unresolved.append(path)
        elif written is not None:
            tables[path] = (table_key(written.rpartition(".")[2]), "explicit")
        else:
            tables[path] = (laravel_table_name(path), "default")
    return tables, unresolved


# Sequelize: models, migrations and static model calls -------------------------

# The English plural rules of the npm ``inflection`` package 1.13.4, which
# Sequelize 6 uses for default table names (``Utils.pluralize``), copied
# from its ``lib/inflection.js``: the first rule whose pattern matches wins;
# a rule without a replacement leaves an already plural word as it is.
_INFLECTION_UNCOUNTABLE = frozenset(
    (
        "accommodation",
        "adulthood",
        "advertising",
        "advice",
        "aggression",
        "aid",
        "air",
        "aircraft",
        "alcohol",
        "anger",
        "applause",
        "arithmetic",
        "assistance",
        "athletics",
        "bacon",
        "baggage",
        "beef",
        "biology",
        "blood",
        "botany",
        "bread",
        "butter",
        "carbon",
        "cardboard",
        "cash",
        "chalk",
        "chaos",
        "chess",
        "crossroads",
        "countryside",
        "dancing",
        "deer",
        "dignity",
        "dirt",
        "dust",
        "economics",
        "education",
        "electricity",
        "engineering",
        "enjoyment",
        "envy",
        "equipment",
        "ethics",
        "evidence",
        "evolution",
        "fame",
        "fiction",
        "flour",
        "flu",
        "food",
        "fuel",
        "fun",
        "furniture",
        "gallows",
        "garbage",
        "garlic",
        "genetics",
        "gold",
        "golf",
        "gossip",
        "gratitude",
        "grief",
        "guilt",
        "gymnastics",
        "happiness",
        "hardware",
        "harm",
        "hate",
        "hatred",
        "health",
        "heat",
        "help",
        "homework",
        "honesty",
        "honey",
        "hospitality",
        "housework",
        "humour",
        "hunger",
        "hydrogen",
        "ice",
        "importance",
        "inflation",
        "information",
        "innocence",
        "iron",
        "irony",
        "jam",
        "jewelry",
        "judo",
        "karate",
        "knowledge",
        "lack",
        "laughter",
        "lava",
        "leather",
        "leisure",
        "lightning",
        "linguine",
        "linguini",
        "linguistics",
        "literature",
        "litter",
        "livestock",
        "logic",
        "loneliness",
        "luck",
        "luggage",
        "macaroni",
        "machinery",
        "magic",
        "management",
        "mankind",
        "marble",
        "mathematics",
        "mayonnaise",
        "measles",
        "methane",
        "milk",
        "minus",
        "money",
        "mud",
        "music",
        "mumps",
        "nature",
        "news",
        "nitrogen",
        "nonsense",
        "nurture",
        "nutrition",
        "obedience",
        "obesity",
        "oxygen",
        "pasta",
        "patience",
        "physics",
        "poetry",
        "pollution",
        "poverty",
        "pride",
        "psychology",
        "publicity",
        "punctuation",
        "quartz",
        "racism",
        "relaxation",
        "reliability",
        "research",
        "respect",
        "revenge",
        "rice",
        "rubbish",
        "rum",
        "safety",
        "scenery",
        "seafood",
        "seaside",
        "series",
        "shame",
        "sheep",
        "shopping",
        "sleep",
        "smoke",
        "smoking",
        "snow",
        "soap",
        "software",
        "soil",
        "spaghetti",
        "species",
        "steam",
        "stuff",
        "stupidity",
        "sunshine",
        "symmetry",
        "tennis",
        "thirst",
        "thunder",
        "timber",
        "traffic",
        "transportation",
        "trust",
        "underwear",
        "unemployment",
        "unity",
        "validity",
        "veal",
        "vegetation",
        "vegetarianism",
        "vengeance",
        "violence",
        "vitality",
        "warmth",
        "wealth",
        "weather",
        "welfare",
        "wheat",
        "wildlife",
        "wisdom",
        "yoga",
        "zinc",
        "zoology",
    )
)
_INFLECTION_PLURALS = (
    ("^(m|wom)en$", None),
    ("(pe)ople$", None),
    ("(child)ren$", None),
    ("([ti])a$", None),
    ("((a)naly|(b)a|(d)iagno|(p)arenthe|(p)rogno|(s)ynop|(t)he)ses$", None),
    ("(database)s$", None),
    ("(drive)s$", None),
    ("(hi|ti)ves$", None),
    ("(curve)s$", None),
    ("([lr])ves$", None),
    ("([^fo])ves$", None),
    ("([^aeiouy]|qu)ies$", None),
    ("(s)eries$", None),
    ("(m)ovies$", None),
    ("(x|ch|ss|sh)es$", None),
    ("([m|l])ice$", None),
    ("(bus)es$", None),
    ("(o)es$", None),
    ("(shoe)s$", None),
    ("(cris|ax|test)es$", None),
    ("(octop|vir)uses$", None),
    ("(alias|canvas|status|campus)es$", None),
    ("^(summons|bonus)es$", None),
    ("^(ox)en", None),
    ("(matr)ices$", None),
    ("^feet$", None),
    ("^teeth$", None),
    ("^geese$", None),
    ("(quiz)zes$", None),
    ("^(whereas)es$", None),
    ("^(criteri)a$", None),
    ("^genera$", None),
    ("^(m|wom)an$", "$1en"),
    ("(pe)rson$", "$1ople"),
    ("(child)$", "$1ren"),
    ("(drive)$", "$1s"),
    ("^(ox)$", "$1en"),
    ("(ax|test)is$", "$1es"),
    ("(octop|vir)us$", "$1uses"),
    ("(alias|status|canvas|campus)$", "$1es"),
    ("^(summons|bonus)$", "$1es"),
    ("(bu)s$", "$1ses"),
    ("(buffal|tomat|potat)o$", "$1oes"),
    ("([ti])um$", "$1a"),
    ("sis$", "ses"),
    ("(?:([^f])fe|([lr])f)$", "$1$2ves"),
    ("(hi|ti)ve$", "$1ves"),
    ("([^aeiouy]|qu)y$", "$1ies"),
    ("(matr)ix$", "$1ices"),
    ("(vert|ind)ex$", "$1ices"),
    ("(x|ch|ss|sh)$", "$1es"),
    ("([m|l])ouse$", "$1ice"),
    ("^foot$", "feet"),
    ("^tooth$", "teeth"),
    ("^goose$", "geese"),
    ("(quiz)$", "$1zes"),
    ("^(whereas)$", "$1es"),
    ("^(criteri)on$", "$1a"),
    ("^genus$", "genera"),
    ("s$", "s"),
    ("$", "s"),
)


def _js_replacement(template):
    return re.sub(r"\$(\d)", r"\\g<\1>", template)


_INFLECTION_PLURAL_RULES = tuple(
    (re.compile(pattern, re.I), None if r is None else _js_replacement(r))
    for pattern, r in _INFLECTION_PLURALS
)


def inflection_pluralize(word):
    """``inflection.pluralize(word)`` of the npm package 1.13.4."""
    if word.lower() in _INFLECTION_UNCOUNTABLE:
        return word
    for pattern, replacement in _INFLECTION_PLURAL_RULES:
        if pattern.search(word):
            if replacement is not None:
                word = pattern.sub(replacement, word)
            break
    return word


def inflection_underscore(word):
    """``inflection.underscore(word)`` of the npm package 1.13.4."""
    return "/".join(
        re.sub(r"^_", "", re.sub(r"([A-Z])", r"_\1", part)) for part in word.split("::")
    ).lower()


def sequelize_table_name(model_name, freeze=False, underscored=False):
    """Sequelize 6's default table name of a model (``Model.init``): the
    model name itself with ``freezeTableName``, else its plural, underscored
    with ``underscored``."""
    if freeze:
        return model_name
    plural = inflection_pluralize(model_name)
    return inflection_underscore(plural) if underscored else plural


SEQUELIZE_PACKAGES = {"sequelize", "@sequelize/core", "sequelize-typescript"}
SEQUELIZE_READS = {
    "findAll", "findOne", "findByPk", "findAndCountAll", "count", "max", "min",
    "sum", "aggregate", "findOrBuild",
}  # fmt: skip
SEQUELIZE_WRITES = {
    "create", "bulkCreate", "update", "destroy", "upsert", "truncate",
    "restore", "increment", "decrement",
}  # fmt: skip
SEQUELIZE_READ_WRITES = {"findOrCreate", "findCreateFind"}
SEQUELIZE_OPTIONS = ("tableName", "modelName", "freezeTableName", "underscored")
UNKNOWN = object()  # An option whose value the code does not fix.


@dataclass(frozen=True)
class SequelizeModel:
    """A class (``class X extends Model``) or a ``define`` call. ``options``
    holds the literal SEQUELIZE_OPTIONS values (UNKNOWN when not fixed), or
    is None when the options are not an object literal. ``superclass`` is
    "base" for Sequelize's own Model, else the name of a class."""

    file: str
    kind: str  # "class" or "define"
    name: str  # class name, or the name given to define
    binding: str | None  # the variable a define is assigned to
    model_id: str | None
    superclass: str | None
    decorated: bool
    options: tuple | None
    line: int
    column: int
    superclass_module: str | None = None  # the module it is imported from


@dataclass(frozen=True)
class SequelizeUse:
    file: str
    caller_id: str | None
    model: str
    reads: bool
    writes: bool
    line: int
    column: int


def script_bindings(root, content):
    """{local name: (module, imported name)} bound by ``import`` (named,
    default as "default", namespace as "*") or ``require`` (the whole module
    as "default", a destructured or ``.member`` name as itself)."""
    found = {}
    for node in root.named_children:
        if node.type != "import_statement":
            continue
        source = _script_string(node.child_by_field_name("source"), content)
        if source is None:
            continue
        for clause in node.named_children:
            if clause.type != "import_clause":
                continue
            for part in clause.named_children:
                if part.type == "identifier":
                    found[_text(part, content)] = (source, "default")
                elif part.type == "namespace_import":
                    for c in part.named_children:
                        if c.type == "identifier":
                            found[_text(c, content)] = (source, "*")
                elif part.type == "named_imports":
                    for spec in part.named_children:
                        if spec.type != "import_specifier":
                            continue
                        name = _text(spec.child_by_field_name("name"), content)
                        alias = spec.child_by_field_name("alias")
                        local = _text(alias, content) if alias is not None else name
                        found[local] = (source, name)

    def required(node):
        member = None
        if node is not None and node.type == "member_expression":
            member = _text(node.child_by_field_name("property"), content)
            node = node.child_by_field_name("object")
        if node is None or node.type != "call_expression":
            return None
        callee, receiver, args = _call_parts(node, content)
        if callee != "require" or receiver is not None or len(args) != 1:
            return None
        source = _script_string(args[0], content)
        return None if source is None else (source, member)

    for declarator in _descendants(root, "variable_declarator"):
        found_require = required(declarator.child_by_field_name("value"))
        name = declarator.child_by_field_name("name")
        if found_require is None or name is None:
            continue
        source, member = found_require
        if name.type == "identifier":
            found[_text(name, content)] = (source, member or "default")
        elif name.type == "object_pattern" and member is None:
            for part in name.named_children:
                if part.type == "shorthand_property_identifier_pattern":
                    text = _text(part, content)
                    found[text] = (source, text)
                elif part.type == "pair_pattern":
                    key = part.child_by_field_name("key")
                    value = part.child_by_field_name("value")
                    if value is not None and value.type == "identifier":
                        found[_text(value, content)] = (source, _text(key, content))
    return found


def _script_key(node, content):
    if node is None:
        return None
    if node.type in ("property_identifier", "identifier"):
        return _text(node, content)
    return _script_string(node, content)


def _sequelize_options(node, content):
    """The SEQUELIZE_OPTIONS of an options object literal as sorted pairs,
    or None when ``node`` is not an object literal or spreads another."""
    if node is None or node.type != "object":
        return None
    found = {}
    for pair in node.named_children:
        if pair.type == "spread_element":
            return None
        if pair.type == "shorthand_property_identifier":
            if _text(pair, content) in SEQUELIZE_OPTIONS:
                found[_text(pair, content)] = UNKNOWN
            continue
        if pair.type != "pair":
            continue
        key = _script_key(pair.child_by_field_name("key"), content)
        if key not in SEQUELIZE_OPTIONS:
            continue
        value = pair.child_by_field_name("value")
        if value is not None and value.type in ("true", "false"):
            found[key] = value.type == "true"
        else:
            text = _script_string(value, content)
            found[key] = UNKNOWN if text is None else text
    return tuple(sorted(found.items()))


def sequelize_define_options(root, content):
    """The ``define`` option objects of a file that constructs ``new
    Sequelize``: a list of option tuples (None for a non-literal object)."""
    if not any(
        _text(n.child_by_field_name("constructor"), content) == "Sequelize"
        for n in _descendants(root, "new_expression")
    ):
        return []
    found = []
    for pair in _descendants(root, "pair"):
        if _script_key(pair.child_by_field_name("key"), content) == "define":
            found.append(_sequelize_options(pair.child_by_field_name("value"), content))
    return found


def _heritage(node):
    """The expression a class extends, or None."""
    for child in node.named_children:
        if child.type != "class_heritage":
            continue
        for part in child.named_children:
            if part.type == "extends_clause":
                return part.child_by_field_name("value") or (
                    part.named_children[0] if part.named_children else None
                )
            return part
    return None


def extract_sequelize(tree, content, file, definitions):
    """(models, inits, tables, uses) of one TypeScript or JavaScript file:
    classes extending a Model (decorated with sequelize-typescript's
    ``@Table`` or not), ``X.init(attributes, options)`` calls, ``define``
    calls, ``queryInterface.createTable``/``renameTable`` tables and static
    model calls (``User.findAll``, ``db.User.create``)."""
    root = tree.root_node
    bindings = script_bindings(root, content)
    sequelize = {
        local: name
        for local, (source, name) in bindings.items()
        if source in SEQUELIZE_PACKAGES
    }
    models, inits, tables, uses = [], {}, [], []
    local_models = set()
    classes = [
        *_descendants(root, "class_declaration"),
        *_descendants(root, "abstract_class_declaration"),
    ]
    for node in classes:
        name_node = node.child_by_field_name("name")
        base = _heritage(node)
        if name_node is None or base is None:
            continue
        superclass = module = None
        if base.type == "identifier":
            written = _text(base, content)
            if sequelize.get(written) == "Model":
                superclass = "base"
            elif written in bindings:
                module, imported = bindings[written]
                superclass = written if imported in ("default", "*") else imported
            else:
                superclass = written
        elif base.type == "member_expression":
            target = base.child_by_field_name("object")
            if (
                target is not None
                and sequelize.get(_text(target, content)) in ("default", "*")
                and _text(base.child_by_field_name("property"), content) == "Model"
            ):
                superclass = "base"
        if superclass is None:
            continue
        decorators = [c for c in node.named_children if c.type == "decorator"]
        if node.parent is not None and node.parent.type == "export_statement":
            decorators += [
                c for c in node.parent.named_children if c.type == "decorator"
            ]
        decorated, options = False, ()
        for decorator in decorators:
            call = decorator.named_children[0] if decorator.named_children else None
            if call is not None and call.type == "identifier":
                callee, args = _text(call, content), []
            elif call is not None and call.type == "call_expression":
                callee, receiver, args = _call_parts(call, content)
                if receiver is not None:
                    continue
            else:
                continue
            if sequelize.get(callee) != "Table":
                continue
            decorated = True
            options = _sequelize_options(args[0], content) if args else ()
        name = _text(name_node, content)
        local_models.add(name)
        models.append(
            SequelizeModel(
                file,
                "class",
                name,
                None,
                _innermost(definitions, file, name_node),
                superclass,
                decorated,
                options,
                name_node.start_point.row + 1,
                name_node.start_point.column,
                module,
            )
        )
    calls = list(_descendants(root, "call_expression"))
    for call in calls:
        method, receiver, args = _call_parts(call, content)
        if receiver is None:
            continue
        where = _call_function(call).child_by_field_name("property")
        line, column = where.start_point.row + 1, where.start_point.column
        if method == "init" and receiver.type == "identifier" and len(args) >= 2:
            inits.setdefault(_text(receiver, content), []).append(
                _sequelize_options(args[1], content)
            )
        elif method == "define" and args:
            written = _text(receiver, content).rpartition(".")[2]
            if not sequelize and "sequelize" not in written.lower():
                continue
            name = _script_string(args[0], content)
            if name is None:
                continue
            binding = None
            parent = call.parent
            while parent is not None and parent.type in (
                "await_expression",
                "parenthesized_expression",
            ):
                parent = parent.parent
            if parent is not None and parent.type == "variable_declarator":
                target = parent.child_by_field_name("name")
                if target is not None and target.type == "identifier":
                    binding = _text(target, content)
            options = _sequelize_options(args[2], content) if len(args) > 2 else ()
            local_models.update(n for n in (name, binding) if n)
            models.append(
                SequelizeModel(
                    file,
                    "define",
                    name,
                    binding,
                    _innermost(definitions, file, call),
                    None,
                    False,
                    options,
                    line,
                    column,
                )
            )
        elif method in ("createTable", "renameTable") and args:
            interface = (
                receiver.type == "identifier"
                and _text(receiver, content) == "queryInterface"
            ) or (
                receiver.type == "call_expression"
                and _call_parts(receiver, content)[0] == "getQueryInterface"
            )
            if not interface:
                continue
            argument = args[-1] if method == "renameTable" else args[0]
            name = _script_string(argument, content)
            if argument.type == "object":
                options = dict(_sequelize_options(argument, content) or ())
                name = options.get("tableName")
            if isinstance(name, str) and name:
                tables.append((name, line))
    for call in calls:
        method, receiver, args = _call_parts(call, content)
        if receiver is None:
            continue
        if method not in SEQUELIZE_READS | SEQUELIZE_WRITES | SEQUELIZE_READ_WRITES:
            continue
        model = None
        if receiver.type == "identifier":
            written = _text(receiver, content)
            if written in local_models:
                model = written
            elif written in bindings:
                imported = bindings[written][1]
                model = written if imported in ("default", "*") else imported
        elif receiver.type == "member_expression":
            target = receiver.child_by_field_name("object")
            if target is not None and (
                (target.type == "identifier" and _text(target, content) in bindings)
                or (
                    target.type == "member_expression"
                    and _text(target.child_by_field_name("property"), content)
                    == "models"
                )
            ):
                model = _text(receiver.child_by_field_name("property"), content)
        if model is None:
            continue
        where = _call_function(call).child_by_field_name("property")
        uses.append(
            SequelizeUse(
                file,
                _innermost(definitions, file, call),
                model,
                method in SEQUELIZE_READS | SEQUELIZE_READ_WRITES,
                method in SEQUELIZE_WRITES | SEQUELIZE_READ_WRITES,
                where.start_point.row + 1,
                where.start_point.column,
            )
        )
    return models, inits, tables, uses


def merge_sequelize_defines(found):
    """The repository's global (freezeTableName, underscored): False when
    unset, UNKNOWN when not fixed or set differently in several places."""
    values = {"freezeTableName": set(), "underscored": set()}
    for options in found:
        if options is None:
            return UNKNOWN, UNKNOWN
        given = dict(options)
        for key in values:
            values[key].add(given.get(key, False))
    merged = []
    for key in ("freezeTableName", "underscored"):
        seen = values[key] or {False}
        merged.append(
            next(iter(seen)) if len(seen) == 1 and UNKNOWN not in seen else UNKNOWN
        )
    return tuple(merged)


def resolve_sequelize(models, inits, defines, scope=None, resolve=None):
    """{index into ``models``: (table, "explicit" | "default", names)} and
    the unresolved indices. A class is a model when its superclass chain
    reaches Sequelize's Model through classes of the repository (the one
    class of a name in its namespace, else in the repository) and it is
    decorated with ``@Table`` or has an ``init`` call in its file; its
    options come from the decorator or that call, over the global ``define``
    options. A superclass imported from a module that ``resolve(file,
    module)`` finds in the repository is looked up in that file first."""
    scope = scope or (lambda file: "")
    by_name = {}
    for index, m in enumerate(models):
        if m.kind == "class":
            by_name.setdefault(m.name, []).append(index)

    def lookup(name, file, module):
        if module is not None and resolve is not None:
            path = resolve(file, module)
            if path is not None:
                there = [
                    i
                    for i, m in enumerate(models)
                    if m.kind == "class" and m.file == path
                ]
                named = [i for i in there if models[i].name == name]
                if len(named) == 1 or len(there) == 1:
                    return (named or there)[0]
        candidates = by_name.get(name, [])
        own = [i for i in candidates if scope(models[i].file) == scope(file)]
        found = own or candidates
        return found[0] if len(found) == 1 else None

    def reaches_base(index, seen=()):
        m = models[index]
        if m.superclass == "base":
            return True
        if index in seen or m.superclass is None:
            return False
        parent = lookup(m.superclass, m.file, m.superclass_module)
        return parent is not None and reaches_base(parent, (*seen, index))

    freeze_all, underscored_all = defines
    tables, unresolved = {}, []
    for index, m in enumerate(models):
        options = m.options
        if m.kind == "class":
            if not reaches_base(index):
                continue
            calls = inits.get((m.file, m.name), [])
            if not m.decorated and not calls:
                continue
            if calls:
                options = calls[0] if len(calls) == 1 else None
        if options is None:
            unresolved.append(index)
            continue
        given = dict(options)
        if any(v is UNKNOWN for v in given.values()):
            unresolved.append(index)
            continue
        model_name = given.get("modelName", m.name)
        names = {model_name, m.binding if m.kind == "define" else m.name} - {None}
        if given.get("tableName"):
            tables[index] = (table_key(given["tableName"]), "explicit", names)
            continue
        freeze = given.get("freezeTableName", freeze_all)
        underscored = given.get("underscored", underscored_all)
        if freeze is UNKNOWN or underscored is UNKNOWN:
            unresolved.append(index)
            continue
        tables[index] = (
            table_key(sequelize_table_name(model_name, freeze, underscored)),
            "default",
            names,
        )
    return tables, unresolved


# Mongoose: rules and uncountables of mongoose 8.9.5's lib/helpers/pluralize.js
# (the same as mongoose-legacy-pluralize, which mongoose 5 used). Each rule
# carries the first mongoose major that has it: 6 added "goose", 7 "human".
_MONGOOSE_PLURALS = (
    (r"human$", "humans", 7),
    (r"(m)an$", "$1en", 5),
    (r"(pe)rson$", "$1ople", 5),
    (r"(child)$", "$1ren", 5),
    (r"^(ox)$", "$1en", 5),
    (r"(ax|test)is$", "$1es", 5),
    (r"(octop|vir)us$", "$1i", 5),
    (r"(alias|status)$", "$1es", 5),
    (r"(bu)s$", "$1ses", 5),
    (r"(buffal|tomat|potat)o$", "$1oes", 5),
    (r"([ti])um$", "$1a", 5),
    (r"sis$", "ses", 5),
    (r"(?:([^f])fe|([lr])f)$", "$1$2ves", 5),
    (r"(hive)$", "$1s", 5),
    (r"([^aeiouy]|qu)y$", "$1ies", 5),
    (r"(x|ch|ss|sh)$", "$1es", 5),
    (r"(matr|vert|ind)ix|ex$", "$1ices", 5),
    (r"([m|l])ouse$", "$1ice", 5),
    (r"(kn|w|l)ife$", "$1ives", 5),
    (r"(quiz)$", "$1zes", 5),
    (r"^goose$", "geese", 6),
    (r"s$", "s", 5),
    (r"([^a-z])$", "$1", 5),
    (r"$", "s", 5),
)
_MONGOOSE_PLURAL_RULES = tuple(
    (re.compile(pattern), _js_replacement(r), since)
    for pattern, r, since in _MONGOOSE_PLURALS
)
_MONGOOSE_UNCOUNTABLE = frozenset(
    (
        "advice energy excretion digestion cooperation health justice labour "
        "machinery equipment information pollution sewage paper money species "
        "series rain rice fish sheep moose deer news expertise status media"
    ).split()
)
MONGOOSE_MAJORS = (5, 6, 7)  # the majors whose pluralize rules differ


def mongoose_pluralize(name, major=7):
    """The collection name mongoose ``major`` (5, 6, 7 or later) gives a
    model name: ``utils.toCollectionName`` with the default pluralize."""
    if name in ("system.profile", "system.indexes"):
        return name
    word = name.lower()
    if word in _MONGOOSE_UNCOUNTABLE:
        return word
    for pattern, replacement, since in _MONGOOSE_PLURAL_RULES:
        if since <= major and pattern.search(word):
            return pattern.sub(replacement, word)
    return word


MONGOOSE_READS = {
    "find", "findOne", "findById", "countDocuments", "estimatedDocumentCount",
    "count", "distinct", "exists", "aggregate", "where",
}  # fmt: skip
MONGOOSE_WRITES = {
    "create", "insertMany", "updateOne", "updateMany", "update", "deleteOne",
    "deleteMany", "remove", "replaceOne", "bulkWrite", "bulkSave",
}  # fmt: skip
MONGOOSE_READ_WRITES = {
    "findOneAndUpdate", "findByIdAndUpdate", "findOneAndDelete",
    "findByIdAndDelete", "findOneAndRemove", "findByIdAndRemove",
    "findOneAndReplace",
}  # fmt: skip


@dataclass(frozen=True)
class MongooseModel:
    """A ``model(name, schema, collection)`` call, or a discriminator
    (``Base.discriminator(name, schema)``), which shares its base's
    collection. ``collection`` is the literal third argument (UNKNOWN when
    not fixed); ``schema`` is ("local", binding), ("import", module, name),
    ("inline", collection option) or UNKNOWN. ``base`` is a discriminator's
    base reference, the way a call receiver is given in MongooseUse."""

    file: str
    name: str
    model_id: str | None
    collection: object
    schema: object
    base: tuple | None
    line: int
    column: int


@dataclass(frozen=True)
class MongooseUse:
    """A static model call. ``target`` is ("model", index of the file's
    model), ("import", module, exported name) or ("name", model name); or
    ("field", class name, field) for ``this.field.find()`` in a class of
    the file, or ("typed", class reference, method) for a method called on
    a value whose declared type is a class, whose reads and writes come
    from that method's body."""

    file: str
    caller_id: str | None
    target: tuple
    reads: bool
    writes: bool
    line: int
    column: int


@dataclass(frozen=True)
class MongooseClass:
    """A class that may hold a model in a field, the way repository base
    classes do (``constructor(protected model: Model<T>)``, ``super(User)``).
    ``base`` is the ``extends`` class as ("local", name) or ("import",
    module, name); ``fields`` maps a field to the constructor parameter it
    is set from; ``super_args`` gives each ``super(...)`` argument as
    ("model", receiver reference), ("param", index) or None; ``effects``
    maps a method to the (field, reads, writes) of its model calls on
    ``this.field`` and ``calls`` to the methods it calls on ``this``."""

    name: str
    base: tuple | None
    fields: dict
    super_args: tuple
    methods: frozenset
    effects: dict
    calls: dict


@dataclass(frozen=True)
class MongooseFile:
    models: tuple
    uses: tuple
    # {name: collection option} of the file's schemas (None: no option).
    schemas: dict
    # {exported name: ("model", index) | ("schema", name) | ("import",
    # module, name)}; "default" is module.exports or export default.
    exports: dict
    pluralize: object  # None, "off" (pluralize(null)) or UNKNOWN
    # {name: MongooseClass} of the file's classes that extend another or
    # set a field from a constructor parameter.
    classes: dict | None = None
    # Modules re-exported whole (``export * from './x'``).
    stars: tuple = ()


def _unwrap(node):
    while node is not None and node.type in (
        "await_expression",
        "parenthesized_expression",
        "as_expression",
        "satisfies_expression",
        "non_null_expression",
    ):
        node = node.named_children[0] if node.named_children else None
    return node


def extract_mongoose(tree, content, file, definitions):
    """The MongooseFile of one TypeScript or JavaScript file, or None when
    it neither imports mongoose nor exports anything a model call could
    reach."""
    root = tree.root_node
    bindings = script_bindings(root, content)
    mongoose = {
        local: name
        for local, (source, name) in bindings.items()
        if source == "mongoose"
    }
    connections = set()  # bindings of mongoose.createConnection() / useDb()
    # A parameter typed as the mongoose module is mongoose itself:
    # ``typeof import('mongoose')``, ``typeof mongoose`` or ``Mongoose``.
    # Bindings are per file, so a name some other parameter or variable
    # of the file also declares is left out.
    typed, other = set(), set()
    for parameter in [
        *_descendants(root, "required_parameter"),
        *_descendants(root, "optional_parameter"),
    ]:
        name = parameter.child_by_field_name("pattern")
        if name is None or name.type != "identifier":
            continue
        annotation = parameter.child_by_field_name("type")
        kind = annotation.named_children[0] if annotation is not None else None
        text = _text(kind, content).replace(" ", "") if kind is not None else ""
        if (
            text in ('typeofimport("mongoose")', "typeofimport('mongoose')")
            or (
                text.startswith("typeof")
                and mongoose.get(text[len("typeof") :]) in ("default", "*")
            )
            or (
                kind is not None
                and kind.type == "type_identifier"
                and mongoose.get(text) == "Mongoose"
            )
        ):
            typed.add(_text(name, content))
        else:
            other.add(_text(name, content))
    for declarator in _descendants(root, "variable_declarator"):
        target = declarator.child_by_field_name("name")
        if target is not None and target.type == "identifier":
            other.add(_text(target, content))
    for name in typed - other:
        mongoose.setdefault(name, "default")
    if mongoose:
        namespaces = {k for k, v in mongoose.items() if v in ("default", "*")}
        for declarator in _descendants(root, "variable_declarator"):
            value = _unwrap(declarator.child_by_field_name("value"))
            target = declarator.child_by_field_name("name")
            if value is None or target is None:
                continue
            # const { Schema, model } = mongoose; const Schema = mongoose.Schema
            if value.type == "identifier" and _text(value, content) in namespaces:
                if target.type == "object_pattern":
                    for part in target.named_children:
                        if part.type == "shorthand_property_identifier_pattern":
                            mongoose[_text(part, content)] = _text(part, content)
                        elif part.type == "pair_pattern":
                            local = part.child_by_field_name("value")
                            if local is not None and local.type == "identifier":
                                mongoose[_text(local, content)] = _script_key(
                                    part.child_by_field_name("key"), content
                                )
                continue
            if (
                value.type == "member_expression"
                and target.type == "identifier"
                and _text(value.child_by_field_name("object"), content) in namespaces
            ):
                mongoose[_text(target, content)] = _text(
                    value.child_by_field_name("property"), content
                )
                continue
            if value.type != "call_expression":
                continue
            method, receiver, _ = _call_parts(value, content)
            if method in ("createConnection", "useDb") and target.type == "identifier":
                connections.add(_text(target, content))

    def mongoose_root(node):
        """Whether ``node`` is mongoose itself, a connection, or a member
        of one (``mongoose.connection``)."""
        while node is not None and node.type == "member_expression":
            node = node.child_by_field_name("object")
        if node is None or node.type != "identifier":
            return False
        text = _text(node, content)
        return mongoose.get(text) in ("default", "*") or text in connections

    def is_schema(node):
        """``new Schema(...)``, or ``Schema(...)``, which Mongoose's Schema
        constructor turns into the same ``new`` call."""
        node = _unwrap(node)
        if node is None or node.type not in ("new_expression", "call_expression"):
            return False
        constructor = node.child_by_field_name(
            "constructor" if node.type == "new_expression" else "function"
        )
        if constructor is None:
            return False
        if constructor.type == "identifier":
            return mongoose.get(_text(constructor, content)) == "Schema"
        return (
            constructor.type == "member_expression"
            and _text(constructor.child_by_field_name("property"), content) == "Schema"
            and mongoose_root(constructor.child_by_field_name("object"))
        )

    constants = {}
    for declarator in _descendants(root, "variable_declarator"):
        target = declarator.child_by_field_name("name")
        if target is not None and target.type == "identifier":
            constants.setdefault(_text(target, content), []).append(
                declarator.child_by_field_name("value")
            )

    def collection_in(node, depth=0):
        """The ``collection`` key an options expression sets: None when
        unset, UNKNOWN when the code does not fix it. Object literals,
        spreads, constants of the file and ``Object.assign``, lodash
        ``defaults``/``assign``/``merge``/``extend``/``clone`` are read."""
        node = _unwrap(node)
        if node is None or depth > 8:
            return UNKNOWN
        if node.type == "object":
            found = set()
            for pair in node.named_children:
                if pair.type == "spread_element":
                    inner = pair.named_children[0] if pair.named_children else None
                    found.add(collection_in(inner, depth + 1))
                elif pair.type == "shorthand_property_identifier":
                    if _text(pair, content) == "collection":
                        return UNKNOWN
                elif pair.type == "pair":
                    key = pair.child_by_field_name("key")
                    if key is not None and key.type == "computed_property_name":
                        return UNKNOWN
                    if _script_key(key, content) == "collection":
                        text = _script_string(
                            pair.child_by_field_name("value"), content
                        )
                        found.add(text or UNKNOWN)
            found.discard(None)
            return found.pop() if len(found) == 1 else (UNKNOWN if found else None)
        if node.type == "identifier":
            text = _text(node, content)
            values = constants.get(text, [])
            if not values and text in bindings and bindings[text][0] != "mongoose":
                # Options imported from another file, resolved across files.
                return ("option", *bindings[text])
            if len(values) != 1:
                return UNKNOWN
            return collection_in(values[0], depth + 1)
        if node.type == "call_expression":
            method, receiver, args = _call_parts(node, content)
            merging = method in (
                "defaults",
                "assign",
                "merge",
                "extend",
                "defaultsDeep",
            )
            cloning = method in ("clone", "cloneDeep")
            if receiver is not None and _text(receiver, content) not in (
                "_",
                "Object",
                "lodash",
            ):
                return UNKNOWN
            if not (merging or cloning) or (cloning and len(args) != 1):
                return UNKNOWN
            found = {collection_in(a, depth + 1) for a in args}
            found.discard(None)
            return found.pop() if len(found) == 1 else (UNKNOWN if found else None)
        return UNKNOWN

    def collection_option(node):
        """The ``collection`` option of a ``new Schema(fields, options)``."""
        arguments = node.child_by_field_name("arguments")
        args = [
            a
            for a in (arguments.named_children if arguments else ())
            if a.type != "comment"
        ]
        return collection_in(args[1]) if len(args) >= 2 else None

    def model_name(node):
        """The model a receiver names directly: ``mongoose.model('User')``,
        ``this.model('User')``, ``mongoose.models.User``."""
        node = _unwrap(node)
        if node is None:
            return None
        if node.type == "call_expression":
            method, receiver, args = _call_parts(node, content)
            if method == "model" and len(args) == 1 and receiver is not None:
                if receiver.type == "this" or mongoose_root(receiver):
                    return _script_string(args[0], content)
            return None
        if node.type == "member_expression":
            target = node.child_by_field_name("object")
            if (
                target is not None
                and target.type == "member_expression"
                and _text(target.child_by_field_name("property"), content) == "models"
                and mongoose_root(target.child_by_field_name("object"))
            ):
                return _text(node.child_by_field_name("property"), content)
        return None

    schemas, schema_nodes = {}, {}
    for declarator in _descendants(root, "variable_declarator"):
        target = declarator.child_by_field_name("name")
        value = declarator.child_by_field_name("value")
        if target is not None and target.type == "identifier" and is_schema(value):
            name = _text(target, content)
            schema_nodes[name] = _unwrap(value)
            schemas[name] = collection_option(_unwrap(value))
    # @nestjs/mongoose: @Schema(options) classes, SchemaFactory.createForClass.
    nest = {
        local: name
        for local, (source, name) in bindings.items()
        if source == "@nestjs/mongoose"
    }
    decorated = {}
    if nest:
        for node in _descendants(root, "class_declaration"):
            name_node = node.child_by_field_name("name")
            decorators = [c for c in node.named_children if c.type == "decorator"]
            if node.parent is not None and node.parent.type == "export_statement":
                decorators += [
                    c for c in node.parent.named_children if c.type == "decorator"
                ]
            for decorator in decorators:
                call = decorator.named_children[0] if decorator.named_children else None
                if call is None or name_node is None:
                    continue
                if (
                    call.type == "identifier"
                    and nest.get(_text(call, content)) == "Schema"
                ):
                    decorated[_text(name_node, content)] = None
                elif call.type == "call_expression":
                    callee, receiver, args = _call_parts(call, content)
                    if receiver is None and nest.get(callee) == "Schema":
                        decorated[_text(name_node, content)] = (
                            collection_in(args[0]) if args else None
                        )
        for declarator in _descendants(root, "variable_declarator"):
            target = declarator.child_by_field_name("name")
            value = _unwrap(declarator.child_by_field_name("value"))
            if target is None or target.type != "identifier" or value is None:
                continue
            if value.type != "call_expression":
                continue
            method, receiver, args = _call_parts(value, content)
            if (
                method == "createForClass"
                and receiver is not None
                and nest.get(_text(receiver, content)) == "SchemaFactory"
                and len(args) == 1
            ):
                written = _text(args[0], content)
                schemas[_text(target, content)] = decorated.get(written, UNKNOWN)

    def nest_name(node):
        """A Nest model name: a string, or ``X.name`` of a class."""
        node = _unwrap(node)
        text = _script_string(node, content)
        if text:
            return text
        if (
            node is not None
            and node.type == "member_expression"
            and _text(node.child_by_field_name("property"), content) == "name"
        ):
            target = node.child_by_field_name("object")
            if target is not None and target.type == "identifier":
                written = _text(target, content)
                imported = bindings.get(written, (None, "default"))[1]
                return written if imported in ("default", "*") else imported
        return None

    models, local_models, model_nodes = [], {}, {}
    pluralize = None

    def schema_ref(node):
        node = _unwrap(node)
        if node is None:
            return UNKNOWN
        if is_schema(node):
            return ("inline", collection_option(node))
        if node.type == "identifier":
            text = _text(node, content)
            if text in schemas:
                return ("local", text)
            if text in bindings:
                return ("import", *bindings[text])
        return UNKNOWN

    def as_model(node):
        """The index of the model a ``model(...)`` or ``discriminator``
        call defines, recording it once."""
        node = _unwrap(node)
        if node is None:
            return None
        if node.type == "binary_expression":  # mongoose.models.X || model(...)
            for part in (
                node.child_by_field_name("right"),
                node.child_by_field_name("left"),
            ):
                found = as_model(part)
                if found is not None:
                    return found
            return None
        if node.type != "call_expression":
            return None
        if node.id in model_nodes:
            return model_nodes[node.id]
        method, receiver, args = _call_parts(node, content)
        base = None
        if method == "model" and len(args) >= 2:
            if receiver is None:
                if (
                    mongoose.get(_text(node.child_by_field_name("function"), content))
                    != "model"
                ):
                    return None
            elif not mongoose_root(receiver):
                return None
        elif method == "discriminator" and len(args) >= 2 and receiver is not None:
            base = receiver_ref(receiver)
            if base is None:
                return None
        else:
            return None
        name = _script_string(args[0], content)
        if not name:
            return None
        collection = None
        if method == "model" and len(args) >= 3:
            collection = _script_string(args[2], content)
            if collection is None and args[2].type == "identifier":
                # A constant of the file: ``const COLLECTION = 'x'``.
                values = constants.get(_text(args[2], content), [])
                declaration = values[0].parent.parent if values[0] else None
                if (
                    len(values) == 1
                    and declaration is not None
                    and declaration.type == "lexical_declaration"
                    and _text(declaration, content).startswith("const")
                ):
                    collection = _script_string(values[0], content)
            collection = collection or UNKNOWN
        where = node.child_by_field_name("function")
        where = where.child_by_field_name("property") or where
        models.append(
            MongooseModel(
                file,
                name,
                _innermost(definitions, file, node),
                collection,
                schema_ref(args[1]),
                base,
                where.start_point.row + 1,
                where.start_point.column,
            )
        )
        model_nodes[node.id] = len(models) - 1
        return len(models) - 1

    def receiver_ref(node):
        node = _unwrap(node)
        if node is None:
            return None
        direct = model_name(node)
        if direct:
            return ("name", direct)
        if node.type == "identifier":
            text = _text(node, content)
            if text in local_models:
                return ("model", local_models[text])
            if text in bindings and bindings[text][0] != "mongoose":
                return ("import", *bindings[text])
        elif (
            node.type == "member_expression"
            and _text(node.child_by_field_name("object"), content) == "this"
            and injected
        ):
            owner = node
            while owner is not None and owner.type not in (
                "class_declaration",
                "class",
            ):
                owner = owner.parent
            prop = _text(node.child_by_field_name("property"), content)
            name = injected.get(owner.id if owner is not None else None, {}).get(prop)
            return ("name", name) if name else None
        elif node.type == "member_expression":
            # A member of a namespace import or a required module object.
            target = node.child_by_field_name("object")
            if target is not None and target.type == "identifier":
                source, name = bindings.get(_text(target, content), (None, None))
                if source not in (None, "mongoose") and name in ("default", "*"):
                    prop = _text(node.child_by_field_name("property"), content)
                    return ("import", source, prop)
        return None

    if mongoose:
        for declarator in _descendants(root, "variable_declarator"):
            target = declarator.child_by_field_name("name")
            if target is None or target.type != "identifier":
                continue
            found = as_model(declarator.child_by_field_name("value"))
            if found is not None:
                local_models[_text(target, content)] = found
        # Discriminators of discriminators need their base bound first.
        for declarator in _descendants(root, "variable_declarator"):
            target = declarator.child_by_field_name("name")
            if target is None or target.type != "identifier":
                continue
            found = as_model(declarator.child_by_field_name("value"))
            if found is not None:
                local_models.setdefault(_text(target, content), found)
        # Any other declaration: a model returned by a factory function,
        # as LibreChat's ``createUserModel(mongoose)``, or made for effect.
        for call in _descendants(root, "call_expression"):
            as_model(call)
    injected = {}  # {class node id: {property: model name}}
    if nest:
        for call in _descendants(root, "call_expression"):
            method, receiver, args = _call_parts(call, content)
            if (
                method in ("forFeature", "forFeatureAsync")
                and receiver is not None
                and nest.get(_text(receiver, content)) == "MongooseModule"
                and args
                and args[0].type == "array"
            ):
                for entry in args[0].named_children:
                    if entry.type != "object":
                        continue
                    fields = {}
                    for pair in entry.named_children:
                        if pair.type == "pair":
                            key = _script_key(pair.child_by_field_name("key"), content)
                            fields[key] = pair.child_by_field_name("value")
                    name = nest_name(fields.get("name"))
                    if not name or "schema" not in fields:
                        continue
                    collection = None
                    if "collection" in fields:
                        collection = (
                            _script_string(fields["collection"], content) or UNKNOWN
                        )
                    models.append(
                        MongooseModel(
                            file,
                            name,
                            _innermost(definitions, file, entry),
                            collection,
                            schema_ref(fields["schema"]),
                            None,
                            entry.start_point.row + 1,
                            entry.start_point.column,
                        )
                    )
        for decorator in _descendants(root, "decorator"):
            call = decorator.named_children[0] if decorator.named_children else None
            if call is None or call.type != "call_expression":
                continue
            callee, receiver, args = _call_parts(call, content)
            if receiver is not None or nest.get(callee) != "InjectModel" or not args:
                continue
            name = nest_name(args[0])
            holder = decorator.parent
            prop = None
            if holder is not None and holder.type in (
                "required_parameter",
                "optional_parameter",
            ):
                pattern = holder.child_by_field_name("pattern")
                prop = _text(pattern, content) if pattern is not None else None
            elif holder is not None and holder.type == "public_field_definition":
                prop = _text(holder.child_by_field_name("name"), content)
            owner = holder
            while owner is not None and owner.type not in (
                "class_declaration",
                "class",
            ):
                owner = owner.parent
            if name and prop and owner is not None:
                injected.setdefault(owner.id, {})[prop] = name

    exports = {}

    def export_value(name, node):
        node = _unwrap(node)
        if node is None:
            return
        found = as_model(node) if mongoose else None
        if found is not None:
            exports[name] = ("model", found)
        elif node.type in ("identifier", "shorthand_property_identifier"):
            text = _text(node, content)
            if text in local_models:
                exports[name] = ("model", local_models[text])
            elif text in schemas:
                exports[name] = ("schema", text)
            elif text in constants:
                exports[name] = ("options", collection_in(node))
            elif text in bindings:
                exports[name] = ("import", *bindings[text])
        elif is_schema(node):
            exports[name] = ("schema-inline", collection_option(node))
        elif node.type == "object":
            exports[name] = ("options", collection_in(node))
        elif node.type == "call_expression":
            method, receiver, args = _call_parts(node, content)
            if method == "require" and receiver is None and len(args) == 1:
                source = _script_string(args[0], content)
                if source:
                    exports[name] = ("import", source, "default")
        elif node.type == "member_expression":
            target = node.child_by_field_name("object")
            if target is not None and target.type == "call_expression":
                method, receiver, args = _call_parts(target, content)
                source = _script_string(args[0], content) if len(args) == 1 else None
                if method == "require" and receiver is None and source:
                    exports[name] = (
                        "import",
                        source,
                        _text(node.child_by_field_name("property"), content),
                    )

    for node in root.named_children:
        if node.type == "export_statement":
            value = node.child_by_field_name("value")
            source = _script_string(node.child_by_field_name("source"), content)
            if value is not None:
                export_value("default", value)
                continue
            for child in node.named_children:
                if child.type in ("lexical_declaration", "variable_declaration"):
                    for declarator in child.named_children:
                        target = declarator.child_by_field_name("name")
                        if (
                            declarator.type == "variable_declarator"
                            and target is not None
                            and target.type == "identifier"
                        ):
                            export_value(
                                _text(target, content),
                                declarator.child_by_field_name("value"),
                            )
                            if _text(target, content) in schemas:
                                exports.setdefault(
                                    _text(target, content),
                                    ("schema", _text(target, content)),
                                )
                elif child.type == "export_clause":
                    for spec in child.named_children:
                        if spec.type != "export_specifier":
                            continue
                        local = _text(spec.child_by_field_name("name"), content)
                        alias = spec.child_by_field_name("alias")
                        exported = _text(alias, content) if alias is not None else local
                        if source is not None:
                            exports[exported] = ("import", source, local)
                        elif local in local_models:
                            exports[exported] = ("model", local_models[local])
                        elif local in schemas:
                            exports[exported] = ("schema", local)
                        elif local in bindings:
                            exports[exported] = ("import", *bindings[local])
        elif node.type == "expression_statement":
            expression = node.named_children[0] if node.named_children else None
            if expression is None or expression.type != "assignment_expression":
                continue
            left = expression.child_by_field_name("left")
            right = expression.child_by_field_name("right")
            written = _text(left, content) if left is not None else ""
            if written == "module.exports":
                right = _unwrap(right)
                if right is not None and right.type == "object":
                    for pair in right.named_children:
                        if pair.type == "shorthand_property_identifier":
                            export_value(_text(pair, content), pair)
                        elif pair.type == "pair":
                            key = _script_key(pair.child_by_field_name("key"), content)
                            if key:
                                export_value(key, pair.child_by_field_name("value"))
                else:
                    export_value("default", right)
            elif left is not None and left.type == "member_expression":
                target = _text(left.child_by_field_name("object"), content)
                if target in ("module.exports", "exports"):
                    export_value(
                        _text(left.child_by_field_name("property"), content), right
                    )

    # Functions whose ``this`` is a schema's model: statics assigned to
    # ``schema.statics``, given to ``schema.static()``, and the static
    # methods of a class given to ``schema.loadClass()``.
    statics = {}
    classes = {
        _text(n.child_by_field_name("name"), content): n
        for n in _descendants(root, "class_declaration")
        if n.child_by_field_name("name") is not None
    }

    def static_functions(node, schema):
        node = _unwrap(node)
        if node is None:
            return
        if node.type in ("function_expression", "function", "generator_function"):
            statics[node.id] = schema
        elif node.type == "object":
            for part in node.named_children:
                if part.type == "method_definition":
                    statics[part.id] = schema
                elif part.type == "pair":
                    static_functions(part.child_by_field_name("value"), schema)

    for node in _descendants(root, "assignment_expression"):
        left = node.child_by_field_name("left")
        if left is None or left.type != "member_expression":
            continue
        target = left.child_by_field_name("object")
        prop = _text(left.child_by_field_name("property"), content)
        if (
            target is not None
            and target.type == "member_expression"
            and _text(target.child_by_field_name("property"), content) == "statics"
            and _text(target.child_by_field_name("object"), content) in schemas
        ):
            static_functions(
                node.child_by_field_name("right"),
                _text(target.child_by_field_name("object"), content),
            )
        elif prop == "statics" and _text(target, content) in schemas:
            static_functions(node.child_by_field_name("right"), _text(target, content))
    for call in _descendants(root, "call_expression"):
        method, receiver, args = _call_parts(call, content)
        if receiver is None or receiver.type != "identifier":
            continue
        schema = _text(receiver, content)
        if schema not in schemas or not args:
            continue
        if method == "static":
            static_functions(args[-1], schema)
        elif method == "loadClass" and args[0].type == "identifier":
            body = classes.get(_text(args[0], content))
            body = None if body is None else body.child_by_field_name("body")
            for member in body.named_children if body is not None else ():
                if member.type == "method_definition" and any(
                    c.type == "static" for c in member.children
                ):
                    statics[member.id] = schema
    schema_models = {}
    for index, model in enumerate(models):
        if model.schema is not UNKNOWN and model.schema[0] == "local":
            schema_models.setdefault(model.schema[1], set()).add(index)

    def this_model(node):
        """The model ``this`` is at ``node``, inside a static."""
        while node is not None:
            if node.type in (
                "function_expression",
                "function",
                "generator_function",
                "function_declaration",
                "generator_function_declaration",
                "method_definition",
            ):
                schema = statics.get(node.id)
                found = schema_models.get(schema, set())
                return ("model", next(iter(found))) if len(found) == 1 else None
            if node.type in ("class_body", "program"):
                return None
            node = node.parent
        return None

    # Classes that may hold a model in a field, and the declared class
    # types of their properties.
    class_nodes = {}
    for node in [
        *_descendants(root, "class_declaration"),
        *_descendants(root, "abstract_class_declaration"),
    ]:
        name = node.child_by_field_name("name")
        if name is not None:
            class_nodes.setdefault(_text(name, content), node)

    def type_ref(annotation):
        """The class a type annotation names, as a reference."""
        if annotation is None:
            return None
        written = annotation.named_children[0] if annotation.named_children else None
        if written is not None and written.type == "generic_type":
            written = written.child_by_field_name("name")
        if written is None or written.type != "type_identifier":
            return None
        text = _text(written, content)
        if text in class_nodes:
            return ("local", text)
        if text in bindings and bindings[text][0] != "mongoose":
            return ("import", *bindings[text])
        return None

    def own_class(node):
        while node is not None and node.type not in (
            "class_declaration",
            "abstract_class_declaration",
            "class",
        ):
            node = node.parent
        return node

    def own_method(node):
        while node is not None and node.type != "method_definition":
            if node.type in ("class_body", "program"):
                return None
            node = node.parent
        return node

    repository_classes, stars = {}, []
    typed = {}  # {class node id: {property: class reference}}
    for cname, node in class_nodes.items():
        base = None
        for heritage in node.named_children:
            if heritage.type != "class_heritage":
                continue
            for clause in heritage.named_children:
                value = clause.child_by_field_name("value")
                if clause.type == "extends_clause" and value is not None:
                    if value.type == "identifier":
                        text = _text(value, content)
                        if text in class_nodes:
                            base = ("local", text)
                        elif text in bindings and bindings[text][0] != "mongoose":
                            base = ("import", *bindings[text])
        body = node.child_by_field_name("body")
        fields, super_args, methods, effects, calls = {}, (), set(), {}, {}
        props = {}
        for member in body.named_children if body is not None else ():
            if member.type == "public_field_definition":
                found = type_ref(member.child_by_field_name("type"))
                value = _unwrap(member.child_by_field_name("value"))
                built = (
                    value.child_by_field_name("constructor")
                    if found is None
                    and value is not None
                    and value.type == "new_expression"
                    else None
                )
                if built is not None and built.type == "identifier":
                    # private repo = new UserRepository()
                    text = _text(built, content)
                    if text in class_nodes:
                        found = ("local", text)
                    elif text in bindings and bindings[text][0] != "mongoose":
                        found = ("import", *bindings[text])
                if found is not None:
                    props[_text(member.child_by_field_name("name"), content)] = found
            if member.type != "method_definition":
                continue
            mname = _text(member.child_by_field_name("name"), content)
            if mname != "constructor":
                methods.add(mname)
                continue
            params = member.child_by_field_name("parameters")
            names = []
            for index, param in enumerate(
                p
                for p in (params.named_children if params is not None else ())
                if p.type in ("required_parameter", "optional_parameter")
            ):
                pattern = param.child_by_field_name("pattern")
                pname = _text(pattern, content) if pattern is not None else None
                names.append(pname)
                if pname and any(
                    c.type in ("accessibility_modifier", "readonly")
                    for c in param.children
                ):
                    fields[pname] = index
                    found = type_ref(param.child_by_field_name("type"))
                    if found is not None:
                        props[pname] = found
            for call in _descendants(member, "call_expression"):
                function = call.child_by_field_name("function")
                if function is not None and function.type == "super":
                    _, _, args = _call_parts(call, content)
                    found = []
                    for arg in args:
                        arg = _unwrap(arg)
                        text = _text(arg, content) if arg is not None else ""
                        if (
                            arg is not None
                            and arg.type == "identifier"
                            and text in names
                        ):
                            found.append(("param", names.index(text)))
                        else:
                            ref = receiver_ref(arg)
                            found.append(("model", ref) if ref is not None else None)
                    super_args = tuple(found)
            for assignment in _descendants(member, "assignment_expression"):
                left = assignment.child_by_field_name("left")
                right = _unwrap(assignment.child_by_field_name("right"))
                if (
                    left is not None
                    and left.type == "member_expression"
                    and _text(left.child_by_field_name("object"), content) == "this"
                    and right is not None
                    and right.type == "identifier"
                    and _text(right, content) in names
                ):
                    fields[_text(left.child_by_field_name("property"), content)] = (
                        names.index(_text(right, content))
                    )
        typed[node.id] = props
        if base is None and not fields:
            continue
        for call in _descendants(node, "call_expression"):
            if getattr(own_class(call), "id", None) != node.id:
                continue
            method, receiver, _ = _call_parts(call, content)
            holder = own_method(call)
            if holder is None or receiver is None:
                continue
            hname = _text(holder.child_by_field_name("name"), content)
            if receiver.type == "this":
                calls.setdefault(hname, set()).add(method)
            elif (
                receiver.type == "member_expression"
                and _text(receiver.child_by_field_name("object"), content) == "this"
                and method in MONGOOSE_READS | MONGOOSE_WRITES | MONGOOSE_READ_WRITES
            ):
                effects.setdefault(hname, set()).add(
                    (
                        _text(receiver.child_by_field_name("property"), content),
                        method in MONGOOSE_READS | MONGOOSE_READ_WRITES,
                        method in MONGOOSE_WRITES | MONGOOSE_READ_WRITES,
                    )
                )
        # const doc = new this.field(data); doc.save(): a write.
        for declarator in _descendants(node, "variable_declarator"):
            value = _unwrap(declarator.child_by_field_name("value"))
            target = declarator.child_by_field_name("name")
            holder = own_method(declarator)
            if (
                value is None
                or value.type != "new_expression"
                or target is None
                or target.type != "identifier"
                or holder is None
                or getattr(own_class(declarator), "id", None) != node.id
            ):
                continue
            built = value.child_by_field_name("constructor")
            if (
                built is None
                or built.type != "member_expression"
                or _text(built.child_by_field_name("object"), content) != "this"
            ):
                continue
            variable = _text(target, content)
            saved = any(
                _call_parts(call, content)[0] == "save"
                and _call_parts(call, content)[1] is not None
                and _text(_call_parts(call, content)[1], content) == variable
                for call in _descendants(holder, "call_expression")
            )
            if saved:
                effects.setdefault(
                    _text(holder.child_by_field_name("name"), content), set()
                ).add(
                    (_text(built.child_by_field_name("property"), content), False, True)
                )
        repository_classes[cname] = MongooseClass(
            cname,
            base,
            fields,
            super_args,
            frozenset(methods),
            {k: tuple(sorted(v)) for k, v in effects.items()},
            {k: tuple(sorted(v)) for k, v in calls.items()},
        )
    for node in root.named_children:
        declaration = (
            node.child_by_field_name("declaration")
            if node.type == "export_statement"
            else None
        )
        if declaration is not None and declaration.type in (
            "class_declaration",
            "abstract_class_declaration",
        ):
            name = _text(declaration.child_by_field_name("name"), content)
            exports.setdefault(name, ("class", name))
        elif node.type == "export_statement":
            source = _script_string(node.child_by_field_name("source"), content)
            if source and not any(
                c.type in ("export_clause", "namespace_export")
                for c in node.named_children
            ):
                stars.append(source)
            value = node.child_by_field_name("value")
            if value is not None and value.type == "identifier":
                if _text(value, content) in class_nodes:
                    exports["default"] = ("class", _text(value, content))
            for clause in node.named_children:
                if clause.type != "export_clause" or node.child_by_field_name("source"):
                    continue
                for spec in clause.named_children:
                    # A comment in the braces is a named child too.
                    if spec.type != "export_specifier":
                        continue
                    local = _text(spec.child_by_field_name("name"), content)
                    alias = spec.child_by_field_name("alias")
                    if local in class_nodes:
                        exported = _text(alias, content) if alias is not None else local
                        exports[exported] = ("class", local)

    def property_use(receiver, call):
        """("field", ...) or ("typed", ...) for ``this.p.m()`` in a class."""
        owner = own_class(receiver)
        if owner is None or owner.type == "class":
            return None
        cname = _text(owner.child_by_field_name("name"), content)
        prop = _text(receiver.child_by_field_name("property"), content)
        method, _, _ = _call_parts(call, content)
        ref = typed.get(owner.id, {}).get(prop)
        if ref is not None:
            return ("typed", ref, method)
        if (
            cname in repository_classes
            and method in MONGOOSE_READS | MONGOOSE_WRITES | MONGOOSE_READ_WRITES
        ):
            return ("field", cname, prop)
        return None

    uses = []
    for call in _descendants(root, "call_expression"):
        method, receiver, args = _call_parts(call, content)
        if receiver is None:
            continue
        found = None
        if (
            receiver.type == "member_expression"
            and _text(receiver.child_by_field_name("object"), content) == "this"
            and receiver_ref(receiver) is None
        ):
            found = property_use(receiver, call)
        elif receiver.type == "this":
            owner = own_class(call)
            if (
                owner is not None
                and owner.type != "class"
                and _text(owner.child_by_field_name("name"), content)
                in repository_classes
                and this_model(call) is None
            ):
                found = (
                    "typed",
                    ("local", _text(owner.child_by_field_name("name"), content)),
                    method,
                )
        if found is not None:
            kind = found[0]
            fixed = kind == "field"
            where = _call_function(call).child_by_field_name("property")
            uses.append(
                MongooseUse(
                    file,
                    _innermost(definitions, file, call),
                    found,
                    fixed and method in MONGOOSE_READS | MONGOOSE_READ_WRITES,
                    fixed and method in MONGOOSE_WRITES | MONGOOSE_READ_WRITES,
                    where.start_point.row + 1,
                    where.start_point.column,
                )
            )
            continue
        if method == "pluralize" and mongoose_root(receiver) and args:
            pluralize = "off" if args[0].type in ("null", "undefined") else UNKNOWN
            continue
        if (
            mongoose
            and method == "set"
            and receiver.type == "identifier"
            and len(args) == 2
        ):
            if (
                _script_string(args[0], content) == "collection"
                and _text(receiver, content) in schemas
            ):
                text = _script_string(args[1], content)
                schemas[_text(receiver, content)] = text or UNKNOWN
            continue
        if method not in MONGOOSE_READS | MONGOOSE_WRITES | MONGOOSE_READ_WRITES:
            continue
        target = this_model(call) if receiver.type == "this" else receiver_ref(receiver)
        if target is None:
            continue
        where = _call_function(call).child_by_field_name("property")
        uses.append(
            MongooseUse(
                file,
                _innermost(definitions, file, call),
                target,
                method in MONGOOSE_READS | MONGOOSE_READ_WRITES,
                method in MONGOOSE_WRITES | MONGOOSE_READ_WRITES,
                where.start_point.row + 1,
                where.start_point.column,
            )
        )
    if not (models or uses or exports or pluralize or repository_classes or stars):
        return None
    return MongooseFile(
        tuple(models),
        tuple(uses),
        schemas,
        exports,
        pluralize,
        repository_classes,
        tuple(stars),
    )


def mongoose_majors(specs):
    """The pluralize variants (MONGOOSE_MAJORS) the ``mongoose`` version
    specifiers of the package manifests allow: every major a specifier
    names, all of them when none does."""
    found = set()
    for spec in specs:
        numbers = re.findall(r"(?:^|[\s|^~>=<v]|\|\|)(\d+)(?:\.|\s|$|\b)", spec)
        if not numbers:
            return set(MONGOOSE_MAJORS)
        found.update(min(max(int(n), 5), 7) for n in numbers)
    return found or set(MONGOOSE_MAJORS)


def resolve_mongoose(files, majors=None, resolve=None, scope=None):
    """({(file, index): (collection, how, base key or None)}, {(file,
    index): reason}, resolve_use) over the MongooseFile of each file.
    ``how`` is "explicit" (the collection argument), "schema" (the schema's
    collection option) or "default" (the model name pluralized as every
    allowed mongoose major does). A discriminator takes its base's
    collection. ``resolve_use(use)`` gives a use's model key, or the reason
    it has none: "package", "not_a_model" or "ambiguous". A ("field", ...)
    use has no model ("field") unless its class passes one to ``super``.
    A ("typed", ...) use gives [(model key or reason, reads, writes)] for
    the method it calls, inherited from the class's bases, or the reason it
    has none: "package" (not a class that may hold a model),
    "declared_method" (the class's own method, whose body carries the
    uses), "method_not_found" or "no_model_access"."""
    majors = majors or set(MONGOOSE_MAJORS)
    resolve = resolve or (lambda file, module: None)
    scope = scope or (lambda file: "")
    by_name = {}
    for file, found in sorted(files.items()):
        for index, model in enumerate(found.models):
            by_name.setdefault(model.name, []).append((file, index))
    pluralize = {f.pluralize for f in files.values() if f.pluralize is not None}

    starred = {}

    def export(file, name, depth=0):
        """What ``file`` exports as ``name``, following re-exports. Of
        ``export *`` modules, only one may export the name, as in ES
        modules, where an ambiguous star export is no export."""
        found = files.get(file)
        if found is None or depth > 8:
            return None
        value = found.exports.get(name)
        if value is not None and value[0] == "import":
            target = resolve(file, value[1])
            return None if target is None else export(target, value[2], depth + 1)
        if value is None and name != "default" and found.stars:
            if (file, name) not in starred:
                starred[file, name] = None  # a cycle of star exports
                hits = []
                for source in found.stars:
                    target = resolve(file, source)
                    hit = None if target is None else export(target, name, depth + 1)
                    if hit is not None and hit not in hits:
                        hits.append(hit)
                starred[file, name] = hits[0] if len(hits) == 1 else None
            return starred[file, name]
        return None if value is None else (file, value)

    def model_key(file, ref):
        kind = ref[0]
        if kind == "model":
            return (file, ref[1])
        if kind == "name":
            keys = by_name.get(ref[1], [])
            own = [k for k in keys if scope(k[0]) == scope(file)]
            keys = own or keys
            if len(keys) > 1:
                # One model registered in several places (Nest's forFeature
                # in each module) is one model when they name one collection.
                found = {(collection(k) or (None,))[0] for k in keys}
                if len(found) == 1 and None not in found:
                    return keys[0]
            return (
                keys[0] if len(keys) == 1 else ("ambiguous" if keys else "not_a_model")
            )
        target = resolve(file, ref[1])
        if target is None:
            return "package"
        value = export(target, ref[2])
        if value is None or value[1][0] != "model":
            return "not_a_model"
        return (value[0], value[1][1])

    def option(file, value, depth=0):
        """A collection option, following options imported from a file."""
        if not isinstance(value, tuple):
            return value
        target = resolve(file, value[1])
        found = None if target is None or depth > 8 else export(target, value[2])
        if found is None or found[1][0] != "options":
            return UNKNOWN
        return option(found[0], found[1][1], depth + 1)

    def schema_collection(file, ref, depth=0):
        if ref is UNKNOWN or depth > 8:
            return UNKNOWN
        if ref[0] == "inline":
            return option(file, ref[1])
        if ref[0] == "local":
            return option(file, files[file].schemas.get(ref[1], UNKNOWN))
        target = resolve(file, ref[1])
        value = None if target is None else export(target, ref[2])
        if value is None:
            return UNKNOWN
        where, (kind, *rest) = value
        if kind == "schema":
            return option(where, files[where].schemas.get(rest[0], UNKNOWN))
        if kind == "schema-inline":
            return option(where, rest[0])
        return UNKNOWN

    tables, unresolved = {}, {}

    def collection(key, seen=()):
        if key in tables or key in unresolved:
            return tables.get(key)
        file, index = key
        model = files[file].models[index]
        if model.base is not None:
            base = model_key(file, model.base)
            if not isinstance(base, tuple) or base in seen:
                unresolved[key] = "base"
                return None
            found = collection(base, (*seen, key))
            if found is None:
                unresolved[key] = "base"
                return None
            tables[key] = (found[0], found[1], base)
            return tables[key]
        if model.collection is UNKNOWN:
            unresolved[key] = "collection"
        elif model.collection is not None:
            tables[key] = (model.collection, "explicit", None)
        else:
            option = schema_collection(file, model.schema)
            if option is UNKNOWN:
                unresolved[key] = "schema"
            elif option is not None:
                tables[key] = (option, "schema", None)
            elif UNKNOWN in pluralize:
                unresolved[key] = "pluralize"
            elif "off" in pluralize:
                tables[key] = (model.name, "default", None)
            else:
                names = {mongoose_pluralize(model.name, m) for m in majors}
                if len(names) == 1:
                    tables[key] = (names.pop(), "default", None)
                else:
                    unresolved[key] = "version"
        return tables.get(key)

    for file, found in sorted(files.items()):
        for index in range(len(found.models)):
            collection((file, index))

    def class_of(file, ref):
        """(file, class name) of a class reference, when that class may
        hold a model in a field."""
        if ref is None:
            return None
        if ref[0] == "local":
            where, name = file, ref[1]
        else:
            target = resolve(file, ref[1])
            value = None if target is None else export(target, ref[2])
            if value is None or value[1][0] != "class":
                return None
            where, name = value[0], value[1][1]
        return (where, name) if name in (files[where].classes or {}) else None

    def cls(key):
        return files[key[0]].classes[key[1]]

    def field_param(key, field, depth=0):
        """The constructor parameter of ``key`` that ends up in ``field``."""
        c = cls(key)
        if field in c.fields:
            return c.fields[field]
        base = class_of(key[0], c.base)
        if base is None or depth > 8:
            return None
        index = field_param(base, field, depth + 1)
        arg = (
            c.super_args[index]
            if index is not None and index < len(c.super_args)
            else None
        )
        return arg[1] if arg is not None and arg[0] == "param" else None

    def field_model(key, field, depth=0):
        """The model key (or a reason) that the class ``key`` holds in
        ``field`` because its constructor passes it to ``super``."""
        c = cls(key)
        if field in c.fields or depth > 8:
            return None
        base = class_of(key[0], c.base)
        if base is None:
            return None
        index = field_param(base, field)
        if index is None:
            return field_model(base, field, depth + 1)
        arg = c.super_args[index] if index < len(c.super_args) else None
        if arg is None or arg[0] != "model":
            return None
        return model_key(key[0], arg[1])

    def dispatch(key, method, depth=0):
        """The class that ``method`` on an instance of ``key`` runs from."""
        while key is not None and depth <= 8:
            if method in cls(key).methods:
                return key
            key = class_of(key[0], cls(key).base)
            depth += 1
        return None

    def effects(key, method):
        """{model key or reason: [reads, writes]} of ``method`` run on an
        instance of ``key``, through the methods it calls on ``this``."""
        found, seen, todo = {}, set(), [method]
        while todo:
            name = todo.pop()
            if name in seen:
                continue
            seen.add(name)
            where = dispatch(key, name)
            if where is None:
                continue
            for field, reads, writes in cls(where).effects.get(name, ()):
                model = field_model(key, field)
                if model is None:
                    continue
                flags = found.setdefault(model, [False, False])
                flags[0] |= reads
                flags[1] |= writes
            todo.extend(cls(where).calls.get(name, ()))
        return found

    holding = {}

    def holds_model(key):
        """Whether the class, or a base, calls model methods on a field
        that holds a model for instances of ``key``."""
        if key not in holding:
            holding[key] = False
            fields, where, depth = set(), key, 0
            while where is not None and depth <= 8:
                for found in cls(where).effects.values():
                    fields.update(field for field, _, _ in found)
                where = class_of(where[0], cls(where).base)
                depth += 1
            holding[key] = any(field_model(key, f) is not None for f in fields)
        return holding[key]

    def resolve_use(use):
        kind = use.target[0]
        if kind == "field":
            found = field_model((use.file, use.target[1]), use.target[2])
            return "field" if found is None else found
        if kind != "typed":
            return model_key(use.file, use.target)
        key = class_of(use.file, use.target[1])
        method = use.target[2]
        if key is None or not holds_model(key):
            return "package"
        if method in cls(key).methods:
            return "declared_method"
        if dispatch(key, method) is None:
            return "method_not_found"
        found = effects(key, method)
        return [(k, r, w) for k, (r, w) in sorted(found.items(), key=str)] or (
            "no_model_access"
        )

    return tables, unresolved, resolve_use


# Doctrine ORM: entity mappings (attributes, docblock annotations, XML),
# repositories and EntityManager calls -------------------------------------

DOCTRINE_MAPPING = "Doctrine\\ORM\\Mapping\\"
DOCTRINE_FINDERS = {"find", "findBy", "findOneBy", "findAll", "count", "matching"}
DOCTRINE_REPOSITORY_BASES = {
    "Doctrine\\ORM\\EntityRepository",
    "Doctrine\\Bundle\\DoctrineBundle\\Repository\\ServiceEntityRepository",
    "Doctrine\\Persistence\\ObjectRepository",
}
# Methods a repository calls on itself that read its entity's table.
DOCTRINE_SELF_READS = DOCTRINE_FINDERS | {
    "createQueryBuilder",
    "createResultSetMappingBuilder",
}


def _doctrine_finder(method):
    """A built-in or Doctrine 2 magic finder (``findOneByEmail``)."""
    return method in DOCTRINE_FINDERS or bool(
        re.match(r"(?:findBy|findOneBy|countBy)[A-Z]", method or "")
    )


@dataclass(frozen=True)
class DoctrineClass:
    """A PHP class, or an XML ``<entity>`` (``file`` is the XML file and
    ``class_id`` None). ``kind`` is "entity", "mapped" (mapped superclass),
    "embeddable" or None; ``table`` is the literal table name, None when
    unset, PLACEHOLDER when not fixed; ``inheritance`` is the literal
    InheritanceType or PLACEHOLDER."""

    file: str
    class_id: str | None
    path: str
    superclass: str | None
    kind: str | None
    table: str | None
    inheritance: str | None
    repository: str | None  # the entity's repositoryClass
    repository_of: str | None  # parent::__construct($registry, X::class)
    join_tables: tuple  # ((name, line), ...)
    line: int
    column: int
    methods: tuple = ()  # the methods the class declares


@dataclass(frozen=True)
class DoctrineUse:
    """A read or write of an entity (``entity``) or of a repository's entity
    (``repository``), both fully qualified. ``finder`` is the method of a
    finder call on a typed repository, which a repository that declares a
    method of that name answers itself."""

    file: str
    caller_id: str | None
    entity: str | None
    repository: str | None
    reads: bool
    writes: bool
    line: int
    column: int
    finder: str | None = None
    how: str = "call"  # "call", or "dql" when ``entity`` is as DQL writes it


def _doc_annotations(text):
    """[(name, positional args, {key: raw value})] of a docblock's top-level
    ``@Name(...)`` annotations; values keep their quotes."""
    found, i = [], 0
    while True:
        match = re.compile(r"@([A-Za-z_\\][\w\\]*)").search(text, i)
        if match is None:
            return found
        i = match.end()
        j = i
        while j < len(text) and text[j] in " \t":
            j += 1
        args, named = [], {}
        if j < len(text) and text[j] == "(":
            depth, k, quote, start, parts = 0, j, None, j + 1, []
            while k < len(text):
                c = text[k]
                if quote:
                    if c == quote:
                        quote = None
                elif c in "\"'":
                    quote = c
                elif c in "({[":
                    depth += 1
                elif c in ")}]":
                    depth -= 1
                    if depth == 0:
                        parts.append(text[start:k])
                        break
                elif c == "," and depth == 1:
                    parts.append(text[start:k])
                    start = k + 1
                k += 1
            i = k + 1
            for part in parts:
                part = re.sub(r"\s*\*\s*", " ", part).strip()
                key = re.match(r"(\w+)\s*[=:]\s*(.*)$", part, re.S)
                if key:
                    named[key.group(1)] = key.group(2).strip()
                elif part:
                    args.append(part)
        found.append((match.group(1), args, named))


def _quoted(raw):
    """The text of a "..." or '...' annotation value, else None."""
    match = re.fullmatch(r"\"([^\"]*)\"|'([^']*)'", (raw or "").strip())
    return None if match is None else (match.group(1) or match.group(2) or "")


def extract_php_doctrine(tree, content, file, definitions):
    """(classes, uses, renames) of one PHP file: Doctrine mappings of its
    classes, reads and writes of entities and repositories, and whether a
    metadata listener renames tables (``setPrimaryTable``/``setTableName``)."""
    classes, uses = [], []
    renames = False
    namespace, imports = "", {}

    def resolve(written):
        if not written:
            return None
        written = written.strip()
        if written.startswith("\\"):
            return written[1:]
        if written in ("self", "static", "parent"):
            return None
        head, _, rest = written.partition("\\")
        if head in imports:
            return imports[head] + ("\\" + rest if rest else "")
        return f"{namespace}\\{written}" if namespace else written

    def use_clauses(declaration):
        prefix = ""
        for child in declaration.named_children:
            if child.type == "namespace_name":
                prefix = _text(child, content) + "\\"
            elif child.type == "namespace_use_clause":
                add_clause(child, "")
            elif child.type == "namespace_use_group":
                for clause in child.named_children:
                    if clause.type == "namespace_use_clause":
                        add_clause(clause, prefix)

    def add_clause(clause, prefix):
        if any(c.type in ("function", "const") for c in clause.children):
            return
        alias = clause.child_by_field_name("alias")
        target = next(
            (c for c in clause.named_children if c.type in ("qualified_name", "name")),
            None,
        )
        if target is None:
            return
        path = (prefix + _text(target, content)).lstrip("\\")
        local = _text(alias, content) if alias is not None else path.rpartition("\\")[2]
        imports[local] = path

    def mapping(written):
        """The Doctrine mapping class an attribute or annotation names."""
        path = resolve(written)
        if path and path.startswith(DOCTRINE_MAPPING):
            return path[len(DOCTRINE_MAPPING) :]
        # Doctrine 2's simple annotation reader takes unimported names.
        if written in ("Entity", "Table", "MappedSuperclass", "Embeddable",
                       "InheritanceType", "JoinTable") and written not in imports:  # fmt: skip
            return written
        return None

    def class_of(node):
        """The class an ``X::class`` expression names."""
        if node is not None and node.type == "class_constant_access_expression":
            parts = node.named_children
            if len(parts) == 2 and _text(parts[1], content) == "class":
                return resolve(_text(parts[0], content))
        literal = _php_literal(node, content)
        return literal.lstrip("\\") if literal else None

    def type_of(node):
        """The class a parameter or property type names, if one."""
        for child in node.named_children if node is not None else ():
            if child.type == "named_type":
                return resolve(_text(child, content))
            if child.type == "optional_type":
                return type_of(child)
        return None

    def read_class(node):
        nonlocal renames
        name = node.child_by_field_name("name")
        if name is None:
            return
        path = (
            f"{namespace}\\{_text(name, content)}"
            if namespace
            else _text(name, content)
        )
        base = next((c for c in node.named_children if c.type == "base_clause"), None)
        superclass = (
            resolve(_text(base.named_children[0], content))
            if base is not None and base.named_children
            else None
        )
        kind = table = inheritance = repository = repository_of = None
        join_tables = []
        found = []  # (mapping, positional, named) with values as raw text
        for group in _descendants(node, "attribute_list"):
            if group.parent != node:
                continue
            for attribute in _descendants(group, "attribute"):
                written = next(
                    (
                        c
                        for c in attribute.named_children
                        if c.type in ("name", "qualified_name")
                    ),
                    None,
                )
                args, named = [], {}
                arguments = attribute.child_by_field_name("parameters") or next(
                    (c for c in attribute.named_children if c.type == "arguments"), None
                )
                for argument in (
                    arguments.named_children if arguments is not None else ()
                ):
                    if argument.type != "argument":
                        continue
                    key = next(
                        (c for c in argument.named_children if c.type == "name"), None
                    )
                    value = (
                        argument.named_children[-1] if argument.named_children else None
                    )
                    if key is not None and key != value:
                        named[_text(key, content)] = value
                    else:
                        args.append(value)
                found.append(
                    (mapping(_text(written, content) if written else None), args, named)
                )
        previous = node.prev_named_sibling
        docblock = (
            _text(previous, content)
            if previous is not None
            and previous.type == "comment"
            and _text(previous, content).startswith("/**")
            else ""
        )
        for written, args, named in _doc_annotations(docblock):
            found.append((mapping(written), args, named))

        def literal(value):
            if value is None:
                return None
            if isinstance(value, str):
                text = _quoted(value)
                return PLACEHOLDER if text is None else text
            text = _php_literal(value, content)
            return PLACEHOLDER if text is None else text

        def class_value(value):
            if value is None:
                return None
            if isinstance(value, str):
                text = _quoted(value)
                if text is not None:
                    return text.lstrip("\\")
                match = re.fullmatch(r"([\w\\]+)::class", value.strip())
                return resolve(match.group(1)) if match else None
            return class_of(value)

        for which, args, named in found:
            if which == "Entity":
                kind = "entity"
                repository = class_value(named.get("repositoryClass"))
            elif which == "MappedSuperclass":
                kind = kind or "mapped"
                repository = repository or class_value(named.get("repositoryClass"))
            elif which == "Embeddable":
                kind = kind or "embeddable"
            elif which == "Table":
                table = literal(named.get("name", args[0] if args else None))
            elif which == "InheritanceType":
                inheritance = literal(named.get("value", args[0] if args else None))
        body = node.child_by_field_name("body")
        # Join tables on the properties: #[ORM\JoinTable(name: '...')].
        for member in body.named_children if body is not None else ():
            if member.type != "property_declaration":
                continue
            entries = []
            for group in member.named_children:
                if group.type == "attribute_list":
                    for attribute in _descendants(group, "attribute"):
                        written = next(
                            (
                                c
                                for c in attribute.named_children
                                if c.type in ("name", "qualified_name")
                            ),
                            None,
                        )
                        if (
                            written is not None
                            and mapping(_text(written, content)) == "JoinTable"
                        ):
                            arguments = next(
                                (
                                    c
                                    for c in attribute.named_children
                                    if c.type == "arguments"
                                ),
                                None,
                            )
                            for argument in (
                                arguments.named_children if arguments else ()
                            ):
                                key = next(
                                    (
                                        c
                                        for c in argument.named_children
                                        if c.type == "name"
                                    ),
                                    None,
                                )
                                if key is None or _text(key, content) == "name":
                                    entries.append(literal(argument.named_children[-1]))
                                    break
            doc = member.prev_named_sibling
            if doc is not None and doc.type == "comment":
                for written, args, named in _doc_annotations(_text(doc, content)):
                    if mapping(written) == "JoinTable":
                        entries.append(
                            literal(named.get("name", args[0] if args else None))
                        )
            for entry in entries:
                if entry and entry is not PLACEHOLDER:
                    join_tables.append((entry, member.start_point.row + 1))
        for call in _descendants(node, "scoped_call_expression"):
            if (
                _text(call.child_by_field_name("scope"), content) == "parent"
                and _text(call.child_by_field_name("name"), content) == "__construct"
            ):
                for argument in _php_arguments(call)[1:2]:
                    repository_of = class_of(argument)
        classes.append(
            DoctrineClass(
                file,
                _innermost(definitions, file, name),
                path,
                superclass,
                kind,
                table,
                inheritance,
                repository,
                repository_of,
                tuple(join_tables),
                name.start_point.row + 1,
                name.start_point.column,
                tuple(
                    _text(m.child_by_field_name("name"), content)
                    for m in (body.named_children if body is not None else ())
                    if m.type == "method_declaration"
                ),
            )
        )
        # Uses in the class's methods.
        properties = {}
        for member in body.named_children if body is not None else ():
            if member.type == "property_declaration":
                kind_of = type_of(member)
                for element in member.named_children:
                    if element.type == "property_element":
                        variable = (
                            element.child_by_field_name("name")
                            or element.named_children[0]
                        )
                        properties[_text(variable, content).lstrip("$")] = kind_of
            elif member.type == "method_declaration":
                parameters = member.child_by_field_name("parameters")
                for parameter in parameters.named_children if parameters else ():
                    if parameter.type == "property_promotion_parameter":
                        variable = parameter.child_by_field_name("name") or next(
                            (
                                c
                                for c in parameter.named_children
                                if c.type == "variable_name"
                            ),
                            None,
                        )
                        if variable is not None:
                            properties[_text(variable, content).lstrip("$")] = type_of(
                                parameter
                            )
        is_repository = (
            repository_of is not None or superclass in DOCTRINE_REPOSITORY_BASES
        )
        for method in body.named_children if body is not None else ():
            if method.type != "method_declaration":
                continue
            variables = {}
            parameters = method.child_by_field_name("parameters")
            for parameter in parameters.named_children if parameters else ():
                variable = next(
                    (c for c in parameter.named_children if c.type == "variable_name"),
                    None,
                )
                if variable is not None:
                    variables[_text(variable, content)] = type_of(parameter)
            for assignment in _descendants(method, "assignment_expression"):
                left = assignment.child_by_field_name("left")
                right = assignment.child_by_field_name("right")
                if (
                    left is not None
                    and left.type == "variable_name"
                    and right is not None
                ):
                    created = (
                        resolve(
                            _text(
                                right.child_by_field_name("class")
                                or right.named_children[0],
                                content,
                            )
                        )
                        if right.type == "object_creation_expression"
                        and right.named_children
                        else None
                    )
                    name_ = _text(left, content)
                    if name_ in variables and variables[name_] != created:
                        variables[name_] = None
                    else:
                        variables[name_] = created
            walk_method(method, path, is_repository, properties, variables)

    def receiver_type(node, properties, variables):
        if node is None:
            return None
        if node.type == "variable_name":
            return variables.get(_text(node, content))
        if node.type == "member_access_expression":
            target = node.child_by_field_name("object")
            if target is not None and _text(target, content) == "$this":
                return properties.get(_text(node.child_by_field_name("name"), content))
        return None

    def use(call, entity, repository, reads, writes, finder=None, how="call"):
        where = call.child_by_field_name("name") or call
        uses.append(
            DoctrineUse(
                file,
                _innermost(definitions, file, call),
                entity,
                repository,
                reads,
                writes,
                where.start_point.row + 1,
                where.start_point.column,
                finder,
                how,
            )
        )

    def chain(call):
        names, node = [], call
        while node.parent is not None and node.parent.type == "member_call_expression":
            if node.parent.child_by_field_name("object") != node:
                break
            node = node.parent
            names.append(_text(node.child_by_field_name("name"), content))
        return names

    def walk_method(method, path, is_repository, properties, variables):
        nonlocal renames
        for call in _descendants(method, "member_call_expression"):
            name = _text(call.child_by_field_name("name"), content)
            target = call.child_by_field_name("object")
            arguments = _php_arguments(call)
            if name in ("setPrimaryTable", "setTableName"):
                renames = True
            elif name in ("createQuery", "setDQL") and len(arguments) == 1:
                text = _php_sql_text(arguments[0], content, classes=resolve)
                if text is not None:
                    reads, writes = dql_entities(text)
                    for entity in sorted(set(reads) | set(writes)):
                        use(
                            call,
                            entity,
                            None,
                            entity in reads,
                            entity in writes,
                            None,
                            "dql",
                        )
            elif name == "getRepository" and arguments:
                entity = class_of(arguments[0])
                following = chain(call)
                if entity and following and _doctrine_finder(following[0]):
                    use(call, entity, None, True, False)
            elif name in ("from", "update", "delete") and arguments:
                entity = class_of(arguments[0])
                if entity and arguments[0].type == "class_constant_access_expression":
                    use(call, entity, None, name == "from", name != "from")
            elif (
                name in ("find", "getReference", "getPartialReference")
                and len(arguments) >= 2
            ):
                entity = class_of(arguments[0])
                if entity and arguments[0].type == "class_constant_access_expression":
                    use(call, entity, None, True, False)
            elif name in ("persist", "remove") and len(arguments) == 1:
                entity = receiver_type(arguments[0], properties, variables)
                if entity:
                    use(call, entity, None, False, True)
            elif target is not None and _text(target, content) == "$this":
                if is_repository and name in DOCTRINE_SELF_READS:
                    following = chain(call)
                    writes = name == "createQueryBuilder" and any(
                        m in ("update", "delete") for m in following
                    )
                    use(call, None, path, not writes or "select" in following, writes)
            elif _doctrine_finder(name):
                kind = receiver_type(target, properties, variables)
                if kind:
                    use(call, None, kind, True, False, name)

    def walk(node):
        nonlocal namespace
        for child in node.named_children:
            if child.type == "namespace_definition":
                name = child.child_by_field_name("name")
                namespace = _text(name, content) if name is not None else ""
                imports.clear()
                body = child.child_by_field_name("body")
                if body is not None:
                    walk(body)
                continue
            if child.type == "namespace_use_declaration":
                use_clauses(child)
                continue
            if child.type == "class_declaration":
                read_class(child)
                continue
            walk(child)

    walk(tree.root_node)
    return classes, uses, renames


def doctrine_xml(path, content):
    """The DoctrineClass entries of a ``*.orm.xml`` mapping file."""
    import xml.etree.ElementTree as ElementTree

    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return []
    found = []
    for element in root.iter():
        tag = element.tag.rpartition("}")[2]
        kind = {
            "entity": "entity",
            "mapped-superclass": "mapped",
            "embeddable": "embeddable",
        }.get(tag)
        if kind is None or not element.get("name"):
            continue
        joins = tuple(
            (j.get("name"), 0)
            for j in element.iter()
            if j.tag.rpartition("}")[2] == "join-table" and j.get("name")
        )
        found.append(
            DoctrineClass(
                path,
                None,
                element.get("name").lstrip("\\"),
                None,
                kind,
                element.get("table"),
                element.get("inheritance-type"),
                (element.get("repository-class") or "").lstrip("\\") or None,
                None,
                joins,
                0,
                0,
            )
        )
    return found


def doctrine_table_name(class_path, strategy, number_aware=True):
    """A Doctrine naming strategy's ``classToTableName``: "default" keeps the
    short class name; "underscore" lowercases it with ``_`` before each
    capital that follows a lowercase letter (or a digit, number aware)."""
    short = class_path.rpartition("\\")[2]
    if strategy == "default":
        return short
    pattern = r"(?<=[a-z0-9])([A-Z])" if number_aware else r"(?<=[a-z])([A-Z])"
    return re.sub(pattern, r"_\1", short).lower()


def doctrine_strategies(configured, php_sources, majors):
    """The (strategy, number_aware) choices a repository's Doctrine naming
    can be, or UNKNOWN. ``configured`` holds the ``naming_strategy`` service
    ids of the Symfony configuration; without one, a PHP bootstrap's ``new
    UnderscoreNamingStrategy(...)`` passed to ``setNamingStrategy`` counts,
    else DoctrineBundle's and the ORM's default. ``majors`` are the
    doctrine/orm majors in use: plain ``underscore`` is number aware only
    from ORM 3."""
    kinds = set()
    for service in configured:
        kinds.add(
            {
                "doctrine.orm.naming_strategy.default": "default",
                "doctrine.orm.naming_strategy.underscore": "underscore",
                "doctrine.orm.naming_strategy.underscore_number_aware": "number_aware",
            }.get(service, UNKNOWN)
        )
    if not kinds:
        text = "\n".join(php_sources)
        if "setNamingStrategy(" in text:
            created = re.findall(
                r"new\s+\\?(?:[\w\\]+\\)?UnderscoreNamingStrategy\s*\(([^)]*)\)", text
            )
            if len(set(created)) != 1 or "CASE_UPPER" in created[0]:
                return UNKNOWN
            kinds.add(
                "number_aware"
                if re.search(r"\btrue\b", created[0], re.I)
                else "underscore"
            )
        else:
            kinds.add("default")
    if len(kinds) != 1 or UNKNOWN in kinds:
        return UNKNOWN
    kind = kinds.pop()
    if kind == "default":
        return {("default", True)}
    if kind == "number_aware":
        return {("underscore", True)}
    return {("underscore", major >= 3) for major in majors or (2, 3)}


def resolve_doctrine(classes, strategies, renames=False):
    """({path: (DoctrineClass, table, how)}, {path: reason}, {repository
    path: entity path}) of the mapped entities. ``how`` is "explicit" (a
    Table name) or "default" (the naming strategy). Single-table subclasses
    take their root's table; XML mappings take precedence over the class."""
    php, xml, duplicates = {}, {}, set()
    for c in classes:
        target = xml if c.class_id is None and c.file.endswith(".xml") else php
        if c.path in target and target[c.path] != c:
            duplicates.add(c.path)
        target.setdefault(c.path, c)
    merged = {}
    for path in sorted(set(php) | set(xml)):
        own, mapped = php.get(path), xml.get(path)
        if mapped is not None and own is not None:
            mapped = replace(
                mapped,
                file=own.file,
                class_id=own.class_id,
                superclass=own.superclass,
                repository_of=own.repository_of,
                line=own.line,
                column=own.column,
            )
        merged[path] = mapped or own
    repositories = {}
    for c in merged.values():
        for repository, entity in ((c.path, c.repository_of), (c.repository, c.path)):
            if repository and entity:
                repositories.setdefault(repository, set()).add(entity)
    repositories = {r: e.pop() for r, e in repositories.items() if len(e) == 1}
    tables, unresolved = {}, {}

    def own_table(c):
        if c.table is PLACEHOLDER:
            return None, "table"
        if c.table:
            return (c.table, "explicit"), None
        if strategies is UNKNOWN:
            return None, "naming"
        names = {doctrine_table_name(c.path, s, n) for s, n in strategies}
        return ((names.pop(), "default"), None) if len(names) == 1 else (None, "naming")

    for path, c in merged.items():
        if c.kind != "entity":
            continue
        if renames:
            unresolved[path] = "metadata_listener"
            continue
        if path in duplicates:
            unresolved[path] = "duplicate"
            continue
        # The topmost entity ancestor with an inheritance type is the root.
        chain, seen, node = [], {path}, c
        while node.superclass in merged and node.superclass not in seen:
            seen.add(node.superclass)
            node = merged[node.superclass]
            if node.kind == "entity":
                chain.append(node)
        roots = [n for n in chain if n.inheritance]
        root = roots[-1] if roots else (c if c.inheritance else None)
        if root is not None and root.inheritance is PLACEHOLDER:
            unresolved[path] = "inheritance"
            continue
        source = (
            root
            if root is not None and root.inheritance.upper() == "SINGLE_TABLE"
            else c
        )
        found, reason = own_table(source)
        if found is None:
            unresolved[path] = reason
        else:
            tables[path] = (c, table_key(found[0]), found[1])
    return tables, unresolved, repositories


def dql_entities(text):
    """(reads, writes) of a DQL statement: the entity names (fully
    qualified, or ``Bundle:Entity`` aliases) its FROM, JOIN, UPDATE and
    DELETE clauses name. Association paths (``JOIN p.tags t``) are not
    entities, and names holding text the code does not fix are dropped."""
    text = re.sub(r"'(?:[^']|'')*'", "''", text)
    reads, writes = [], []
    for statement in text.split(" ;\n"):
        verb = (re.match(r"\s*(\w+)", statement) or [None, ""])[1].upper()
        refs = re.finditer(
            r"\b(FROM|JOIN|UPDATE|DELETE)\s+(?!FROM\b)"
            r"([A-Za-z_\\][\w\\]*(?::[\w\\]+)?[^\s,()]*)",
            statement,
            re.I,
        )
        for match in refs:
            keyword, name = match.group(1).upper(), match.group(2)
            if "." in name or PLACEHOLDER in name:
                continue
            name = name.lstrip("\\")
            written = (verb == "UPDATE" and keyword == "UPDATE") or (
                verb == "DELETE"
                and keyword in ("DELETE", "FROM")
                and not reads
                and not writes
            )
            (writes if written else reads).append(name)
    return tuple(dict.fromkeys(reads)), tuple(dict.fromkeys(writes))


MONGODB_READS = frozenset(
    "find findOne countDocuments estimatedDocumentCount count distinct "
    "aggregate watch indexes indexExists listIndexes findEach stats".split()
)
MONGODB_WRITES = frozenset(
    "insertOne insertMany insert updateOne updateMany update deleteOne "
    "deleteMany remove replaceOne bulkWrite save createIndex createIndexes "
    "dropIndex dropIndexes drop rename ensureIndex initializeOrderedBulkOp "
    "initializeUnorderedBulkOp".split()
)
_MONGODB_MODULES = ("mongodb", "mongoose", "mongoskin")


@dataclass(frozen=True)
class MongoCollectionUse:
    """A MongoDB driver collection a file opens by its literal name."""

    file: str
    caller_id: str | None
    collection: str
    reads: bool
    writes: bool
    line: int
    column: int


def _mongodb_handle(node, content, names=frozenset()):
    """Whether the expression is a database handle: ``x.db``,
    ``x.connection``, ``x.db(...)``, one of ``names``, or ``a || b`` /
    ``a ?? b`` of handles."""
    node = _unwrap(node)
    if node is None:
        return False
    if node.type == "identifier":
        return _text(node, content) in names
    if node.type == "member_expression":
        prop = _text(node.child_by_field_name("property"), content)
        return prop in ("db", "connection")
    if node.type == "call_expression":
        function = node.child_by_field_name("function")
        return (
            function is not None
            and function.type == "member_expression"
            and _text(function.child_by_field_name("property"), content) == "db"
        )
    if node.type == "binary_expression":
        operator = node.child_by_field_name("operator")
        return (
            operator is not None
            and operator.type in ("||", "??")
            and all(
                _mongodb_handle(node.child_by_field_name(side), content, names)
                for side in ("left", "right")
            )
        )
    return False


def _mongodb_handle_names(root, content, db_types):
    """(names the file binds only to database handles, names it binds to
    anything else). Declarations, plain assignments and ``Db`` type
    annotations bind; a declaration without a value or type binds nothing."""
    bindings = []
    for declarator in _descendants(root, "variable_declarator"):
        target = declarator.child_by_field_name("name")
        if target is None or target.type != "identifier":
            continue
        value = declarator.child_by_field_name("value")
        annotation = declarator.child_by_field_name("type")
        typed = (
            annotation is not None
            and _text(annotation, content).lstrip(": ") in db_types
        )
        if value is None and not typed:
            continue
        bindings.append((_text(target, content), None if typed else value))
    for assignment in _descendants(root, "assignment_expression"):
        target = assignment.child_by_field_name("left")
        if target is not None and target.type == "identifier":
            bindings.append(
                (_text(target, content), assignment.child_by_field_name("right"))
            )
    # Names bound to other handle names settle over a few passes; a name
    # with any binding that is not a handle is never one.
    handles = set()
    for _ in range(3):
        other = {
            name
            for name, value in bindings
            if value is not None and not _mongodb_handle(value, content, handles)
        }
        handles = {name for name, _ in bindings} - other
    return handles, other


def extract_mongodb_collections(tree, content, file, definitions, project=False):
    """``<db>.collection('name')`` calls of the MongoDB driver in a file that
    imports mongodb, mongoose or mongoskin (a ``typeof import('mongoose')``
    type counts), or of a package that depends on one (``project``). The
    receiver must be a database handle the file can see: ``x.db``,
    ``x.db(...)``, ``x.connection``, ``a || b`` of handles, or a name the
    file binds (declarations and assignments) only to handles or types as
    mongodb's ``Db``. A collection assigned to ``this.f`` takes the methods
    the file calls on ``this.f``. The method called on the collection,
    directly or through a ``const`` holding it in the same function, says
    whether it reads or writes."""
    root = tree.root_node
    if b".collection(" not in content:
        return []
    bindings = script_bindings(root, content)
    typed_import = any(
        f"import({q}{module}{q})".encode() in content
        for module in _MONGODB_MODULES
        for q in "'\""
    )
    if (
        not project
        and not typed_import
        and not any(source in _MONGODB_MODULES for source, _ in bindings.values())
    ):
        return []
    db_types = {
        local
        for local, (source, name) in bindings.items()
        if source == "mongodb" and name == "Db"
    }

    def handle(node):
        return _mongodb_handle(node, content)

    # Names bound to a database handle, and every other binding of a name.
    handles, other = _mongodb_handle_names(root, content, db_types)
    for kind in ("required_parameter", "optional_parameter"):
        for parameter in _descendants(root, kind):
            target = parameter.child_by_field_name("pattern")
            if target is None or target.type != "identifier":
                continue
            annotation = parameter.child_by_field_name("type")
            typed = (
                annotation is not None
                and _text(annotation, content).lstrip(": ") in db_types
            )
            (handles if typed else other).add(_text(target, content))
    for parameter in _descendants(root, "formal_parameters"):
        for target in parameter.named_children:
            if target.type == "identifier":
                other.add(_text(target, content))
    handles -= other

    def method_of(call):
        parent = call.parent
        if parent is None or parent.type != "member_expression":
            return None
        outer = parent.parent
        if outer is None or outer.type != "call_expression":
            return None
        return _text(parent.child_by_field_name("property"), content)

    found = []
    for call in _descendants(root, "call_expression"):
        function = call.child_by_field_name("function")
        if function is None or function.type != "member_expression":
            continue
        if _text(function.child_by_field_name("property"), content) != "collection":
            continue
        receiver = _unwrap(function.child_by_field_name("object"))
        named = (
            receiver is not None
            and receiver.type == "identifier"
            and _text(receiver, content) in handles
        )
        if not (handle(receiver) or named):
            continue
        arguments = call.child_by_field_name("arguments")
        args = arguments.named_children if arguments is not None else []
        if len(args) != 1:
            continue
        name = _script_string(args[0], content)
        if not name:
            continue
        methods = set()
        direct = method_of(call)
        if direct is not None:
            methods.add(direct)
        else:
            holder = call.parent
            while holder is not None and holder.type in (
                "await_expression",
                "parenthesized_expression",
                "as_expression",
                "non_null_expression",
            ):
                holder = holder.parent
            if holder is not None and holder.type == "variable_declarator":
                target = holder.child_by_field_name("name")
                scope_node = holder
                while scope_node.parent is not None and scope_node.type not in (
                    "function_declaration",
                    "function_expression",
                    "arrow_function",
                    "method_definition",
                    "program",
                ):
                    scope_node = scope_node.parent
                if target is not None and target.type == "identifier":
                    local = _text(target, content)
                    for use in _descendants(scope_node, "member_expression"):
                        obj = use.child_by_field_name("object")
                        if (
                            obj is not None
                            and obj.type == "identifier"
                            and _text(obj, content) == local
                            and use.parent is not None
                            and use.parent.type == "call_expression"
                            and use.parent.child_by_field_name("function") == use
                        ):
                            methods.add(
                                _text(use.child_by_field_name("property"), content)
                            )
                        elif obj is not None and _text(obj, content) == local:
                            methods.add(None)  # used some other way
                    if not methods:
                        methods.add(None)
            elif (
                holder is not None
                and holder.type == "assignment_expression"
                and holder.child_by_field_name("left") is not None
                and holder.child_by_field_name("left").type == "member_expression"
                and _text(
                    holder.child_by_field_name("left").child_by_field_name("object"),
                    content,
                )
                == "this"
            ):
                # this.f = db.collection('x'): the methods called on this.f
                # anywhere in the file.
                held = _text(holder.child_by_field_name("left"), content)
                for use in _descendants(root, "member_expression"):
                    obj = use.child_by_field_name("object")
                    if obj is None or _text(obj, content) != held:
                        continue
                    if (
                        use.parent is not None
                        and use.parent.type == "call_expression"
                        and use.parent.child_by_field_name("function") == use
                    ):
                        methods.add(_text(use.child_by_field_name("property"), content))
                    else:
                        methods.add(None)
                if not methods:
                    methods.add(None)
            else:
                methods.add(None)
        reads = any(m not in MONGODB_WRITES for m in methods)
        writes = any(m not in MONGODB_READS for m in methods)
        found.append(
            MongoCollectionUse(
                file,
                _innermost(definitions, file, call),
                name,
                reads,
                writes,
                call.start_point.row + 1,
                call.start_point.column,
            )
        )
    return found


# Knex query builder -----------------------------------------------------------
KNEX_MODULES = ("knex",)
# Builder methods whose first argument names a table.
KNEX_TABLE_METHODS = frozenset(
    "table from into join innerJoin leftJoin leftOuterJoin rightJoin "
    "rightOuterJoin fullOuterJoin crossJoin joinRaw".split()
) - {"joinRaw"}
# Builder methods that write to the query's table.
KNEX_WRITES = frozenset(
    "insert update del delete truncate increment decrement upsert merge".split()
)
# Builder methods that read rows, alongside a write on a held query.
KNEX_READS = frozenset(
    "select first pluck count countDistinct min max sum avg distinct".split()
)
KNEX_TYPES = ("Knex", "Knex.Transaction", "Transaction", "Knex.QueryBuilder")


@dataclass(frozen=True)
class KnexUse:
    """A table a Knex query names, or a ``createTable`` that declares one."""

    file: str
    caller_id: str | None
    table: str
    reads: bool
    writes: bool
    declares: bool
    line: int
    column: int


def _unwrap_type(node):
    """The expression without await and parentheses (type assertions kept)."""
    while node is not None and node.type in (
        "await_expression",
        "parenthesized_expression",
    ):
        node = node.named_children[0] if node.named_children else None
    return node


def script_string_constants(root, content):
    """{name: text} of the top-level ``const X = 'text'`` declarations of a
    JavaScript or TypeScript module (exported or not); a name declared
    more than once is left out."""
    found, seen = {}, set()
    for node in root.named_children:
        if node.type == "export_statement":
            node = node.child_by_field_name("declaration") or node
        if node.type != "lexical_declaration" or not any(
            c.type == "const" for c in node.children
        ):
            continue
        for declarator in node.named_children:
            if declarator.type != "variable_declarator":
                continue
            target = declarator.child_by_field_name("name")
            if target is None or target.type != "identifier":
                continue
            name = _text(target, content)
            text = _script_string(
                _unwrap(declarator.child_by_field_name("value")), content
            )
            if name in seen:
                found.pop(name, None)
            elif text:
                found[name] = text
            seen.add(name)
    return found


_SCRIPT_SCOPES = (
    "function_declaration",
    "function_expression",
    "arrow_function",
    "method_definition",
    "generator_function_declaration",
    "class_body",
)


def _script_declares(scope, name, content):
    """Whether a function (or class body) declares ``name`` anywhere in
    it: a parameter, variable, function or class."""
    for node in _descendants(scope, "identifier"):
        if _text(node, content) != name or node.parent is None:
            continue
        parent = node.parent
        if (
            parent.type
            in ("variable_declarator", "function_declaration", "class_declaration")
            and parent.child_by_field_name("name") == node
        ) or parent.type in (
            "formal_parameters",
            "required_parameter",
            "optional_parameter",
            "assignment_pattern",
            "shorthand_property_identifier_pattern",
            "array_pattern",
            "rest_pattern",
        ):
            return True
        if (
            parent.type == "arrow_function"
            and parent.child_by_field_name("parameter") == node
        ):
            return True
    return False


def _script_constant(node, content, constants):
    """The text of the module-level string constant an identifier names,
    when no function or class around it declares that name too."""
    if not constants or node is None or node.type != "identifier":
        return None
    name = _text(node, content)
    text = constants.get(name)
    if text is None:
        return None
    up = node.parent
    while up is not None:
        if up.type in _SCRIPT_SCOPES and _script_declares(up, name, content):
            return None
        up = up.parent
    return text


def _knex_table(name):
    """The table of a Knex table argument: alias (``t as x``) and schema
    dropped; None for an empty name."""
    name = re.split(r"\s+as\s+", name.strip(), maxsplit=1, flags=re.I)[0]
    name = name.rsplit(".", 1)[-1].strip()
    return name or None


def extract_knex(tree, content, file, definitions, project=False, constants=None):
    """Tables of Knex queries in a file that imports knex or lies below a
    package.json depending on it (``project``). A Knex instance is a name
    ``knex`` or a ``.knex`` (or private ``.#knex``) property, a name the file binds to a call of the
    imported knex factory, a parameter typed ``Knex``/``Knex.Transaction``,
    or a parameter of a callback passed to an instance's ``transaction``.
    ``K('t')``, ``K.table('t')`` and ``K.batchInsert('t', ...)`` name a
    table, and so do ``table``/``from``/``into``/``*join`` in a chain rooted
    at an instance (outside ``K.schema``). A chain with ``insert``,
    ``update``, ``del``, ``truncate``, ``increment``, ``decrement``,
    ``upsert`` or ``merge`` (or a query held in a variable that one of
    those is later called on, in the same function) writes its table, and
    also reads it with select, first, pluck or an aggregate; any other
    reads, and a joined table is always read. Names of common table
    expressions the file defines (``.with('x', ...)``) are not tables.
    ``K.schema.createTable('t')`` declares the table. A table argument is a
    string literal, or a name bound to one by a module-level ``const`` of
    the file or imported from one of the project's modules (``constants``),
    which no function around it declares again. A use's position is its
    table argument's."""
    if b"knex" not in content and b"Knex" not in content:
        return []
    root = tree.root_node
    bindings = script_bindings(root, content)
    factories = {
        local for local, (source, _) in bindings.items() if source in KNEX_MODULES
    }
    if not project and not factories:
        return []

    instances = {"knex"}
    for declarator in _descendants(root, "variable_declarator"):
        target = declarator.child_by_field_name("name")
        value = _unwrap(declarator.child_by_field_name("value"))
        if target is None or target.type != "identifier" or value is None:
            continue
        if value.type == "call_expression":
            function = value.child_by_field_name("function")
            if (
                function is not None
                and function.type == "identifier"
                and _text(function, content) in factories
            ):
                instances.add(_text(target, content))
            # require('knex')(config)
            elif (
                function is not None
                and function.type == "call_expression"
                and _text(function, content).replace('"', "'") in ("require('knex')",)
            ):
                instances.add(_text(target, content))

    def knex_type(annotation, aliases=()):
        """Whether a type annotation is Knex, Knex.Transaction,
        Knex.QueryBuilder, a type alias of the file for one of them, or a
        union of them."""
        if annotation is None:
            return False
        text = _text(annotation, content).lstrip(": ").strip()
        return all(
            part.strip() in KNEX_TYPES or part.strip() in aliases
            for part in text.split("|")
        )

    # Type aliases of the file for Knex types (type Connection = Knex |
    # Knex.Transaction), declared once.
    declared = Counter(
        _text(node.child_by_field_name("name"), content)
        for node in _descendants(root, "type_alias_declaration")
        if node.child_by_field_name("name") is not None
    )
    knex_aliases = frozenset(
        _text(node.child_by_field_name("name"), content)
        for node in _descendants(root, "type_alias_declaration")
        if node.child_by_field_name("name") is not None
        and declared[_text(node.child_by_field_name("name"), content)] == 1
        and node.child_by_field_name("type_parameters") is None
        and knex_type(node.child_by_field_name("value"))
    )

    # Fields and properties the file types as Knex: x.f and this.f are
    # instances, and so is a name destructured as f.
    fields = set()
    for kind, name_field in (
        ("required_parameter", "pattern"),
        ("optional_parameter", "pattern"),
        ("property_signature", "name"),
        ("public_field_definition", "name"),
    ):
        for node in _descendants(root, kind):
            target = node.child_by_field_name(name_field)
            if target is None or not knex_type(
                node.child_by_field_name("type"), knex_aliases
            ):
                continue
            if target.type == "identifier" and kind.endswith("_parameter"):
                instances.add(_text(target, content))
            # Properties, and constructor parameter properties
            # (private readonly db: Knex).
            if not kind.endswith("_parameter") or any(
                c.type in ("accessibility_modifier", "readonly")
                or _text(c, content) == "readonly"
                for c in node.children
            ):
                fields.add(_text(target, content))
    for declarator in _descendants(root, "variable_declarator"):
        target = declarator.child_by_field_name("name")
        value = _unwrap_type(declarator.child_by_field_name("value"))
        if (
            target is not None
            and target.type == "identifier"
            and value is not None
            and value.type == "as_expression"
            and knex_type(value.named_children[-1], knex_aliases)
        ):
            instances.add(_text(target, content))
    for pattern in _descendants(root, "shorthand_property_identifier_pattern"):
        if _text(pattern, content) in fields:
            instances.add(_text(pattern, content))
    instances -= factories - {"knex"}

    def instance(node):
        node = _unwrap(node)
        if node is None:
            return False
        if node.type == "identifier":
            return _text(node, content) in instances
        if node.type == "member_expression":
            prop = _text(node.child_by_field_name("property"), content)
            return prop in ("knex", "#knex") or prop in fields
        return False

    # Parameters of callbacks passed to K.transaction(...).
    for call in _descendants(root, "call_expression"):
        function = call.child_by_field_name("function")
        if (
            function is None
            or function.type != "member_expression"
            or _text(function.child_by_field_name("property"), content) != "transaction"
            or not instance(function.child_by_field_name("object"))
        ):
            continue
        arguments = call.child_by_field_name("arguments")
        for callback in arguments.named_children if arguments is not None else []:
            if callback.type not in ("arrow_function", "function_expression"):
                continue
            parameters = callback.child_by_field_name(
                "parameters"
            ) or callback.child_by_field_name("parameter")
            if parameters is None:
                continue
            first = (
                parameters
                if parameters.type == "identifier"
                else next(iter(parameters.named_children), None)
            )
            if first is not None and first.type in (
                "required_parameter",
                "optional_parameter",
            ):
                first = first.child_by_field_name("pattern")
            if first is not None and first.type == "identifier":
                instances.add(_text(first, content))

    def method(call):
        function = call.child_by_field_name("function")
        if function is not None and function.type == "member_expression":
            return _text(function.child_by_field_name("property"), content)
        return None

    def chain_root(node):
        """(root expression, methods called below it) of a builder chain,
        or None when the chain passes through ``.schema``."""
        methods = []
        node = _unwrap(node)
        while node is not None:
            if instance(node):
                return node, methods
            if node.type == "call_expression":
                function = _unwrap(node.child_by_field_name("function"))
                if instance(function):
                    return function, methods
                node = function
            elif node.type == "member_expression":
                prop = _text(node.child_by_field_name("property"), content)
                if prop == "schema":
                    return None
                methods.append(prop)
                node = _unwrap(node.child_by_field_name("object"))
            else:
                return None
        return None

    def chain_above(call):
        """The methods called on the chain above this call, and its top."""
        methods, top = [], call
        while True:
            parent = top.parent
            while parent is not None and parent.type in (
                "await_expression",
                "parenthesized_expression",
                "non_null_expression",
                "as_expression",
            ):
                top, parent = parent, parent.parent
            if (
                parent is not None
                and parent.type == "member_expression"
                and parent.parent is not None
                and parent.parent.type == "call_expression"
                and parent.parent.child_by_field_name("function") == parent
            ):
                methods.append(_text(parent.child_by_field_name("property"), content))
                top = parent.parent
            else:
                return methods, top

    def held_methods(top):
        """Methods later called on a variable a query is assigned to, in the
        same function; None when the query is not held."""
        holder = top.parent
        if holder is None or holder.type != "variable_declarator":
            return None
        target = holder.child_by_field_name("name")
        if target is None or target.type != "identifier":
            return None
        local = _text(target, content)
        scope = holder
        while scope.parent is not None and scope.type not in (
            "function_declaration",
            "function_expression",
            "arrow_function",
            "method_definition",
            "program",
        ):
            scope = scope.parent
        found = set()
        for use in _descendants(scope, "member_expression"):
            obj = use.child_by_field_name("object")
            if (
                obj is not None
                and obj.type == "identifier"
                and (_text(obj, content) == local)
            ):
                outer = use
                while (
                    outer.parent is not None
                    and outer.parent.type == "call_expression"
                    and outer.parent.child_by_field_name("function") == outer
                ):
                    found.add(_text(outer.child_by_field_name("property"), content))
                    call = outer.parent
                    if (
                        call.parent is not None
                        and call.parent.type == "member_expression"
                    ):
                        outer = call.parent
                    else:
                        break
        return found

    # Common table expressions the file names (.with('x', ...)) are not
    # tables.
    ctes = set()
    for call in _descendants(root, "call_expression"):
        if method(call) in ("with", "withRecursive", "withMaterialized"):
            arguments = call.child_by_field_name("arguments")
            first = next(iter(arguments.named_children), None) if arguments else None
            name = _script_string(first, content)
            if name:
                ctes.add(name.strip().lower())

    found = []
    for call in _descendants(root, "call_expression"):
        function = _unwrap(call.child_by_field_name("function"))
        arguments = call.child_by_field_name("arguments")
        args = arguments.named_children if arguments is not None else []
        if not args or function is None:
            continue
        name = _script_string(args[0], content) or _script_constant(
            args[0], content, constants
        )
        if not name:
            continue
        declares, joined = False, False
        if instance(function):
            below = []
        elif function.type == "member_expression":
            prop = _text(function.child_by_field_name("property"), content)
            obj = _unwrap(function.child_by_field_name("object"))
            if prop in ("createTable", "createTableIfNotExists"):
                schema = obj
                while (
                    schema is not None
                    and schema.type == "call_expression"
                    and method(schema) == "withSchema"
                ):
                    schema = _unwrap(
                        schema.child_by_field_name("function").child_by_field_name(
                            "object"
                        )
                    )
                if not (
                    schema is not None
                    and schema.type == "member_expression"
                    and _text(schema.child_by_field_name("property"), content)
                    == "schema"
                    and instance(schema.child_by_field_name("object"))
                ):
                    continue
                declares, below = True, []
            elif prop == "batchInsert" and instance(obj):
                below = ["insert"]
            elif prop in KNEX_TABLE_METHODS:
                rooted = chain_root(obj)
                if rooted is None:
                    continue
                below = rooted[1]
                joined = prop not in ("table", "from", "into")
            else:
                continue
        else:
            continue
        table = _knex_table(name)
        if table is None or (not declares and table.lower() in ctes):
            continue
        above, top = chain_above(call)
        methods = set(below) | set(above)
        held = None if declares else held_methods(top)
        if held:
            methods |= held
        # A joined table is read whatever the query does to its own table.
        writes = not declares and not joined and bool(methods & KNEX_WRITES)
        reads = not declares and (not writes or bool(methods & KNEX_READS))
        found.append(
            KnexUse(
                file,
                _innermost(definitions, file, call),
                table,
                reads,
                writes,
                declares,
                args[0].start_point.row + 1,
                args[0].start_point.column,
            )
        )
    return found


# Node SQL drivers and query builders whose query methods take SQL text.
SCRIPT_SQL_MODULES = (
    "pg",
    "pg-pool",
    "pg-promise",
    "mysql",
    "mysql2",
    "mysql2/promise",
    "mariadb",
    "sqlite3",
    "sqlite",
    "better-sqlite3",
    "mssql",
    "tedious",
    "oracledb",
    "@neondatabase/serverless",
    "@vercel/postgres",
    "@libsql/client",
    "@planetscale/database",
    "knex",
)
# Their methods whose first argument is SQL (or pg's {text} config).
SCRIPT_SQL_METHODS = {
    "query",
    "execute",
    "raw",
    "prepare",
    "exec",
    "run",
    "all",
    "get",
    "each",
    "none",
    "one",
    "oneOrNone",
    "many",
    "manyOrNone",
    "any",
}


def _script_sql_text(node, content, depth=0):
    """The SQL text an argument fixes: string literals, template strings
    and ``+`` concatenation, with PLACEHOLDER for what the code does not
    fix; pg's ``{ text: ... }`` config reads its text. None when the
    argument is no text at all."""
    if node is None or depth > 16:
        return None
    if node.type == "parenthesized_expression" and node.named_children:
        return _script_sql_text(node.named_children[0], content, depth + 1)
    if node.type == "string":
        return _script_string(node, content)
    if node.type == "template_string":
        parts = []
        for child in node.named_children:
            if child.type in ("string_fragment", "escape_sequence"):
                parts.append(_text(child, content))
            elif child.type == "template_substitution":
                parts.append(PLACEHOLDER)
        if not node.named_children:
            # Older grammars hold the text between the backticks.
            return _text(node, content)[1:-1]
        return "".join(parts)
    if node.type == "binary_expression":
        if _text(node.child_by_field_name("operator"), content) != "+":
            return None
        left = _script_sql_text(node.child_by_field_name("left"), content, depth + 1)
        right = _script_sql_text(node.child_by_field_name("right"), content, depth + 1)
        if left is None and right is None:
            return None
        return (left if left is not None else PLACEHOLDER) + (
            right if right is not None else PLACEHOLDER
        )
    if node.type == "object":
        for pair in node.named_children:
            if (
                pair.type == "pair"
                and _script_key(pair.child_by_field_name("key"), content) == "text"
            ):
                return _script_sql_text(
                    pair.child_by_field_name("value"), content, depth + 1
                )
    return None


# scip-typescript symbols of TypeORM's raw query methods.
TYPEORM_QUERY_SYMBOL = re.compile(
    r"scip-typescript npm typeorm \S+ .*/"
    r"(Repository|TreeRepository|EntityManager|DataSource|Connection|QueryRunner)"
    r"#query\(\)\."
)


def extract_script_sql(tree, content, file, definitions, references=None):
    """SQL that one JavaScript or TypeScript file passes to a Node driver's
    query method, in a file importing a SQL driver or knex
    (``script-sql``: the receiver's type is not checked), or to TypeORM's
    ``query`` where a scip-typescript index (``references``) resolves the
    method to it (``typeorm-sql``). The text must start with a statement
    verb."""
    root = tree.root_node
    bindings = script_bindings(root, content)
    driver = any(source in SCRIPT_SQL_MODULES for source, _ in bindings.values())
    covered = references is not None and references.covers(file)
    if not driver and not covered:
        return []
    found = []
    for call in _descendants(root, "call_expression"):
        function = call.child_by_field_name("function")
        if function is None or function.type != "member_expression":
            continue
        name = function.child_by_field_name("property")
        if name is None or _text(name, content) not in SCRIPT_SQL_METHODS:
            continue
        how = "script-sql"
        if covered and _text(name, content) == "query":
            called = references.symbols(
                file, name.start_point.row + 1, name.start_point.column
            )
            if any(TYPEORM_QUERY_SYMBOL.fullmatch(c) for c in called or ()):
                how = "typeorm-sql"
        if how == "script-sql" and not driver:
            continue
        arguments = call.child_by_field_name("arguments")
        first = (
            next((c for c in arguments.named_children if c.type != "comment"), None)
            if arguments is not None
            else None
        )
        text = _script_sql_text(first, content)
        if text is None:
            continue
        words = re.findall(r"[A-Za-z_]+", _COMMENT_OR_STRING.sub(_blank, text))
        if not words or words[0].lower() not in SQL_VERBS:
            continue  # Not a statement that reads or writes tables.
        # Driver parameter markers ($1, ?, :name, @name) never name a table.
        statement = statement_tables(re.sub(r"\$\d+", "?", text))
        if not (statement.reads or statement.writes) and PLACEHOLDER in text:
            statement = None
        found.append(
            SqlUse(
                file,
                _innermost(definitions, file, call),
                how,
                False,
                statement,
                call.start_point.row + 1,
                call.start_point.column,
            )
        )
    return found


# Kysely query builder ---------------------------------------------------------
KYSELY_MODULE = "kysely"
# Kysely methods whose first argument names tables: (reads, writes).
KYSELY_METHODS = {
    **dict.fromkeys(
        ("insertInto", "replaceInto", "updateTable", "mergeInto"), (False, True)
    ),
    "deleteFrom": (False, True),
    **dict.fromkeys(
        (
            "selectFrom",
            "innerJoin",
            "leftJoin",
            "rightJoin",
            "fullJoin",
            "crossJoin",
            "innerJoinLateral",
            "leftJoinLateral",
        ),
        (True, False),
    ),
}
# Kysely methods that name common table expressions, not tables.
KYSELY_CTE_METHODS = (
    "with",
    "withRecursive",
    "withMaterialized",
    "withNotMaterialized",
)
# The statements a query chain sits in, for common table expression names.
_SCRIPT_STATEMENTS = (
    "expression_statement",
    "return_statement",
    "lexical_declaration",
    "variable_declaration",
    "export_statement",
)


@dataclass(frozen=True)
class KyselyCall:
    """A call of a Kysely table method with literal table names."""

    file: str
    caller_id: str | None
    method: str
    tables: tuple[str, ...]
    line: int  # where the method's name is written (byte column)
    column: int


def _kysely_names(argument, content):
    """Table names of a Kysely table argument: a string literal (``'t as
    x'``) or an array of them; None when any is not a literal."""
    items = [argument]
    if argument.type == "array":
        items = argument.named_children
    names = []
    for item in items:
        text = _script_string(item, content)
        if text is None:
            return None
        name = _knex_table(text)
        if name is None:
            return None
        names.append(name)
    return tuple(names) or None


def extract_kysely(tree, content, file, definitions):
    """Calls ``x.m('t', ...)`` of a method Kysely names tables with
    (``KYSELY_METHODS``) whose first argument is a string literal (alias
    dropped) or an array of them. A table read (``selectFrom``, joins)
    that a ``with``/``withRecursive`` in the same statement declares as a
    common table expression is left out, outside that CTE's own body. The receiver is not checked here: the caller keeps a call only
    where a SCIP index resolves the method to Kysely's own, or, in a file
    no index covers, where the file imports ``kysely``."""
    if b"selectFrom" not in content and not any(
        m.encode() in content for m in KYSELY_METHODS
    ):
        return []
    calls, ctes = [], defaultdict(list)
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if (
            function is None
            or function.type != "member_expression"
            or arguments is None
        ):
            continue
        name = function.child_by_field_name("property")
        method = _text(name, content)
        first = next((c for c in arguments.named_children if c.type != "comment"), None)
        if first is None:
            continue
        statement = node
        while statement.parent is not None and statement.type not in _SCRIPT_STATEMENTS:
            statement = statement.parent
        if method in KYSELY_CTE_METHODS:
            text = _script_string(first, content)
            if text is None and first.type == "arrow_function":
                # with((cte) => cte('name').materialized(), ...)
                inner = [first.child_by_field_name("body")]
                while inner and text is None:
                    item = inner.pop()
                    if item is None:
                        continue
                    if item.type == "call_expression":
                        callee = item.child_by_field_name("function")
                        args = item.child_by_field_name("arguments")
                        if callee is not None and callee.type == "identifier" and args:
                            text = next(
                                (
                                    _script_string(a, content)
                                    for a in args.named_children
                                    if a.type == "string"
                                ),
                                None,
                            )
                    inner.extend(item.named_children)
            if text:
                ctes[None].append(
                    (
                        text.split("(", 1)[0].strip(),
                        statement.start_byte,
                        statement.end_byte,
                        arguments.start_byte,
                        # A recursive CTE's body names the CTE itself.
                        arguments.start_byte
                        if method == "withRecursive"
                        else arguments.end_byte,
                    )
                )
            continue
        if method not in KYSELY_METHODS:
            continue
        tables = _kysely_names(first, content)
        if tables is None:
            continue
        calls.append(
            (
                node.start_byte,
                KyselyCall(
                    file,
                    _innermost(definitions, file, node),
                    method,
                    tables,
                    name.start_point.row + 1,
                    name.start_point.column,
                ),
            )
        )
    found = []
    for start, call in calls:
        reads, _ = KYSELY_METHODS[call.method]
        # A write names a table (SQL writes no CTE), and so does a read in
        # the CTE's own (non-recursive) body.
        tables = tuple(
            t
            for t in call.tables
            if not reads
            or not any(
                name == t and first <= start < last and not (begin <= start < end)
                for name, first, last, begin, end in ctes[None]
            )
        )
        if tables:
            found.append(replace(call, tables=tables))
    return found


def imports_kysely(content):
    """Whether a script imports the kysely package (or one of its paths)."""
    return bool(
        re.search(rb"""(?:from|require\()\s*['"]kysely(?:/[^'"]*)?['"]""", content)
    )
