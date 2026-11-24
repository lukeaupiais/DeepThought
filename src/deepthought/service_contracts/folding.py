"""Fold Python, JavaScript/TypeScript and Go string expressions.

Folded: string literals, ``+`` concatenation, template strings and f-strings
(each substitution is unknown text), Python ``str.format`` and ``%``
formatting (each field is unknown text), and names bound exactly once in
their scope to a foldable value before the use: a function's locals, else
the module's top-level names (Python assignments, JavaScript ``const``/
``let``/``var``, Go ``var``/``const``/``:=``), and Python ``self.NAME`` for a
class attribute assigned once in the class body and never through an
attribute assignment in the class. A name bound more than once in
that scope, or assigned anywhere else, is unknown text. A name no scope
binds (an import, or a global a bundler defines) takes the value
``externals`` gives it. JavaScript/TypeScript ``this.NAME`` is the
initializer of the instance field NAME of the enclosing class, or of a base
class declared in the same file, when no class of the file assigns
``this.NAME``, no constructor parameter property is named NAME and no class
of the file that extends the enclosing class declares NAME again (a
subclass in another file is not seen). Everything else is unknown text
(PLACEHOLDER).
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
# Functions that bind their own ``this``.
_THIS_FUNCTIONS = (
    "function_declaration",
    "function_expression",
    "function",
    "generator_function_declaration",
    "generator_function",
)
_JS_CLASSES = ("class_declaration", "class", "abstract_class_declaration")
_JS_FIELDS = ("public_field_definition", "field_definition")
_MAX_DEPTH = 8
_MISSING = object()


class Fields(dict):
    """The folded properties of an object literal passed as an argument,
    {key: text}; only literals without spreads or computed keys."""


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
    def __init__(self, language, root, content, externals=None):
        self.language = language
        self.content = content
        self.root = root
        self.externals = externals or {}
        self._bindings = {}
        self._active = set()
        # JavaScript/TypeScript parameters: {function id: {index: value}}
        # gives their values; a parameter without one is recorded in
        # ``parameters_seen`` as its function node.
        self.substitutions = {}
        self.parameters_seen = {}

    def with_arguments(self, function, values):
        """A folder of the same file in which the parameters of ``function``
        take ``values`` ({index: text})."""
        folder = Folder(self.language, self.root, self.content, self.externals)
        folder._bindings = self._bindings
        folder.substitutions = {**self.substitutions, function.id: values}
        return folder

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
                if (
                    node.parent is not None
                    and node.parent.parent is not None
                    and node.parent.parent.type == "ambient_declaration"
                ):
                    return []  # ``declare const X`` gives no value
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

    def parameter(self, node):
        """(function, index) when the JavaScript/TypeScript identifier
        ``node`` names a parameter of its function that nothing in the
        function assigns again, else None."""
        if self.language not in ("javascript", "typescript"):
            return None
        name = text(node, self.content)
        scope = self._scope_of(node)
        if scope is self.root:
            return None
        bindings = self._collect(scope).get(name)
        if not bindings or len(bindings) != 1:
            return None
        if bindings[0][0].type != "formal_parameters":
            return None
        positional = [p for p in bindings[0][0].named_children if p.type != "comment"]
        for index, parameter in enumerate(positional):
            target = parameter.child_by_field_name("pattern") or parameter
            if target.type == "assignment_pattern":
                target = target.child_by_field_name("left")
            if (
                target is not None
                and target.type == "identifier"
                and (text(target, self.content) == name)
            ):
                return scope, index
        return None

    def _default(self, function, index):
        """The default value node of parameter ``index`` of ``function``."""
        parameters = function.child_by_field_name("parameters")
        if parameters is None:
            return None
        positional = [p for p in parameters.named_children if p.type != "comment"]
        if index >= len(positional):
            return None
        parameter = positional[index]
        pattern = parameter.child_by_field_name("pattern") or parameter
        if pattern.type == "assignment_pattern":
            return pattern.child_by_field_name("right")
        return parameter.child_by_field_name("value")

    def _passed(self, node, depth):
        """The value a call passes to the parameter ``node`` names: text,
        ``Fields`` for an object literal, None when not known."""
        parameter = self.parameter(node)
        if parameter is None:
            return None
        function, index = parameter
        if function.id not in self.substitutions:
            self.parameters_seen[function.id] = function
            return None
        passed = self.substitutions[function.id]
        if index in passed:
            return passed[index]
        # An argument the call leaves out takes the default.
        default = self._default(function, index)
        return None if default is None else self.argument(default, depth + 1)

    def argument(self, node, depth=0):
        """An argument's value: ``Fields`` for a JavaScript/TypeScript
        object literal of fixed keys, else its text."""
        if node is not None and node.type == "object":
            fields = Fields()
            for pair in node.named_children:
                if pair.type == "comment":
                    continue
                if pair.type == "shorthand_property_identifier":
                    fields[text(pair, self.content)] = self.evaluate(pair, depth)
                    continue
                key = (
                    pair.child_by_field_name("key" if pair.type == "pair" else "name")
                    if pair.type in ("pair", "method_definition")
                    else None
                )
                if key is None or key.type not in (
                    "property_identifier",
                    "string",
                    "number",
                ):
                    return PLACEHOLDER
                name = text(key, self.content)
                if key.type == "string":
                    name = name[1:-1]
                fields[name] = self.evaluate(pair.child_by_field_name("value"), depth)
                if pair.type == "method_definition":
                    fields[name] = PLACEHOLDER
            return fields
        return self.evaluate(node, depth)

    def _field(self, node, depth):
        """``param.key`` of a parameter given an object literal: its text,
        ``_MISSING`` when the literal has no such key, None when unknown."""
        if node.type != "member_expression":
            return None
        owner = node.child_by_field_name("object")
        key = node.child_by_field_name("property")
        if owner.type != "identifier" or key.type != "property_identifier":
            return None
        if self.binding(owner) is not None:
            return None
        fields = self._passed(owner, depth)
        if not isinstance(fields, Fields):
            return None
        return fields.get(text(key, self.content), _MISSING)

    def _name(self, node, depth):
        found = self.binding(node)
        if found is None:
            name = text(node, self.content)
            if name in self.externals and not self._bound(node):
                return self.externals[name]
            value = self._passed(node, depth)
            return PLACEHOLDER if value is None or isinstance(value, Fields) else value
        return self._fold(found[0].id, found[1], depth)

    def _bound(self, node):
        """Some scope around ``node`` binds its name."""
        name = text(node, self.content)
        scope = self._scope_of(node)
        while True:
            if self._collect(scope).get(name):
                return True
            if scope is self.root:
                return False
            scope = self._scope_of(scope)

    # -- JavaScript/TypeScript instance fields ------------------------------

    def _top_classes(self):
        """name -> class nodes declared at the top level of the file."""
        if not hasattr(self, "_classes"):
            self._classes = {}
            for node in self.root.named_children:
                if node.type == "export_statement":
                    node = node.child_by_field_name("declaration") or node
                if node.type in _JS_CLASSES:
                    name = node.child_by_field_name("name")
                    if name is not None:
                        self._classes.setdefault(text(name, self.content), []).append(
                            node
                        )
        return self._classes

    def _base_class(self, cls):
        """The class of this file that ``cls`` extends, or None."""
        heritage = next(
            (c for c in cls.named_children if c.type == "class_heritage"), None
        )
        if heritage is None:
            return None
        clause = next(
            (c for c in heritage.named_children if c.type == "extends_clause"),
            heritage,
        )
        base = clause.child_by_field_name("value") or next(
            iter(clause.named_children), None
        )
        if base is None or base.type != "identifier":
            return None
        found = self._top_classes().get(text(base, self.content), [])
        return found[0] if len(found) == 1 else None

    def _fields(self, cls, name):
        """Instance field definitions of ``name`` in a class body, and the
        constructor parameter property named ``name`` (or None)."""
        body = cls.child_by_field_name("body")
        fields, parameter = [], None
        for member in body.named_children if body is not None else ():
            if member.type in _JS_FIELDS:
                key = member.child_by_field_name("name") or member.child_by_field_name(
                    "property"
                )
                static = any(c.type == "static" for c in member.children)
                if key is not None and text(key, self.content) == name and not static:
                    fields.append(member)
            elif (
                member.type == "method_definition"
                and text(member.child_by_field_name("name"), self.content)
                == "constructor"
            ):
                parameters = member.child_by_field_name("parameters")
                for p in parameters.named_children if parameters is not None else ():
                    pattern = p.child_by_field_name("pattern")
                    if (
                        pattern is not None
                        and text(pattern, self.content) == name
                        and any(
                            c.type in ("accessibility_modifier", "readonly")
                            for c in p.children
                        )
                    ):
                        parameter = p
        return fields, parameter

    def _this_assigned(self, name):
        """Some code of the file assigns ``this.NAME``."""
        if not hasattr(self, "_this_targets"):
            self._this_targets = set()
            stack = [self.root]
            while stack:
                node = stack.pop()
                target = None
                if node.type in (
                    "assignment_expression",
                    "augmented_assignment_expression",
                ):
                    target = node.child_by_field_name("left")
                elif node.type == "update_expression":
                    target = node.child_by_field_name("argument") or next(
                        iter(node.named_children), None
                    )
                if (
                    target is not None
                    and target.type == "member_expression"
                    and target.child_by_field_name("object").type == "this"
                ):
                    self._this_targets.add(
                        text(target.child_by_field_name("property"), self.content)
                    )
                stack.extend(node.named_children)
        return name in self._this_targets

    def _redeclared_below(self, cls, name):
        """A class of this file extending ``cls`` (at any depth) declares
        ``name``."""
        classes = [c for found in self._top_classes().values() for c in found]
        below, frontier = set(), {cls.id}
        while frontier:
            frontier = {
                c.id
                for c in classes
                if c.id not in below
                and (base := self._base_class(c)) is not None
                and base.id in frontier
            }
            below |= frontier
        for c in classes:
            if c.id in below:
                fields, parameter = self._fields(c, name)
                if fields or parameter is not None:
                    return True
        return False

    def enclosing_class(self, node):
        """The class whose instance ``this`` is at ``node``, or None."""
        current = node.parent
        while current is not None and current.type != "class_body":
            if current.type in _THIS_FUNCTIONS or current.type == "class_static_block":
                return None
            if current.type in ("method_definition", *_JS_FIELDS) and any(
                c.type == "static" for c in current.children
            ):
                return None
            current = current.parent
        return None if current is None else current.parent

    def instance_member(self, node):
        """The field definition or constructor parameter property that
        declares ``this.NAME`` (``node`` is the member expression), or None:
        declared once in the enclosing class or its same-file bases, never
        assigned through ``this.NAME`` and not declared again by a same-file
        subclass."""
        name = text(node.child_by_field_name("property"), self.content)
        cls = self.enclosing_class(node)
        if cls is None or self._this_assigned(name):
            return None
        if self._redeclared_below(cls, name):
            return None
        while cls is not None:
            fields, parameter = self._fields(cls, name)
            if len(fields) + (parameter is not None) > 1:
                return None
            if fields:
                return fields[0]
            if parameter is not None:
                return parameter
            cls = self._base_class(cls)
        return None

    def instance_field(self, node):
        """The field definition of ``this.NAME`` and its initializer, or
        None (also for a constructor parameter property)."""
        member = self.instance_member(node)
        if member is None or member.type not in _JS_FIELDS:
            return None
        return member, member.child_by_field_name("value")

    def _this_field(self, node, depth):
        found = self.instance_field(node)
        if found is None or found[1] is None:
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
        if kind in ("identifier", "shorthand_property_identifier"):
            return self._name(node, depth)
        # A conditional whose branches have the same value has that value,
        # whatever the condition (``prod ? "//host:1" : "//host:1"``).
        branches = None
        if kind == "ternary_expression":
            branches = (
                node.child_by_field_name("consequence"),
                node.child_by_field_name("alternative"),
            )
        elif kind == "conditional_expression" and self.language == "python":
            values = [c for c in node.named_children if c.type != "comment"]
            if len(values) == 3:
                branches = (values[0], values[2])
        if branches is not None:
            first, second = (self.evaluate(b, depth) for b in branches)
            return (
                first if first == second and PLACEHOLDER not in first else (PLACEHOLDER)
            )
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
                if operator is not None and operator.type in ("??", "||"):
                    # options.key ?? fallback, with options an object literal.
                    left = self._field(node.child_by_field_name("left"), depth)
                    if left is _MISSING or (operator.type == "||" and left == ""):
                        return self.evaluate(node.child_by_field_name("right"), depth)
                    return PLACEHOLDER if left is None else left
            if kind == "member_expression":
                value = self._field(node, depth)
                if value is not None:
                    return PLACEHOLDER if value is _MISSING else value
            if (
                kind == "member_expression"
                and node.child_by_field_name("object").type == "this"
                and node.child_by_field_name("property").type == "property_identifier"
            ):
                return self._this_field(node, depth)
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
