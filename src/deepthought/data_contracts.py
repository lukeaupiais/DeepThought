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

from dataclasses import dataclass
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
