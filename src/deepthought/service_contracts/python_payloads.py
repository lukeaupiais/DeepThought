"""The types a FastAPI path operation reads its request body as and
writes its response from, resolved through a scip-python index.

The route reader (``web._fastapi_payloads``) records the spans of the
handler's parameters and of its response model (the decorator's
``response_model``, else the return annotation). Here every name written in
such an annotation is resolved through the index (``ScipReferences``) to
the repository's class it declares, and FastAPI's own reading of a
parameter is followed:

- a parameter named in the path, or whose default or ``Annotated`` metadata
  is ``Depends``, ``Security``, ``Query``, ``Path``, ``Header`` or
  ``Cookie`` of ``fastapi.param_functions``, is not the body;
- one marked ``Body``, ``Form`` or ``File`` is;
- an unmarked one is the body when its annotation names a model class: a
  class of the repository with a base of ``pydantic`` or ``sqlmodel``
  (directly or through classes of the repository) or decorated with
  ``dataclasses.dataclass`` or ``pydantic.dataclasses.dataclass``. An enum
  or another plain class is a query parameter.

When more than one parameter is the body, or a ``Body`` says
``embed=True``, FastAPI embeds each under its name (or literal ``alias``),
which is the edge's ``body_key``. ``Annotated[T, ...]`` gives T; every
other subscript is read through (``list[Order]``, ``Optional[Order]``,
``Page[Order]`` carry Order). The markers and the bases are told by the
SCIP symbols of the packages that declare them, so they resolve only when
the indexer saw FastAPI and pydantic installed; a name the index records no
reference for is counted as unresolved.
"""

import re

from .folding import text

_SYMBOL = re.compile(r"scip-python python \S+ \S+ `?([\w.]+)`?/(\w+)(\(\)\.|#|\.)$")
_NOT_BODY = {"Depends", "Security", "Query", "Path", "Header", "Cookie"}
_BODY = {"Body", "Form", "File"}
_MODEL_PACKAGES = ("pydantic", "sqlmodel")
_DATACLASS = {("dataclasses", "dataclass"), ("pydantic.dataclasses", "dataclass")}


def _symbol(native):
    """(module, name) of a SCIP symbol of scip-python, else None."""
    match = _SYMBOL.match(native)
    return None if match is None else (match.group(1), match.group(2))


def _symbols_at(references, file, node):
    """(module, name) of the symbols the name ``node`` (an identifier, or
    the last name of an attribute) references; None when unrecorded."""
    if node.type == "attribute":
        node = node.child_by_field_name("attribute")
    if node is None or node.type != "identifier":
        return None
    found = references.symbols(file, node.start_point.row + 1, node.start_point.column)
    parsed = {s for s in map(_symbol, found or ()) if s is not None}
    # A local symbol only: the import did not resolve.
    return parsed or None


def _marker(references, file, node):
    """The fastapi.param_functions function a call ``node`` makes, else
    None; "" when it cannot be told (no index reference there)."""
    if node is None or node.type != "call":
        return None
    symbols = _symbols_at(references, file, node.child_by_field_name("function"))
    if symbols is None:
        return ""
    for module, name in symbols:
        if module in ("fastapi.param_functions", "fastapi.params", "fastapi"):
            if name in _BODY or name in _NOT_BODY:
                return name
    return None


def _keywords(call, content):
    arguments = call.child_by_field_name("arguments")
    return {
        text(a.child_by_field_name("name"), content): a.child_by_field_name("value")
        for a in ([] if arguments is None else arguments.named_children)
        if a.type == "keyword_argument"
    }


class _Reader:
    def __init__(self, references, trees):
        self.references = references
        self.trees = trees  # file -> (content, tree)
        self.models = {}

    @staticmethod
    def _generic(node):
        """(the generic, [its arguments]) of ``X[...]``, written as a type
        (generic_type) or an expression (subscript), else None."""
        if node.type == "subscript":
            value = node.child_by_field_name("value")
            return value, [c for c in node.named_children if c != value]
        if node.type == "generic_type" and node.named_children:
            value = node.named_children[0]
            arguments = [
                a
                for c in node.named_children[1:]
                if c.type == "type_parameter"
                for a in c.named_children
            ]
            return value, arguments
        return None

    def _annotated(self, file, value):
        """Whether the generic ``value`` is typing's Annotated."""
        symbols = _symbols_at(self.references, file, value)
        return bool(symbols) and any(
            name == "Annotated" and module in ("typing", "typing_extensions")
            for module, name in symbols
        )

    def names(self, file, node):
        """[(name node, Annotated metadata nodes)] of the type names written
        in the annotation ``node``; metadata of Annotated is not a type."""
        found, metadata = [], []
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type in ("identifier", "attribute"):
                found.append(current)
            elif (generic := self._generic(current)) is not None:
                value, inner = generic
                if self._annotated(file, value):
                    metadata.extend(
                        c.named_children[0]
                        if c.type == "type" and c.named_child_count == 1
                        else c
                        for c in inner[1:]
                    )
                    stack.extend(inner[:1])
                else:
                    stack.append(value)
                    stack.extend(inner)
            elif current.type == "string":
                continue
            else:
                stack.extend(current.named_children)
        return found, metadata

    def classes(self, file, name):
        """The repository's classes the type name ``name`` resolves to;
        None when the index records no reference there."""
        last = (
            name.child_by_field_name("attribute") if name.type == "attribute" else name
        )
        if last is None:
            return ()
        content = self.trees[file][0]
        declared = self.references.declarations(
            file, last.start_point.row + 1, last.start_point.column, text(last, content)
        )
        if declared is None:
            return None
        if not declared and _symbols_at(self.references, file, last) is None:
            return None  # only a local symbol: the import did not resolve
        return tuple(d for d in declared if d.kind == "Class")

    def _class_node(self, definition):
        content, tree = self.trees.get(definition.file, (None, None))
        if tree is None:
            return None
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == "class_definition":
                name = node.child_by_field_name("name")
                at = {
                    (node.start_point.row + 1, node.start_point.column),
                    (name.start_point.row + 1, name.start_point.column),
                }
                if (definition.line, definition.column) in at:
                    return node
            stack.extend(node.named_children)
        return None

    def is_model(self, definition, depth=8):
        """Whether a class is one FastAPI reads as a body model; None when
        its bases cannot be told."""
        if definition.id in self.models:
            return self.models[definition.id]
        self.models[definition.id] = False  # cycles
        node = self._class_node(definition)
        found = False if node is not None else None
        if node is not None:
            file = definition.file
            decorated = node.parent
            if decorated is not None and decorated.type == "decorated_definition":
                for decorator in decorated.named_children:
                    if decorator.type != "decorator" or not decorator.named_children:
                        continue
                    expression = decorator.named_children[0]
                    if expression.type == "call":
                        expression = expression.child_by_field_name("function")
                    symbols = _symbols_at(self.references, file, expression)
                    if symbols is None:
                        found = None
                    elif symbols & _DATACLASS:
                        found = True
            bases = node.child_by_field_name("superclasses")
            for base in [] if bases is None or found else bases.named_children:
                if base.type not in ("identifier", "attribute"):
                    continue
                symbols = _symbols_at(self.references, file, base)
                if symbols is None:
                    found = None
                    continue
                if any(m.split(".")[0] in _MODEL_PACKAGES for m, _ in symbols):
                    found = True
                    break
                for parent in self.classes(file, base) or ():
                    if depth > 0 and self.is_model(parent, depth - 1):
                        found = True
                        break
                if found:
                    break
        self.models[definition.id] = found
        return found


def route_payloads(references, trees, file, payloads, path_parameters):
    """([(direction, class Definition, body key or None)], unresolved) of a
    FastAPI route's ``payloads`` (``HttpRoute.payloads`` spans in ``file``).
    ``trees`` maps a file to its (content, tree); ``path_parameters`` are
    the names in the route's path."""
    reader = _Reader(references, trees)
    content, tree = trees[file]
    found, unresolved, bodies = [], 0, []
    for direction, start, end, _ in payloads:
        node = tree.root_node.descendant_for_byte_range(start, end)
        if node is None:
            continue
        if direction == "response":
            names, _ = reader.names(file, node)
            for name in names:
                classes = reader.classes(file, name)
                if classes is None:
                    unresolved += 1
                    continue
                found.extend(("response", c, None) for c in classes)
            continue
        if node.type not in ("typed_parameter", "typed_default_parameter"):
            continue
        name_node = node.child_by_field_name("name") or next(
            (c for c in node.named_children if c.type == "identifier"), None
        )
        if name_node is None:
            continue
        name = text(name_node, content)
        if name in path_parameters:
            continue
        annotation = node.child_by_field_name("type")
        if annotation is None:
            continue
        names, metadata = reader.names(file, annotation)
        markers = [
            (m, call)
            for call in [node.child_by_field_name("value"), *metadata]
            if (m := _marker(references, file, call)) is not None
        ]
        if any(m == "" for m, _ in markers):
            unresolved += 1
            continue
        if any(m in _NOT_BODY for m, _ in markers):
            continue
        marked = [call for m, call in markers if m in _BODY]
        classes, unknown = [], False
        for written in names:
            resolved = reader.classes(file, written)
            if resolved is None:
                unknown = True
                continue
            classes.extend(resolved)
        if not marked:
            models = [reader.is_model(c) for c in classes]
            if not any(models):
                if unknown or None in models:
                    unresolved += 1
                continue
            classes = [c for c, model in zip(classes, models) if model]
        elif unknown:
            unresolved += 1
        key, embed = name, False
        for call in marked:
            keywords = _keywords(call, content)
            alias, embedded = keywords.get("alias"), keywords.get("embed")
            if alias is not None and alias.type == "string":
                key = text(alias, content)[1:-1]
            embed = embed or (
                embedded is not None and text(embedded, content) == "True"
            )
        bodies.append((key, embed, classes))
    for key, embed, classes in bodies:
        key = key if embed or len(bodies) > 1 else None
        found.extend(("request", c, key) for c in classes)
    return found, unresolved
