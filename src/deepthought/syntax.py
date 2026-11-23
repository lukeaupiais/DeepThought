# Definition mappings and structural name-field extraction adapted from
# DeepThought's Tree-sitter analyzer (Copyright (c) 2025 DeepThought, MIT).
# See licenses/deepthought-original-MIT.txt. Graph/ML/database coupling removed.
"""Explicit selected grammars and structural definitions; no semantic guessing."""

import importlib
from hashlib import sha256
import json

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
}
# Languages analyzed when none are selected. JavaScript and Go are opt-in so
# that existing Python/TypeScript analyses keep their output and do not need
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


def extract(tree, content: bytes, repository_id: str, file: str) -> list[Definition]:
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
        if name_node is not None:
            name = content[name_node.start_byte : name_node.end_byte].decode("utf-8")
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
            definitions.append(
                Definition(
                    id=identifier(repository_id, file, symbol, kind, ordinal),
                    file=file,
                    symbol=symbol,
                    kind=kind,
                    line=node.start_point.row + 1,
                    column=node.start_point.column,
                    end_line=node.end_point.row + 1,
                    end_column=node.end_point.column,
                )
            )
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


def extract_imports(
    tree, content: bytes, repository_id: str, file: str, language: str
) -> list[Import]:
    """Static import statements in source order; dynamic loads are not facts here."""
    visit = {"python": _python_imports, "go": _go_imports}.get(
        language, _typescript_imports
    )
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


def _callee(node, content):
    if node.type in {"call", "call_expression"}:
        target = node.child_by_field_name("function")
        return (_dotted(target, content), "call") if target is not None else None
    if node.type == "new_expression":
        target = node.child_by_field_name("constructor")
        return (_dotted(target, content), "new") if target is not None else None
    return None


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
                    )
                )
        stack.extend(reversed(node.named_children))
    return sites


_TYPE_DECLARATIONS = {
    "class_definition",
    "class_declaration",
    "abstract_class_declaration",
    "interface_declaration",
}


def _base_name(node, content):
    """Dotted name of one heritage entry, without type arguments; else None.

    Calls, splats, keyword arguments (``metaclass=``) and other expressions
    name no declaration syntactically and are not recorded.
    """
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
        if d.file == file and d.kind in {"Class", "Interface"}
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
    """The package a Go file declares: its first top-level package clause."""
    for node in tree.root_node.named_children:
        if node.type != "package_clause":
            continue
        name = next(
            (c for c in node.named_children if c.type == "package_identifier"), None
        )
        if name is None:
            return []
        text = _text(name, content)
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
