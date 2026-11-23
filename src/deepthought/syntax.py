# Definition mappings and structural name-field extraction adapted from
# DeepThought's Tree-sitter analyzer (Copyright (c) 2025 DeepThought, MIT).
# See licenses/deepthought-original-MIT.txt. Graph/ML/database coupling removed.
"""Explicit selected grammars and structural definitions; no semantic guessing."""

import importlib
from hashlib import sha256
import json

from .models import Definition

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


def extract(
    tree, content: bytes, repository_id: str, file: str, *, declaration_context=None
) -> list[Definition]:
    definitions = []
    ordinals = {}
    stack = [(tree.root_node, (), False, None, 0)]
    lines = content.split(b"\n") if declaration_context is not None else None
    file_digest = (
        sha256(content).hexdigest() if declaration_context is not None else None
    )
    while stack:
        node, scope, in_class, parent_id, lod = stack.pop()
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
            definitions.append(definition)
            if declaration_context is not None:
                source_node = node
                if node.parent is not None and node.parent.type in {
                    "decorated_definition",
                    "export_statement",
                }:
                    source_node = node.parent
                ranges = {}
                for encoding, width in (("utf-8", 1), ("utf-16", 2), ("utf-32", 4)):
                    columns = []
                    for point in (name_node.start_point, name_node.end_point):
                        prefix = lines[point.row][: point.column].decode("utf-8")
                        columns.append(
                            len(prefix.encode(encoding + "-le")) // width
                            if encoding != "utf-8"
                            else point.column
                        )
                    ranges[encoding] = [
                        name_node.start_point.row + 1,
                        columns[0],
                        name_node.end_point.row + 1,
                        columns[1],
                    ]
                source_code = content[
                    source_node.start_byte : source_node.end_byte
                ].decode("utf-8")
                declaration_context[definition.id] = {
                    "source_context_schema": "1",
                    "source_code": source_code,
                    "source_sha256": sha256(source_code.encode()).hexdigest(),
                    "file_sha256": file_digest,
                    "source_start_line": source_node.start_point.row + 1,
                    "source_start_column": source_node.start_point.column,
                    "source_end_line": source_node.end_point.row + 1,
                    "source_end_column": source_node.end_point.column,
                    "name_ranges": ranges,
                    "parent_definition_id": parent_id,
                    "lod": lod,
                }
            parent_id = definition.id
            lod += 1
            scope = (*scope, name)
            in_class = kind == "Class"
        stack.extend(
            (child, scope, in_class, parent_id, lod)
            for child in reversed(node.named_children)
        )
    return definitions
