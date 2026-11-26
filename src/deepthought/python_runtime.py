"""The Python interpreter that reads a repository's Python sources.

Python's ``ast`` parses only the syntax of the interpreter it runs in: under
Python 3.13 a module written for 3.14 (``except A, B:``) does not parse, and
the rules that read ``ast`` (``deepthought.python_ast_facts``) would see
nothing in it. So the sources are read by the newest Python the repository
declares, in ``.python-version`` files and the ``requires-python`` of
``pyproject.toml`` files. uv (a required dependency) finds that interpreter,
installing a managed CPython when none is present. When the running
interpreter is at least as new it reads them itself. When no declared
interpreter can be obtained, the running one reads them and the reason is
reported; files it cannot parse are reported by the caller. A request whose
lowest admitted version is not newer than the running interpreter needs no
other interpreter, since newer Pythons parse older syntax.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from . import python_ast_facts

_SKIP = {".git", ".hg", ".svn", "node_modules", "__pycache__", "site-packages"}
_RUNNING = "{}.{}.{}".format(*sys.version_info[:3])


@dataclass(frozen=True)
class Interpreter:
    """``executable`` reads the sources (None: the running interpreter) at
    ``version``; ``declared`` are the requests found, and ``errors`` why any
    could not be obtained."""

    executable: str | None
    version: str
    declared: tuple = ()
    errors: tuple = ()


def _toml_parser():
    """tomllib, tomli on Python 3.10, or None when neither is installed (a
    core-only install on Python 3.10)."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            return None
    return tomllib


def declared_pythons(root):
    """The Python requests a repository declares, sorted: each
    ``.python-version`` file's first entry and each ``pyproject.toml``'s
    ``requires-python``. Virtual environments and dependency trees are
    skipped. Without a TOML parser, ``pyproject.toml`` files are not read."""
    return _declared(root)[0]


def _declared(root):
    """``declared_pythons(root)`` and the ``pyproject.toml`` files left unread
    because no TOML parser is installed."""
    tomllib = _toml_parser()
    found, unread = set(), []
    for directory, names, files in os.walk(root):
        names[:] = [
            n
            for n in names
            if n not in _SKIP and not (Path(directory) / n / "pyvenv.cfg").is_file()
        ]
        if ".python-version" in files:
            text = (Path(directory) / ".python-version").read_text(
                encoding="utf-8", errors="replace"
            )
            entries = [
                line.strip()
                for line in text.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            if entries:
                found.add(entries[0])
        if "pyproject.toml" in files and tomllib is None:
            unread.append(Path(directory) / "pyproject.toml")
        elif "pyproject.toml" in files:
            try:
                project = tomllib.loads(
                    (Path(directory) / "pyproject.toml").read_text(encoding="utf-8")
                ).get("project", {})
            except (tomllib.TOMLDecodeError, UnicodeDecodeError):
                continue
            requested = project.get("requires-python")
            if isinstance(requested, str) and requested.strip():
                found.add(requested.strip())
    return tuple(sorted(found)), unread


def _uv():
    """The uv binary, or RuntimeError saying why there is none."""
    try:
        from uv import find_uv_bin

        return find_uv_bin()
    except (ImportError, OSError) as error:
        raise RuntimeError(f"uv is not available ({error})") from error


def _run(argv, timeout, environ=None, stdin=None):
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=environ,
    )


def _failure(result):
    """The last line a failed command wrote to stderr, or its exit status
    when it wrote nothing (killed by a signal, such as the OOM killer)."""
    lines = result.stderr.strip().splitlines()
    return lines[-1] if lines else f"exit status {result.returncode}"


def _version(executable, environ):
    result = _run(
        [
            executable,
            "-I",
            "-c",
            "import sys; print('%d.%d.%d' % sys.version_info[:3])",
        ],
        60,
        environ,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "no version")
    return result.stdout.strip()


def _find(request, environ, timeout):
    """The executable uv finds (installing a managed CPython if needed) for
    ``request``."""
    uv = _uv()
    find = [uv, "python", "find", "--no-project", "--no-config", request]
    result = _run(find, 60, environ)
    if result.returncode:
        install = _run(
            [uv, "python", "install", "--no-config", request], timeout, environ
        )
        if install.returncode:
            raise RuntimeError(_failure(install))
        result = _run(find, 60, environ)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "not found after install")
    return result.stdout.strip()


def _key(version):
    return tuple(int(part) for part in version.split(".")[:2])


_VERSION = re.compile(r"(\d+)\.(\d+)")


def _minimum(request):
    """The lowest ``(major, minor)`` a request admits (``3.14``,
    ``cpython-3.14.1``, ``>=3.12,<4``), or None. Upper bounds and
    exclusions do not raise it."""
    bounds = []
    for clause in request.split(","):
        clause = clause.strip()
        if clause.startswith(("<", "!=")):
            continue
        found = _VERSION.search(clause)
        if found:
            bounds.append((int(found[1]), int(found[2])))
    return max(bounds, default=None)


def interpreter_for(root, environ=None, timeout=600):
    """The interpreter that reads ``root``'s Python sources: the newest one
    the repository declares, or the running one when that is as new."""
    declared, unread = _declared(root)
    candidates, errors = [], []
    if unread:
        errors.append(
            f"{len(unread)} pyproject.toml not read: "
            "provision tomli to read requires-python on Python 3.10"
        )
    for request in declared:
        minimum = _minimum(request)
        if minimum is None or minimum <= _key(_RUNNING):
            # The running interpreter parses that syntax already.
            continue
        try:
            executable = _find(request, environ, timeout)
            candidates.append((_version(executable, environ), executable))
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            errors.append(f"{request}: {error}")
    newest = max(candidates, key=lambda c: _key(c[0]), default=None)
    if newest is None or _key(newest[0]) <= _key(_RUNNING):
        return Interpreter(None, _RUNNING, declared, tuple(errors))
    return Interpreter(newest[1], newest[0], declared, tuple(errors))


def read_ast_facts(root, sources, environ=None, timeout=600):
    """``python_ast_facts.ast_facts`` of ``sources`` read by
    ``interpreter_for(root)``, and that interpreter. If the other
    interpreter fails, the running one reads them and the failure is added
    to the interpreter's ``errors``."""
    interpreter = interpreter_for(root, environ, timeout)
    if interpreter.executable is None:
        return python_ast_facts.ast_facts(sources), interpreter
    with tempfile.TemporaryDirectory() as scratch:
        here = Path(python_ast_facts.__file__).parent
        for name in python_ast_facts.MODULES:
            shutil.copyfile(here / name, Path(scratch) / name)
        try:
            result = _run(
                [
                    interpreter.executable,
                    "-I",
                    str(Path(scratch) / "python_ast_facts.py"),
                ],
                timeout,
                environ,
                json.dumps(sources),
            )
            if result.returncode:
                raise RuntimeError(_failure(result))
            return json.loads(result.stdout), interpreter
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            failed = Interpreter(
                None,
                _RUNNING,
                interpreter.declared,
                (*interpreter.errors, f"{interpreter.version}: {error}"),
            )
            return python_ast_facts.ast_facts(sources), failed
