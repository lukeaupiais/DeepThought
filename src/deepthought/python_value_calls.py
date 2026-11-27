"""Python calls through a parameter, linked to the functions passed to it.

``python_parameter_calls`` reads, from Python's ``ast``, each function's
parameters with the calls written as a bare parameter name, and each call's
arguments. Here the two meet. A call the index or pyright links to a parsed
function binds its arguments to that function's parameters, as Python
does: positional arguments in order (after ``self`` or ``cls`` for a method
called through an instance or a class method; ``__init__`` for a class
called), keyword arguments by name, nothing after a ``*`` or ``**``
argument; a decorator written as a name gets the function it decorates. An argument whose name the SCIP index resolves to a parsed
function, method or class is a value of that parameter, and so is every
value of a caller's own parameter passed on as a bare name (``g(f)``
inside ``def h(f)``), to any depth. A call written as the parameter's name
in the function's body (its nested functions included, unless they bind
the name again) calls each value. A parameter the body rebinds anywhere
takes part in nothing.

Not followed: values stored and read back (``self.steps.append(step)``,
then ``for step in self.steps: step()``), values taken from containers or
returned from calls, and arguments the call passes through ``*args`` or
``**kwargs``.
"""

from collections import defaultdict

PYTHON_CALL_PARAMETER_VALUE = "python.call-parameter-value"
_CALLABLE = {"Function", "Method", "Class"}


def parameter_calls(result, parameters, references, targets, linked):
    """{call site id: (declaration ids, ...)} for the Python calls written as
    a parameter's name that are not in ``linked``. ``parameters`` is
    ``{path: parameter_facts}``; ``references`` a ``ScipReferences``;
    ``targets`` ``{call site id: declaration id}`` of the linked calls."""
    definitions = {d.id: d for d in result.definitions}
    sites = {
        (s.file, s.line, s.column, s.end_line, s.end_column): s
        for s in result.call_sites
        if s.file.endswith((".py", ".pyi"))
    }
    functions, by_id = {}, {}
    for d in result.definitions:
        if d.kind in {"Function", "Method"}:
            functions[(d.file, d.line, d.symbol.rsplit(".", 1)[-1])] = d
    for path, facts in parameters.items():
        for function in facts["functions"]:
            d = functions.get((path, function["line"], function["name"]))
            if d is not None:
                by_id[d.id] = function
    initializers = {
        d.id: found
        for d in result.definitions
        if d.kind == "Class"
        and (found := _member(result, d, "__init__")) is not None
        and found.id in by_id
    }

    values = defaultdict(set)  # (function id, parameter) -> declaration ids
    flows = defaultdict(set)  # (function id, parameter) -> {(caller id, parameter)}
    for path, facts in parameters.items():
        for call in facts["calls"]:
            site = sites.get((path, *call["span"]))
            target = None if site is None else targets.get(site.id)
            if target is None:
                continue
            callee, offset = _callee(
                definitions[target], initializers, by_id, call, references, path
            )
            if callee is None:
                continue
            function = by_id[callee.id]
            bound = []
            for index, argument in enumerate(call["positional"]):
                position = index + offset
                if argument is not None and position < len(function["positional"]):
                    bound.append((function["positional"][position], argument))
            names = set(function["positional"][offset:]) | set(function["keyword_only"])
            for name, argument in call["keywords"].items():
                if argument is not None and name in names:
                    bound.append((name, argument))
            enclosing = by_id.get(site.caller_id)
            for parameter, (line, column, name) in bound:
                if parameter not in function["parameters"]:
                    continue
                key = (callee.id, parameter)
                if (
                    enclosing is not None
                    and name in enclosing["parameters"]
                    and call["span"] in enclosing["parameters"][name]["passed"]
                ):
                    flows[key].add((site.caller_id, name))
                    continue
                found = references.declarations(path, line, column, name) or ()
                values[key] |= {d.id for d in found if d.kind in _CALLABLE}

    for path, facts in parameters.items():
        for decorator in facts.get("decorators", ()):
            decorated = functions.get((path, decorator["line"], decorator["name"]))
            found = references.declarations(path, *decorator["decorator"]) or ()
            for d in found:
                function = by_id.get(d.id)
                if decorated is None or function is None or d.kind != "Function":
                    continue
                if function["positional"][:1] and (
                    function["positional"][0] in function["parameters"]
                ):
                    values[(d.id, function["positional"][0])].add(decorated.id)

    changed = True
    while changed:
        changed = False
        for key, sources in flows.items():
            for source in sources:
                more = values.get(source, set()) - values[key]
                if more:
                    values[key] |= more
                    changed = True

    calls = defaultdict(set)
    for (function_id, parameter), ids in values.items():
        if not ids:
            continue
        file = definitions[function_id].file
        for span in by_id[function_id]["parameters"][parameter]["called"]:
            site = sites.get((file, *span))
            if site is not None and site.id not in linked:
                calls[site.id] |= ids
    return {site: tuple(sorted(ids)) for site, ids in sorted(calls.items())}


def _member(result, owner, name):
    symbol = f"{owner.symbol}.{name}"
    return next(
        (
            d
            for d in result.definitions
            if d.file == owner.file and d.symbol == symbol and d.kind == "Method"
        ),
        None,
    )


def _callee(target, initializers, by_id, call, references, path):
    """(the function the call runs, how many leading parameters the call
    binds itself), or (None, 0) when the call binds no parsed function."""
    if target.kind == "Class":
        initializer = initializers.get(target.id)
        return (initializer, 1) if initializer is not None else (None, 0)
    function = by_id.get(target.id)
    if function is None:
        return None, 0
    if target.kind != "Method" or function["staticmethod"]:
        return target, 0
    if function["classmethod"]:
        return target, 1
    if not call["attribute"]:
        return None, 0
    receiver = call["receiver"]
    if receiver is not None:
        found = references.declarations(path, *receiver) or ()
        if any(d.kind == "Class" for d in found):
            # Called through the class: the instance is an argument.
            return target, 0
    return target, 1
