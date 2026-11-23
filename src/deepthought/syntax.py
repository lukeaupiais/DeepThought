# Definition mappings and structural name-field extraction adapted from
# DeepThought's Tree-sitter analyzer (Copyright (c) 2025 DeepThought, MIT).
# See licenses/deepthought-original-MIT.txt. Graph/ML/database coupling removed.
"""Explicit selected grammars and structural definitions; no semantic guessing."""

import importlib
from hashlib import sha256
import json
import re

from .models import CallSite, Definition, Import, PackageClause, Supertype

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
}
# Languages analyzed when none are selected. JavaScript, Go, Java and Rust are opt-in
# so that existing Python/TypeScript analyses keep their output and do not need
# their grammars; select them with languages=("go", ...) or --language.
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
    tree, content: bytes, repository_id: str, file: str, *, nodes=None
) -> list[Definition]:
    """Named declarations in source order; ``nodes`` receives id -> (node, name)."""
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
        stack.extend(
            (child, scope, in_class) for child in reversed(node.named_children)
        )
    return definitions


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


def _rust_modules(node, content):
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
    parents = []
    outer = node.parent
    while outer is not None and outer.type != "source_file":
        if outer.type == "mod_item":
            parents.append(_text(outer.child_by_field_name("name"), content))
        outer = outer.parent
    parents = [p.removeprefix("r#") for p in reversed(parents)]
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
    tree, content: bytes, repository_id: str, file: str, language: str
) -> list[Import]:
    """Static import statements in source order; dynamic loads are not facts here."""
    visit = {
        "python": _python_imports,
        "go": _go_imports,
        "java": _java_imports,
        "rust": _rust_modules,
    }.get(language, _typescript_imports)
    imports, ordinals = [], {}
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        for specifier, kind, names, local_names in visit(node, content):
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
                )
            )
        stack.extend(reversed(node.named_children))
    return imports


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


def _callee(node, content):
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
    tree, content: bytes, repository_id: str, file: str, definitions
) -> list[CallSite]:
    """Named call sites with their innermost enclosing declaration.

    Calls whose callee is not a plain or dotted name (for example ``f()()`` or
    ``x[0]()``) are not recorded.
    """
    spans = sorted(
        (
            ((d.line, d.column), (d.end_line, d.end_column), d.id)
            for d in definitions
            if d.file == file
        ),
        reverse=True,
    )
    symbols = {d.id: d.symbol for d in definitions if d.file == file}
    sites, ordinals = [], {}
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        found = _callee(node, content)
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
                    )
                )
        stack.extend(reversed(node.named_children))
    return sites


_TYPE_DECLARATIONS = {
    "class_definition",
    "class_declaration",
    "abstract_class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
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


def extract_supertypes(
    tree, content: bytes, repository_id: str, file: str, definitions
) -> list[Supertype]:
    """Bases named by each class and interface declaration, as written.

    Python bases, TypeScript ``extends`` and JavaScript ``extends`` are
    ``extends``; TypeScript ``implements`` entries are ``implements``. Only
    plain or dotted names are recorded; type arguments are dropped.
    """
    owners = {
        (d.line, d.column, d.end_line, d.end_column): d.id
        for d in definitions
        if d.file == file and d.kind in {"Class", "Interface", "Enum", "Record"}
    }
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
            for entry, kind in _heritage(node) if owner else ():
                name = _base_name(entry, content)
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
        stack.extend(reversed(node.named_children))
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
_SOURCE_WRAPPERS = {"decorated_definition", "export_statement"}


def _columns(line: bytes, column: int):
    prefix = line[:column].decode("utf-8")
    return {
        "utf-8": column,
        "utf-16": len(prefix.encode("utf-16-le")) // 2,
        "utf-32": len(prefix),
    }


def declaration_sources(tree, content: bytes, repository_id: str, file: str) -> dict:
    """Each declaration's source text, its span and its name's position, by id.

    The text runs from the declaration's decorators or ``export`` keyword to its
    end. Name positions ``[line, column, end line, end column]`` are given with
    UTF-8, UTF-16 and UTF-32 code-unit columns (1-based lines), so clients and
    indexes using any encoding can find them.
    """
    nodes = {}
    extract(tree, content, repository_id, file, nodes=nodes)
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
