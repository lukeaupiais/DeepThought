"""Django REST framework router routes, identified through the scip-python
index.

A router is a value the index resolves ``register`` calls on to DRF's
``BaseRouter.register`` (``rest_framework.routers``). Each registration of
a ViewSet yields the routes ``SimpleRouter`` generates for the actions the
ViewSet has: ``{prefix}/`` (``GET`` list, ``POST`` create) and
``{prefix}/{lookup}/`` (``GET`` retrieve, ``PUT`` update, ``PATCH``
partial_update, ``DELETE`` destroy), plus one route per ``@action`` method
(``detail`` placing it under the lookup, ``methods`` defaulting to
``GET``, ``url_path`` to the method name). A route is generated only when
the ViewSet has one of its actions, as DRF's ``get_method_map`` does.

The ViewSet's actions are read from its class: methods it or a repository
base class defines, and the actions of DRF's own ViewSet and mixin classes
among its bases (``ModelViewSet``, ``ListModelMixin``, ...), as the index
resolves each base. A ViewSet with a base the index resolves to neither is
not read (``viewset_base_not_resolved``): its actions are not known.

The routes join the patterns of the URLconf entries that include
``router.urls``. ``trailing_slash=False`` given to the router's
constructor drops the trailing slash. DefaultRouter's API root view and
format suffix patterns are DRF's own views and are not read.
"""

import re

from .folding import Folder, text
from .http import PLACEHOLDER

_REGISTER = re.compile(
    r"^scip-python python djangorestframework \S+ `rest_framework\.routers`/"
    r"\w*Router#register\(\)\.$"
)
_URLS = re.compile(
    r"^scip-python python djangorestframework \S+ `rest_framework\.routers`/"
    r"\w*Router#urls\(\)\.$"
)
_CONSTRUCTOR = re.compile(
    r"^scip-python python djangorestframework \S+ `rest_framework\.routers`/"
    r"\w*Router#(?:__init__\(\)\.)?$"
)
_ACTION = re.compile(
    r"^scip-python python djangorestframework \S+ `rest_framework\.decorators`/"
    r"action\(\)\.$"
)
_LIBRARY_CLASS = re.compile(
    r"^scip-python python djangorestframework \S+ "
    r"`rest_framework\.(?:viewsets|mixins|generics|views)`/(\w+)#$"
)

_ALL = frozenset(("list", "create", "retrieve", "update", "partial_update", "destroy"))
# Actions of DRF's own classes. Classes with none (GenericViewSet,
# ViewSet, ViewSetMixin, GenericAPIView, APIView) add nothing.
_LIBRARY_ACTIONS = {
    "ModelViewSet": _ALL,
    "ReadOnlyModelViewSet": frozenset(("list", "retrieve")),
    "ListModelMixin": frozenset(("list",)),
    "CreateModelMixin": frozenset(("create",)),
    "RetrieveModelMixin": frozenset(("retrieve",)),
    "UpdateModelMixin": frozenset(("update", "partial_update")),
    "DestroyModelMixin": frozenset(("destroy",)),
    "GenericViewSet": frozenset(),
    "ViewSet": frozenset(),
    "ViewSetMixin": frozenset(),
    "GenericAPIView": frozenset(),
    "APIView": frozenset(),
}
# SimpleRouter.routes: (detail, {HTTP method: action}).
_ROUTES = (
    (False, (("GET", "list"), ("POST", "create"))),
    (
        True,
        (
            ("GET", "retrieve"),
            ("PUT", "update"),
            ("PATCH", "partial_update"),
            ("DELETE", "destroy"),
        ),
    ),
)
_DEPTH = 8


def _symbols(references, file, node):
    return references.symbols(file, node.start_point.row + 1, node.start_point.column)


def _name_node(node):
    """The name a call's function or a base expression ends with."""
    if node is None:
        return None
    if node.type == "attribute":
        node = node.child_by_field_name("attribute")
    return node if node is not None and node.type == "identifier" else None


def _arguments(call):
    arguments = call.child_by_field_name("arguments")
    positional, keywords = [], {}
    for child in arguments.named_children if arguments else ():
        if child.type == "keyword_argument":
            name = child.child_by_field_name("name")
            value = child.child_by_field_name("value")
            if name is not None:
                keywords[name.text.decode()] = value
        elif child.type != "comment":
            positional.append(child)
    return positional, keywords


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


class DrfRouters:
    """The routers of the repository's Python sources and their
    registrations; ``routes_of(file, attribute)`` the route entries of a
    ``router.urls`` expression."""

    def __init__(self, python_sources, references, by_site, regex_path):
        self.references = references
        self.by_site = by_site
        self.by_line = {}
        for (file, line, _), declarations in by_site.items():
            self.by_line.setdefault((file, line), []).extend(declarations)
        self.regex_path = regex_path
        self.sources = {}
        self.folders = {}
        self.classes = {}
        self.unread = []
        # Router value by its definition site: trailing slash and the
        # (file, register call) of each registration.
        self.trailing = {}
        self.registrations = {}
        for coverage, content, tree in python_sources:
            file = coverage.file
            self.sources[file] = (content, tree)
            if b"register" not in content and b"Router" not in content:
                continue
            if not references.covers(file):
                continue
            for node in _walk(tree.root_node):
                if node.type == "assignment":
                    self._constructor(file, node)
                elif node.type == "call":
                    self._registration(file, node)

    def _site(self, file, node):
        """The definition site of the name ``node``, as the index gives it."""
        sites = self.references.definition_sites(
            file, node.start_point.row + 1, node.start_point.column
        )
        for site in sites or ():
            return (site[0], site[1], site[2])
        return None

    def _constructor(self, file, assignment):
        left = assignment.child_by_field_name("left")
        right = assignment.child_by_field_name("right")
        if left is None or left.type != "identifier" or right is None:
            return
        if right.type != "call":
            return
        name = _name_node(right.child_by_field_name("function"))
        if name is None or not any(
            _CONSTRUCTOR.match(s) for s in _symbols(self.references, file, name)
        ):
            return
        _, keywords = _arguments(right)
        slash = keywords.get("trailing_slash")
        trailing = "" if slash is not None and slash.type == "false" else "/"
        self.trailing[(file, left.start_point.row + 1, left.start_point.column)] = (
            trailing
        )

    def _registration(self, file, call):
        function = call.child_by_field_name("function")
        if function is None or function.type != "attribute":
            return
        name = function.child_by_field_name("attribute")
        receiver = function.child_by_field_name("object")
        if name is None or receiver is None or receiver.type != "identifier":
            return
        if not any(_REGISTER.match(s) for s in _symbols(self.references, file, name)):
            return
        site = self._site(file, receiver)
        if site is not None:
            self.registrations.setdefault(site, []).append((file, call))

    def is_urls(self, file, node):
        """Whether ``node`` is a ``router.urls`` expression of a DRF
        router, by the index."""
        return (
            node.type == "attribute"
            and node.child_by_field_name("attribute") is not None
            and self.references.covers(file)
            and any(
                _URLS.match(s)
                for s in _symbols(
                    self.references, file, node.child_by_field_name("attribute")
                )
            )
        )

    def routes_of(self, file, node):
        """(path, view declaration, register call, methods, file of the
        call) of each route the router ``node.urls`` generates."""
        receiver = node.child_by_field_name("object")
        if receiver is None or receiver.type != "identifier":
            return []
        site = self._site(file, receiver)
        if site is None:
            return []
        trailing = self.trailing.get(site, "/")
        found = []
        for call_file, call in self.registrations.get(site, ()):
            found.extend(self._registered(call_file, call, trailing))
        return found

    def _folder(self, file):
        if file not in self.folders:
            content, tree = self.sources[file]
            self.folders[file] = Folder("python", tree.root_node, content)
        return self.folders[file]

    def _literal(self, file, node):
        if node is None or node.type != "string":
            return None
        value = self._folder(file).evaluate(node)
        return None if PLACEHOLDER in value else value

    def _registered(self, file, call, trailing):
        positional, keywords = _arguments(call)
        prefix_node = positional[0] if positional else keywords.get("prefix")
        viewset_node = positional[1] if len(positional) > 1 else keywords.get("viewset")
        line = call.start_point.row + 1
        prefix = self._literal(file, prefix_node)
        prefix = None if prefix is None else self.regex_path(prefix)
        if prefix is None:
            self.unread.append((file, line, "path_not_literal"))
            return []
        name = _name_node(viewset_node)
        site = None if name is None else self._site(file, name)
        viewset = None if site is None else self._class(site)
        if viewset is None:
            self.unread.append((file, line, "view_not_resolved"))
            return []
        actions = self._actions(viewset, 0)
        if actions is None:
            self.unread.append((file, line, "viewset_base_not_resolved"))
            return []
        methods, extra = actions
        class_declaration = viewset[2]
        prefix = prefix.strip("/")
        found = []
        for detail, mapping in _ROUTES:
            path = prefix + ("/{var}" if detail else "") + trailing
            by_handler = {}
            for http, action in mapping:
                if action in methods:
                    handler = methods[action] or class_declaration
                    by_handler.setdefault(handler.id, (handler, []))[1].append(http)
            for handler, verbs in by_handler.values():
                found.append((path, handler, call, tuple(verbs), file))
        for detail, url_path, verbs, handler in extra:
            handler = handler or class_declaration
            path = prefix + ("/{var}" if detail else "") + "/" + url_path + trailing
            found.append((path, handler, call, verbs, file))
        return found

    def _class(self, site):
        """(file, class node, declaration) of the repository class whose
        name is defined at ``site``."""
        file, line, column = site
        if file not in self.sources:
            return None
        key = (file, line, column)
        if key not in self.classes:
            content, tree = self.sources[file]
            found = None
            for node in _walk(tree.root_node):
                if node.type != "class_definition":
                    continue
                name = node.child_by_field_name("name")
                if (
                    name is not None
                    and name.start_point.row + 1 == line
                    and name.start_point.column == column
                ):
                    declarations = self.by_site.get(
                        (file, line, text(name, content)), ()
                    )
                    if len(declarations) == 1:
                        found = (file, node, declarations[0])
                    break
            self.classes[key] = found
        return self.classes[key]

    def _actions(self, viewset, depth):
        """({action: declaration or None}, [(detail, url path, methods,
        declaration)]) of a ViewSet class, or None when a base is not
        known. Methods of the class win over its bases', as in the MRO."""
        if depth > _DEPTH:
            return None
        file, node, _ = viewset
        content = self.sources[file][0]
        methods, extra = {}, []
        superclasses = node.child_by_field_name("superclasses")
        for base in superclasses.named_children if superclasses else ():
            if base.type in ("keyword_argument", "comment"):
                continue
            name = _name_node(base)
            if name is None:
                return None
            library = None
            for symbol in _symbols(self.references, file, name):
                match = _LIBRARY_CLASS.match(symbol)
                if match and match.group(1) in _LIBRARY_ACTIONS:
                    library = _LIBRARY_ACTIONS[match.group(1)]
            if library is not None:
                for action in library:
                    methods.setdefault(action, None)
                continue
            site = self._site(file, name)
            parent = None if site is None else self._class(site)
            if parent is None:
                return None
            inherited = self._actions(parent, depth + 1)
            if inherited is None:
                return None
            for action, declaration in inherited[0].items():
                if methods.get(action) is None:
                    methods[action] = declaration
            extra.extend(inherited[1])
        body = node.child_by_field_name("body")
        own_extra = []
        for statement in body.named_children if body else ():
            decorators = []
            definition = statement
            if statement.type == "decorated_definition":
                decorators = [
                    c for c in statement.named_children if c.type == "decorator"
                ]
                definition = statement.child_by_field_name("definition")
            if definition is None or definition.type != "function_definition":
                continue
            name = definition.child_by_field_name("name")
            method = text(name, content)
            found = [
                d
                for d in self.by_line.get((file, name.start_point.row + 1), ())
                if d.symbol == method or d.symbol.endswith("." + method)
            ]
            declaration = found[0] if len(found) == 1 else None
            action = self._action(file, decorators, method)
            if action is not None:
                detail, url_path, verbs = action
                own_extra.append((detail, url_path, verbs, declaration))
            elif method in _ALL and declaration is not None:
                methods[method] = declaration
        overridden = {(d, p) for d, p, _, _ in own_extra}
        extra = [e for e in extra if (e[0], e[1]) not in overridden] + own_extra
        return methods, extra

    def _action(self, file, decorators, method):
        """(detail, url path, methods) of an ``@action`` decorator."""
        for decorator in decorators:
            call = decorator.named_children[0] if decorator.named_children else None
            if call is None or call.type != "call":
                continue
            name = _name_node(call.child_by_field_name("function"))
            if name is None or not any(
                _ACTION.match(s) for s in _symbols(self.references, file, name)
            ):
                continue
            _, keywords = _arguments(call)
            detail = keywords.get("detail")
            if detail is None or detail.type not in ("true", "false"):
                return None
            url_path = keywords.get("url_path")
            path = method if url_path is None else self._literal(file, url_path)
            path = None if path is None else self.regex_path(path)
            verbs = ("GET",)
            given = keywords.get("methods")
            if given is not None:
                if given.type not in ("list", "tuple"):
                    return None
                values = [self._literal(file, v) for v in given.named_children]
                if not values or None in values:
                    return None
                verbs = tuple(sorted({v.upper() for v in values}))
            if path is None:
                return None
            return detail.type == "true", path.strip("/"), verbs
        return None
