"""Where a code name appears in source text, and in what kind of text.

A graph answer about one declaration can say whether a text search for its
name would find anything the graph does not account for. This module does
the text half of that check: it finds every line of a file that names the
identifier (as a whole word) and says whether each mention is code, a
comment or a string, from the file's own syntax tree (tree-sitter, the
grammars the analysis parses with). It knows nothing about the graph;
``GraphQueryService`` matches the lines against the snapshot's links.
"""

import re

from .indexers import LANGUAGE_SUFFIXES

CODE = "code"
COMMENT = "comment"
STRING = "string"

# Syntax nodes inside a string that hold code again (f-string and template
# literal substitutions).
_INTERPOLATIONS = frozenset(
    {"interpolation", "template_substitution", "string_interpolation"}
)

_parsers = {}


def _parser(language, suffix):
    key = (language, suffix == ".tsx")
    if key not in _parsers:
        from .syntax import make_parser

        try:
            _parsers[key] = make_parser(language, suffix)
        except (ImportError, KeyError, ValueError):
            _parsers[key] = None
    return _parsers[key]


def _kind(node):
    """What kind of text holds ``node``: the nearest enclosing comment or
    string, unless a substitution inside the string is nearer."""
    while node is not None:
        kind = node.type
        if kind in _INTERPOLATIONS:
            return CODE
        if "comment" in kind:
            return COMMENT
        if "string" in kind or kind in ("heredoc_body", "raw_string_content"):
            # A string that is a statement on its own is documentation (a
            # Python docstring), read like a comment.
            while node.parent is not None and "string" in node.parent.type:
                node = node.parent
            parent = node.parent
            if (
                parent is not None
                and parent.type == "expression_statement"
                and parent.named_child_count == 1
            ):
                return COMMENT
            return STRING
        node = node.parent
    return CODE


def mentions(path, content, name):
    """``{line: kind}`` for each 1-based line of ``content`` naming ``name``
    as a whole identifier. A line with any code mention is ``code``; else
    ``string`` when a string names it; else ``comment``. Without a grammar
    for the file's language every mention reads as code."""
    word = name.encode()
    if word not in content:
        return {}
    pattern = re.compile(rb"(?<![\w$])" + re.escape(word) + rb"(?![\w$])")
    found = [m.start() for m in pattern.finditer(content)]
    if not found:
        return {}
    suffix = "." + path.rpartition(".")[2] if "." in path else ""
    language = LANGUAGE_SUFFIXES.get(suffix)
    tree = None
    if language is not None:
        parser = _parser(language, suffix)
        if parser is not None:
            from .syntax import parse_source

            tree = parse_source(parser, language, suffix, content)
    lines = {}
    rank = {COMMENT: 0, STRING: 1, CODE: 2}
    for start in found:
        line = content.count(b"\n", 0, start) + 1
        if tree is None:
            kind = CODE
        else:
            node = tree.root_node.descendant_for_byte_range(start, start + len(word))
            kind = _kind(node)
        if rank[kind] > rank.get(lines.get(line), -1):
            lines[line] = kind
    return lines


def declaration_lines(source_code, start_line, name):
    """The lines a declaration's header spans up to its name: a mention
    of the name there is the declaration itself (decorators and modifiers
    may come first)."""
    if not source_code or start_line is None:
        return {start_line} if start_line is not None else set()
    pattern = re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])")
    for offset, text in enumerate(source_code.split("\n")[:40]):
        if pattern.search(text):
            return set(range(start_line, start_line + offset + 1))
    return {start_line}
