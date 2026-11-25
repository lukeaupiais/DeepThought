"""pytest fixtures and the tests and fixtures that request them.

pytest gives a test the fixtures its parameters name, so a test depends on
those fixtures without calling them. Which fixture a name means follows
pytest's documented lookup, read here from the source with Python's own
``ast``: the test's class, then its module, then ``conftest.py`` in the
module's directory and in each directory above it, nearest first. Listing
fixtures with pytest itself (``--fixtures-per-test``) imports the project's
code and runs its plugins, which analysis never does, so this is a rule over
the parsed source.

Read: functions decorated ``@pytest.fixture`` or ``@pytest_asyncio.fixture``
(or ``fixture`` imported from either), named by their ``name=`` argument
when it is a string; and the parameters, without default values, of test
functions (``test*``), test methods (``test*`` in classes named ``Test*``)
and fixtures in test modules (``test_*.py``, ``*_test.py``) and
``conftest.py``, plus the string arguments of ``@pytest.mark.usefixtures``
on those functions and classes. A fixture that requests its own name means
the fixture of that name further out, as pytest overrides it. Not read:
fixtures from plugins and ``pytest_plugins``, ``pytestmark`` and
``usefixtures`` in configuration files, ``getfixturevalue``, and other
``python_files`` or ``python_functions`` settings.
"""

import ast
from dataclasses import dataclass
from pathlib import PurePosixPath

_FIXTURE_MODULES = {"pytest", "pytest_asyncio"}


@dataclass(frozen=True)
class FixtureUse:
    """``consumer`` requests ``fixture``; each is ``(file, line)`` of its
    ``def`` or ``class`` line. ``line`` is where the name is written."""

    consumer: tuple[str, int]
    fixture: tuple[str, int]
    name: str
    line: int
    rule: str  # "parameter" or "usefixtures"


def _is_test_module(path):
    name = PurePosixPath(path).name
    return (
        name == "conftest.py"
        or (name.startswith("test_") and name.endswith(".py"))
        or name.endswith("_test.py")
    )


def _fixture_names(tree):
    """The names a module binds to pytest's fixture decorator: dotted module
    names (``pytest``, an ``import pytest as pt``) and bare ``fixture``."""
    modules, bare = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _FIXTURE_MODULES:
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module in _FIXTURE_MODULES:
            for alias in node.names:
                if alias.name == "fixture":
                    bare.add(alias.asname or alias.name)
    return modules, bare


def _decorator_target(decorator):
    return decorator.func if isinstance(decorator, ast.Call) else decorator


def _fixture_name(function, modules, bare):
    """The fixture name ``function`` declares, or None if it is no fixture."""
    for decorator in function.decorator_list:
        target = _decorator_target(decorator)
        is_fixture = (
            isinstance(target, ast.Attribute)
            and target.attr == "fixture"
            and isinstance(target.value, ast.Name)
            and target.value.id in modules
        ) or (isinstance(target, ast.Name) and target.id in bare)
        if not is_fixture:
            continue
        if isinstance(decorator, ast.Call):
            for keyword in decorator.keywords:
                if (
                    keyword.arg == "name"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    return keyword.value.value
        return function.name
    return None


def _usefixtures(node, modules):
    """``(name, line)`` of each string ``@pytest.mark.usefixtures`` names."""
    found = []
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        target = decorator.func
        if (
            isinstance(target, ast.Attribute)
            and target.attr == "usefixtures"
            and isinstance(target.value, ast.Attribute)
            and target.value.attr == "mark"
            and isinstance(target.value.value, ast.Name)
            and target.value.value.id in modules
        ):
            found.extend(
                (arg.value, arg.lineno)
                for arg in decorator.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            )
    return found


def _parameters(function, method):
    """``(name, line)`` of the parameters pytest fills: those without a
    default value, after ``self`` or ``cls`` of a method."""
    arguments = function.args
    positional = arguments.posonlyargs + arguments.args
    if method:
        positional = positional[1:]
    without_default = positional[: len(positional) - len(arguments.defaults)]
    keyword_only = [
        a
        for a, default in zip(arguments.kwonlyargs, arguments.kw_defaults)
        if default is None
    ]
    return [(a.arg, a.lineno) for a in without_default + keyword_only]


_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def fixture_uses(sources):
    """Every fixture use in ``sources`` (repository-relative path to Python
    text), in file and line order. Files that do not parse are skipped."""
    trees = {}
    for path, text in sources.items():
        try:
            trees[path] = ast.parse(text)
        except (SyntaxError, ValueError):
            continue
    imports = {path: _fixture_names(tree) for path, tree in trees.items()}
    # Fixtures by module, and by class within a module.
    module_fixtures, class_fixtures = {}, {}
    for path, tree in trees.items():
        modules, bare = imports[path]
        own = module_fixtures.setdefault(path, {})
        for node in tree.body:
            if isinstance(node, _FUNCTIONS):
                name = _fixture_name(node, modules, bare)
                if name is not None:
                    own[name] = (path, node.lineno)
            elif isinstance(node, ast.ClassDef):
                members = class_fixtures.setdefault((path, node.lineno), {})
                for item in node.body:
                    if isinstance(item, _FUNCTIONS):
                        name = _fixture_name(item, modules, bare)
                        if name is not None:
                            members[name] = (path, item.lineno)

    def scopes(path, klass):
        """The fixture tables a test sees, nearest first."""
        if klass is not None:
            yield class_fixtures.get((path, klass.lineno), {})
        yield module_fixtures.get(path, {})
        directory = PurePosixPath(path).parent
        while True:
            yield module_fixtures.get(str(directory / "conftest.py"), {})
            if directory == PurePosixPath("."):
                break
            directory = directory.parent

    def resolve(name, path, klass, itself):
        """The fixture ``name`` means; ``itself`` is the requesting fixture's
        own place, skipped so that an override reaches the one it overrides."""
        for table in scopes(path, klass):
            found = table.get(name)
            if found is not None and found != itself:
                return found
        return None

    uses = []

    def consume(node, path, klass, method):
        modules, bare = imports[path]
        fixture = _fixture_name(node, modules, bare)
        if fixture is None and not node.name.startswith("test"):
            return
        itself = (path, node.lineno)
        wanted = [(n, line, "parameter") for n, line in _parameters(node, method)]
        wanted += [(n, line, "usefixtures") for n, line in _usefixtures(node, modules)]
        for name, line, rule in wanted:
            target = resolve(name, path, klass, itself if name == fixture else None)
            if target is not None:
                uses.append(FixtureUse(itself, target, name, line, rule))

    for path in sorted(trees):
        if not _is_test_module(path):
            continue
        modules, _ = imports[path]
        for node in trees[path].body:
            if isinstance(node, _FUNCTIONS):
                consume(node, path, None, False)
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                for name, line in _usefixtures(node, modules):
                    target = resolve(name, path, node, None)
                    if target is not None:
                        uses.append(
                            FixtureUse(
                                (path, node.lineno), target, name, line, "usefixtures"
                            )
                        )
                for item in node.body:
                    if isinstance(item, _FUNCTIONS):
                        static = any(
                            isinstance(d, ast.Name) and d.id == "staticmethod"
                            for d in item.decorator_list
                        )
                        consume(item, path, node, not static)
    return sorted(uses, key=lambda u: (u.consumer, u.line, u.name))
