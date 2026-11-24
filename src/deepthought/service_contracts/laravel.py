"""Laravel and Lumen routes in PHP route files.

Routes are calls on the ``Route`` facade (imported from
``Illuminate\\Support\\Facades``, or used unqualified in a project whose
composer.json requires ``laravel/framework``) or, in a Lumen project, on
``$router``:

- ``get``, ``post``, ``put``, ``patch``, ``delete``, ``options``, ``any``
  and ``match([...], ...)`` with a literal path; ``view`` (GET) and
  ``redirect``/``permanentRedirect`` (any method);
- ``resource``/``apiResource`` (and the plural forms with an array), with
  ``only``/``except`` and dotted nested names;
- groups: ``Route::prefix('x')->...->group(function () {...})``,
  ``Route::group(['prefix' => 'x'], function () {...})`` and
  ``controller(C::class)`` groups, nested.

A route file's own prefix comes from the code that loads it
(``route_loaders``): ``Route::prefix('api')->group(base_path('routes/api.php'))``
in a RouteServiceProvider, ``withRouting(api: ..., apiPrefix: ...)`` in
``bootstrap/app.php`` (``api`` by default), or a Lumen
``$app->router->group([...], function ($router) { require ...; })``.

The handler is the controller method an action names: ``[C::class, 'm']``,
``'C@m'``, an invokable ``C::class`` (``__invoke``), a method name in a
``controller`` group, or ``['uses' => ...]``. The class is resolved through
the file's ``use`` imports, else by its unique short name.
"""

import posixpath
import re

from ..syntax import identifier
from .http import ANY_METHOD, PLACEHOLDER
from .model import HttpRoute, Unread

FACADE = "Illuminate\\Support\\Facades\\Route"
_VERBS = {
    "get": ("GET",),
    "post": ("POST",),
    "put": ("PUT",),
    "patch": ("PATCH",),
    "delete": ("DELETE",),
    "options": ("OPTIONS",),
    "any": (ANY_METHOD,),
    "view": ("GET",),
    "redirect": (ANY_METHOD,),
    "permanentRedirect": (ANY_METHOD,),
}
_LUMEN_VERBS = ("get", "post", "put", "patch", "delete", "options")
# Resource actions: (name, methods, suffix after the collection path).
_RESOURCE = (
    ("index", ("GET",), ""),
    ("create", ("GET",), "/create"),
    ("store", ("POST",), ""),
    ("show", ("GET",), "/{var}"),
    ("edit", ("GET",), "/{var}/edit"),
    ("update", ("PATCH", "PUT"), "/{var}"),
    ("destroy", ("DELETE",), "/{var}"),
)
_API_ACTIONS = ("index", "store", "show", "update", "destroy")
# Chain links that do not change a route's path or handler.
_NEUTRAL = {
    "middleware",
    "withoutMiddleware",
    "name",
    "as",
    "domain",
    "where",
    "whereNumber",
    "whereAlpha",
    "whereAlphaNumeric",
    "whereUuid",
    "whereUlid",
    "whereIn",
    "scopeBindings",
    "withoutScopedBindings",
    "withTrashed",
    "missing",
    "can",
    "names",
    "parameters",
    "shallow",
    "middlewareFor",
    "withoutMiddlewareFor",
    "block",
    "lock",
}


def _text(node, content):
    return content[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def _arguments(node, content):
    """(positional value nodes, {name: value node}) of a call's arguments."""
    arguments = node.child_by_field_name("arguments")
    positional, named = [], {}
    for argument in arguments.named_children if arguments is not None else ():
        if argument.type != "argument" or not argument.named_children:
            continue
        name = argument.child_by_field_name("name")
        value = argument.named_children[-1]
        if name is not None:
            named[_text(name, content)] = value
        else:
            positional.append(value)
    return positional, named


def _array(node, content):
    """{key: value node} and [value nodes] of an array literal."""
    keyed, values = {}, []
    if node is None or node.type != "array_creation_expression":
        return keyed, values
    for element in node.named_children:
        if element.type != "array_element_initializer":
            continue
        children = element.named_children
        if len(children) == 2:
            key = _string(children[0], content)
            if key is not None:
                keyed[key] = children[1]
        elif len(children) == 1:
            values.append(children[0])
    return keyed, values


def _string(node, content):
    """The text of a literal string without interpolation, else None."""
    if node is None or node.type not in ("string", "encapsed_string"):
        return None
    if any(
        c.type not in ("string_content", "escape_sequence") for c in node.named_children
    ):
        return None
    return "".join(_text(c, content) for c in node.named_children)


def _uses(root, content):
    """Local class name -> fully qualified name, from ``use`` clauses, and
    the file's namespace."""
    names, namespace = {}, ""
    for node in _walk(root):
        if node.type == "namespace_definition":
            name = node.child_by_field_name("name")
            if name is not None:
                namespace = _text(name, content)
        if node.type != "namespace_use_clause":
            continue
        written = [
            c for c in node.named_children if c.type in ("qualified_name", "name")
        ]
        if not written:
            continue
        full = _text(written[0], content).lstrip("\\")
        alias = node.child_by_field_name("alias")
        names[
            _text(alias, content) if alias is not None else full.rpartition("\\")[2]
        ] = full
    return names, namespace


def _chain(node, content):
    """The links of a call chain from its base outward, as (name, call
    node), and the base: ("facade", name) for ``X::first(...)``, ("variable",
    name) for ``$x->first(...)``; None when ``node`` is not a call chain."""
    links = []
    current = node
    while current is not None and current.type == "member_call_expression":
        name = current.child_by_field_name("name")
        links.append((_text(name, content), current))
        current = current.child_by_field_name("object")
    if current is None:
        return None
    if current.type == "scoped_call_expression":
        scope = current.child_by_field_name("scope")
        name = current.child_by_field_name("name")
        links.append((_text(name, content), current))
        return ("facade", _text(scope, content).lstrip("\\")), links[::-1]
    if current.type == "variable_name" and links:
        return ("variable", _text(current, content)), links[::-1]
    if current.type == "member_access_expression" and links:
        return ("variable", _text(current, content)), links[::-1]
    return None


def _join(*parts):
    """Laravel's path join: each part trimmed of slashes; optional
    parameters (``{id?}``) are plain parameters."""
    kept = [p.strip("/") for p in parts if p and p.strip("/")]
    path = "/" + "/".join(kept)
    return re.sub(r"\{([^{}?]+)\?\}", r"{\1}", path)


def _base_path(node, content, file, project):
    """The repository path a route file argument names: ``base_path('x')``
    or ``__DIR__ . '/x'``; None otherwise."""
    if node is None:
        return None
    if node.type == "function_call_expression":
        function = node.child_by_field_name("function")
        positional = [
            a.named_children[-1]
            for a in node.child_by_field_name("arguments").named_children
            if a.type == "argument" and a.named_children
        ]
        if (
            _text(function, content).lstrip("\\") == "base_path"
            and len(positional) == 1
            and _string(positional[0], content) is not None
        ):
            joined = posixpath.join(
                project, _string(positional[0], content).lstrip("/")
            )
            return posixpath.normpath(joined) if joined else None
        return None
    if node.type == "binary_expression":
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if _text(left, content) == "__DIR__" and _string(right, content) is not None:
            joined = posixpath.join(
                posixpath.dirname(file), _string(right, content).lstrip("/")
            )
            return posixpath.normpath(joined)
    return None


def route_loaders(tree, content, file, project):
    """(route file, prefix, controller namespace) for each route file
    ``file`` loads: a
    ``group`` whose argument is ``base_path(...)``/``__DIR__ . '...'`` under
    the chain's ``prefix`` (or group array ``prefix``),
    ``withRouting(web: ..., api: ..., apiPrefix: ...)``, a ``require`` of a
    route file inside a ``group`` closure, and ``loadRoutesFrom``.
    ``project`` is the directory ``base_path`` names."""
    found = []
    root = tree.root_node
    for node in _walk(root):
        if node.type not in ("member_call_expression", "scoped_call_expression"):
            continue
        name = _text(node.child_by_field_name("name"), content)
        positional, named = _arguments(node, content)
        if name == "withRouting":
            api_prefix = "api"
            if "apiPrefix" in named:
                api_prefix = _string(named["apiPrefix"], content)
            for key in ("web", "api"):
                target = _base_path(named.get(key), content, file, project)
                if target is not None:
                    prefix = api_prefix if key == "api" else ""
                    found.append(
                        (target, PLACEHOLDER if prefix is None else prefix, "")
                    )
            continue
        if name == "loadRoutesFrom" and positional:
            target = _base_path(positional[0], content, file, project)
            if target is not None:
                found.append((target, "", ""))
            continue
        if name != "group" or not positional:
            continue
        prefix, namespace = _group_settings(node, content, root)
        target = _base_path(positional[-1], content, file, project)
        if target is not None:
            found.append((target, prefix, namespace))
            continue
        if positional[-1].type in ("anonymous_function", "arrow_function"):
            for inner in _walk(positional[-1]):
                if (
                    inner.type
                    in (
                        "include_expression",
                        "require_expression",
                        "require_once_expression",
                        "include_once_expression",
                    )
                    and inner.named_children
                ):
                    target = _base_path(
                        inner.named_children[-1], content, file, project
                    )
                    if target is not None:
                        found.append((target, prefix, namespace))
    return found


def _namespace_value(node, content, root):
    """A literal namespace, or the literal default of the class property
    ``$this->namespace`` names; None when unknown, "" for ``null``."""
    if node is None:
        return None
    if _text(node, content).lower() == "null":
        return ""
    value = _string(node, content)
    if value is not None:
        return value.replace("\\\\", "\\").lstrip("\\")
    if _text(node, content) == "$this->namespace":
        for candidate in _walk(root):
            if candidate.type != "property_element":
                continue
            children = candidate.named_children
            if len(children) == 2 and _text(children[0], content) == "$namespace":
                return _namespace_value(children[1], content, root)
            if len(children) == 1 and _text(children[0], content) == "$namespace":
                return ""
    return None


def _group_settings(group, content, root):
    """(prefix, namespace) a ``group`` call's own chain and array give."""
    chain = _chain(group, content)
    prefix, namespace = "", ""
    if chain is not None:
        for name, call in chain[1]:
            positional, _ = _arguments(call, content)
            if name == "prefix" and positional:
                value = _string(positional[0], content)
                prefix = _join(prefix, value if value is not None else PLACEHOLDER)
            if name == "namespace" and positional:
                namespace = _namespace_value(positional[0], content, root) or ""
            if call is group:
                keyed, _ = _array(positional[0] if positional else None, content)
                if "prefix" in keyed:
                    value = _string(keyed["prefix"], content)
                    prefix = _join(prefix, value if value is not None else PLACEHOLDER)
                if "namespace" in keyed:
                    namespace = (
                        _namespace_value(keyed["namespace"], content, root) or ""
                    )
    return prefix.rstrip("/"), namespace


class _Handlers:
    """Controller method definitions by fully qualified and short names."""

    def __init__(self, definitions, project):
        self.by_symbol, self.by_short = {}, {}
        inside = project.rstrip("/") + "/" if project else ""
        for d in definitions:
            if d.kind != "Method" or not d.file.endswith(".php"):
                continue
            if not d.file.startswith(inside):
                continue
            self.by_symbol.setdefault(d.symbol, []).append(d.id)
            short = ".".join(d.symbol.split(".")[-2:])
            self.by_short.setdefault(short, []).append(d.id)

    def find(self, cls, method):
        """The method ``method`` of class ``cls`` (qualified with
        backslashes, or a short name), when exactly one is found."""
        if cls is None or method is None:
            return None
        symbol = cls.lstrip("\\").replace("\\", ".") + "." + method
        found = self.by_symbol.get(symbol)
        if found and len(found) == 1:
            return found[0]
        short = cls.rpartition("\\")[2] + "." + method
        found = self.by_short.get(short)
        return found[0] if found and len(found) == 1 else None


def extract_laravel(
    tree,
    content,
    repository_id,
    file,
    definitions,
    *,
    framework,
    project="",
    prefix="",
    namespace="",
):
    """Routes and unread sites of one PHP file of a Laravel (``framework``
    "laravel") or Lumen ("lumen") project in directory ``project``;
    ``prefix`` and ``namespace`` are what the code that loads the file
    gives its routes and controller names."""
    root = tree.root_node
    uses, namespace = _uses(root, content)
    facades = {name for name, full in uses.items() if full == FACADE}
    if framework == "laravel" and "Route" not in uses:
        facades.add("Route")
    if not facades and framework != "lumen":
        return [], []
    handlers = _Handlers(definitions, project)
    routes, unread = [], []
    ordinals = {}

    def make_id(key):
        ordinal = ordinals.get(key, 0)
        ordinals[key] = ordinal + 1
        return identifier(repository_id, file, key, "HttpRoute", ordinal)

    def qualified(written):
        written = written.lstrip("\\") if written.startswith("\\") else written
        head, _, rest = written.partition("\\")
        if head in uses:
            return uses[head] + ("\\" + rest if rest else "")
        return written

    def class_of(node):
        """The class a ``C::class`` expression names."""
        if node is not None and node.type == "class_constant_access_expression":
            parts = node.named_children
            if len(parts) == 2 and _text(parts[1], content) == "class":
                return qualified(_text(parts[0], content))
        return None

    def handler(action, context):
        """The definition id an action names, or None."""
        if action is None:
            return None
        if action.type == "array_creation_expression":
            keyed, values = _array(action, content)
            if "uses" in keyed:
                return handler(keyed["uses"], context)
            if len(values) == 2:
                return handlers.find(class_of(values[0]), _string(values[1], content))
            return None
        cls = class_of(action)
        if cls is not None:
            return handlers.find(cls, "__invoke")
        written = _string(action, content)
        if written is None:
            return None
        if "@" in written:
            cls, _, method = written.partition("@")
            if not cls.startswith("\\") and context["namespace"]:
                cls = context["namespace"].rstrip("\\") + "\\" + cls
            return handlers.find(qualified(cls), method)
        if context["controller"] is not None:
            return handlers.find(context["controller"], written)
        return None

    def add(node, methods, path, handler_id):
        if PLACEHOLDER in path:
            unread.append(
                Unread(file, node.start_point.row + 1, "route", "path_not_literal")
            )
            return
        routes.append(
            HttpRoute(
                id=make_id(f"{','.join(methods)} {path}"),
                file=file,
                line=node.start_point.row + 1,
                column=node.start_point.column,
                handler_id=handler_id,
                methods=tuple(sorted(set(methods))),
                path=path,
                framework=framework,
            )
        )

    def literal(node):
        value = _string(node, content)
        return value if value is not None else PLACEHOLDER

    def resources(node, name, controller, api, links, context):
        actions = _API_ACTIONS if api else tuple(a for a, _, _ in _RESOURCE)
        for link, call in links:
            positional, _ = _arguments(call, content)
            if link in ("only", "except") and positional:
                listed = (
                    [_string(v, content) for v in _array(positional[0], content)[1]]
                    if positional[0].type == "array_creation_expression"
                    else [_string(v, content) for v in positional]
                )
                if None in listed:
                    return
                actions = tuple(a for a in actions if (a in listed) == (link == "only"))
        if name is None:
            add(node, ("GET",), PLACEHOLDER, None)
            return
        parts = name.split(".")
        base = context["prefix"]
        for parent in parts[:-1]:
            base = _join(base, parent, "{var}")
        base = _join(base, parts[-1])
        for action, methods, suffix in _RESOURCE:
            if action in actions:
                add(
                    node,
                    methods,
                    _join(base + suffix),
                    handlers.find(controller, action),
                )

    def visit(body, context):
        for node in body.named_children:
            if node.type == "expression_statement" and node.named_children:
                statement(node.named_children[0], context)
            elif node.type in (
                "compound_statement",
                "if_statement",
                "else_clause",
                "else_if_clause",
            ):
                visit(node, context)

    def statement(expression, context):
        chain = _chain(expression, content)
        if chain is None:
            return
        (kind, base), links = chain
        if kind == "facade" and base not in facades:
            return
        if kind == "variable" and not (
            framework == "lumen" and base in ("$router", "$app->router")
        ):
            return
        context = dict(context)
        for index, (name, call) in enumerate(links):
            positional, named = _arguments(call, content)
            if name == "prefix" and positional:
                context["prefix"] = _join(context["prefix"], literal(positional[0]))
            elif name == "controller" and positional:
                context["controller"] = class_of(positional[0])
            elif name == "namespace" and positional:
                value = _string(positional[0], content)
                context["namespace"] = value or ""
            elif name in _NEUTRAL:
                continue
            elif name == "group":
                if not positional:
                    return
                keyed, _ = _array(positional[0], content)
                if "prefix" in keyed:
                    context["prefix"] = _join(
                        context["prefix"], literal(keyed["prefix"])
                    )
                if "namespace" in keyed:
                    value = _string(keyed["namespace"], content) or ""
                    context["namespace"] = (
                        value.lstrip("\\")
                        if value.startswith("\\") or not context["namespace"]
                        else context["namespace"].rstrip("\\") + "\\" + value
                    )
                if "controller" in keyed:
                    context["controller"] = class_of(keyed["controller"])
                closure = positional[-1]
                if closure.type in ("anonymous_function", "arrow_function"):
                    inner = closure.child_by_field_name("body")
                    if inner is not None:
                        visit(inner, context)
                return
            elif name in _VERBS and (kind == "facade" or name in _LUMEN_VERBS):
                if not positional:
                    return
                path = _join(context["prefix"], literal(positional[0]))
                if name in ("redirect", "permanentRedirect", "view"):
                    add(call, _VERBS[name], path, None)
                    return
                action = positional[1] if len(positional) > 1 else named.get("action")
                add(call, _VERBS[name], path, handler(action, context))
                return
            elif name == "match" and len(positional) >= 2:
                methods = [
                    _string(v, content) for v in _array(positional[0], content)[1]
                ]
                if not methods or None in methods:
                    unread.append(
                        Unread(
                            file,
                            call.start_point.row + 1,
                            "route",
                            "method_not_literal",
                        )
                    )
                    return
                path = _join(context["prefix"], literal(positional[1]))
                action = positional[2] if len(positional) > 2 else None
                add(call, [m.upper() for m in methods], path, handler(action, context))
                return
            elif name in ("resource", "apiResource") and len(positional) >= 2:
                resources(
                    call,
                    _string(positional[0], content),
                    class_of(positional[1]),
                    name == "apiResource",
                    links[index + 1 :],
                    context,
                )
                return
            elif name in ("resources", "apiResources") and positional:
                keyed, _ = _array(positional[0], content)
                for resource, controller in keyed.items():
                    resources(
                        call,
                        resource,
                        class_of(controller),
                        name == "apiResources",
                        (),
                        context,
                    )
                return
            else:
                return

    visit(root, {"prefix": prefix, "controller": None, "namespace": namespace})
    return routes, unread
