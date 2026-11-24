"""Which JavaScript/TypeScript names hold a STOMP client.

A name (``x``, or ``this.x`` for a class member) holds a client kind when
every declaration and assignment of it in the file gives that kind:

- ``stompjs``: ``Stomp.over``/``Stomp.client``, ``webstomp.over``/
  ``webstomp.client``, or a ``@stomp/stompjs`` ``Client`` (or the UMD
  bundle's global ``StompJs.Client`` when the file does not bind
  ``StompJs``);
- ``rx-stomp``: a ``@stomp/rx-stomp`` ``RxStomp`` (or the global
  ``RxStomp.RxStomp``);
- ``ng2-stomp-service``: an ``ng2-stomp-service`` ``StompService``.

A value is ``new C(...)`` of such a class or a factory call. In TypeScript a
declaration typed with such a class gives its kind too: a variable or field
without a value, a parameter (and ``this.x`` for a constructor parameter
property) and a getter's return type.
"""

from .folding import text

CLASSES = {
    ("@stomp/stompjs", "Client"): "stompjs",
    ("@stomp/rx-stomp", "RxStomp"): "rx-stomp",
    ("ng2-stomp-service", "StompService"): "ng2-stomp-service",
}
# Globals of the libraries' browser bundles.
GLOBALS = {"StompJs.Client": "stompjs", "RxStomp.RxStomp": "rx-stomp"}
FACTORIES = {"Stomp.over", "Stomp.client", "webstomp.over", "webstomp.client"}

_FIELDS = ("public_field_definition", "field_definition")
_PARAMETERS = ("required_parameter", "optional_parameter")


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def class_kind(written, modules, globals_allowed=True, classes=None):
    """The client kind of the class written ``written`` (``Client``,
    ``StompJs.Client``), given the file's imports {local name: module}."""
    owner, _, member = written.rpartition(".")
    for (module, name), kind in (CLASSES if classes is None else classes).items():
        if modules.get(written) == f"{module}.{name}" or (
            owner and member == name and modules.get(owner) == module
        ):
            return kind
    if globals_allowed and written in GLOBALS and owner not in modules:
        return GLOBALS[written]
    return None


def value_kind(value, content, modules):
    """The client kind a value gives, or None."""
    if value is None:
        return None
    if value.type == "new_expression":
        constructor = value.child_by_field_name("constructor")
        return class_kind(text(constructor, content), modules)
    if value.type == "call_expression":
        called = text(value.child_by_field_name("function"), content)
        return "stompjs" if called in FACTORIES else None
    return None


def type_kind(node, content, modules, classes=None):
    """The client kind of the type annotation (or getter return type) on a
    declaration, or None."""
    annotation = node.child_by_field_name("return_type") or next(
        (c for c in node.named_children if c.type == "type_annotation"), None
    )
    if annotation is None:
        return None
    types = [c for c in annotation.named_children if c.type != "comment"]
    if len(types) != 1 or types[0].type not in (
        "type_identifier",
        "nested_type_identifier",
    ):
        return None
    return class_kind(
        text(types[0], content), modules, globals_allowed=False, classes=classes
    )


def _declarations(root, content, modules, classify, classes):
    """(written name, kind or None) for each declaration or assignment."""
    for node in _walk(root):
        kind = node.type
        if kind == "variable_declarator":
            name, value = (
                node.child_by_field_name("name"),
                node.child_by_field_name("value"),
            )
            if name is None:
                continue
            if value is not None:
                yield text(name, content), classify(value)
            else:
                typed = type_kind(node, content, modules, classes)
                if typed is not None:
                    yield text(name, content), typed
        elif kind == "assignment_expression":
            name, value = (
                node.child_by_field_name("left"),
                node.child_by_field_name("right"),
            )
            if name is not None and value is not None:
                yield text(name, content), classify(value)
        elif kind in _FIELDS:
            name = node.child_by_field_name("name") or node.child_by_field_name(
                "property"
            )
            if name is None:
                continue
            value = node.child_by_field_name("value")
            written = "this." + text(name, content)
            if value is not None:
                yield written, classify(value)
            else:
                typed = type_kind(node, content, modules, classes)
                if typed is not None:
                    yield written, typed
        elif kind in _PARAMETERS:
            pattern = node.child_by_field_name("pattern")
            if pattern is None or pattern.type != "identifier":
                continue
            typed = type_kind(node, content, modules, classes)
            if node.child_by_field_name("value") is not None and typed is None:
                continue
            written = text(pattern, content)
            property_ = any(
                c.type in ("accessibility_modifier", "readonly") for c in node.children
            )
            if typed is not None or property_:
                yield written, typed
            if property_:
                yield "this." + written, typed
        elif kind == "method_definition" and any(
            c.type == "get" for c in node.children
        ):
            typed = type_kind(node, content, modules, classes)
            if typed is not None:
                yield "this." + text(node.child_by_field_name("name"), content), typed


def bound_kinds(root, content, modules, classify, classes=None):
    """{name: kind} for the names (``x`` or ``this.x``) that every
    declaration and assignment in the file gives the same kind;
    ``classify(value)`` gives a value's kind or None, and ``classes``
    ({(module, class): kind}, by default the STOMP clients) the kind of a
    declared type."""
    found = {}
    for written, kind in _declarations(root, content, modules, classify, classes):
        found.setdefault(written, set()).add(kind)
    return {
        name: next(iter(kinds))
        for name, kinds in found.items()
        if len(kinds) == 1 and None not in kinds
    }


def stomp_names(root, content, modules):
    """{name: STOMP client kind} of a file."""
    return bound_kinds(
        root, content, modules, lambda value: value_kind(value, content, modules)
    )
