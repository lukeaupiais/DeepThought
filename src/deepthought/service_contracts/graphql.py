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
whose field name is not literal. JavaScript and TypeScript files of a
package that depends on a server taking resolver maps add two more sources:
the members of ``{Query: {...}}`` objects resolve fields, and GraphQL
templates (tagged ``gql``/``graphql``, marked ``/* GraphQL */`` or starting
``#graphql``) declare them.
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
    # A schema definition that names its root types other than Query,
    # Mutation and Subscription renames them: nothing here is read then.
    for match in re.finditer(r"\bschema\s*\{([^}]*)\}", stripped):
        roots = re.findall(
            r"\b(query|mutation|subscription)\s*:\s*(\w+)", match.group(1)
        )
        if any(name != operation.capitalize() for operation, name in roots):
            line = stripped.count("\n", 0, match.start()) + 1
            return [], [Unread(file, line, "graphql", "schema_root_renamed")]
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


# Node packages whose servers take a resolver map: {Query: {field: fn}}.
RESOLVER_MAP_SERVERS = frozenset(
    {
        "@apollo/server",
        "@apollo/subgraph",
        "@graphql-tools/schema",
        "apollo-server",
        "apollo-server-express",
        "apollo-server-fastify",
        "apollo-server-hapi",
        "apollo-server-koa",
        "apollo-server-lambda",
        "graphql-tools",
        "graphql-yoga",
        "mercurius",
    }
)
_FUNCTIONS = ("arrow_function", "function_expression", "function")


def _key_name(key, content):
    """The literal name of an object key, or None."""
    if key is None:
        return None
    if key.type in ("property_identifier", "identifier"):
        return text(key, content)
    if key.type == "string":
        return text(key, content)[1:-1]
    return None


def extract_resolver_maps(tree, content, repository_id, file, definitions, language):
    """Root fields of the resolver maps of one JavaScript or TypeScript file:
    the members of an object literal under a ``Query``, ``Mutation`` or
    ``Subscription`` key. The caller reads only the files of Node packages
    that depend on a server taking such maps."""
    context = _Context(tree, content, repository_id, file, definitions, language)
    resolvers, unread = [], []
    for node in _walk(context.root):
        if node.type != "pair":
            continue
        operation = _key_name(node.child_by_field_name("key"), content)
        if operation not in ROOT_TYPES:
            continue
        # A map stands alone or is the server's `resolvers` option. Other
        # nested maps with the same keys are not resolvers: Apollo Client's
        # {typePolicies: {Query}} and {cacheRedirects: {Query}}.
        outer = node.parent.parent if node.parent is not None else None
        if outer is not None and outer.type == "pair":
            if _key_name(outer.child_by_field_name("key"), content) != "resolvers":
                continue
        value = node.child_by_field_name("value")
        line = node.start_point.row + 1
        if value is None or value.type != "object":
            unread.append(Unread(file, line, "graphql", "resolver_map_not_literal"))
            continue
        for member in value.named_children:
            member_line = member.start_point.row + 1
            if member.type == "comment":
                continue
            if member.type == "method_definition":
                field = _key_name(member.child_by_field_name("name"), content)
                handler = context.definition_of(member)
            elif member.type == "shorthand_property_identifier":
                field = text(member, content)
                handler = context.named_function(field)
            elif member.type == "pair":
                field = _key_name(member.child_by_field_name("key"), content)
                found = member.child_by_field_name("value")
                if found is not None and found.type == "object":
                    # A subscription or a field with options: {subscribe, resolve}.
                    options = _js_object(found, content)
                    found = options.get("subscribe") or options.get("resolve")
                if found is None:
                    handler = None
                elif found.type in _FUNCTIONS:
                    handler = context.definition_of(found)
                elif found.type == "identifier":
                    handler = context.named_function(text(found, content))
                else:
                    handler = None
            else:  # a spread or anything else: fields not listed here
                unread.append(Unread(file, member_line, "graphql", "resolver_spread"))
                continue
            if field is None or not re.fullmatch(r"[_A-Za-z]\w*", field):
                unread.append(Unread(file, member_line, "graphql", "field_not_literal"))
                continue
            resolvers.append(
                GraphqlResolver(
                    file,
                    member_line,
                    member.start_point.column,
                    handler,
                    operation,
                    field,
                    "resolver-map",
                )
            )
    return resolvers, unread


def template_schemas(tree, content, file):
    """Root fields declared in GraphQL templates: tagged ``gql`` or
    ``graphql``, marked by a ``/* GraphQL */`` comment before them, or
    starting with ``#graphql``."""
    declared, unread = [], []
    for argument in _walk(tree.root_node):
        if argument.type != "template_string":
            continue
        source = text(argument, content)[1:-1]
        parent = argument.parent
        function = None
        if parent is not None and parent.type == "call_expression":
            function = parent.child_by_field_name("function")
        before = argument.prev_sibling
        if not (
            (
                function is not None
                and function.type == "identifier"
                and text(function, content) in ("gql", "graphql")
            )
            or (
                before is not None
                and before.type == "comment"
                and re.fullmatch(r"/\*\s*graphql\s*\*/", text(before, content), re.I)
            )
            or re.match(r"\s*#graphql\b", source)
        ):
            continue
        # A substitution (${fragment}) is blanked: its fields are not read.
        source = re.sub(r"\$\{[^}]*\}", _blank, source)
        offset = argument.start_point.row
        found, problems = schema_fields(source, file)
        declared += [
            GraphqlDeclaration(file, d.line + offset, d.operation, d.field)
            for d in found
        ]
        unread += [Unread(file, p.line + offset, p.kind, p.reason) for p in problems]
    return declared, unread
