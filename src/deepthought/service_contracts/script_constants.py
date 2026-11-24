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


def function_name(function):
    """The module-level name of a function: a ``function`` declaration, or a
    ``const``/``let``/``var`` at the top level whose value is the function;
    None otherwise."""
    parent = function.parent
    if function.type in ("function_declaration", "generator_function_declaration"):
        name = function.child_by_field_name("name")
        holder = parent
    elif parent is not None and parent.type == "variable_declarator":
        if parent.child_by_field_name("value") != function:
            return None
        name = parent.child_by_field_name("name")
        holder = parent.parent
        holder = holder.parent if holder is not None else None
    else:
        return None
    if holder is not None and holder.type == "export_statement":
        holder = holder.parent
    if name is None or name.type != "identifier":
        return None
    if holder is None or holder.type != "program":
        return None
    return name


def method_name(function):
    """The name of a class method (not static, not an accessor) that calls
    of ``this.name(...)`` in its class reach; None otherwise."""
    if function.type != "method_definition":
        return None
    parent = function.parent
    if parent is None or parent.type != "class_body":
        return None
    if any(c.type in ("static", "get", "set") for c in function.children):
        return None
    name = function.child_by_field_name("name")
    if name is None or name.type != "property_identifier":
        return None
    return name


_THIS_BREAKERS = (
    "function_declaration",
    "function_expression",
    "function",
    "generator_function",
    "generator_function_declaration",
    "class_body",
)


def _this_is_class(node, body):
    """``this`` at ``node`` is an instance of the class whose body is
    ``body``: no ordinary function or other class lies in between."""
    current = node.parent
    while current is not None:
        if current.id == body.id:
            return True
        if current.type in _THIS_BREAKERS:
            return False
        current = current.parent
    return False


class ScriptCalls:
    """The values that calls of a module-level function pass to its
    parameters: calls in the function's own module, and in modules that
    import it by name, or import the module as a namespace and call
    ``ns.name(...)``."""

    def __init__(self, constants):
        self.constants = constants
        self._importers = None

    def _imports(self):
        """{(target file, exported name): [(importing file, local name)]}."""
        if self._importers is None:
            self._importers = {}
            constants = self.constants
            for file, (tree, content, _) in constants.sources.items():
                for node in tree.root_node.named_children:
                    if node.type != "import_statement":
                        continue
                    source = _module_text(node.child_by_field_name("source"), content)
                    target = constants.resolve(file, source) if source else None
                    if target is None:
                        continue
                    for clause in node.named_children:
                        if clause.type != "import_clause":
                            continue
                        for item in clause.named_children:
                            if item.type == "namespace_import":
                                local = text(item.named_children[-1], content)
                                self._importers.setdefault((target, "*"), []).append(
                                    (file, local)
                                )
                                continue
                            if item.type != "named_imports":
                                continue
                            for spec in item.named_children:
                                if spec.type != "import_specifier":
                                    continue
                                name = text(spec.child_by_field_name("name"), content)
                                alias = spec.child_by_field_name("alias")
                                local = text(alias, content) if alias else name
                                self._importers.setdefault((target, name), []).append(
                                    (file, local)
                                )
        return self._importers

    def calls(self, file, name, method=None):
        """[({index: value}, "file:line")] for each call of the module-level
        function ``name`` of ``file``, or, with ``method`` (a class method
        node), of ``this.name(...)`` in the method's class: the arguments
        folded in the calling module (none from a spread argument on)."""
        if method is not None:
            return self._method_calls(file, name, method)
        imports = self._imports()
        callers = (
            [(file, name, True)]
            + [
                (caller, local, False)
                for caller, local in imports.get((file, name), [])
            ]
            # import * as ns: ns.name(...)
            + [
                (caller, f"{local}.{name}", False)
                for caller, local in imports.get((file, "*"), [])
            ]
        )
        found = []
        for caller, local, own in callers:
            tree, content, language = self.constants.sources[caller]
            folder = Folder(
                language, tree.root_node, content, self.constants.externals(caller)
            )
            stack = [tree.root_node]
            while stack:
                node = stack.pop()
                stack.extend(node.named_children)
                if node.type != "call_expression":
                    continue
                function = node.child_by_field_name("function")
                if text(function, content) != local:
                    continue
                if function.type == "member_expression":
                    function = function.child_by_field_name("object")
                    if function.type != "identifier" or folder._bound(function):
                        continue
                elif function.type != "identifier":
                    continue
                elif own:
                    # The name must be the module's own declaration.
                    bound = folder.binding(function)
                    if bound is None and folder._bound(function):
                        continue
                    if bound is not None and function_name(bound[1]) is None:
                        continue
                elif folder._bound(function):
                    continue  # shadowed by a local name
                call = _call(folder, node, caller)
                if call is not None:
                    found.append(call)
        return found

    def _method_calls(self, file, name, method):
        tree, content, language = self.constants.sources[file]
        folder = Folder(
            language, tree.root_node, content, self.constants.externals(file)
        )
        body = method.parent
        found = []
        stack = [body]
        while stack:
            node = stack.pop()
            stack.extend(node.named_children)
            if node.type != "call_expression":
                continue
            function = node.child_by_field_name("function")
            if function.type != "member_expression":
                continue
            owner = function.child_by_field_name("object")
            if owner.type != "this" or text(function, content) != f"this.{name}":
                continue
            if not _this_is_class(node, body):
                continue
            call = _call(folder, node, file)
            if call is not None:
                found.append(call)
        return found


def _call(folder, node, caller):
    """({index: value}, "file:line") of a call, or None."""
    arguments = node.child_by_field_name("arguments")
    if arguments is None or arguments.type != "arguments":
        return None
    positional = [c for c in arguments.named_children if c.type != "comment"]
    values = {}
    for index, argument in enumerate(positional):
        if argument.type == "spread_element":
            break
        values[index] = folder.argument(argument)
    return values, f"{caller}:{node.start_point.row + 1}"


def fold_through_calls(folder, compute, calls_of, unknown=None):
    """[(value, calls)] for the text ``compute(folder)`` gives. When it has
    unknown text from parameters of exactly one module-level function or
    class method, and ``calls_of(name)`` (``calls_of(name, method)`` for a
    method) finds calls of it, each call gives the
    value with its arguments in place of the parameters; ``calls`` lists the
    calls ("file:line") that give each value. ``unknown(value)`` tells
    whether a value is not fixed (by default: it has unknown text)."""
    if unknown is None:

        def unknown(value):
            return PLACEHOLDER in value

    folder.parameters_seen = {}
    value = compute(folder)
    if not unknown(value) or calls_of is None:
        return [(value, ())]
    if len(folder.parameters_seen) != 1:
        return [(value, ())]
    function = next(iter(folder.parameters_seen.values()))
    name = function_name(function)
    if name is not None:
        found = calls_of(text(name, folder.content))
    else:
        name = method_name(function)
        found = (
            calls_of(text(name, folder.content), function) if name is not None else None
        )
    if not found:
        return [(value, ())]
    values = {}
    for arguments, call in found:
        passed = compute(folder.with_arguments(function, arguments))
        # A call that leaves the value as it was does not give it.
        values.setdefault(passed, [])
        if passed != value:
            values[passed].append(call)
    return [
        (v, tuple(sorted(values[v]))) for v in sorted(values, key=lambda v: repr(v))
    ]
