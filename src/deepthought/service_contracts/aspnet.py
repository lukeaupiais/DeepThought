"""ASP.NET Core routes: attribute-routed controller actions and minimal API
endpoints.

Controllers: a class that carries ``[ApiController]``, derives from
``Controller``, ``ControllerBase`` or an Ardalis.ApiEndpoints base
(``EndpointBaseAsync...``), or is named ``...Controller``. An
action is a method with ``[HttpGet]``, ``[HttpPost]``, ... (optionally with
a template) or ``[Route]``; its template joins the class's ``[Route]``
templates unless it starts with ``/`` or ``~/``. ``[controller]`` is the
class name without ``Controller``, ``[action]`` the method name without an
``Async`` suffix, ``[area]`` the class's ``[Area]``. An action with no
template on either level is conventionally routed and not read.

Minimal APIs: ``x.MapGet/MapPost/MapPut/MapDelete/MapPatch(template, ...)``
and ``x.MapMethods(template, new[] { "GET", ... }, ...)`` with a literal
template. ``x.MapGroup(prefix)`` chains, or a local bound once to one, add
their prefixes; a receiver that is a ``RouteGroupBuilder`` or
``IEndpointRouteBuilder`` parameter has an unknown prefix.

Route constraints and defaults (``{id:int}``, ``{id?}``, ``{*rest}``) are
read as plain parameters.
"""

import re

from ..syntax import identifier
from .http import ANY_METHOD, HTTP_METHODS, join_paths
from .model import HttpRoute, Unread

_VERBS = {f"Http{m[0]}{m[1:].lower()}": m for m in HTTP_METHODS}
_MAPS = {
    f"Map{m[0]}{m[1:].lower()}": m for m in ("GET", "POST", "PUT", "DELETE", "PATCH")
}
_CONTROLLER_BASES = {"Controller", "ControllerBase"}
# Ardalis.ApiEndpoints base classes, which derive from ControllerBase.
_ENDPOINT_BASES = {"EndpointBase", "EndpointBaseAsync", "EndpointBaseSync"}
_GROUP_TYPES = {"RouteGroupBuilder", "IEndpointRouteBuilder"}


def _text(node, content):
    return content[node.start_byte : node.end_byte].decode("utf-8")


def _method_list(argument, content):
    """The methods of a ``MapMethods`` list: string literals and
    ``HttpMethods.X`` constants; None when any element is something else."""
    collection = argument.named_children[-1] if argument.named_children else None
    if collection is None:
        return None
    elements = [
        e
        for e in _walk(collection)
        if e.parent is not None
        and e.parent.type in ("initializer_expression", "collection_expression")
    ]
    methods = []
    for element in elements:
        while element.type in ("collection_element", "expression_element"):
            if len(element.named_children) != 1:  # a spread, for one
                return None
            element = element.named_children[0]
        text = _string(element, content)
        if text is None and element.type == "member_access_expression":
            receiver = element.child_by_field_name("expression")
            if receiver is not None and _text(receiver, content).endswith(
                "HttpMethods"
            ):
                text = _text(element.child_by_field_name("name"), content)
        if text is None:
            return None
        methods.append(text.upper())
    return methods


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def _string(node, content):
    """A C# string literal's text, or None for anything else."""
    if node is None:
        return None
    if node.type == "argument":
        node = node.named_children[-1] if node.named_children else None
        if node is None:
            return None
    if node.type == "string_literal":
        parts = []
        for child in node.named_children:
            if child.type == "string_literal_content":
                parts.append(_text(child, content))
            else:
                return None  # escapes are not read
        return "".join(parts)
    if node.type == "verbatim_string_literal":
        return _text(node, content)[2:-1].replace('""', '"')
    return None


def _plain(template):
    """A template with constraints, defaults and catch-alls as plain
    parameters: {id:int} -> {id}, {id?} -> {id}, {*rest} -> {rest}."""
    return re.sub(
        r"\{\*{0,2}([A-Za-z_]\w*)[^}]*\}",
        lambda m: "{" + m.group(1) + "}",
        template,
    )


def _name(node, content):
    """An attribute's simple name, without an ``Attribute`` suffix."""
    name = _text(node.child_by_field_name("name"), content).rpartition(".")[2]
    return name[: -len("Attribute")] if name.endswith("Attribute") else name


def _attributes(node, content):
    """[(name, [arguments])] of a declaration's attributes."""
    found = []
    for lists in node.named_children:
        if lists.type != "attribute_list":
            continue
        for attribute in lists.named_children:
            if attribute.type != "attribute":
                continue
            arguments = next(
                (
                    c
                    for c in attribute.named_children
                    if c.type == "attribute_argument_list"
                ),
                None,
            )
            values = (
                [a for a in arguments.named_children if a.type == "attribute_argument"]
                if arguments is not None
                else []
            )
            found.append((_name(attribute, content), values))
    return found


def _template(arguments, content):
    """(template, readable): the first positional or ``Template =``
    argument; ("", True) without one; (None, False) when not a literal."""
    for argument in arguments:
        named = argument.named_children
        if not named:
            continue
        if len(named) == 2 and named[0].type in ("identifier", "name_equals"):
            key = _text(named[0], content).rstrip("=: ").strip()
            if key != "Template":
                continue
            value = named[1]
        elif argument.child_by_field_name("name") is not None:
            continue
        else:
            value = named[-1]
        text = _string(value, content)
        return (text, True) if text is not None else (None, False)
    return "", True


def _controller(declaration, content, attributes):
    name = _text(declaration.child_by_field_name("name"), content)
    if any(a == "NonController" for a, _ in attributes):
        return False
    if name.endswith("Controller") or any(a == "ApiController" for a, _ in attributes):
        return True
    bases = next((c for c in declaration.named_children if c.type == "base_list"), None)
    if bases is None:
        return False
    for base in bases.named_children:
        written = re.sub(r"\s+", "", _text(base, content))
        while "<" in written and re.search(r"<[^<>]*>", written):
            written = re.sub(r"<[^<>]*>", "", written)
        if written.rpartition(".")[2] in _CONTROLLER_BASES:
            return True
        # Ardalis.ApiEndpoints: EndpointBaseAsync.WithRequest<T>.WithResult<R>
        if written.split(".")[0] in _ENDPOINT_BASES:
            return True
    return False


def _tokens(template, controller, action, area):
    """Replace [controller], [action] and [area]; None when a token is not
    known."""
    values = {"controller": controller, "action": action, "area": area}

    def replace(match):
        value = values.get(match.group(1).lower())
        if value is None:
            raise KeyError(match.group(1))
        return value

    try:
        return re.sub(r"\[(\w+)\]", replace, template)
    except KeyError:
        return None


def extract_aspnet(tree, content, repository_id, file, definitions):
    """(routes, requests, unread) of one C# file; requests are not read
    yet."""
    root = tree.root_node
    file_definitions = [d for d in definitions if d.file == file]
    spans = {(d.line, d.column, d.end_line, d.end_column): d for d in file_definitions}
    enclosing = sorted(
        (
            ((d.line, d.column), (d.end_line, d.end_column), d.id)
            for d in file_definitions
        ),
        reverse=True,
    )
    routes, unread = [], []
    ordinals = {}

    def make_id(kind, key):
        ordinal = ordinals.get((kind, key), 0)
        ordinals[(kind, key)] = ordinal + 1
        return identifier(repository_id, file, key, kind, ordinal)

    def span_of(node):
        return (
            node.start_point.row + 1,
            node.start_point.column,
            node.end_point.row + 1,
            node.end_point.column,
        )

    def caller(node):
        start = (node.start_point.row + 1, node.start_point.column)
        end = (node.end_point.row + 1, node.end_point.column)
        return next((i for s, e, i in enclosing if s <= start and end <= e), None)

    def add(node, methods, path, framework, handler, base_unknown=False):
        full = join_paths(_plain(path))
        routes.append(
            HttpRoute(
                id=make_id("HttpRoute", f"{','.join(methods)} {full}"),
                file=file,
                line=node.start_point.row + 1,
                column=node.start_point.column,
                handler_id=handler,
                methods=tuple(sorted(set(methods))),
                path=full,
                framework=framework,
                base_unknown=base_unknown,
                case_insensitive=True,
            )
        )

    def not_read(node, reason="path_not_literal"):
        unread.append(Unread(file, node.start_point.row + 1, "route", reason))

    # -- controllers ---------------------------------------------------------
    for declaration in _walk(root):
        if declaration.type != "class_declaration":
            continue
        attributes = _attributes(declaration, content)
        if not _controller(declaration, content, attributes):
            continue
        name = _text(declaration.child_by_field_name("name"), content)
        controller = name[: -len("Controller")] if name.endswith("Controller") else name
        area = None
        prefixes, readable = [], True
        for attribute, arguments in attributes:
            if attribute == "Area":
                area, _ = _template(arguments, content)
            elif attribute == "Route":
                template, ok = _template(arguments, content)
                readable = readable and ok
                if ok:
                    prefixes.append(template)
        body = declaration.child_by_field_name("body")
        for method in body.named_children if body is not None else ():
            if method.type != "method_declaration":
                continue
            action = _text(method.child_by_field_name("name"), content)
            if action.endswith("Async"):
                action = action[: -len("Async")]
            verbs, templates, method_ok = [], [], True
            for attribute, arguments in _attributes(method, content):
                if attribute in _VERBS:
                    template, ok = _template(arguments, content)
                    method_ok = method_ok and ok
                    verbs.append((_VERBS[attribute], template))
                elif attribute == "Route":
                    template, ok = _template(arguments, content)
                    method_ok = method_ok and ok
                    templates.append(template)
            if not verbs and not templates:
                continue
            if not (readable and method_ok):
                not_read(method)
                continue
            # [HttpGet("x")] gives its own template; [HttpGet] with [Route("x")]
            # takes the Route templates.
            pairs = []
            for verb, template in verbs:
                if template:
                    pairs.append(([verb], template))
                else:
                    pairs.extend(([verb], t) for t in templates or [""])
            if not verbs:
                pairs = [([ANY_METHOD], t) for t in templates]
            definition = spans.get(span_of(method))
            handler = definition.id if definition is not None else caller(method)
            for methods, template in pairs:
                if template.startswith(("/", "~/")):
                    paths = [template.lstrip("~")]
                elif prefixes:
                    paths = [join_paths(p, template) for p in prefixes]
                elif template:
                    paths = [template]
                else:
                    continue  # conventionally routed
                for path in paths:
                    path = _tokens(path, controller, action, area)
                    if path is None:
                        not_read(method)
                        continue
                    add(method, methods, path, "aspnet-controller", handler)

    # -- minimal APIs ----------------------------------------------------------
    def bound(identifier_node):
        """The value a local bound once is declared with, the type of a
        parameter of that name, or None."""
        name = _text(identifier_node, content)
        scope = identifier_node.parent
        while scope is not None and scope.type not in (
            "method_declaration",
            "local_function_statement",
            "lambda_expression",
            "compilation_unit",
            "constructor_declaration",
        ):
            scope = scope.parent
        if scope is None:
            return None, None
        values, types = [], []
        for node in _walk(scope):
            if node.type == "variable_declarator":
                written = node.child_by_field_name("name") or (
                    node.named_children[0] if node.named_children else None
                )
                if written is not None and _text(written, content) == name:
                    value = next(
                        (
                            c
                            for c in node.named_children[1:]
                            if c.type != "bracketed_argument_list"
                        ),
                        None,
                    )
                    values.append(value)
            elif node.type == "parameter":
                written = node.child_by_field_name("name")
                if written is not None and _text(written, content) == name:
                    kind = node.child_by_field_name("type")
                    types.append(_text(kind, content) if kind is not None else "")
            elif node.type == "assignment_expression":
                left = node.child_by_field_name("left")
                if left is not None and _text(left, content) == name:
                    return None, None
        if len(values) == 1 and not types:
            return values[0], None
        if len(types) == 1 and not values:
            return None, types[0]
        return None, None

    def prefix(receiver, depth=0):
        """(prefix, known) of a route builder expression."""
        if receiver is None or depth > 8:
            return "", False
        if receiver.type == "invocation_expression":
            function = receiver.child_by_field_name("function")
            if function is None or function.type != "member_access_expression":
                return "", False
            called = _text(function.child_by_field_name("name"), content)
            inner = function.child_by_field_name("expression")
            if called == "MapGroup":
                arguments = receiver.child_by_field_name("arguments")
                first = (
                    arguments.named_children[0]
                    if arguments and arguments.named_children
                    else None
                )
                text = _string(first, content)
                if text is None:
                    return "", False
                base, known = prefix(inner, depth + 1)
                return join_paths(base, text), known
            # Builder methods return the same builder (WithTags,
            # RequireAuthorization, ...).
            return prefix(inner, depth + 1)
        if receiver.type == "identifier":
            value, kind = bound(receiver)
            if value is not None:
                return prefix(value, depth + 1)
            if kind is not None and kind.rpartition(".")[2] in _GROUP_TYPES:
                return "", False
            return "", True
        return "", True

    for node in _walk(root):
        if node.type != "invocation_expression":
            continue
        function = node.child_by_field_name("function")
        if function is None or function.type != "member_access_expression":
            continue
        called = _text(function.child_by_field_name("name"), content)
        if called not in _MAPS and called != "MapMethods":
            continue
        arguments = node.child_by_field_name("arguments")
        positional = (
            [a for a in arguments.named_children if a.type == "argument"]
            if arguments
            else []
        )
        if not positional or positional[0].child_by_field_name("name") is not None:
            continue
        template = _string(positional[0], content)
        if template is None:
            if positional[0].named_children and positional[0].named_children[
                -1
            ].type in (
                "interpolated_string_expression",
                "identifier",
                "binary_expression",
                "member_access_expression",
            ):
                not_read(node)
            continue  # a project's own Map* overload taking a handler first
        if called == "MapMethods":
            if len(positional) < 2:
                continue
            methods = _method_list(positional[1], content)
            if not methods or any(m not in HTTP_METHODS for m in methods):
                not_read(node, "methods_not_literal")
                continue
        else:
            methods = [_MAPS[called]]
        base, known = prefix(function.child_by_field_name("expression"))
        add(
            node,
            methods,
            join_paths(base, template),
            "aspnet-minimal-api",
            caller(node),
            base_unknown=not known,
        )
    return routes, [], unread
