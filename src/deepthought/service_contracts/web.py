"""HTTP routes and requests in Python, JavaScript/TypeScript and Go sources.

Servers: Flask and FastAPI route decorators, Express routes, and Go
``net/http``/gorilla ``HandleFunc``/``Handle``. Clients: Python ``requests``
and ``httpx``, Locust ``client``, JavaScript ``fetch``, ``axios``,
``request``, AngularJS ``$http``, Angular ``HttpClient`` and
``httpResource``, WebSocket and SockJS handshakes, and
Go ``net/http``. A framework or client object is recognised
only through the file's own imports and bindings; URLs are folded by
``folding.Folder``.
"""

import re

from ..syntax import identifier
from .folding import Folder, text
from .http import ANY_METHOD, HTTP_METHODS, PLACEHOLDER, join_paths
from .model import HttpRequest, HttpRoute, Unread
from .stomp_clients import stomp_names, value_kind

_VERBS = {m.lower(): m for m in HTTP_METHODS}
_PY_FRAMEWORKS = {
    ("flask", "Flask"): "flask",
    ("flask", "Blueprint"): "flask",
    ("fastapi", "FastAPI"): "fastapi",
    ("fastapi", "APIRouter"): "fastapi",
}
_PY_PREFIX = {"Blueprint": "url_prefix", "APIRouter": "prefix"}
_PY_CLIENTS = ("requests", "httpx")
_GO_METHODS = {f"http.Method{m.capitalize()}": m for m in HTTP_METHODS}


def _string_list(folder, node):
    """Strings of a list/tuple/array literal, or None if any is not fixed."""
    if node is None or node.type not in ("list", "tuple", "array"):
        return None
    values = [folder.evaluate(c) for c in node.named_children]
    if any(PLACEHOLDER in v for v in values):
        return None
    return values


def _keyword_arguments(arguments, content):
    found, positional = {}, []
    for child in arguments.named_children:
        if child.type == "keyword_argument":
            found[text(child.child_by_field_name("name"), content)] = (
                child.child_by_field_name("value")
            )
        elif child.type not in ("comment",):
            positional.append(child)
    return found, positional


class _Context:
    def __init__(
        self, tree, content, repository_id, file, definitions, language, externals=None
    ):
        self.root = tree.root_node
        self.content = content
        self.repository_id = repository_id
        self.file = file
        self.language = language
        self.folder = Folder(language, self.root, content, externals)
        file_definitions = [d for d in definitions if d.file == file]
        self.spans = {
            (d.line, d.column, d.end_line, d.end_column): d for d in file_definitions
        }
        self.enclosing = sorted(
            (
                ((d.line, d.column), (d.end_line, d.end_column), d.id)
                for d in file_definitions
            ),
            reverse=True,
        )
        self.by_name = {}
        for d in file_definitions:
            self.by_name.setdefault(d.symbol, []).append(d)
        self.routes, self.requests, self.unread = [], [], []
        self._ordinals = {}

    def make_id(self, kind, key):
        ordinal = self._ordinals.get((kind, key), 0)
        self._ordinals[(kind, key)] = ordinal + 1
        return identifier(self.repository_id, self.file, key, kind, ordinal)

    def caller(self, node):
        start = (node.start_point.row + 1, node.start_point.column)
        end = (node.end_point.row + 1, node.end_point.column)
        return next((i for s, e, i in self.enclosing if s <= start and end <= e), None)

    def definition_of(self, node):
        key = (
            node.start_point.row + 1,
            node.start_point.column,
            node.end_point.row + 1,
            node.end_point.column,
        )
        found = self.spans.get(key)
        return None if found is None else found.id

    def named_function(self, name):
        found = self.by_name.get(name, [])
        return found[0].id if len(found) == 1 else None

    def route(self, node, methods, path, framework, handler_id, base_unknown=False):
        line = node.start_point.row + 1
        if PLACEHOLDER in path:
            self.unread.append(Unread(self.file, line, "route", "path_not_literal"))
            return
        full = join_paths(path)
        self.routes.append(
            HttpRoute(
                id=self.make_id("HttpRoute", f"{','.join(methods)} {full}"),
                file=self.file,
                line=line,
                column=node.start_point.column,
                handler_id=handler_id,
                methods=tuple(sorted(set(methods))),
                path=full,
                framework=framework,
                base_unknown=base_unknown,
            )
        )

    def request(self, node, method, url, client):
        line = node.start_point.row + 1
        self.requests.append(
            HttpRequest(
                id=self.make_id("HttpRequest", f"{client} {line}"),
                file=self.file,
                line=line,
                column=node.start_point.column,
                caller_id=self.caller(node),
                method=method,
                url=url,
                client=client,
            )
        )


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def _unknown_base(path):
    """A route path that starts with unknown text (a base path variable)."""
    if path.startswith(PLACEHOLDER) and path.lstrip(PLACEHOLDER).startswith("/"):
        return path.lstrip(PLACEHOLDER), True
    return path, False


def _python_path(path, framework):
    """Flask ``<id>``/``<int:id>`` and FastAPI ``{rest:path}`` in route
    template form; a Flask ``<path:rest>`` converter matches several segments."""
    if framework == "flask":
        path = re.sub(r"<path:[^>]*>", "{*rest}", path)
        return re.sub(r"<[^>]*>", "{var}", path)
    return re.sub(r"\{[^{}:]*:path\}", "{*rest}", path)


def _express_path(path):
    """Express ``:id`` (optionally ``:id?`` or ``:id(\\d+)``) as ``{id}``."""
    return re.sub(r"(?<=/):[A-Za-z0-9_]+(?:\([^)]*\))?\??", "{var}", path)


# -- Python -----------------------------------------------------------------


def _python_imports(root, content):
    """local name -> dotted origin, for import and from-import statements."""
    names = {}
    for node in _walk(root):
        if node.type == "import_statement":
            for child in node.named_children:
                if child.type == "dotted_name":
                    names[text(child, content).split(".")[0]] = text(
                        child, content
                    ).split(".")[0]
                elif child.type == "aliased_import":
                    names[text(child.child_by_field_name("alias"), content)] = text(
                        child.child_by_field_name("name"), content
                    )
        elif node.type == "import_from_statement":
            module = text(node.child_by_field_name("module_name"), content)
            for child in node.children_by_field_name("name"):
                if child.type == "dotted_name":
                    names[text(child, content)] = f"{module}.{text(child, content)}"
                elif child.type == "aliased_import":
                    names[text(child.child_by_field_name("alias"), content)] = (
                        f"{module}.{text(child.child_by_field_name('name'), content)}"
                    )
    return names


def _python_origin(function, imports, content):
    """The dotted origin a callee names through the file's imports."""
    written = text(function, content)
    head, _, rest = written.partition(".")
    origin = imports.get(head)
    if origin is None:
        return None
    return origin + ("." + rest if rest else "")


def extract_python(context):
    content, root = context.content, context.root
    imports = _python_imports(root, content)

    def app_of(owner):
        """(framework, prefix) when ``owner`` names a Flask/FastAPI app or
        router bound once in its scope."""
        if owner.type != "identifier":
            return None
        found = context.folder.binding(owner)
        if found is None or found[1].type != "call":
            return None
        value = found[1]
        origin = _python_origin(value.child_by_field_name("function"), imports, content)
        module, _, constructor = (origin or "").rpartition(".")
        framework = _PY_FRAMEWORKS.get((module, constructor))
        if framework is None:
            return None
        keywords, _ = _keyword_arguments(
            value.child_by_field_name("arguments"), content
        )
        prefix = ""
        if _PY_PREFIX.get(constructor) in keywords:
            prefix = context.folder.evaluate(keywords[_PY_PREFIX[constructor]])
        return framework, prefix

    locust = any(origin.split(".")[0] == "locust" for origin in imports.values())
    for node in _walk(root):
        if node.type == "decorated_definition":
            definition = node.child_by_field_name("definition")
            handler = context.definition_of(definition)
            for decorator in node.named_children:
                if decorator.type != "decorator" or not decorator.named_children:
                    continue
                call = decorator.named_children[0]
                if call.type != "call":
                    continue
                function = call.child_by_field_name("function")
                if function.type != "attribute":
                    continue
                verb = text(function.child_by_field_name("attribute"), content)
                app = app_of(function.child_by_field_name("object"))
                if app is None:
                    continue
                framework, prefix = app
                keywords, positional = _keyword_arguments(
                    call.child_by_field_name("arguments"), content
                )
                path_node = (
                    positional[0]
                    if positional
                    else keywords.get("path", keywords.get("rule"))
                )
                if verb in _VERBS:
                    methods = [_VERBS[verb]]
                elif verb in ("route", "api_route"):
                    listed = keywords.get("methods")
                    if listed is None:
                        methods = ["GET"] if framework == "flask" else [ANY_METHOD]
                    else:
                        methods = _string_list(context.folder, listed)
                        if methods is None or any(
                            m.upper() not in HTTP_METHODS for m in methods
                        ):
                            context.unread.append(
                                Unread(
                                    context.file,
                                    call.start_point.row + 1,
                                    "route",
                                    "method_not_literal",
                                )
                            )
                            continue
                        methods = [m.upper() for m in methods]
                else:
                    continue
                path = _python_path(
                    prefix + context.folder.evaluate(path_node), framework
                )
                context.route(call, methods, path, framework, handler)
        elif node.type == "call":
            function = node.child_by_field_name("function")
            if function.type != "attribute":
                continue
            verb = text(function.child_by_field_name("attribute"), content)
            owner_node = function.child_by_field_name("object")
            owner = text(owner_node, content)
            arguments = node.child_by_field_name("arguments")
            if arguments is None or arguments.type != "argument_list":
                continue
            keywords, positional = _keyword_arguments(arguments, content)
            client = None
            if imports.get(owner) in _PY_CLIENTS:
                client = imports[owner]
            elif (
                locust and owner.endswith(".client") and owner_node.type == "attribute"
            ):
                client = "locust"
            if client is None:
                continue
            if verb in _VERBS:
                method, url_node = (
                    _VERBS[verb],
                    positional[0] if positional else keywords.get("url"),
                )
            elif verb == "request":
                method_node = positional[0] if positional else keywords.get("method")
                method = context.folder.evaluate(method_node).upper()
                method = method if method in HTTP_METHODS else None
                url_node = positional[1] if len(positional) > 1 else keywords.get("url")
            else:
                continue
            if url_node is None:
                continue
            context.request(node, method, context.folder.evaluate(url_node), client)


# -- JavaScript / TypeScript ------------------------------------------------


def _browser_url(url):
    """A relative URL in browser code, read against the site root ("api/x"
    is "/api/x"): the page is assumed to be served at the root."""
    if url and url[0] not in "/" + PLACEHOLDER and not re.match(r"(?i)^[a-z]+:", url):
        return "/" + url
    return url


def _js_modules(root, content):
    """local name -> module, for require() bindings and import statements."""
    names = {}
    for node in _walk(root):
        if node.type == "variable_declarator":
            name, value = (
                node.child_by_field_name("name"),
                node.child_by_field_name("value"),
            )
            if (
                name is not None
                and name.type == "identifier"
                and value is not None
                and value.type == "call_expression"
                and text(value.child_by_field_name("function"), content) == "require"
            ):
                arguments = value.child_by_field_name("arguments").named_children
                if arguments and arguments[0].type == "string":
                    names[text(name, content)] = text(arguments[0], content)[1:-1]
        elif node.type == "import_statement":
            source = node.child_by_field_name("source")
            module = text(source, content)[1:-1] if source is not None else None
            for clause in node.named_children:
                if clause.type != "import_clause":
                    continue
                for item in clause.named_children:
                    if item.type == "identifier":
                        names[text(item, content)] = module
                    elif item.type == "namespace_import":
                        names[text(item.named_children[-1], content)] = module
                    elif item.type == "named_imports":
                        for spec in item.named_children:
                            # Comments may sit between the braces.
                            if spec.type != "import_specifier":
                                continue
                            alias = spec.child_by_field_name(
                                "alias"
                            ) or spec.child_by_field_name("name")
                            names[text(alias, content)] = (
                                f"{module}.{text(spec.child_by_field_name('name'), content)}"
                            )
    return names


def _js_object(node, content):
    """{key: value node} of an object literal."""
    found = {}
    if node is None or node.type != "object":
        return found
    for pair in node.named_children:
        if pair.type == "pair":
            key = pair.child_by_field_name("key")
            name = text(key, content).strip("'\"")
            found[name] = pair.child_by_field_name("value")
    return found


# Global constructor -> (client, modules that export it under any local
# name); a name not imported is the browser's global.
_WEBSOCKET_CLIENTS = {
    "WebSocket": ("websocket", ("ws", "ws.WebSocket", "isomorphic-ws")),
    "SockJS": ("sockjs", ("sockjs-client",)),
}


def _websocket_client(node, modules, content):
    """The client a ``new WebSocket(url)`` or ``new SockJS(url)`` (or
    ``SockJS(url)``, which SockJS allows) opens, or None: the constructor is
    the global or imported from its module."""
    if node.type == "call_expression":
        constructor = node.child_by_field_name("function")
        if constructor is None or text(constructor, content) not in (
            "SockJS",
            *(n for n, m in modules.items() if m == "sockjs-client"),
        ):
            return None
    else:
        constructor = node.child_by_field_name("constructor")
    if constructor is None or constructor.type != "identifier":
        return None
    written = text(constructor, content)
    if written in modules:
        return next(
            (
                c
                for c, sources in _WEBSOCKET_CLIENTS.values()
                if modules[written] in sources
            ),
            None,
        )
    return _WEBSOCKET_CLIENTS.get(written, (None,))[0]


_ANGULAR_HTTP = "@angular/common/http"
_ANGULAR_VERBS = ("get", "post", "put", "delete", "patch", "head", "options")
_ANGULAR_RESOURCES = ("", "text", "blob", "arrayBuffer")


def _angular_client(context, receiver, modules):
    """``receiver`` is an Angular ``HttpClient``: ``this.NAME`` for a field
    set to ``inject(HttpClient)`` or typed ``HttpClient``, or a constructor
    parameter property typed ``HttpClient``; or a name bound once to
    ``inject(HttpClient)``. ``HttpClient`` and ``inject`` are imported from
    their Angular modules."""
    content, folder = context.content, context.folder
    types = {n for n, m in modules.items() if m == _ANGULAR_HTTP + ".HttpClient"}
    injects = {n for n, m in modules.items() if m == "@angular/core.inject"}

    def injected(value):
        if value is None or value.type != "call_expression":
            return False
        function = value.child_by_field_name("function")
        arguments = value.child_by_field_name("arguments")
        found = [c for c in arguments.named_children if c.type != "comment"]
        return (
            function.type == "identifier"
            and text(function, content) in injects
            and len(found) == 1
            and found[0].type == "identifier"
            and text(found[0], content) in types
        )

    def typed(node):
        annotation = next(
            (c for c in node.named_children if c.type == "type_annotation"), None
        )
        return annotation is not None and any(
            c.type == "type_identifier" and text(c, content) in types
            for c in annotation.named_children
        )

    if not types:
        return False
    if receiver.type == "identifier":
        found = folder.binding(receiver)
        return found is not None and injected(found[1])
    if (
        receiver.type != "member_expression"
        or receiver.child_by_field_name("object").type != "this"
    ):
        return False
    member = folder.instance_member(receiver)
    if member is None:
        return False
    value = member.child_by_field_name("value")
    return injected(value) or (value is None and typed(member))


def _returned(function):
    """Expressions a function returns: an arrow's expression body, or the
    values of its own ``return`` statements."""
    body = function.child_by_field_name("body")
    if body is None:
        return []
    if body.type != "statement_block":
        return [body]
    found, stack = [], list(body.named_children)
    while stack:
        node = stack.pop()
        if node.type in (
            "arrow_function",
            "function_expression",
            "function_declaration",
            "class_declaration",
            "class",
        ):
            continue
        if node.type == "return_statement":
            found.extend(c for c in node.named_children if c.type != "comment")
            continue
        stack.extend(node.named_children)
    return found


def _stomp_handshake(context, node, modules, stomp):
    """The WebSocket handshake a STOMP client's options name: ``brokerURL``
    given to a ``@stomp/stompjs`` ``Client`` or ``RxStomp`` constructor or
    ``configure``, or the SockJS ``host`` given to an ng2-stomp-service
    ``configure``."""
    content = context.content
    arguments = node.child_by_field_name("arguments")
    if arguments is None:
        return
    positional = [c for c in arguments.named_children if c.type != "comment"]
    if not positional or positional[0].type != "object":
        return
    if node.type == "new_expression":
        kind = value_kind(node, content, modules)
    elif node.type == "call_expression":
        function = node.child_by_field_name("function")
        if (
            function.type != "member_expression"
            or text(function.child_by_field_name("property"), content) != "configure"
        ):
            return
        kind = stomp.get(text(function.child_by_field_name("object"), content))
    else:
        return
    options = _js_object(positional[0], content)
    if kind in ("stompjs", "rx-stomp") and "brokerURL" in options:
        value, client = options["brokerURL"], "websocket"
    elif kind == "ng2-stomp-service" and "host" in options:
        value, client = options["host"], "sockjs"
    else:
        return
    url = _browser_url(context.folder.evaluate(value))
    context.request(value, "GET", url, client)


def extract_angular(context, modules):
    """Requests of Angular's ``HttpClient`` (``get``/``post``/... and
    ``request(method, url)``) and ``httpResource`` (and its ``text``,
    ``blob`` and ``arrayBuffer`` forms): the URL is the first argument, or
    what the reactive function returns (a URL, or a request object's
    ``url`` and ``method``, by default GET)."""
    content = context.content
    resources = {n for n, m in modules.items() if m == _ANGULAR_HTTP + ".httpResource"}
    for node in _walk(context.root):
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if arguments is None or arguments.type != "arguments":
            continue
        positional = [c for c in arguments.named_children if c.type != "comment"]
        if not positional:
            continue
        written = text(function, content)
        owner, _, kind = written.partition(".")
        if owner in resources and kind in _ANGULAR_RESOURCES:
            if positional[0].type not in ("arrow_function", "function_expression"):
                continue
            for value in _returned(positional[0]):
                if (
                    value.type in ("undefined", "null")
                    or text(value, content) == "undefined"
                ):
                    continue
                method = "GET"
                if value.type == "object":
                    options = _js_object(value, content)
                    if "url" not in options:
                        continue
                    if "method" in options:
                        method = context.folder.evaluate(options["method"]).upper()
                        method = method if method in HTTP_METHODS else None
                    value = options["url"]
                url = _browser_url(context.folder.evaluate(value))
                context.request(value, method, url, "angular-http")
            continue
        if function.type != "member_expression":
            continue
        verb = text(function.child_by_field_name("property"), content)
        if verb not in _ANGULAR_VERBS and verb != "request":
            continue
        if not _angular_client(
            context, function.child_by_field_name("object"), modules
        ):
            continue
        if verb == "request":
            if len(positional) < 2:
                continue  # request(HttpRequest)
            method = context.folder.evaluate(positional[0]).upper()
            method = method if method in HTTP_METHODS else None
            url_node = positional[1]
        else:
            method, url_node = _VERBS[verb], positional[0]
        url = _browser_url(context.folder.evaluate(url_node))
        context.request(node, method, url, "angular-http")


def extract_javascript(context, nest=None):
    content, root = context.content, context.root
    modules = _js_modules(root, content)
    if any(m.startswith(_ANGULAR_HTTP + ".") for m in modules.values()):
        extract_angular(context, modules)
    if context.language == "typescript" and any(
        m.startswith(_NEST + ".") for m in modules.values()
    ):
        extract_nest(context, modules, nest or (None, None))
    express_names = {n for n, m in modules.items() if m == "express"}
    router_constructors = {n for n, m in modules.items() if m == "express.Router"}

    def is_app(owner):
        """``owner`` names an Express app or Router bound once in its scope."""
        if owner.type != "identifier":
            return False
        found = context.folder.binding(owner)
        if found is None or found[1].type != "call_expression":
            return False
        callee = text(found[1].child_by_field_name("function"), content)
        return (
            callee in express_names
            or callee in router_constructors
            or (
                callee.partition(".")[0] in express_names and callee.endswith(".Router")
            )
        )

    stomp = stomp_names(root, content, modules) if b"tomp" in content else {}
    for node in _walk(root):
        _stomp_handshake(context, node, modules, stomp)
        if node.type == "new_expression":
            socket = _websocket_client(node, modules, content)
            arguments = node.child_by_field_name("arguments")
            positional = [
                c
                for c in (arguments.named_children if arguments is not None else ())
                if c.type != "comment"
            ]
            if socket is not None and positional:
                url = _browser_url(context.folder.evaluate(positional[0]))
                context.request(node, "GET", url, socket)
            continue
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if arguments is None or arguments.type != "arguments":
            continue
        positional = [c for c in arguments.named_children if c.type != "comment"]
        written = text(function, content)
        socket = _websocket_client(node, modules, content)
        if socket is not None and positional:
            url = _browser_url(context.folder.evaluate(positional[0]))
            context.request(node, "GET", url, socket)
            continue
        if function.type == "member_expression":
            owner = text(function.child_by_field_name("object"), content)
            verb = text(function.child_by_field_name("property"), content)
            if (
                is_app(function.child_by_field_name("object"))
                and (verb in _VERBS or verb == "all")
                and len(positional) >= 2
            ):
                path = _express_path(context.folder.evaluate(positional[0]))
                handler_node = positional[-1]
                handler = (
                    context.named_function(text(handler_node, content))
                    if handler_node.type == "identifier"
                    else None
                )
                methods = [ANY_METHOD] if verb == "all" else [_VERBS[verb]]
                context.route(node, methods, path, "express", handler)
                continue
            if owner == "$http" and verb in _VERBS and positional:
                url = _browser_url(context.folder.evaluate(positional[0]))
                context.request(node, _VERBS[verb], url, "angularjs-http")
                continue
            module = modules.get(owner)
            if (
                module in ("axios", "request", "superagent")
                and verb in _VERBS
                and positional
            ):
                context.request(
                    node, _VERBS[verb], context.folder.evaluate(positional[0]), module
                )
            continue
        if written == "$http" and positional:
            options = _js_object(positional[0], content)
            if "url" not in options:
                continue
            method = "GET"
            if "method" in options:
                method = context.folder.evaluate(options["method"]).upper()
                method = method if method in HTTP_METHODS else None
            url = _browser_url(context.folder.evaluate(options["url"]))
            context.request(node, method, url, "angularjs-http")
            continue
        if written == "fetch" and positional:
            options = _js_object(
                positional[1] if len(positional) > 1 else None, content
            )
            method = "GET"
            if "method" in options:
                method = context.folder.evaluate(options["method"]).upper()
                method = method if method in HTTP_METHODS else None
            context.request(
                node, method, context.folder.evaluate(positional[0]), "fetch"
            )
        elif modules.get(written) in ("request", "axios") and positional:
            options = _js_object(positional[0], content)
            if options:
                url_node = options.get("url", options.get("uri"))
                method_node = options.get("method")
                method = "GET"
                if method_node is not None:
                    method = context.folder.evaluate(method_node).upper()
                    method = method if method in HTTP_METHODS else None
                if url_node is None:
                    continue
                url = context.folder.evaluate(url_node)
            else:
                method, url = "GET", context.folder.evaluate(positional[0])
            context.request(node, method, url, modules[written])


# -- NestJS -------------------------------------------------------------------

_NEST = "@nestjs/common"
_NEST_VERBS = {
    "Get": "GET",
    "Post": "POST",
    "Put": "PUT",
    "Delete": "DELETE",
    "Patch": "PATCH",
    "Options": "OPTIONS",
    "Head": "HEAD",
    "All": ANY_METHOD,
}


def _nest_path(path):
    """A Nest route path (Express style) in route template form: ``:id`` is a
    variable and a segment starting with ``*`` matches the rest."""
    path = re.sub(r"(?<=/)\*[A-Za-z0-9_]*$", "{*rest}", path)
    return _express_path(path)


def _nest_decorator(node, modules, content):
    """(name, argument nodes) of a decorator imported from @nestjs/common."""
    call = node.named_children[0] if node.named_children else None
    if call is None or call.type != "call_expression":
        return None, []
    function = call.child_by_field_name("function")
    if function is None or function.type != "identifier":
        return None, []
    origin = modules.get(text(function, content), "")
    if not origin.startswith(_NEST + "."):
        return None, []
    arguments = call.child_by_field_name("arguments")
    found = [] if arguments is None else arguments.named_children
    return origin.rpartition(".")[2], [c for c in found if c.type != "comment"]


def _nest_paths(context, node):
    """Literal paths of a Nest path argument: a string or an array of them;
    an absent argument is the empty path."""
    if node is None:
        return [""]
    if node.type == "array":
        items = [c for c in node.named_children if c.type != "comment"]
        return [context.folder.evaluate(item) for item in items] or [""]
    return [context.folder.evaluate(node)]


def nest_settings(tree, content, language):
    """Application-wide NestJS settings a file makes: ("prefix", text) for
    ``x.setGlobalPrefix("text")``, and ("unread", reason) for a prefix that
    is not one literal argument, ``enableVersioning`` or ``RouterModule``."""
    folder = Folder(language, tree.root_node, content)
    found = []
    for node in _walk(tree.root_node):
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        if function is None or function.type != "member_expression":
            continue
        name = text(function.child_by_field_name("property"), content)
        owner = text(function.child_by_field_name("object"), content)
        arguments = node.child_by_field_name("arguments")
        positional = (
            []
            if arguments is None
            else [c for c in arguments.named_children if c.type != "comment"]
        )
        if name == "setGlobalPrefix":
            value = folder.evaluate(positional[0]) if positional else PLACEHOLDER
            if len(positional) != 1:
                found.append(("unread", "global_prefix_options"))
            elif PLACEHOLDER in value:
                found.append(("unread", "global_prefix_not_literal"))
            else:
                found.append(("prefix", value))
        elif name == "enableVersioning":
            found.append(("unread", "versioning"))
        elif owner == "RouterModule":
            found.append(("unread", "router_module"))
    return found


def extract_nest(context, modules, nest):
    """Routes of NestJS controllers: a class decorated ``@Controller(path)``
    whose methods carry ``@Get(path)`` and the like, all from
    @nestjs/common; ``nest`` is (global prefix, unread reason) of the
    application this file belongs to."""
    content = context.content
    prefix, reason = nest
    for node in _walk(context.root):
        if node.type != "class_declaration":
            continue
        decorators = [c for c in node.named_children if c.type == "decorator"]
        if node.parent is not None and node.parent.type == "export_statement":
            decorators += [
                c for c in node.parent.named_children if c.type == "decorator"
            ]
        controller = None
        for decorator in decorators:
            name, arguments = _nest_decorator(decorator, modules, content)
            if name == "Controller":
                controller = (decorator, arguments)
        if controller is None:
            continue
        decorator, arguments = controller
        line = decorator.start_point.row + 1
        first = arguments[0] if arguments else None
        if first is not None and first.type == "object":
            options = _js_object(first, content)
            if "version" in options:
                context.unread.append(Unread(context.file, line, "route", "version"))
                continue
            first = options.get("path")
        bases = _nest_paths(context, first)
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
                name, args = _nest_decorator(item, modules, content)
                if name == "Version":
                    context.unread.append(
                        Unread(
                            context.file, item.start_point.row + 1, "route", "version"
                        )
                    )
                    break
            else:
                for item in decorators:
                    name, args = _nest_decorator(item, modules, content)
                    if name not in _NEST_VERBS:
                        continue
                    if reason is not None:
                        context.unread.append(
                            Unread(
                                context.file, item.start_point.row + 1, "route", reason
                            )
                        )
                        continue
                    for base in bases:
                        for path in _nest_paths(context, args[0] if args else None):
                            full = _nest_path(join_paths(prefix or "", base, path))
                            if PLACEHOLDER in base or PLACEHOLDER in path:
                                full = PLACEHOLDER
                            context.route(
                                item,
                                [_NEST_VERBS[name]],
                                full,
                                "nestjs",
                                handler,
                            )


# -- Go -----------------------------------------------------------------------


def _go_imports(root, content):
    """local package name -> import path."""
    names = {}
    for node in _walk(root):
        if node.type == "import_spec":
            path = text(node.child_by_field_name("path"), content).strip('"`')
            alias = node.child_by_field_name("name")
            local = (
                text(alias, content) if alias is not None else path.rsplit("/", 1)[-1]
            )
            names[local] = path
    return names


def _go_method(folder, node, content):
    written = text(node, content)
    if written in _GO_METHODS:
        return _GO_METHODS[written]
    value = folder.evaluate(node).upper()
    return value if value in HTTP_METHODS else None


def extract_go(context, package):
    """``package`` is (functions, methods): function and method names mapped
    to the declarations of that name in the file's package directory, to
    anchor ``handler`` and ``s.handler`` handlers."""
    package_functions, package_methods = package
    content, root = context.content, context.root
    imports = _go_imports(root, content)
    http_names = {n for n, p in imports.items() if p == "net/http"}
    routing = bool(http_names) or any(
        p in ("github.com/gorilla/mux",) for p in imports.values()
    )
    for node in _walk(root):
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if function.type != "selector_expression" or arguments is None:
            continue
        positional = arguments.named_children
        operand = function.child_by_field_name("operand")
        field = text(function.child_by_field_name("field"), content)
        operand_text = text(operand, content)
        if routing and field in ("HandleFunc", "Handle") and len(positional) >= 2:
            path = context.folder.evaluate(positional[0])
            methods = [ANY_METHOD]
            pattern = re.match(r"^([A-Z]+) (/.*)$", path)
            if pattern and pattern.group(1) in HTTP_METHODS:
                methods, path = [pattern.group(1)], pattern.group(2)
            parent = node.parent
            if (
                parent is not None
                and parent.type == "selector_expression"
                and text(parent.child_by_field_name("field"), content) == "Methods"
                and parent.parent is not None
                and parent.parent.type == "call_expression"
            ):
                listed = [
                    _go_method(context.folder, c, content)
                    for c in parent.parent.child_by_field_name(
                        "arguments"
                    ).named_children
                ]
                if None in listed:
                    context.unread.append(
                        Unread(
                            context.file,
                            node.start_point.row + 1,
                            "route",
                            "method_not_literal",
                        )
                    )
                    continue
                methods = listed
            path, base_unknown = _unknown_base(path)
            handler_node = positional[1]
            found = []
            if handler_node.type == "identifier":
                found = package_functions.get(text(handler_node, content), [])
            elif handler_node.type == "selector_expression":
                name = text(handler_node.child_by_field_name("field"), content)
                found = package_methods.get(name, [])
            handler = found[0] if len(found) == 1 else None
            context.route(node, methods, path, "go-net-http", handler, base_unknown)
            continue
        if operand_text not in http_names:
            continue
        if field in ("Get", "Head") and positional:
            context.request(
                node,
                field.upper(),
                context.folder.evaluate(positional[0]),
                "go-net-http",
            )
        elif field in ("Post", "PostForm") and positional:
            context.request(
                node, "POST", context.folder.evaluate(positional[0]), "go-net-http"
            )
        elif field in ("NewRequest", "NewRequestWithContext"):
            offset = 1 if field == "NewRequestWithContext" else 0
            if len(positional) < offset + 2:
                continue
            method = _go_method(context.folder, positional[offset], content)
            context.request(
                node,
                method,
                context.folder.evaluate(positional[offset + 1]),
                "go-net-http",
            )


def extract_web(
    tree,
    content,
    repository_id,
    file,
    definitions,
    language,
    *,
    package=None,
    nest=None,
    externals=None,
):
    """Routes, requests and unread facts of one non-Java source file.

    ``nest`` is the (global prefix, unread reason) of the NestJS application
    a TypeScript file belongs to; ``externals`` the values of names the file
    imports or has defined (``script_constants``)."""
    context = _Context(
        tree, content, repository_id, file, definitions, language, externals
    )
    if language == "python":
        extract_python(context)
    elif language in ("javascript", "typescript"):
        extract_javascript(context, nest)
    elif language == "go":
        extract_go(context, package or ({}, {}))
    return context.routes, context.requests, context.unread
