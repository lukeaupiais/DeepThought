# Definition mappings and structural name-field extraction adapted from
# DeepThought's Tree-sitter analyzer (Copyright (c) 2025 DeepThought, MIT).
# See licenses/deepthought-original-MIT.txt. Graph/ML/database coupling removed.
"""Explicit selected grammars and structural definitions; no semantic guessing."""

import importlib
from hashlib import sha256
import json

from .models import Definition, Import

LANGUAGES = {
    "python": ((".py",), "tree_sitter_python", "language"),
    "typescript": ((".ts", ".tsx"), "tree_sitter_typescript", "language_typescript"),
}
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
}


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
        if name_node is not None:
            name = content[name_node.start_byte : name_node.end_byte].decode("utf-8")
            symbol = ".".join((*scope, name))
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
    """Yield (specifier, kind, names, local names) for an import or re-export."""
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


def extract_imports(
    tree, content: bytes, repository_id: str, file: str, language: str
) -> list[Import]:
    """Static import statements in source order; dynamic loads are not facts here."""
    visit = _python_imports if language == "python" else _typescript_imports
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
