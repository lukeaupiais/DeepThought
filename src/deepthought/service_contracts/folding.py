"""Fold Python, JavaScript/TypeScript and Go string expressions.

Folded: string literals, ``+`` concatenation, template strings and f-strings
(each substitution is unknown text), Python ``str.format`` and ``%``
formatting (each field is unknown text), and names bound exactly once in
their scope to a foldable value before the use: a function's locals, else
the module's top-level names (Python assignments, JavaScript ``const``/
``let``/``var``, Go ``var``/``const``/``:=``), and Python ``self.NAME`` for a
class attribute assigned once in the class body and never through an
attribute assignment in the class. A name bound more than once in
that scope, or assigned anywhere else, is unknown text. Everything else is
unknown text (PLACEHOLDER).
"""

import re

from .http import PLACEHOLDER

_FUNCTIONS = {
    "python": ("function_definition", "lambda"),
    "javascript": (
        "function_declaration",
        "function_expression",
        "arrow_function",
        "method_definition",
        "generator_function_declaration",
    ),
    "go": ("function_declaration", "method_declaration", "func_literal"),
}
_FUNCTIONS["typescript"] = _FUNCTIONS["javascript"]
_MAX_DEPTH = 8


def text(node, content):
    return content[node.start_byte : node.end_byte].decode("utf-8")


def _python_string(node, content):
    start = next((c for c in node.children if c.type == "string_start"), None)
    prefix = text(start, content).lower() if start is not None else ""
    if "b" in prefix:
        return None
    parts = []
    for child in node.children:
        if child.type == "string_content":
            parts.append(text(child, content))
        elif child.type == "escape_sequence":
            value = text(child, content)
            parts.append(value if "r" in prefix else _unescape(value))
        elif child.type == "interpolation":
            parts.append(PLACEHOLDER)
    return "".join(parts)


def _unescape(value):
    simple = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"'}
    if len(value) == 2 and value[1] in simple:
        return simple[value[1]]
    return PLACEHOLDER


class Folder:
    def __init__(self, language, root, content):
        self.language = language
        self.content = content
        self.root = root
        self._bindings = {}
        self._active = set()

    # -- bindings -----------------------------------------------------------

    def _scope_of(self, node):
        current = node.parent
        while current is not None:
            if current.type in _FUNCTIONS[self.language]:
                return current
            current = current.parent
        return self.root

    def _collect(self, scope):
        """name -> list of (binding node, value node or None) in one scope."""
        if scope.id in self._bindings:
            return self._bindings[scope.id]
        found = {}
        functions = _FUNCTIONS[self.language]
        stack = list(scope.named_children)
        while stack:
            node = stack.pop()
            if node.type in functions:
                if self.language == "python" and node.type == "function_definition":
                    name = node.child_by_field_name("name")
                    found.setdefault(text(name, self.content), []).append((node, None))
                continue  # inner scopes bind their own names
            for name, value in self._binds(node):
                found.setdefault(name, []).append((node, value))
            stack.extend(node.named_children)
        self._bindings[scope.id] = found
        return found

    def _binds(self, node):
        """(name, value node or None) pairs that a node binds."""
        content, kind = self.content, node.type
        if self.language == "python":
            if kind in ("assignment", "augmented_assignment"):
                left = node.child_by_field_name("left")
                if left.type == "identifier":
                    value = (
                        node.child_by_field_name("right")
                        if kind == "assignment"
                        else None
                    )
                    return [(text(left, content), value)]
                return [(text(n, content), None) for n in _identifiers(left)]
            if kind in (
                "for_statement",
                "with_item",
                "parameters",
                "lambda_parameters",
            ):
                target = node.child_by_field_name("left") or node
                return [(text(n, content), None) for n in _identifiers(target)]
            return []
        if self.language in ("javascript", "typescript"):
            if kind == "variable_declarator":
                name = node.child_by_field_name("name")
                if name.type == "identifier":
                    return [(text(name, content), node.child_by_field_name("value"))]
                return [(text(n, content), None) for n in _identifiers(name)]
            if kind in ("assignment_expression", "augmented_assignment_expression"):
                left = node.child_by_field_name("left")
                if left.type == "identifier":
                    return [(text(left, content), None)]
            if kind in ("formal_parameters", "for_in_statement", "catch_clause"):
                target = node.child_by_field_name("left") or node
                if kind == "catch_clause":
                    target = node.child_by_field_name("parameter")
                return [(text(n, content), None) for n in _identifiers(target)]
            if kind == "update_expression":
                return [
                    (text(n, content), None)
                    for n in node.named_children
                    if n.type == "identifier"
                ]
            return []
        if self.language == "go":
            if kind in ("var_spec", "const_spec"):
                names = node.children_by_field_name("name")
                value = node.child_by_field_name("value")
                values = value.named_children if value is not None else []
                return [
                    (
                        text(n, content),
                        values[i] if len(values) == len(names) else None,
                    )
                    for i, n in enumerate(names)
                ]
            if kind in ("short_var_declaration", "assignment_statement"):
                left = node.child_by_field_name("left").named_children
                right = node.child_by_field_name("right").named_children
                operator = node.child_by_field_name("operator")
                plain = kind == "short_var_declaration" or (
                    operator is not None and text(operator, content) == "="
                )
                return [
                    (
                        text(n, content),
                        right[i] if plain and len(right) == len(left) else None,
                    )
                    for i, n in enumerate(left)
                    if n.type == "identifier"
                ]
            if kind in ("parameter_list", "range_clause"):
                target = node.child_by_field_name("left") or node
                return [(text(n, content), None) for n in _identifiers(target)]
            if kind in ("inc_statement", "dec_statement"):
                return [
                    (text(n, content), None)
                    for n in node.named_children
                    if n.type == "identifier"
                ]
            return []
        return []

    def binding(self, node):
        """(binding node, value node) of the identifier ``node`` when its name
        is bound exactly once, to a value, in the nearest scope binding it,
        before this use unless at module level, and not assigned elsewhere."""
        name = text(node, self.content)
        scope = self._scope_of(node)
        while True:
            bindings = self._collect(scope).get(name)
            if bindings:
                if len(bindings) != 1:
                    return None
                binding, value = bindings[0]
                if value is None or (
                    scope is not self.root and binding.start_byte > node.start_byte
                ):
                    return None
                if self._assigned_elsewhere(name, scope):
                    return None
                return binding, value
            if scope is self.root:
                return None
            scope = self._scope_of(scope)

    def _name(self, node, depth):
        found = self.binding(node)
        if found is None:
            return PLACEHOLDER
        return self._fold(found[0].id, found[1], depth)

    def _class_attribute(self, node, depth):
        """``self.NAME``/``cls.NAME`` inside a class whose body assigns NAME
        once, when nothing in the class assigns ``<anything>.NAME``."""
        owner = node.child_by_field_name("object")
        attribute = text(node.child_by_field_name("attribute"), self.content)
        if owner.type != "identifier" or text(owner, self.content) not in (
            "self",
            "cls",
        ):
            return PLACEHOLDER
        current = node.parent
        while current is not None and current.type != "class_definition":
            current = current.parent
        if current is None:
            return PLACEHOLDER
        body = current.child_by_field_name("body")
        values = [
            child
            for statement in body.named_children
            if statement.type == "expression_statement"
            for child in statement.named_children
            if child.type == "assignment"
            and child.child_by_field_name("left").type == "identifier"
            and text(child.child_by_field_name("left"), self.content) == attribute
        ]
        if len(values) != 1:
            return PLACEHOLDER
        stack = [body]
        while stack:
            current = stack.pop()
            if current.type in ("assignment", "augmented_assignment"):
                left = current.child_by_field_name("left")
                targets = [left, *left.named_children]
                if any(
                    t.type == "attribute"
                    and text(t.child_by_field_name("attribute"), self.content)
                    == attribute
                    for t in targets
                ):
                    return PLACEHOLDER
            stack.extend(current.named_children)
        return self._fold(values[0].id, values[0].child_by_field_name("right"), depth)

    def _assigned_elsewhere(self, name, scope):
        """A module name rebound inside a function (``global``) is not fixed."""
        if scope is not self.root:
            return False
        stack = [self.root]
        while stack:
            node = stack.pop()
            if node.type in _FUNCTIONS[self.language]:
                inner = self._collect(node).get(name)
                if inner and self.language == "python":
                    # A Python function assigning the name has its own local
                    # unless it declares it global.
                    if _declares_global(node, name, self.content):
                        return True
                elif self.language != "python" and self._assigns(node, name):
                    return True
            stack.extend(node.named_children)
        return False

    def _assigns(self, function, name):
        stack = [function]
        while stack:
            node = stack.pop()
            if node.type in (
                "assignment_expression",
                "augmented_assignment_expression",
                "assignment_statement",
            ):
                left = node.child_by_field_name("left")
                targets = (
                    left.named_children if left.type == "expression_list" else [left]
                )
                if any(
                    t.type == "identifier" and text(t, self.content) == name
                    for t in targets
                ):
                    # Only when the function does not declare its own name.
                    if not any(
                        b
                        for b in self._collect(function).get(name, [])
                        if b[0].type
                        not in (
                            "assignment_expression",
                            "assignment_statement",
                            "augmented_assignment_expression",
                        )
                    ):
                        return True
            stack.extend(node.named_children)
        return False

    def _fold(self, key, node, depth):
        if key in self._active or depth >= _MAX_DEPTH:
            return PLACEHOLDER
        self._active.add(key)
        try:
            return self.evaluate(node, depth + 1)
        finally:
            self._active.discard(key)

    # -- expressions --------------------------------------------------------

    def evaluate(self, node, depth=0):
        """The expression's text with PLACEHOLDER for what is not fixed."""
        if node is None:
            return PLACEHOLDER
        content, kind = self.content, node.type
        if kind == "parenthesized_expression":
            return (
                self.evaluate(node.named_children[0], depth)
                if node.named_children
                else PLACEHOLDER
            )
        if kind == "identifier":
            return self._name(node, depth)
        if self.language == "python":
            if kind == "string":
                value = _python_string(node, content)
                return PLACEHOLDER if value is None else value
            if kind == "concatenated_string":
                return "".join(self.evaluate(c, depth) for c in node.named_children)
            if kind == "binary_operator":
                operator = text(node.child_by_field_name("operator"), content)
                left = self.evaluate(node.child_by_field_name("left"), depth)
                if operator == "+":
                    return left + self.evaluate(
                        node.child_by_field_name("right"), depth
                    )
                if operator == "%":
                    return re.sub(
                        r"%(?:\([^)]*\))?[-#0 +]*\d*(?:\.\d+)?[sdifr]",
                        PLACEHOLDER,
                        left,
                    )
                return PLACEHOLDER
            if kind == "attribute":
                return self._class_attribute(node, depth)
            if kind == "call":
                function = node.child_by_field_name("function")
                if (
                    function.type == "attribute"
                    and text(function.child_by_field_name("attribute"), content)
                    == "format"
                ):
                    template = self.evaluate(
                        function.child_by_field_name("object"), depth
                    )
                    return re.sub(r"\{[^{}]*\}", PLACEHOLDER, template)
            return PLACEHOLDER
        if self.language in ("javascript", "typescript"):
            if kind == "string":
                parts = []
                for child in node.named_children:
                    if child.type == "string_fragment":
                        parts.append(text(child, content))
                    elif child.type == "escape_sequence":
                        parts.append(_unescape(text(child, content)))
                return "".join(parts)
            if kind == "template_string":
                parts = []
                for child in node.named_children:
                    if child.type == "string_fragment":
                        parts.append(text(child, content))
                    elif child.type == "template_substitution":
                        inner = child.named_children
                        parts.append(
                            self.evaluate(inner[0], depth) if inner else PLACEHOLDER
                        )
                    elif child.type == "escape_sequence":
                        parts.append(_unescape(text(child, content)))
                return "".join(parts)
            if kind == "binary_expression":
                operator = node.child_by_field_name("operator")
                if operator is not None and operator.type == "+":
                    return self.evaluate(
                        node.child_by_field_name("left"), depth
                    ) + self.evaluate(node.child_by_field_name("right"), depth)
            return PLACEHOLDER
        if self.language == "go":
            if kind == "interpreted_string_literal":
                parts = []
                for child in node.named_children:
                    if child.type == "interpreted_string_literal_content":
                        parts.append(text(child, content))
                    elif child.type == "escape_sequence":
                        parts.append(_unescape(text(child, content)))
                return "".join(parts)
            if kind == "raw_string_literal":
                return "".join(
                    text(c, content)
                    for c in node.named_children
                    if c.type == "raw_string_literal_content"
                )
            if kind == "binary_expression":
                operator = node.child_by_field_name("operator")
                if operator is not None and operator.type == "+":
                    return self.evaluate(
                        node.child_by_field_name("left"), depth
                    ) + self.evaluate(node.child_by_field_name("right"), depth)
            if kind == "call_expression":
                function = node.child_by_field_name("function")
                if text(function, content) == "fmt.Sprintf":
                    arguments = node.child_by_field_name("arguments").named_children
                    if arguments:
                        template = self.evaluate(arguments[0], depth)
                        return re.sub(
                            r"%[-+# 0]*\d*(?:\.\d+)?[vsdqx]", PLACEHOLDER, template
                        )
            return PLACEHOLDER
        return PLACEHOLDER


def _identifiers(node):
    if node is None:
        return []
    if node.type == "identifier":
        return [node]
    found = []
    for child in node.named_children:
        found.extend(_identifiers(child))
    return found


def _declares_global(function, name, content):
    stack = [function]
    while stack:
        node = stack.pop()
        if node.type in ("global_statement", "nonlocal_statement") and any(
            text(c, content) == name for c in node.named_children
        ):
            return True
        stack.extend(node.named_children)
    return False
