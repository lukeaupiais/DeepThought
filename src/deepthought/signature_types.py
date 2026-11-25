"""The types a function's signature names, from compiler-backed SCIP references.

A function or method's signature is its declaration up to its body: the
parameters with their annotations and defaults, the result type, type
parameters and, for a function assigned to a variable, the variable's
annotation. The parser gives that span (the tree-sitter node of the
declaration, up to its ``body``; all of it for a signature without a body).
Every SCIP reference an index records inside the span is read back to the
parsed declaration its symbol is defined at (``deepthought.scip_calls``),
and the declarations that are types (classes, interfaces, structs, type
aliases, enums) are the types the signature names. A function's own name
and its parameters are definitions, not references, and are skipped.

Only files an index covers are read; a type declared outside the
repository (a library's) has no parsed declaration and is not reported.
"""

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .scip_calls import _IndexView, _Lines, _declaration, _key, _to_utf8
from .syntax import LANGUAGES, make_parser, parse_source

# Declarations whose signatures are read, and the ones that are types.
FUNCTION_KINDS = frozenset({"Function", "Method", "Constructor"})
TYPE_KINDS = frozenset(
    {
        "Class",
        "Interface",
        "Struct",
        "Type",
        "TypeAlias",
        "Enum",
        "Record",
        "Trait",
        "Union",
    }
)
# The SCIP role bit of a definition occurrence.
_DEFINITION = 1


@dataclass(frozen=True)
class SignatureType:
    """A type a declaration's signature names, at its first reference."""

    definition_id: str
    type_id: str
    line: int
    column: int  # UTF-8 bytes


def _language(file):
    for language, (suffixes, _, _) in LANGUAGES.items():
        if file.endswith(suffixes):
            return language
    return None


def signature_spans(root, file, definitions):
    """{definition id: ((start line, column), (end line, column))}: the
    signature of each function-like definition of ``file`` (1-based lines,
    UTF-8 byte columns), up to its body."""
    language = _language(file)
    wanted = {(d.line, d.column): d for d in definitions if d.kind in FUNCTION_KINDS}
    if language is None or not wanted:
        return {}
    try:
        content = (Path(root) / file).read_bytes()
    except OSError:
        return {}
    suffix = Path(file).suffix
    tree = parse_source(make_parser(language, suffix), language, suffix, content)
    spans = {}
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        found = wanted.get((node.start_point.row + 1, node.start_point.column))
        if found is None or found.id in spans:
            continue
        if (node.end_point.row + 1, node.end_point.column) != (
            found.end_line,
            found.end_column,
        ):
            continue
        holder = node
        if node.type == "variable_declarator":
            holder = node.child_by_field_name("value") or node
        body = holder.child_by_field_name("body")
        end = body.start_point if body is not None else node.end_point
        spans[found.id] = (
            (node.start_point.row + 1, node.start_point.column),
            (end.row + 1, end.column),
        )
    return spans


def signature_types(result, indexes, root):
    """The ``SignatureType`` facts of ``result``'s definitions in the files
    ``indexes`` (pairs of an ingested SCIP index and its receipt digest, as
    ``bind_calls`` takes them) cover, sorted."""
    if not indexes:
        return []
    root = Path(root)
    lines = _Lines(root)
    in_file = defaultdict(list)
    for d in result.definitions:
        in_file[d.file].append(d)
    covering, shared_by_indexer = {}, {}
    for index, receipt in indexes:
        tool = next(
            (f"{n}@{v}" for n, v in index.analyzer_versions if n.startswith("scip-")),
            "scip",
        )
        view = _IndexView(index, tool, receipt)
        shared = shared_by_indexer.setdefault(tool.split("@", 1)[0], defaultdict(list))
        for key, found in view.definitions.items():
            shared[key].extend(found)
        view.definitions = shared
        for coverage in index.coverage:
            covering.setdefault(coverage.file, view)
    references = defaultdict(list)
    for view in {id(v): v for v in covering.values()}.values():
        for (file, _, _), found in view.references.items():
            if covering.get(file) is view:
                references[file].extend(found)
    facts = {}
    for file in sorted(references):
        view = covering[file]
        spans = signature_spans(root, file, in_file[file])
        if not spans:
            continue
        ordered = sorted(spans.items(), key=lambda item: item[1])
        for reference in references[file]:
            if reference.symbol_roles & _DEFINITION:
                continue
            text = lines.get(file, reference.line)
            point = (
                reference.line,
                _to_utf8(text, reference.column, view.encoding(reference)),
            )
            # The innermost signature holding it: a nested function's
            # signature lies inside its parent's body, never its signature.
            holders = [
                definition_id
                for definition_id, (start, end) in ordered
                if start <= point < end
            ]
            if not holders:
                continue
            owner = holders[-1]
            for scip in view.definitions.get(_key(file, reference.native_id), ()):
                parsed = _declaration(scip, view, lines, in_file)
                if parsed is None or parsed.kind not in TYPE_KINDS:
                    continue
                key = (owner, parsed.id)
                if key not in facts or point < (facts[key].line, facts[key].column):
                    facts[key] = SignatureType(owner, parsed.id, *point)
    return sorted(facts.values(), key=lambda f: (f.definition_id, f.type_id))
