"""HTTP routes and requests in Python, JavaScript/TypeScript and Go sources.

Servers: Flask and FastAPI route decorators, Express routes, and Go
``net/http``/gorilla ``HandleFunc``/``Handle``. Clients: Python ``requests``
and ``httpx``, Locust ``client``, JavaScript ``fetch``, ``axios`` and
``request``, and Go ``net/http``. A framework or client object is recognised
only through the file's own imports and bindings; URLs are folded by
``folding.Folder``.
"""

import re

from ..syntax import identifier
from .folding import Folder, text
from .http import ANY_METHOD, HTTP_METHODS, PLACEHOLDER, join_paths
from .model import HttpRequest, HttpRoute, Unread

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
    def __init__(self, tree, content, repository_id, file, definitions, language):
        self.root = tree.root_node
        self.content = content
        self.repository_id = repository_id
        self.file = file
        self.language = language
        self.folder = Folder(language, self.root, content)
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


def extract_javascript(context):
    content, root = context.content, context.root
    modules = _js_modules(root, content)
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

    for node in _walk(root):
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if arguments is None or arguments.type != "arguments":
            continue
        positional = [c for c in arguments.named_children if c.type != "comment"]
        written = text(function, content)
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
    tree, content, repository_id, file, definitions, language, *, package=None
):
    """Routes, requests and unread facts of one non-Java source file."""
    context = _Context(tree, content, repository_id, file, definitions, language)
    if language == "python":
        extract_python(context)
    elif language in ("javascript", "typescript"):
        extract_javascript(context)
    elif language == "go":
        extract_go(context, package or ({}, {}))
    return context.routes, context.requests, context.unread
