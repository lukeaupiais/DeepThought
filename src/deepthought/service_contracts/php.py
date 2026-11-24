"""PHP routes and requests: Symfony ``Route`` annotations and attributes,
and curl requests.

Routes: a ``@Route`` annotation in a method's docblock, or a ``#[Route]``
attribute on it, where ``Route`` is imported from
``Symfony\\Component\\Routing\\Annotation`` or ``...\\Attribute``. The path
is the first argument or ``path``, joined with the class's own ``Route``
path; ``methods`` gives the methods (any method without it). A path that is
not a literal is unread.

Requests: ``curl_init($url)``, with the URL folded in its function (string
literals, interpolated strings, ``.``, ``sprintf`` with ``%s``/``%d``, and
local variables assigned once). The method is GET, or the literal
``CURLOPT_CUSTOMREQUEST`` (POST for ``CURLOPT_POST``) that a
``curl_setopt``/``curl_setopt_array`` in the same function sets; any other
setting of either leaves the method unknown.

Also Guzzle and Laravel HTTP client requests (``_client_requests``): verb
methods and ``request($method, $url)`` on a ``GuzzleHttp\\Client`` (a local
variable or ``$this`` property set once to ``new Client([...])``, or typed
as the client or its interface), with a literal ``base_uri`` resolved as
Guzzle does (RFC 3986); and ``Http::...->get($url)`` chains on Laravel's
``Http`` facade, under a literal ``baseUrl(...)``.
"""

import re

from ..syntax import identifier
from .http import ANY_METHOD, PLACEHOLDER, join_paths
from .laravel import _array, _chain, _uses
from .model import HttpRequest, HttpRoute, Unread

_ROUTE_PACKAGES = (
    "Symfony\\Component\\Routing\\Annotation\\Route",
    "Symfony\\Component\\Routing\\Attribute\\Route",
)
_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")


def _text(node, content):
    return content[node.start_byte : node.end_byte].decode("utf-8")


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def _route_names(root, content):
    """Local names of the Symfony Route class (``use ... [as X]``)."""
    names = set()
    for node in _walk(root):
        if node.type != "namespace_use_clause":
            continue
        written = [c for c in node.named_children if c.type == "qualified_name"]
        if not written:
            continue
        full = _text(written[0], content).lstrip("\\")
        if full not in _ROUTE_PACKAGES:
            continue
        alias = node.child_by_field_name("alias")
        names.add(_text(alias, content) if alias is not None else "Route")
    return names


# -- annotations -------------------------------------------------------------

_STRING = r'"((?:[^"\\]|\\.)*)"'


def _annotation_routes(comment, names):
    """[(path or None, methods)] of the ``@Route(...)`` of a docblock."""
    found = []
    for name in names:
        for match in re.finditer(rf"@{re.escape(name)}\s*\(", comment):
            depth, index = 1, match.end()
            while index < len(comment) and depth:
                depth += {"(": 1, ")": -1}.get(comment[index], 0)
                index += 1
            body = re.sub(r"\n\s*\*", " ", comment[match.end() : index - 1])
            path = re.match(rf"\s*{_STRING}", body)
            named = re.search(rf"\bpath\s*=\s*{_STRING}", body)
            value = (path or named).group(1) if (path or named) else None
            methods = re.search(r"\bmethods\s*=\s*(\{[^}]*\}|" + _STRING + ")", body)
            listed = (
                [m.upper() for m in re.findall(_STRING, methods.group(1))]
                if methods
                else []
            )
            found.append((value, listed))
    return found


def _docblock(node):
    """The ``/** */`` comment just before a declaration, or None."""
    previous = node.prev_named_sibling
    while previous is not None and previous.type == "attribute_list":
        previous = previous.prev_named_sibling
    if previous is not None and previous.type == "comment":
        return previous
    return None


def _attribute_routes(node, content, names, folder):
    """[(path or None, methods)] of the ``#[Route(...)]`` attributes."""
    found = []
    for lists in (c for c in node.named_children if c.type == "attribute_list"):
        for attribute in _walk(lists):
            if attribute.type != "attribute":
                continue
            name = next((c for c in attribute.named_children if c.type == "name"), None)
            if name is None or _text(name, content) not in names:
                continue
            arguments = attribute.child_by_field_name("parameters")
            path, methods = None, []
            positional = 0
            for argument in arguments.named_children if arguments else ():
                if argument.type != "argument":
                    continue
                label = argument.child_by_field_name("name")
                value = argument.named_children[-1]
                key = _text(label, content) if label is not None else None
                if key is None:
                    positional += 1
                if key == "path" or (key is None and positional == 1):
                    folded = folder.evaluate(value)
                    path = None if PLACEHOLDER in folded else folded
                elif key == "methods":
                    if value.type == "array_creation_expression":
                        for element in value.named_children:
                            methods.append(
                                folder.evaluate(element.named_children[0]).upper()
                                if element.named_children
                                else PLACEHOLDER
                            )
                    else:
                        methods.append(folder.evaluate(value).upper())
            found.append((path, methods))
    return found


# -- values ------------------------------------------------------------------


class _Folder:
    """Folds PHP string expressions; what is not fixed is PLACEHOLDER."""

    def __init__(self, content):
        self.content = content

    def evaluate(self, node, depth=0):
        if node is None or depth > 8:
            return PLACEHOLDER
        kind = node.type
        if kind == "argument":
            return self.evaluate(node.named_children[-1], depth)
        if kind == "parenthesized_expression":
            return self.evaluate(node.named_children[0], depth)
        if kind == "string":
            raw = _text(node, self.content)
            if raw.startswith("'"):
                return raw[1:-1].replace("\\'", "'").replace("\\\\", "\\")
            return "".join(
                _text(c, self.content) if c.type == "string_content" else PLACEHOLDER
                for c in node.named_children
            )
        if kind == "encapsed_string":
            return "".join(
                _text(c, self.content) if c.type == "string_content" else PLACEHOLDER
                for c in node.named_children
            )
        if kind == "binary_expression":
            operator = node.child_by_field_name("operator")
            if operator is not None and _text(operator, self.content) == ".":
                return self.evaluate(
                    node.child_by_field_name("left"), depth + 1
                ) + self.evaluate(node.child_by_field_name("right"), depth + 1)
            return PLACEHOLDER
        if kind == "function_call_expression":
            function = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if (
                function is not None
                and _text(function, self.content).lstrip("\\") == "sprintf"
                and arguments is not None
                and arguments.named_children
            ):
                template = self.evaluate(arguments.named_children[0], depth + 1)
                return re.sub(r"%[-+ 0]*\d*(?:\.\d+)?[sdu]", PLACEHOLDER, template)
            return PLACEHOLDER
        if kind == "variable_name":
            return self._variable(node, depth)
        return PLACEHOLDER

    def _variable(self, node, depth):
        """A local variable assigned exactly once in its function."""
        name = _text(node, self.content)
        scope = node.parent
        while scope is not None and scope.type not in (
            "method_declaration",
            "function_definition",
            "anonymous_function",
            "arrow_function",
            "program",
        ):
            scope = scope.parent
        if scope is None or scope.type == "program":
            return PLACEHOLDER
        assignments = []
        for candidate in _walk(scope):
            if candidate.type in (
                "assignment_expression",
                "augmented_assignment_expression",
            ):
                left = candidate.child_by_field_name("left")
                if left is not None and _text(left, self.content) == name:
                    assignments.append(candidate)
            elif candidate.type == "simple_parameter":
                written = candidate.child_by_field_name("name")
                if written is not None and _text(written, self.content) == name:
                    return PLACEHOLDER
        if len(assignments) != 1 or assignments[0].type != "assignment_expression":
            return PLACEHOLDER
        if assignments[0].start_byte > node.start_byte:
            return PLACEHOLDER
        return self.evaluate(assignments[0].child_by_field_name("right"), depth + 1)


# -- extraction ----------------------------------------------------------------


def _curl_method(function, variable, content, folder):
    """The method curl option calls in ``function`` give the handle."""
    method = "GET"
    for node in _walk(function):
        if node.type != "function_call_expression":
            continue
        called = _text(node.child_by_field_name("function"), content).lstrip("\\")
        arguments = node.child_by_field_name("arguments")
        values = (
            [a.named_children[-1] for a in arguments.named_children]
            if arguments
            else []
        )
        if not values or _text(values[0], content) != variable:
            continue
        options = []
        if called == "curl_setopt" and len(values) == 3:
            options.append((_text(values[1], content), values[2]))
        elif called == "curl_setopt_array" and len(values) == 2:
            if values[1].type != "array_creation_expression":
                if variable:
                    return None
                continue
            for element in values[1].named_children:
                pair = element.named_children
                if len(pair) == 2:
                    options.append((_text(pair[0], content), pair[1]))
        for option, value in options:
            if option == "CURLOPT_CUSTOMREQUEST":
                written = folder.evaluate(value).upper()
                if written not in _METHODS:
                    return None
                method = written
            elif option == "CURLOPT_POST":
                if _text(value, content).lower() not in ("true", "1"):
                    return None
                method = "POST" if method == "GET" else method
    return method


# Client types and the client their requests are made with.
_CLIENT_TYPES = {
    "GuzzleHttp\\Client": "guzzle",
    "GuzzleHttp\\ClientInterface": "guzzle",
    "Symfony\\Contracts\\HttpClient\\HttpClientInterface": "symfony-http",
}
_SYMFONY_FACTORY = "Symfony\\Component\\HttpClient\\HttpClient"
_AUTOWIRE = "Symfony\\Component\\DependencyInjection\\Attribute\\Autowire"
_TARGET = "Symfony\\Component\\DependencyInjection\\Attribute\\Target"
_HTTP_FACADE = "Illuminate\\Support\\Facades\\Http"
_CLIENT_VERBS = {
    m.lower(): m for m in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
}
_SCHEME = re.compile(r"(?i)^[a-z][a-z0-9+.-]*://")


def _resolve_reference(base, url):
    """``url`` resolved against a literal absolute ``base`` (RFC 3986, as
    Guzzle does); ``url`` as written when ``base`` is not one."""
    match = _SCHEME.match(base)
    if match is None or _SCHEME.match(url):
        return url
    if not url:
        return base
    origin = base[: match.end()] + base[match.end() :].partition("/")[0]
    if url.startswith("/"):
        return origin + url
    rest = base[len(origin) :]
    return origin + rest.rpartition("/")[0] + "/" + url


def _appended(base, url):
    """Laravel's ``baseUrl``: the URL appended to the base unless absolute;
    as written when the base is not a literal absolute URL."""
    if _SCHEME.match(base) is None or _SCHEME.match(url):
        return url
    return base.rstrip("/") + "/" + url.lstrip("/")


def symfony_argument_name(name):
    """The argument name Symfony autowires a named service alias under
    (``Target::parse``: ``orders.api_client`` is ``ordersApiClient``)."""
    words = [w for w in re.split(r"[^a-zA-Z0-9\x7f-\uffff]+", name) if w]
    joined = "".join(w[:1].upper() + w[1:] for w in words)
    return joined[:1].lower() + joined[1:]


def _enclosing(node, kinds):
    while node is not None and node.type not in kinds:
        node = node.parent
    return node


def _client_requests(root, content, folder, laravel, scoped):
    """(call node, client, method or None, URL) of each Guzzle, Symfony or
    Laravel HTTP client request in the file. ``scoped`` maps the argument
    name of each Symfony scoped client to its base URL."""
    uses, _ = _uses(root, content)

    def qualified(written):
        written = written.lstrip("\\")
        head, _, rest = written.partition("\\")
        return uses[head] + ("\\" + rest if rest else "") if head in uses else written

    def client_type(node):
        """The client a type names, else None."""
        return _CLIENT_TYPES.get(qualified(_text(node, content))) if node else None

    def creation(node):
        """(client, base URL: "" for none) of ``new GuzzleHttp\\Client([...])``,
        ``HttpClient::create([...])`` or ``HttpClient::createForBaseUri(...)``,
        else None."""
        if node is None:
            return None
        if node.type == "object_creation_expression":
            named = [
                c for c in node.named_children if c.type in ("name", "qualified_name")
            ]
            if not named or qualified(_text(named[0], content)) != "GuzzleHttp\\Client":
                return None
            client = "guzzle"
            holder = next(
                (c for c in node.named_children if c.type == "arguments"), None
            )
        elif node.type == "scoped_call_expression":
            scope = node.child_by_field_name("scope")
            name = _text(node.child_by_field_name("name"), content)
            if qualified(_text(scope, content)) != _SYMFONY_FACTORY or name not in (
                "create",
                "createForBaseUri",
            ):
                return None
            client = "symfony-http"
            holder = node.child_by_field_name("arguments")
        else:
            return None
        arguments = [
            c.named_children[-1]
            for c in (holder.named_children if holder is not None else ())
            if c.type == "argument" and c.named_children
        ]
        if not arguments:
            return client, ""
        if node.type == "scoped_call_expression" and name == "createForBaseUri":
            return client, folder.evaluate(arguments[0])
        keyed, _ = _array(arguments[0], content)
        return client, folder.evaluate(keyed["base_uri"]) if "base_uri" in keyed else ""

    def typed(parameter_or_property):
        return client_type(parameter_or_property.child_by_field_name("type"))

    def named_service(parameter):
        """The service a parameter's ``#[Autowire(service: ...)]``,
        ``#[Autowire('@...')]`` or ``#[Target(...)]`` names: a literal, else
        PLACEHOLDER (also for any other ``#[Autowire]``); None when it has
        none of these."""
        attributes = parameter.child_by_field_name("attributes")
        for attribute in _walk(attributes) if attributes is not None else ():
            if attribute.type != "attribute" or not attribute.named_children:
                continue
            kind = qualified(_text(attribute.named_children[0], content))
            holder = attribute.child_by_field_name("parameters")
            for argument in holder.named_children if holder is not None else ():
                if argument.type != "argument" or not argument.named_children:
                    continue
                label = argument.child_by_field_name("name")
                label = _text(label, content) if label is not None else None
                value = folder.evaluate(argument.named_children[-1])
                if kind == _AUTOWIRE and label == "service":
                    return value
                if kind == _AUTOWIRE and label in (None, "value"):
                    return value[1:] if value.startswith("@") else PLACEHOLDER
                if kind == _TARGET and label in (None, "name"):
                    return value
            if kind == _AUTOWIRE:
                return PLACEHOLDER  # wired some other way
        return None

    def injected(parameter):
        """(client, base URL) of a typed constructor or method parameter:
        a Symfony scoped client is autowired by its argument name, or named
        by an attribute."""
        client = typed(parameter)
        if client != "symfony-http":
            return client, ""
        name = named_service(parameter)
        if name is None:
            name = _text(parameter.child_by_field_name("name"), content).lstrip("$")
        elif PLACEHOLDER in name:
            return client, ""
        return client, scoped.get(symfony_argument_name(name), "")

    def receiver(node):
        """(client, base URL: "" when it has none or it is not known) of the
        client object ``node`` names, else None."""
        written = _text(node, content)
        if node.type == "variable_name":
            scope = _enclosing(
                node,
                ("method_declaration", "function_definition", "anonymous_function"),
            )
            if scope is None:
                return None
            assigned = [
                c.child_by_field_name("right")
                for c in _walk(scope)
                if c.type == "assignment_expression"
                and _text(c.child_by_field_name("left"), content) == written
            ]
            if len(assigned) == 1 and creation(assigned[0]) is not None:
                return creation(assigned[0])
            parameters = scope.child_by_field_name("parameters")
            for parameter in (
                parameters.named_children if parameters is not None else ()
            ):
                name = parameter.child_by_field_name("name")
                if (
                    name is not None
                    and _text(name, content) == written
                    and typed(parameter)
                ):
                    return injected(parameter) if not assigned else None
            return None
        if node.type == "member_access_expression" and written.startswith("$this->"):
            name = written[len("$this->") :]
            cls = _enclosing(node, ("class_declaration",))
            if cls is None or not name.isidentifier():
                return None
            assigned = [
                c.child_by_field_name("right")
                for c in _walk(cls)
                if c.type == "assignment_expression"
                and _text(c.child_by_field_name("left"), content) == written
            ]
            if len(assigned) == 1 and creation(assigned[0]) is not None:
                return creation(assigned[0])
            if len(assigned) == 1 and assigned[0].type == "variable_name":
                passed = receiver(assigned[0])  # say, a constructor parameter
                if passed is not None:
                    return passed
            for candidate in _walk(cls):
                if candidate.type == "property_declaration" and typed(candidate):
                    if any(
                        e.named_children
                        and _text(e.named_children[0], content) == "$" + name
                        for e in candidate.named_children
                        if e.type == "property_element"
                    ):
                        return typed(candidate), ""
                if candidate.type == "property_promotion_parameter" and typed(
                    candidate
                ):
                    variable = candidate.child_by_field_name("name")
                    if variable is not None and _text(variable, content) == "$" + name:
                        return injected(candidate)
        return None

    facades = {name for name, full in uses.items() if full == _HTTP_FACADE}
    if laravel and "Http" not in uses:
        facades.add("Http")
    found = []
    for node in _walk(root):
        if node.type not in ("member_call_expression", "scoped_call_expression"):
            continue
        name_node = node.child_by_field_name("name")
        if name_node is None or name_node.type != "name":
            continue
        verb = _text(name_node, content)
        stem = verb[: -len("Async")] if verb.endswith("Async") else verb
        if stem not in _CLIENT_VERBS and stem not in ("request", "send"):
            continue
        arguments = node.child_by_field_name("arguments")
        values = [
            a.named_children[-1]
            for a in (arguments.named_children if arguments is not None else ())
            if a.type == "argument" and a.named_children
        ]
        two = stem in ("request", "send")
        if len(values) < (2 if two else 1):
            continue
        method_node, url_node = (values[0], values[1]) if two else (None, values[0])
        method = _CLIENT_VERBS.get(stem)
        if two:
            written = folder.evaluate(method_node).upper()
            method = written if written in _CLIENT_VERBS.values() else None
        url = folder.evaluate(url_node)
        chain = _chain(node, content)
        if chain is not None and chain[0][0] == "facade" and chain[0][1] in facades:
            if stem == "request" or verb.endswith("Async"):
                continue
            base = ""
            for link, call in chain[1][:-1]:
                if link == "baseUrl":
                    link_arguments = call.child_by_field_name("arguments")
                    link_values = [
                        a.named_children[-1]
                        for a in link_arguments.named_children
                        if a.type == "argument" and a.named_children
                    ]
                    base = folder.evaluate(link_values[0]) if link_values else ""
            found.append((node, "laravel-http", method, _appended(base, url)))
            continue
        if stem == "send" or node.type != "member_call_expression":
            continue
        target = receiver(node.child_by_field_name("object"))
        if target is None:
            continue
        client, base = target
        if client == "symfony-http" and stem != "request":
            continue  # its only request method
        found.append((node, client, method, _resolve_reference(base, url)))
    return found


def extract_php(
    tree, content, repository_id, file, definitions, *, laravel=False, scoped=None
):
    """Routes, requests and unread sites of one PHP file; ``laravel`` when
    the file belongs to a Laravel project (``Http`` may be unqualified);
    ``scoped`` maps Symfony scoped client argument names to base URLs."""
    root = tree.root_node
    folder = _Folder(content)
    file_definitions = [d for d in definitions if d.file == file]
    spans = {(d.line, d.column, d.end_line, d.end_column): d for d in file_definitions}
    enclosing = sorted(
        (
            ((d.line, d.column), (d.end_line, d.end_column), d.id)
            for d in file_definitions
        ),
        reverse=True,
    )
    routes, requests, unread = [], [], []
    ordinals = {}

    def make_id(kind, key):
        ordinal = ordinals.get((kind, key), 0)
        ordinals[(kind, key)] = ordinal + 1
        return identifier(repository_id, file, key, kind, ordinal)

    def caller(node):
        start = (node.start_point.row + 1, node.start_point.column)
        end = (node.end_point.row + 1, node.end_point.column)
        return next((i for s, e, i in enclosing if s <= start and end <= e), None)

    names = _route_names(root, content)

    def declared(node):
        comment = _docblock(node)
        found = (
            _annotation_routes(_text(comment, content), names)
            if comment is not None and _text(comment, content).startswith("/**")
            else []
        )
        return found + _attribute_routes(node, content, names, folder)

    if names:
        for declaration in _walk(root):
            if declaration.type != "class_declaration":
                continue
            prefixes = [p for p, _ in declared(declaration)]
            if any(p is None for p in prefixes) or len(prefixes) > 1:
                unread.append(
                    Unread(
                        file,
                        declaration.start_point.row + 1,
                        "route",
                        "path_not_literal",
                    )
                )
                continue
            prefix = prefixes[0] if prefixes else ""
            body = declaration.child_by_field_name("body")
            for method in body.named_children if body is not None else ():
                if method.type != "method_declaration":
                    continue
                handler = spans.get(
                    (
                        method.start_point.row + 1,
                        method.start_point.column,
                        method.end_point.row + 1,
                        method.end_point.column,
                    )
                )
                for path, methods in declared(method):
                    line = method.start_point.row + 1
                    if path is None or any(m not in _METHODS for m in methods):
                        unread.append(Unread(file, line, "route", "path_not_literal"))
                        continue
                    full = join_paths(prefix + path)
                    routes.append(
                        HttpRoute(
                            id=make_id("HttpRoute", f"{','.join(methods)} {full}"),
                            file=file,
                            line=line,
                            column=method.start_point.column,
                            handler_id=handler.id
                            if handler is not None
                            else caller(method),
                            methods=tuple(sorted(set(methods))) or (ANY_METHOD,),
                            path=full,
                            framework="symfony",
                        )
                    )

    for node in _walk(root):
        if node.type != "function_call_expression":
            continue
        function = node.child_by_field_name("function")
        if function is None or _text(function, content).lstrip("\\") != "curl_init":
            continue
        arguments = node.child_by_field_name("arguments")
        values = (
            [a for a in arguments.named_children if a.type == "argument"]
            if arguments
            else []
        )
        line = node.start_point.row + 1
        if len(values) != 1:
            continue  # the URL is set with CURLOPT_URL: not read
        url = folder.evaluate(values[0])
        # The handle: $x = curl_init(...).
        parent = node.parent
        variable = (
            _text(parent.child_by_field_name("left"), content)
            if parent is not None and parent.type == "assignment_expression"
            else None
        )
        scope = node.parent
        while scope is not None and scope.type not in (
            "method_declaration",
            "function_definition",
            "program",
        ):
            scope = scope.parent
        method = (
            _curl_method(scope, variable, content, folder)
            if variable is not None and scope is not None
            else "GET"
        )
        requests.append(
            HttpRequest(
                id=make_id("HttpRequest", f"curl {line}"),
                file=file,
                line=line,
                column=node.start_point.column,
                caller_id=caller(node),
                method=method,
                url=url,
                client="php-curl",
            )
        )
    if b"Client" in content or b"Http" in content:
        for node, client, method, url in _client_requests(
            root, content, folder, laravel, scoped or {}
        ):
            line = node.start_point.row + 1
            requests.append(
                HttpRequest(
                    id=make_id("HttpRequest", f"{client} {line}"),
                    file=file,
                    line=line,
                    column=node.start_point.column,
                    caller_id=caller(node),
                    method=method,
                    url=url,
                    client=client,
                )
            )
    return routes, requests, unread
