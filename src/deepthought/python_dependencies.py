"""The project's declared Python dependencies, installed for scip-python.

scip-python (pyright) types a call into a library only when that library is
importable: without it, ``session.add(obj)`` on a SQLAlchemy session has no
type and binds nowhere. Before scip-python runs, the dependencies the project
declares are installed into a scratch directory, from wheels only, so no
package's build or install code runs. The indexer's interpreter is an empty
virtual environment, so packages of the machine running the engine never
reach the index.

Declared means, in the workspace root and in every directory below it (a
service, a package of a monorepo), the ``requirements*.txt`` files;
``pyproject.toml`` ``[project]`` dependencies and optional dependencies,
``[dependency-groups]``, and Poetry's dependencies, dev-dependencies and
groups (``^`` and ``~`` constraints converted to PEP 440); and ``setup.cfg``
``install_requires`` and ``extras_require``; and Pipenv's ``Pipfile``. A
directory with a lockfile (``uv.lock``, ``poetry.lock``, ``Pipfile.lock``)
gives its pins instead of those ranges, so the versions are the ones the
project runs and do not move when a package is released. Hidden directories,
``node_modules`` and virtual environments are not read. One scip-python run
indexes the workspace, so it gets one set of packages: every directory's
declarations, installed together. The project itself is never installed. pip reads its
usual configuration (``PIP_INDEX_URL``,
``PIP_FIND_LINKS``, ``PIP_NO_INDEX``), so an offline mirror works unchanged.
"""

import configparser
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# A requirement line pip can take from a wheel index: a name, optional
# extras, specifiers and markers. URLs, paths and editable installs are not.
_REQUIREMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[^\]]*\])?\s*([<>=!~;(@ ]|$)")


def requirement_files(root):
    root = Path(root)
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_file() and p.name.startswith("requirements") and p.suffix == ".txt"
    )


_SKIP = {"node_modules", "site-packages", "__pycache__"}


def project_directories(root):
    """Workspace-relative directories that declare Python dependencies, in a
    fixed order: the root (``.``) and every directory below it with a
    ``requirements*.txt``, ``pyproject.toml``, ``setup.cfg``, ``Pipfile`` or
    lockfile."""
    root = Path(root)
    found = []
    for directory, names, filenames in os.walk(root):
        names[:] = sorted(
            n
            for n in names
            if n not in _SKIP
            and not n.startswith(".")
            # A virtual environment's packages are not the project's.
            and not (Path(directory) / n / "pyvenv.cfg").is_file()
        )
        if any(
            f in {"pyproject.toml", "setup.cfg", "Pipfile", *LOCKFILES}
            or (f.startswith("requirements") and f.endswith(".txt"))
            for f in filenames
        ):
            found.append(Path(directory).relative_to(root).as_posix())
    return found


# Lockfiles, in the order one is chosen when a directory has several.
LOCKFILES = ("uv.lock", "poetry.lock", "Pipfile.lock")


def lockfiles(root):
    """The workspace-relative lockfile each directory's pins come from."""
    root = Path(root)
    found = []
    for relative in project_directories(root):
        name = next((n for n in LOCKFILES if (root / relative / n).is_file()), None)
        if name is not None:
            found.append(name if relative == "." else f"{relative}/{name}")
    return found


def declared(root):
    """The requirement strings the workspace declares, in a fixed order.

    A directory with a lockfile contributes its pins (``name==version``) in
    place of its ``pyproject.toml``, ``setup.cfg`` and ``Pipfile`` ranges, so
    the index types against the versions the project runs."""
    root = Path(root)
    found = []
    for relative in project_directories(root):
        directory = root / relative
        for name in requirement_files(directory):
            found += _requirement_lines(directory / name, root)
        pins = _locked(directory)
        if pins is not None:
            found += pins
            continue
        found += _pyproject(directory / "pyproject.toml")
        found += _setup_cfg(directory / "setup.cfg")
        found += _pipfile(directory / "Pipfile")
    return sorted(set(found))


def _toml(path):
    try:
        import tomllib
    except ImportError:  # Python 3.10
        return None
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None


def _locked(directory):
    """The pins of the directory's first lockfile; None when it has none.

    Packages from a path, a VCS or a URL have no wheel index and are left
    out, as is the project itself."""
    for name in LOCKFILES:
        path = directory / name
        if not path.is_file():
            continue
        if name == "Pipfile.lock":
            try:
                document = json.loads(path.read_text())
            except (OSError, ValueError):
                return []
            pins = []
            for section in ("default", "develop"):
                for package, entry in (document.get(section) or {}).items():
                    if not isinstance(entry, dict) or not isinstance(
                        entry.get("version"), str
                    ):
                        continue
                    if any(k in entry for k in ("path", "file", "git", "editable")):
                        continue
                    markers = entry.get("markers")
                    pins.append(
                        package
                        + entry["version"]
                        + (f" ; {markers}" if isinstance(markers, str) else "")
                    )
            return [p for p in pins if _REQUIREMENT.match(p)]
        document = _toml(path)
        if document is None:
            return []
        pins = []
        for package in document.get("package", []):
            if not isinstance(package, dict):
                continue
            package_name, version = package.get("name"), package.get("version")
            source = package.get("source")
            if not isinstance(package_name, str) or not isinstance(version, str):
                continue
            if name == "uv.lock":
                # registry: an index; editable, virtual, path, git: no wheel.
                if not (isinstance(source, dict) and "registry" in source):
                    continue
            elif isinstance(source, dict) and source.get("type") != "legacy":
                # Poetry: no source is PyPI, legacy a private index.
                continue
            markers = package.get("markers")
            pins.append(
                f"{package_name}=={version}"
                + (f" ; {markers}" if isinstance(markers, str) else "")
            )
        return [p for p in pins if _REQUIREMENT.match(p)]
    return None


def _pipfile(path):
    """Pipenv's packages and dev-packages; their versions are PEP 440."""
    if not path.is_file():
        return []
    document = _toml(path)
    if document is None:
        return []
    found = []
    for section in ("packages", "dev-packages"):
        for name, constraint in (document.get(section) or {}).items():
            extras = []
            if isinstance(constraint, dict):
                if any(k in constraint for k in ("path", "file", "git", "editable")):
                    continue
                extras = constraint.get("extras", [])
                constraint = constraint.get("version", "*")
            if not isinstance(constraint, str):
                continue
            found.append(
                name
                + (f"[{','.join(extras)}]" if extras else "")
                + ("" if constraint.strip() in {"", "*"} else constraint.strip())
            )
    return [r for r in found if _REQUIREMENT.match(r)]


def _requirement_lines(path, root, seen=None):
    seen = set() if seen is None else seen
    if path in seen or not path.is_file():
        return []
    seen.add(path)
    lines = []
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.split(" #")[0].strip()
        if line.startswith(("-r ", "--requirement ")):
            included = (path.parent / line.split(None, 1)[1]).resolve()
            if included.is_relative_to(root.resolve()):
                lines += _requirement_lines(included, root, seen)
        elif line and not line.startswith(("#", "-")) and _REQUIREMENT.match(line):
            if "://" not in line and " @ " not in line:
                lines.append(line)
    return lines


def _pyproject(path):
    if not path.is_file():
        return []
    try:
        import tomllib
    except ImportError:  # Python 3.10
        return []
    try:
        document = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return []
    project = document.get("project", {})
    found = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        found += extra
    # PEP 735 groups; an include-group entry is a table, not a requirement.
    for group in document.get("dependency-groups", {}).values():
        found += [r for r in group if isinstance(r, str)]
    poetry = document.get("tool", {}).get("poetry", {})
    tables = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {})]
    tables += [g.get("dependencies", {}) for g in poetry.get("group", {}).values()]
    for table in tables:
        for name, constraint in table.items():
            requirement = _poetry_requirement(name, constraint)
            if requirement is not None:
                found.append(requirement)
    return [r for r in found if isinstance(r, str) and _REQUIREMENT.match(r)]


def _poetry_requirement(name, constraint):
    """A Poetry dependency as a PEP 508 requirement; None for Python itself
    and for path, git and URL dependencies, which have no wheel index."""
    if name.lower() == "python":
        return None
    extras = []
    if isinstance(constraint, list):  # multiple constraints: take the first
        constraint = constraint[0] if constraint else "*"
    if isinstance(constraint, dict):
        if any(key in constraint for key in ("path", "git", "url")):
            return None
        extras = constraint.get("extras", [])
        constraint = constraint.get("version", "*")
    if not isinstance(constraint, str):
        return None
    specifier = ",".join(
        filter(None, (_poetry_specifier(c.strip()) for c in constraint.split(",")))
    )
    return name + (f"[{','.join(extras)}]" if extras else "") + specifier


def _poetry_specifier(constraint):
    """^1.2.3, ~1.2, 1.2.*, 1.2 and plain comparisons, as PEP 440."""
    if constraint in {"", "*"}:
        return ""
    if constraint[0] in "<>=!":
        return constraint
    if constraint.startswith(("^", "~")) and not constraint.startswith("~="):
        version = constraint[1:].strip()
        parts = [int(p) if p.isdigit() else 0 for p in version.split(".")]
        if constraint[0] == "^":
            # The first non-zero component may not change.
            index = next((i for i, p in enumerate(parts) if p), len(parts) - 1)
        else:
            index = 0 if len(parts) == 1 else 1
        upper = parts[: index + 1]
        upper[-1] += 1
        return f">={version},<{'.'.join(map(str, upper))}"
    if constraint.startswith("~="):
        return constraint
    if constraint.endswith(".*"):
        return "==" + constraint
    return "==" + constraint


def _setup_cfg(path):
    if not path.is_file():
        return []
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(path.read_text())
    except (configparser.Error, UnicodeDecodeError):
        return []
    found = []
    if parser.has_option("options", "install_requires"):
        found += parser.get("options", "install_requires").splitlines()
    if parser.has_section("options.extras_require"):
        for _, value in parser.items("options.extras_require"):
            found += value.splitlines()
    return [r.strip() for r in found if r.strip() and _REQUIREMENT.match(r.strip())]


def install(root, scratch, *, environ=None, timeout=600):
    """Install the declared dependencies under ``scratch``.

    Returns ``{"status", "python", "projects", "lockfiles", "requirements",
    "packages", "failed", "without_dependencies", "detail", "bin", "site"}``;
    ``projects`` names the directories the requirements were read from,
    ``lockfiles`` those pinned by a lockfile. ``without_dependencies`` names
    the requirements installed from their own wheel although one of their
    dependencies has none: the indexer reads their source, nothing runs it.
    ``status`` is ``none`` (nothing declared), ``installed``, ``partial``
    (some requirements, or their dependencies, have no installable wheel) or
    ``failed`` (none could be installed, or pip is unavailable). ``bin``
    holds the empty interpreter scip-python runs, ``site`` the packages.
    """
    scratch = Path(scratch)
    env = dict(os.environ if environ is None else environ)
    environment = scratch / "python"
    site = scratch / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(environment)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    requirements = declared(root)
    outcome = {
        "projects": project_directories(root),
        "lockfiles": lockfiles(root),
        "status": "none",
        "python": "{}.{}.{}".format(*sys.version_info[:3]),
        "requirements": requirements,
        "packages": [],
        "failed": [],
        "without_dependencies": [],
        "detail": None,
        "bin": str(environment / ("Scripts" if os.name == "nt" else "bin")),
        "site": str(site),
    }
    if not requirements:
        return outcome

    def pip(*arguments):
        try:
            return subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "--disable-pip-version-check",
                    "--no-input",
                    *arguments,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**env, "PYTHONNOUSERSITE": "1"},
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None

    def install_all(items, *options):
        return pip(
            "install",
            "--only-binary",
            ":all:",
            "--target",
            str(site),
            "--upgrade",
            *options,
            *items,
        )

    result = install_all(requirements)
    if result is None or result.returncode:
        # One requirement without a wheel must not cost the rest: each is
        # tried alone, and the ones pip refuses are named.
        outcome["detail"] = (
            "pip timed out" if result is None else (result.stderr or "")[-2000:]
        )
        for requirement in requirements:
            alone = pip("install", "--dry-run", "--only-binary", ":all:", requirement)
            if alone is None or alone.returncode:
                # Its own wheel may still exist when a dependency has none
                # (instana needs fysom, a source-only package): its source
                # is what the indexer reads, so it is installed on its own.
                own = pip(
                    "install",
                    "--dry-run",
                    "--only-binary",
                    ":all:",
                    "--no-deps",
                    requirement,
                )
                if own is None or own.returncode:
                    outcome["failed"].append(requirement)
                else:
                    outcome["without_dependencies"].append(requirement)
        # The rest are installed together, so pip resolves them as one set
        # and a lockfile's pins stay the versions installed.
        rest = [
            r
            for r in requirements
            if r not in outcome["failed"] and r not in outcome["without_dependencies"]
        ]
        together = install_all(rest) if rest else None
        if rest and (together is None or together.returncode):
            # They conflict (two services pin different versions): each is
            # installed in turn, and the one installed last wins.
            for requirement in rest:
                alone = install_all([requirement])
                if alone is None or alone.returncode:
                    outcome["failed"].append(requirement)
        for requirement in list(outcome["without_dependencies"]):
            own = install_all([requirement], "--no-deps")
            if own is None or own.returncode:
                outcome["without_dependencies"].remove(requirement)
                outcome["failed"].append(requirement)
    listed = pip("list", "--path", str(site), "--format", "freeze")
    if listed is not None and not listed.returncode:
        outcome["packages"] = sorted(
            line.strip() for line in listed.stdout.splitlines() if "==" in line
        )
    if outcome["failed"] == requirements:
        outcome["status"] = "failed"
    else:
        incomplete = outcome["failed"] or outcome["without_dependencies"]
        outcome["status"] = "partial" if incomplete else "installed"
    return outcome
