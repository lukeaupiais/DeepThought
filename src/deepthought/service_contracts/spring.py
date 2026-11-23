"""Spring MVC routes, Feign clients and RestTemplate requests in Java sources.

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


class _Values:
    """Folds string expressions; text they do not fix becomes PLACEHOLDER.

    Folded: string literals, ``+``, ``String.format`` with ``%s``/``%d``, and
    names that hold one value: String fields of the file with an initializer,
    no ``@Value`` injection and no assignment anywhere in the file; String
    locals declared once with an initializer and never assigned in their
    method; and calls to a method of the file, unique by name and argument
    count, whose body is a single ``return`` (arguments substituted for
    parameters).
    """

    def __init__(self, root, content):
        self.content = content
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
        if scope.id not in self._locals:
            content = self.content
            assigned = _assigned_names(scope, content)
            found = {}
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
                            value if string and name not in assigned else None
                        )
                elif node.type == "formal_parameter" or (
                    node is not scope and node.type in _SCOPES
                ):
                    if node.type == "formal_parameter":
                        name = node.child_by_field_name("name")
                        found.setdefault(_text(name, content), []).append(None)
                    if node is not scope and node.type in _SCOPES:
                        continue  # nested lambdas have their own locals
                stack.extend(node.named_children)
            self._locals[scope.id] = {n: v[0] for n, v in found.items() if len(v) == 1}
        return self._locals[scope.id]

    def _name(self, node, env, depth):
        name = _text(node, self.content)
        if env is not None and name in env:
            return env[name]
        scope = node.parent
        while scope is not None and scope.type not in _SCOPES:
            scope = scope.parent
        if scope is not None:
            scoped = self._scope_locals(scope)
            if name in scoped:
                value = scoped[name]
                if value is None or value.start_byte >= node.start_byte:
                    return PLACEHOLDER
                return self._fold(("local", value.id), value, None, depth)
        value = self._fields.get(name)
        if value is None:
            return PLACEHOLDER
        return self._fold(("field", name), value, None, depth)

    def _fold(self, key, node, env, depth):
        if key in self._active:
            return PLACEHOLDER
        self._active.add(key)
        try:
            return self.evaluate(node, env, depth)
        finally:
            self._active.discard(key)

    def _call(self, node, env, depth):
        name = _text(node.child_by_field_name("name"), self.content)
        target = node.child_by_field_name("object")
        arguments = node.child_by_field_name("arguments").named_children
        if target is not None and _text(target, self.content) == "String":
            if name == "format" and arguments:
                template = self.evaluate(arguments[0], env, depth)
                return re.sub(r"%[sd]", PLACEHOLDER, template)
            return PLACEHOLDER
        if target is not None and target.type != "this":
            return PLACEHOLDER
        found = self._methods.get((name, len(arguments)))
        if found is None or depth >= _MAX_INLINE:
            return PLACEHOLDER
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
            return PLACEHOLDER
        returned = statements[0].named_children
        if not returned:
            return PLACEHOLDER
        bound = {p: self.evaluate(a, env, depth) for p, a in zip(parameters, arguments)}
        return self._fold(("method", method.id), returned[0], bound, depth + 1)

    def evaluate(self, node, env=None, depth=0):
        """The expression's text with PLACEHOLDER for what is not fixed."""
        if node is None:
            return PLACEHOLDER
        kind = node.type
        if kind == "string_literal":
            parts = []
            for child in node.named_children:
                text = _text(child, self.content)
                if child.type == "escape_sequence":
                    text = _unescape(text)
                elif child.type == "multiline_string_fragment":
                    return PLACEHOLDER  # text blocks are not read
                parts.append(text)
            return "".join(parts)
        if kind == "parenthesized_expression":
            return self.evaluate(node.named_children[0], env, depth)
        if kind == "binary_expression":
            operator = node.child_by_field_name("operator")
            if operator is not None and operator.type == "+":
                left = self.evaluate(node.child_by_field_name("left"), env, depth)
                right = self.evaluate(node.child_by_field_name("right"), env, depth)
                return left + right
            return PLACEHOLDER
        if kind == "identifier":
            return self._name(node, env, depth)
        if kind == "field_access":
            # this.x or Class.CONSTANT of a class in this file.
            field = node.child_by_field_name("field")
            value = self._fields.get(_text(field, self.content))
            if value is None:
                return PLACEHOLDER
            return self._fold(("field", _text(field, self.content)), value, None, depth)
        if kind == "method_invocation":
            return self._call(node, env, depth)
        return PLACEHOLDER

    def strings(self, node):
        """Each string of an annotation value (an array gives several)."""
        if node is None:
            return []
        if node.type == "element_value_array_initializer":
            return [self.evaluate(c) for c in node.named_children]
        return [self.evaluate(node)]


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
_SEARCH_LOCATIONS = "spring.cloud.config.server.native.search-locations"


def _module_root(root_path, file):
    """The nearest directory above ``file`` with a Maven or Gradle build."""
    path = PurePosixPath(file).parent
    while True:
        if any((root_path / path / name).is_file() for name in _BUILD_FILES):
            return path
        if path == PurePosixPath("."):
            return None
        path = path.parent


def _yaml_values(text, key_path):
    """Scalars at ``key_path`` in a YAML file, read by indentation only.

    Block mappings with scalar values are read; flow collections, anchors and
    multi-line scalars are not. Every document of the file is read.
    """
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


def _properties_values(text, key):
    found = []
    for raw in text.splitlines():
        match = re.match(r"^\s*([^#!=:\s]+)\s*[=:]\s*(.*?)\s*$", raw)
        if match and match.group(1) == key:
            found.append(match.group(2))
    return found


def _read_values(path, key):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    reader = _properties_values if path.suffix == ".properties" else _yaml_values
    return [v for v in reader(text, key) if v and "${" not in v]


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
                for value in _read_values(path, _SEARCH_LOCATIONS):
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


def extract_spring(
    tree, content, repository_id, file, definitions, *, service=None, context_path=None
):
    """Routes, requests and unread mappings of one Java file.

    ``definitions`` are the file's parsed declarations; a handler or caller
    is the declaration whose span is exactly the method's, or the innermost
    one enclosing a call.
    """
    root = tree.root_node
    imports = _Imports(root, content)
    values = _Values(root, content)
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
            url = values.evaluate(first)
            start = (line, node.start_point.column)
            end = (node.end_point.row + 1, node.end_point.column)
            caller = next((i for s, e, i in enclosing if s <= start and end <= e), None)
            requests.append(
                HttpRequest(
                    id=make_id("HttpRequest", f"{name} {line}"),
                    file=file,
                    line=line,
                    column=node.start_point.column,
                    caller_id=caller,
                    method=method,
                    url=url,
                    client=client,
                )
            )
    return routes, requests, unread
