"""GraphQL root fields: where a schema declares them and which code resolves them.

Read here:

- NestJS resolvers: in a TypeScript file importing from ``@nestjs/graphql``,
  a method decorated ``@Query``, ``@Mutation`` or ``@Subscription`` of a
  class decorated ``@Resolver``, all imported from that package, resolves
  the root field named by the decorator's string argument, by the ``name``
  of its options object, or else by the method's name;
- schema files (``*.graphql``, ``*.gql``): the fields of ``type Query``,
  ``type Mutation`` and ``type Subscription`` and their ``extend type``
  blocks.

Root fields are scoped to the Node package (the nearest ``package.json``)
of the file: one GraphQL server per package. A schema that renames its root
types (``schema { query: Root }``) is counted as unread, as is a resolver
whose field name is not literal.
"""

from dataclasses import dataclass
import re

from .folding import text
from .model import Unread
from .web import _js_modules, _js_object, _walk, _Context

ROOT_TYPES = ("Query", "Mutation", "Subscription")
_NEST_GRAPHQL = "@nestjs/graphql"


@dataclass(frozen=True)
class GraphqlResolver:
    file: str
    line: int
    column: int
    handler_id: str | None
    operation: str  # Query | Mutation | Subscription
    field: str
    framework: str


@dataclass(frozen=True)
class GraphqlDeclaration:
    file: str
    line: int
    operation: str
    field: str


def _decorator(node, modules, content):
    call = node.named_children[0] if node.named_children else None
    if call is None or call.type != "call_expression":
        return None, []
    function = call.child_by_field_name("function")
    if function is None or function.type != "identifier":
        return None, []
    origin = modules.get(text(function, content), "")
    if not origin.startswith(_NEST_GRAPHQL + "."):
        return None, []
    arguments = call.child_by_field_name("arguments")
    found = [] if arguments is None else arguments.named_children
    return origin.rpartition(".")[2], [c for c in found if c.type != "comment"]


def extract_nest_resolvers(tree, content, repository_id, file, definitions, language):
    """Root-field resolvers and unread resolvers of one TypeScript file."""
    context = _Context(tree, content, repository_id, file, definitions, language)
    modules = _js_modules(context.root, content)
    resolvers, unread = [], []
    for node in _walk(context.root):
        if node.type != "class_declaration":
            continue
        decorators = [c for c in node.named_children if c.type == "decorator"]
        if node.parent is not None and node.parent.type == "export_statement":
            decorators += [
                c for c in node.parent.named_children if c.type == "decorator"
            ]
        if not any(
            _decorator(d, modules, content)[0] == "Resolver" for d in decorators
        ):
            continue
        body = node.child_by_field_name("body")
        class_name = text(node.child_by_field_name("name"), content)
        pending = []
        for member in [] if body is None else body.named_children:
            if member.type == "decorator":
                pending.append(member)
                continue
            if member.type != "method_definition":
                pending = []
                continue
            decorators, pending = pending, []
            method_name = text(member.child_by_field_name("name"), content)
            handler = context.definition_of(member) or context.named_function(
                f"{class_name}.{method_name}"
            )
            for item in decorators:
                operation, arguments = _decorator(item, modules, content)
                if operation not in ROOT_TYPES:
                    continue
                line = item.start_point.row + 1
                field = method_name
                if arguments and arguments[0].type not in (
                    "arrow_function",
                    "function_expression",
                    "function",
                    "object",
                ):
                    # A name: a string, a constant or anything else (unread).
                    field = context.folder.evaluate(arguments[0])
                else:
                    for argument in arguments:
                        options = _js_object(argument, content)
                        if "name" in options:
                            field = context.folder.evaluate(options["name"])
                if not re.fullmatch(r"[_A-Za-z]\w*", field):
                    unread.append(Unread(file, line, "graphql", "field_not_literal"))
                    continue
                resolvers.append(
                    GraphqlResolver(
                        file,
                        line,
                        item.start_point.column,
                        handler,
                        operation,
                        field,
                        "nestjs-graphql",
                    )
                )
    return resolvers, unread


def _blank(match):
    return re.sub(r"[^\n]", " ", match.group(0))


def schema_fields(source, file):
    """Root fields a GraphQL schema file declares, and unread facts."""
    # Descriptions, strings and comments carry no fields; keep line numbers.
    stripped = re.sub(r'"""[\s\S]*?"""|"(?:[^"\\\n]|\\.)*"|#[^\n]*', _blank, source)
    # A directive's name is not a field; its arguments are skipped by depth.
    stripped = re.sub(r"@[_A-Za-z]\w*", _blank, stripped)
    if re.search(r"\b(schema|extend\s+schema)\s*(@\w+[^{]*)?\{", stripped):
        return [], [Unread(file, 1, "graphql", "schema_root_renamed")]
    declared = []
    for match in re.finditer(
        r"\btype\s+(Query|Mutation|Subscription)\b[^{]*\{",
        stripped,
    ):
        operation = match.group(1)
        depth, index = 0, match.end()
        start = index
        while index < len(stripped):
            char = stripped[index]
            if char in "([":
                depth += 1
            elif char in ")]":
                depth -= 1
            elif char == "}" and depth == 0:
                break
            index += 1
        body = stripped[start:index]
        depth = 0
        position = 0
        for token in re.finditer(r"[()\[\]]|[_A-Za-z]\w*\s*(?=[(:])", body):
            value = token.group(0)
            if value in "([":
                depth += 1
                continue
            if value in ")]":
                depth -= 1
                continue
            if depth != 0:
                continue
            # A name followed by ':' or '(' at depth 0 starts a field, unless
            # it is the type after a previous field's ':'.
            before = body[position : token.start()].rstrip()
            position = token.end()
            if before.endswith(":"):
                continue
            line = stripped.count("\n", 0, start + token.start()) + 1
            declared.append(GraphqlDeclaration(file, line, operation, value.strip()))
    return declared, []
