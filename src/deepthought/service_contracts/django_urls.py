"""Django URLconf routes, identified through the scip-python index.

A route is an entry of a URLconf: a call the index resolves to Django's
``path``, ``re_path`` or ``url`` (``django.urls.conf``, ``django.conf.urls``)
whose view is a function or class of the repository (``View.as_view()``
names its class), or an ``include`` of another URLconf module, whose
entries take the include's pattern as their prefix. A URLconf no other
includes is mounted at ``/``. What a name is (Django's ``path`` or a
local one, a view of this repository or a library's) is the index's
answer.

Patterns are read the way Django reverses them: a ``path`` route's
``<converter:name>`` is one parameter; a ``re_path``/``url`` regex is read
with Python's own regular expression parser, its named or numbered groups
become parameters, anchors are dropped and an optional or zero-or-more
part is left out, as ``django.utils.regex_helper.normalize`` takes the
shortest form. A regex with any other construct (alternation, a character
class outside a group) is not read. Django dispatches every method to the
view, so each route takes any method.

Routes a Django REST framework router registers (``include(router.urls)``)
come from ViewSet actions and are read by ``drf_routers``.
"""

import re

try:  # Python 3.11+
    import re._parser as _sre_parse
    import re._constants as _sre_constants
except ImportError:  # pragma: no cover
    import sre_constants as _sre_constants
    import sre_parse as _sre_parse

from ..syntax import identifier
from .drf_routers import DrfRouters
from .folding import Folder, text
from .http import ANY_METHOD, PLACEHOLDER, join_paths
from .model import HttpRoute, Unread

_DJANGO = re.compile(
    r"^scip-python python [Dd]jango \S+ `django\.(?:urls\.conf|conf\.urls)`/"
    r"(path|re_path|url|include)(?:\(\))?\.$"
)
_CONVERTER = re.compile(r"<(?:[^>:]+:)?[^>]+>")


def _regex_path(pattern):
    """The path a ``re_path``/``url`` regex matches, with ``{var}`` for
    each group, or None for a construct that is not one fixed path."""
    try:
        parsed = _sre_parse.parse(pattern)
    except Exception:
        return None
    return _walk_regex(parsed)


def _walk_regex(parsed):
    out = []
    for op, value in parsed:
        if op is _sre_constants.LITERAL:
            out.append(chr(value))
        elif op is _sre_constants.AT:
            continue
        elif op is _sre_constants.SUBPATTERN:
            group = value[0]
            if group is None:
                inner = _walk_regex(value[-1])
                if inner is None:
                    return None
                out.append(inner)
            else:
                out.append("{var}")
        elif op in (_sre_constants.MAX_REPEAT, _sre_constants.MIN_REPEAT):
            low, _high, inner = value
            if low == 0:
                continue  # optional: the shortest form leaves it out
            if low == 1 and _high == 1:
                walked = _walk_regex(inner)
                if walked is None:
                    return None
                out.append(walked)
            else:
                return None
        else:
            return None
    return "".join(out)


def _route_path(kind, pattern):
    if kind == "path":
        return _CONVERTER.sub("{var}", pattern)
    return _regex_path(pattern)


def _module_file(module, files):
    """The analysed file a dotted URLconf module names: the longest
    directory suffix of the repository that holds it."""
    parts = module.split(".")
    for candidate in ("/".join(parts) + ".py", "/".join(parts) + "/__init__.py"):
        found = sorted(
            f for f in files if f == candidate or f.endswith("/" + candidate)
        )
        if len(found) == 1:
            return found[0]
    return None


def _call_name(call, content):
    function = call.child_by_field_name("function")
    if function is None:
        return None
    if function.type == "attribute":
        return function.child_by_field_name("attribute")
    return function if function.type == "identifier" else None


def django_routes(python_sources, references, definitions, repository_id):
    """(routes, unread) of the Django URLconfs among ``python_sources``
    ((coverage, content, tree) of each file), through the ``references``
    of the SCIP indexes. ``definitions`` are the parsed declarations, for
    linking a route to its view."""
    by_site = {}
    for d in definitions:
        by_site.setdefault((d.file, d.line, d.symbol), []).append(d)
    files = {coverage.file for coverage, _, _ in python_sources}
    entries, unread = {}, []
    drf = DrfRouters(python_sources, references, by_site, _regex_path)

    def django_kind(file, call, content):
        name = _call_name(call, content)
        if name is None or not references.covers(file):
            return None
        for symbol in references.symbols(
            file, name.start_point.row + 1, name.start_point.column
        ):
            match = _DJANGO.match(symbol)
            if match:
                return match.group(1)
        return None

    def view_of(file, node):
        """The repository declaration a view argument names."""
        if node.type == "call":
            function = node.child_by_field_name("function")
            if (
                function is not None
                and function.type == "attribute"
                and function.child_by_field_name("attribute") is not None
                and text(function.child_by_field_name("attribute"), content_of[file])
                == "as_view"
            ):
                node = function.child_by_field_name("object")
            else:
                return None
        if node.type == "attribute":
            node = node.child_by_field_name("attribute")
        if node is None or node.type != "identifier":
            return None
        name = text(node, content_of[file])
        for defined_in, line, _ in (
            references.definition_sites(
                file, node.start_point.row + 1, node.start_point.column
            )
            or ()
        ):
            found = by_site.get((defined_in, line, name), ())
            if len(found) == 1:
                return found[0]
        return None

    content_of = {}
    for coverage, content, tree in python_sources:
        if b"urlpatterns" not in content:
            continue
        file = coverage.file
        content_of[file] = content
        folder = Folder("python", tree.root_node, content)
        # Module-level bindings of names to lists of entries, in order
        # (``urlpatterns += [...]`` adds to the list).
        bound = {}
        for statement in tree.root_node.named_children:
            if statement.type != "expression_statement" or not statement.named_children:
                continue
            assignment = statement.named_children[0]
            if assignment.type not in ("assignment", "augmented_assignment"):
                continue
            left = assignment.child_by_field_name("left")
            right = assignment.child_by_field_name("right")
            if left is None or left.type != "identifier" or right is None:
                continue
            name = text(left, content)
            if assignment.type == "assignment":
                bound[name] = [right]
            else:
                bound.setdefault(name, []).append(right)

        def entries_of(node, prefix, depth, file=file, content=content, folder=folder):
            if depth > 8 or node is None:
                return
            if node.type in ("list", "tuple"):
                for element in node.named_children:
                    yield from entries_of(element, prefix, depth + 1)
            elif node.type == "binary_operator":
                yield from entries_of(
                    node.child_by_field_name("left"), prefix, depth + 1
                )
                yield from entries_of(
                    node.child_by_field_name("right"), prefix, depth + 1
                )
            elif node.type == "identifier":
                for value in bound.get(text(node, content), ()):
                    yield from entries_of(value, prefix, depth + 1)
            elif node.type == "attribute" and drf.is_urls(file, node):
                for path, view, call, methods, site in drf.routes_of(file, node):
                    yield ("view", prefix + path, view, call, methods, site)
            elif node.type == "call":
                yield from entry(node, prefix, depth)

        def entry(node, prefix, depth, file=file, content=content, folder=folder):
            if django_kind(file, node, content) not in ("path", "re_path", "url"):
                return
            arguments = node.child_by_field_name("arguments")
            positional = [
                c
                for c in (arguments.named_children if arguments else ())
                if c.type not in ("keyword_argument", "comment")
            ]
            if len(positional) < 2:
                return
            kind = text(_call_name(node, content), content)
            line = node.start_point.row + 1
            pattern = folder.evaluate(positional[0])
            path = (
                None
                if PLACEHOLDER in pattern
                else _route_path("path" if kind == "path" else "regex", pattern)
            )
            target = positional[1]
            if (
                target.type == "call"
                and django_kind(file, target, content) == "include"
            ):
                included = target.child_by_field_name("arguments")
                first = [
                    c
                    for c in (included.named_children if included else ())
                    if c.type not in ("keyword_argument", "comment")
                ]
                if path is None:
                    # Mounted at a pattern the code does not fix: the
                    # included entries keep an unknown base.
                    path = PLACEHOLDER
                if first and first[0].type == "string":
                    child = _module_file(folder.evaluate(first[0]), files)
                    if child is None:
                        unread.append(
                            Unread(file, line, "route", "include_not_resolved")
                        )
                    else:
                        yield ("include", prefix + path, child, node, None, file)
                elif first:
                    patterns = first[0]
                    if patterns.type == "tuple" and patterns.named_children:
                        patterns = patterns.named_children[0]
                    found = list(entries_of(patterns, prefix + path, depth + 1))
                    if not found:
                        unread.append(
                            Unread(file, line, "route", "include_not_resolved")
                        )
                    yield from found
                return
            if path is None:
                unread.append(Unread(file, line, "route", "path_not_literal"))
                return
            view = view_of(file, target)
            if view is None:
                unread.append(Unread(file, line, "route", "view_not_resolved"))
                return
            yield ("view", prefix + path, view, node, None, file)

        found = []
        for value in bound.get("urlpatterns", ()):
            found.extend(entries_of(value, "", 0))
        if found:
            entries[file] = found

    parents = {}
    for file, found in entries.items():
        for kind, path, child, *_ in found:
            if kind == "include":
                parents.setdefault(child, []).append((file, path))

    def prefixes(file, active):
        if file not in parents:
            return {""}
        found = set()
        for parent, path in parents[file]:
            if parent in active:
                continue
            for outer in prefixes(parent, active | {file}):
                found.add(outer + path)
        return found

    routes, ordinals = [], {}
    for file in sorted(entries):
        outer = sorted(prefixes(file, frozenset({file})))
        for kind, path, view, node, methods, site in entries[file]:
            if kind != "view":
                continue
            methods = methods or (ANY_METHOD,)
            framework = (
                "django" if methods == (ANY_METHOD,) else "django-rest-framework"
            )
            for prefix in outer:
                written = prefix + path
                base_unknown = PLACEHOLDER in written
                if base_unknown:
                    written = written.rpartition(PLACEHOLDER)[2]
                full = join_paths("/" + written)
                key = f"{','.join(methods)} {full}"
                ordinal = ordinals.get((site, key), 0)
                ordinals[(site, key)] = ordinal + 1
                routes.append(
                    HttpRoute(
                        id=identifier(repository_id, site, key, "HttpRoute", ordinal),
                        file=site,
                        line=node.start_point.row + 1,
                        column=node.start_point.column,
                        handler_id=view.id,
                        methods=methods,
                        path=full,
                        framework=framework,
                        base_unknown=base_unknown,
                        base_written=base_unknown,
                    )
                )
    unread.extend(
        Unread(f, line, "route", reason) for f, line, reason in sorted(set(drf.unread))
    )
    return routes, unread
