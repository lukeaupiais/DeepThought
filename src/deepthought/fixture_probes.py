"""Types for pytest fixture parameters, from scip-python itself.

A test receives a fixture's value through a parameter without an annotation
(``def test_x(app): app.route(...)``), so pyright gives the parameter no
type and scip-python records nothing at ``route``, nor at anything reached
from it (``client = app.test_client(); client.get(...)``). pytest decides
which fixture fills the parameter (``deepthought.pytest_fixtures`` follows
its lookup), and the fixture function returns or yields the value.

So the tests are indexed a second time, in a copy of the workspace's Python
files where each such parameter has the fixture's value as its default
(``def test_x(*, app=_y(_f0())):``, with ``_f0`` imported from the
fixture's module at the end of an earlier line): pyright types a parameter
from its default, so the indexer, not a rule, types what the fixture gives.
A fixture that requests fixtures is rewritten the same way, so its own value
is typed. ``*`` makes the parameters after it keyword-only, so the ones
without a default stay valid; ``_y``, ``_a`` and ``_ay`` take what a
generator yields, a coroutine returns and an async generator yields. The
fixture decorators are blanked out (spaces): pytest's ``fixture`` hides the
function behind a ``FixtureFunctionDefinition``.

Every edit adds text at the end of a line or within a ``def`` signature, so
the rest of each file keeps its positions. ``merge`` adds the second index's
global symbols, outside the edited spans, at the places the first index has
none. Not rewritten: parameters with an annotation (pyright reads it) or that
the function assigns, functions with ``*args`` or ``/`` after such a
parameter, fixtures defined in a class, fixtures pytest plugins provide
(``monkeypatch``, ``tmp_path``), files without a simple statement before the
first test whose line can take an import, and files Python's ``ast`` cannot
parse.
"""

import ast
import os
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .pytest_fixtures import _FUNCTIONS, fixture_uses

HELPERS = "_deepthought_fixture_probes"
HELPERS_TEXT = """\
from typing import AsyncIterator, Awaitable, Iterator, TypeVar

T = TypeVar("T")


def yielded(value: Iterator[T]) -> T: ...


def awaited(value: Awaitable[T]) -> T: ...


def ayielded(value: AsyncIterator[T]) -> T: ...
"""
_IMPORT_HELPERS = f"from {HELPERS} import yielded as _y, awaited as _a, ayielded as _ay"
_COMPOUND = (
    *_FUNCTIONS,
    ast.ClassDef,
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    *((ast.TryStar,) if hasattr(ast, "TryStar") else ()),
    *((ast.Match,) if hasattr(ast, "Match") else ()),
)


@dataclass
class Rewrite:
    """The copy's rewritten files and, per file, the first column (UTF-16)
    each 0-based line was edited at."""

    files: dict = field(default_factory=dict)
    edited: dict = field(default_factory=dict)
    parameters: int = 0


def _utf16_column(line, byte_column):
    prefix = line.encode("utf-8")[:byte_column].decode("utf-8", "replace")
    return len(prefix.encode("utf-16-le")) // 2


def _module_name(path):
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if not parts or not all(p.isidentifier() for p in parts):
        return None
    return ".".join(parts)


def _functions(tree):
    """Every function in ``tree`` by its ``def`` line, with whether it is
    declared in a class."""
    found = {}

    def visit(node, in_class):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _FUNCTIONS):
                found[child.lineno] = (child, in_class)
                visit(child, False)
            elif isinstance(child, ast.ClassDef):
                visit(child, True)
            else:
                visit(child, in_class)

    visit(tree, False)
    return found


def _assigned(function):
    """Names ``function`` rebinds, or a function inside it binds."""
    names = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif node is not function and isinstance(node, (*_FUNCTIONS, ast.Lambda)):
            arguments = node.args
            names.update(
                a.arg
                for a in arguments.posonlyargs
                + arguments.args
                + arguments.kwonlyargs
                + [x for x in (arguments.vararg, arguments.kwarg) if x]
            )
    return names


def _yields(function):
    """Whether ``function`` itself (not a function inside it) yields."""
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return True
        if not isinstance(node, (*_FUNCTIONS, ast.Lambda, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))
    return False


def _decorator_blanks(lines, function):
    """(line, start, end) byte spans of ``function``'s decorators."""
    spans = []
    for decorator in function.decorator_list:
        first, last = decorator.lineno - 1, decorator.end_lineno - 1
        at = lines[first].encode("utf-8").rfind(b"@", 0, decorator.col_offset + 1)
        if at < 0:
            continue
        for number in range(first, last + 1):
            raw = lines[number].encode("utf-8")
            end = decorator.end_col_offset if number == last else len(raw)
            spans.append((number, at if number == first else 0, end))
    return spans


def _import_line(tree, lines, before):
    """The 0-based line, and its byte length, of the first top-level simple
    statement ending before line ``before`` (1-based) with nothing after it
    on its line, where an import can be appended."""
    for statement in tree.body:
        if statement.end_lineno >= before:
            return None
        if isinstance(statement, _COMPOUND):
            continue
        raw = lines[statement.end_lineno - 1].encode("utf-8")
        if raw[statement.end_col_offset :].strip():
            continue
        return statement.end_lineno - 1, len(raw.rstrip())
    return None


def plan(sources):
    """The rewrite of ``sources`` (repository-relative path to Python text)
    that gives each fixture parameter its fixture's value as default."""
    trees = {}
    for path, text in sources.items():
        try:
            trees[path] = ast.parse(text)
        except (SyntaxError, ValueError):
            continue
    requested = {}
    for use in fixture_uses(sources):
        if use.rule == "parameter":
            requested.setdefault(use.consumer, {})[use.name] = use.fixture
    functions = {path: _functions(tree) for path, tree in trees.items()}
    lines = {path: sources[path].splitlines(keepends=True) for path in trees}
    edits = {}  # path -> [(line, byte column, text)]
    blanks = {}  # path -> {(line, start, end)}
    imports = {}  # path -> {fixture place: alias}
    first_use = {}  # path -> 1-based line of the first rewritten function

    def value(path, place):
        """The default expression for the value fixture ``place`` gives to
        a function of ``path``, or None."""
        found = functions.get(place[0], {}).get(place[1])
        module = _module_name(place[0])
        if found is None or found[1] or module is None:
            return None
        function = found[0]
        blanks.setdefault(place[0], set()).update(
            _decorator_blanks(lines[place[0]], function)
        )
        aliases = imports.setdefault(path, {})
        alias = aliases.setdefault(place, (f"_f{len(aliases)}", module, function.name))
        call = f"{alias[0]}()"
        asynchronous = isinstance(function, ast.AsyncFunctionDef)
        if _yields(function):
            return f"_ay({call})" if asynchronous else f"_y({call})"
        return f"_a({call})" if asynchronous else call

    rewrite = Rewrite()
    for (path, line), names in sorted(requested.items()):
        found = functions.get(path, {}).get(line)
        if found is None:
            continue
        function = found[0]
        arguments = function.args
        assigned = _assigned(function)
        positional = arguments.posonlyargs + arguments.args
        candidates = [
            a
            for a in positional + arguments.kwonlyargs
            if a.arg in names and a.annotation is None and a.arg not in assigned
        ]
        defaults = [(a, value(path, names[a.arg])) for a in candidates]
        defaults = [(a, d) for a, d in defaults if d is not None]
        given = {a for a, _ in defaults}
        # Positional parameters without a default may not follow one with a
        # default: `*` makes them keyword-only, unless the signature has its
        # own `*` or `*args`, or the first is positional-only.
        first = next((a for a in positional if a in given), None)
        star = first is not None and any(
            a not in given
            for a in positional[positional.index(first) :][
                : len(positional) - positional.index(first) - len(arguments.defaults)
            ]
        )
        if first is not None and (
            first in arguments.posonlyargs
            or (star and (arguments.vararg is not None or arguments.kwonlyargs))
        ):
            defaults = [(a, d) for a, d in defaults if a not in positional]
            star = False
        if not defaults:
            continue
        own = edits.setdefault(path, [])
        if star:
            own.append((first.lineno - 1, first.col_offset, "*, "))
        for argument, default in defaults:
            own.append(
                (argument.end_lineno - 1, argument.end_col_offset, "=" + default)
            )
        rewrite.parameters += len(defaults)
        start = min([function.lineno] + [d.lineno for d in function.decorator_list])
        first_use[path] = min(first_use.get(path, start), start)
    for path in sorted(edits):
        anchor = _import_line(trees[path], lines[path], first_use[path])
        if anchor is None:
            rewrite.parameters -= sum(1 for e in edits[path] if e[2] != "*, ")
            del edits[path]
            continue
        statements = [_IMPORT_HELPERS] + [
            f"from {module} import {name} as {alias}"
            for alias, module, name in sorted(imports[path].values())
        ]
        edits[path].append((anchor[0], anchor[1], "; " + "; ".join(statements)))
    for path in sorted(set(edits) | set(blanks)):
        text = list(lines[path])
        for number, start, end in sorted(blanks.get(path, ())):
            raw = text[number].encode("utf-8")
            raw = raw[:start] + b" " * (end - start) + raw[end:]
            text[number] = raw.decode("utf-8")
        edited = {}
        for number, column, insert in sorted(
            edits.get(path, ()), key=lambda e: (e[0], -e[1])
        ):
            line_text = text[number]
            raw = line_text.encode("utf-8")
            edited[number] = min(
                edited.get(number, 1 << 30), _utf16_column(line_text, column)
            )
            text[number] = (
                raw[:column] + insert.encode("utf-8") + raw[column:]
            ).decode("utf-8")
        rewrite.files[path] = "".join(text)
        rewrite.edited[path] = edited
    return rewrite


def target(rewrite, documents):
    """The directory the second index covers: the deepest one holding every
    rewritten file among ``documents`` ("." for the workspace), or None
    when none is."""
    paths = sorted(
        p for p, edited in rewrite.edited.items() if edited and p in documents
    )
    if not paths:
        return None
    common = os.path.commonpath([str(PurePosixPath(p).parent) for p in paths])
    return PurePosixPath(common).as_posix() if common else "."


def merge(index, rewritten, rewrite, directory):
    """Add to ``index``'s documents the global symbols ``rewritten`` (the
    index of the rewritten copy, limited to ``directory``) records outside
    the edited spans where ``index`` has no occurrence. Returns how many."""
    from ._scip import scip_pb2

    documents = {d.relative_path: d for d in index.documents}
    added = 0
    for document in rewritten.documents:
        path = (
            document.relative_path
            if directory == "."
            else f"{directory}/{document.relative_path}"
        )
        if path not in rewrite.files or path not in documents:
            continue
        original = documents[path]
        edited = rewrite.edited.get(path, {})
        taken = {(o.range[0], o.range[1]) for o in original.occurrences}
        for occurrence in document.occurrences:
            if (
                occurrence.symbol.startswith("local")
                or occurrence.symbol_roles & 1
                or not occurrence.range
            ):
                continue
            line, start = occurrence.range[0], occurrence.range[1]
            if len(occurrence.range) != 3 or start >= edited.get(line, 1 << 30):
                continue
            if (line, start) in taken:
                continue
            original.occurrences.append(
                scip_pb2.Occurrence(
                    range=list(occurrence.range),
                    symbol=occurrence.symbol,
                    symbol_roles=occurrence.symbol_roles,
                )
            )
            taken.add((line, start))
            added += 1
    return added
