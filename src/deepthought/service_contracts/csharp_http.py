"""C# HttpClient requests.

A request is a call on a value of type ``HttpClient``: a parameter (primary
constructor parameters included), field, property or local declared with
that type, or ``new HttpClient(...)``, looked up in the innermost enclosing
member, function or type that declares the name. ``GetAsync``,
``GetFromJsonAsync``, ``PostAsJsonAsync``, ... fix the method; ``SendAsync``
takes it from ``new HttpRequestMessage(HttpMethod.X, url)``, in place or
through a local bound once to one.

The URL is folded from string literals, interpolated strings, ``+``,
``new Uri(...)``, locals assigned once in their member, fields of the class
assigned only by their initializer, ``Trim``/``TrimEnd``/``TrimStart`` with
character literals, and calls of a method of the file that returns one
expression, with its parameters bound to the call's arguments; anything else
is unknown text.

A typed client ``T`` registered with
``AddHttpClient<T>(o => o.BaseAddress = new(...))`` (or
``AddHttpClient<I, T>``) anywhere in the repository has that base address
for the HttpClient parameters, fields and properties of class ``T``; a class
registered with different literal bases has none. A relative URL is
resolved against the base (``https+http://`` service discovery URLs count
as ``http://``); with no base it is a path under an unknown base.
"""

import re

from ..syntax import identifier
from .http import PLACEHOLDER
from .model import HttpRequest

CLIENT = "dotnet-httpclient"
_HTTP_CLIENT = re.compile(r"(?:global::)?(?:System\.Net\.Http\.)?HttpClient")
_VERBS = {
    "GetAsync": "GET",
    "GetStringAsync": "GET",
    "GetStreamAsync": "GET",
    "GetByteArrayAsync": "GET",
    "GetFromJsonAsync": "GET",
    "GetFromJsonAsAsyncEnumerable": "GET",
    "PostAsync": "POST",
    "PostAsJsonAsync": "POST",
    "PutAsync": "PUT",
    "PutAsJsonAsync": "PUT",
    "PatchAsync": "PATCH",
    "PatchAsJsonAsync": "PATCH",
    "DeleteAsync": "DELETE",
    "DeleteFromJsonAsync": "DELETE",
}
_METHODS = {"Get", "Post", "Put", "Patch", "Delete", "Head", "Options"}
_TYPES = {
    "class_declaration",
    "struct_declaration",
    "record_declaration",
    "interface_declaration",
}
_SCOPES = _TYPES | {
    "compilation_unit",
    "method_declaration",
    "constructor_declaration",
    "local_function_statement",
    "lambda_expression",
    "anonymous_method_expression",
    "accessor_declaration",
    "operator_declaration",
}
_SCHEME = re.compile(r"(?i)^([a-z][a-z0-9+.-]*)://")


def _text(node, content):
    return content[node.start_byte : node.end_byte].decode("utf-8")


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def _unwrap(node):
    while node is not None and node.type in ("parenthesized_expression", "argument"):
        node = node.named_children[-1] if node.named_children else None
    return node


def _name_text(node, content):
    """``M`` of ``M`` or ``M<T>``."""
    if node is None:
        return None
    if node.type == "generic_name":
        node = node.named_children[0] if node.named_children else None
    return None if node is None else _text(node, content)


def _scope(node):
    node = node.parent
    while node is not None and node.type not in _SCOPES:
        node = node.parent
    return node


def _span(node):
    return None if node is None else (node.start_byte, node.end_byte, node.type)


def _literal(node, content):
    """The text of a string literal node, or None."""
    if node.type == "string_literal":
        text = _text(node, content)[1:-1]
        return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m[1], m[1]), text)
    if node.type == "verbatim_string_literal":
        return _text(node, content)[2:-1].replace('""', '"')
    if node.type == "raw_string_literal":
        parts = [c for c in node.named_children if c.type == "raw_string_content"]
        return "".join(_text(c, content) for c in parts)
    return None


def _namespaces(root, node, content):
    """The namespaces a name written at ``node`` is looked up in: those that
    enclose it (and their parents) and the file's ``using N;`` directives."""
    found = set()
    for child in _walk(root):
        if child.type == "using_directive":
            names = child.named_children
            if len(names) == 1 and child.child_by_field_name("name") is None:
                found.add(_text(names[0], content))
        elif child.type == "file_scoped_namespace_declaration":
            name = child.child_by_field_name("name")
            if name is not None:
                found.add(_text(name, content))
    ancestor = node.parent
    while ancestor is not None:
        if ancestor.type == "namespace_declaration":
            name = ancestor.child_by_field_name("name")
            if name is not None:
                found.add(_text(name, content))
        ancestor = ancestor.parent
    for namespace in list(found):
        parts = namespace.split(".")
        found.update(".".join(parts[:end]) for end in range(1, len(parts)))
    return found


def typed_client_registrations(trees):
    """(file, written class, namespaces in scope, base URL or None) of each
    ``AddHttpClient<T>(...)`` (or ``<I, T>``) that assigns ``BaseAddress``;
    the base is None when it is not a literal."""
    found = []
    for file, tree, content in trees:
        for node in _walk(tree.root_node):
            if node.type != "invocation_expression":
                continue
            function = node.child_by_field_name("function")
            if function is None or function.type != "member_access_expression":
                continue
            name = function.child_by_field_name("name")
            if name is None or name.type != "generic_name":
                continue
            if _name_text(name, content) != "AddHttpClient":
                continue
            arguments = [
                c for c in name.named_children if c.type == "type_argument_list"
            ]
            types = arguments[0].named_children if arguments else []
            if not types:
                continue
            target = re.sub(r"\s+", "", _text(types[-1], content))
            call_arguments = node.child_by_field_name("arguments")
            for child in _walk(call_arguments) if call_arguments else []:
                if child.type != "assignment_expression":
                    continue
                left = child.child_by_field_name("left")
                if left is None or _text(left, content).rpartition(".")[2] != (
                    "BaseAddress"
                ):
                    continue
                value = _unwrap(child.child_by_field_name("right"))
                if value is not None and value.type in (
                    "object_creation_expression",
                    "implicit_object_creation_expression",
                ):
                    args = [
                        a
                        for a in _walk(value)
                        if a.type == "argument" and a.parent.parent == value
                    ]
                    value = _unwrap(args[0]) if len(args) == 1 else None
                literal = None if value is None else _literal(value, content)
                found.append(
                    (
                        file,
                        target,
                        _namespaces(tree.root_node, node, content),
                        literal,
                    )
                )
    return found


def typed_client_bases(registrations, classes, global_namespaces=None):
    """{qualified class: base URL or None}. A written class names the one
    class of ``classes`` (qualified names) it denotes from the namespaces in
    scope (plus ``global_namespaces(file)``, the ``global using``s of the
    registering file's project), or, failing that, the only class of that
    name; a class registered with different bases, or with one that is not
    literal, has None."""
    by_name = {}
    for qualified in classes:
        by_name.setdefault(qualified.rpartition(".")[2], []).append(qualified)
    bases = {}
    for file, written, namespaces, base in registrations:
        simple = written.rpartition(".")[2]
        candidates = [
            c
            for c in by_name.get(simple, [])
            if c == written or c.endswith("." + written)
        ]
        scope = set(namespaces) | (
            global_namespaces(file) if global_namespaces else set()
        )
        in_scope = [
            c for c in candidates if "." in written or c.rpartition(".")[0] in scope
        ]
        chosen = in_scope if len(in_scope) == 1 else candidates
        if len(chosen) != 1:
            continue
        bases.setdefault(chosen[0], set()).add(base)
    return {
        name: next(iter(found)) if len(found) == 1 else None
        for name, found in bases.items()
    }


def project_of(root, file):
    """The nearest directory above ``file`` holding a ``.csproj`` ("" for
    the repository root), or None."""
    parts = file.split("/")[:-1]
    for end in range(len(parts), -1, -1):
        directory = root.joinpath(*parts[:end]) if end else root
        if any(directory.glob("*.csproj")):
            return "/".join(parts[:end])
    return None


def global_usings(root, trees):
    """A function giving, for a file, the namespaces of the ``global using
    N;`` directives of its project (the files under the same nearest
    ``.csproj`` directory)."""
    by_project = {}
    projects = {}

    def project(file):
        directory = file.rpartition("/")[0]
        if directory not in projects:
            projects[directory] = project_of(root, file)
        return projects[directory]

    for file, tree, content in trees:
        found = by_project.setdefault(project(file), set())
        for node in _walk(tree.root_node):
            if node.type == "using_directive" and _text(node, content).startswith(
                "global "
            ):
                names = node.named_children
                if len(names) == 1 and node.child_by_field_name("name") is None:
                    found.add(_text(names[0], content))
    return lambda file: by_project.get(project(file), set())


def resolve_url(base, url):
    """``url`` resolved against ``base`` (None when unknown)."""

    def normal(text):
        match = _SCHEME.match(text)
        if match and match.group(1).lower() in ("https+http", "http+https"):
            text = "http://" + text[match.end() :]
        return text

    url = normal(url)
    if _SCHEME.match(url):
        return url
    if base is None:
        return url if url.startswith(("/", PLACEHOLDER)) else PLACEHOLDER + "/" + url
    base = normal(base)
    match = _SCHEME.match(base)
    if match is None:
        return PLACEHOLDER + "/" + url.lstrip("/")
    authority, slash, path = base[match.end() :].partition("/")
    origin = base[: match.end()] + authority
    if url.startswith("/"):
        return origin + url
    directory = (slash + path).rpartition("/")[0]
    return origin + directory + "/" + url


def extract_csharp_requests(tree, content, repository_id, file, definitions, bases):
    """HttpClient requests of one C# file; ``bases`` is
    ``typed_client_bases`` of the repository."""
    root = tree.root_node
    file_definitions = [d for d in definitions if d.file == file]
    classes = sorted(
        (
            ((d.line, d.column), (d.end_line, d.end_column), d.symbol)
            for d in file_definitions
            if d.kind in ("Class", "Struct", "Record")
        ),
        reverse=True,
    )

    def class_of(node):
        start = (node.start_point.row + 1, node.start_point.column)
        end = (node.end_point.row + 1, node.end_point.column)
        return next((q for s, e, q in classes if s <= start and end <= e), None)

    enclosing = sorted(
        (
            ((d.line, d.column), (d.end_line, d.end_column), d.id)
            for d in file_definitions
        ),
        reverse=True,
    )

    def caller(node):
        start = (node.start_point.row + 1, node.start_point.column)
        end = (node.end_point.row + 1, node.end_point.column)
        return next((i for s, e, i in enclosing if s <= start and end <= e), None)

    # name -> declarations: (scope span, name) -> [(kind, type text, value)]
    declared, assigned = {}, {}
    for node in _walk(root):
        if node.type in ("parameter", "property_declaration"):
            name = node.child_by_field_name("name")
            type_node = node.child_by_field_name("type")
            if name is not None:
                value = node.child_by_field_name("value")
                declared.setdefault(
                    (_span(_scope(node)), _text(name, content)), []
                ).append(
                    (
                        node.type,
                        None if type_node is None else _text(type_node, content),
                        value,
                        node,
                    )
                )
        elif node.type == "variable_declaration":
            type_node = node.child_by_field_name("type")
            written = None if type_node is None else _text(type_node, content)
            kind = (
                "field"
                if node.parent is not None and node.parent.type == "field_declaration"
                else "local"
            )
            for declarator in node.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name = declarator.child_by_field_name("name")
                if name is None:
                    continue
                values = [c for c in declarator.named_children if c != name]
                declared.setdefault(
                    (_span(_scope(node)), _text(name, content)), []
                ).append((kind, written, values[-1] if values else None, node))
        elif node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            if left is None:
                continue
            text = _text(left, content).removeprefix("this.")
            assigned[text] = assigned.get(text, 0) + 1

    def lookup(at, name, members_only=False):
        """The declarations of ``name`` in the innermost scope above ``at``
        that declares it, or None."""
        where = _scope(at)
        while where is not None:
            if not members_only or where.type in _TYPES:
                found = declared.get((_span(where), name))
                if found is not None:
                    return found
            where = _scope(where)
        return None

    def own(node):
        members_only = False
        if node is not None and node.type == "member_access_expression":
            if not _text(node, content).startswith("this."):
                return None
            node, members_only = node.child_by_field_name("name"), True
        if node is not None and node.type == "identifier":
            return _text(node, content), members_only
        return None

    def bound(node):
        """The one declaration a name used at ``node`` denotes, whose value
        is fixed (a constructor or initializer value nothing else assigns),
        or None."""
        found = own(node)
        if found is None:
            return None
        declarations = lookup(node, *found)
        if declarations is None or len(declarations) != 1:
            return None
        return declarations[0]

    methods = {}
    for node in _walk(root):
        if node.type == "method_declaration":
            name = node.child_by_field_name("name")
            if name is not None:
                methods.setdefault(_text(name, content), []).append(node)

    def returned(method):
        """The one expression a method returns, or None."""
        body = method.child_by_field_name("body")
        if body is None:
            for child in method.named_children:
                if child.type == "arrow_expression_clause" and child.named_children:
                    return child.named_children[0]
            return None
        found, stack = [], list(body.named_children)
        while stack:
            current = stack.pop()
            if current.type in (
                "local_function_statement",
                "lambda_expression",
                "anonymous_method_expression",
            ):
                continue
            if current.type == "return_statement":
                found.append(current)
            stack.extend(current.named_children)
        if len(found) != 1 or not found[0].named_children:
            return None
        return found[0].named_children[0]

    def parameters(method):
        listed = method.child_by_field_name("parameters")
        names = []
        for parameter in listed.named_children if listed else []:
            name = parameter.child_by_field_name("name")
            if parameter.type != "parameter" or name is None:
                return None
            names.append(_text(name, content))
        return names

    def fold(node, depth=0, env=None):
        """The text ``node`` evaluates to, PLACEHOLDER for unknown parts.
        ``env`` maps the parameters of a helper method being folded to
        (argument node, the caller's env)."""
        node = _unwrap(node)
        if node is None or depth > 8:
            return PLACEHOLDER
        literal = _literal(node, content)
        if literal is not None:
            return literal
        if node.type == "interpolated_string_expression":
            parts = []
            for child in node.named_children:
                if child.type == "string_content":
                    parts.append(_text(child, content))
                elif child.type == "escape_sequence":
                    parts.append(_literal_escape(_text(child, content)))
                elif child.type == "interpolation":
                    inner = [
                        c
                        for c in child.named_children
                        if not c.type.startswith("interpolation_")
                    ]
                    parts.append(
                        fold(inner[0], depth + 1, env) if inner else PLACEHOLDER
                    )
            return "".join(parts)
        if node.type == "binary_expression":
            operator = node.child_by_field_name("operator")
            if operator is not None and _text(operator, content) != "+":
                return PLACEHOLDER
            return fold(node.child_by_field_name("left"), depth + 1, env) + fold(
                node.child_by_field_name("right"), depth + 1, env
            )
        if node.type == "object_creation_expression":
            type_node = node.child_by_field_name("type")
            arguments = node.child_by_field_name("arguments")
            if (
                type_node is not None
                and _text(type_node, content).rpartition(".")[2] == "Uri"
                and arguments is not None
                and len(arguments.named_children) == 1
            ):
                return fold(arguments.named_children[0], depth + 1, env)
            return PLACEHOLDER
        if node.type == "invocation_expression":
            return folded_call(node, depth, env)
        if env and node.type == "identifier" and _text(node, content) in env:
            argument, outer = env[_text(node, content)]
            return fold(argument, depth + 1, outer)
        declaration = bound(node)
        if declaration is None:
            return PLACEHOLDER
        kind, _, value, _ = declaration
        name = own(node)[0]
        if kind == "parameter" or value is None or assigned.get(name):
            return PLACEHOLDER
        return fold(value, depth + 1, env)

    def folded_call(node, depth, env):
        """``s.TrimEnd('c', ...)`` (and ``TrimStart``/``Trim``) with char
        literals, or a call of a method of the file (one of its name) that
        returns one expression, folded with its parameters bound to the
        call's positional arguments."""
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        positional = (
            [a for a in arguments.named_children if a.type == "argument"]
            if arguments
            else []
        )
        if function is not None and function.type == "member_access_expression":
            name = _text(function.child_by_field_name("name"), content)
            if name in ("TrimEnd", "TrimStart", "Trim"):
                chars = []
                for argument in positional:
                    value = _unwrap(argument)
                    if value is None or value.type != "character_literal":
                        return PLACEHOLDER
                    chars.append(_text(value, content)[1:-1])
                text = fold(function.child_by_field_name("expression"), depth + 1, env)
                stripped = "".join(chars) if chars else None
                if name in ("TrimEnd", "Trim") and not text.endswith(PLACEHOLDER):
                    text = text.rstrip(stripped)
                if name in ("TrimStart", "Trim") and not text.startswith(PLACEHOLDER):
                    text = text.lstrip(stripped)
                return text
            if not _text(function, content).startswith("this."):
                return PLACEHOLDER
            function = function.child_by_field_name("name")
        if function is None or function.type != "identifier":
            return PLACEHOLDER
        declared = methods.get(_text(function, content), [])
        if len(declared) != 1 or any(
            a.child_by_field_name("name") is not None for a in positional
        ):
            return PLACEHOLDER
        expression, names = returned(declared[0]), parameters(declared[0])
        if expression is None or names is None or len(names) != len(positional):
            return PLACEHOLDER
        return fold(
            expression,
            depth + 1,
            {n: (a, env) for n, a in zip(names, positional)},
        )

    def is_client(node):
        """Whether ``node`` is an HttpClient value; (True, from a member of
        the class) or (True, False) or None."""
        node = _unwrap(node)
        if node is None:
            return None
        if node.type == "object_creation_expression":
            type_node = node.child_by_field_name("type")
            if type_node is not None and _HTTP_CLIENT.fullmatch(
                re.sub(r"\s+", "", _text(type_node, content))
            ):
                return False
            return None
        found = own(node)
        if found is None:
            return None
        declarations = lookup(node, *found)
        if not declarations or len(declarations) != 1:
            return None
        kind, written, value, where = declarations[0]
        if written is not None and _HTTP_CLIENT.fullmatch(re.sub(r"\s+", "", written)):
            # Members and constructor parameters of the class take the
            # typed client's base address; locals and other parameters
            # do not.
            if kind in ("field", "property_declaration"):
                return True
            if kind == "parameter":
                owner = where.parent.parent if where.parent is not None else None
                return owner is not None and owner.type in (
                    _TYPES | {"constructor_declaration"}
                )
            return False
        if written == "var" and value is not None:
            return is_client(value)
        return None

    def message(node, depth=0):
        """(method, URL node) of ``new HttpRequestMessage(HttpMethod.X, url)``
        or a local bound once to one, or None."""
        node = _unwrap(node)
        if node is None or depth > 2:
            return None
        if node.type == "object_creation_expression":
            type_node = node.child_by_field_name("type")
            if type_node is None or (
                _text(type_node, content).rpartition(".")[2] != "HttpRequestMessage"
            ):
                return None
            arguments = node.child_by_field_name("arguments")
            positional = arguments.named_children if arguments else []
            if len(positional) != 2:
                return None
            verb = _unwrap(positional[0])
            method = None
            if verb is not None and verb.type == "member_access_expression":
                owner, name = (
                    verb.child_by_field_name("expression"),
                    verb.child_by_field_name("name"),
                )
                if (
                    owner is not None
                    and _text(owner, content).rpartition(".")[2] == "HttpMethod"
                    and _text(name, content) in _METHODS
                ):
                    method = _text(name, content).upper()
            return method, positional[1]
        declaration = bound(node)
        if declaration is None:
            return None
        kind, _, value, _ = declaration
        if kind != "local" or value is None or assigned.get(own(node)[0]):
            return None
        return message(value, depth + 1)

    requests, ordinals = [], {}
    for node in _walk(root):
        if node.type != "invocation_expression":
            continue
        function = node.child_by_field_name("function")
        if function is None or function.type != "member_access_expression":
            continue
        verb = _name_text(function.child_by_field_name("name"), content)
        if verb not in _VERBS and verb != "SendAsync":
            continue
        member = is_client(function.child_by_field_name("expression"))
        if member is None:
            continue
        arguments = node.child_by_field_name("arguments")
        positional = (
            [a for a in arguments.named_children if a.type == "argument"]
            if arguments
            else []
        )
        if not positional:
            continue
        if verb == "SendAsync":
            found = message(positional[0])
            if found is None:
                continue
            method, url_node = found
        else:
            method, url_node = _VERBS[verb], positional[0]
        base = bases.get(class_of(node)) if member else None
        url = resolve_url(base, fold(url_node))
        at = function.child_by_field_name("name")
        line = at.start_point.row + 1
        key = f"{CLIENT} {line}"
        ordinal = ordinals.get(key, 0)
        ordinals[key] = ordinal + 1
        requests.append(
            HttpRequest(
                id=identifier(repository_id, file, key, "HttpRequest", ordinal),
                file=file,
                line=line,
                column=at.start_point.column,
                caller_id=caller(node),
                method=method,
                url=url,
                client=CLIENT,
            )
        )
    return requests


def _literal_escape(text):
    if text in ("{{", "}}"):
        return text[0]
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m[1], m[1]), text)
