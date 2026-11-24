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
# Node packages that serve GraphQL: a package depending on one is a server.
SERVERS = RESOLVER_MAP_SERVERS | {
    "@nestjs/graphql",
    "apollo-server-micro",
    "express-graphql",
    "graphql-http",
}
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


@dataclass(frozen=True)
class ResolverMapReference:
    """A root key of a resolver map whose fields are in another module:
    ``{Query}`` or ``{Query: require('./query')}``. ``export`` is
    ``default`` (the module's exports object), ``*`` (its named exports) or
    the name of one export."""

    file: str
    line: int
    operation: str
    module: str
    export: str


def _members(context, value, operation, file, resolvers, unread):
    """Fields of the members of one root type's resolver object."""
    content = context.content
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


def _require(node, content):
    """The module of ``require('m')``, or None."""
    if node is None or node.type != "call_expression":
        return None
    function = node.child_by_field_name("function")
    arguments = node.child_by_field_name("arguments")
    if function is None or text(function, content) != "require" or arguments is None:
        return None
    found = [c for c in arguments.named_children if c.type != "comment"]
    if len(found) == 1 and found[0].type == "string":
        return text(found[0], content)[1:-1]
    return None


def _bindings(root, content):
    """Top-level names bound to an object literal or to a module:
    name -> ("object", node) | ("module", module, export)."""
    found = {}
    for node in root.named_children:
        if node.type == "export_statement":
            node = node.child_by_field_name("declaration") or node
        if node.type == "import_statement":
            source = node.child_by_field_name("source")
            if source is None:
                continue
            module = text(source, content)[1:-1]
            for clause in node.named_children:
                if clause.type != "import_clause":
                    continue
                for item in clause.named_children:
                    if item.type == "identifier":
                        found[text(item, content)] = ("module", module, "default")
                    elif item.type == "namespace_import":
                        name = text(item.named_children[-1], content)
                        found[name] = ("module", module, "*")
                    elif item.type == "named_imports":
                        for spec in item.named_children:
                            if spec.type != "import_specifier":
                                continue
                            imported = spec.child_by_field_name("name")
                            alias = spec.child_by_field_name("alias") or imported
                            found[text(alias, content)] = (
                                "module",
                                module,
                                text(imported, content),
                            )
        elif node.type in ("lexical_declaration", "variable_declaration"):
            for declarator in node.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name = declarator.child_by_field_name("name")
                value = declarator.child_by_field_name("value")
                if name is None or value is None:
                    continue
                if name.type == "identifier" and value.type == "object":
                    found[text(name, content)] = ("object", value)
                    continue
                export = "default"
                if value.type == "member_expression":
                    # require('./resolvers').Query
                    export = text(value.child_by_field_name("property"), content)
                    value = value.child_by_field_name("object")
                module = _require(value, content)
                if module is None:
                    continue
                if name.type == "identifier":
                    found[text(name, content)] = ("module", module, export)
                elif name.type == "object_pattern" and export == "default":
                    # const { Query, Mutation: M } = require('./resolvers')
                    for item in name.named_children:
                        if item.type == "shorthand_property_identifier_pattern":
                            local = text(item, content)
                            found[local] = ("module", module, local)
                        elif item.type == "pair_pattern":
                            key = _key_name(item.child_by_field_name("key"), content)
                            local = item.child_by_field_name("value")
                            if key and local is not None and local.type == "identifier":
                                found[text(local, content)] = ("module", module, key)
    return found


def _is_resolver_map(node, content):
    """An object holding root keys that stands alone or is the server's
    ``resolvers`` option. Other nested objects with the same keys are not
    resolvers: Apollo Client's {typePolicies: {Query}} and
    {cacheRedirects: {Query}}."""
    outer = node.parent
    if outer is None or outer.type != "pair":
        return True
    return _key_name(outer.child_by_field_name("key"), content) == "resolvers"


def extract_resolver_maps(tree, content, repository_id, file, definitions, language):
    """Root fields of the resolver maps of one JavaScript or TypeScript file:
    the members of an object under a ``Query``, ``Mutation`` or
    ``Subscription`` key, written in place or bound to a name in the file.
    A key bound to another module is returned as a reference for the caller
    to follow. The caller reads only the files of Node packages that depend
    on a server taking such maps."""
    context = _Context(tree, content, repository_id, file, definitions, language)
    bindings = _bindings(context.root, content)
    resolvers, unread, references = [], [], []
    for node in _walk(context.root):
        if node.type == "pair":
            operation = _key_name(node.child_by_field_name("key"), content)
            value = node.child_by_field_name("value")
        elif node.type == "shorthand_property_identifier":
            operation = text(node, content)
            value = node
        else:
            continue
        if operation not in ROOT_TYPES or not _is_resolver_map(node.parent, content):
            continue
        line = node.start_point.row + 1
        if value is not None and value.type in (
            "identifier",
            "shorthand_property_identifier",
        ):
            bound = bindings.get(text(value, content))
            if bound is not None and bound[0] == "object":
                value = bound[1]
            elif bound is not None:
                references.append(
                    ResolverMapReference(file, line, operation, bound[1], bound[2])
                )
                continue
        elif _require(value, content) is not None:
            references.append(
                ResolverMapReference(
                    file, line, operation, _require(value, content), "default"
                )
            )
            continue
        if value is None or value.type != "object":
            unread.append(Unread(file, line, "graphql", "resolver_map_not_literal"))
            continue
        _members(context, value, operation, file, resolvers, unread)
    return resolvers, unread, references


def _exported_object(root, content, export):
    """The object literal a module exports as ``export``: ``module.exports``
    or ``export default`` for ``default``, a named export otherwise."""
    bindings = _bindings(root, content)

    def literal(node):
        if node is not None and node.type == "identifier":
            bound = bindings.get(text(node, content))
            node = bound[1] if bound is not None and bound[0] == "object" else None
        return node if node is not None and node.type == "object" else None

    for node in root.named_children:
        if node.type == "expression_statement" and node.named_children:
            assignment = node.named_children[0]
            if assignment.type != "assignment_expression":
                continue
            left = text(assignment.child_by_field_name("left"), content)
            right = assignment.child_by_field_name("right")
            if left == "module.exports":
                found = literal(right)
                if found is None:
                    return None
                if export == "default":
                    return found
                member = _js_object(found, content).get(export)
                if member is not None:
                    return literal(member)
                if any(
                    c.type == "shorthand_property_identifier"
                    and text(c, content) == export
                    for c in found.named_children
                ):
                    bound = bindings.get(export)
                    return bound[1] if bound and bound[0] == "object" else None
            elif left in (f"module.exports.{export}", f"exports.{export}"):
                return literal(right)
        elif node.type == "export_statement":
            value = node.child_by_field_name("value")
            if value is not None and export == "default":
                return literal(value)
            declaration = node.child_by_field_name("declaration")
            if declaration is not None and declaration.type in (
                "lexical_declaration",
                "variable_declaration",
            ):
                for declarator in declaration.named_children:
                    name = declarator.child_by_field_name("name")
                    if name is not None and text(name, content) == export:
                        return literal(declarator.child_by_field_name("value"))
    return None


def _named_exports(root, content):
    """(name, node) of a module's exported functions and constants."""
    for node in root.named_children:
        if node.type != "export_statement":
            continue
        declaration = node.child_by_field_name("declaration")
        if declaration is None:
            continue
        if declaration.type in (
            "function_declaration",
            "generator_function_declaration",
        ):
            yield text(declaration.child_by_field_name("name"), content), declaration
        elif declaration.type in ("lexical_declaration", "variable_declaration"):
            for declarator in declaration.named_children:
                name = declarator.child_by_field_name("name")
                if declarator.type == "variable_declarator" and name is not None:
                    if name.type == "identifier":
                        yield text(name, content), declarator


def follow_reference(
    reference, tree, content, repository_id, file, definitions, language
):
    """Root fields of the module a resolver map reference names, and unread
    facts: its exports object, one of its named exports as an object
    literal, or all its named exports for a namespace import."""
    context = _Context(tree, content, repository_id, file, definitions, language)
    resolvers, unread = [], []
    if reference.export == "*":
        for name, node in _named_exports(context.root, content):
            value = (
                node.child_by_field_name("value")
                if node.type == "variable_declarator"
                else node
            )
            handler = context.definition_of(value) if value is not None else None
            resolvers.append(
                GraphqlResolver(
                    file,
                    node.start_point.row + 1,
                    node.start_point.column,
                    handler or context.named_function(name),
                    reference.operation,
                    name,
                    "resolver-map",
                )
            )
        if resolvers or "module.exports" not in content.decode("utf-8", "replace"):
            return resolvers, unread
        reference = ResolverMapReference(
            reference.file,
            reference.line,
            reference.operation,
            reference.module,
            "default",
        )
    found = _exported_object(context.root, content, reference.export)
    if found is None:
        unread.append(
            Unread(
                reference.file, reference.line, "graphql", "resolver_map_not_literal"
            )
        )
        return resolvers, unread
    _members(context, found, reference.operation, file, resolvers, unread)
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


# Node packages whose code sends GraphQL operations.
CLIENTS = frozenset(
    {
        "@apollo/client",
        "@apollo/react-hooks",
        "@urql/core",
        "apollo-boost",
        "apollo-client",
        "graphql-request",
        "react-apollo",
        "react-relay",
        "relay-runtime",
        "urql",
    }
)


@dataclass(frozen=True)
class GraphqlOperation:
    """One root field that a client operation selects."""

    file: str
    line: int
    column: int
    caller_id: str | None
    operation: str  # Query | Mutation | Subscription
    field: str
    name: str | None  # the operation's name


_TOKEN = re.compile(r"\.\.\.|[{}()\[\]:=$!@,]|[_A-Za-z]\w*|-?\d[\w.+-]*")


def operation_fields(source, file, line_offset=0, column_offset=0, caller_id=None):
    """Root fields selected by the operations of an executable GraphQL
    document, and unread facts. Fragments are skipped; a fragment spread or
    inline fragment among an operation's root fields is unread."""
    stripped = re.sub(r'"""[\s\S]*?"""|"(?:[^"\\\n]|\\.)*"|#[^\n]*', _blank, source)
    tokens = list(_TOKEN.finditer(stripped))
    found, unread = [], []
    index, previous = 0, None

    def position(token):
        start = token.start()
        line = stripped.count("\n", 0, start)
        column = start - (stripped.rfind("\n", 0, start) + 1)
        return line + 1 + line_offset, column + (column_offset if line == 0 else 0)

    def skip_group(i, opening, closing):
        level = 0
        while i < len(tokens):
            value = tokens[i].group(0)
            if value == opening:
                level += 1
            elif value == closing:
                level -= 1
                if level == 0:
                    return i + 1
            i += 1
        return i

    while index < len(tokens):
        value = tokens[index].group(0)
        # A definition starts the document or follows another's closing brace.
        starts = previous in (None, "}")
        if starts and value in ("query", "mutation", "subscription", "{"):
            if value == "{":
                operation, name = "Query", None
            else:
                operation, name = value.capitalize(), None
                index += 1
                if index < len(tokens) and re.fullmatch(
                    r"[_A-Za-z]\w*", tokens[index].group(0)
                ):
                    name = tokens[index].group(0)
                    index += 1
                # Variables and directives, up to the selection set.
                while index < len(tokens) and tokens[index].group(0) != "{":
                    if tokens[index].group(0) == "(":
                        index = skip_group(index, "(", ")")
                    else:
                        index += 1
            # The selection set: names at its first level are root fields.
            index += 1
            level = 1
            while index < len(tokens) and level > 0:
                token = tokens[index]
                value = token.group(0)
                if value == "{":
                    level += 1
                elif value == "}":
                    level -= 1
                elif value == "(":
                    index = skip_group(index, "(", ")")
                    continue
                elif value == "@" and index + 1 < len(tokens):
                    index += 2  # a directive's name; its arguments are skipped
                    continue
                elif level == 1 and value == "...":
                    line, _ = position(token)
                    unread.append(Unread(file, line, "graphql", "client_fragment_root"))
                    # Skip the spread's name, or the inline fragment.
                    index += 1
                    if index < len(tokens) and tokens[index].group(0) == "on":
                        index += 2
                    if index < len(tokens) and tokens[index].group(0) == "{":
                        index = skip_group(index, "{", "}")
                    elif index < len(tokens) and tokens[index].group(0) != "@":
                        index += 1
                    continue
                elif level == 1 and re.fullmatch(r"[_A-Za-z]\w*", value):
                    if index + 1 < len(tokens) and tokens[index + 1].group(0) == ":":
                        # An alias: the field is the name after the colon.
                        index += 2
                        if index >= len(tokens):
                            break
                        token = tokens[index]
                        value = token.group(0)
                    # Apollo Client resolves a field marked @client locally.
                    ahead = index + 1
                    if ahead < len(tokens) and tokens[ahead].group(0) == "(":
                        ahead = skip_group(ahead, "(", ")")
                    local = False
                    while ahead + 1 < len(tokens) and tokens[ahead].group(0) == "@":
                        local = local or tokens[ahead + 1].group(0) == "client"
                        ahead += 2
                        if ahead < len(tokens) and tokens[ahead].group(0) == "(":
                            ahead = skip_group(ahead, "(", ")")
                    if not value.startswith("__") and not local:
                        line, column = position(token)
                        found.append(
                            GraphqlOperation(
                                file, line, column, caller_id, operation, value, name
                            )
                        )
                index += 1
            previous = "}"
            continue
        if value == "{":
            # Any other braces (a fragment's selection, a type definition).
            index = skip_group(index, "{", "}")
            previous = "}"
            continue
        previous = value
        index += 1
    return found, unread


def _graphql_templates(tree, content):
    """(template node, source) of the GraphQL templates of a script."""
    for argument in _walk(tree.root_node):
        if argument.type != "template_string":
            continue
        source = text(argument, content)[1:-1]
        parent = argument.parent
        function = None
        if parent is not None and parent.type == "call_expression":
            function = parent.child_by_field_name("function")
        before = argument.prev_sibling
        if (
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
            yield argument, source


def client_operations(tree, content, repository_id, file, definitions, language):
    """Root fields the GraphQL templates of one client script select."""
    context = _Context(tree, content, repository_id, file, definitions, language)
    found, unread = [], []
    for node, source in _graphql_templates(tree, content):
        operations, problems = operation_fields(
            source,
            file,
            line_offset=node.start_point.row,
            column_offset=node.start_point.column + 1,
            caller_id=context.caller(node),
        )
        found += operations
        unread += problems
    return found, unread
