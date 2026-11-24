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

from dataclasses import dataclass, replace
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

_NAME = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*)'
_CREATE_TABLE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:GLOBAL|LOCAL)\s+)?"
    r"(TEMP(?:ORARY)?\s+)?(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    rf"({_NAME}(?:\s*\.\s*{_NAME}){{0,2}})",
    re.IGNORECASE,
)
_COMMENT_OR_STRING = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'", re.DOTALL)


@dataclass(frozen=True)
class Ddl:
    file: str
    line: int
    name: str
    schema: str | None = None


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


def _blank(match):
    return re.sub(r"[^\n]", " ", match.group(0))


def extract_ddl(file, content: bytes):
    """The permanent tables a SQL file's CREATE TABLE statements declare."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    text = _COMMENT_OR_STRING.sub(_blank, text)
    found = []
    for match in _CREATE_TABLE.finditer(text):
        if match.group(1):
            continue  # Temporary tables are not part of the schema.
        parts = [p.strip() for p in re.split(rf"\s*\.\s*(?={_NAME})", match.group(2))]
        found.append(
            Ddl(
                file,
                text.count("\n", 0, match.start()) + 1,
                table_key(parts[-1]),
                table_key(parts[-2]) if len(parts) > 1 else None,
            )
        )
    return found


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


def _word(token):
    return token.lower() if token[:1].isalpha() or token[:1] == "_" else None


def _is_name(token):
    return token[:1] in '"`[' or (
        (token[:1].isalpha() or token[:1] in "_\x00")
        and token.lower() not in _SQL_KEYWORDS
    )


def statement_tables(text, jpql=False):
    """The tables a SQL statement (or entities a JPQL one) names after FROM,
    JOIN, USING, INTO, UPDATE and TRUNCATE.

    INSERT/MERGE/REPLACE INTO, UPDATE, DELETE FROM and TRUNCATE targets are
    written; every other table is read. Comments, string literals, common
    table expression names, subqueries' own parentheses, function calls in
    FROM (a name followed by ``(``) and FROM inside function arguments
    (``EXTRACT(YEAR FROM d)``) are skipped. A name holding text the code
    does not fix (PLACEHOLDER) is dropped. In SQL ``schema.table`` gives
    ``table``; in JPQL a dotted name is a path (``o.pets``), not an entity.
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
    how: str  # spring-data-query | jpa-query | jdbc-sql | jdbc-insert-table
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
    if not (persistence or jdbc or _imports_package(imports, _SPRING_DATA)):
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
                if written.rpartition(".")[2] != "Query" or not _from(
                    imports, written, (_SPRING_DATA,)
                ):
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
        if (
            target is None
            or target.type != "identifier"
            or name is None
            or _text(name, content) != "objects"
        ):
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
                node.start_point.row + 1,
                node.start_point.column,
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
    how: str  # "query" (Session.query) or "statement" (select/insert/...)
    line: int
    column: int


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


def _from_sqlalchemy(origin):
    return origin is not None and any(
        origin == p or origin.startswith(p + ".") for p in _SQLALCHEMY
    )


def extract_sqlalchemy(tree, content, file, definitions):
    """(mappings, tables, accesses) of one Python file that imports SQLAlchemy."""
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

    mappings, tables, accesses = [], [], []
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
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
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
        if called in SQLALCHEMY_READS | SQLALCHEMY_WRITES:
            how, writes = "statement", called in SQLALCHEMY_WRITES
        elif (
            function.type == "attribute"
            and _text(function.child_by_field_name("attribute"), content) == "query"
        ):
            how, writes = "query", False
        else:
            continue
        for argument in positional[:1] if writes else positional:
            while argument.type == "attribute":
                argument = argument.child_by_field_name("object")
            if argument.type == "identifier":
                accesses.append(
                    SaAccess(
                        file,
                        _innermost(definitions, file, node),
                        _text(argument, content),
                        writes,
                        how,
                        node.start_point.row + 1,
                        node.start_point.column,
                    )
                )
    return mappings, tables, accesses


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

_PRISMA_MODEL = re.compile(r"^[ \t]*model[ \t]+([A-Za-z_]\w*)[ \t]*\{", re.MULTILINE)
_PRISMA_MAP = re.compile(r'@@map\(\s*(?:name\s*:\s*)?"([^"]+)"\s*\)')
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


def extract_prisma_schema(file, content: bytes):
    """The models a Prisma schema declares, each with its table: the
    ``@@map`` name, else the model name. None if the file is not UTF-8."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # Comments (// and ///) blanked; Prisma has no block comments.
    text = re.sub(
        r'"(?:[^"\\\n]|\\.)*"|//[^\n]*',
        lambda m: m.group(0) if m.group(0)[0] == '"' else _blank(m),
        text,
    )
    found = []
    for match in _PRISMA_MODEL.finditer(text):
        end = re.search(r"^[ \t]*\}", text[match.end() :], re.MULTILINE)
        body = text[match.end() : match.end() + end.start()] if end else ""
        mapped = _PRISMA_MAP.search(body)
        found.append(
            PrismaModel(
                file,
                match.group(1),
                mapped.group(1) if mapped else match.group(1),
                mapped is not None,
                text.count("\n", 0, match.start()) + 1,
            )
        )
    return found


def prisma_accessor(model):
    """The Prisma Client property of a model: its name, first letter lower."""
    return model[:1].lower() + model[1:]


def typeorm_table_name(entity):
    """TypeORM's DefaultNamingStrategy table name: snakeCase of the class
    name (``UserProfile`` is ``user_profile``, ``HTTPLog`` is ``http_log``)."""
    name = re.sub(r"([A-Z])([A-Z])([a-z])", r"\1_\2\3", entity)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()


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


def _call_parts(call, content):
    """(callee name, receiver node or None, arguments) of a call."""
    function = call.child_by_field_name("function")
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
    """(mappings, uses) of one TypeScript or JavaScript file: classes
    decorated with TypeORM's ``@Entity`` and the entity classes passed to
    ``getRepository`` or ``@InjectRepository``."""
    root = tree.root_node
    imports = _script_imports(root, content)
    typeorm = {
        local: name for local, (module, name) in imports.items() if module in _TYPEORM
    }
    if not typeorm:
        return [], []
    mappings, uses = [], []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.named_children))
        if node.type == "class_declaration":
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
                        table_key(name) if name else typeorm_table_name(entity),
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
            if args[0].type != "identifier":
                continue
            uses.append(
                ModelUse(
                    file,
                    _innermost(definitions, file, node),
                    _text(args[0], content),
                    True,
                    True,
                    node.start_point.row + 1,
                    node.start_point.column,
                )
            )
    return mappings, uses


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
            )
        )
    return found


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
        if node.type != "call":
            return
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
        # full_table_name_prefix: the innermost enclosing module that
        # defines table_name_prefix; an enclosing ActiveRecord class has none.
        prefix = ""
        while module:
            enclosing = "::".join(module)
            if enclosing in prefixes:
                prefix = prefixes[enclosing]
                break
            if is_model(enclosing):
                break
            module.pop()
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
