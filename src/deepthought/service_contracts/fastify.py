"""Fastify plugins and the route prefixes they are registered under.

A Fastify route is declared on an instance: ``instance.get(path, ...)``
(and ``head``, ``post``, ``put``, ``delete``, ``options``, ``patch``,
``all``) or ``instance.route({method, url, handler})``. The instance is
known two ways: the scip-typescript index resolves the called method to
Fastify's ``FastifyInstance`` (TypeScript, or JavaScript annotated with
its types), or the receiver is the first parameter of a plugin function,
which Fastify always calls with the instance. A plugin function is one
the repository registers: an argument of ``instance.register(plugin,
options)``, or the export of a module ``@fastify/autoload`` loads.

The prefixes follow Fastify's encapsulation: a plugin registered with
``{prefix}`` has its routes under the prefixes of the plugin that
registers it plus that prefix, except a plugin wrapped with
``fastify-plugin``, which runs in its parent's context and takes none.
``@fastify/autoload`` (``register(autoload, {dir, options,
dirNameRoutePrefix, routeParams, ignorePattern})``) loads, as its
``find-plugins`` does, each directory's ``index`` file, or every script
of a directory without one (not ``autohooks`` files), with the
directory's path below ``dir`` as the prefix unless ``dirNameRoutePrefix``
is false, a module's literal ``autoPrefix`` export in its place, and
``options.prefix`` before it. A ``dir`` the code does not fix
(``path.join(__dirname, ...)`` and ``import.meta.dirname`` with literal
parts are read) loads nothing. An options value that is not a literal
prefix leaves the base unknown.
"""

import posixpath
import re

from .folding import text
from .http import PLACEHOLDER

VERBS = {
    "get": "GET",
    "head": "HEAD",
    "post": "POST",
    "put": "PUT",
    "delete": "DELETE",
    "options": "OPTIONS",
    "patch": "PATCH",
    "all": "*",
    "route": None,
}
_INSTANCE = re.compile(r"^scip-typescript npm fastify \S+ \S*FastifyInstance#(\w+)\.$")
_AUTOLOAD = re.compile(r"^scip-typescript npm @fastify/autoload \S+ ")
_SCRIPT = re.compile(
    r"(?:(?:^.?|\.[^d]|[^.]d|[^.][^d])\.ts|\.js|\.cjs|\.mjs|\.cts|\.mts)$", re.I
)
_INDEX = re.compile(r"^index(?:\.ts|\.js|\.cjs|\.mjs|\.cts|\.mts)$", re.I)
_AUTOHOOKS = re.compile(
    r"^[_.]?auto_?hooks(?:\.ts|\.js|\.cjs|\.mjs|\.cts|\.mts)$", re.I
)
_FUNCTIONS = (
    "function_declaration",
    "function_expression",
    "arrow_function",
    "function",
)
_ROUNDS = 12


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def _key(file, node):
    return (file, node.start_byte, node.end_byte)


def _first_parameter(function, content):
    parameters = function.child_by_field_name("parameters")
    if parameters is None:
        parameter = function.child_by_field_name("parameter")
        return text(parameter, content) if parameter is not None else None
    for child in parameters.named_children:
        if child.type == "identifier":
            return text(child, content)
        if child.type in ("required_parameter", "optional_parameter"):
            pattern = child.child_by_field_name("pattern")
            if pattern is not None and pattern.type == "identifier":
                return text(pattern, content)
        return None
    return None


def _string(node, content):
    if node is None or node.type not in ("string", "template_string"):
        return None
    if node.type == "template_string" and any(
        c.type == "template_substitution" for c in node.named_children
    ):
        return None
    return text(node, content)[1:-1]


def _object(node, content):
    found = {}
    if node is None or node.type != "object":
        return found
    for pair in node.named_children:
        if pair.type == "pair":
            key = pair.child_by_field_name("key")
            found[text(key, content).strip("'\"")] = pair.child_by_field_name("value")
        elif pair.type == "shorthand_property_identifier":
            found[text(pair, content)] = pair
    return found


def _arguments(call):
    arguments = call.child_by_field_name("arguments")
    return [
        c
        for c in (arguments.named_children if arguments else ())
        if c.type != "comment"
    ]


class _Module:
    """One JavaScript/TypeScript source: its module-level bindings and
    imports."""

    def __init__(self, file, content, tree):
        self.file, self.content, self.tree = file, content, tree
        self.bindings = {}
        self.imports = {}
        for statement in tree.root_node.named_children:
            node = statement
            if node.type == "export_statement":
                node = node.child_by_field_name("declaration") or node
            if node.type in ("function_declaration", "generator_function_declaration"):
                name = node.child_by_field_name("name")
                if name is not None:
                    self.bindings[text(name, content)] = node
            elif node.type in ("lexical_declaration", "variable_declaration"):
                for declarator in node.named_children:
                    if declarator.type != "variable_declarator":
                        continue
                    name = declarator.child_by_field_name("name")
                    value = declarator.child_by_field_name("value")
                    if name is None or value is None:
                        continue
                    if name.type == "identifier":
                        self.bindings[text(name, content)] = value
            elif statement.type == "import_statement":
                source = _string(statement.child_by_field_name("source"), content)
                clause = next(
                    (c for c in statement.named_children if c.type == "import_clause"),
                    None,
                )
                for child in clause.named_children if clause else ():
                    if child.type == "identifier" and source is not None:
                        self.imports[text(child, content)] = source

    def module_of(self, name):
        """The module specifier a name is the default import or the
        ``require`` of."""
        if name in self.imports:
            return self.imports[name]
        value = self.bindings.get(name)
        return self.required(value)

    def required(self, node):
        if node is None or node.type != "call_expression":
            return None
        function = node.child_by_field_name("function")
        if function is None or text(function, self.content) != "require":
            return None
        arguments = _arguments(node)
        return _string(arguments[0], self.content) if arguments else None

    def export(self):
        """The node a module exports as its plugin: ``module.exports =``
        or ``export default``."""
        for statement in self.tree.root_node.named_children:
            if statement.type == "export_statement" and any(
                c.type == "default" for c in statement.children
            ):
                return (
                    statement.child_by_field_name("declaration")
                    or statement.child_by_field_name("value")
                    or (
                        statement.named_children[-1]
                        if statement.named_children
                        else None
                    )
                )
            if statement.type == "expression_statement" and statement.named_children:
                assignment = statement.named_children[0]
                if assignment.type != "assignment_expression":
                    continue
                left = assignment.child_by_field_name("left")
                if left is not None and text(left, self.content) == "module.exports":
                    return assignment.child_by_field_name("right")
        return None

    def exported_value(self, name):
        """A literal string the module exports as ``name``
        (``export const autoPrefix = "/x"``, ``module.exports.autoPrefix``)."""
        for statement in self.tree.root_node.named_children:
            if statement.type == "export_statement":
                declaration = statement.child_by_field_name("declaration")
                for declarator in declaration.named_children if declaration else ():
                    if (
                        declarator.type == "variable_declarator"
                        and text(declarator.child_by_field_name("name"), self.content)
                        == name
                    ):
                        return _string(
                            declarator.child_by_field_name("value"), self.content
                        )
            if statement.type == "expression_statement" and statement.named_children:
                assignment = statement.named_children[0]
                if assignment.type != "assignment_expression":
                    continue
                left = text(assignment.child_by_field_name("left"), self.content)
                if left in (f"module.exports.{name}", f"exports.{name}"):
                    return _string(
                        assignment.child_by_field_name("right"), self.content
                    )
        return None


class FastifyPlugins:
    """The plugin functions of the repository's scripts and the prefixes
    each runs under; ``route_of(file, call, member)`` reads a route call."""

    def __init__(self, sources, references, resolve_module):
        self.references = references
        self.resolve_module = resolve_module
        self.modules = {}
        for coverage, content, tree in sources:
            if coverage.language in ("javascript", "typescript"):
                self.modules[coverage.file] = _Module(coverage.file, content, tree)
        self.files = sorted(self.modules)
        # {(file, start, end) of a plugin function: set of prefixes}
        self.plugins = {}
        self.unread = []
        candidates = [
            m
            for m in self.modules.values()
            if b"register" in m.content
            and (b"fastify" in m.content or b"autoload" in m.content)
        ]
        if not candidates:
            return
        calls = []
        for module in candidates:
            for node in _walk(module.tree.root_node):
                if node.type != "call_expression":
                    continue
                function = node.child_by_field_name("function")
                if (
                    function is not None
                    and function.type == "member_expression"
                    and text(function.child_by_field_name("property"), module.content)
                    == "register"
                ):
                    calls.append((module, node, function))
        for _ in range(_ROUNDS):
            before = {k: frozenset(v) for k, v in self.plugins.items()}
            for module, call, function in calls:
                self._register(module, call, function)
            if before == {k: frozenset(v) for k, v in self.plugins.items()}:
                break

    # -- instances --------------------------------------------------------

    def _symbols(self, file, node):
        if node is None or not self.references.covers(file):
            return ()
        return self.references.symbols(
            file, node.start_point.row + 1, node.start_point.column
        )

    def _typed(self, file, property_node, method):
        return any(
            (m := _INSTANCE.match(s)) is not None and m.group(1) == method
            for s in self._symbols(file, property_node)
        )

    def _enclosing(self, module, node, receiver):
        """(plugin key, is a known plugin) of the nearest enclosing function
        whose first parameter is ``receiver``, else None."""
        current = node.parent
        while current is not None:
            if (
                current.type in _FUNCTIONS
                and _first_parameter(current, module.content) == receiver
            ):
                key = _key(module.file, current)
                return key, key in self.plugins
            current = current.parent
        return None

    def _instance_prefixes(self, module, node, receiver_node):
        """The prefixes of the instance a call is made on, or None when the
        receiver is not known to be a Fastify instance."""
        base = receiver_node
        while base is not None and base.type == "call_expression":
            inner = base.child_by_field_name("function")
            if inner is None or inner.type != "member_expression":
                return None
            base = inner.child_by_field_name("object")
        if base is None or base.type != "identifier":
            return None
        found = self._enclosing(module, node, text(base, module.content))
        if found is not None and found[1]:
            return set(self.plugins[found[0]])
        if found is not None:
            return {PLACEHOLDER}
        value = self._local_value(module, node, text(base, module.content))
        if (
            value is not None
            and value.type == "await_expression"
            and value.named_children
        ):
            value = value.named_children[0]
        if value is not None and self._is_root(module, value):
            return {""}
        return None

    def _local_value(self, module, node, name):
        """The value a name is declared with in the scopes around ``node``."""
        current = node.parent
        while current is not None:
            if current.type in ("statement_block", "program"):
                for statement in current.named_children:
                    if statement.type not in (
                        "lexical_declaration",
                        "variable_declaration",
                    ):
                        continue
                    for declarator in statement.named_children:
                        if (
                            declarator.type == "variable_declarator"
                            and text(
                                declarator.child_by_field_name("name"), module.content
                            )
                            == name
                        ):
                            return declarator.child_by_field_name("value")
            current = current.parent
        return None

    def _is_root(self, module, value):
        """Whether ``value`` makes a Fastify application:
        ``fastify(...)`` imported or required from ``fastify``."""
        if value.type != "call_expression":
            return False
        callee = value.child_by_field_name("function")
        if callee is None:
            return False
        if callee.type == "call_expression":
            return module.required(callee) == "fastify"
        if callee.type == "identifier":
            return module.module_of(text(callee, module.content)) == "fastify"
        return False

    # -- registration -----------------------------------------------------

    def _register(self, module, call, function):
        property_node = function.child_by_field_name("property")
        receiver = function.child_by_field_name("object")
        prefixes = self._instance_prefixes(module, call, receiver)
        if prefixes is None:
            if not self._typed(module.file, property_node, "register"):
                return
            found = self._enclosing_any(module, call)
            prefixes = set(self.plugins[found]) if found is not None else {""}
        arguments = _arguments(call)
        if not arguments:
            return
        options = arguments[1] if len(arguments) > 1 else None
        if self._is_autoload(module, arguments[0]):
            own = self._register_prefix(module, options, fp=False)
            self._autoload(module, call, options, {p + own for p in prefixes})
            return
        plugin = self._plugin(module, arguments[0], 0)
        if plugin is None:
            return
        key, fp = plugin
        own = self._register_prefix(module, options, fp)
        self._add(key, {p + own for p in prefixes})

    def _enclosing_any(self, module, node):
        current = node.parent
        while current is not None:
            if (
                current.type in _FUNCTIONS
                and _key(module.file, current) in self.plugins
            ):
                return _key(module.file, current)
            current = current.parent
        return None

    def _add(self, key, prefixes):
        self.plugins.setdefault(key, set()).update(prefixes)

    def _register_prefix(self, module, options, fp):
        if fp or options is None:
            return ""
        if options.type != "object":
            return PLACEHOLDER
        value = _object(options, module.content).get("prefix")
        if value is None:
            return ""
        literal = _string(value, module.content)
        return PLACEHOLDER if literal is None else literal.rstrip("/")

    def _is_autoload(self, module, node):
        if node.type != "identifier":
            return False
        if any(_AUTOLOAD.match(s) for s in self._symbols(module.file, node)):
            return True
        return module.module_of(text(node, module.content)) == "@fastify/autoload"

    def _plugin(self, module, node, depth):
        """((file, start, end) of the plugin function a register argument
        names, wrapped with fastify-plugin), or None."""
        if node is None or depth > 6:
            return None
        if node.type in _FUNCTIONS:
            return _key(module.file, node), False
        if node.type == "parenthesized_expression" and node.named_children:
            return self._plugin(module, node.named_children[0], depth + 1)
        if node.type == "await_expression" and node.named_children:
            return self._plugin(module, node.named_children[0], depth + 1)
        if node.type == "call_expression":
            callee = node.child_by_field_name("function")
            name = text(callee, module.content) if callee is not None else ""
            specifier = module.required(node)
            if specifier is not None:
                return self._module_plugin(module.file, specifier, depth)
            if (
                callee is not None
                and callee.type == "identifier"
                and (
                    module.module_of(name) == "fastify-plugin"
                    or name in ("fp", "fastifyPlugin")
                )
            ):
                arguments = _arguments(node)
                inner = (
                    self._plugin(module, arguments[0], depth + 1) if arguments else None
                )
                return None if inner is None else (inner[0], True)
            return None
        if node.type == "member_expression":
            # require("./x").default
            obj = node.child_by_field_name("object")
            if text(node.child_by_field_name("property"), module.content) == "default":
                return self._plugin(module, obj, depth + 1)
            return None
        if node.type == "identifier":
            name = text(node, module.content)
            specifier = module.module_of(name)
            if specifier is not None:
                return self._module_plugin(module.file, specifier, depth)
            value = module.bindings.get(name)
            if value is not None and value is not node:
                return self._plugin(module, value, depth + 1)
        return None

    def _module_plugin(self, file, specifier, depth):
        target = self.resolve_module(file, specifier, self.files)
        if target is None or target not in self.modules:
            return None
        module = self.modules[target]
        return self._plugin(module, module.export(), depth + 1)

    # -- autoload ---------------------------------------------------------

    def _directory(self, module, node):
        """The repository directory a ``dir`` option names, or None."""
        if node is None or node.type != "call_expression":
            return None
        callee = text(node.child_by_field_name("function"), module.content)
        if callee not in ("path.join", "join", "path.resolve", "resolve"):
            return None
        arguments = _arguments(node)
        if not arguments:
            return None
        first = text(arguments[0], module.content)
        if first not in ("__dirname", "import.meta.dirname"):
            return None
        parts = [module.file.rpartition("/")[0]]
        for argument in arguments[1:]:
            literal = _string(argument, module.content)
            if literal is None:
                return None
            parts.append(literal)
        joined = posixpath.normpath(posixpath.join(*[p or "." for p in parts]))
        return "" if joined == "." else joined

    def _autoload(self, module, call, options, prefixes):
        line = call.start_point.row + 1
        given = _object(options, module.content)
        directory = self._directory(module, given.get("dir"))
        if directory is None:
            self.unread.append((module.file, line, "autoload_dir_not_literal"))
            return
        dir_names = given.get("dirNameRoutePrefix")
        use_dirs = dir_names is None or dir_names.type != "false"
        route_params = given.get("routeParams")
        params = route_params is not None and route_params.type == "true"
        ignore = given.get("ignorePattern")
        ignore_pattern = None
        if ignore is not None:
            if ignore.type == "regex":
                pattern = ignore.child_by_field_name("pattern")
                ignore_pattern = re.compile(text(pattern, module.content))
            else:
                literal = _string(ignore, module.content)
                if literal is None:
                    self.unread.append(
                        (module.file, line, "autoload_ignore_not_literal")
                    )
                    return
                ignore_pattern = re.compile(literal)
        inner = _object(given.get("options"), module.content).get("prefix")
        option_prefix = ""
        if inner is not None:
            literal = _string(inner, module.content)
            option_prefix = PLACEHOLDER if literal is None else literal.rstrip("/")
        root = directory + "/" if directory else ""
        below = [f[len(root) :] for f in self.files if f.startswith(root)]
        for relative, prefix in self._tree(below, use_dirs, ignore_pattern):
            file = root + relative
            module_found = self.modules.get(file)
            plugin = (
                None
                if module_found is None
                else self._plugin(module_found, module_found.export(), 0)
            )
            if plugin is None:
                continue
            key, fp = plugin
            if params:
                prefix = re.sub(r"/_", "/:", prefix)
            auto = module_found.exported_value("autoPrefix")
            override = module_found.exported_value("prefixOverride")
            if override is not None:
                own = override
            elif fp:
                own = ""
            else:
                own = option_prefix + re.sub(
                    r"/+", "/", auto if auto is not None else prefix
                )
            self._add(key, {p + own.rstrip("/") for p in prefixes})

    def _tree(self, files, use_dirs, ignore_pattern):
        """(file below the autoload directory, directory prefix) of each
        module autoload loads."""
        by_directory = {}
        for relative in files:
            directory, _, name = relative.rpartition("/")
            by_directory.setdefault(directory, []).append(name)
        directories = set()
        for directory in by_directory:
            parts = directory.split("/") if directory else []
            for i in range(len(parts) + 1):
                directories.add("/".join(parts[:i]))
        loaded = []
        for directory in sorted(directories):
            if (
                ignore_pattern is not None
                and directory
                and ignore_pattern.search(directory)
            ):
                continue
            prefix = ""
            if use_dirs and directory:
                prefix = "/" + directory
            names = by_directory.get(directory, [])
            index = [n for n in names if _INDEX.match(n)]
            chosen = index[:1] or [
                n for n in names if _SCRIPT.search(n) and not _AUTOHOOKS.match(n)
            ]
            for name in sorted(chosen):
                relative = f"{directory}/{name}" if directory else name
                if ignore_pattern is not None and ignore_pattern.search(relative):
                    continue
                loaded.append((relative, prefix))
        return loaded

    # -- routes -----------------------------------------------------------

    def route_of(self, file, call, member):
        """(verb, prefixes) of a Fastify route call, or None."""
        module = self.modules.get(file)
        if module is None:
            return None
        property_node = member.child_by_field_name("property")
        verb = text(property_node, module.content)
        if verb not in VERBS:
            return None
        prefixes = self._instance_prefixes(
            module, call, member.child_by_field_name("object")
        )
        typed = self._typed(file, property_node, verb)
        if prefixes is None:
            if not typed:
                return None
            found = self._enclosing_any(module, call)
            prefixes = set(self.plugins[found]) if found is not None else {""}
        elif prefixes == {PLACEHOLDER} and not typed:
            return None
        return verb, sorted(prefixes)
