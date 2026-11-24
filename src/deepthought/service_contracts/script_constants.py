"""String constants that JavaScript/TypeScript modules share.

A module's exported constants are its ``export const NAME = value``
declarations, folded with ``folding.Folder``, its ``export { a as b }`` of a
name bound once in the module, and the names it re-exports from another
repository module (``export { a } from``, ``export * from``; a name two
``export *`` give differently is not exported, as in ECMAScript). A
file's externals are the constants it imports by name from a repository
module, and the globals the Angular CLI ``define`` option of the project
compiling it replaces with a string literal. Modules are found by the
``resolve(file, specifier)`` callable the caller gives.
"""

import json
import re

from .folding import Folder, text
from .http import PLACEHOLDER

_CONFLICT = object()


def _module_text(node, content):
    return text(node, content)[1:-1] if node is not None else None


def _js_string_literal(value):
    """The text of a JavaScript string literal written as ``value``, or None."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(['\"])((?:(?!\1)[^\\])*)\1\s*", value)
    return match.group(2) if match else None


def angular_defines(path, content):
    """[(directory, {global: string value or None})] of an angular.json: each
    project's ``build`` target ``define`` options (the options and every
    configuration), for the files under the project's source root. A global
    defined as anything other than one string literal is None."""
    try:
        workspace = json.loads(content)
    except ValueError:
        return []
    projects = workspace.get("projects") if isinstance(workspace, dict) else None
    if not isinstance(projects, dict):
        return []
    base = path.rpartition("/")[0]
    found = []
    for project in projects.values():
        if not isinstance(project, dict):
            continue
        build = (project.get("architect") or project.get("targets") or {}).get("build")
        if not isinstance(build, dict):
            continue
        options = [build.get("options")]
        configurations = build.get("configurations")
        if isinstance(configurations, dict):
            options.extend(configurations.values())
        values = {}
        for option in options:
            define = option.get("define") if isinstance(option, dict) else None
            if not isinstance(define, dict):
                continue
            for name, value in define.items():
                literal = _js_string_literal(value)
                if name in values and values[name] != literal:
                    literal = None
                values[name] = literal
        if not values:
            continue
        directory = project.get("sourceRoot") or project.get("root") or ""
        if not isinstance(directory, str):
            continue
        directory = "/".join(p for p in (base, directory.strip("/")) if p)
        found.append((directory, values))
    return found


class ScriptConstants:
    """Exported and imported string constants of the repository's scripts.

    ``sources`` is {file: (tree, content, language)}; ``defines`` is the
    list ``angular_defines`` gives for every angular.json."""

    def __init__(self, sources, resolve, defines=()):
        self.sources = sources
        self.resolve = resolve
        self.defines = list(defines)
        self._exports = {}
        self._externals = {}
        self._active = set()

    def _defined(self, file):
        """Globals defined for ``file``: by the innermost project around it."""
        best = None
        for directory, values in self.defines:
            if (not directory or file.startswith(directory + "/")) and (
                best is None or len(directory) > len(best[0])
            ):
                best = (directory, values)
        if best is None:
            return {}
        return {k: v for k, v in best[1].items() if v is not None}

    def externals(self, file):
        """{name: value} of the names ``file`` imports or has defined."""
        if file in self._externals:
            return self._externals[file]
        if file not in self.sources or file in self._active:
            return {}
        self._active.add(file)
        try:
            tree, content, _ = self.sources[file]
            found = self._defined(file)
            for node in tree.root_node.named_children:
                if node.type != "import_statement":
                    continue
                source = _module_text(node.child_by_field_name("source"), content)
                for clause in node.named_children:
                    if clause.type != "import_clause":
                        continue
                    for item in clause.named_children:
                        if item.type != "named_imports":
                            if item.type == "identifier":
                                found.pop(text(item, content), None)
                            continue
                        for spec in item.named_children:
                            if spec.type != "import_specifier":
                                continue
                            name = text(spec.child_by_field_name("name"), content)
                            alias = spec.child_by_field_name("alias")
                            local = text(alias, content) if alias else name
                            # An import shadows any global of the same name.
                            found.pop(local, None)
                            target = self.resolve(file, source) if source else None
                            value = self.exports(target).get(name) if target else None
                            if value is not None:
                                found[local] = value
            self._externals[file] = found
            return found
        finally:
            self._active.discard(file)

    def exports(self, file):
        """{name: value} of the string constants ``file`` exports."""
        if file in self._exports:
            return self._exports[file]
        if file not in self.sources or file in self._active:
            return {}
        tree, content, language = self.sources[file]
        folder = Folder(language, tree.root_node, content, self.externals(file))
        self._active.add(file)
        found, starred = {}, {}
        try:
            for node in tree.root_node.named_children:
                if node.type != "export_statement":
                    continue
                source = _module_text(node.child_by_field_name("source"), content)
                target = self.resolve(file, source) if source else None
                clause = next(
                    (c for c in node.named_children if c.type == "export_clause"),
                    None,
                )
                declaration = node.child_by_field_name("declaration")
                if declaration is not None:
                    if declaration.type == "lexical_declaration" and any(
                        c.type == "const" for c in declaration.children
                    ):
                        for item in declaration.named_children:
                            name = item.child_by_field_name("name")
                            if (
                                item.type == "variable_declarator"
                                and name is not None
                                and name.type == "identifier"
                            ):
                                found[text(name, content)] = folder.evaluate(
                                    item.child_by_field_name("value")
                                )
                elif clause is not None:
                    names = target and self.exports(target)
                    for spec in clause.named_children:
                        if spec.type != "export_specifier":
                            continue
                        name = spec.child_by_field_name("name")
                        alias = spec.child_by_field_name("alias")
                        exported = text(alias or name, content)
                        if source is None:
                            found[exported] = folder.evaluate(name)
                        elif target is not None:
                            found[exported] = names.get(text(name, content))
                        else:
                            found[exported] = None
                elif source is not None and not any(
                    c.type == "namespace_export" for c in node.named_children
                ):
                    # export * from: names this module declares win; a name
                    # two stars give differently is not exported.
                    for name, value in (self.exports(target) if target else {}).items():
                        if name == "default":
                            continue
                        if name in starred and starred[name] != value:
                            starred[name] = _CONFLICT
                        else:
                            starred[name] = value
        finally:
            self._active.discard(file)
        for name, value in starred.items():
            if name not in found and value is not _CONFLICT:
                found[name] = value
        found = {k: v for k, v in found.items() if v is not None and v != PLACEHOLDER}
        self._exports[file] = found
        return found
