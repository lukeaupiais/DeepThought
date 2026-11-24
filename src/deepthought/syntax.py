# Definition mappings and structural name-field extraction adapted from
# DeepThought's Tree-sitter analyzer (Copyright (c) 2025 DeepThought, MIT).
# See licenses/deepthought-original-MIT.txt. Graph/ML/database coupling removed.
"""Explicit selected grammars and structural definitions; no semantic guessing."""

import importlib
from hashlib import sha256
import json
import re

from .models import CallSite, Definition, ImplBlock, Import, PackageClause, Supertype

LANGUAGES = {
    "python": ((".py",), "tree_sitter_python", "language"),
    "typescript": ((".ts", ".tsx"), "tree_sitter_typescript", "language_typescript"),
    "javascript": (
        (".js", ".jsx", ".mjs", ".cjs"),
        "tree_sitter_javascript",
        "language",
    ),
    "go": ((".go",), "tree_sitter_go", "language"),
    "java": ((".java",), "tree_sitter_java", "language"),
    "rust": ((".rs",), "tree_sitter_rust", "language"),
    "c": ((".c", ".h"), "tree_sitter_c", "language"),
    "cpp": (
        (
            ".cc",
            ".cpp",
            ".cxx",
            ".c++",
            ".hh",
            ".hpp",
            ".hxx",
            ".h++",
            ".ipp",
            ".inl",
            ".h",
        ),
        "tree_sitter_cpp",
        "language",
    ),
    "ruby": ((".rb",), "tree_sitter_ruby", "language"),
    "php": ((".php",), "tree_sitter_php", "language_php"),
}
# Languages analyzed when none are selected. JavaScript, Go, Java, Rust, C, C++,
# Ruby and PHP are opt-in so that existing Python/TypeScript analyses keep their
# output and do not need their grammars; select them with languages=("go", ...)
# or --language.
DEFAULT_LANGUAGES = ("python", "typescript")
KINDS = {
    "function_definition": "Function",
    "function_declaration": "Function",
    "function_signature": "Function",
    "generator_function_declaration": "Function",
    "method_definition": "Method",
    "method_signature": "Method",
    "abstract_method_signature": "Method",
    "class_definition": "Class",
    "class_declaration": "Class",
    "abstract_class_declaration": "Class",
    "interface_declaration": "Interface",
    "enum_declaration": "Enum",
    "type_alias_declaration": "TypeAlias",
    "internal_module": "Namespace",
    "module": "Namespace",
    # Go: methods are named by receiver type, type specs by their type.
    "method_declaration": "Method",
    "method_elem": "Method",
    "type_spec": "Type",
    "type_alias": "TypeAlias",
    # Java: class, interface, enum and method declarations share the names
    # above; annotation elements are methods, as javac declares them.
    "record_declaration": "Record",
    "annotation_type_declaration": "Annotation",
    "annotation_type_element_declaration": "Method",
    "constructor_declaration": "Constructor",
    "compact_constructor_declaration": "Constructor",
    # Rust: functions in an impl or trait are methods (see _rust_kind); an
    # impl names no declaration but scopes its items by the implementing type.
    "function_item": "Function",
    "function_signature_item": "Function",
    "struct_item": "Struct",
    "enum_item": "Enum",
    "union_item": "Union",
    "trait_item": "Trait",
    "type_item": "TypeAlias",
    "associated_type": "TypeAlias",
    "mod_item": "Module",
    "macro_definition": "Macro",
}
_GO_TYPE_KINDS = {"struct_type": "Struct", "interface_type": "Interface"}


def _go_receiver(node, content):
    """Receiver type name of a Go method declaration, without pointer or type args."""
    receiver = node.child_by_field_name("receiver")
    parameter = (
        next(
            (c for c in receiver.named_children if c.type == "parameter_declaration"),
            None,
        )
        if receiver is not None
        else None
    )
    kind = parameter.child_by_field_name("type") if parameter is not None else None
    while kind is not None and kind.type in {"pointer_type", "generic_type"}:
        kind = (
            next((c for c in kind.named_children if c.type != "type_arguments"), None)
            if kind.type == "pointer_type"
            else kind.child_by_field_name("type")
        )
    if kind is None or kind.type != "type_identifier":
        return None
    return content[kind.start_byte : kind.end_byte].decode("utf-8")


def _rust_impl_type(node, content):
    """Name of the type an impl block implements for: the last path segment,
    without references, pointers or type arguments; None for other types."""
    kind = node.child_by_field_name("type")
    while kind is not None and kind.type in {
        "reference_type",
        "pointer_type",
        "generic_type",
    }:
        kind = kind.child_by_field_name("type")
    if kind is not None and kind.type == "scoped_type_identifier":
        kind = kind.child_by_field_name("name")
    if kind is None or kind.type not in {"type_identifier", "primitive_type"}:
        return None
    return _text(kind, content)


def _rust_kind(node, kind):
    """A Rust function is a method in an impl or trait body."""
    body = node.parent
    if (
        node.type in {"function_item", "function_signature_item"}
        and body is not None
        and body.parent is not None
        and body.parent.type in {"impl_item", "trait_item"}
    ):
        return "Method"
    return kind


def identifier(
    repository_id: str, file: str, symbol: str, kind: str, ordinal: int | str
) -> str:
    key = json.dumps(
        [repository_id, file, symbol, kind, ordinal], separators=(",", ":")
    )
    return sha256(key.encode()).hexdigest()


def make_parser(language: str, suffix: str):
    from tree_sitter import Language, Parser

    _, module, factory = LANGUAGES[language]
    if suffix == ".tsx":
        factory = "language_tsx"
    grammar = importlib.import_module(module)
    return Parser(Language(getattr(grammar, factory)()))


def extract(
    tree,
    content: bytes,
    repository_id: str,
    file: str,
    *,
    nodes=None,
    expander: "RustExpander | None" = None,
) -> list[Definition]:
    """Named declarations in source order; ``nodes`` receives id -> (node,
    name). An ``expander`` reads Rust items written in pass-through macro
    invocations."""
    children = expander.children if expander else (lambda node: node.named_children)
    definitions = []
    ordinals = {}
    stack = [(tree.root_node, (), False)]
    while stack:
        node, scope, in_class = stack.pop()
        kind = KINDS.get(node.type)
        if node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            name = node.child_by_field_name("name")
            if (
                value is not None
                and value.type == "arrow_function"
                and name is not None
                and name.type == "identifier"
            ):
                kind = "Function"
        name_node = node.child_by_field_name("name") if kind else None
        if node.type == "type_spec":
            kind = _GO_TYPE_KINDS.get(
                getattr(node.child_by_field_name("type"), "type", None), "Type"
            )
        if node.type == "impl_item":
            implemented = _rust_impl_type(node, content)
            if implemented is not None:
                scope = (*scope, implemented)
        kind = _rust_kind(node, kind)
        if name_node is not None:
            name = content[name_node.start_byte : name_node.end_byte].decode("utf-8")
            if name.startswith("r#"):
                name = name[2:]  # A Rust raw identifier names the bare word.
            symbol = ".".join((*scope, name))
            if node.type == "method_declaration" and node.child_by_field_name(
                "receiver"
            ):
                # A Go method belongs to its receiver type, not to a scope.
                receiver = _go_receiver(node, content)
                symbol = f"{receiver}.{name}" if receiver else name
            if node.type == "function_definition" and in_class:
                kind = "Method"
            ordinal = ordinals.get((symbol, kind), 0)
            ordinals[(symbol, kind)] = ordinal + 1
            parameters = node.child_by_field_name("parameters")
            arity = {}
            if parameters is not None and parameters.type == "formal_parameters":
                # Java; a receiver parameter (``Outer this``) is not counted.
                declared = [
                    c
                    for c in parameters.named_children
                    if c.type in {"formal_parameter", "spread_parameter"}
                ]
                arity = {
                    "parameters": len(declared),
                    "variadic": bool(declared)
                    and declared[-1].type == "spread_parameter",
                }
            elif node.type == "annotation_type_element_declaration":
                arity = {"parameters": 0}
            elif node.type == "macro_definition":
                arity = {"macro_export": _rust_macro_export(node)}
            definition = Definition(
                id=identifier(repository_id, file, symbol, kind, ordinal),
                file=file,
                symbol=symbol,
                kind=kind,
                line=node.start_point.row + 1,
                column=node.start_point.column,
                end_line=node.end_point.row + 1,
                end_column=node.end_point.column,
                **arity,
            )
            definitions.append(definition)
            if nodes is not None:
                nodes[definition.id] = (node, name_node)
            scope = (*scope, name)
            in_class = kind == "Class"
        stack.extend((child, scope, in_class) for child in reversed(children(node)))
    return definitions


# C: declarators that wrap the declared name.
_C_WRAPPERS = {
    "pointer_declarator",
    "parenthesized_declarator",
    "attributed_declarator",
    "array_declarator",
    "function_declarator",
}
_C_SPECIFIERS = {
    "struct_specifier": "Struct",
    "union_specifier": "Union",
    "enum_specifier": "Enum",
}


def _c_prefixed_name(node):
    """The name of a function declared after a macro the grammar does not
    know (``API int f(...)``, ``API int (f)(...)``), from the shape the
    grammar gives it; None for any other function declarator.

    ``API T f(...)`` with ``T`` a type name parses as the declarator ``T``
    with ``f`` in an error node before the parameters. ``API int (f)(...)``
    parses as a function declarator applied to ``int(f)``, which C does not
    allow without parentheses: the name is the inner list's one type.
    """
    error = next((c for c in node.children if c.type == "ERROR"), None)
    if (
        error is not None
        and len(error.named_children) == 1
        and error.named_children[0].type == "identifier"
    ):
        return error.named_children[0]
    inner = node.child_by_field_name("declarator")
    parameters = inner.child_by_field_name("parameters") if inner else None
    entries = parameters.named_children if parameters is not None else []
    if (
        len(entries) == 1
        and entries[0].type == "parameter_declaration"
        and entries[0].child_by_field_name("declarator") is None
        and getattr(entries[0].child_by_field_name("type"), "type", None)
        == "type_identifier"
    ):
        return entries[0].child_by_field_name("type")
    return None


def _c_declared_name(declarator, kinds):
    """The identifier a C declarator declares and the declarator holding it
    (parentheses aside), when the identifier is of ``kinds``; else None."""
    node, holder = declarator, None
    while node is not None and node.type in _C_WRAPPERS:
        if node.type == "parenthesized_declarator":
            # `(name)` groups; the declarator around it holds the name.
            node = next(iter(node.named_children), None)
            continue
        if node.type == "function_declarator":
            recovered = _c_prefixed_name(node)
            if recovered is not None:
                return recovered, node
        holder, node = node, node.child_by_field_name("declarator")
    if node is None or node.type not in kinds:
        return None
    return node, holder


def extract_c(
    tree, content: bytes, repository_id: str, file: str, *, nodes=None
) -> list[Definition]:
    """C declarations at file scope, in source order, in every preprocessor
    branch: functions (definitions and prototypes), named structs, unions
    and enums that have a body, typedef names, and macros wherever they are
    defined. Names are not qualified: C has one file scope. A macro the
    grammar could not read (it contains an error node) is left out. ``nodes`` receives id -> (node, name).
    """
    found, ordinals = [], {}

    def add(node, name_node, kind):
        name = _text(name_node, content)
        ordinal = ordinals.get((name, kind), 0)
        ordinals[(name, kind)] = ordinal + 1
        definition = Definition(
            id=identifier(repository_id, file, name, kind, ordinal),
            file=file,
            symbol=name,
            kind=kind,
            line=node.start_point.row + 1,
            column=node.start_point.column,
            end_line=node.end_point.row + 1,
            end_column=node.end_point.column,
        )
        found.append(definition)
        if nodes is not None:
            nodes[definition.id] = (node, name_node)

    def macro(node):
        # A definition the grammar could not read (a comment in a continued
        # line) may take a later word for its name.
        if not node.has_error:
            add(node, node.child_by_field_name("name"), "Macro")

    def specifiers(node):
        """Structs, unions and enums with a body written in a type, at any
        depth of member declarations (C does not scope them)."""
        stack = [node]
        while stack:
            current = stack.pop()
            kind = _C_SPECIFIERS.get(current.type)
            if kind is not None:
                name = current.child_by_field_name("name")
                if name is not None and current.child_by_field_name("body"):
                    add(current, name, kind)
                body = current.child_by_field_name("body")
                if body is not None and kind != "Enum":
                    stack.extend(
                        member.child_by_field_name("type")
                        for member in reversed(body.named_children)
                        if member.type == "field_declaration"
                        and member.child_by_field_name("type") is not None
                    )

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in {"translation_unit", "linkage_specification", "ERROR"} or (
            node.type.startswith("preproc_")
            and node.type not in {"preproc_def", "preproc_function_def"}
        ):
            stack.extend(reversed(node.named_children))
            continue
        if node.type == "declaration_list":
            stack.extend(reversed(node.named_children))
            continue
        if node.type in {"preproc_def", "preproc_function_def"}:
            macro(node)
            continue
        kind = node.child_by_field_name("type")
        if kind is not None and node.type in {
            "function_definition",
            "declaration",
            "type_definition",
        }:
            specifiers(kind)
        if node.type == "function_definition":
            declared = _c_declared_name(
                node.child_by_field_name("declarator"), {"identifier"}
            )
            if declared is not None and getattr(declared[1], "type", None) == (
                "function_declarator"
            ):
                add(node, declared[0], "Function")
            elif node.child_by_field_name("body") is not None:
                # `namespace n {` in a C++ branch reads as a definition with
                # no parameters; its body holds file-scope items.
                stack.extend(reversed(node.child_by_field_name("body").named_children))
                continue
        elif (
            node.type in {"function_declarator", "pointer_declarator"}
            and node.parent is not None
            and node.parent.type == "ERROR"
        ):
            # A function's declarator standing alone in text the grammar
            # could not parse: its type and body are siblings.
            declared = _c_declared_name(node, {"identifier"})
            if declared is not None and getattr(declared[1], "type", None) == (
                "function_declarator"
            ):
                add(node, declared[0], "Function")
        elif node.type == "declaration":
            for declarator in node.children_by_field_name("declarator"):
                declared = _c_declared_name(declarator, {"identifier"})
                # A prototype; a pointer to a function is a variable.
                if (
                    declared is not None
                    and declared[1] is not None
                    and (declared[1].type == "function_declarator")
                ):
                    add(node, declared[0], "Function")
        elif node.type == "type_definition":
            for declarator in node.children_by_field_name("declarator"):
                declared = _c_declared_name(declarator, {"type_identifier"})
                if declared is not None:
                    add(node, declared[0], "TypeAlias")
        elif node.type in _C_SPECIFIERS:
            specifiers(node)
        # A macro defined inside an item (a function body, a struct's
        # members) is defined for the rest of the file.
        inner = list(reversed(node.named_children))
        while inner:
            current = inner.pop()
            if current.type in {"preproc_def", "preproc_function_def"}:
                macro(current)
            else:
                inner.extend(reversed(current.named_children))
    return found


# `class API Name {` and `class API Name : Base {`: C++ allows no word
# between a class key and the class's name, so the first word is a macro
# (an export or visibility attribute). Comments and literals are skipped.
_CPP_CLASS_MACRO = re.compile(
    rb"//[^\n]*|/\*.*?\*/|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'"
    rb"|\b(?:class|struct|union)\s+(?P<macro>[A-Za-z_]\w*)"
    rb"(?=\s+(?!final\b)[A-Za-z_]\w*\s*(?:final\s*)?[:{])",
    re.DOTALL,
)


def prepare_source(language: str, content: bytes) -> bytes:
    """The text the grammar parses. For C++, a macro between a class key
    and the class's name (``class API Name {``) is replaced with spaces, so
    offsets, lines and every other byte are kept; other languages are
    parsed as written."""
    if language != "cpp":
        return content

    def blank(match):
        # A comment or literal has no macro: its span is empty.
        start, end = match.span("macro")
        whole = match.group(0)
        offset = start - match.start()
        return whole[:offset] + b" " * (end - start) + whole[offset + end - start :]

    return _CPP_CLASS_MACRO.sub(blank, content)


# C++: declarators that wrap the declared name, beyond C's.
_CPP_WRAPPERS = _C_WRAPPERS | {"reference_declarator"}
_CPP_CLASSES = {
    "class_specifier": "Class",
    "struct_specifier": "Struct",
    "union_specifier": "Union",
    "enum_specifier": "Enum",
}
_CPP_NAMES = {
    "identifier",
    "field_identifier",
    "type_identifier",
    "namespace_identifier",
    "destructor_name",
    "operator_name",
    "operator_cast",
    "qualified_identifier",
    "template_function",
    "template_type",
}


def _cpp_name(node, content):
    """The scope written before a C++ name and the name's last part, as
    (scope parts, name text, name node): ``n::C<int>::C`` gives
    (["n", "C"], "C", node of the last ``C``). Operators are spelled as clang
    spells them (``operator==``, ``operator new``)."""
    scope = []
    while node.type == "qualified_identifier":
        written = node.child_by_field_name("scope")
        if written is not None:
            if written.type in {"template_type", "template_function"}:
                written = written.child_by_field_name("name")
            scope.append(_text(written, content))
        node = node.child_by_field_name("name")
        if node is None:
            return None
    if node.type in {"template_type", "template_function"}:
        node = node.child_by_field_name("name")
    if node is None:
        return None
    text = _text(node, content)
    if node.type == "destructor_name":
        text = "~" + text.lstrip("~").strip()
    elif node.type == "operator_name":
        text = re.sub(r"\s+", "", text)
        text = re.sub(r"^operator(?=[A-Za-z_])", "operator ", text)
    elif node.type == "operator_cast":
        # `operator const char*() const`: the type runs to the parameters.
        end, stack = node.end_byte, [node]
        while stack:
            current = stack.pop()
            if current.type == "parameter_list":
                end = min(end, current.start_byte)
            else:
                stack.extend(current.named_children)
        cast = content[node.start_byte : end].decode("utf-8")
        text = "operator " + re.sub(r"\s+", " ", cast.removeprefix("operator")).strip()
    return scope, text, node


def _cpp_function(declarator):
    """The name a C++ function declarator declares, when the declarator is a
    function's (not a pointer to one); else None."""
    node, holder = declarator, None
    while node is not None and node.type in _CPP_WRAPPERS:
        if node.type == "parenthesized_declarator":
            node = next(iter(node.named_children), None)
            continue
        if node.type == "function_declarator":
            recovered = _c_prefixed_name(node)
            if recovered is not None:
                return recovered
        error = next((c for c in node.named_children if c.type == "ERROR"), None)
        if error is not None and len(error.named_children) == 1:
            # `int *f() LOCKS(m)` and `int *x GUARDED_BY(m)`: what is
            # declared is in an error node and the macro reads as the
            # declarator (see _cpp_declarators).
            node = error.named_children[0]
            continue
        holder, node = (
            node,
            node.child_by_field_name("declarator")
            or next(
                # `T& f()` holds its declarator in no field.
                (c for c in node.named_children if c.type in _CPP_WRAPPERS),
                None,
            ),
        )
    if node is not None and node.type == "operator_cast":
        return node
    if (
        node is None
        or node.type not in _CPP_NAMES
        or getattr(holder, "type", None) != "function_declarator"
    ):
        return None
    return node


def _cpp_declarators(node):
    """The declarators of a declaration. ``void f() LOCKS(m);`` parses with
    the function's declarator in an error node and the macro as the
    declarator: when an error node holds one declarator, that is the one
    declared. ``API T f();`` puts the type ``T`` in the error node and keeps
    ``f``, so ``int x GUARDED_BY(m);``, which has the same shape, reads as a
    function ``GUARDED_BY``."""
    declarators = node.children_by_field_name("declarator")
    errors = [c for c in node.children if c.type == "ERROR"]
    if (
        len(declarators) == 1
        and len(errors) == 1
        and [c.type in _CPP_WRAPPERS for c in errors[0].named_children] == [True]
    ):
        return [errors[0].named_children[0]]
    return declarators


def extract_cpp(
    tree, content: bytes, repository_id: str, file: str, *, nodes=None
) -> list[Definition]:
    """C++ declarations outside function bodies, in source order, in every
    preprocessor branch: namespaces, classes, structs, unions and enums with
    a body, functions and methods (definitions and prototypes), type aliases
    (``typedef`` and ``using``), and macros wherever they are defined.

    Symbols join the enclosing namespaces and classes and the scope written
    before a name with ``.``. A function is a Method when it is declared in
    a class body or its name is written with a scope (``C::f``) whose last
    part is not a namespace the file opens; the written scope is not looked
    up further, so ``n::f`` in a file that never opens ``n`` is a Method. A friend function is declared in the namespace around
    its class. Anonymous namespaces and unnamed types add no scope.
    ``nodes`` receives id -> (node, name).
    """
    found, ordinals = [], {}

    def add(node, name_node, scope, name, kind):
        symbol = ".".join((*scope, name))
        ordinal = ordinals.get((symbol, kind), 0)
        ordinals[(symbol, kind)] = ordinal + 1
        definition = Definition(
            id=identifier(repository_id, file, symbol, kind, ordinal),
            file=file,
            symbol=symbol,
            kind=kind,
            line=node.start_point.row + 1,
            column=node.start_point.column,
            end_line=node.end_point.row + 1,
            end_column=node.end_point.column,
        )
        found.append(definition)
        if nodes is not None:
            nodes[definition.id] = (node, name_node)

    def macros(node):
        # A macro defined inside an item is defined for the rest of the file.
        inner = [node]
        while inner:
            current = inner.pop()
            if current.type in {"preproc_def", "preproc_function_def"}:
                if not current.has_error:
                    name = current.child_by_field_name("name")
                    add(current, name, (), _text(name, content), "Macro")
            else:
                inner.extend(reversed(current.named_children))

    def function(node, declarator, scope, in_class):
        name = _cpp_function(declarator)
        if name is None:
            return False
        parts = _cpp_name(name, content)
        if parts is None:
            return False
        written, text, name_node = parts
        method = in_class or (written and written[-1] not in namespaces)
        add(
            node,
            name_node,
            (*scope, *written),
            text,
            "Method" if method else "Function",
        )
        return True

    # Namespaces this file opens: `void n::f() {}` with `n` one of them is
    # a function, any other written scope is taken to be a class.
    namespaces, pending = set(), [tree.root_node]
    while pending:
        current = pending.pop()
        if current.type == "namespace_identifier" and (
            current.parent.type
            in {"namespace_definition", "nested_namespace_specifier"}
        ):
            namespaces.add(_text(current, content))
        pending.extend(current.named_children)

    # (node, scope, in a class body, scope of the innermost namespace)
    stack = [(tree.root_node, (), False, ())]
    while stack:
        node, scope, in_class, outer = stack.pop()
        kind = node.type
        if kind in {
            "translation_unit",
            "linkage_specification",
            "ERROR",
            "declaration_list",
            "template_declaration",
        } or (
            kind.startswith("preproc_")
            and kind not in {"preproc_def", "preproc_function_def"}
        ):
            stack.extend(
                (c, scope, in_class, outer) for c in reversed(node.named_children)
            )
            continue
        if kind == "friend_declaration":
            stack.extend(
                (c, outer, False, outer) for c in reversed(node.named_children)
            )
            continue
        if kind in {"preproc_def", "preproc_function_def"}:
            macros(node)
            continue
        if kind == "namespace_definition":
            name = node.child_by_field_name("name")
            inner = scope
            parts = (
                name.named_children
                if name is not None and name.type == "nested_namespace_specifier"
                else [name]
                if name is not None
                else []
            )
            for part in parts:
                add(node, part, inner, _text(part, content), "Namespace")
                inner = (*inner, _text(part, content))
            body = node.child_by_field_name("body")
            if body is not None:
                stack.extend(
                    (c, inner, False, inner) for c in reversed(body.named_children)
                )
            continue
        if kind in _CPP_CLASSES:
            body = node.child_by_field_name("body")
            if body is None:
                continue
            name = node.child_by_field_name("name")
            parts = _cpp_name(name, content) if name is not None else None
            inner = scope
            if parts is not None:
                written, text, name_node = parts
                add(node, name_node, (*scope, *written), text, _CPP_CLASSES[kind])
                inner = (*scope, *written, text)
            # An enum's body holds enumerators, which are not declarations.
            stack.extend((c, inner, True, outer) for c in reversed(body.named_children))
            continue
        written = node.child_by_field_name("type")
        if written is not None and written.type in _CPP_CLASSES:
            # A type defined where it is used: `struct S {...} s;`.
            stack.append((written, scope, in_class, outer))
        if kind == "function_definition":
            function(node, node.child_by_field_name("declarator"), scope, in_class)
        elif (
            kind
            in {"function_declarator", "pointer_declarator", "reference_declarator"}
            and node.parent is not None
            and node.parent.type == "ERROR"
        ):
            # A function's declarator standing alone in text the grammar
            # could not parse (see extract_c).
            function(node, node, scope, in_class)
        elif kind in {"declaration", "field_declaration"}:
            for declarator in _cpp_declarators(node):
                function(node, declarator, scope, in_class)
        elif kind == "type_definition":
            for declarator in node.children_by_field_name("declarator"):
                declared = _c_declared_name(declarator, {"type_identifier"})
                if declared is not None:
                    add(
                        node,
                        declared[0],
                        scope,
                        _text(declared[0], content),
                        "TypeAlias",
                    )
        elif kind == "alias_declaration":
            name = node.child_by_field_name("name")
            add(node, name, scope, _text(name, content), "TypeAlias")
        macros_in = [c for c in node.named_children if c is not written]
        for child in macros_in:
            macros(child)
    return found


_RUBY_SCOPES = {"class": "Class", "module": "Module"}


def _ruby_constant_path(node, content):
    """(names, absolute) of a class or module name: ``B::C`` gives
    (["B", "C"], False), ``::C`` gives (["C"], True)."""
    names = []
    while node is not None and node.type == "scope_resolution":
        names.append(_text(node.child_by_field_name("name"), content))
        node = node.child_by_field_name("scope")
        if node is None:
            return list(reversed(names)), True
    if node.type != "constant":
        return None
    names.append(_text(node, content))
    return list(reversed(names)), False


def extract_ruby(
    tree, content: bytes, repository_id: str, file: str, *, nodes=None
) -> list[Definition]:
    """Ruby modules, classes and methods, in source order.

    Symbols join the enclosing modules and classes and the path written in
    a name (``class B::C`` in ``module A`` is ``A.B.C``; ``::C`` starts at
    the top) with ``.``. A ``def`` in a class or module body, in its
    ``class << self`` or in a block written there (``included do``,
    ``Class.new do``) is a ``Method`` of that class or module, whether it
    defines an instance or a singleton method (``def self.m``); elsewhere it
    is a ``Function``. A ``def`` inside another method's body is not
    recorded. ``nodes`` receives id -> (node, name).
    """
    found, ordinals = [], {}

    def add(node, name_node, symbol, kind):
        ordinal = ordinals.get((symbol, kind), 0)
        ordinals[(symbol, kind)] = ordinal + 1
        definition = Definition(
            id=identifier(repository_id, file, symbol, kind, ordinal),
            file=file,
            symbol=symbol,
            kind=kind,
            line=node.start_point.row + 1,
            column=node.start_point.column,
            end_line=node.end_point.row + 1,
            end_column=node.end_point.column,
        )
        found.append(definition)
        if nodes is not None:
            nodes[definition.id] = (node, name_node)

    # (node, scope, inside a class or module body)
    stack = [(tree.root_node, (), False)]
    while stack:
        node, scope, in_type = stack.pop()
        kind = _RUBY_SCOPES.get(node.type)
        if kind is not None:
            name = node.child_by_field_name("name")
            path = _ruby_constant_path(name, content) if name is not None else None
            if path is None:
                continue
            names, absolute = path
            inner = (*(() if absolute else scope), *names)
            last = name.child_by_field_name("name") if name.type != "constant" else name
            add(node, last, ".".join(inner), kind)
            stack.extend((c, inner, True) for c in reversed(node.named_children))
            continue
        if node.type in {"method", "singleton_method"}:
            name = node.child_by_field_name("name")
            if name is not None:
                text = _text(name, content)
                symbol = ".".join((*scope, text))
                # A receiver (`def obj.m`) makes it a method of that object.
                method = in_type or node.type == "singleton_method"
                add(node, name, symbol, "Method" if method else "Function")
            # A def in a method body is defined when the method runs.
            continue
        stack.extend((c, scope, in_type) for c in reversed(node.named_children))
    return found


_PHP_TYPES = {
    "class_declaration": "Class",
    "interface_declaration": "Interface",
    "trait_declaration": "Trait",
    "enum_declaration": "Enum",
}
# Bodies run later, so what they declare is declared when they run. (An
# arrow function's body is an expression, which declares nothing outside
# these.)
_PHP_BODIES = {"anonymous_function", "anonymous_class"}


def extract_php(
    tree, content: bytes, repository_id: str, file: str, *, nodes=None
) -> list[Definition]:
    """PHP classes, interfaces, traits, enums, functions and methods, in
    source order.

    Symbols join the namespace (``namespace A\\B;`` up to the next
    namespace statement, or a braced ``namespace A\\B { }``) and the
    enclosing class-like declaration with ``.``. Declarations in a
    conditional block (``if (!function_exists('f')) { function f() {} }``)
    are recorded; those in a function or method body, a closure or an
    anonymous class are not, since PHP declares them only when that code
    runs. ``nodes`` receives id -> (node, name).
    """
    found, ordinals = [], {}

    def add(node, name_node, symbol, kind):
        ordinal = ordinals.get((symbol, kind), 0)
        ordinals[(symbol, kind)] = ordinal + 1
        definition = Definition(
            id=identifier(repository_id, file, symbol, kind, ordinal),
            file=file,
            symbol=symbol,
            kind=kind,
            line=node.start_point.row + 1,
            column=node.start_point.column,
            end_line=node.end_point.row + 1,
            end_column=node.end_point.column,
        )
        found.append(definition)
        if nodes is not None:
            nodes[definition.id] = (node, name_node)

    def children(node, scope):
        """Each named child with its scope: a namespace statement scopes
        the statements after it."""
        items = []
        for child in node.named_children:
            if child.type == "namespace_definition":
                name = child.child_by_field_name("name")
                names = tuple(_text(name, content).split("\\")) if name else ()
                if child.child_by_field_name("body") is None:
                    scope = names
                    continue
                items.append((child, names, False))
                continue
            items.append((child, scope, False))
        return items

    # (node, scope, inside a class-like body)
    stack = list(reversed(children(tree.root_node, ())))
    while stack:
        node, scope, in_type = stack.pop()
        kind = _PHP_TYPES.get(node.type)
        if kind is not None:
            name = node.child_by_field_name("name")
            inner = (*scope, _text(name, content))
            add(node, name, ".".join(inner), kind)
            stack.extend((c, inner, True) for c in reversed(node.named_children))
            continue
        if node.type in {"function_definition", "method_declaration"}:
            name = node.child_by_field_name("name")
            symbol = ".".join((*scope, _text(name, content)))
            add(node, name, symbol, "Method" if in_type else "Function")
            continue
        if node.type in _PHP_BODIES:
            continue
        stack.extend((c, scope, in_type) for c in reversed(node.named_children))
    return found


def _rust_macro_export(node):
    """Whether a ``macro_rules!`` definition carries ``#[macro_export]``."""
    sibling = node.prev_named_sibling
    while sibling is not None and sibling.type in {
        "attribute_item",
        "line_comment",
        "block_comment",
    }:
        if sibling.type == "attribute_item":
            attribute = sibling.named_children[0] if sibling.named_children else None
            path = attribute.named_children[0] if attribute else None
            if path is not None and path.text == b"macro_export":
                return True
        sibling = sibling.prev_named_sibling
    return False


def _text(node, content: bytes) -> str:
    return content[node.start_byte : node.end_byte].decode("utf-8")


def _bound(node, content):
    """(imported name, local name) for a possibly aliased import element."""
    if node.type in {"aliased_import", "import_specifier", "export_specifier"}:
        name = node.child_by_field_name("name")
        alias = node.child_by_field_name("alias")
        name = _text(name, content)
        return name, _text(alias, content) if alias is not None else name
    name = _text(node, content)
    return name, name


def _python_imports(node, content):
    """Yield (specifier, kind, names, local names) for one Python import."""
    if node.type == "import_statement":
        for child in node.children_by_field_name("name"):
            module, local = _bound(child, content)
            # `import a.b` binds `a`; `import a.b as c` binds `c`.
            if child.type != "aliased_import":
                local = module.split(".")[0]
            yield module, "import", (), (local,)
    elif node.type == "import_from_statement":
        module = node.child_by_field_name("module_name")
        if module is None:
            return
        pairs = [_bound(c, content) for c in node.children_by_field_name("name")]
        if any(child.type == "wildcard_import" for child in node.children):
            pairs.append(("*", "*"))
        yield (
            _text(module, content),
            "from_import",
            tuple(name for name, _ in pairs),
            tuple(local for _, local in pairs),
        )


def _typescript_imports(node, content):
    """Yield (specifier, kind, names, local names) for an import or re-export.

    JavaScript ES modules share this syntax; CommonJS require() is not recorded.
    """
    if node.type not in {"import_statement", "export_statement"}:
        return
    source = node.child_by_field_name("source")
    if source is None or source.type != "string":
        return
    fragments = [c for c in source.named_children if c.type == "string_fragment"]
    if len(fragments) != 1:
        return
    pairs = []
    if node.type == "import_statement":
        clause = next(
            (c for c in node.named_children if c.type == "import_clause"), None
        )
        for child in clause.named_children if clause is not None else ():
            if child.type == "identifier":
                pairs.append(("default", _text(child, content)))
            elif child.type == "namespace_import":
                local = next(c for c in child.named_children if c.type == "identifier")
                pairs.append(("*", _text(local, content)))
            elif child.type == "named_imports":
                pairs.extend(
                    _bound(c, content)
                    for c in child.named_children
                    if c.type == "import_specifier"
                )
        names = tuple(name for name, _ in pairs)
        yield (
            _text(fragments[0], content),
            "import",
            names,
            tuple(local for _, local in pairs),
        )
        return
    clause = next((c for c in node.named_children if c.type == "export_clause"), None)
    if clause is None:
        names = ("*",)
    else:
        names = tuple(
            _bound(c, content)[0]
            for c in clause.named_children
            if c.type == "export_specifier"
        )
    # A re-export binds no local name in the exporting module.
    yield _text(fragments[0], content), "export_from", names, ()


def _go_imports(node, content):
    """Yield one import per Go import spec: the package path and its binding.

    An alias binds that name, ``.`` imports every exported name (``*``), and
    ``_`` binds nothing. Without an alias the binding is the package's declared
    name, which only the imported package states, so none is recorded.
    """
    if node.type != "import_spec":
        return
    path = node.child_by_field_name("path")
    if path is None or path.type != "interpreted_string_literal":
        return
    parts = [c for c in path.named_children]
    if len(parts) != 1 or parts[0].type != "interpreted_string_literal_content":
        return
    alias = node.child_by_field_name("name")
    if alias is None or alias.type == "blank_identifier":
        names = local_names = ()
    elif alias.type == "dot":
        names = local_names = ("*",)
    else:
        names, local_names = (), (_text(alias, content),)
    yield _text(parts[0], content), "import", names, local_names


def _java_name(node, content):
    """Dotted text of a Java (scoped) identifier, without spaces or comments."""
    if node.type == "identifier":
        return _text(node, content)
    scope = node.child_by_field_name("scope")
    name = node.child_by_field_name("name")
    if node.type != "scoped_identifier" or scope is None or name is None:
        return None
    prefix = _java_name(scope, content)
    return None if prefix is None else prefix + "." + _text(name, content)


def _java_imports(node, content):
    """Yield one import per Java import declaration.

    The specifier is the imported type or package. ``import a.b.C;`` imports
    the type and binds ``C``; ``import static a.b.C.m;`` imports member ``m``
    of type ``a.b.C``; ``a.b.*`` imports every type of a package or every
    member type (or, with ``static``, static member) of a type, named ``*``.
    """
    if node.type != "import_declaration":
        return
    path = next(
        (
            c
            for c in node.named_children
            if c.type in {"scoped_identifier", "identifier"}
        ),
        None,
    )
    name = _java_name(path, content) if path is not None else None
    if name is None:
        return
    if any(c.type == "asterisk" for c in node.named_children):
        yield name, "import", ("*",), ("*",)
    elif any(c.type == "static" for c in node.children):
        owner, _, member = name.rpartition(".")
        yield owner, "import", (member,), (member,)
    else:
        yield name, "import", (), (name.rsplit(".", 1)[-1],)


def _php_imports(node, content):
    """Yield one import per clause of a PHP ``use`` declaration.

    The specifier is the imported name as written, without a leading
    backslash and with a group's prefix (``use A\\{B, C}`` imports
    ``A\\B`` and ``A\\C``). The kind is ``use`` for a class or namespace,
    ``use_function`` or ``use_const`` for ``use function`` or ``use const``;
    each binds its alias or the name's last part.
    """
    if node.type != "namespace_use_declaration":
        return
    group = node.child_by_field_name("body")
    prefix = next((c for c in node.named_children if c.type == "namespace_name"), None)
    clauses = [
        c for c in (group or node).named_children if c.type == "namespace_use_clause"
    ]
    # `use function A\\f, A\\g;`: the grammar puts the keyword in the first
    # clause, though it applies to every clause.
    written = node.child_by_field_name("type")
    if written is None and group is None and clauses:
        written = clauses[0].child_by_field_name("type")
    for clause in clauses:
        keyword = written or clause.child_by_field_name("type")
        name = next(
            c for c in clause.named_children if c.type in {"qualified_name", "name"}
        )
        specifier = _text(name, content).lstrip("\\")
        if prefix is not None:
            specifier = _text(prefix, content) + "\\" + specifier
        alias = clause.child_by_field_name("alias")
        local = _text(alias, content) if alias else specifier.rsplit("\\", 1)[-1]
        kind = "use_" + keyword.type if keyword is not None else "use"
        yield specifier, kind, (), (local,)


def _rust_inline_modules(node, content, expander=None):
    """Names of the inline ``mod name { ... }`` around a node, outermost first;
    an item written in an expanded macro invocation is in the modules around
    the invocation."""
    parents = []
    outer = node.parent
    while outer is not None:
        if outer.type == "source_file":
            outer = expander.invocation(outer) if expander else None
            continue
        if outer.type == "mod_item":
            parents.append(_text(outer.child_by_field_name("name"), content))
        outer = outer.parent
    return [p.removeprefix("r#") for p in reversed(parents)]


# Rust macros that emit each item they are given unchanged, apart from added
# attributes: one rule, `($($i:item)*)` (optionally after `#![$m:meta]`),
# transcribed as `$( #[...]* $i )*`.
_RUST_ITEM_MATCHER = re.compile(r"\((?:#!\[\$\w+:meta\])?\$\(\$(\w+):item\)\*\)")
_RUST_ATTRIBUTE = r"#\[(?:[^\[\]]|\[[^\[\]]*\])*\]"


def rust_macro_rules(tree, content: bytes):
    """(name, passes items through) for each ``macro_rules!`` definition."""
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.named_children))
        if node.type != "macro_definition":
            continue
        name = node.child_by_field_name("name")
        rules = [c for c in node.named_children if c.type == "macro_rule"]
        through = False
        if name is not None and len(rules) == 1:
            left = rules[0].child_by_field_name("left")
            right = rules[0].child_by_field_name("right")
            compact = [
                re.sub(r"\s+", "", _text(side, content)) if side else ""
                for side in (left, right)
            ]
            matched = _RUST_ITEM_MATCHER.fullmatch(compact[0])
            through = bool(matched) and bool(
                re.fullmatch(
                    r"[\(\[\{]\$\((?:%s)*\$%s\)\*[\)\]\}]"
                    % (_RUST_ATTRIBUTE, re.escape(matched.group(1))),
                    compact[1],
                )
            )
        if name is not None:
            yield _text(name, content).removeprefix("r#"), through


class RustExpander:
    """Items written in invocations of item pass-through macros.

    An invocation at module level of a macro in ``item_macros`` (each of its
    definitions passes items through, see rust_macro_rules) is read as the
    items in its body: the body is parsed on its own, in place, and its
    items take the invocation's place in the tree.
    """

    def __init__(self, content: bytes, item_macros):
        self.content = content
        self.item_macros = frozenset(item_macros)
        self.parser = make_parser("rust", ".rs") if self.item_macros else None
        self.trees, self.invocations = [], {}

    def invocation(self, root):
        """The invocation an expanded body's root stands for, or None."""
        return self.invocations.get((root.start_byte, root.end_byte))

    def children(self, node):
        if (
            self.parser is None
            or node.type != "macro_invocation"
            or node.parent is None
            or not (
                node.parent.type == "source_file"
                or (
                    node.parent.type == "declaration_list"
                    and node.parent.parent is not None
                    and node.parent.parent.type == "mod_item"
                )
            )
        ):
            return node.named_children
        name = node.child_by_field_name("macro")
        body = next((c for c in node.named_children if c.type == "token_tree"), None)
        if (
            name is None
            or body is None
            or _text(name, self.content).split("::")[-1] not in self.item_macros
        ):
            return node.named_children
        from tree_sitter import Range

        start, end = body.start_point, body.end_point
        self.parser.included_ranges = [
            Range(
                (start[0], start[1] + 1),
                (end[0], end[1] - 1),
                body.start_byte + 1,
                body.end_byte - 1,
            )
        ]
        tree = self.parser.parse(self.content)
        if tree.root_node.has_error:
            return node.named_children
        self.trees.append(tree)
        self.invocations[(tree.root_node.start_byte, tree.root_node.end_byte)] = node
        return tree.root_node.named_children


def _rust_path(node, content):
    """Segments of a use path: ``crate::a::b`` -> ["crate", "a", "b"]; a
    leading ``::`` is an empty first segment."""
    if node is None:
        return []
    if node.type == "scoped_identifier":
        path = node.child_by_field_name("path")
        prefix = _rust_path(path, content) if path is not None else [""]
        return prefix + _rust_path(node.child_by_field_name("name"), content)
    return [_text(node, content).removeprefix("r#")]


def _rust_use_tree(node, content, prefix):
    """(path, kind, local name) for each leaf of a use tree."""
    if node.type == "use_list":
        for child in node.named_children:
            yield from _rust_use_tree(child, content, prefix)
    elif node.type == "scoped_use_list":
        path = prefix + _rust_path(node.child_by_field_name("path"), content)
        yield from _rust_use_tree(node.child_by_field_name("list"), content, path)
    elif node.type == "use_wildcard":
        inner = node.named_children[0] if node.named_children else None
        yield prefix + _rust_path(inner, content), "use_glob", None
    elif node.type == "use_as_clause":
        path = prefix + _rust_path(node.child_by_field_name("path"), content)
        alias = _text(node.child_by_field_name("alias"), content)
        yield path, "use", alias.removeprefix("r#")
    elif node.type in {"identifier", "scoped_identifier", "self", "crate", "super"}:
        path = prefix + _rust_path(node, content)
        if path[-1] == "self" and len(path) > 1:
            # `a::{self}` names the module a itself.
            path = path[:-1]
        yield path, "use", path[-1]


def _rust_imports(node, content, expander=None):
    """Rust ``mod name;``, ``use`` and ``extern crate`` declarations.

    Each leaf of a ``use`` tree is one import of kind ``use`` (or
    ``use_glob`` for ``path::*``): the specifier is the full path joined by
    ``::`` (a leading ``::`` kept), ``names`` the inline modules around the
    declaration, and ``local_names`` the name it binds (an alias applied).
    ``extern crate name [as alias];`` is kind ``extern_crate``.
    """
    if node.type == "mod_item":
        yield from _rust_modules(node, content, expander)
    elif node.type == "use_declaration":
        parents = tuple(_rust_inline_modules(node, content, expander))
        argument = node.child_by_field_name("argument")
        if argument is None:
            return
        for path, kind, local in _rust_use_tree(argument, content, []):
            if not path or not all(path[1:]):
                continue
            yield (
                "::".join(path),
                kind,
                parents,
                () if local is None else (local,),
            )
    elif node.type == "extern_crate_declaration":
        name = node.child_by_field_name("name")
        alias = node.child_by_field_name("alias")
        if name is None:
            return
        word = _text(name, content).removeprefix("r#")
        local = _text(alias, content).removeprefix("r#") if alias else word
        yield (
            word,
            "extern_crate",
            tuple(_rust_inline_modules(node, content, expander)),
            (local,),
        )


def _rust_modules(node, content, expander=None):
    """A Rust ``mod name;`` declaration, as the file module it includes.

    The specifier is the module's path from the declaring file's module
    directory (``a/b`` for ``mod a { mod b; }``). With a ``#[path]``
    attribute (kind ``module_path``) it is the file path the attribute gives,
    and ``names`` are the inline modules around the declaration.
    """
    if node.type != "mod_item" or node.child_by_field_name("body") is not None:
        return
    name = node.child_by_field_name("name")
    if name is None:
        return
    word = _text(name, content).removeprefix("r#")
    parents = _rust_inline_modules(node, content, expander)
    path = None
    sibling = node.prev_named_sibling
    while sibling is not None and sibling.type in {
        "attribute_item",
        "line_comment",
        "block_comment",
    }:
        attribute = sibling.named_children[0] if sibling.named_children else None
        if (
            sibling.type == "attribute_item"
            and attribute is not None
            and attribute.type == "attribute"
            and _text(attribute.named_children[0], content) == "path"
        ):
            value = next(
                (c for c in attribute.named_children if c.type == "string_literal"),
                None,
            )
            if value is not None:
                path = _text(value, content)[1:-1]
        sibling = sibling.prev_named_sibling
    if path is not None:
        yield path, "module_path", tuple(parents), (word,)
    else:
        yield "/".join((*parents, word)), "module", (), (word,)


def extract_imports(
    tree,
    content: bytes,
    repository_id: str,
    file: str,
    language: str,
    *,
    expander: "RustExpander | None" = None,
) -> list[Import]:
    """Static import statements in source order; dynamic loads are not facts
    here. An ``expander`` reads Rust items written in pass-through macro
    invocations."""
    children = expander.children if expander else (lambda node: node.named_children)
    visit = {
        "python": _python_imports,
        "go": _go_imports,
        "java": _java_imports,
        "php": _php_imports,
    }.get(language, _typescript_imports)
    imports, ordinals = [], {}
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        found = (
            _rust_imports(node, content, expander)
            if language == "rust"
            else visit(node, content)
        )
        block = (
            _rust_block(node)
            if node.type in {"use_declaration", "extern_crate_declaration"}
            else None
        )
        for specifier, kind, names, local_names in found:
            ordinal = ordinals.get(specifier, 0)
            ordinals[specifier] = ordinal + 1
            imports.append(
                Import(
                    id=identifier(repository_id, file, specifier, "Import", ordinal),
                    file=file,
                    specifier=specifier,
                    kind=kind,
                    line=node.start_point.row + 1,
                    column=node.start_point.column,
                    end_line=node.end_point.row + 1,
                    end_column=node.end_point.column,
                    names=names,
                    local_names=local_names,
                    block=block,
                )
            )
        stack.extend(reversed(children(node)))
    return imports


def _rust_block(node):
    """The span of the innermost block a Rust declaration is written in (a
    function body, or a block in one), or None when a module holds it."""
    outer = node.parent
    while outer is not None and outer.type not in {"mod_item", "source_file"}:
        if outer.type == "block":
            return (
                outer.start_point.row + 1,
                outer.start_point.column,
                outer.end_point.row + 1,
                outer.end_point.column,
            )
        outer = outer.parent
    return None


_CHAIN_PARTS = {"identifier", "property_identifier", "field_identifier", "this"}


def _dotted(node, content):
    """'a.b.c' for a pure identifier chain; None for any other callee shape."""
    if node.type in _CHAIN_PARTS:
        return _text(node, content)
    if node.type in {"attribute", "member_expression", "selector_expression"}:
        obj = node.child_by_field_name(
            "operand" if node.type == "selector_expression" else "object"
        )
        prop = node.child_by_field_name(
            {
                "attribute": "attribute",
                "member_expression": "property",
                "selector_expression": "field",
            }[node.type]
        )
        if obj is None or prop is None or prop.type not in _CHAIN_PARTS:
            return None
        if any(child.type == "optional_chain" for child in node.children):
            return None
        head = _dotted(obj, content)
        return None if head is None else head + "." + _text(prop, content)
    return None


def _java_dotted(node, content):
    """'a.B.c' for a Java name, field chain or (possibly generic) type."""
    if node.type in {"identifier", "type_identifier", "this"}:
        return _text(node, content)
    if node.type == "field_access":
        obj = node.child_by_field_name("object")
        field = node.child_by_field_name("field")
        if obj is None or field is None or field.type != "identifier":
            return None
        head = _java_dotted(obj, content)
        return None if head is None else head + "." + _text(field, content)
    if node.type in {"scoped_type_identifier", "generic_type"}:
        parts = [c for c in node.named_children if c.type != "type_arguments"]
        names = [_java_dotted(c, content) for c in parts]
        if not names or None in names:
            return None
        return ".".join(names)
    return None


def _java_arguments(node):
    """Arguments written in a Java call or ``new``; None for other languages."""
    if node.type not in {"method_invocation", "object_creation_expression"}:
        return None
    arguments = node.child_by_field_name("arguments")
    if arguments is None:
        return None
    return sum(
        1
        for c in arguments.named_children
        if c.type not in {"line_comment", "block_comment"}
    )


def _rust_call_path(node, content):
    """Segments of a Rust call path, or None: ``a::b::f`` -> ["a", "b", "f"],
    ``Vec::<u8>::new`` -> ["Vec", "new"]; a leading ``::`` is an empty first
    segment, and a qualified path (``<T as Trait>::f``) is None."""
    if node.type == "scoped_identifier":
        path = node.child_by_field_name("path")
        name = node.child_by_field_name("name")
        prefix = [""] if path is None else _rust_call_path(path, content)
        if prefix is None or name is None:
            return None
        return prefix + [_text(name, content).removeprefix("r#")]
    if node.type == "generic_type":
        inner = node.child_by_field_name("type")
        return None if inner is None else _rust_call_path(inner, content)
    if node.type in {
        "identifier",
        "type_identifier",
        "self",
        "super",
        "crate",
    }:
        return [_text(node, content).removeprefix("r#")]
    return None


def _rust_receiver(node, content):
    """A method call's receiver as a dotted name (``self``, ``x``,
    ``self.a.b``), or None for any other expression."""
    if node.type in {"self", "identifier"}:
        return _text(node, content).removeprefix("r#")
    if node.type == "field_expression":
        value = node.child_by_field_name("value")
        field = node.child_by_field_name("field")
        head = None if value is None else _rust_receiver(value, content)
        if head is None or field is None or field.type != "field_identifier":
            return None
        return head + "." + _text(field, content).removeprefix("r#")
    return None


def _rust_callee(node, content):
    """A Rust call's callee: a path joined by ``::`` (a leading ``::`` kept),
    or a method call on a dotted receiver as ``receiver.name``; else None."""
    function = node.child_by_field_name("function")
    if function is not None and function.type == "generic_function":
        function = function.child_by_field_name("function")
    if function is None:
        return None
    if function.type == "field_expression":
        value = function.child_by_field_name("value")
        field = function.child_by_field_name("field")
        head = None if value is None else _rust_receiver(value, content)
        if head is None or field is None or field.type != "field_identifier":
            return None
        return head + "." + _text(field, content).removeprefix("r#")
    path = _rust_call_path(function, content)
    return None if path is None else "::".join(path)


def _callee(node, content, rust=False):
    if rust:
        return (
            (_rust_callee(node, content), "call")
            if node.type == "call_expression"
            else None
        )
    if node.type == "method_invocation":
        name = node.child_by_field_name("name")
        receiver = node.child_by_field_name("object")
        if receiver is None:
            return _text(name, content), "call"
        head = _java_dotted(receiver, content)
        return (None if head is None else head + "." + _text(name, content)), "call"
    if node.type == "object_creation_expression":
        target = node.child_by_field_name("type")
        return (_java_dotted(target, content) if target is not None else None), "new"
    if node.type in {"call", "call_expression"}:
        target = node.child_by_field_name("function")
        return (_dotted(target, content), "call") if target is not None else None
    if node.type == "new_expression":
        target = node.child_by_field_name("constructor")
        return (_dotted(target, content), "new") if target is not None else None
    return None


_PYTHON_COMPREHENSIONS = {
    "list_comprehension",
    "set_comprehension",
    "dictionary_comprehension",
    "generator_expression",
}
_PYTHON_SCOPES = {"function_definition", "lambda", "class_definition"}
_PYTHON_PATTERNS = {
    "pattern_list",
    "tuple_pattern",
    "list_pattern",
    "tuple",
    "list",
    "parenthesized_expression",
    "list_splat_pattern",
    "dictionary_splat_pattern",
    "as_pattern_target",
}


def _python_targets(node, content, found):
    """Add the names a binding target binds (``a``, ``a, *b``); an attribute
    or subscript target binds no name."""
    if node is None:
        return
    if node.type == "identifier":
        found.add(_text(node, content))
    elif node.type in _PYTHON_PATTERNS:
        for child in node.named_children:
            _python_targets(child, content, found)


def _python_parameters(parameters, content, found):
    for item in parameters.named_children if parameters is not None else ():
        name = item.child_by_field_name("name")
        if name is not None:
            found.add(_text(name, content))
        elif item.type == "typed_parameter":
            _python_targets(item.named_children[0], content, found)
        else:
            _python_targets(item, content, found)


def _python_bindings(scope, content):
    """(names bound in a function, lambda or comprehension scope, names it
    declares ``global`` or with a nested ``def`` or ``class``, which the
    lexical rule resolves as before even when also assigned). A function binds its parameters and every name it
    assigns, iterates into, catches, opens with ``as``, deletes, captures
    in a ``case`` or assigns with ``:=``, wherever in its body (a
    ``nonlocal`` name is bound in an enclosing function already); nested
    functions, classes, lambdas and comprehensions are scopes of their own
    (an ``:=`` in a comprehension still binds in the function). Imports and
    nested ``def``/``class`` names are declarations, resolved as before."""
    bound, declared = set(), set()
    if scope.type in _PYTHON_COMPREHENSIONS:
        for clause in scope.named_children:
            if clause.type == "for_in_clause":
                _python_targets(clause.child_by_field_name("left"), content, bound)
        return bound, declared
    _python_parameters(scope.child_by_field_name("parameters"), content, bound)
    # A comprehension's body is walked: only `:=` binds there.
    stack = [scope.child_by_field_name("body")]
    while stack:
        node = stack.pop()
        if node.type == "named_expression":
            _python_targets(node.child_by_field_name("name"), content, bound)
        elif node.type in {"assignment", "augmented_assignment", "for_statement"}:
            _python_targets(node.child_by_field_name("left"), content, bound)
        elif node.type == "as_pattern":
            _python_targets(node.child_by_field_name("alias"), content, bound)
        elif node.type == "delete_statement":
            for child in node.named_children:
                _python_targets(child, content, bound)
        elif node.type == "global_statement":
            declared |= {_text(c, content) for c in node.named_children}
        elif (
            node.type == "dotted_name"
            and node.parent.type == "case_pattern"
            and len(node.named_children) == 1
        ):
            # `case f:` and `case [f]:` capture; `case C.x:` compares.
            bound.add(_text(node, content))
        if node.type not in _PYTHON_SCOPES:
            stack.extend(node.named_children)
        elif node.type != "lambda":
            # A nested `def` or `class` declares its name in this scope.
            declared.add(_text(node.child_by_field_name("name"), content))
    return bound, declared


def _python_shadowed(node, content, callee, cache):
    """Whether the first name of a Python call's callee is bound in a
    function, lambda or comprehension around the call, so that it does not
    name a module-level declaration or import. True or None."""
    head = callee.split(".")[0]
    child, scope = node, node.parent
    while scope is not None:
        inside = False
        if scope.type in {"function_definition", "lambda"}:
            inside = child == scope.child_by_field_name("body")
        elif scope.type in _PYTHON_COMPREHENSIONS:
            # The first iterable is evaluated in the enclosing scope.
            first = next(
                (c for c in scope.named_children if c.type == "for_in_clause"), None
            )
            inside = first is None or child != first
        elif scope.type == "class_definition":
            # Names a class body binds are not seen in its methods.
            pass
        if inside:
            key = (scope.start_byte, scope.end_byte, scope.type)
            if key not in cache:
                cache[key] = _python_bindings(scope, content)
            bound, declared = cache[key]
            if head in declared:
                return None
            if head in bound:
                return True
        child, scope = scope, scope.parent
    return None


def _java_anonymous(node, content, spans, symbols):
    """Anonymous classes around a Java call, innermost first; None when
    there are none or for other languages.

    Each is (base as written, symbol of the scope its methods are named in,
    first and last line): a class body's methods are named in the
    declaration enclosing the ``new`` (an enum constant's body: the enum).
    """
    if node.type not in {"method_invocation", "object_creation_expression"}:
        return None
    found = []
    child, parent = node, node.parent
    while parent is not None:
        if child.type == "class_body" and parent.type in {
            "object_creation_expression",
            "enum_constant",
        }:
            start = (parent.start_point.row + 1, parent.start_point.column)
            end = (parent.end_point.row + 1, parent.end_point.column)
            owner = next((i for s, e, i in spans if s <= start and end <= e), None)
            if parent.type == "enum_constant":
                base = owner and symbols[owner].rpartition(".")[2]
            else:
                target = parent.child_by_field_name("type")
                base = _java_dotted(target, content) if target is not None else None
            if base and owner is not None:
                found.append((base, symbols[owner], start[0], end[0]))
        child, parent = parent, parent.parent
    return tuple(found) or None


def extract_call_sites(
    tree,
    content: bytes,
    repository_id: str,
    file: str,
    definitions,
    *,
    expander: "RustExpander | None" = None,
) -> list[CallSite]:
    """Named call sites with their innermost enclosing declaration.

    Calls whose callee is not a plain or dotted name (for example ``f()()`` or
    ``x[0]()``) are not recorded. A Rust callee is a path joined by ``::`` or
    a method call ``receiver.name`` on a dotted receiver; an ``expander``
    reads Rust items written in item macro invocations.
    """
    rust = expander is not None
    children = expander.children if expander else (lambda node: node.named_children)
    spans = sorted(
        (
            ((d.line, d.column), (d.end_line, d.end_column), d.id)
            for d in definitions
            if d.file == file
        ),
        reverse=True,
    )
    symbols = {d.id: d.symbol for d in definitions if d.file == file}
    python = tree.root_node.type == "module"
    sites, ordinals, bindings = [], {}, {}
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        found = _callee(node, content, rust)
        if found is not None:
            callee, kind = found
            if callee is not None:
                start = (node.start_point.row + 1, node.start_point.column)
                end = (node.end_point.row + 1, node.end_point.column)
                # Latest-starting enclosing span is the innermost declaration.
                caller = next((i for s, e, i in spans if s <= start and end <= e), None)
                ordinal = ordinals.get(callee, 0)
                ordinals[callee] = ordinal + 1
                sites.append(
                    CallSite(
                        id=identifier(repository_id, file, callee, "Call", ordinal),
                        file=file,
                        callee=callee,
                        kind=kind,
                        line=start[0],
                        column=start[1],
                        end_line=end[0],
                        end_column=end[1],
                        caller_id=caller,
                        arguments=_java_arguments(node),
                        anonymous=_java_anonymous(node, content, spans, symbols),
                        shadowed=_python_shadowed(node, content, callee, bindings)
                        if python
                        else None,
                    )
                )
        stack.extend(reversed(children(node)))
    return sites


_TYPE_DECLARATIONS = {
    "class_definition",
    "class_declaration",
    "abstract_class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
    "trait_item",
}
# Java heritage clauses: the node holding each list, and its kind.
_JAVA_HERITAGE = {
    "superclass": "extends",
    "super_interfaces": "implements",
    "extends_interfaces": "extends",
}


def _base_name(node, content):
    """Dotted name of one heritage entry, without type arguments; else None.

    Calls, splats, keyword arguments (``metaclass=``) and other expressions
    name no declaration syntactically and are not recorded.
    """
    if node.type in {"scoped_type_identifier"} or (
        node.type == "generic_type" and node.child_by_field_name("name") is None
    ):
        # Java: a.b.Base and Base<T>.
        return _java_dotted(node, content)
    if node.type in {"subscript", "generic_type"}:
        # Generic[T] and Base<T> name Generic and Base.
        node = node.child_by_field_name("value" if node.type == "subscript" else "name")
        if node is None:
            return None
    if node.type in {"type_identifier", "identifier"}:
        return _text(node, content)
    if node.type == "nested_type_identifier":
        module = node.child_by_field_name("module")
        name = node.child_by_field_name("name")
        if module is None or name is None:
            return None
        head = (
            _text(module, content)
            if module.type == "identifier"
            else _base_name(module, content)
            if module.type == "nested_type_identifier"
            else None
        )
        if head is None and module.type == "nested_identifier":
            parts = [_text(part, content) for part in module.named_children]
            head = ".".join(parts) if all(parts) else None
        return None if head is None else head + "." + _text(name, content)
    if node.type in {"attribute", "member_expression"}:
        name = _dotted(node, content)
        return None if name is None or "this" in name.split(".") else name
    return None


def _heritage(node):
    """(entry node, kind) for each base a type declaration names, in order."""
    if node.type == "class_definition":
        bases = node.child_by_field_name("superclasses")
        return [(child, "extends") for child in bases.named_children] if bases else []
    entries = []
    for child in node.named_children:
        if child.type in _JAVA_HERITAGE:
            kind = _JAVA_HERITAGE[child.type]
            lists = [c for c in child.named_children if c.type == "type_list"]
            types = lists[0].named_children if lists else child.named_children
            entries.extend((t, kind) for t in types)
            continue
        if child.type == "class_heritage":
            clauses = child.named_children
            if clauses and clauses[0].type not in {
                "extends_clause",
                "implements_clause",
            }:
                # JavaScript: class_heritage holds the extended expression.
                entries.extend((clause, "extends") for clause in clauses[:1])
                continue
            for clause in clauses:
                if clause.type == "extends_clause":
                    value = clause.child_by_field_name("value")
                    if value is not None:
                        entries.append((value, "extends"))
                elif clause.type == "implements_clause":
                    entries.extend((t, "implements") for t in clause.named_children)
        elif child.type == "extends_type_clause":
            entries.extend((t, "extends") for t in child.named_children)
    return entries


def _rust_supertraits(node, content):
    """(bound node, path) for each supertrait a Rust trait names: its bounds
    and the bounds of ``where Self: ...``, without lifetimes, ``?Sized`` or
    type arguments."""
    lists = [node.child_by_field_name("bounds")]
    for clause in node.named_children:
        if clause.type == "where_clause":
            lists.extend(
                p.child_by_field_name("bounds")
                for p in clause.named_children
                if p.type == "where_predicate"
                and (left := p.child_by_field_name("left")) is not None
                and _text(left, content) == "Self"
            )
    entries = []
    for bounds in lists:
        for bound in bounds.named_children if bounds is not None else ():
            if bound.type == "higher_ranked_trait_bound":
                bound = bound.child_by_field_name("type")
            path = None if bound is None else _rust_type_path(bound, content)
            if path is not None:
                entries.append((bound, path))
    return entries


def extract_supertypes(
    tree, content: bytes, repository_id: str, file: str, definitions, *, expander=None
) -> list[Supertype]:
    """Bases named by each class and interface declaration, as written.

    Python bases, TypeScript ``extends`` and JavaScript ``extends`` are
    ``extends``; TypeScript ``implements`` entries are ``implements``. Only
    plain or dotted names are recorded; type arguments are dropped. A Rust
    trait's supertraits (its bounds and ``where Self:`` bounds) are
    ``extends``, their paths joined by ``::``; an ``expander`` reads the
    items of item macros.
    """
    owners = {
        (d.line, d.column, d.end_line, d.end_column): d.id
        for d in definitions
        if d.file == file
        and d.kind in {"Class", "Interface", "Enum", "Record", "Trait"}
    }
    children = expander.children if expander else (lambda node: node.named_children)
    found, ordinals = [], {}
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in _TYPE_DECLARATIONS:
            owner = owners.get(
                (
                    node.start_point.row + 1,
                    node.start_point.column,
                    node.end_point.row + 1,
                    node.end_point.column,
                )
            )
            entries = (
                [(e, "extends", name) for e, name in _rust_supertraits(node, content)]
                if node.type == "trait_item"
                else [(e, kind, None) for e, kind in _heritage(node)]
            )
            for entry, kind, name in entries if owner else ():
                name = name or _base_name(entry, content)
                if name is None:
                    continue
                key = (owner, name, kind)
                ordinal = ordinals.get(key, 0)
                ordinals[key] = ordinal + 1
                found.append(
                    Supertype(
                        id=identifier(
                            repository_id, file, f"{owner}:{name}", kind, ordinal
                        ),
                        file=file,
                        owner_id=owner,
                        name=name,
                        kind=kind,
                        line=entry.start_point.row + 1,
                        column=entry.start_point.column,
                        end_line=entry.end_point.row + 1,
                        end_column=entry.end_point.column,
                    )
                )
        stack.extend(reversed(children(node)))
    return found


def _rust_type_path(node, content):
    """A Rust type written as a path, joined by ``::``, without references,
    pointers or type arguments; None for any other type (``dyn T``, ``[T]``,
    tuples, primitive types)."""
    while node is not None and node.type in {
        "reference_type",
        "pointer_type",
        "generic_type",
    }:
        node = node.child_by_field_name("type")
    if node is None:
        return None
    if node.type == "type_identifier":
        return _text(node, content).removeprefix("r#")
    if node.type != "scoped_type_identifier":
        return None
    path = node.child_by_field_name("path")
    name = node.child_by_field_name("name")
    prefix = [""] if path is None else _rust_call_path(path, content)
    if prefix is None or name is None:
        return None
    return "::".join(prefix + [_text(name, content).removeprefix("r#")])


def extract_impls(
    tree, content: bytes, repository_id: str, file: str, *, expander
) -> list[ImplBlock]:
    """Rust impl blocks whose type is a path, in source order, with the items
    pass-through macros hold (see RustExpander). A negative impl
    (``impl !Send for T``) implements nothing and is left out."""
    found, ordinals = [], {}
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(reversed(expander.children(node)))
        if node.type != "impl_item" or any(c.type == "!" for c in node.children):
            continue
        kind = _rust_type_path(node.child_by_field_name("type"), content)
        trait = node.child_by_field_name("trait")
        if trait is not None:
            trait = _rust_type_path(trait, content)
            if trait is None:
                continue
        if kind is None:
            continue
        parameters = node.child_by_field_name("type_parameters")
        generics = tuple(
            _text(name, content)
            for c in (parameters.named_children if parameters is not None else ())
            if c.type == "type_parameter"
            and (name := c.child_by_field_name("name")) is not None
        )
        key = (kind, trait)
        ordinal = ordinals.get(key, 0)
        ordinals[key] = ordinal + 1
        found.append(
            ImplBlock(
                id=identifier(
                    repository_id, file, f"{kind}:{trait or ''}", "impl", ordinal
                ),
                file=file,
                type=kind,
                trait=trait,
                generics=generics,
                line=node.start_point.row + 1,
                column=node.start_point.column,
                end_line=node.end_point.row + 1,
                end_column=node.end_point.column,
            )
        )
    return found


def extract_packages(
    tree, content: bytes, repository_id: str, file: str
) -> list[PackageClause]:
    """The package a Go or Java file declares: its first top-level declaration."""
    for node in tree.root_node.named_children:
        if node.type not in {"package_clause", "package_declaration"}:
            continue
        name = next(
            (
                c
                for c in node.named_children
                if c.type in {"package_identifier", "scoped_identifier", "identifier"}
            ),
            None,
        )
        text = _java_name(name, content) if name is not None else None
        if name is not None and name.type == "package_identifier":
            text = _text(name, content)
        if text is None:
            return []
        return [
            PackageClause(
                id=identifier(repository_id, file, text, "Package", 0),
                file=file,
                name=text,
                line=name.start_point.row + 1,
                column=name.start_point.column,
                end_line=name.end_point.row + 1,
                end_column=name.end_point.column,
            )
        ]
    return []


_GO_MOD_TOKEN = re.compile(rb'"(?:[^"\\\n]|\\.)*"|`[^`]*`|[()]|[^\s()"`]+')


def _go_mod_path(token: bytes):
    text = token.decode("utf-8")
    if text.startswith("`"):
        return text[1:-1]
    if text.startswith('"'):
        try:
            return json.loads(text)
        except ValueError:
            return None
    return text


def extract_go_module(
    content: bytes, repository_id: str, file: str
) -> list[PackageClause]:
    """The module path a go.mod ``module`` directive declares, or nothing.

    Reads the first ``module`` directive, in line or block form; ``//``
    comments are ignored. Other directives are not facts.
    """
    in_block = False
    for row, raw in enumerate(content.split(b"\n")):
        tokens = [
            (match.start(), match.group())
            for match in _GO_MOD_TOKEN.finditer(raw.split(b"//", 1)[0])
        ]
        if not tokens:
            continue
        if in_block:
            if tokens[0][1] == b")":
                return []
            token = tokens[0]
        elif tokens[0][1] != b"module":
            continue
        elif len(tokens) > 2 and tokens[1][1] == b"(":
            token = tokens[2]
        elif len(tokens) == 2 and tokens[1][1] == b"(":
            in_block = True
            continue
        elif len(tokens) == 2:
            token = tokens[1]
        else:
            return []
        column, text = token
        path = _go_mod_path(text)
        if not path or text in {b"(", b")"}:
            return []
        return [
            PackageClause(
                id=identifier(repository_id, file, path, "Module", 0),
                file=file,
                name=path,
                line=row + 1,
                column=column,
                end_line=row + 1,
                end_column=column + len(text),
                kind="module",
            )
        ]
    return []


# Cargo target tables whose `path` names a crate root file; `lib` is a table,
# the others arrays of tables.
_CARGO_TARGETS = ("lib", "bin", "test", "example", "bench")


def _cargo_header(lines, table, index):
    """Row and span of the index-th ``[table]`` or ``[[table]]`` header."""
    pattern = re.compile(
        rb"^\s*(\[\[\s*"
        + table.encode()
        + rb"\s*\]\]|\[\s*"
        + table.encode()
        + rb"\s*\])"
    )
    seen = 0
    for row, raw in enumerate(lines):
        match = pattern.match(raw)
        if match:
            if seen == index:
                return row, match.start(1), match.end(1)
            seen += 1
    return None


def extract_cargo_manifest(
    content: bytes, repository_id: str, file: str
) -> list[Import] | None:
    """The crate roots a ``Cargo.toml`` package declares, or None if it is not
    valid TOML.

    The library is ``[lib]`` (``path`` defaults to ``src/lib.rs``, ``name`` to
    the package name with ``-`` as ``_``), kind ``crate_library``, even when
    the table is absent. Each ``[[bin]]``, ``[[test]]``, ``[[example]]`` and
    ``[[bench]]`` that gives a ``path`` is a ``crate_target``; targets Cargo
    discovers by layout are not written here. The specifier is the root file's
    path from the manifest's directory, ``names`` the package name and target
    kind, ``local_names`` the crate name. A manifest without ``[package]`` (a
    virtual workspace) declares no crates.
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib
    try:
        manifest = tomllib.loads(content.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    package = manifest.get("package")
    if not isinstance(package, dict) or not isinstance(package.get("name"), str):
        return []
    lines = content.split(b"\n")
    targets = []
    library = manifest.get("lib", {})
    if isinstance(library, dict):
        targets.append(("lib", 0, library, "src/lib.rs"))
    for kind in _CARGO_TARGETS[1:]:
        entries = manifest.get(kind, [])
        for index, entry in enumerate(entries if isinstance(entries, list) else []):
            if isinstance(entry, dict):
                targets.append((kind, index, entry, None))
    facts, ordinals = [], {}
    for kind, index, table, default in targets:
        path = table.get("path", default)
        name = table.get("name", package["name"] if kind == "lib" else None)
        if not isinstance(path, str) or not path or not isinstance(name, str):
            continue
        name = name.replace("-", "_")
        header = _cargo_header(lines, kind, index) or _cargo_header(lines, "package", 0)
        row, start, end = header or (0, 0, 0)
        specifier = path.replace("\\", "/")
        ordinal = ordinals.get(specifier, 0)
        ordinals[specifier] = ordinal + 1
        facts.append(
            Import(
                id=identifier(repository_id, file, specifier, "Import", ordinal),
                file=file,
                specifier=specifier,
                kind="crate_library" if kind == "lib" else "crate_target",
                line=row + 1,
                column=start,
                end_line=row + 1,
                end_column=end,
                names=(package["name"], kind),
                local_names=(name,),
            )
        )
    return facts


# Wrappers whose text belongs to the declaration they hold: decorators and
# the export keyword.
_SOURCE_WRAPPERS = {"decorated_definition", "export_statement", "template_declaration"}


def _columns(line: bytes, column: int):
    prefix = line[:column].decode("utf-8")
    return {
        "utf-8": column,
        "utf-16": len(prefix.encode("utf-16-le")) // 2,
        "utf-32": len(prefix),
    }


def declaration_sources(
    tree,
    content: bytes,
    repository_id: str,
    file: str,
    *,
    expander: "RustExpander | None" = None,
    language: str | None = None,
) -> dict:
    """Each declaration's source text, its span and its name's position, by id.

    The text runs from the declaration's decorators or ``export`` keyword to its
    end. Name positions ``[line, column, end line, end column]`` are given with
    UTF-8, UTF-16 and UTF-32 code-unit columns (1-based lines), so clients and
    indexes using any encoding can find them.
    """
    nodes = {}
    if language == "c":
        extract_c(tree, content, repository_id, file, nodes=nodes)
    elif language == "cpp":
        extract_cpp(tree, content, repository_id, file, nodes=nodes)
    elif language == "ruby":
        extract_ruby(tree, content, repository_id, file, nodes=nodes)
    elif language == "php":
        extract_php(tree, content, repository_id, file, nodes=nodes)
    else:
        extract(tree, content, repository_id, file, nodes=nodes, expander=expander)
    lines = content.split(b"\n")
    sources = {}
    for definition_id, (node, name) in nodes.items():
        while node.parent is not None and node.parent.type in _SOURCE_WRAPPERS:
            node = node.parent
        text = content[node.start_byte : node.end_byte].decode("utf-8")
        start = _columns(lines[name.start_point.row], name.start_point.column)
        end = _columns(lines[name.end_point.row], name.end_point.column)
        sources[definition_id] = {
            "source_code": text,
            "source_sha256": sha256(text.encode()).hexdigest(),
            "source_start_line": node.start_point.row + 1,
            "source_start_column": node.start_point.column,
            "source_end_line": node.end_point.row + 1,
            "source_end_column": node.end_point.column,
            **{
                "name_range_" + encoding.replace("-", ""): [
                    name.start_point.row + 1,
                    start[encoding],
                    name.end_point.row + 1,
                    end[encoding],
                ]
                for encoding in ("utf-8", "utf-16", "utf-32")
            },
        }
    return sources
