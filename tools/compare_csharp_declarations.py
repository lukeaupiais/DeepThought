"""Check syntax.extract_csharp against an independent walk of the same trees.

Each type, method and constructor node is named from its ancestors
(namespaces and enclosing types) instead of the extractor's scope stack, and
declarations inside bodies (blocks, lambdas, local functions) are dropped.
Type keywords are also counted lexically, with comments and strings removed.

Usage: python tools/compare_csharp_declarations.py <directory>
"""

import re
import sys
from pathlib import Path

from deepthought.syntax import extract_csharp, make_parser

TYPES = {
    "class_declaration": "Class",
    "struct_declaration": "Struct",
    "interface_declaration": "Interface",
    "record_declaration": "Record",
    "enum_declaration": "Enum",
}
MEMBERS = {"method_declaration": "Method", "constructor_declaration": "Constructor"}
BODIES = {
    "block",
    "arrow_expression_clause",
    "lambda_expression",
    "anonymous_method_expression",
    "local_function_statement",
}
KEYWORDS = re.compile(
    rb"\b(class|struct|interface|enum|record)\s+[A-Za-z_]\w*\s*(<[^>]*>)?\s*[:{(\n]"
)
NOISE = re.compile(rb'//[^\n]*|/\*.*?\*/|@?"(?:[^"\\]|\\.)*"', re.S)


def _name(node, source):
    name = node.child_by_field_name("name")
    return source[name.start_byte : name.end_byte].decode().replace(" ", "")


def _walk(node):
    yield node
    for child in node.named_children:
        yield from _walk(child)


def reference(tree, source):
    """(symbol, kind, line) of each declaration, named from its ancestors."""
    scoped = [
        c
        for c in tree.root_node.named_children
        if c.type == "file_scoped_namespace_declaration"
    ]
    found = []
    for node in _walk(tree.root_node):
        kind = TYPES.get(node.type) or MEMBERS.get(node.type)
        if kind is None:
            continue
        parts, inside, ancestor = [], False, node.parent
        while ancestor is not None:
            if ancestor.type in BODIES:
                inside = True
            if ancestor.type in TYPES or ancestor.type == "namespace_declaration":
                parts.append(_name(ancestor, source))
            ancestor = ancestor.parent
        if inside or (node.type in MEMBERS and not parts):
            continue
        parts.extend(_name(n, source) for n in scoped if n.start_byte < node.start_byte)
        symbol = ".".join([*reversed(parts), _name(node, source)])
        found.append((symbol, kind, node.start_point.row + 1))
    return sorted(found)


def main(directory):
    root = Path(directory)
    parser = make_parser("csharp", ".cs")
    differences = total = errors = lexical = extracted = 0
    for path in sorted(root.rglob("*.cs")):
        source = path.read_bytes()
        tree = parser.parse(source)
        errors += tree.root_node.has_error
        ours = sorted(
            (d.symbol, d.kind, d.line)
            for d in extract_csharp(
                tree, source, "r", path.relative_to(root).as_posix()
            )
        )
        expected = reference(tree, source)
        total += len(expected)
        if ours != expected:
            differences += 1
            print(path, sorted(set(ours) ^ set(expected)))
        lexical += len(KEYWORDS.findall(NOISE.sub(b"", source)))
        extracted += sum(1 for d in ours if d[1] in TYPES.values())
    print(
        f"declarations {total}, files with differences {differences}, "
        f"files with grammar errors {errors}, "
        f"types lexical {lexical} / extracted {extracted}"
    )
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
