"""Go HTTP routes from the Go type checker's facts (``go_facts``).

A route is a method call whose receiver go/types types as a router of gin,
Echo, chi or Fiber, so the router is known by its type, not by the name the
code gives it. The router's prefix is followed through the references
gofacts gives: the variable or struct field it is assigned to, the call it
is (``r.Group("/v1").GET(...)``), the parameter of a module function a call
passes it to, or a func literal's parameter (chi's ``r.Route(prefix,
func(r chi.Router) {...})``). The handler is the module function or method
the handler argument names, or the one a call there calls (a handler
factory, ``h.Create()``).

The libraries' APIs (which methods register routes, where the handler and
the path are among the arguments) are written here: no tool states what a
router method does.
"""

from dataclasses import dataclass

from .http import ANY_METHOD, HTTP_METHODS, PLACEHOLDER


@dataclass(frozen=True)
class _Framework:
    name: str
    # Verb methods: method name -> HTTP method.
    verbs: dict
    # Methods registering any method: (path index, handler index).
    any: dict
    # Methods taking the method first: name -> (path index, first handler
    # index); the methods are a string (Handle, Add) or []string (Match).
    explicit: dict
    # Handler position among the arguments after the path: 0 or -1.
    handler: int
    # Methods returning a router under a prefix written first (Group).
    groups: frozenset
    # Methods returning the same router (Use, With).
    same: frozenset
    # Methods calling a func literal with the router under a prefix written
    # first (chi Route), or with the same router (chi Group).
    scoped: frozenset = frozenset()
    scoped_same: frozenset = frozenset()
    # The router types, and among them the roots, which no prefix is
    # added to (gin's Engine).
    types: frozenset = frozenset()
    roots: frozenset = frozenset()
    # Methods mounting a router under a prefix: (prefix, router) arguments
    # (chi's Mount, Fiber's Mount).
    mounts: frozenset = frozenset()


_UPPER = {m: m for m in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")}
_TITLE = {m.title(): m for m in HTTP_METHODS}

_GIN = _Framework(
    "gin",
    _UPPER,
    {"Any": (0, -1)},
    {"Handle": (1, 2), "Match": (1, 2)},
    -1,  # middleware first, the handler last
    frozenset({"Group"}),
    frozenset({"Use"}),
    types=frozenset({"Engine", "RouterGroup", "IRouter", "IRoutes"}),
    roots=frozenset({"Engine"}),
)
_ECHO = _Framework(
    "echo",
    _UPPER,
    {"Any": (0, 1)},
    {"Add": (1, 2), "Match": (1, 2)},
    0,  # the handler first, then route middleware
    frozenset({"Group"}),
    frozenset(),
    types=frozenset({"Echo", "Group"}),
    roots=frozenset({"Echo"}),
)
_CHI = _Framework(
    "chi",
    _TITLE,
    {"Handle": (0, -1), "HandleFunc": (0, -1)},
    {"Method": (1, 2), "MethodFunc": (1, 2)},
    -1,
    frozenset({"Route"}),
    frozenset({"With"}),
    frozenset({"Route"}),
    frozenset({"Group"}),
    types=frozenset({"Mux", "Router"}),
    roots=frozenset({"Mux"}),
    mounts=frozenset({"Mount"}),
)
# Fiber v2: Get(path, handlers ...Handler), the handler last.
_FIBER_V2 = _Framework(
    "fiber",
    _TITLE,
    {"All": (0, -1)},
    {"Add": (1, 2)},
    -1,
    frozenset({"Group"}),
    frozenset({"Use"}),
    frozenset({"Route"}),
    types=frozenset({"App", "Group", "Router"}),
    roots=frozenset({"App"}),
    mounts=frozenset({"Mount"}),
)
# Fiber v3: Get(path, handler, middleware ...Handler), which registers
# append([]any{handler}, middleware...) run in order, so the endpoint is
# the last (auth.Post("/logout", Protected(), Logout)); Route(path) returns
# a Register whose verbs take no path.
_FIBER_V3 = _Framework(
    "fiber",
    _TITLE,
    {"All": (0, -1)},
    {"Add": (1, 2)},
    -1,
    frozenset({"Group", "Route"}),
    frozenset({"Use"}),
    types=frozenset({"App", "Group", "Router", "Register"}),
    roots=frozenset({"App"}),
)

ROUTER_PACKAGES = {
    "github.com/gin-gonic/gin": _GIN,
    "github.com/labstack/echo/v4": _ECHO,
    "github.com/labstack/echo": _ECHO,
    "github.com/go-chi/chi/v5": _CHI,
    "github.com/go-chi/chi": _CHI,
    "github.com/gofiber/fiber/v2": _FIBER_V2,
    "github.com/gofiber/fiber/v3": _FIBER_V3,
}


@dataclass(frozen=True)
class GoRoute:
    """A route call: its framework, methods, full paths (PLACEHOLDER where
    the code does not fix the text, at the start for a router whose prefix
    is not followed), the reference of its handler function, and of the
    first parameter of a handler written as a func literal."""

    file: str
    line: int
    column: int  # 0-based
    method: str  # the router method called
    framework: str
    methods: tuple[str, ...]
    paths: tuple[str, ...]
    handler: tuple | None
    literal: tuple | None = None


def _joined(prefix, path):
    if not path:
        return prefix
    if path.startswith(PLACEHOLDER) and not prefix.strip(PLACEHOLDER + "/"):
        # An unknown prefix before a path that starts unknown: one unknown
        # base (a router not followed, under baseURL + "/items").
        return path
    return prefix.rstrip("/") + "/" + path.lstrip("/") if prefix else path


class GoRouters:
    """The routes of every router call in ``facts`` (a GoFacts)."""

    def __init__(self, facts):
        self.calls = {c.reference: c for c in facts.calls if c.reference is not None}
        self.assigned = {}
        self.scopes = {}
        for call in facts.calls:
            if self._framework(call) is None:
                continue
            if call.assigned is not None:
                self.assigned.setdefault(call.assigned, []).append(("call", call))
            for index, parameter in enumerate(call.literal_parameters):
                if parameter is not None:
                    self.scopes.setdefault(parameter, []).append((call, index))
        # Routers mounted under a prefix, by the reference of the variable
        # or field they are held in.
        self.mounted = {}
        for call in facts.calls:
            framework = self._framework(call)
            if (
                framework is not None
                and call.method in framework.mounts
                and len(call.refs) > 1
                and call.refs[1] is not None
                and call.refs[1][0] == "o"
            ):
                self.mounted.setdefault(call.refs[1], []).append(call)
        self.constructed = set()
        for new in facts.constructors:
            if new.type is not None and new.type[0] in ROUTER_PACKAGES:
                self.constructed.add(new.reference)
                if new.assigned is not None:
                    self.assigned.setdefault(new.assigned, []).append(("new", None))
        self.passed = {}
        for left, right in facts.aliases:
            if left is not None and right is not None:
                self.passed.setdefault(left, []).append(right)
        for passing in facts.passes:
            for argument, parameter in passing.arguments:
                if argument is not None and parameter is not None:
                    self.passed.setdefault(parameter, []).append(argument)

    @staticmethod
    def _framework(call):
        if call.receiver is None:
            return None
        framework = ROUTER_PACKAGES.get(call.receiver[0])
        if framework is None or call.receiver[1] not in framework.types:
            return None
        return framework

    def _parents(self, call, seen):
        """The prefixes of the router a router call is made on."""
        framework = self._framework(call)
        if call.receiver[1] in framework.roots:
            mounts = self.mounted.get(call.operand, ())
            if not mounts:
                return {""}
            # A root router mounted under a prefix (chi's r.Mount("/api",
            # api)) takes the prefix of each mount.
            found = set()
            for mount in mounts:
                own = self._text(mount, 0)
                found |= {_joined(p, own) for p in self._parents(mount, seen)}
            return found
        return self.prefixes(call.operand, seen)

    def prefixes(self, ref, seen=frozenset()):
        """The prefixes of the router ``ref`` holds; PLACEHOLDER for one
        that is not followed."""
        if ref is None or ref in seen or len(seen) > 24:
            return {PLACEHOLDER}
        seen = seen | {ref}
        if ref[0] == "c":
            if ref in self.constructed:
                return {""}
            call = self.calls.get(ref)
            if call is None:
                return {PLACEHOLDER}
            return self._made(call, seen)
        found = set()
        for kind, call in self.assigned.get(ref, ()):
            found |= {""} if kind == "new" else self._made(call, seen)
        for call, index in self.scopes.get(ref, ()):
            framework = self._framework(call)
            parents = self._parents(call, seen)
            if call.method in framework.scoped:
                own = self._text(call, 0)
                found |= {_joined(p, own) for p in parents}
            elif call.method in framework.scoped_same:
                found |= parents
        for argument in self.passed.get(ref, ()):
            found |= self.prefixes(argument, seen)
        return found or {PLACEHOLDER}

    def _made(self, call, seen):
        """The prefixes of the router a router call returns."""
        framework = self._framework(call)
        if framework is None:
            return {PLACEHOLDER}
        parents = self._parents(call, seen)
        if call.method in framework.groups:
            own = self._text(call, 0)
            return {_joined(p, own) for p in parents}
        if (
            call.method in framework.same
            or call.method in framework.verbs
            or call.method in framework.any
            or call.method in framework.explicit
        ):
            # A route call returning a router returns the one it was made
            # on (gin's r.GET(...).POST(...), Fiber's chained verbs).
            return parents
        return {PLACEHOLDER}

    @staticmethod
    def _text(call, index):
        texts = call.texts[index] if index < len(call.texts) else None
        return texts[0] if texts and len(texts) == 1 else PLACEHOLDER

    def routes(self):
        """GoRoute of each route call; with the reason for each router call
        that registers a route the reader cannot read: (routes, unread)."""
        found, unread = [], []
        for call in self.calls.values():
            framework = self._framework(call)
            if framework is None:
                continue
            route = self._route(call, framework)
            if route is None:
                continue
            if isinstance(route, str):
                unread.append((call.file, call.line, route))
                continue
            found.append(route)
        return found, unread

    def _route(self, call, framework):
        count = len(call.refs)
        register = (
            framework is _FIBER_V3
            and call.receiver[1] == "Register"
            and call.method in framework.verbs
        )
        if register:
            methods, path_at, first = [framework.verbs[call.method]], None, 0
        elif call.method in framework.verbs:
            methods, path_at, first = [framework.verbs[call.method]], 0, 1
        elif call.method in framework.any:
            path_at, _ = framework.any[call.method]
            methods, first = [ANY_METHOD], 1
        elif call.method in framework.explicit:
            path_at, first = framework.explicit[call.method]
            value = call.values[0] if call.values else None
            if value is not None and value.strings is not None:
                methods = list(value.strings)
            else:
                methods = [self._text(call, 0)]
            if not methods or any(m not in HTTP_METHODS for m in methods):
                return "method_not_literal"
        else:
            return None
        handlers = list(range(first, count))
        if not handlers:
            return None
        handler_at = handlers[framework.handler]
        if register:
            paths = sorted(self._parents(call, frozenset()))
        else:
            texts = call.texts[path_at] if path_at < len(call.texts) else None
            if not texts:
                return "path_not_literal"
            parents = self._parents(call, frozenset())
            paths = sorted({_joined(p, t) for p in parents for t in texts})
        return GoRoute(
            call.file,
            call.line,
            call.column,
            call.method,
            framework.name,
            tuple(methods),
            tuple(paths),
            self._handler(call, handler_at),
            self._literal(call, handler_at),
        )

    @staticmethod
    def _handler(call, index):
        return call.functions[index] if index < len(call.functions) else None

    @staticmethod
    def _literal(call, index):
        parameters = call.literal_parameters
        return parameters[index] if index < len(parameters) else None
