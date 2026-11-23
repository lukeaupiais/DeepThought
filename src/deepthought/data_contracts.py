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
Datasource configuration is not read, so two packages sharing one database
are not joined, and SQL run from strings is not read.
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
