"""FastAPI dependencies: the functions and classes an endpoint injects.

``Depends(get_db)`` and ``Security(auth)`` hand FastAPI a function or class
to call when the endpoint runs, so the endpoint depends on it without calling
it. A call is read as one only when the SCIP index resolves its callee to
FastAPI's own ``fastapi.param_functions`` ``Depends`` or ``Security``, or
the ``fastapi.params`` classes of those names. Its
dependency is the first argument or ``dependency=``; for a ``Depends()``
with neither, FastAPI calls the parameter's annotation, or the ``T`` of
``Annotated[T, Depends()]``. The dependency is linked only when the index
resolves that name to one parsed declaration, so both ends come from the
indexer. Which declaration depends on it is read with Python's ``ast``: the
function whose parameters (defaults or ``Annotated`` types) or decorators
(``dependencies=[...]``) hold the call, else the innermost function around
it, else the file (a router or app made with ``dependencies=[...]``).

A type alias of ``Annotated[T, Depends(f)]`` (``SessionDep = Annotated[
Session, Depends(get_db)]``) injects ``f`` wherever a parameter is annotated
with it: the index resolves the annotation to the alias's definition, and the
function holding that parameter depends on ``f``. The alias itself is no use.

Not read: a dependency given as an instance (``Security(auth)`` with
``auth = Auth()``), whose ``__call__`` runs; an alias of an alias, or one
written inside another annotation (``Optional[SessionDep]``); and files
``ast`` cannot parse in the Python the repository declares
(``deepthought.python_runtime``).
"""

import ast
from dataclasses import dataclass

_FASTAPI = "`fastapi.param_functions`/"
# The classes those functions return, which FastAPI accepts as well.
_PARAMS = "`fastapi.params`/"
_INJECTORS = ("Depends", "Security")


@dataclass(frozen=True)
class DependencyUse:
    """``consumer`` is ``(file, def line)`` of the depending function, or
    ``(file, None)`` for the top level of the file; ``dependency`` the parsed
    declaration; ``name``, ``line`` and UTF-8 byte ``column`` what is written
    and where: the dependency, or the alias that injects it, with
    ``alias`` the ``file:line`` that defines that alias."""

    consumer: tuple
    dependency: object
    name: str
    line: int
    column: int
    injector: str
    alias: str | None = None


def _last_name(node):
    """(name, line, UTF-8 byte column) of the last name of a Name or dotted
    Attribute, or None."""
    if isinstance(node, ast.Name):
        return node.id, node.lineno, node.col_offset
    if isinstance(node, ast.Attribute):
        return (
            node.attr,
            node.end_lineno,
            node.end_col_offset - len(node.attr.encode()),
        )
    return None


def _owners(tree):
    """Each Depends-capable call's owning function: the function whose
    parameters or decorators hold it, else the innermost function around it.
    Maps ``id(call)`` to a function node or None."""
    owners = {}

    def visit(node, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Parameters and decorators belong to the function itself.
            heads = [*node.decorator_list, *ast.iter_child_nodes(node.args)]
            for head in heads:
                for inner in ast.walk(head):
                    if isinstance(inner, ast.Call):
                        owners[id(inner)] = node
            for statement in node.body:
                visit(statement, node)
            return
        if isinstance(node, ast.Call):
            owners[id(node)] = function
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, None)
    return owners


def _annotated(annotation):
    """``(T, extras)`` of ``Annotated[T, *extras]``, or None."""
    if not isinstance(annotation, ast.Subscript):
        return None
    named = _last_name(annotation.value)
    if named is None or named[0] != "Annotated":
        return None
    parts = annotation.slice
    if not isinstance(parts, ast.Tuple):
        return None
    return parts.elts[0], parts.elts[1:]


def _declared_types(tree):
    """The type FastAPI calls for a ``Depends()`` with no dependency: the
    annotation of the parameter it is the default of, or ``T`` of the
    ``Annotated[T, Depends()]`` it is in. Maps ``id(call)`` to that node
    (other default and ``Annotated`` nodes are mapped too, never looked up)."""
    types = {}
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        arguments = function.args
        positional = arguments.posonlyargs + arguments.args
        with_default = positional[len(positional) - len(arguments.defaults) :]
        for argument, default in [
            *zip(with_default, arguments.defaults),
            *zip(arguments.kwonlyargs, arguments.kw_defaults),
        ]:
            types[id(default)] = argument.annotation
        for argument in positional + arguments.kwonlyargs:
            found = _annotated(argument.annotation)
            if found is not None:
                for extra in found[1]:
                    types[id(extra)] = found[0]
    return types


def _aliases(tree):
    """Type aliases of ``Annotated[T, Depends(...)]`` (``SessionDep =
    Annotated[Session, Depends(get_db)]``, annotated or ``type`` statements
    too) in a module, class or function: maps ``id(call)`` of each call
    directly in the ``Annotated`` to the alias's (first) target and ``T``."""
    found = {}
    for statement in ast.walk(tree):
        if isinstance(statement, ast.Assign):
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign):
            target, value = statement.target, statement.value
        elif type(statement).__name__ == "TypeAlias":
            target, value = statement.name, statement.value
        else:
            continue
        annotated = _annotated(value)
        if annotated is not None:
            for extra in annotated[1]:
                found[id(extra)] = target, annotated[0]
    return found


def _injector_names(tree):
    """The names ``Depends`` and ``Security`` may be called by here: their
    own, and ``as`` names they are imported under."""
    names = set(_INJECTORS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(
                a.asname for a in node.names if a.name in _INJECTORS and a.asname
            )
    return names


def dependency_facts(tree):
    """What the index must settle for one parsed module, as plain data (so
    another interpreter can produce it): ``calls`` that may be ``Depends`` or
    ``Security``, each with its ``callee`` and the dependency ``written`` as
    ``[name, line, UTF-8 column]``, the ``owner`` function's def line (None
    for the file) and the ``alias`` target ``[line, column]`` it is in (or
    None); and the ``parameters`` annotated with a plain or dotted name,
    with their ``function``'s def line."""
    owners = _owners(tree)
    alias_of = _aliases(tree)
    declared = _declared_types(tree)
    declared.update((call, type_) for call, (_, type_) in alias_of.items())
    names = _injector_names(tree)
    calls = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        callee = _last_name(call.func)
        if callee is None or callee[0] not in names:
            continue
        argument = call.args[0] if call.args else None
        for keyword in call.keywords:
            if keyword.arg == "dependency":
                argument = keyword.value
        if argument is None:
            # Depends() calls the parameter's declared type.
            argument = declared.get(id(call))
        written = _last_name(argument)
        if written is None:
            continue
        owner = owners.get(id(call))
        alias, _ = alias_of.get(id(call), (None, None))
        calls.append(
            {
                "callee": list(callee),
                "written": list(written),
                "owner": None if owner is None else owner.lineno,
                "alias": None if alias is None else [alias.lineno, alias.col_offset],
            }
        )
    parameters = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        arguments = function.args
        for argument in [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]:
            written = _last_name(argument.annotation)
            if written is not None:
                parameters.append(
                    {"function": function.lineno, "annotation": list(written)}
                )
    return {"calls": calls, "parameters": parameters}


def _injected(path, call, references):
    """(declaration, injector) of the dependency a FastAPI ``Depends`` or
    ``Security`` call injects, or None."""
    _, line, column = call["callee"]
    symbols = references.symbols(path, line, column)
    injector = next(
        (
            name
            for name in _INJECTORS
            for s in symbols
            if s.endswith((_FASTAPI + name + "().", _PARAMS + name + "#"))
        ),
        None,
    )
    if injector is None:
        return None
    name, line, column = call["written"]
    targets = references.declarations(path, line, column, name)
    if len(targets or ()) != 1:
        return None
    return targets[0], injector


def dependency_uses(facts, references):
    """Every FastAPI dependency use in ``facts`` (repository-relative path
    to ``dependency_facts`` of that file). ``references`` is a
    ``deepthought.scip_calls.ScipReferences``; files no index covers are
    skipped."""
    covered = sorted(path for path in facts if references.covers(path))
    uses, aliases = [], {}
    for path in covered:
        for call in facts[path]["calls"]:
            found = _injected(path, call, references)
            if found is None:
                continue
            declaration, injector = found
            if call["alias"] is not None:
                # Used where a parameter is annotated with the alias.
                site = (path, *call["alias"])
                aliases.setdefault(site, []).append(found)
                continue
            uses.append(
                DependencyUse(
                    (path, call["owner"]), declaration, *call["written"], injector
                )
            )
    for path in covered:
        for parameter in facts[path]["parameters"]:
            _, line, column = parameter["annotation"]
            for site in references.definition_sites(path, line, column):
                for declaration, injector in aliases.get(site, ()):
                    uses.append(
                        DependencyUse(
                            (path, parameter["function"]),
                            declaration,
                            *parameter["annotation"],
                            injector,
                            f"{site[0]}:{site[1]}",
                        )
                    )
    return uses
