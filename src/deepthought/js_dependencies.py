"""The project's declared JavaScript and TypeScript packages, for scip-typescript.

The TypeScript compiler types a call into a package only when the package's
declarations are in ``node_modules``. When the workspace declares packages
and has no ``node_modules``, the workspace's sources are copied to a scratch
directory and its package manager installs there, with install scripts off,
so no package's code runs and the workspace is left as it was. The manager
follows the lockfile: ``pnpm-lock.yaml`` pnpm, ``yarn.lock`` yarn,
``package-lock.json`` ``npm ci``, otherwise ``npm install``. They read their
usual configuration (``.npmrc``, ``npm_config_registry``), so a mirror works.
A workspace that already has ``node_modules`` is indexed as it is.
"""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

LOCKFILES = (
    ("pnpm-lock.yaml", "pnpm", ["install", "--frozen-lockfile", "--ignore-scripts"]),
    ("yarn.lock", "yarn", ["install", "--frozen-lockfile", "--ignore-scripts"]),
    (
        "package-lock.json",
        "npm",
        ["ci", "--ignore-scripts", "--no-audit", "--no-fund"],
    ),
)
NO_LOCKFILE = (
    "npm",
    ["install", "--ignore-scripts", "--no-audit", "--no-fund", "--no-package-lock"],
)
_COPY_SKIP = {".git", ".hg", ".svn", "node_modules"}


def declares_packages(root):
    """Whether any package.json outside node_modules declares a dependency."""
    for directory, names, filenames in os.walk(root):
        names[:] = [n for n in names if n not in _COPY_SKIP and not n.startswith(".")]
        if "package.json" in filenames:
            try:
                package = json.loads((Path(directory) / "package.json").read_text())
            except (OSError, ValueError):
                continue
            if isinstance(package, dict) and any(
                package.get(key)
                for key in (
                    "dependencies",
                    "devDependencies",
                    "peerDependencies",
                    "optionalDependencies",
                )
            ):
                return True
    return False


def _manager(directory):
    for lockfile, manager, arguments in LOCKFILES:
        if (Path(directory) / lockfile).is_file():
            return manager, arguments, lockfile
    return (*NO_LOCKFILE, None)


def install_directories(root):
    """Workspace-relative directories to install in, in a fixed order.

    Each directory with a lockfile, except one inside another lockfile's
    directory (a pnpm or yarn workspace installs its packages from the root);
    the root alone when no lockfile exists anywhere."""
    root = Path(root)
    found = []
    for directory, names, filenames in os.walk(root):
        names[:] = sorted(
            n for n in names if n not in _COPY_SKIP and not n.startswith(".")
        )
        relative = Path(directory).relative_to(root).as_posix()
        if any(lockfile in filenames for lockfile, _, _ in LOCKFILES):
            if not any(
                relative == d or relative.startswith(d + "/") or d == "." for d in found
            ):
                found.append(relative)
    return found or ["."]


def prepare(root, scratch, *, environ=None, timeout=600):
    """The directory scip-typescript should index, and what was installed.

    Returns ``{"status", "root", "installs", "packages", "detail"}``.
    ``status`` is ``none`` (nothing declared), ``present`` (node_modules is
    already where every install would put it), ``installed``, ``partial``
    (some directories' installs failed) or ``failed``. ``root`` is the
    workspace, or the scratch copy the packages were installed into;
    ``installs`` names each directory's manager, command, lockfile digest and
    outcome.
    """
    root = Path(root)
    outcome = {
        "status": "none",
        "root": str(root),
        "installs": [],
        "packages": 0,
        "detail": None,
    }
    if all((root / d / "node_modules").is_dir() for d in install_directories(root)):
        outcome["status"] = "present"
        return outcome
    if not declares_packages(root):
        return outcome
    env = dict(os.environ if environ is None else environ)
    copy = Path(scratch) / "workspace"
    shutil.copytree(
        root,
        copy,
        symlinks=True,
        ignore=lambda _directory, names: [n for n in names if n in _COPY_SKIP],
    )
    for relative in install_directories(root):
        manager, arguments, lockfile = _manager(root / relative)
        install = {
            "directory": relative,
            "manager": manager,
            "command": [manager, *arguments],
            "lockfile_sha256": (
                hashlib.sha256((root / relative / lockfile).read_bytes()).hexdigest()
                if lockfile is not None
                else None
            ),
            "status": "failed",
        }
        outcome["installs"].append(install)
        executable = shutil.which(manager, path=env.get("PATH"))
        if executable is None:
            install["detail"] = f"{manager} is not installed"
            continue
        try:
            result = subprocess.run(
                [executable, *arguments],
                cwd=copy / relative,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env={**env, "CI": "1", "npm_config_ignore_scripts": "true"},
            )
        except subprocess.TimeoutExpired:
            install["detail"] = f"{manager} timed out"
            continue
        if result.returncode or not (copy / relative / "node_modules").is_dir():
            install["detail"] = (result.stdout + result.stderr)[-2000:]
            continue
        install["status"] = "installed"
    succeeded = [i for i in outcome["installs"] if i["status"] == "installed"]
    if not succeeded:
        outcome["status"] = "failed"
        outcome["detail"] = "; ".join(
            f"{i['directory']}: {i.get('detail', '')[-300:]}"
            for i in outcome["installs"]
        )
        return outcome
    if len(succeeded) < len(outcome["installs"]):
        outcome["status"] = "partial"
    else:
        outcome["status"] = "installed"
    outcome["root"] = str(copy)
    outcome["packages"] = sum(
        _count(copy / i["directory"] / "node_modules") for i in succeeded
    )
    return outcome


def _count(node_modules):
    count = 0
    for path in node_modules.iterdir():
        if path.name.startswith("."):
            continue
        count += sum(1 for _ in path.iterdir()) if path.name.startswith("@") else 1
    return count
