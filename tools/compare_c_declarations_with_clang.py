"""Compare C or C++ declarations with clang's (libclang).

Every file of the language under ROOT is parsed by clang as a translation
unit (one with a fatal error, such as a missing generated header, is left
out), with each directory holding a header, and each one above it, on the
include path. Every declaration clang reports at file scope in a repository
file (and every struct or union declared inside one) is collected across
translation units:
functions (definitions and prototypes), named structs, unions and enums with
a body, typedef names and macros; for C++ also namespaces, classes, methods,
constructors, destructors, conversion functions, templates of these and
``using`` aliases, in namespaces and class bodies. For C++ a matched
declaration whose symbol differs from clang's qualified name (its semantic
parents joined with ``.``) is counted as ``matched_scope_differs``. A Tree-sitter declaration matches when
clang reports one of the same kind and name at its name's line in the same
file. Each difference is classified:

- Tree-sitter only: in a preprocessor branch clang skipped (``#if``) in
  every translation unit, in a file no translation unit reports any declaration in, or in
  a region Tree-sitter could not parse;
- clang only: written by a macro (the name is not written at clang's
  location), a type declared in a function body, or in a region Tree-sitter
  could not parse;
- either: on a line where clang reported an error in some translation unit
  (a type from a header generated at build time), or, for Tree-sitter, a
  name that is also a macro (``#define f g`` before ``void f(void)``, which
  clang declares as ``g``).

Usage:
  pip install libclang==18.1.1
  python tools/compare_c_declarations_with_clang.py ROOT [--language cpp]
"""

import argparse
from collections import Counter
import ctypes
import json
from pathlib import Path
import re
import subprocess

import clang.cindex as ci

import deepthought
from deepthought.syntax import extract_c, extract_cpp, make_parser, prepare_source

_KINDS = {
    "FUNCTION_DECL": "Function",
    "STRUCT_DECL": "Struct",
    "UNION_DECL": "Union",
    "ENUM_DECL": "Enum",
    "TYPEDEF_DECL": "TypeAlias",
    "MACRO_DEFINITION": "Macro",
    # C++
    "NAMESPACE": "Namespace",
    "CLASS_DECL": "Class",
    "CXX_METHOD": "Method",
    "CONSTRUCTOR": "Method",
    "DESTRUCTOR": "Method",
    "CONVERSION_FUNCTION": "Method",
    "TYPE_ALIAS_DECL": "TypeAlias",
    "TYPE_ALIAS_TEMPLATE_DECL": "TypeAlias",
}
_TEMPLATES = {"CLASS_TEMPLATE", "CLASS_TEMPLATE_PARTIAL_SPECIALIZATION"}
_RECORDS = {"Class", "Struct", "Union"}
_SCOPES = {"NAMESPACE", "CLASS_DECL", "STRUCT_DECL", "UNION_DECL", "ENUM_DECL"}
_HEADERS = (".h", ".hh", ".hpp", ".hxx", ".h++", ".ipp", ".inl")


def _kind(cursor):
    """The Tree-sitter kind of a clang declaration, or None."""
    name = cursor.kind.name
    if name in _TEMPLATES:
        name = ci.CursorKind.from_id(
            ci.conf.lib.clang_getTemplateCursorKind(cursor)
        ).name
    if name == "FUNCTION_TEMPLATE":
        parent = cursor.semantic_parent
        return (
            "Method" if parent is not None and _kind(parent) in _RECORDS else "Function"
        )
    return _KINDS.get(name)


def _qualified(cursor):
    """clang's qualified name of a declaration, its named namespaces and
    classes joined with ``.``, and whether one of them is not written where
    clang locates it (a namespace opened by a macro)."""
    parts, hidden = [_spelling(cursor)], False
    parent = cursor.semantic_parent
    while parent is not None and parent.kind.name != "TRANSLATION_UNIT":
        name = parent.kind.name
        if name in _TEMPLATES:
            name = ci.CursorKind.from_id(
                ci.conf.lib.clang_getTemplateCursorKind(parent)
            ).name
        if name in _SCOPES and parent.spelling and not parent.is_anonymous():
            parts.append(parent.spelling)
            hidden = hidden or not _written_at(parent.location, parent.spelling)
        parent = parent.semantic_parent
    return ".".join(reversed(parts)), hidden


def _spelling(cursor):
    """A declaration's name; clang spells a class template's constructors
    and destructor with the template's arguments (``C<T>``)."""
    name = cursor.spelling
    bare = re.sub(r"<.*>$", "", name)
    parent = cursor.semantic_parent
    if bare != name and parent is not None and bare.lstrip("~") == parent.spelling:
        name = bare
    return _conversion(name)


def _conversion(name):
    """A conversion function's name; clang spells its type canonically
    (``operator basic_string_view<char>`` for ``operator string_view``), so
    conversion functions are compared by kind and line alone."""
    if re.match(r"operator (?!(new|delete|co_await)\b)", name):
        return "operator (conversion)"
    return name


def _written_at(location, name):
    if location.file is None:
        return False
    lines = _lines(location.file.name)
    text = lines[location.line - 1] if location.line <= len(lines) else b""
    return text[location.column - 1 :].startswith(name.encode())


_CONTENTS = {}


def _lines(path):
    if path not in _CONTENTS:
        _CONTENTS[path] = Path(path).read_bytes().split(b"\n")
    return _CONTENTS[path]


class _RangeList(ctypes.Structure):
    _fields_ = [("count", ctypes.c_uint), ("ranges", ctypes.POINTER(ci.SourceRange))]


def _skipped(unit, root):
    """(file, first line, last line) of each preprocessor branch clang
    skipped in a repository file."""
    function = ci.conf.lib.clang_getAllSkippedRanges
    function.restype = ctypes.POINTER(_RangeList)
    function.argtypes = [ctypes.c_void_p]
    ranges = function(unit.obj)
    found = set()
    try:
        for index in range(ranges.contents.count):
            item = ranges.contents.ranges[index]
            if item.start.file is None:
                continue
            try:
                file = Path(item.start.file.name).resolve().relative_to(root)
            except ValueError:
                continue
            found.add((file.as_posix(), item.start.line, item.end.line))
    finally:
        ci.conf.lib.clang_disposeSourceRangeList(ranges)
    return found


def clang_declarations(root, files, language="c"):
    resource = subprocess.check_output(
        ["clang", "-print-resource-dir"], text=True
    ).strip()
    # Each directory holding a header, and each directory above one, so
    # that `#include "lib/x.h"` is found from the project's include root.
    includes = sorted(
        {
            str(root / parent)
            for f in files
            if f.endswith(".h")
            for parent in Path(f).parents
        }
    )
    args = ["-ferror-limit=0", "-w", "-resource-dir", resource]
    if language == "cpp":
        args.append("-std=c++20")
    args += [f"-I{d}" for d in includes]
    index = ci.Index.create()
    found, skipped, errors, fatal = set(), set(), set(), []
    qualified = {}

    def visit(cursor):
        kind = _kind(cursor)
        location = cursor.location
        if cursor.kind.name in {"LINKAGE_SPEC", "FRIEND_DECL"} or (
            kind == "Namespace" and cursor.is_anonymous()
        ):
            for child in cursor.get_children():
                visit(child)
            return
        if kind is None or location.file is None:
            return
        try:
            relative = Path(location.file.name).resolve().relative_to(root)
        except ValueError:
            return
        if not (
            kind in {"Class", "Struct", "Union", "Enum"}
            and (
                cursor.is_anonymous()
                or not cursor.is_definition()
                # An unnamed struct spelled by the typedef naming it.
                or _keyword_at(location)
            )
        ):
            key = (
                relative.as_posix(),
                location.line,
                location.column - 1,
                _spelling(cursor),
                kind,
            )
            found.add(key)
            if language == "cpp":
                qualified.setdefault(key[:2] + key[3:], set()).add(_qualified(cursor))
        if kind in _RECORDS or kind == "Namespace":
            for child in cursor.get_children():
                visit(child)

    for file in files:
        unit = index.parse(
            str(root / file),
            args=_mode(file, language) + args,
            options=ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
            | ci.TranslationUnit.PARSE_SKIP_FUNCTION_BODIES,
        )
        if any(d.severity == ci.Diagnostic.Fatal for d in unit.diagnostics):
            # An include not found (a header generated at build time): what
            # clang recovers after it is not the program.
            fatal.append(file)
            continue
        for cursor in unit.cursor.get_children():
            visit(cursor)
        skipped |= _skipped(unit, root)
        for diagnostic in unit.diagnostics:
            where = diagnostic.location
            if diagnostic.severity < ci.Diagnostic.Error or where.file is None:
                continue
            try:
                file = Path(where.file.name).resolve().relative_to(root)
            except ValueError:
                continue
            errors.add((file.as_posix(), where.line))
    return found, skipped, errors, fatal, qualified


def _mode(file, language):
    header = file.endswith(_HEADERS)
    if language == "cpp":
        return ["-x", "c++-header" if header else "c++"]
    return ["-x", "c-header"] if header else []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--language", choices=("c", "cpp"), default="c")
    args = parser.parse_args()
    root = args.root.resolve()
    language = args.language
    result = deepthought.analyze_repository(
        root, repository_id="comparison", languages=(language,)
    )
    files = sorted(c.file for c in result.coverage if c.language == language)
    clang, skipped, errors, fatal, qualified = clang_declarations(root, files, language)
    macros = {d.symbol for d in result.definitions if d.kind == "Macro"}
    skipped_lines = {}
    for file, first, last in skipped:
        skipped_lines.setdefault(file, []).append((first, last))
    by_key = {}
    for file, line, column, name, kind in clang:
        by_key.setdefault((file, line, name, kind), column)
    reported = {item[0] for item in clang}
    source_parser = make_parser(language, ".c" if language == "c" else ".cc")
    extract = extract_c if language == "c" else extract_cpp
    ours, symbols, trees, contents = {}, {}, {}, {}
    for file in files:
        content = (root / file).read_bytes()
        tree = source_parser.parse(prepare_source(language, content))
        nodes = {}
        declared = extract(tree, content, "comparison", file, nodes=nodes)
        trees[file], contents[file] = tree, content
        for definition in declared:
            node, name = nodes[definition.id]
            short = (
                definition.symbol
                if definition.kind == "Macro"
                else (definition.symbol.rsplit(".", 1)[-1])
            )
            key = (file, name.start_point.row + 1, _conversion(short), definition.kind)
            ours[key] = node
            head, _, last = definition.symbol.rpartition(".")
            symbols.setdefault(key, set()).add(
                ".".join(filter(None, (head, _conversion(last))))
            )
    counts, examples = Counter(), {}

    def note(outcome, example):
        counts[outcome] += 1
        examples.setdefault(outcome, []).append(example)

    seen = set()
    for key, node in sorted(ours.items(), key=lambda i: i[0]):
        file, line, name, kind = key
        seen.add(key)
        if key in by_key:
            counts["matched"] += 1
            names = {name for name, _ in qualified.get(key, ())}
            if language == "cpp" and not symbols[key] & names:
                if any(hidden for _, hidden in qualified[key]):
                    outcome = "matched_scope_differs_macro_scope"
                elif _in_error(node) or _error_before(trees[file], node):
                    outcome = "matched_scope_differs_parse_error"
                else:
                    outcome = "matched_scope_differs"
                note(outcome, [file, line, sorted(symbols[key]), sorted(names)])
            continue
        if any(first <= line <= last for first, last in skipped_lines.get(file, ())):
            outcome = "tree_sitter_only_inactive_branch"
        elif file not in reported:
            outcome = "tree_sitter_only_file_not_reported"
        elif (file, line) in errors:
            outcome = "tree_sitter_only_clang_error"
        elif name in macros:
            # `#define f g` then `void f(void)`: clang declares g.
            outcome = "tree_sitter_only_name_is_macro"
        elif _in_error(node):
            outcome = "tree_sitter_only_parse_error"
        elif (file, line, name, _OTHER.get(kind)) in by_key and _error_before(
            trees[file], node
        ):
            # A grammar error before it closed or opened a class early.
            outcome = "tree_sitter_only_kind_after_parse_error"
        elif kind == "Method" and (file, line, name, "Function") in by_key:
            # `void n::f() {}` in a file that does not open `n`.
            outcome = "tree_sitter_only_namespace_qualified_function"
        else:
            outcome = "tree_sitter_only_unexplained"
        note(outcome, [file, line, name, kind])
    for (file, line, name, kind), column in sorted(by_key.items()):
        if (file, line, name, kind) in seen:
            continue
        content = contents.get(file)
        lines = content.split(b"\n") if content is not None else []
        text = lines[line - 1] if line <= len(lines) else b""
        written = re.match(rb"[A-Za-z_~][A-Za-z0-9_]*", text[column:]) and (
            text[column:].startswith(name.encode())
            or (name.startswith("operator") and text[column:].startswith(b"operator"))
        )
        if (file, line) in errors:
            outcome = "clang_only_clang_error"
        elif (file, line, name, _OTHER.get(kind)) in ours and _error_before(
            trees[file], ours[(file, line, name, _OTHER.get(kind))]
        ):
            outcome = "clang_only_kind_after_parse_error"
        elif kind == "Function" and (file, line, name, "Method") in ours:
            outcome = "clang_only_namespace_qualified_function"
        elif file in trees and _in_body(trees[file], line, column):
            # Types declared in a function body are left out.
            outcome = "clang_only_in_function_body"
        elif not written:
            outcome = "clang_only_macro_generated"
        elif file in trees and _error_at(trees[file], line, column):
            outcome = "clang_only_parse_error"
        elif file in trees and _expression_at(trees[file], line, column):
            # `int(f)(int);` reads as a call: C++ is ambiguous there.
            outcome = "clang_only_read_as_expression"
        else:
            outcome = "clang_only_unexplained"
        note(outcome, [file, line, name, kind])
    print(
        json.dumps(
            {
                "translation_units": len(files),
                "translation_units_with_fatal_error": len(fatal),
                "tree_sitter": len(ours),
                "clang": len(by_key),
                **dict(sorted(counts.items())),
                "examples": {k: v[:15] for k, v in sorted(examples.items())},
            },
            indent=2,
        )
    )


def _keyword_at(location):
    lines = Path(location.file.name).read_bytes().split(b"\n")
    text = lines[location.line - 1][location.column - 1 :]
    return re.match(rb"(struct|union|enum|class)\b", text) is not None


def _text(node, content):
    return content[node.start_byte : node.end_byte].decode("utf-8")


def _in_error(node):
    """Whether the file-scope item holding a node (the node itself, or one
    of its ancestors below the root) contains text the grammar could not
    parse, or the node lies in an error node."""
    while node is not None:
        if node.type == "ERROR" or (node.has_error and node.parent is not None):
            return True
        node = node.parent
    return False


def _expression_at(tree, line, column):
    """Whether the grammar read a declaration outside function bodies as an
    expression statement, which only a function body can hold."""
    node = tree.root_node.descendant_for_point_range(
        (line - 1, column), (line - 1, column)
    )
    while node is not None and node.type != "expression_statement":
        node = node.parent
    return node is not None and not _in_body(tree, line, column)


_OTHER = {"Function": "Method", "Method": "Function"}
_FIRST_ERROR = {}


def _error_before(tree, node):
    """Whether the grammar could not parse some text before a node: the
    enclosing namespaces and classes of C++ code depend on every brace
    before it."""
    if id(tree) not in _FIRST_ERROR:
        first, stack = None, [tree.root_node]
        while stack:
            current = stack.pop()
            if not current.has_error and not current.is_missing:
                continue
            if current.type == "ERROR" or current.is_missing:
                if first is None or current.start_byte < first:
                    first = current.start_byte
                continue
            stack.extend(current.children)
        _FIRST_ERROR[id(tree)] = first
    first = _FIRST_ERROR[id(tree)]
    return first is not None and first < node.start_byte


def _in_body(tree, line, column):
    node = tree.root_node.descendant_for_point_range(
        (line - 1, column), (line - 1, column)
    )
    while node is not None:
        if node.type == "compound_statement":
            return True
        node = node.parent
    return False


def _error_at(tree, line, column):
    node = tree.root_node.descendant_for_point_range(
        (line - 1, column), (line - 1, column)
    )
    if node is None:
        return False
    if _in_error(node):
        return True
    # The grammar may end the item before with a missing token and read
    # the declaration as an expression: `A B *f(...);` with A a macro.
    while node.parent is not None and node.parent.parent is not None:
        node = node.parent
    before = node.prev_named_sibling
    return before is not None and before.has_error


if __name__ == "__main__":
    main()
