"""What Python's ``ast`` says about functions passed as arguments and called
through the parameter they are bound to, as plain data.

``def apply(f, x): return f(x)`` called as ``apply(double, 3)`` runs
``double``: Python binds the argument to the parameter ``f``, and the call
``f(x)`` calls whatever ``f`` holds. No maintained tool answers this for
Python: pyright and scip-python resolve names, not the values flowing into
a parameter, and the points-to call graph tools (PyCG, archived in 2023;
pytype, retired in 2024) do not run on current Python. So this rule is
written here, as Python's own argument binding over the parse tree, and
links only names an index resolves (``deepthought.python_value_calls``).

Per file, it reads:

- ``functions``: each ``def`` and ``async def``, by the line its ``def``
  is on and its name: its positional parameters in order, its keyword-only
  parameters, whether it is a ``staticmethod`` or a ``classmethod``, and,
  for each parameter the body never rebinds, the calls written as that bare
  name (``f(...)``, in nested functions and lambdas too, unless one of them
  binds the name again) and the calls the parameter is passed to as a bare
  argument (``g(f)``, forwarded).
- ``calls``: each call, by where it starts and ends, with each positional and
  keyword argument that is a name or a dotted name (where its last name is
  written), up to the first ``*`` or ``**`` argument.
- ``decorators``: each decorator written as a name or a dotted name (where
  its last name is written), with the function it decorates, which Python
  passes to it.

Calls are known by where they start and end, other names by where they
are written, in 1-based lines and 0-based UTF-8 byte columns. This module
uses the standard library only, so it also runs in the Python that the
repository declares (``deepthought.python_ast_facts``).
"""

import ast


def _position(node):
    """Where the last name of a name or dotted name is written."""
    if isinstance(node, ast.Name):
        return [node.lineno, node.col_offset, node.id]
    if (
        isinstance(node, ast.Attribute)
        and node.end_lineno == node.lineno
        and node.end_col_offset is not None
    ):
        return [node.lineno, node.end_col_offset - len(node.attr), node.attr]
    return None


def _span(node):
    """Where a call starts and ends: calls in a chain start together."""
    return [node.lineno, node.col_offset, node.end_lineno, node.end_col_offset]


def _arguments(call):
    positional, keywords = [], {}
    for argument in call.args:
        if isinstance(argument, ast.Starred):
            break
        positional.append(_position(argument))
    for keyword in call.keywords:
        if keyword.arg is None:
            break
        keywords[keyword.arg] = _position(keyword.value)
    return positional, keywords


def _parameters(arguments):
    positional = [a.arg for a in arguments.posonlyargs + arguments.args]
    return positional, [a.arg for a in arguments.kwonlyargs]


def _bound(node):
    """Every name a function, lambda or class body binds itself, nested
    scopes left out; its parameters are not counted."""
    names = set()

    def visit(child):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(child.name)
            for decorator in child.decorator_list:
                visit(decorator)
            return
        if isinstance(child, ast.Lambda):
            return
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            names.add(child.id)
        elif isinstance(child, (ast.Global, ast.Nonlocal)):
            names.update(child.names)
        elif isinstance(child, ast.alias):
            names.add((child.asname or child.name).split(".")[0])
        elif isinstance(child, ast.ExceptHandler) and child.name:
            names.add(child.name)
        elif isinstance(child, (ast.MatchAs, ast.MatchStar)) and child.name:
            names.add(child.name)
        elif isinstance(child, ast.MatchMapping) and child.rest:
            names.add(child.rest)
        for grandchild in ast.iter_child_nodes(child):
            visit(grandchild)

    body = node.body if isinstance(node.body, list) else [node.body]
    for statement in body:
        visit(statement)
    return names


def _uses(function, name):
    """(calls written as the bare ``name``, calls ``name`` is passed to as a
    bare argument) in ``function``'s body, nested scopes included unless
    they bind ``name`` themselves."""
    called, passed = [], []

    def visit(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for default in node.args.defaults + [
                d for d in node.args.kw_defaults if d is not None
            ]:
                visit(default)
            if isinstance(node, ast.Lambda):
                inner = node.body
            else:
                for decorator in node.decorator_list:
                    visit(decorator)
                inner = node.body
            positional, keyword_only = _parameters(node.args)
            shadowing = set(positional) | set(keyword_only)
            shadowing |= {a.arg for a in (node.args.vararg, node.args.kwarg) if a}
            if name in shadowing or name in _bound(node):
                return
            for child in inner if isinstance(inner, list) else [inner]:
                visit(child)
            return
        if isinstance(node, ast.ClassDef):
            # A class body is its own scope, but its methods do not see it.
            for child in ast.iter_child_nodes(node):
                visit(child)
            return
        if isinstance(
            node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
        ):
            if any(
                isinstance(n, ast.Name) and n.id == name
                for g in node.generators
                for n in ast.walk(g.target)
            ):
                return
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == name:
                called.append(_span(node))
            if any(
                isinstance(a, ast.Name) and a.id == name
                for a in node.args + [k.value for k in node.keywords]
            ):
                passed.append(_span(node))
        for child in ast.iter_child_nodes(node):
            visit(child)

    for statement in function.body:
        visit(statement)
    return called, passed


def _decorated(function, name):
    return any(
        (isinstance(d, ast.Name) and d.id == name)
        or (isinstance(d, ast.Attribute) and d.attr == name)
        for d in function.decorator_list
    )


def parameter_facts(tree):
    """``{"functions": [...], "calls": [...], "decorators": [...]}`` of one
    parsed module."""
    functions, calls, decorators = [], [], []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            positional, keyword_only = _parameters(node.args)
            rebound = _bound(node)
            parameters = {}
            for parameter in positional + keyword_only:
                if parameter in rebound:
                    continue
                called, passed = _uses(node, parameter)
                if called or passed:
                    parameters[parameter] = {"called": called, "passed": passed}
            for decorator in node.decorator_list:
                # ``@deco`` over ``def g`` calls ``deco(g)``.
                written = _position(decorator)
                if written is not None:
                    decorators.append(
                        {"decorator": written, "line": node.lineno, "name": node.name}
                    )
            functions.append(
                {
                    "line": node.lineno,
                    "name": node.name,
                    "positional": positional,
                    "keyword_only": keyword_only,
                    "staticmethod": _decorated(node, "staticmethod"),
                    "classmethod": _decorated(node, "classmethod"),
                    "parameters": parameters,
                }
            )
        elif isinstance(node, ast.Call):
            positional, keywords = _arguments(node)
            if any(positional) or any(keywords.values()):
                receiver = (
                    _position(node.func.value)
                    if isinstance(node.func, ast.Attribute)
                    else None
                )
                calls.append(
                    {
                        "span": _span(node),
                        "attribute": isinstance(node.func, ast.Attribute),
                        "receiver": receiver,
                        "positional": positional,
                        "keywords": keywords,
                    }
                )
    functions.sort(key=lambda f: (f["line"], f["name"]))
    calls.sort(key=lambda c: c["span"])
    decorators.sort(key=lambda d: (d["line"], d["decorator"]))
    return {"functions": functions, "calls": calls, "decorators": decorators}
