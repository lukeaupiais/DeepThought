"""The project's declared JavaScript and TypeScript packages, for scip-typescript.

The TypeScript compiler types a call into a package only when the package's
declarations are in ``node_modules``. When the workspace declares packages
and has no ``node_modules``, the workspace's sources are copied to a scratch
directory and its package manager installs there, with install scripts off,
so no package's code runs and the workspace is left as it was. The manager
follows the lockfile: ``pnpm-lock.yaml`` pnpm, ``bun.lock`` or ``bun.lockb``
bun, ``yarn.lock`` Yarn (Yarn 2 and later as the project's own release, with
the node-modules linker), ``npm-shrinkwrap.json`` or ``package-lock.json``
``npm ci``, otherwise ``npm install``. They read their
usual configuration (``.npmrc``, ``npm_config_registry``), so a mirror works.
``npm ci`` first fetches every pinned tarball from the configured registry
(``--replace-registry-host=always``; the lockfile's integrity hashes still
check each one), then, if that fails, from the hosts the lockfile names.
A workspace that already has ``node_modules`` is indexed as it is; in a
copy, a directory that already has ``node_modules`` links to it.

After an install, each declared package that ships no declarations gets
its DefinitelyTyped package (``@types/<name>``), as the TypeScript language
server's automatic type acquisition gives one: without it the compiler
types no call into a plain JavaScript package (``express``, ``koa-router``),
so the index names no library method there. Which packages DefinitelyTyped
has, and the version of each for the compiler scip-typescript runs, comes
from the pinned ``types-registry`` package (``TYPES_REGISTRY_VERSION``),
which the engine installs into its cache; the versions it names are
installed exactly, with npm, scripts off, into a scratch prefix and moved
into ``node_modules`` where nothing is there yet. A failed acquisition is
recorded on the install (``types``) and makes the run ``partial``.
"""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

# A directory's lockfile, in the order one is chosen, and the command that
# installs exactly what it pins without running any package's scripts.
LOCKFILES = (
    ("pnpm-lock.yaml", "pnpm", ["install", "--frozen-lockfile", "--ignore-scripts"]),
    ("bun.lock", "bun", ["install", "--frozen-lockfile", "--ignore-scripts"]),
    ("bun.lockb", "bun", ["install", "--frozen-lockfile", "--ignore-scripts"]),
    ("yarn.lock", "yarn", ["install", "--frozen-lockfile", "--ignore-scripts"]),
    (
        "npm-shrinkwrap.json",
        "npm",
        ["ci", "--ignore-scripts", "--no-audit", "--no-fund"],
    ),
    (
        "package-lock.json",
        "npm",
        ["ci", "--ignore-scripts", "--no-audit", "--no-fund"],
    ),
)
# Yarn 2 and later (a yarn.lock with __metadata): --immutable keeps the
# lockfile, skip-build and enableScripts run no package's scripts, and the
# node-modules linker writes node_modules instead of Plug'n'Play, which the
# TypeScript compiler cannot read.
YARN_BERRY = ["install", "--immutable", "--mode=skip-build"]
YARN_BERRY_ENVIRONMENT = {
    "YARN_NODE_LINKER": "node-modules",
    "YARN_ENABLE_SCRIPTS": "false",
    "YARN_ENABLE_TELEMETRY": "0",
    "YARN_ENABLE_GLOBAL_CACHE": "false",
    "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
}
NO_LOCKFILE = (
    "npm",
    ["install", "--ignore-scripts", "--no-audit", "--no-fund", "--no-package-lock"],
)
_COPY_SKIP = {".git", ".hg", ".svn", "node_modules"}

# DefinitelyTyped's package list, pinned, and the compiler release whose
# dist-tag picks each package's version (scip-typescript 0.6.2 runs
# TypeScript 6.0); a package without that tag takes its latest version.
TYPES_REGISTRY_VERSION = "0.1.794"
TYPES_TAG = "ts6.0"


_DEPENDENCY_KEYS = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)


def _package(directory):
    try:
        package = json.loads((Path(directory) / "package.json").read_text())
    except (OSError, ValueError):
        return None
    return package if isinstance(package, dict) else None


def _declares(package):
    return package is not None and any(package.get(k) for k in _DEPENDENCY_KEYS)


def declares_packages(root):
    """Whether any package.json outside node_modules declares a dependency."""
    for directory, names, filenames in os.walk(root):
        names[:] = [n for n in names if n not in _COPY_SKIP and not n.startswith(".")]
        if "package.json" in filenames and _declares(_package(directory)):
            return True
    return False


def _managers(directory):
    """The installs to try in a directory, in order: one per lockfile it
    holds (a stale lockfile fails its frozen install, and the next one is
    tried), or ``npm install`` when it holds none."""
    found = []
    for lockfile, manager, arguments in LOCKFILES:
        if (Path(directory) / lockfile).is_file():
            if lockfile == "yarn.lock" and _yarn_berry(directory):
                found.append(("yarn", YARN_BERRY, lockfile))
            elif manager == "npm":
                # A lockfile may pin tarballs to a mirror that is gone or
                # unreachable (nestjs-realworld pins 194 to one, and npm
                # retries it until the install times out). The configured
                # registry serves the same bytes, checked against the
                # lockfile's integrity hashes; the lockfile's own hosts
                # are tried next, for packages only a private host has.
                found.append(
                    (manager, [*arguments, "--replace-registry-host=always"], lockfile)
                )
                found.append((manager, arguments, lockfile))
            else:
                found.append((manager, arguments, lockfile))
    return found or [(*NO_LOCKFILE, None)]


def _no_scripts():
    """Settings that keep every package's scripts from running.

    npm runs a ``file:`` dependency's ``prepare`` script even with
    ``--ignore-scripts`` (npm 10.9); a shell that does nothing makes any
    script npm still starts a no-op."""
    settings = {
        "CI": "1",
        "npm_config_ignore_scripts": "true",
        # pnpm would otherwise download and run the release packageManager
        # names (hono pins pnpm@12.6.0); the pnpm on PATH installs.
        "npm_config_manage_package_manager_versions": "false",
    }
    noop = shutil.which("true")
    if noop is not None:
        settings["npm_config_script_shell"] = noop
    return settings


def _yarn_berry(directory):
    try:
        with open(Path(directory) / "yarn.lock", encoding="utf-8") as lockfile:
            head = lockfile.read(4096)
    except (OSError, UnicodeDecodeError):
        return False
    return "\n__metadata:" in "\n" + head


def _yarn_berry_command(directory, environ):
    """The project's own Yarn: the release ``.yarnrc.yml`` names, else the
    version ``packageManager`` names, through corepack."""
    try:
        rc = (Path(directory) / ".yarnrc.yml").read_text()
    except (OSError, UnicodeDecodeError):
        rc = ""
    for line in rc.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "yarnPath" and value.strip():
            release = Path(directory) / value.strip().strip("\"'")
            node = shutil.which("node", path=environ.get("PATH"))
            if release.is_file() and node:
                return [node, str(release)]
    corepack = shutil.which("corepack", path=environ.get("PATH"))
    return [corepack, "yarn"] if corepack else None


def install_directories(root):
    """Workspace-relative directories to install in, in a fixed order.

    Each directory with a lockfile, and each directory whose package.json
    declares packages, except one inside a directory that installs it: a
    directory with a lockfile, or a workspace root (``workspaces`` in its
    package.json, or a ``pnpm-workspace.yaml``), installs its packages from
    there. A repository of packages that are not workspaces, with no
    lockfile, so installs each package where it is. A lockfile counts only
    beside a package.json. An npm lockfile in a directory that is not a
    workspace root installs that directory alone, so a project below it with
    its own lockfile (an app kept in a subdirectory) installs where it is."""
    root = Path(root)
    found, covering = [], []
    for directory, names, filenames in os.walk(root):
        names[:] = sorted(
            n for n in names if n not in _COPY_SKIP and not n.startswith(".")
        )
        relative = Path(directory).relative_to(root).as_posix()
        # A lockfile without a package.json beside it installs nothing.
        lockfiles = (
            [name for name, _, _ in LOCKFILES if name in filenames]
            if "package.json" in filenames
            else []
        )
        locked = bool(lockfiles)
        if any(
            (relative.startswith(d + "/") or d == ".") and (everything or not locked)
            for d, everything in covering
        ):
            continue
        package = _package(directory) if "package.json" in filenames else None
        workspace = "pnpm-workspace.yaml" in filenames or bool(
            package and package.get("workspaces")
        )
        if locked or workspace or _declares(package):
            found.append(relative)
        if locked or workspace:
            npm_only = all(
                manager == "npm" for name, manager, _ in LOCKFILES if name in lockfiles
            )
            covering.append((relative, workspace or not npm_only))
    return found


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
    directories = install_directories(root)
    if not directories or not declares_packages(root):
        return outcome
    if all((root / d / "node_modules").is_dir() for d in directories):
        outcome["status"] = "present"
        return outcome
    env = dict(os.environ if environ is None else environ)
    copy = Path(scratch) / "workspace"
    shutil.copytree(
        root,
        copy,
        symlinks=True,
        ignore=lambda _directory, names: [n for n in names if n in _COPY_SKIP],
    )
    for relative in directories:
        installed = root / relative / "node_modules"
        if installed.is_dir():
            # Packages already installed in the workspace are used as they
            # are, as a workspace with node_modules everywhere is; the copy
            # skipped them.
            (copy / relative / "node_modules").symlink_to(
                installed.resolve(), target_is_directory=True
            )
            outcome["installs"].append(
                {
                    "directory": relative,
                    "status": "installed",
                    "manager": None,
                    "command": None,
                    "lockfile": None,
                    "lockfile_sha256": None,
                    "source": "workspace",
                }
            )
            continue
        attempts = []
        for manager, arguments, lockfile in _managers(root / relative):
            install = _install(
                root, copy, relative, manager, arguments, lockfile, env, timeout
            )
            if install["status"] == "installed":
                break
            attempts.append(install)
            # A failed install may leave a partial node_modules behind.
            shutil.rmtree(copy / relative / "node_modules", ignore_errors=True)
        if install["status"] == "installed" and attempts:
            install["failed_attempts"] = [
                {k: a.get(k) for k in ("lockfile", "manager", "detail")}
                for a in attempts
            ]
        outcome["installs"].append(install)
    succeeded = [i for i in outcome["installs"] if i["status"] == "installed"]
    for install in succeeded:
        if install.get("source") != "workspace":
            install["types"] = acquire_types(copy / install["directory"], env, timeout)
    if not succeeded:
        outcome["status"] = "failed"
        outcome["detail"] = "; ".join(
            f"{i['directory']}: {i.get('detail', '')[-300:]}"
            for i in outcome["installs"]
        )
        return outcome
    if len(succeeded) < len(outcome["installs"]) or any(
        i.get("types", {}).get("status") == "failed" for i in succeeded
    ):
        outcome["status"] = "partial"
    else:
        outcome["status"] = "installed"
    outcome["root"] = str(copy)
    outcome["packages"] = sum(
        _count(copy / i["directory"] / "node_modules") for i in succeeded
    )
    return outcome


def _install(root, copy, relative, manager, arguments, lockfile, env, timeout):
    install = {
        "directory": relative,
        "manager": manager,
        "command": [manager, *arguments],
        "lockfile": lockfile,
        "lockfile_sha256": (
            hashlib.sha256((root / relative / lockfile).read_bytes()).hexdigest()
            if lockfile is not None
            else None
        ),
        "status": "failed",
    }
    extra = {}
    if arguments is YARN_BERRY:
        command = _yarn_berry_command(copy / relative, env)
        extra = YARN_BERRY_ENVIRONMENT
        if command is None:
            install["detail"] = "corepack is not installed"
            return install
    else:
        executable = shutil.which(manager, path=env.get("PATH"))
        if executable is None:
            install["detail"] = f"{manager} is not installed"
            return install
        command = [executable]
    try:
        result = subprocess.run(
            [*command, *arguments],
            cwd=copy / relative,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**env, **_no_scripts(), **extra},
        )
    except subprocess.TimeoutExpired:
        install["detail"] = f"{manager} timed out"
        return install
    if result.returncode or not (copy / relative / "node_modules").is_dir():
        install["detail"] = (result.stdout + result.stderr)[-2000:]
        return install
    install["status"] = "installed"
    return install


def _count(node_modules):
    count = 0
    for path in node_modules.iterdir():
        if path.name.startswith("."):
            continue
        count += sum(1 for _ in path.iterdir()) if path.name.startswith("@") else 1
    return count


def _ships_declarations(directory):
    """Whether an installed package gives the compiler declarations: a
    ``types``/``typings`` entry, a ``types`` export condition, or an
    ``index.d.ts`` beside its package.json."""
    package = _package(directory)
    if package is None:
        return False
    if package.get("types") or package.get("typings"):
        return True
    if '"types"' in json.dumps(package.get("exports") or {}):
        return True
    return (Path(directory) / "index.d.ts").is_file()


def untyped_packages(directory):
    """The packages ``directory``'s package.json declares that are
    installed in its ``node_modules``, ship no declarations and have no
    ``@types`` package installed, as (name, DefinitelyTyped name), sorted."""
    package = _package(directory) or {}
    modules = Path(directory) / "node_modules"
    found = set()
    for key in _DEPENDENCY_KEYS:
        declared = package.get(key)
        if not isinstance(declared, dict):
            continue
        for name in declared:
            if not isinstance(name, str) or name.startswith("@types/"):
                continue
            typed = name[1:].replace("/", "__") if name.startswith("@") else name
            if (modules / "@types" / typed).is_dir():
                continue
            installed = modules / name
            if (installed / "package.json").is_file() and not _ships_declarations(
                installed
            ):
                found.add((name, typed))
    return sorted(found)


def _types_registry(environ):
    """{DefinitelyTyped name: {dist-tag: version}} of the pinned
    types-registry, installed into the engine's cache when missing."""
    from .tool_provisioning import npm_package

    directory = npm_package("types-registry", TYPES_REGISTRY_VERSION, environ)
    index = directory / "node_modules" / "types-registry" / "index.json"
    return json.loads(index.read_text())["entries"]


def acquire_types(directory, environ, timeout):
    """Install the ``@types`` packages of ``directory``'s untyped packages
    into its ``node_modules``. Returns ``{"status", "packages", "detail"}``:
    ``none`` (nothing to acquire), ``acquired`` with the ``@types/<name>@
    <version>`` installed, or ``failed``."""
    from .tool_provisioning import ToolInstallFailed

    result = {"status": "none", "packages": [], "detail": None}
    untyped = untyped_packages(directory)
    if not untyped:
        return result
    try:
        registry = _types_registry(environ)
    except (ToolInstallFailed, OSError, ValueError, KeyError) as error:
        result.update(status="failed", detail=f"types-registry: {error}"[-600:])
        return result
    wanted = []
    for _, typed in untyped:
        tags = registry.get(typed)
        if isinstance(tags, dict) and (tags.get(TYPES_TAG) or tags.get("latest")):
            wanted.append(f"@types/{typed}@{tags.get(TYPES_TAG) or tags['latest']}")
    if not wanted:
        return result
    npm = shutil.which("npm", path=environ.get("PATH"))
    if npm is None:
        result.update(status="failed", detail="npm is not installed")
        return result
    modules = Path(directory) / "node_modules"
    prefix = Path(directory).parent / f".types-{Path(directory).name or 'root'}"
    try:
        completed = subprocess.run(
            [
                npm,
                "install",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--no-save",
                "--no-package-lock",
                "--prefix",
                str(prefix),
                *wanted,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**environ, **_no_scripts()},
        )
        if completed.returncode or not (prefix / "node_modules").is_dir():
            result.update(
                status="failed",
                detail=(completed.stdout + completed.stderr)[-600:],
            )
            return result
        # Move what the prefix installed (the @types packages and the
        # packages they depend on) where nothing of that name is installed.
        for entry in sorted((prefix / "node_modules").iterdir()):
            if entry.name.startswith("."):
                continue
            children = (
                sorted(entry.iterdir()) if entry.name.startswith("@") else [entry]
            )
            for child in children:
                relative = child.relative_to(prefix / "node_modules")
                if not (modules / relative).exists():
                    (modules / relative).parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(child), str(modules / relative))
    except subprocess.TimeoutExpired:
        result.update(status="failed", detail="npm timed out acquiring @types")
        return result
    finally:
        shutil.rmtree(prefix, ignore_errors=True)
    result.update(status="acquired", packages=sorted(wanted))
    return result
