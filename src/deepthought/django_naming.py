"""Django's column names for model fields, from Django's own code.

``djangonames/names.py`` builds each field with Django's own field class and
asks it ``get_attname_column()``: ``db_column`` when given, else the field's
attname (``author_id`` for a ``ForeignKey`` named ``author``). Django is the
release the project declares (``python_dependencies.declared``: a lockfile
pin, else a requirement's specifier, resolved by pip), installed from its
wheel (and its own dependencies, asgiref and sqlparse) with ``pip install
--only-binary :all: --target`` into the cache. Only Django runs; nothing
of the analyzed project is imported.

A project that declares no Django, or whose Django cannot be installed,
gets no names, and the reason is returned for a warning.
"""

from hashlib import sha256
from importlib import resources
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from .prisma_schema import _cache_directory
from .python_dependencies import declared

_TIMEOUT = 300
_DJANGO = re.compile(r"^django(\[[^\]]*\])?\s*([<>=!~;].*)?$", re.I)


def project_requirement(root):
    """The requirement string for Django the workspace declares (a pin
    first), or None."""
    found = []
    for requirement in declared(root):
        match = _DJANGO.match(requirement.strip())
        if match:
            specifier = (match.group(2) or "").split(";")[0].strip()
            found.append(f"Django{specifier}")
    pinned = sorted(r for r in found if "==" in r)
    if pinned:
        return pinned[-1]
    return sorted(found)[-1] if found else None


def install(requirement, environ):
    """The directory Django ``requirement`` is installed in, installing it
    into the cache when missing."""
    key = sha256(requirement.encode()).hexdigest()[:16]
    target = _cache_directory(environ) / "django" / key
    if (target / "django" / "__init__.py").is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = tempfile.mkdtemp(dir=target.parent, prefix=".install-")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "--disable-pip-version-check",
                "--no-input",
                "install",
                "--only-binary",
                ":all:",
                "--target",
                scratch,
                requirement,
            ],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            env={**environ, "PYTHONNOUSERSITE": "1"},
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"pip install {requirement} failed: {completed.stderr.strip()[-300:]}"
            )
        try:
            os.replace(scratch, target)
        except OSError:
            if not (target / "django" / "__init__.py").is_file():
                raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return target


def _key(request):
    return json.dumps(list(request), separators=(",", ":"))


def column_names(root, requests, environ=None):
    """(names, reason): ``names`` maps each request (field class name, field
    name, db_column or None) to Django's column name; ``reason`` is None, or
    why no name could be read."""
    environ = dict(os.environ if environ is None else environ)
    requests = sorted(set(requests), key=_key)
    if not requests:
        return {}, None
    requirement = project_requirement(root)
    if requirement is None:
        return {}, "no Django declared in a requirements file, pyproject or lockfile"
    try:
        target = install(requirement, environ)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        return {}, str(error)
    script = resources.files(__package__).joinpath("djangonames/names.py")
    with resources.as_file(script) as path:
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "import sys, runpy; sys.path.insert(0, sys.argv[1]); "
                    "runpy.run_path(sys.argv[2], run_name='__main__')",
                    str(target),
                    str(path),
                ],
                input=json.dumps([list(r) for r in requests]),
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
                env={k: v for k, v in environ.items() if k != "DJANGO_SETTINGS_MODULE"},
            )
        except subprocess.SubprocessError as error:
            return {}, str(error)
    if completed.returncode != 0:
        return {}, f"Django's naming failed: {completed.stderr.strip()[-300:]}"
    found = json.loads(completed.stdout or "{}")
    return {r: found[_key(r)] for r in requests if _key(r) in found}, None
