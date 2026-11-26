"""HTTP routes and requests in Python, JavaScript/TypeScript and Go sources.

Servers: Flask and FastAPI route decorators, Express routes, and Go
``net/http``/gorilla ``HandleFunc``/``Handle``. Clients: Python ``requests``
and ``httpx`` module calls, their ``Session``/``Client`` objects and
``aiohttp`` sessions, Locust ``client``, JavaScript ``fetch``, ``axios``,
``request``, AngularJS ``$http``, Angular ``HttpClient`` and
``httpResource``, WebSocket and SockJS handshakes, and
Go ``net/http``. A framework or client object is recognised
through the file's own imports and bindings, or, for a Python route
decorator whose router another module defines, through the router the
scip-python index resolves the receiver name to; URLs are folded by
``folding.Folder``.
"""

import re

from ..syntax import identifier
from .folding import Folder, text
from .http import ANY_METHOD, HTTP_METHODS, PLACEHOLDER, join_paths
from .model import HttpRequest, HttpRoute, Unread
from .script_constants import fold_through_calls
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
# Client objects whose methods send requests: their client name, and the
# keyword that sets a base URL. httpx appends the request path to the base
# path; aiohttp joins it as a relative reference (RFC 3986).
_PY_SESSIONS = {
    "requests.Session": ("requests", None),
    "requests.sessions.Session": ("requests", None),
    "httpx.Client": ("httpx", "base_url"),
    "httpx.AsyncClient": ("httpx", "base_url"),
    "aiohttp.ClientSession": ("aiohttp", "base_url"),
}
_URL_SCHEME = re.compile(r"(?i)^[a-z][a-z0-9+.-]*://")
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
        self.calls_of = None
        self.python_router_of = None
        # Python and Express: {router name: full prefixes} of the file's
        # mounted routers.
        self.mounts = {}
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

    def route(
        self,
        node,
        methods,
        path,
        framework,
        handler_id,
        base_unknown=False,
        payloads=(),
        receiver_indexed=False,
    ):
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
                # Every unknown base read here is text written before the
                # path (``_unknown_base``).
                base_written=base_unknown,
                payloads=tuple(payloads),
                receiver_indexed=receiver_indexed,
            )
        )

    def request(self, node, method, url, client, argument_from=()):
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
                argument_from=tuple(argument_from),
            )
        )

    def request_folded(self, node, compute, client):
        """Add the requests ``compute(folder)`` gives as (method, URL). A
        request whose method or URL is built from parameters of the
        module-level function around the site takes, instead, each one that
        calls of the function give (``self.calls_of``)."""
        found = fold_through_calls(
            self.folder,
            compute,
            self.calls_of,
            lambda value: value[0] is None or PLACEHOLDER in value[1],
        )
        for (method, url), calls in found:
            self.request(node, method, url, client, calls)


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
            # ``from . import x`` names ``.x``, not ``..x``.
            joint = "" if module.strip(".") == "" else "."
            for child in node.children_by_field_name("name"):
                if child.type == "dotted_name":
                    names[text(child, content)] = (
                        f"{module}{joint}{text(child, content)}"
                    )
                elif child.type == "aliased_import":
                    names[text(child.child_by_field_name("alias"), content)] = (
                        f"{module}{joint}"
                        f"{text(child.child_by_field_name('name'), content)}"
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


# Mounting one router in another: (framework, keyword giving the prefix).
_PY_MOUNTS = {
    "include_router": ("fastapi", "prefix"),
    "register_blueprint": ("flask", "url_prefix"),
}


def python_routers(tree, content):
    """Routers of one Python file and the routers it mounts, for composing
    route prefixes across files (``compose_python_prefixes``).

    Returns (routers, mounts, imports). ``routers`` maps each name bound
    exactly once in the file to a Flask/FastAPI application, ``Blueprint``
    or ``APIRouter`` to (framework, its own prefix). ``mounts`` lists
    (parent name, child, prefix or None) for each
    ``parent.include_router(child, prefix=...)`` and
    ``parent.register_blueprint(child, url_prefix=...)`` whose parent is a
    name and whose child is a name or ``name.attribute``; the child is the
    dotted origin through the file's imports, or the local name. ``imports``
    maps the file's imported names to their origins."""
    root = tree.root_node
    folder = Folder("python", root, content)
    imports = _python_imports(root, content)
    bound, routers, mounts = {}, {}, []
    for node in _walk(root):
        if node.type == "assignment":
            left = node.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                bound.setdefault(text(left, content), []).append(
                    node.child_by_field_name("right")
                )
        elif node.type == "call":
            function = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if function is None or function.type != "attribute" or arguments is None:
                continue
            mount = _PY_MOUNTS.get(
                text(function.child_by_field_name("attribute"), content)
            )
            parent = function.child_by_field_name("object")
            if mount is None or parent.type != "identifier":
                continue
            keywords, positional = _keyword_arguments(arguments, content)
            child = (
                positional[0]
                if positional
                else keywords.get("router", keywords.get("blueprint"))
            )
            if child is None or child.type not in ("identifier", "attribute"):
                continue
            written = text(child, content)
            head, dot, rest = written.partition(".")
            if dot and head not in imports:
                continue
            origin = imports[head] + dot + rest if head in imports else written
            prefix = keywords.get(mount[1])
            mounts.append(
                (
                    text(parent, content),
                    origin,
                    None if prefix is None else folder.evaluate(prefix),
                )
            )
    for name, values in bound.items():
        value = values[0]
        if len(values) != 1 or value is None or value.type != "call":
            continue
        origin = _python_origin(value.child_by_field_name("function"), imports, content)
        module, _, constructor = (origin or "").rpartition(".")
        framework = _PY_FRAMEWORKS.get((module, constructor))
        if framework is None:
            continue
        keywords, _ = _keyword_arguments(
            value.child_by_field_name("arguments"), content
        )
        own = ""
        if _PY_PREFIX.get(constructor) in keywords:
            own = folder.evaluate(keywords[_PY_PREFIX[constructor]])
        routers[name] = (framework, own)
    return routers, mounts, imports


def _fastapi_payloads(function, keywords):
    """(direction, start, end, None) of what a FastAPI path operation
    writes for its payloads: each parameter of the handler ``function``
    (``request``, the parameter's span; which of them is the body is decided
    where the names resolve, ``python_payloads``) and its response, the
    decorator's ``response_model`` or else the return annotation."""
    found = []
    parameters = function.child_by_field_name("parameters")
    for parameter in [] if parameters is None else parameters.named_children:
        if parameter.type in ("typed_parameter", "typed_default_parameter"):
            found.append(("request", parameter.start_byte, parameter.end_byte, None))
    model = keywords.get("response_model")
    if model is None:
        model = function.child_by_field_name("return_type")
    if model is not None and model.type != "none":
        found.append(("response", model.start_byte, model.end_byte, None))
    return tuple(found)


def _python_module_file(file, module, files):
    """The analysed file a dotted ``module`` imported from ``file`` names:
    relative to the file's package for leading dots, else under the nearest
    enclosing directory that holds it."""
    dots = len(module) - len(module.lstrip("."))
    parts = [p for p in module[dots:].split(".") if p]
    directory = file.rpartition("/")[0]
    if dots:
        for _ in range(dots - 1):
            if not directory:
                return None
            directory = directory.rpartition("/")[0]
        bases = [directory]
    else:
        bases = []
        while True:
            bases.append(directory)
            if not directory:
                break
            directory = directory.rpartition("/")[0]
    for base in bases:
        path = "/".join(([base] if base else []) + parts)
        for candidate in (path + "/__init__.py", path + ".py"):
            if candidate in files:
                return candidate
    return None


def compose_python_prefixes(found):
    """Full route prefixes of mounted Python routers.

    ``found`` maps each Python file to its ``python_routers`` result. Returns
    {(file, router name): sorted full prefixes} for each router mounted in
    another: FastAPI joins the parent's prefix, the ``include_router`` prefix
    and the router's own; Flask joins the parent's prefix and the
    ``register_blueprint`` ``url_prefix``, which replaces the blueprint's own.
    A mount whose parent is not a known router counts as mounted at the root.
    Routers that are never mounted are left out, and keep their own prefix."""
    files = set(found)

    def resolve(file, origin, depth=0):
        """(file, name) of the router a child origin names, following one
        re-export per step."""
        if depth > 4:
            return None
        if "." not in origin.lstrip(".") and not origin.startswith("."):
            return (file, origin) if origin in found[file][0] else None
        dots = len(origin) - len(origin.lstrip("."))
        parts = origin[dots:].split(".")
        module, name = "." * dots + ".".join(parts[:-1]), parts[-1]
        target = _python_module_file(file, module, files)
        if target is None:
            return None
        routers, _, imports = found[target]
        if name in routers:
            return (target, name)
        if name in imports:
            return resolve(target, imports[name], depth + 1)
        return None

    parents = {}
    for file, (routers, mounts, _) in found.items():
        for parent, origin, prefix in mounts:
            child = resolve(file, origin)
            if child is None:
                continue
            framework = found[child[0]][0][child[1]][0]
            key = (file, parent) if parent in routers else None
            parents.setdefault(child, []).append((key, framework, prefix))

    def full(key, active):
        own = found[key[0]][0][key[1]][1]
        if key not in parents or key in active:
            return {own}
        prefixes = set()
        for parent, framework, prefix in parents[key]:
            outer = full(parent, active | {key}) if parent is not None else {""}
            if framework == "flask":
                tail = own if prefix is None else prefix
            else:
                tail = (prefix or "") + own
            prefixes.update(o.rstrip("/") + tail for o in outer)
        return prefixes

    return {key: sorted(full(key, frozenset())) for key in parents}


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
        mounted = context.mounts.get(text(owner, content))
        if mounted is not None:
            return framework, mounted
        prefix = ""
        if _PY_PREFIX.get(constructor) in keywords:
            prefix = context.folder.evaluate(keywords[_PY_PREFIX[constructor]])
        return framework, [prefix]

    def session(value):
        """(client, base URL: "" for none, else text) when ``value`` makes a
        client object, else None."""
        if value is None or value.type != "call":
            return None
        origin = _python_origin(value.child_by_field_name("function"), imports, content)
        found = _PY_SESSIONS.get(origin or "")
        if found is None:
            return None
        client, keyword = found
        base = ""
        if keyword is not None:
            keywords, _ = _keyword_arguments(
                value.child_by_field_name("arguments"), content
            )
            if keyword in keywords:
                base = context.folder.evaluate(keywords[keyword])
        return client, base

    # self.NAME assigned exactly once in a class, to a client object.
    members = {}
    for node in _walk(root):
        if node.type != "assignment":
            continue
        left = node.child_by_field_name("left")
        if left is None or left.type != "attribute":
            continue
        written = text(left, content)
        if not written.startswith("self."):
            continue
        owner = node.parent
        while owner is not None and owner.type != "class_definition":
            owner = owner.parent
        if owner is not None:
            members.setdefault((owner.id, written), []).append(
                node.child_by_field_name("right")
            )

    def receiver(owner_node):
        """(client, base) of the client object ``owner_node`` names: a name
        bound once to one, the target of an enclosing ``with ... as name``,
        or ``self.NAME`` set once in the class to one; else None."""
        written = text(owner_node, content)
        if owner_node.type == "identifier":
            ancestor = owner_node.parent
            while ancestor is not None:
                if ancestor.type == "with_statement":
                    for item in _walk(ancestor):
                        value = item.child_by_field_name("value")
                        if (
                            item.type == "with_item"
                            and value is not None
                            and value.type == "as_pattern"
                        ):
                            alias = value.child_by_field_name("alias")
                            if alias is not None and text(alias, content) == written:
                                return session(value.named_children[0])
                if ancestor.type in ("function_definition", "class_definition"):
                    break
                ancestor = ancestor.parent
            found = context.folder.binding(owner_node)
            return session(found[1]) if found is not None else None
        if owner_node.type == "attribute" and written.startswith("self."):
            owner = owner_node.parent
            while owner is not None and owner.type != "class_definition":
                owner = owner.parent
            assigned = members.get((owner.id, written)) if owner else None
            if assigned and len(assigned) == 1:
                return session(assigned[0])
        return None

    def joined(client, base, url):
        """``url`` sent from a ``client`` object with base URL ``base``. An
        absolute URL, or a base that is not a literal absolute URL, leaves
        ``url`` as written."""
        if not _URL_SCHEME.match(base) or _URL_SCHEME.match(url):
            return url
        if client == "httpx":
            return base.rstrip("/") + "/" + url.lstrip("/") if url else base
        match = _URL_SCHEME.match(base)
        origin = base[: match.end()] + base[match.end() :].partition("/")[0]
        if url.startswith("/"):
            return origin + url
        return (
            base.rpartition("/")[0] + "/" + url
            if "/" in base[match.end() :]
            else origin + "/" + url
        )

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
                owner_node = function.child_by_field_name("object")
                app = app_of(owner_node)
                indexed = False
                if app is None and context.python_router_of is not None:
                    # A router another module defines, which the index
                    # resolves the name to (``from app.api import bp``).
                    app = context.python_router_of(context.file, owner_node)
                    indexed = app is not None
                if app is None:
                    continue
                framework, prefixes = app
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
                payloads = (
                    _fastapi_payloads(definition, keywords)
                    if framework == "fastapi"
                    else ()
                )
                for prefix in prefixes:
                    path = _python_path(
                        prefix + context.folder.evaluate(path_node), framework
                    )
                    # A prefix the code does not fix (a setting) leaves the
                    # route, with its base marked unknown.
                    path, base_unknown = _unknown_base(path)
                    context.route(
                        call,
                        methods,
                        path,
                        framework,
                        handler,
                        base_unknown,
                        payloads=payloads,
                        receiver_indexed=indexed,
                    )
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
            client, base = None, ""
            if imports.get(owner) in _PY_CLIENTS:
                client = imports[owner]
            elif (
                locust and owner.endswith(".client") and owner_node.type == "attribute"
            ):
                client = "locust"
            elif verb in _VERBS or verb == "request":
                found = receiver(owner_node)
                if found is not None:
                    client, base = found
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
            context.request(
                node,
                method,
                joined(client, base, context.folder.evaluate(url_node)),
                client,
            )


# -- JavaScript / TypeScript ------------------------------------------------


def _method(folder, node, default="GET"):
    """The HTTP method a node names, ``default`` without one, None when it
    is not one fixed method."""
    if node is None:
        return default
    method = folder.evaluate(node).upper()
    return method if method in HTTP_METHODS else None


_ORVAL_HEADER = re.compile(rb"^ \* Generated by orval\b", re.MULTILINE)
_HEY_API_HEADER = re.compile(
    rb"^// This file is auto-generated by @hey-api/openapi-ts\b", re.MULTILINE
)


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
                and name.type in ("identifier", "object_pattern")
                and value is not None
                and value.type == "call_expression"
                and text(value.child_by_field_name("function"), content) == "require"
            ):
                arguments = value.child_by_field_name("arguments").named_children
                if not arguments or arguments[0].type != "string":
                    continue
                module = text(arguments[0], content)[1:-1]
                if name.type == "identifier":
                    names[text(name, content)] = module
                    continue
                # const { Router, json: parse } = require('express')
                for item in name.named_children:
                    if item.type == "shorthand_property_identifier_pattern":
                        names[text(item, content)] = f"{module}.{text(item, content)}"
                    elif item.type == "pair_pattern":
                        key = item.child_by_field_name("key")
                        local = item.child_by_field_name("value")
                        if local is not None and local.type == "identifier":
                            names[text(local, content)] = (
                                f"{module}.{text(key, content)}"
                            )
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
    """{key: value node} of an object literal; a shorthand property's value
    is its own node."""
    found = {}
    if node is None or node.type != "object":
        return found
    for pair in node.named_children:
        if pair.type == "pair":
            key = pair.child_by_field_name("key")
            name = text(key, content).strip("'\"")
            found[name] = pair.child_by_field_name("value")
        elif pair.type == "shorthand_property_identifier":
            # {topic} is {topic: topic}: the node folds as the name.
            found[text(pair, content)] = pair
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
    context.request_folded(
        value, lambda f: ("GET", _browser_url(f.evaluate(value))), client
    )


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
                method_node = None
                if value.type == "object":
                    options = _js_object(value, content)
                    if "url" not in options:
                        continue
                    method_node = options.get("method")
                    value = options["url"]
                context.request_folded(
                    node,
                    lambda f, m=method_node, u=value: (
                        _method(f, m),
                        _browser_url(f.evaluate(u)),
                    ),
                    "angular-http",
                )
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
            method_node, url_node, fixed = positional[0], positional[1], None
        else:
            method_node, url_node, fixed = None, positional[0], _VERBS[verb]
        context.request_folded(
            node,
            lambda f, m=method_node, u=url_node, x=fixed: (
                x or _method(f, m),
                _browser_url(f.evaluate(u)),
            ),
            "angular-http",
        )


def _express_router_value(value, modules, content):
    """Whether ``value`` makes an Express app or Router: ``express()``,
    ``express.Router()`` or an imported ``Router()``."""
    if value is None or value.type != "call_expression":
        return False
    callee = text(value.child_by_field_name("function"), content)
    head, _, rest = callee.partition(".")
    return modules.get(callee) in ("express", "express.Router") or (
        modules.get(head) == "express" and rest == "Router"
    )


def express_routers(tree, content):
    """Express apps and routers of one script file, what it exports and the
    routers it mounts, for composing route prefixes across files
    (``compose_express_prefixes``).

    Returns (routers, exports, mounts, modules). ``routers`` is the set of
    names bound exactly once to an Express app or Router; ``exports`` maps
    "default" (``module.exports = x``, ``export default x``) and each named
    export (``exports.n = x``, ``export const n = ...``) to the local name;
    ``mounts`` lists (parent name, prefixes, children) for each
    ``parent.use([path,] ...)``: the folded path (several for an array, ""
    for none) and each argument that may be a router, as ("local", name) or
    (module specifier, export name). ``modules`` is ``_js_modules``."""
    root = tree.root_node
    modules = _js_modules(root, content)
    folder = Folder("javascript", root, content)
    bound, exports, mounts = {}, {}, []

    def child_of(node):
        if node.type == "identifier":
            name = text(node, content)
            return ("local", name) if name not in modules else ("import", modules[name])
        if (
            node.type == "call_expression"
            and text(node.child_by_field_name("function"), content) == "require"
        ):
            arguments = node.child_by_field_name("arguments").named_children
            if arguments and arguments[0].type == "string":
                return ("import", text(arguments[0], content)[1:-1])
        return None

    for node in _walk(root):
        if node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            if name is not None and name.type == "identifier":
                bound.setdefault(text(name, content), []).append(
                    node.child_by_field_name("value")
                )
                ancestor = node.parent.parent if node.parent is not None else None
                if ancestor is not None and ancestor.type == "export_statement":
                    exports[text(name, content)] = text(name, content)
        elif node.type == "export_statement":
            value = node.child_by_field_name("value")
            if value is not None and value.type == "identifier":
                exports["default"] = text(value, content)
        elif node.type == "assignment_expression":
            left = text(node.child_by_field_name("left"), content)
            right = node.child_by_field_name("right")
            if right is None or right.type != "identifier":
                continue
            if left == "module.exports":
                exports["default"] = text(right, content)
            elif left.startswith(("module.exports.", "exports.")):
                exports[left.rpartition(".")[2]] = text(right, content)
        elif node.type == "call_expression":
            function = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if (
                function is None
                or function.type != "member_expression"
                or arguments is None
                or text(function.child_by_field_name("property"), content) != "use"
            ):
                continue
            parent = function.child_by_field_name("object")
            if parent.type != "identifier":
                continue
            positional = [c for c in arguments.named_children if c.type != "comment"]
            if not positional:
                continue
            first = positional[0]
            if first.type in ("string", "template_string", "binary_expression") or (
                first.type == "identifier"
                and text(first, content) not in modules
                and folder.evaluate(first).startswith("/")
            ):
                prefixes, rest = [folder.evaluate(first)], positional[1:]
            elif first.type == "array":
                prefixes = [
                    folder.evaluate(c)
                    for c in first.named_children
                    if c.type != "comment"
                ]
                rest = positional[1:]
            else:
                prefixes, rest = [""], positional
            children = [c for c in (child_of(n) for n in rest) if c is not None]
            if children and prefixes:
                mounts.append((text(parent, content), prefixes, children))
    routers = {
        name
        for name, values in bound.items()
        if len(values) == 1 and _express_router_value(values[0], modules, content)
    }
    return routers, exports, mounts, modules


def compose_express_prefixes(found, resolve):
    """Full prefixes of Express routers mounted with ``use``.

    ``found`` maps each script file to its ``express_routers`` result and
    ``resolve(file, specifier)`` gives the file a module specifier names, or
    None. Returns {(file, router name): sorted full prefixes}: the parent's
    prefixes joined with each mount path. A mount on a name that is not a
    known router counts as mounted at the root; routers never mounted are
    left out."""

    def target(file, kind, value, depth=0):
        if depth > 4:
            return None
        routers, exports, _, modules = found[file]
        if kind == "local":
            if value in routers:
                return (file, value)
            if value in exports:
                return target(file, "local", exports[value], depth + 1)
            if value in modules:
                return target(file, "import", modules[value], depth + 1)
            return None
        module, export = value, "default"
        if resolve(file, module) is None and "." in module.rpartition("/")[2]:
            module, _, export = module.rpartition(".")
        other = resolve(file, module)
        if other is None or other not in found:
            return None
        name = found[other][1].get(export)
        return target(other, "local", name, depth + 1) if name else None

    parents = {}
    for file, (routers, _, mounts, _) in found.items():
        for parent, prefixes, children in mounts:
            for kind, value in children:
                child = target(file, kind, value)
                if child is None:
                    continue
                key = (file, parent) if parent in routers else None
                parents.setdefault(child, []).append((key, prefixes))

    def full(key, active):
        if key not in parents or key in active:
            return {""}
        found_prefixes = set()
        for parent, prefixes in parents[key]:
            outer = full(parent, active | {key}) if parent is not None else {""}
            for o in outer:
                for prefix in prefixes:
                    found_prefixes.add((o + prefix).rstrip("/"))
        return found_prefixes

    return {key: sorted(full(key, frozenset())) for key in parents}


class OpenapiFetchBase(str):
    """The base URL of an openapi-fetch client, as ``axios_base`` gives it
    for a ``createClient(config)`` call."""


# The modules openapi-fetch's createClient is imported from: the default
# export or the named one.
_OPENAPI_FETCH = (
    "openapi-fetch",
    "openapi-fetch.default",
    "openapi-fetch.createClient",
)
# openapi-fetch's client methods, one per HTTP method, named in upper case.
_OPENAPI_FETCH_VERBS = frozenset(HTTP_METHODS)


def _client_base(value, folder, content, key):
    """The base URL a client factory call's first argument gives under
    ``key``: "" without one, PLACEHOLDER when the config is not an object
    literal or spreads another object."""
    arguments = value.child_by_field_name("arguments")
    positional = [
        c
        for c in (arguments.named_children if arguments is not None else ())
        if c.type != "comment"
    ]
    if not positional:
        return ""
    config = positional[0]
    if config.type != "object" or any(
        c.type == "spread_element" for c in config.named_children
    ):
        return PLACEHOLDER
    base = _js_object(config, content).get(key)
    return "" if base is None else folder.evaluate(base)


def openapi_fetch_url(base, path):
    """The URL an openapi-fetch client sends ``path`` to: the path, with
    each ``{parameter}`` filled in at run time, appended to the base URL
    without its trailing slashes."""
    return base.rstrip("/") + re.sub(r"\{[^{}/]+\}", PLACEHOLDER, path)


def axios_base(value, modules, folder, content):
    """The base URL ("" for none, PLACEHOLDER-bearing text when not fixed)
    of the axios instance an ``axios.create(config)`` call ``value`` makes,
    or, as an OpenapiFetchBase, of the openapi-fetch client a
    ``createClient(config)`` call makes; else None. A config that is not an
    object literal, or that spreads another object, has an unknown base
    URL."""
    if value is None or value.type != "call_expression":
        return None
    callee = value.child_by_field_name("function")
    if callee is not None and callee.type == "identifier":
        if modules.get(text(callee, content)) in _OPENAPI_FETCH:
            return OpenapiFetchBase(_client_base(value, folder, content, "baseUrl"))
        return None
    if callee is None or callee.type != "member_expression":
        return None
    owner = text(callee.child_by_field_name("object"), content)
    if text(callee.child_by_field_name("property"), content) != "create":
        return None
    owner = owner[: -len(".default")] if owner.endswith(".default") else owner
    if modules.get(owner) != "axios":
        return None
    return _client_base(value, folder, content, "baseURL")


def axios_url(base, url):
    """``url`` sent from an axios instance with base URL ``base``, as axios
    joins them: an absolute or protocol-relative URL ignores the base,
    otherwise the two are joined with one slash. A protocol-relative result
    is read as http."""
    if base and not re.match(r"(?i)^([a-z][a-z0-9+.-]*:)?//", url):
        url = base.rstrip("/") + "/" + url.lstrip("/") if url else base
    return "http:" + url if url.startswith("//") else url


def axios_exports(tree, content, language, externals=None):
    """{exported name, "default" for the default export: base URL} of the
    axios instances an ES module exports: ``export default NAME`` or
    ``export { NAME as x }`` of a name bound once, ``export const NAME =``,
    and ``export default`` of the ``axios.create(...)`` call itself."""
    root = tree.root_node
    modules = _js_modules(root, content)
    if "axios" not in modules.values() and not set(_OPENAPI_FETCH) & set(
        modules.values()
    ):
        return {}
    folder = Folder(language, root, content, externals)
    found = {}

    def bound(name_node):
        binding = folder.binding(name_node)
        return (
            axios_base(binding[1], modules, folder, content)
            if binding is not None
            else None
        )

    for node in root.named_children:
        if node.type != "export_statement" or node.child_by_field_name("source"):
            continue
        value = node.child_by_field_name("value")
        declaration = node.child_by_field_name("declaration")
        if value is not None:
            base = (
                bound(value)
                if value.type == "identifier"
                else axios_base(value, modules, folder, content)
            )
            if base is not None:
                found["default"] = base
        elif declaration is not None and declaration.type in (
            "lexical_declaration",
            "variable_declaration",
        ):
            for declarator in declaration.named_children:
                name = declarator.child_by_field_name("name")
                if declarator.type == "variable_declarator" and name is not None:
                    base = bound(name)
                    if base is not None:
                        found[text(name, content)] = base
        for clause in node.named_children:
            if clause.type != "export_clause":
                continue
            for spec in clause.named_children:
                name = spec.child_by_field_name("name")
                alias = spec.child_by_field_name("alias")
                if name is None or name.type != "identifier":
                    continue
                base = bound(name)
                if base is not None:
                    found[text(alias or name, content)] = base
    return found


def axios_imports(tree, content, exports_of):
    """{local name: base URL} of the axios instances a module imports;
    ``exports_of(specifier)`` is what ``axios_exports`` gives for the module
    the specifier names ({} when it is not a repository module)."""
    found = {}
    for node in tree.root_node.named_children:
        source = node.child_by_field_name("source")
        if node.type != "import_statement" or source is None:
            continue
        exported = exports_of(text(source, content)[1:-1])
        if not exported:
            continue
        for clause in node.named_children:
            if clause.type != "import_clause":
                continue
            for item in clause.named_children:
                if item.type == "identifier" and "default" in exported:
                    found[text(item, content)] = exported["default"]
                elif item.type == "named_imports":
                    for spec in item.named_children:
                        if spec.type != "import_specifier":
                            continue
                        name = text(spec.child_by_field_name("name"), content)
                        alias = spec.child_by_field_name("alias")
                        if name in exported:
                            found[text(alias, content) if alias else name] = exported[
                                name
                            ]
    return found


def _class_members(node, name, content):
    """Values given to ``this.NAME`` in the class around ``node``: its field
    initializers (a declaration without one gives none) and ``this.NAME =
    value`` assignments."""
    owner = node.parent
    while owner is not None and owner.type not in ("class_declaration", "class"):
        owner = owner.parent
    if owner is None:
        return []
    values = []
    for item in _walk(owner):
        if item.type in ("public_field_definition", "field_definition"):
            field = item.child_by_field_name("name") or item.child_by_field_name(
                "property"
            )
            value = item.child_by_field_name("value")
            if field is not None and text(field, content) == name and value:
                values.append(value)
        elif item.type == "assignment_expression":
            left = item.child_by_field_name("left")
            if left is not None and text(left, content) == "this." + name:
                values.append(item.child_by_field_name("right"))
    return values


def extract_javascript(context, nest=None, axios_imports=None):
    content, root = context.content, context.root
    modules = _js_modules(root, content)
    axios_imports = axios_imports or {}

    def instance_config(node, base, config):
        """A request an axios instance sends from a config object literal
        (``instance.request({url, method})`` or ``instance({...})``); the
        method defaults to GET."""
        options = _js_object(config, content)
        if "url" not in options:
            return
        context.request_folded(
            node,
            lambda f, m=options.get("method"), u=options["url"], b=base: (
                _method(f, m),
                axios_url(b, f.evaluate(u)),
            ),
            "axios",
        )

    # A file orval generated from an OpenAPI spec sends each operation
    # through the mutator its configuration names, called with the request
    # config orval builds: {url, method, ...}. The mutator is the project's
    # own function (often wrapping an axios instance whose base URL is set
    # at run time), so the URL is read as the path orval wrote.
    orval = _ORVAL_HEADER.search(content[:2048]) is not None

    def orval_request(node, config):
        if config is None or config.type != "object":
            return False
        options = _js_object(config, content)
        if "url" not in options or "method" not in options:
            return False
        context.request_folded(
            node,
            lambda f, m=options["method"], u=options["url"]: (
                _method(f, m),
                f.evaluate(u),
            ),
            "orval",
        )
        return True

    # A file @hey-api/openapi-ts generated from an OpenAPI spec sends each
    # operation through its client: client.<method>({url, ...}) with the
    # method in the name, or __request(OpenAPI, {method, url, ...}) in its
    # legacy clients. The URL is the spec's path, each {parameter} filled in
    # at run time, under the base URL the client is configured with at run
    # time, which is not read.
    hey_api = _HEY_API_HEADER.search(content[:2048]) is not None

    def hey_api_request(node, config, method=None):
        if config is None or config.type != "object":
            return False
        options = _js_object(config, content)
        if "url" not in options or (method is None and "method" not in options):
            return False
        context.request_folded(
            node,
            lambda f, m=options.get("method"), u=options["url"]: (
                method or _method(f, m),
                openapi_fetch_url("", f.evaluate(u)),
            ),
            "hey-api",
        )
        return True

    def instance_base(owner):
        """The base URL of the axios instance or openapi-fetch client
        (an OpenapiFetchBase) ``owner`` names: a name bound once to
        ``axios.create(...)`` or ``createClient(...)`` or imported as one
        (``axios_imports``),
        or ``this.NAME`` given exactly one value in its class, that call;
        else None."""
        if owner.type == "identifier":
            found = context.folder.binding(owner)
            if found is not None:
                return axios_base(found[1], modules, context.folder, content)
            return axios_imports.get(text(owner, content))
        if (
            owner.type == "member_expression"
            and text(owner.child_by_field_name("object"), content) == "this"
        ):
            name = text(owner.child_by_field_name("property"), content)
            values = _class_members(owner, name, content)
            if len(values) == 1:
                return axios_base(values[0], modules, context.folder, content)
        return None

    if any(m.startswith(_ANGULAR_HTTP + ".") for m in modules.values()):
        extract_angular(context, modules)
    if context.language == "typescript" and any(
        m.startswith(_NEST + ".") for m in modules.values()
    ):
        extract_nest(context, modules, nest or (None, None, None, {}))
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
                context.request_folded(
                    node,
                    lambda f, u=positional[0]: ("GET", _browser_url(f.evaluate(u))),
                    socket,
                )
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
            context.request_folded(
                node,
                lambda f, u=positional[0]: ("GET", _browser_url(f.evaluate(u))),
                socket,
            )
            continue
        if function.type == "member_expression":
            owner = text(function.child_by_field_name("object"), content)
            verb = text(function.child_by_field_name("property"), content)
            if (
                is_app(function.child_by_field_name("object"))
                and (verb in _VERBS or verb == "all")
                and len(positional) >= 2
            ):
                handler_node = positional[-1]
                handler = (
                    context.named_function(text(handler_node, content))
                    if handler_node.type == "identifier"
                    else None
                )
                methods = [ANY_METHOD] if verb == "all" else [_VERBS[verb]]
                declared = context.folder.evaluate(positional[0])
                for prefix in context.mounts.get(owner, [""]):
                    path, base_unknown = _express_path(prefix + declared), False
                    if prefix.startswith(PLACEHOLDER):
                        # Mounted at a path that is not literal.
                        path, base_unknown = _unknown_base(path)
                    context.route(node, methods, path, "express", handler, base_unknown)
                continue
            if owner == "$http" and verb in _VERBS and positional:
                context.request_folded(
                    node,
                    lambda f, u=positional[0], m=_VERBS[verb]: (
                        m,
                        _browser_url(f.evaluate(u)),
                    ),
                    "angularjs-http",
                )
                continue
            if (
                hey_api
                and verb in _VERBS
                and positional
                and hey_api_request(node, positional[0], _VERBS[verb])
            ):
                continue
            module = modules.get(owner)
            if (
                module in ("axios", "request", "superagent")
                and verb in _VERBS
                and positional
            ):
                context.request_folded(
                    node,
                    lambda f, u=positional[0], m=_VERBS[verb]: (m, f.evaluate(u)),
                    module,
                )
                continue
            if verb in _OPENAPI_FETCH_VERBS and positional:
                base = instance_base(function.child_by_field_name("object"))
                if isinstance(base, OpenapiFetchBase):
                    context.request_folded(
                        node,
                        lambda f, u=positional[0], m=verb, b=base: (
                            m,
                            openapi_fetch_url(b, f.evaluate(u)),
                        ),
                        "openapi-fetch",
                    )
                continue
            base = (
                instance_base(function.child_by_field_name("object"))
                if (verb in _VERBS or verb == "request") and positional
                else None
            )
            if isinstance(base, OpenapiFetchBase):
                continue
            if base is not None and verb == "request":
                instance_config(node, base, positional[0])
            elif base is not None:
                context.request_folded(
                    node,
                    lambda f, u=positional[0], m=_VERBS[verb], b=base: (
                        m,
                        axios_url(b, f.evaluate(u)),
                    ),
                    "axios",
                )
            continue
        if function.type == "identifier" and positional:
            base = instance_base(function)
            if base is not None and not isinstance(base, OpenapiFetchBase):
                instance_config(node, base, positional[0])
                continue
            if orval and orval_request(node, positional[0]):
                continue
            if (
                hey_api
                and len(positional) >= 2
                and hey_api_request(node, positional[1])
            ):
                continue
        if written == "$http" and positional:
            options = _js_object(positional[0], content)
            if "url" not in options:
                continue
            context.request_folded(
                node,
                lambda f, m=options.get("method"), u=options["url"]: (
                    _method(f, m),
                    _browser_url(f.evaluate(u)),
                ),
                "angularjs-http",
            )
            continue
        if written == "fetch" and positional:
            options = _js_object(
                positional[1] if len(positional) > 1 else None, content
            )
            context.request_folded(
                node,
                lambda f, m=options.get("method"), u=positional[0]: (
                    _method(f, m),
                    f.evaluate(u),
                ),
                "fetch",
            )
        elif modules.get(written) in ("request", "axios") and positional:
            options = _js_object(positional[0], content)
            if options:
                url_node = options.get("url", options.get("uri"))
                method_node = options.get("method")
                if url_node is None:
                    continue
            else:
                method_node, url_node = None, positional[0]
            context.request_folded(
                node,
                lambda f, m=method_node, u=url_node: (_method(f, m), f.evaluate(u)),
                modules[written],
            )


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


def _nest_versions(folder, node, content):
    """The versions a Nest version value names: a string, an array of them,
    or VERSION_NEUTRAL (None: no version); None when it is not one of
    these."""
    items = (
        [c for c in node.named_children if c.type != "comment"]
        if node.type == "array"
        else [node]
    )
    found = []
    for item in items:
        if text(item, content).rpartition(".")[2] == "VERSION_NEUTRAL":
            found.append(None)
            continue
        value = folder.evaluate(item)
        if PLACEHOLDER in value or not value:
            return None
        found.append(value)
    return tuple(found) or None


def _nest_versioning(folder, positional, content):
    """("versioning", (versions change the path, URI prefix, default
    versions)) of an ``enableVersioning(options)`` call whose options are
    literal, else ("unread", "versioning"). URI versioning puts
    ``{prefix}{version}`` before the controller path (prefix "v" unless
    given, none for ``prefix: false``); header, media type and custom
    versioning leave the path alone."""
    unread = ("unread", "versioning")
    if len(positional) != 1 or positional[0].type != "object":
        return unread
    if any(c.type == "spread_element" for c in positional[0].named_children):
        return unread
    options = _js_object(positional[0], content)
    kind = text(options["type"], content) if "type" in options else ""
    member = kind.rpartition(".")[2]
    if not kind.startswith("VersioningType.") or member not in (
        "URI",
        "HEADER",
        "MEDIA_TYPE",
        "CUSTOM",
    ):
        return unread
    prefix = "v"
    if "prefix" in options:
        written = text(options["prefix"], content)
        prefix = "" if written == "false" else folder.evaluate(options["prefix"])
        if PLACEHOLDER in prefix:
            return unread
    defaults = (None,)
    if "defaultVersion" in options:
        defaults = _nest_versions(folder, options["defaultVersion"], content)
        if defaults is None:
            return unread
    return "versioning", (member == "URI", prefix, defaults)


def nest_settings(tree, content, language):
    """Application-wide NestJS settings a file makes: ("prefix", text) for
    ``x.setGlobalPrefix("text")``, ("versioning", ...) for a literal
    ``enableVersioning`` (``_nest_versioning``), and ("unread", reason) for a
    prefix that is not one literal argument, versioning options that are
    not literal, or ``RouterModule``."""
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
            found.append(_nest_versioning(folder, positional, content))
        elif owner == "RouterModule":
            routes = (
                _nest_router_routes(folder, positional[0], content, "")
                if name == "register" and len(positional) == 1
                else None
            )
            found.append(
                ("router", tuple(routes))
                if routes is not None
                else ("unread", "router_module")
            )
    for node in _walk(tree.root_node):
        if node.type != "class_declaration":
            continue
        decorators = [c for c in node.named_children if c.type == "decorator"]
        if node.parent is not None and node.parent.type == "export_statement":
            decorators += [
                c for c in node.parent.named_children if c.type == "decorator"
            ]
        for decorator in decorators:
            call = decorator.named_children[0] if decorator.named_children else None
            if call is None or call.type != "call_expression":
                continue
            if text(call.child_by_field_name("function"), content) != "Module":
                continue
            arguments = call.child_by_field_name("arguments")
            options = _js_object(
                arguments.named_children[0]
                if arguments is not None and arguments.named_children
                else None,
                content,
            )
            listed = options.get("controllers")
            name = text(node.child_by_field_name("name"), content)
            if listed is None:
                continue
            if listed.type != "array" or any(
                c.type != "identifier"
                for c in listed.named_children
                if c.type != "comment"
            ):
                found.append(("module", (name, None)))
                continue
            found.append(
                (
                    "module",
                    (
                        name,
                        tuple(
                            text(c, content)
                            for c in listed.named_children
                            if c.type == "identifier"
                        ),
                    ),
                )
            )
    return found


def _nest_router_routes(folder, node, content, parent):
    """[(module class name, path)] of a literal ``RouterModule.register``
    routes array, child paths joined to their parent's; None when any
    entry, path or module is not literal."""
    if node is None or node.type != "array":
        return None
    found = []
    for entry in node.named_children:
        if entry.type == "comment":
            continue
        if entry.type != "object" or any(
            c.type == "spread_element" for c in entry.named_children
        ):
            return None
        options = _js_object(entry, content)
        path = folder.evaluate(options["path"]) if "path" in options else ""
        if PLACEHOLDER in path:
            return None
        path = join_paths(parent, path)
        module = options.get("module")
        if module is not None:
            if module.type != "identifier":
                return None
            found.append((text(module, content), path))
        if "children" in options:
            children = _nest_router_routes(folder, options["children"], content, path)
            if children is None:
                return None
            found.extend(children)
    return found


def _type_span(node):
    """(start, end) bytes of the type a type_annotation holds."""
    if node is None or node.type != "type_annotation" or not node.named_children:
        return None
    written = node.named_children[0]
    return written.start_byte, written.end_byte


def _nest_payloads(method, modules, content):
    """(direction, start, end, key) of the types a Nest handler writes for
    its request body (the parameter decorated ``@Body()`` of @nestjs/common;
    ``key`` is the body property ``@Body('key')`` names, else None) and its
    response (the method's return type)."""
    found = []
    parameters = method.child_by_field_name("parameters")
    for parameter in [] if parameters is None else parameters.named_children:
        for decorator in parameter.named_children:
            if decorator.type != "decorator":
                continue
            name, arguments = _nest_decorator(decorator, modules, content)
            if name != "Body":
                continue
            span = _type_span(parameter.child_by_field_name("type"))
            if span is None:
                break
            key = None
            if arguments and arguments[0].type == "string":
                key = text(arguments[0], content)[1:-1]
            found.append(("request", *span, key))
            break
    span = _type_span(method.child_by_field_name("return_type"))
    if span is not None:
        found.append(("response", *span, None))
    return tuple(found)


def extract_nest(context, modules, nest):
    """Routes of NestJS controllers: a class decorated ``@Controller(path)``
    whose methods carry ``@Get(path)`` and the like, all from
    @nestjs/common; ``nest`` is (global prefix, unread reason, versioning)
    of the application this file belongs to. A controller's ``version``
    option, or a method's ``@Version``, is read only when the application
    enables versioning with literal options; then a route without one takes
    the default version."""
    content = context.content
    prefix, reason, versioning, routed = nest
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
        class_versions = versioning[2] if versioning is not None else (None,)
        if first is not None and first.type == "object":
            options = _js_object(first, content)
            if "version" in options:
                class_versions = (
                    _nest_versions(context.folder, options["version"], content)
                    if versioning is not None
                    else None
                )
                if class_versions is None:
                    context.unread.append(
                        Unread(context.file, line, "route", "version")
                    )
                    continue
            first = options.get("path")
        class_name = text(node.child_by_field_name("name"), content)
        module_path = routed.get(class_name, "")
        if module_path is None:
            context.unread.append(Unread(context.file, line, "route", "router_module"))
            continue
        bases = [
            base if PLACEHOLDER in base else join_paths(module_path, base)
            for base in _nest_paths(context, first)
        ]
        body = node.child_by_field_name("body")
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
            payloads = _nest_payloads(member, modules, content)
            versions = class_versions
            for item in decorators:
                name, args = _nest_decorator(item, modules, content)
                if name == "Version":
                    versions = (
                        _nest_versions(context.folder, args[0], content)
                        if versioning is not None and len(args) == 1
                        else None
                    )
                    if versions is None:
                        context.unread.append(
                            Unread(
                                context.file,
                                item.start_point.row + 1,
                                "route",
                                "version",
                            )
                        )
                        break
            else:
                uri, version_prefix = (
                    versioning[:2] if versioning is not None else (False, "")
                )
                segments = sorted(
                    {
                        version_prefix + v if uri and v is not None else ""
                        for v in versions
                    }
                )
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
                            for segment in segments:
                                full = _nest_path(
                                    join_paths(prefix or "", segment, base, path)
                                )
                                if PLACEHOLDER in base or PLACEHOLDER in path:
                                    full = PLACEHOLDER
                                context.route(
                                    item,
                                    [_NEST_VERBS[name]],
                                    full,
                                    "nestjs",
                                    handler,
                                    payloads=payloads,
                                )


# -- Go -----------------------------------------------------------------------


def _go_imports(root, content):
    """local package name -> import path."""
    names = {}
    for node in _walk(root):
        if node.type == "import_spec":
            path = text(node.child_by_field_name("path"), content).strip('"`')
            alias = node.child_by_field_name("name")
            parts = path.split("/")
            # A major version suffix is not the package name (echo/v4).
            if len(parts) > 1 and re.fullmatch(r"v[0-9]+", parts[-1]):
                parts.pop()
            local = text(alias, content) if alias is not None else parts[-1]
            names[local] = path
    return names


def _go_method(folder, node, content):
    written = text(node, content)
    if written in _GO_METHODS:
        return _GO_METHODS[written]
    value = folder.evaluate(node).upper()
    return value if value in HTTP_METHODS else None


def _go_router_path(path, framework=None):
    """Gin/Echo ``:id`` as ``{id}`` and a ``*rest`` segment as ``{*rest}``;
    chi's ``{id}`` and ``{id:regex}`` are already route variables, and its
    ``*`` matches the rest of the path."""
    if framework == "chi":
        return "/".join("{*}" if part == "*" else part for part in path.split("/"))
    return "/".join(
        "{" + part[1:] + "}"
        if part.startswith(":") and len(part) > 1
        else "{*" + part[1:] + "}"
        if part.startswith("*")
        else part
        for part in path.split("/")
    )


def extract_go(context, package, go_routes):
    """``package`` is (functions, methods): function and method names mapped
    to the declarations of that name in the file's package directory, to
    anchor ``handler`` and ``s.handler`` net/http handlers. ``go_routes``
    are the file's gin, Echo, chi and Fiber routes, as the type checker
    gives them (``go_routes``), by the (line, column, method) of their
    call."""
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
        known = go_routes.get(
            (node.start_point.row + 1, node.start_point.column, field)
        )
        if known is not None:
            route, handler = known
            for full in route.paths:
                path, base_unknown = _unknown_base(
                    _go_router_path(full, route.framework)
                )
                context.route(
                    node,
                    list(route.methods),
                    path,
                    route.framework,
                    handler,
                    base_unknown,
                )
            continue
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
    calls_of=None,
    mounts=None,
    axios_imported=None,
    go_routes=None,
    python_router_of=None,
):
    """Routes, requests and unread facts of one non-Java source file.

    ``nest`` is the (global prefix, unread reason) of the NestJS application
    a TypeScript file belongs to; ``externals`` the values of names the file
    imports or has defined (``script_constants``); ``calls_of(name)`` the
    calls of its module-level function ``name`` with their arguments, for
    JavaScript/TypeScript requests built from that function's parameters;
    ``mounts`` the full prefixes of a file's mounted Python or Express
    routers by name (``compose_python_prefixes``,
    ``compose_express_prefixes``); ``axios_imported`` the base URLs of the
    axios instances a script imports by name (``axios_imports``);
    ``go_routes`` the Go file's router routes from the type checker,
    {(line, column, method): (GoRoute, handler declaration id)};
    ``python_router_of(file, receiver node)`` the (framework, full prefixes)
    of a router another Python module defines, as the SCIP index resolves
    the receiver name (``_python_router_resolver``), or None."""
    context = _Context(
        tree, content, repository_id, file, definitions, language, externals
    )
    context.calls_of = calls_of
    context.mounts = mounts or {}
    context.python_router_of = python_router_of
    if language == "python":
        extract_python(context)
    elif language in ("javascript", "typescript"):
        extract_javascript(context, nest, axios_imported)
    elif language == "go":
        extract_go(context, package or ({}, {}), go_routes or {})
    return context.routes, context.requests, context.unread
