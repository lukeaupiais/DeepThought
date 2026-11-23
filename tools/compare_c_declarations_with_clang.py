"""Compare C declarations with clang's (libclang) at file scope.

Every ``.c`` and ``.h`` file under ROOT is parsed by clang as a translation
unit (one with a fatal error, such as a missing generated header, is left
out), with each directory holding a header, and each one above it, on the
include path. Every declaration clang reports at file scope in a repository
file (and every struct or union declared inside one) is collected across
translation units:
functions (definitions and prototypes), named structs, unions and enums with
a body, typedef names and macros. A Tree-sitter declaration matches when
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
  python tools/compare_c_declarations_with_clang.py ROOT
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
from deepthought.syntax import extract_c, make_parser

_KINDS = {
    "FUNCTION_DECL": "Function",
    "STRUCT_DECL": "Struct",
    "UNION_DECL": "Union",
    "ENUM_DECL": "Enum",
    "TYPEDEF_DECL": "TypeAlias",
    "MACRO_DEFINITION": "Macro",
}


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


def clang_declarations(root, files):
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
    args += [f"-I{d}" for d in includes]
    index = ci.Index.create()
    found, skipped, errors, fatal = set(), set(), set(), []

    def visit(cursor):
        kind = _KINDS.get(cursor.kind.name)
        location = cursor.location
        if kind is None or location.file is None:
            return
        try:
            relative = Path(location.file.name).resolve().relative_to(root)
        except ValueError:
            return
        if not (
            kind in {"Struct", "Union", "Enum"}
            and (
                cursor.is_anonymous()
                or not cursor.is_definition()
                # An unnamed struct spelled by the typedef naming it.
                or _keyword_at(location)
            )
        ):
            found.add(
                (
                    relative.as_posix(),
                    location.line,
                    location.column - 1,
                    cursor.spelling,
                    kind,
                )
            )
        if kind in {"Struct", "Union"}:
            for child in cursor.get_children():
                visit(child)

    for file in files:
        unit = index.parse(
            str(root / file),
            args=(["-x", "c-header"] if file.endswith(".h") else []) + args,
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
    return found, skipped, errors, fatal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    result = deepthought.analyze_repository(
        root, repository_id="comparison", languages=("c",)
    )
    files = sorted(c.file for c in result.coverage if c.language == "c")
    clang, skipped, errors, fatal = clang_declarations(root, files)
    macros = {d.symbol for d in result.definitions if d.kind == "Macro"}
    skipped_lines = {}
    for file, first, last in skipped:
        skipped_lines.setdefault(file, []).append((first, last))
    by_key = {}
    for file, line, column, name, kind in clang:
        by_key.setdefault((file, line, name, kind), column)
    reported = {item[0] for item in clang}
    c_parser = make_parser("c", ".c")
    ours, trees, contents = {}, {}, {}
    for file in files:
        content = (root / file).read_bytes()
        tree = c_parser.parse(content)
        nodes = {}
        extract_c(tree, content, "comparison", file, nodes=nodes)
        trees[file], contents[file] = tree, content
        for definition_id, (node, name) in nodes.items():
            key = (file, name.start_point.row + 1, _text(name, content), _kind(node))
            ours[key] = node
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
        else:
            outcome = "tree_sitter_only_unexplained"
        note(outcome, [file, line, name, kind])
    for (file, line, name, kind), column in sorted(by_key.items()):
        if (file, line, name, kind) in seen:
            continue
        content = contents.get(file)
        lines = content.split(b"\n") if content is not None else []
        text = lines[line - 1] if line <= len(lines) else b""
        written = re.match(rb"[A-Za-z_][A-Za-z0-9_]*", text[column:]) and text[
            column:
        ].startswith(name.encode())
        if (file, line) in errors:
            outcome = "clang_only_clang_error"
        elif file in trees and _in_body(trees[file], line, column):
            # Types declared in a function body are left out.
            outcome = "clang_only_in_function_body"
        elif not written:
            outcome = "clang_only_macro_generated"
        elif file in trees and _error_at(trees[file], line, column):
            outcome = "clang_only_parse_error"
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
    return re.match(rb"(struct|union|enum)\b", text) is not None


def _text(node, content):
    return content[node.start_byte : node.end_byte].decode("utf-8")


_NODE_KINDS = {
    "function_definition": "Function",
    "declaration": "Function",
    "type_definition": "TypeAlias",
    "preproc_def": "Macro",
    "preproc_function_def": "Macro",
    "struct_specifier": "Struct",
    "union_specifier": "Union",
    "enum_specifier": "Enum",
}


def _kind(node):
    return _NODE_KINDS[node.type]


def _in_error(node):
    while node is not None:
        if node.has_error or node.type == "ERROR":
            return True
        node = node.parent
    return False


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
    return _in_error(node) if node is not None else False


if __name__ == "__main__":
    main()
