"""TypeORM's default table names, from TypeORM's own naming strategy.

``typeormnames/names.js`` calls ``DefaultNamingStrategy.tableName`` of the
typeorm release the entity's project pins, installed into the cache with
``npm install --ignore-scripts`` (no package scripts run, and nothing of the
analyzed project is loaded). The release is looked up from the entity's
directory up to the repository root: an installed ``node_modules/typeorm``,
then a lockfile entry (npm, pnpm, yarn or bun), then a declared ``typeorm``
in a package.json, a range resolved to its newest release with ``npm view``.
An installed or locked release anywhere up the tree wins over a range in a
nearer package.json.

An entity whose release cannot be found or installed, or with no ``node``
or ``npm``, gets no default name, and the reason is returned for a warning.
"""

from importlib import resources
import json
import os
import re
import shutil
import subprocess
import tempfile

from .prisma_schema import _cache_directory, _directories, _json, _npm_view

_TIMEOUT = 300
_RELEASE = r"\d+\.\d+\.\d+[0-9A-Za-z.+-]*"
# pnpm and bun key entries typeorm@<release>; yarn writes the release on
# the line after the typeorm@<range> key.
_LOCKED = (
    re.compile(rf"(?:^|[\s\"'/])typeorm@({_RELEASE})(?=[\s\"'(:,])", re.M),
    re.compile(rf"^\"?typeorm@[^\n]*:\n\s+version:? \"?({_RELEASE})", re.M),
)


def _locked(directory):
    lock = _json(directory / "package-lock.json")
    if isinstance(lock, dict):
        entry = (lock.get("packages") or {}).get("node_modules/typeorm")
        if isinstance(entry, dict) and entry.get("version"):
            return entry["version"]
        entry = (lock.get("dependencies") or {}).get("typeorm")  # lockfile v1
        if isinstance(entry, dict) and entry.get("version"):
            return entry["version"]
    for name in ("pnpm-lock.yaml", "yarn.lock", "bun.lock"):
        try:
            text = (directory / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        releases = {m for pattern in _LOCKED for m in pattern.findall(text)}
        if len(releases) == 1:
            return releases.pop()
    return None


def project_release(root, file):
    """("release", version) or ("range", spec) of the typeorm the entity
    ``file`` (relative to ``root``) belongs to, or None."""
    for directory in _directories(root, file):
        installed = _json(directory / "node_modules" / "typeorm" / "package.json")
        if isinstance(installed, dict) and installed.get("version"):
            return "release", installed["version"]
        locked = _locked(directory)
        if locked:
            return "release", locked
    for directory in _directories(root, file):
        manifest = _json(directory / "package.json")
        if not isinstance(manifest, dict):
            continue
        for section in ("dependencies", "devDependencies", "peerDependencies"):
            declared = manifest.get(section)
            if isinstance(declared, dict) and isinstance(declared.get("typeorm"), str):
                return "range", declared["typeorm"]
    return None


def install(npm, release, environ):
    """The directory of typeorm ``release``, installed into the cache when
    missing."""
    if not re.fullmatch(r"[0-9A-Za-z.+-]+", release):
        raise RuntimeError(f"not a typeorm release: {release!r}")
    target = _cache_directory(environ) / "typeorm" / release
    package = target / "node_modules" / "typeorm"
    if (package / "package.json").is_file():
        return package
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = tempfile.mkdtemp(dir=target.parent, prefix=".install-")
    try:
        completed = subprocess.run(
            [
                npm,
                "install",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--no-save",
                "--omit=peer",
                "--omit=optional",
                "--prefix",
                scratch,
                f"typeorm@{release}",
            ],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            env=environ,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"npm install typeorm@{release} failed: "
                f"{completed.stderr.strip()[:300]}"
            )
        try:
            os.replace(scratch, target)
        except OSError:
            if not (package / "package.json").is_file():
                raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return package


def table_names(root, entities, environ=None):
    """(names, unread): ``names`` maps (file, class name) to TypeORM's
    default table name for ``entities`` [(file, class name)], and
    ``unread`` maps each file with no name to the reason."""
    environ = dict(os.environ if environ is None else environ)
    node, npm = (
        shutil.which("node", path=environ.get("PATH")),
        shutil.which("npm", path=environ.get("PATH")),
    )
    names, unread = {}, {}
    if node is None or npm is None:
        for file, _ in entities:
            unread[file] = "node and npm are required to run TypeORM's naming"
        return names, unread
    by_release, resolved = {}, {}
    for file, entity in entities:
        found = project_release(root, file)
        if found is None:
            unread[file] = (
                "no typeorm release: no typeorm in a package.json, lockfile or "
                "node_modules from this directory up"
            )
            continue
        try:
            if found not in resolved:
                kind, value = found
                if kind == "release":
                    resolved[found] = value
                else:
                    releases = _npm_view(
                        npm, f"typeorm@{value}", "version", environ=environ
                    )
                    resolved[found] = (
                        releases[-1] if isinstance(releases, list) else releases
                    )
        except (RuntimeError, ValueError, subprocess.SubprocessError) as error:
            resolved[found] = error
        release = resolved[found]
        if isinstance(release, Exception):
            unread[file] = f"typeorm {found[1]}: {release}"
            continue
        by_release.setdefault(release, []).append((file, entity))
    script = resources.files(__package__).joinpath("typeormnames/names.js")
    for release, group in sorted(by_release.items()):
        try:
            package = install(npm, release, environ)
            with resources.as_file(script) as path:
                completed = subprocess.run(
                    [node, str(path), str(package)],
                    input=json.dumps(sorted({entity for _, entity in group})),
                    capture_output=True,
                    text=True,
                    timeout=_TIMEOUT,
                )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"typeorm {release} naming failed: {completed.stderr.strip()[:300]}"
                )
            tables = json.loads(completed.stdout)
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as e:
            for file, _ in group:
                unread[file] = f"typeorm {release}: {e}"
            continue
        for file, entity in group:
            names[file, entity] = tables[entity]
    return names, unread
