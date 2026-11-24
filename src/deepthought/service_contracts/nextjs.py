"""Next.js API routes, read from the file system.

A Next.js project is a directory whose package.json depends on ``next``.
Its routes are files, not calls:

- the pages router: ``pages/api/**`` (or ``src/pages/api/**``); the file's
  path is the route (``index`` is its directory), and the default export
  handles every method;
- the app router: ``app/**/route.{js,ts}`` (or under ``src/app``); the
  directories are the route, without route groups (``(name)``) and parallel
  slots (``@name``); a directory starting with ``_`` is private, and an
  intercepting route (``(.)name``) is not a route of its own. The methods
  are the HTTP verbs the module exports (``export async function GET``,
  ``export const POST = ...``, ``export { handler as PUT }``).

``[id]`` is a route variable, ``[...rest]`` and ``[[...rest]]`` match the
rest of the path. A literal ``basePath`` in ``next.config.{js,mjs,cjs,ts}``
prefixes every route.
"""

import json
import re
from pathlib import PurePosixPath

from ..syntax import identifier
from .http import ANY_METHOD
from .model import HttpRoute

_VERBS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_PAGES = re.compile(r"^(?:src/)?pages/(api(?:/.*)?)\.(?:js|jsx|ts|tsx|mjs|cjs)$")
_APP = re.compile(r"^(?:src/)?app/(?:(.*)/)?route\.(?:js|ts|mjs|cjs)$")
_CONFIGS = ("next.config.js", "next.config.mjs", "next.config.cjs", "next.config.ts")


def _text(node, content):
    return content[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def next_projects(root, files):
    """{project directory ("" for the root): base path} of the Next.js
    projects above ``files``."""
    found = {}
    seen = set()
    for file in files:
        for directory in PurePosixPath(file).parents:
            key = "" if directory.as_posix() == "." else directory.as_posix()
            if key in seen:
                break
            seen.add(key)
            manifest = root / directory / "package.json"
            if not manifest.is_file():
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                break
            listed = {}
            for field in ("dependencies", "devDependencies"):
                if isinstance(data, dict) and isinstance(data.get(field), dict):
                    listed.update(data[field])
            if "next" in listed:
                found[key] = _base_path(root / directory)
            break
    return found


def _base_path(directory):
    """A literal ``basePath`` the project's Next.js config sets, else ""."""
    for name in _CONFIGS:
        path = directory / name
        if not path.is_file():
            continue
        try:
            config = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        values = re.findall(r"\bbasePath\s*:\s*(['\"`])([^'\"`$]*)\1", config)
        return values[0][1] if len(values) == 1 else ""
    return ""


def _segment(part):
    optional = re.fullmatch(r"\[\[\.\.\.([^\]]+)\]\]", part)
    rest = re.fullmatch(r"\[\.\.\.([^\]]+)\]", part)
    variable = re.fullmatch(r"\[([^\]]+)\]", part)
    if optional or rest:
        return "{*" + (optional or rest).group(1) + "}"
    if variable:
        return "{" + variable.group(1) + "}"
    return part


def route_of(file, projects):
    """(project, "pages" | "app", path) of a Next.js route file, else None."""
    path = PurePosixPath(file)
    for project, base in sorted(projects.items(), key=lambda p: -len(p[0])):
        if project and not path.is_relative_to(project):
            continue
        relative = path.relative_to(project).as_posix() if project else file
        pages = _PAGES.match(relative)
        if pages:
            parts = pages.group(1).split("/")
            if parts[-1] == "index":
                parts.pop()
            return project, "pages", base + "/" + "/".join(map(_segment, parts))
        app = _APP.match(relative)
        if app:
            parts = [p for p in (app.group(1) or "").split("/") if p]
            if any(p.startswith("_") or p.startswith("(.") for p in parts):
                return None
            parts = [
                p
                for p in parts
                if not (p.startswith("(") and p.endswith(")")) and not p.startswith("@")
            ]
            return project, "app", base + "/" + "/".join(map(_segment, parts))
    return None


def _exports(root, content):
    """({exported name: local name}, default-exported local name or None)
    of a module."""
    names, default = {}, None
    for node in root.named_children:
        if node.type != "export_statement":
            continue
        is_default = any(c.type == "default" for c in node.children)
        declaration = node.child_by_field_name("declaration")
        value = node.child_by_field_name("value")
        if declaration is not None:
            if declaration.type in (
                "function_declaration",
                "generator_function_declaration",
                "class_declaration",
            ):
                name = declaration.child_by_field_name("name")
                if name is not None:
                    names[_text(name, content)] = _text(name, content)
                    if is_default:
                        default = _text(name, content)
            elif declaration.type in ("lexical_declaration", "variable_declaration"):
                for declarator in declaration.named_children:
                    name = declarator.child_by_field_name("name")
                    if declarator.type == "variable_declarator" and name is not None:
                        names[_text(name, content)] = _text(name, content)
        elif is_default and value is not None and value.type == "identifier":
            default = _text(value, content)
        for clause in node.named_children:
            if clause.type != "export_clause":
                continue
            for specifier in clause.named_children:
                alias = specifier.child_by_field_name("alias")
                name = specifier.child_by_field_name("name")
                exported = alias if alias is not None else name
                if exported is not None and name is not None:
                    names[_text(exported, content)] = _text(name, content)
    return names, default


def extract_nextjs(tree, content, repository_id, file, definitions, route):
    """The routes of one Next.js route file; ``route`` is ``route_of``'s
    answer for it."""
    _, kind, path = route
    root = tree.root_node
    names, default = _exports(root, content)
    own = {}
    for definition in definitions:
        if definition.file == file:
            own.setdefault(definition.symbol.rpartition(".")[2], []).append(definition)

    def handler(name):
        found = own.get(name, []) if name else []
        return found[0].id if len(found) == 1 else None

    routes = []
    if kind == "pages":
        listed = [((ANY_METHOD,), handler(default))]
    else:
        listed = [((verb,), handler(names[verb])) for verb in _VERBS if verb in names]
    for methods, handler_id in listed:
        key = f"{','.join(methods)} {path}"
        routes.append(
            HttpRoute(
                id=identifier(repository_id, file, key, "HttpRoute", 0),
                file=file,
                line=1,
                column=0,
                handler_id=handler_id,
                methods=methods,
                path=path,
                framework="nextjs",
            )
        )
    return routes
