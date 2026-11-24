"""Spring MVC routes, Feign clients, RestTemplate requests and calls of
openapi-generator Java clients in Java sources.

Only what the source fixes is read: annotation names that the file imports
from Spring, string literals, ``+`` concatenation, ``String.format`` with
``%s``/``%d``, and ``static final String`` constants declared in the same
file. Anything else becomes unknown text (a route with unknown text is not
read; a request keeps it as a placeholder).
"""

from pathlib import PurePosixPath
import re

from ..syntax import identifier
from .http import ANY_METHOD, HTTP_METHODS, PLACEHOLDER, join_paths
from .model import HttpRequest, HttpRoute, Unread
from .openapi import CLIENT_METHOD_SUFFIXES

_WEB = "org.springframework.web.bind.annotation"
_FEIGN = (
    "org.springframework.cloud.openfeign",
    "org.springframework.cloud.netflix.feign",
)
_MAPPINGS = {
    "RequestMapping": None,
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}
_CONTROLLERS = {"RestController", "Controller"}
_REST_TYPES = {"RestTemplate", "RestOperations", "OAuth2RestTemplate"}
_WEB_CLIENT_TYPES = {"WebClient", "WebClient.Builder"}
_WEB_CLIENT_VERBS = {v.lower(): v for v in HTTP_METHODS}
# RestTemplate methods: the HTTP method each fixes; None reads it from the
# argument after the URL (exchange and execute).
_REST_METHODS = {
    "getForObject": "GET",
    "getForEntity": "GET",
    "headForHeaders": "HEAD",
    "postForObject": "POST",
    "postForEntity": "POST",
    "postForLocation": "POST",
    "put": "PUT",
    "patchForObject": "PATCH",
    "delete": "DELETE",
    "optionsForAllow": "OPTIONS",
    "exchange": None,
    "execute": None,
}


def _text(node, content):
    return content[node.start_byte : node.end_byte].decode("utf-8")


def _dotted(node, content):
    if node.type in ("identifier", "type_identifier"):
        return _text(node, content)
    if node.type == "scoped_identifier":
        scope = _dotted(node.child_by_field_name("scope"), content)
        return f"{scope}.{_text(node.child_by_field_name('name'), content)}"
    return None


class _Imports:
    """Which simple names a file imports from which packages."""

    def __init__(self, root, content):
        self.names, self.wildcards = {}, set()
        for node in root.named_children:
            if node.type != "import_declaration":
                continue
            path = next(
                (
                    _dotted(c, content)
                    for c in node.named_children
                    if c.type in ("scoped_identifier", "identifier")
                ),
                None,
            )
            if path is None or _text(node, content).split()[1] == "static":
                continue
            if any(c.type == "asterisk" for c in node.named_children):
                self.wildcards.add(path)
            else:
                package, _, name = path.rpartition(".")
                self.names.setdefault(name, set()).add(package)

    def spring_name(self, written, packages):
        """The simple annotation name if it is imported from ``packages``."""
        package, _, name = written.rpartition(".")
        if package:
            return name if package in packages else None
        if self.names.get(name, set()) & set(packages):
            return name
        if name not in self.names and self.wildcards & set(packages):
            return name
        return None

    def from_spring(self, name):
        packages = self.names.get(name)
        if packages:
            return any(p.startswith("org.springframework.") for p in packages)
        return any(p.startswith("org.springframework.") for p in self.wildcards)


def _annotations(node, content):
    """(written name, annotation node) of a declaration's modifiers."""
    modifiers = next((c for c in node.named_children if c.type == "modifiers"), None)
    if modifiers is None:
        return []
    found = []
    for child in modifiers.named_children:
        if child.type in ("annotation", "marker_annotation"):
            found.append((_dotted(child.child_by_field_name("name"), content), child))
    return found


def _arguments(annotation):
    """{key: value node}; a bare value is ``value``."""
    arguments = annotation.child_by_field_name("arguments")
    if arguments is None:
        return {}
    values = {}
    for child in arguments.named_children:
        if child.type == "element_value_pair":
            key = child.child_by_field_name("key")
            values[key.text.decode("utf-8")] = child.child_by_field_name("value")
        elif child.type not in ("line_comment", "block_comment"):
            values["value"] = child
    return values


def _unescape(text):
    return re.sub(
        r"\\(.)",
        lambda m: {"n": "\n", "t": "\t", "r": "\r"}.get(m.group(1), m.group(1)),
        text,
    )


def _text_block(raw):
    """The value of a Java text block (JLS 3.10.6): incidental indentation
    and trailing spaces stripped, then escapes interpreted."""
    body = raw[3:-3].replace("\r\n", "\n").replace("\r", "\n")
    newline = body.find("\n")
    if newline < 0:
        return PLACEHOLDER
    lines = body[newline + 1 :].split("\n")
    # The closing delimiter's line counts even when blank.
    significant = [line for line in lines[:-1] if line.strip()] + [lines[-1]]
    indent = min(len(line) - len(line.lstrip(" \t")) for line in significant)
    text = "\n".join(
        line[indent:].rstrip(" \t") if line.strip() else "" for line in lines
    )
    return re.sub(
        r"\\(\n|.)",
        lambda m: {"\n": "", "s": " ", "n": "\n", "t": "\t", "r": "\r"}.get(
            m.group(1), m.group(1)
        ),
        text,
    )


_STRING_TYPES = ("String", "java.lang.String")
_SCOPES = ("method_declaration", "constructor_declaration", "lambda_expression")
# Nested helper calls folded at most this deep.
_MAX_INLINE = 4


def _assigned_names(node, content):
    """Names assigned anywhere below node (``x = ``, ``this.x = ``, ``x += ``)."""
    names = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "assignment_expression":
            left = current.child_by_field_name("left")
            if left.type == "identifier":
                names.add(_text(left, content))
            elif left.type == "field_access":
                names.add(_text(left.child_by_field_name("field"), content))
        stack.extend(current.named_children)
    return names


_MAX_ALTERNATIVES = 8


def _branches_exclusive(nodes):
    """Whether each pair of nodes lies in different branches of one ``if``.

    Returns the ``if`` statements that separate them, or None.
    """
    separating = []
    for index, first in enumerate(nodes):
        ancestors = []
        current = first.parent
        while current is not None:
            ancestors.append(current)
            current = current.parent
        for second in nodes[index + 1 :]:
            current = second.parent
            while current is not None and all(current.id != a.id for a in ancestors):
                current = current.parent
            if current is None or current.type != "if_statement":
                return None
            branch = {
                current.child_by_field_name("consequence").id: "then",
                getattr(current.child_by_field_name("alternative"), "id", None): "else",
            }

            # Which branch holds each node: walk up to a direct branch child.
            def side(node):
                while node is not None and node.parent is not None:
                    if node.parent.id == current.id:
                        return branch.get(node.id)
                    node = node.parent
                return None

            first_side, second_side = side(first), side(second)
            if None in (first_side, second_side) or first_side == second_side:
                return None
            separating.append(current)
    return separating


def _covers(statement, assignment_ids):
    """Whether every path through ``statement`` runs one of the assignments."""
    if statement is None:
        return False
    if statement.type == "expression_statement":
        return any(c.id in assignment_ids for c in statement.named_children)
    if statement.type == "block":
        return any(_covers(c, assignment_ids) for c in statement.named_children)
    if statement.type == "if_statement":
        return _covers(
            statement.child_by_field_name("consequence"), assignment_ids
        ) and _covers(statement.child_by_field_name("alternative"), assignment_ids)
    return False


class _Values:
    """Folds string expressions; text they do not fix becomes PLACEHOLDER.

    Folded: string literals, ``+``, ``String.format`` with ``%s``/``%d``, and
    names whose values the file fixes: String fields of the file with an
    initializer, no ``@Value`` injection and no assignment anywhere in the
    file; String locals declared once with an initializer and never assigned
    in their method, or declared without a value (or ``null``) and assigned
    with ``=`` only in different branches of ``if`` statements that end
    before the use (each branch gives one alternative); and calls to a method
    of the file, unique by name and argument count, whose body is a single
    ``return`` (arguments substituted for parameters). At most
    ``_MAX_ALTERNATIVES`` alternatives are kept; more give unknown text.
    """

    def __init__(self, root, content, constants=None, imports=None):
        self.content = content
        # Constants of other classes: {qualified class: {field: text}}.
        self._constants = constants or {}
        self._imports = imports
        self._package = _package(root, content)
        self._classes = {
            _text(n.child_by_field_name("name"), content)
            for n in _descendants(root)
            if n.type
            in (
                "class_declaration",
                "interface_declaration",
                "enum_declaration",
                "record_declaration",
            )
        }
        assigned = _assigned_names(root, content)
        declared, methods = {}, {}
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type == "field_declaration":
                modifiers = next(
                    (c for c in node.named_children if c.type == "modifiers"), None
                )
                injected = modifiers is not None and any(
                    c.type in ("annotation", "marker_annotation")
                    for c in modifiers.named_children
                )
                type_node = node.child_by_field_name("type")
                for declarator in node.children_by_field_name("declarator"):
                    name = _text(declarator.child_by_field_name("name"), content)
                    value = declarator.child_by_field_name("value")
                    fixed = (
                        not injected
                        and value is not None
                        and type_node is not None
                        and _text(type_node, content) in _STRING_TYPES
                        and name not in assigned
                    )
                    declared.setdefault(name, []).append(value if fixed else None)
            elif node.type == "method_declaration":
                name = _text(node.child_by_field_name("name"), content)
                parameters = [
                    _text(p.child_by_field_name("name"), content)
                    for p in node.child_by_field_name("parameters").named_children
                    if p.type == "formal_parameter"
                ]
                methods.setdefault((name, len(parameters)), []).append(
                    (node, parameters)
                )
            stack.extend(node.named_children)
        # A name declared twice in the file is not folded.
        self._fields = {n: v[0] for n, v in declared.items() if len(v) == 1 and v[0]}
        self._methods = {k: v[0] for k, v in methods.items() if len(v) == 1}
        self._locals = {}
        self._active = set()

    def _scope_locals(self, scope):
        """name -> (declaration, initializer, assignments), or None if unusable."""
        if scope.id not in self._locals:
            content = self.content
            found, assignments = {}, {}
            stack = [scope]
            while stack:
                node = stack.pop()
                if node.type == "local_variable_declaration":
                    type_node = node.child_by_field_name("type")
                    string = _text(type_node, content) in _STRING_TYPES + ("var",)
                    for declarator in node.children_by_field_name("declarator"):
                        name = _text(declarator.child_by_field_name("name"), content)
                        value = declarator.child_by_field_name("value")
                        found.setdefault(name, []).append(
                            (declarator, value) if string else None
                        )
                elif node.type == "assignment_expression":
                    left = node.child_by_field_name("left")
                    if left.type == "identifier":
                        assignments.setdefault(_text(left, content), []).append(node)
                elif node.type == "formal_parameter":
                    name = node.child_by_field_name("name")
                    found.setdefault(_text(name, content), []).append(None)
                if node is not scope and node.type in _SCOPES:
                    continue  # nested lambdas have their own locals
                stack.extend(node.named_children)
            self._locals[scope.id] = {
                name: (
                    *items[0],
                    tuple(
                        sorted(assignments.get(name, ()), key=lambda a: a.start_byte)
                    ),
                )
                if len(items) == 1 and items[0] is not None
                else None
                for name, items in found.items()
            }
        return self._locals[scope.id]

    def _local(self, node, entry, depth):
        """Values a local may hold at ``node``.

        Assignments must use ``=``, end before the use, and either be one
        statement in the declaration's own block, or lie in different
        branches of ``if`` statements that end before the use. If those
        branches cover every path, the initializer is overwritten; otherwise
        it is one more value (a ``null`` initializer gives none).
        """
        declarator, initializer, assignments = entry
        if declarator.start_byte >= node.start_byte:
            return [PLACEHOLDER]
        if not assignments:
            if initializer is None:
                return [PLACEHOLDER]
            return self._fold(("local", initializer.id), initializer, None, depth)
        if any(
            _text(a.child_by_field_name("operator"), self.content) != "="
            or a.end_byte > node.start_byte
            for a in assignments
        ):
            return [PLACEHOLDER]
        declaration_block = declarator.parent.parent
        first = assignments[0]
        if (
            len(assignments) == 1
            and first.parent.type == "expression_statement"
            and first.parent.parent.id == declaration_block.id
        ):
            sources = [first]  # a straight-line assignment replaces the value
        else:
            if len(assignments) == 1:
                top = first.parent
                while top is not None and top.type != "if_statement":
                    if top.id == declaration_block.id:
                        return [PLACEHOLDER]
                    top = top.parent
                separating = [] if top is None else [top]
            else:
                separating = _branches_exclusive(list(assignments))
            if not separating or any(s.end_byte > node.start_byte for s in separating):
                return [PLACEHOLDER]
            top = min(separating, key=lambda s: s.start_byte)
            if top.parent.id != declaration_block.id:
                return [PLACEHOLDER]
            ids = {a.id for a in assignments}
            sources = list(assignments)
            if not _covers(top, ids):
                if initializer is None or initializer.type == "null_literal":
                    return [PLACEHOLDER]
                sources.insert(0, initializer)
        values = []
        for source in sources:
            value = (
                source.child_by_field_name("right")
                if source.type == "assignment_expression"
                else source
            )
            values.extend(self._fold(("assigned", source.id), value, None, depth))
        return values

    def _name(self, node, env, depth):
        name = _text(node, self.content)
        if env is not None and name in env:
            return [env[name]]
        scope = node.parent
        while scope is not None and scope.type not in _SCOPES:
            scope = scope.parent
        if scope is not None:
            scoped = self._scope_locals(scope)
            if name in scoped:
                entry = scoped[name]
                if entry is None:
                    return [PLACEHOLDER]
                return self._local(node, entry, depth)
        value = self._fields.get(name)
        if value is None:
            return [PLACEHOLDER]
        return self._fold(("field", name), value, None, depth)

    def _fold(self, key, node, env, depth):
        if key in self._active:
            return [PLACEHOLDER]
        self._active.add(key)
        try:
            return self._alternatives(node, env, depth)
        finally:
            self._active.discard(key)

    def _call(self, node, env, depth):
        name = _text(node.child_by_field_name("name"), self.content)
        target = node.child_by_field_name("object")
        arguments = node.child_by_field_name("arguments").named_children
        if target is not None and _text(target, self.content) == "String":
            if name == "format" and arguments:
                return [
                    re.sub(r"%[sd]", PLACEHOLDER, template)
                    for template in self._alternatives(arguments[0], env, depth)
                ]
            return [PLACEHOLDER]
        if target is not None and target.type != "this":
            return [PLACEHOLDER]
        found = self._methods.get((name, len(arguments)))
        if found is None or depth >= _MAX_INLINE:
            return [PLACEHOLDER]
        method, parameters = found
        body = method.child_by_field_name("body")
        statements = (
            []
            if body is None
            else [
                c
                for c in body.named_children
                if c.type not in ("line_comment", "block_comment")
            ]
        )
        if len(statements) != 1 or statements[0].type != "return_statement":
            return [PLACEHOLDER]
        returned = statements[0].named_children
        if not returned:
            return [PLACEHOLDER]
        # Each parameter takes one value; several alternatives are unknown.
        bound = {p: self.evaluate(a, env, depth) for p, a in zip(parameters, arguments)}
        return self._fold(("method", method.id), returned[0], bound, depth + 1)

    def _alternatives(self, node, env, depth):
        if node is None:
            return [PLACEHOLDER]
        kind = node.type
        if kind == "string_literal":
            raw = _text(node, self.content)
            if raw.startswith('"""'):
                return [_text_block(raw)]
            parts = []
            for child in node.named_children:
                text = _text(child, self.content)
                if child.type == "escape_sequence":
                    text = _unescape(text)
                elif child.type == "multiline_string_fragment":
                    return [PLACEHOLDER]  # text blocks are not read
                parts.append(text)
            return ["".join(parts)]
        if kind == "parenthesized_expression":
            return self._alternatives(node.named_children[0], env, depth)
        if kind == "binary_expression":
            operator = node.child_by_field_name("operator")
            if operator is not None and operator.type == "+":
                lefts = self._alternatives(node.child_by_field_name("left"), env, depth)
                rights = self._alternatives(
                    node.child_by_field_name("right"), env, depth
                )
                values = [left + right for left in lefts for right in rights]
                return values if len(values) <= _MAX_ALTERNATIVES else [PLACEHOLDER]
            return [PLACEHOLDER]
        if kind == "identifier":
            return self._name(node, env, depth)
        if kind == "field_access":
            field = _text(node.child_by_field_name("field"), self.content)
            owner = node.child_by_field_name("object")
            written = _text(owner, self.content)
            if written == "this" or written in self._classes:
                # this.x or Class.CONSTANT of a class in this file.
                value = self._fields.get(field)
                if value is None:
                    return [PLACEHOLDER]
                return self._fold(("field", field), value, None, depth)
            qualified = self._qualified_class(written)
            value = self._constants.get(qualified, {}).get(field)
            return [PLACEHOLDER if value is None else value]
        if kind == "method_invocation":
            return self._call(node, env, depth)
        return [PLACEHOLDER]

    def _qualified_class(self, written):
        """The qualified name of a class this file names: written in full,
        imported by name, or (when not imported) in the file's package."""
        if "." in written:
            return written
        if self._imports is None:
            return None
        packages = self._imports.names.get(written, set())
        if len(packages) == 1:
            return f"{next(iter(packages))}.{written}"
        if packages or self._imports.wildcards:
            return None
        return f"{self._package}.{written}" if self._package else written

    def alternatives(self, node):
        """Each text the expression may have, in source order, without repeats."""
        values = list(dict.fromkeys(self._alternatives(node, None, 0)))
        return values if len(values) <= _MAX_ALTERNATIVES else [PLACEHOLDER]

    def evaluate(self, node, env=None, depth=0):
        """The expression's single text, PLACEHOLDER where it is not fixed."""
        values = list(dict.fromkeys(self._alternatives(node, env, depth)))
        return values[0] if len(values) == 1 else PLACEHOLDER

    def strings(self, node):
        """Each string of an annotation value (an array gives several)."""
        if node is None:
            return []
        if node.type == "element_value_array_initializer":
            return [self.evaluate(c) for c in node.named_children]
        return [self.evaluate(node)]


def _descendants(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.named_children)


def _package(root, content):
    for node in root.named_children:
        if node.type == "package_declaration":
            name = next(
                (
                    c
                    for c in node.named_children
                    if c.type in ("scoped_identifier", "identifier")
                ),
                None,
            )
            return _dotted(name, content) if name is not None else None
    return None


def java_constants(tree, content):
    """{qualified class: {field: text}} for the ``static final`` String fields
    of a file's top-level classes whose value the file fixes."""
    root = tree.root_node
    package = _package(root, content)
    values = _Values(root, content)
    found = {}
    for declaration in root.named_children:
        if declaration.type not in (
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
        ):
            continue
        name = _text(declaration.child_by_field_name("name"), content)
        qualified = f"{package}.{name}" if package else name
        body = declaration.child_by_field_name("body")
        for field in body.named_children if body is not None else ():
            if field.type not in ("field_declaration", "constant_declaration"):
                continue
            modifiers = next(
                (c for c in field.named_children if c.type == "modifiers"), None
            )
            words = set(_text(modifiers, content).split()) if modifiers else set()
            constant = (
                field.type == "constant_declaration"
                or {
                    "static",
                    "final",
                }
                <= words
            )
            type_node = field.child_by_field_name("type")
            if (
                not constant
                or type_node is None
                or _text(type_node, content) not in _STRING_TYPES
            ):
                continue
            for declarator in field.children_by_field_name("declarator"):
                value = values.evaluate(declarator.child_by_field_name("value"))
                if PLACEHOLDER not in value:
                    found.setdefault(qualified, {})[
                        _text(declarator.child_by_field_name("name"), content)
                    ] = value
    return found


def _methods(node, content, default):
    """HTTP methods named by a ``method =`` value; ``default`` if absent."""
    if node is None:
        return default
    items = (
        node.named_children
        if node.type == "element_value_array_initializer"
        else [node]
    )
    methods = []
    for item in items:
        name = _text(item, content).rpartition(".")[2]
        if name not in HTTP_METHODS:
            return None
        methods.append(name)
    return tuple(sorted(set(methods))) or default


def _mapping(written, annotation, imports, values, content):
    """(methods, paths) of a Spring mapping annotation, or None if not one.

    ``paths`` are evaluated strings; an empty list is the empty path.
    """
    name = imports.spring_name(written, (_WEB,))
    if name not in _MAPPINGS:
        return None
    arguments = _arguments(annotation)
    fixed = _MAPPINGS[name]
    if fixed is not None:
        methods = (fixed,)
    else:
        methods = _methods(arguments.get("method"), content, (ANY_METHOD,))
    path_node = arguments.get("path", arguments.get("value"))
    return methods, values.strings(path_node)


_BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts")
_CONFIG_STEMS = ("bootstrap", "application")
_CONFIG_SUFFIXES = ("yml", "yaml", "properties")
_CONTEXT_PATH_KEYS = ("server.servlet.context-path", "server.context-path")
# Spring's relaxed binding accepts the camelCase form too.
_SEARCH_LOCATIONS = (
    "spring.cloud.config.server.native.search-locations",
    "spring.cloud.config.server.native.searchLocations",
)


def _module_root(root_path, file):
    """The nearest directory above ``file`` with a Maven or Gradle build."""
    path = PurePosixPath(file).parent
    while True:
        if any((root_path / path / name).is_file() for name in _BUILD_FILES):
            return path
        if path == PurePosixPath("."):
            return None
        path = path.parent


_PROFILE_KEYS = ("spring.config.activate.on-profile", "spring.profiles")


def _documents(text, separator):
    """The documents of a multi-document config file."""
    document = []
    for raw in text.splitlines():
        if raw.strip() == separator:
            yield "\n".join(document)
            document = []
        else:
            document.append(raw)
    yield "\n".join(document)


def _yaml_values(text, key_path, default_profile=False):
    """Scalars at ``key_path`` in a YAML file, read by indentation only.

    Block mappings with scalar values are read; flow collections, anchors and
    multi-line scalars are not. Every document of the file is read, or with
    ``default_profile`` only those that activate on no profile.
    """
    if default_profile:
        return [
            v
            for d in _documents(text, "---")
            if not any(_yaml_values(d, k) for k in _PROFILE_KEYS)
            for v in _yaml_values(d, key_path)
        ]
    found, stack = [], []
    for raw in text.splitlines():
        if raw.strip() == "---":
            stack = []
            continue
        if raw.lstrip().startswith(("#", "- ")) or raw.strip() == "...":
            continue
        match = re.match(r"^( *)([^\s:#][^:#]*?)\s*:(?:\s+(.*?))?\s*$", raw)
        if not match:
            continue
        indent, key, value = len(match.group(1)), match.group(2), match.group(3)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key.strip("'\"")))
        if ".".join(k for _, k in stack) == key_path and value:
            found.append(re.sub(r"\s+#.*$", "", value).strip().strip("'\""))
    return found


def _properties_values(text, key, default_profile=False):
    if default_profile:
        return [
            v
            for d in _documents(text, "#---")
            if not any(_properties_values(d, k) for k in _PROFILE_KEYS)
            for v in _properties_values(d, key)
        ]
    found = []
    for raw in text.splitlines():
        match = re.match(r"^\s*([^#!=:\s]+)\s*[=:]\s*(.*?)\s*$", raw)
        if match and match.group(1) == key:
            found.append(match.group(2))
    return found


def _read_values(path, key, raw=False, default_profile=False):
    """Values of ``key`` in one config file; ``raw`` keeps ``${...}`` ones,
    ``default_profile`` reads only the documents of no profile."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    reader = _properties_values if path.suffix == ".properties" else _yaml_values
    return [
        v for v in reader(text, key, default_profile) if v and (raw or "${" not in v)
    ]


class SpringConfig:
    """Service names and context paths declared in Spring configuration.

    A Java file belongs to the build module of the nearest ``pom.xml`` or
    ``build.gradle``. Its configuration is
    ``src/main/resources/{bootstrap,application}.{yml,yaml,properties}``
    there, plus, for a service name ``n``, ``n.*`` and ``application.*`` in
    the ``classpath:/`` native search locations of any Spring Cloud Config
    server module in the repository. Profile files, environment variables
    and remote config repositories are not read. A key with several
    different values, or one written with ``${``, gives no value.
    """

    def __init__(self, root_path):
        self.root = root_path
        self._modules = {}
        self._shared = None

    def _module_files(self, module):
        resources = self.root / module / "src" / "main" / "resources"
        return [
            resources / f"{stem}.{suffix}"
            for stem in _CONFIG_STEMS
            for suffix in _CONFIG_SUFFIXES
        ]

    def _shared_directories(self):
        if self._shared is None:
            self._shared = []
            for path in sorted(self.root.rglob("src/main/resources/application.*")):
                if path.suffix.lstrip(".") not in _CONFIG_SUFFIXES:
                    continue
                for value in (
                    v for key in _SEARCH_LOCATIONS for v in _read_values(path, key)
                ):
                    for location in value.split(","):
                        location = location.strip()
                        if location.startswith("classpath:"):
                            relative = location[len("classpath:") :].strip("/")
                            directory = path.parent / relative
                            if directory.is_dir():
                                self._shared.append(directory)
        return self._shared

    @staticmethod
    def _single(values):
        values = set(values)
        return next(iter(values)) if len(values) == 1 else None

    def service(self, file):
        """(service name, context path) of the file's module; each may be None."""
        module = _module_root(self.root, file)
        if module is None:
            return None, None
        if module not in self._modules:
            files = self._module_files(module)
            name = self._single(
                v for f in files for v in _read_values(f, "spring.application.name")
            )
            if name is not None:
                files += [
                    directory / f"{stem}.{suffix}"
                    for directory in self._shared_directories()
                    for stem in (name, "application")
                    for suffix in _CONFIG_SUFFIXES
                ]
            context = self._single(
                v
                for f in files
                for key in _CONTEXT_PATH_KEYS
                for v in _read_values(f, key)
            )
            self._modules[module] = (name, context)
        return self._modules[module]

    def files(self, file):
        """The configuration files of the file's module (the shared
        config-server files included) that exist; [] outside a module."""
        module = _module_root(self.root, file)
        if module is None:
            return []
        name, _ = self.service(file)
        files = self._module_files(module)
        if name is not None:
            files += [
                directory / f"{stem}.{suffix}"
                for directory in self._shared_directories()
                for stem in (name, "application")
                for suffix in _CONFIG_SUFFIXES
            ]
        return [f for f in files if f.is_file()]

    def values(self, file, key, default_profile=False):
        """The distinct values of ``key`` in the file's module configuration,
        or None outside a module; ``default_profile`` reads only the
        documents that activate on no profile."""
        if _module_root(self.root, file) is None:
            return None
        return sorted(
            {
                v
                for f in self.files(file)
                for v in _read_values(f, key, raw=True, default_profile=default_profile)
            }
        )


def _receivers(root, content, imports, kinds):
    """Names declared in the file with one of the Spring types ``kinds`` only."""
    declared = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in (
            "field_declaration",
            "local_variable_declaration",
            "formal_parameter",
        ):
            type_node = node.child_by_field_name("type")
            type_name = _text(type_node, content) if type_node else ""
            names = (
                [node.child_by_field_name("name")]
                if node.type == "formal_parameter"
                else [
                    d.child_by_field_name("name")
                    for d in node.children_by_field_name("declarator")
                ]
            )
            for name in names:
                if name is not None:
                    declared.setdefault(_text(name, content), set()).add(type_name)
        stack.extend(node.named_children)
    return {
        name
        for name, types in declared.items()
        if len(types) == 1
        and (t := next(iter(types))) in kinds
        and imports.from_spring(t.partition(".")[0])
    }


def _implemented(declaration, content, values):
    """Qualified names of the interfaces a class declaration implements."""
    found = []
    interfaces = declaration.child_by_field_name("interfaces")
    for type_list in [] if interfaces is None else interfaces.named_children:
        for item in type_list.named_children:
            if item.type in ("type_identifier", "scoped_type_identifier"):
                qualified = values._qualified_class(_text(item, content))
                if qualified is not None:
                    found.append(qualified)
    return found


def _generated_routes(
    declaration,
    body,
    generated,
    values,
    prefixes,
    context_path,
    service,
    spans,
    span,
    make_id,
    routes,
    unread,
    file,
    content,
):
    """Routes a controller handles by overriding methods of interfaces
    generated from an OpenAPI spec."""
    operations = [
        operation
        for interface in _implemented(declaration, content, values)
        for operation in generated.get(interface, [])
    ]
    if not operations:
        return
    members = {}
    for member in body.named_children:
        if member.type == "method_declaration":
            name = _text(member.child_by_field_name("name"), content)
            members.setdefault(name, []).append(member)
    line = declaration.start_point.row + 1
    for operation in operations:
        found = members.get(operation.method_name, [])
        if len(found) != 1:
            # Not overridden: the interface's default method answers 501.
            unread.append(
                Unread(
                    file,
                    line,
                    "route",
                    "not_overridden" if not found else "overloaded",
                )
            )
            continue
        member = found[0]
        handler = spans.get(span(member))
        if handler is None:
            unread.append(Unread(file, line, "route", "no_handler"))
            continue
        for prefix in prefixes:
            if PLACEHOLDER in prefix:
                unread.append(Unread(file, line, "route", "path_not_literal"))
                continue
            full = join_paths(context_path or "", prefix, operation.path)
            routes.append(
                HttpRoute(
                    id=make_id("HttpRoute", f"{operation.http_method} {full}"),
                    file=file,
                    line=member.start_point.row + 1,
                    column=member.start_point.column,
                    handler_id=handler.id,
                    methods=(operation.http_method,),
                    path=full,
                    framework="openapi-spring",
                    service=service,
                    declared_in=f"{operation.spec_file}:{operation.spec_line}",
                )
            )


def _client_requests(
    root,
    content,
    values,
    clients,
    base_fixed,
    enclosing,
    make_id,
    requests,
    file,
):
    """Requests sent by calling methods of Java API classes openapi-generator
    makes from a spec: on a name declared once in the file with that class as
    its type, or on a ``new`` instance of it."""
    declared = {}
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type not in (
            "field_declaration",
            "local_variable_declaration",
            "formal_parameter",
        ):
            continue
        type_node = node.child_by_field_name("type")
        if type_node is None or type_node.type not in (
            "type_identifier",
            "scoped_type_identifier",
        ):
            qualified = None
        else:
            qualified = values._qualified_class(_text(type_node, content))
        names = (
            [node.child_by_field_name("name")]
            if node.type == "formal_parameter"
            else [
                d.child_by_field_name("name")
                for d in node.children_by_field_name("declarator")
            ]
        )
        for name in names:
            if name is not None:
                declared.setdefault(_text(name, content), set()).add(qualified)
    receivers = {
        name: next(iter(types))
        for name, types in declared.items()
        if len(types) == 1 and next(iter(types)) in clients
    }
    sent = {
        api: {
            operation.method_name + suffix: operation
            for operation in operations
            for suffix in CLIENT_METHOD_SUFFIXES
        }
        for api, operations in clients.items()
    }
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.named_children))
        if node.type != "method_invocation":
            continue
        target = node.child_by_field_name("object")
        if target is None:
            continue
        if target.type == "object_creation_expression":
            type_node = target.child_by_field_name("type")
            api = values._qualified_class(_text(type_node, content))
        else:
            written = _text(target, content)
            written = written[5:] if written.startswith("this.") else written
            api = receivers.get(written)
        operation = sent.get(api, {}).get(
            _text(node.child_by_field_name("name"), content)
        )
        if operation is None:
            continue
        line = node.start_point.row + 1
        start = (line, node.start_point.column)
        end = (node.end_point.row + 1, node.end_point.column)
        caller = next((i for s, e, i in enclosing if s <= start and end <= e), None)
        url = (operation.base_url if base_fixed else PLACEHOLDER) + operation.path
        requests.append(
            HttpRequest(
                id=make_id("HttpRequest", f"{operation.method_name} {line}"),
                file=file,
                line=line,
                column=node.start_point.column,
                caller_id=caller,
                method=operation.http_method,
                url=url,
                client="openapi-java",
                declared_in=f"{operation.spec_file}:{operation.spec_line}",
            )
        )


def extract_spring(
    tree,
    content,
    repository_id,
    file,
    definitions,
    *,
    service=None,
    context_path=None,
    constants=None,
    generated=None,
    clients=None,
    client_base_fixed=True,
):
    """Routes, requests and unread mappings of one Java file.

    ``definitions`` are the file's parsed declarations; a handler or caller
    is the declaration whose span is exactly the method's, or the innermost
    one enclosing a call. ``generated`` maps a qualified interface name to
    the spec operations openapi-generator makes it declare
    (``service_contracts.openapi``); a controller implementing it handles
    each operation it overrides. ``clients`` maps a qualified Java API class
    generated from a spec to its operations; a call of one of its methods
    sends that operation to the spec's base URL, or to an unknown base when
    ``client_base_fixed`` is false (the module sets the base at run time).
    """
    root = tree.root_node
    imports = _Imports(root, content)
    values = _Values(root, content, constants, imports)
    spans = {
        (d.line, d.column, d.end_line, d.end_column): d
        for d in definitions
        if d.file == file
    }
    enclosing = sorted(
        (
            ((d.line, d.column), (d.end_line, d.end_column), d.id)
            for d in definitions
            if d.file == file
        ),
        reverse=True,
    )
    routes, requests, unread = [], [], []
    ordinals = {}

    def make_id(kind, key):
        ordinal = ordinals.get((kind, key), 0)
        ordinals[(kind, key)] = ordinal + 1
        return identifier(repository_id, file, key, kind, ordinal)

    def span(node):
        return (
            node.start_point.row + 1,
            node.start_point.column,
            node.end_point.row + 1,
            node.end_point.column,
        )

    def type_declarations(node):
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type in (
                "class_declaration",
                "interface_declaration",
                "record_declaration",
            ):
                yield current
            stack.extend(reversed(current.named_children))

    for declaration in type_declarations(root):
        annotations = _annotations(declaration, content)
        simple = {
            imports.spring_name(w, (_WEB, *_FEIGN)): node for w, node in annotations
        }
        feign = next(
            (
                node
                for w, node in annotations
                if imports.spring_name(w, _FEIGN) == "FeignClient"
            ),
            None,
        )
        prefixes = [""]
        type_mapping = next(
            (
                _mapping(w, node, imports, values, content)
                for w, node in annotations
                if imports.spring_name(w, (_WEB,)) == "RequestMapping"
            ),
            None,
        )
        if type_mapping is not None and type_mapping[1]:
            prefixes = type_mapping[1]
        if feign is not None:
            arguments = _arguments(feign)
            target = values.evaluate(arguments.get("name", arguments.get("value")))
            if not target or PLACEHOLDER in target:
                target = None
            base = ""
            if "url" in arguments:
                # An explicit URL bypasses service discovery: the name is
                # then only a client name, and the host is the URL's.
                target = None
                base = values.evaluate(arguments["url"])
                if "${" in base:
                    base = PLACEHOLDER  # a property: the base is not fixed
            feign_path = (
                values.evaluate(arguments["path"]) if "path" in arguments else ""
            )
            prefixes = [base.rstrip("/") + join_paths(feign_path, p) for p in prefixes]
        is_server = feign is None and (
            declaration.type == "interface_declaration"
            or _CONTROLLERS & simple.keys()
            or "RequestMapping" in simple
        )
        body = declaration.child_by_field_name("body")
        if body is None or (feign is None and not is_server):
            continue
        if generated:
            _generated_routes(
                declaration,
                body,
                generated,
                values,
                prefixes,
                context_path,
                service,
                spans,
                span,
                make_id,
                routes,
                unread,
                file,
                content,
            )
        for member in body.named_children:
            if member.type != "method_declaration":
                continue
            handler = spans.get(span(member))
            for written, annotation in _annotations(member, content):
                mapping = _mapping(written, annotation, imports, values, content)
                if mapping is None:
                    continue
                line = annotation.start_point.row + 1
                methods, paths = mapping
                if methods is None:
                    unread.append(
                        Unread(
                            file,
                            line,
                            "route" if is_server else "request",
                            "method_not_literal",
                        )
                    )
                    continue
                for prefix in prefixes:
                    for path in paths or [""]:
                        if feign is not None:
                            url = (
                                prefix.rstrip("/") + "/" + path.lstrip("/")
                                if path
                                else prefix
                            )
                            for method in methods:
                                requests.append(
                                    HttpRequest(
                                        id=make_id("HttpRequest", f"{method} {url}"),
                                        file=file,
                                        line=line,
                                        column=annotation.start_point.column,
                                        caller_id=handler.id if handler else None,
                                        method=None if method == ANY_METHOD else method,
                                        url=url,
                                        client="feign",
                                        target_service=target,
                                    )
                                )
                            continue
                        if PLACEHOLDER in prefix or PLACEHOLDER in path:
                            unread.append(
                                Unread(file, line, "route", "path_not_literal")
                            )
                            continue
                        if handler is None:
                            unread.append(Unread(file, line, "route", "no_handler"))
                            continue
                        full = join_paths(context_path or "", prefix, path)
                        routes.append(
                            HttpRoute(
                                id=make_id("HttpRoute", f"{','.join(methods)} {full}"),
                                file=file,
                                line=line,
                                column=annotation.start_point.column,
                                handler_id=handler.id,
                                methods=methods,
                                path=full,
                                framework="spring-mvc",
                                service=service,
                            )
                        )
    rest_names = _receivers(root, content, imports, _REST_TYPES)
    web_names = _receivers(root, content, imports, _WEB_CLIENT_TYPES)
    if imports.from_spring("WebClient"):
        web_names.add("WebClient")  # WebClient.create() / WebClient.builder()

    def web_client_method(node):
        """The HTTP method of a WebClient ``.uri()`` chain, '' if unknown,
        None if the chain does not start at a WebClient."""
        verb = node.child_by_field_name("object")
        if verb is None or verb.type != "method_invocation":
            return None
        verb_name = _text(verb.child_by_field_name("name"), content)
        if verb_name in _WEB_CLIENT_VERBS:
            method = _WEB_CLIENT_VERBS[verb_name]
        elif verb_name == "method":
            written = verb.child_by_field_name("arguments").named_children
            method = _text(written[0], content).rpartition(".")[2] if written else ""
            method = method if method in HTTP_METHODS else ""
        else:
            return None
        current = verb.child_by_field_name("object")
        while current is not None and current.type == "method_invocation":
            current = current.child_by_field_name("object")
        if current is None:
            return None
        written = _text(current, content)
        written = written[5:] if written.startswith("this.") else written
        return method if written in web_names else None

    if rest_names or web_names:
        stack = [root]
        while stack:
            node = stack.pop()
            stack.extend(reversed(node.named_children))
            if node.type != "method_invocation":
                continue
            name = _text(node.child_by_field_name("name"), content)
            target = node.child_by_field_name("object")
            if target is None:
                continue
            arguments = node.child_by_field_name("arguments").named_children
            if name == "uri" and web_names:
                method = web_client_method(node)
                if method is None:
                    continue
                client, method = "web-client", method or None
            elif name in _REST_METHODS and rest_names:
                receiver = _text(target, content)
                receiver = receiver[5:] if receiver.startswith("this.") else receiver
                if receiver not in rest_names:
                    continue
                client, method = "rest-template", _REST_METHODS[name]
                if method is None:
                    method_node = arguments[1] if len(arguments) > 1 else None
                    written = _text(method_node, content) if method_node else ""
                    method = written.rpartition(".")[2]
                    if method not in HTTP_METHODS:
                        method = None
            else:
                continue
            line = node.start_point.row + 1
            if not arguments:
                continue
            first = arguments[0]
            if first.type not in (
                "string_literal",
                "binary_expression",
                "identifier",
                "field_access",
                "method_invocation",
                "parenthesized_expression",
            ):
                # A java.net.URI value or a URI builder function.
                unread.append(Unread(file, line, "request", "url_not_string"))
                continue
            urls = values.alternatives(first)
            start = (line, node.start_point.column)
            end = (node.end_point.row + 1, node.end_point.column)
            caller = next((i for s, e, i in enclosing if s <= start and end <= e), None)
            for index, url in enumerate(urls):
                key = f"{name} {line}" if len(urls) == 1 else f"{name} {line} {index}"
                requests.append(
                    HttpRequest(
                        id=make_id("HttpRequest", key),
                        file=file,
                        line=line,
                        column=node.start_point.column,
                        caller_id=caller,
                        method=method,
                        url=url,
                        client=client,
                        alternatives=len(urls),
                    )
                )
    if clients:
        _client_requests(
            root,
            content,
            values,
            clients,
            client_base_fixed,
            enclosing,
            make_id,
            requests,
            file,
        )
    return routes, requests, unread
