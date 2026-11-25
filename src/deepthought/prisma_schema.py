"""Read Prisma schemas with Prisma's own schema engine.

Prisma publishes its schema parser and validator as
``@prisma/prisma-schema-wasm``, the engine that ``prisma generate`` and the
Prisma language server run. Each release of Prisma pins one build of it
(``@prisma/engines-version``), and the schema language changes between
releases (Prisma 7 rejects a datasource ``url`` that Prisma 6 requires), so
a schema is read with the build its own project's Prisma pins:

1. The Prisma version comes from the nearest directory, from the schema
   file up to the repository root, that has one: an installed
   ``node_modules/@prisma/engines-version`` (the build itself), an installed
   ``prisma`` or ``@prisma/client`` package, the build a lockfile pins
   (``package-lock.json``, ``pnpm-lock.yaml``, ``yarn.lock``, ``bun.lock``,
   read only for that one entry), or a ``prisma`` or
   ``@prisma/client`` entry in ``package.json`` dependencies or in
   ``deno.json`` imports (a range is resolved to its newest release with
   ``npm view``).
2. ``npm view @prisma/engines@<version>`` names the build, and
   ``npm install --ignore-scripts`` puts it in the cache directory
   (``$DEEPTHOUGHT_CACHE``, else ``$XDG_CACHE_HOME/deepthought``).
3. ``get_config`` finds the files that declare a datasource; each schema is
   such a file's directory with the ``.prisma`` files under it (Prisma's
   multi-file schema folder), or a lone file. ``get_dmmf`` validates each
   schema and returns its models with their database names (``@@map``).

A file without a Prisma version, a build npm cannot fetch, a missing
``node`` or ``npm``, or a schema the engine rejects is unread, with the
reason; nothing guesses its models. The DMMF has no source positions, so
the line of a model is where ``model <Name>`` is written in its schema.
"""

from dataclasses import dataclass, field
from importlib import resources
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile

from .data_contracts import PrismaModel

_PACKAGE = "@prisma/prisma-schema-wasm"
_CLIENTS = ("prisma", "@prisma/client")
_TIMEOUT = 300


@dataclass
class SchemaRead:
    models: list = field(default_factory=list)
    unread: dict = field(default_factory=dict)  # file -> reason
    engines: dict = field(default_factory=dict)  # schema file -> engines version


def _cache_directory(environ):
    configured = environ.get("DEEPTHOUGHT_CACHE")
    if configured:
        return Path(configured)
    base = environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "deepthought"


def _json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_LOCKED = re.compile(r"@prisma/engines-version[@/](?:npm:)?(\d[0-9A-Za-z.+-]*)")


def _locked_build(directory):
    """The engines build a lockfile in ``directory`` pins: npm's hoisted
    entry, or the one build a pnpm or yarn lockfile names (their entries
    are keyed ``@prisma/engines-version@<build>``)."""
    lock = _json(directory / "package-lock.json")
    if isinstance(lock, dict):
        entry = (lock.get("packages") or {}).get("node_modules/@prisma/engines-version")
        if isinstance(entry, dict) and entry.get("version"):
            return entry["version"]
    for name in ("pnpm-lock.yaml", "yarn.lock", "bun.lock"):
        try:
            text = (directory / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        builds = set(_LOCKED.findall(text))
        if len(builds) == 1:
            return builds.pop()
    return None


def _directories(root, file):
    root = Path(root)
    directory = root / PurePosixPath(file).parent
    while True:
        yield directory
        if directory == root or root not in directory.parents:
            return
        directory = directory.parent


def project_version(root, file):
    """("engines", build) or ("prisma", version or range) for the Prisma
    the schema ``file`` (relative to ``root``) belongs to, or None. An
    installed or locked version anywhere up the tree comes before a range
    declared in a nearer package.json."""
    for directory in _directories(root, file):
        modules = directory / "node_modules"
        engines = _json(modules / "@prisma" / "engines-version" / "package.json")
        if engines and engines.get("version"):
            return "engines", engines["version"]
        for name in _CLIENTS:
            installed = _json(modules / name / "package.json")
            if installed and installed.get("version"):
                return "prisma", installed["version"]
        locked = _locked_build(directory)
        if locked:
            return "engines", locked
    for directory in _directories(root, file):
        manifest = _json(directory / "package.json")
        if not isinstance(manifest, dict):
            continue
        for section in ("dependencies", "devDependencies"):
            declared = manifest.get(section)
            if not isinstance(declared, dict):
                continue
            for name in _CLIENTS:
                if isinstance(declared.get(name), str):
                    return "prisma", declared[name]
    for directory in _directories(root, file):
        # Deno maps the npm packages in deno.json: "npm:prisma@7.10.0".
        deno = _json(directory / "deno.json")
        imports = deno.get("imports") if isinstance(deno, dict) else None
        if not isinstance(imports, dict):
            continue
        for name in _CLIENTS:
            spec = imports.get(name)
            if isinstance(spec, str) and spec.startswith(f"npm:{name}@"):
                return "prisma", spec[len(f"npm:{name}@") :]
    return None


def _npm_view(npm, spec, *fields, environ):
    completed = subprocess.run(
        [npm, "view", spec, *fields, "--json"],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
        env=environ,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError(f"npm view {spec} failed: {completed.stderr.strip()[:300]}")
    return json.loads(completed.stdout)


def engines_build(npm, version, environ):
    """The prisma-schema-wasm build that Prisma ``version`` (a release or a
    range, resolved to its newest release) pins."""
    releases = _npm_view(npm, f"prisma@{version}", "version", environ=environ)
    release = releases[-1] if isinstance(releases, list) else releases
    # Prisma 5 and later list it in dependencies, Prisma 4 in devDependencies.
    for section in ("dependencies", "devDependencies"):
        try:
            listed = _npm_view(
                npm, f"@prisma/engines@{release}", section, environ=environ
            )
        except RuntimeError:
            continue
        if isinstance(listed, dict) and listed.get("@prisma/engines-version"):
            return listed["@prisma/engines-version"]
    raise RuntimeError(
        f"@prisma/engines@{release} names no engines-version (Prisma {release} "
        "does not read schemas with prisma-schema-wasm)"
    )


def install(npm, build, environ):
    """The directory of prisma-schema-wasm ``build``, installed into the
    cache when missing."""
    if not re.fullmatch(r"[0-9A-Za-z.+-]+", build):
        raise RuntimeError(f"not a prisma-schema-wasm version: {build!r}")
    target = _cache_directory(environ) / "prisma-schema-wasm" / build
    package = target / "node_modules" / "@prisma" / "prisma-schema-wasm"
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
                "--prefix",
                scratch,
                f"{_PACKAGE}@{build}",
            ],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            env=environ,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"npm install {_PACKAGE}@{build} failed: "
                f"{completed.stderr.strip()[:300]}"
            )
        # Rename into place, so a concurrent run never sees half an install.
        try:
            os.replace(scratch, target)
        except OSError:
            if not (package / "package.json").is_file():
                raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return package


def _engine(node, package, op, schemas):
    script = resources.files(__package__).joinpath("prismaschema/dmmf.js")
    with resources.as_file(script) as path:
        completed = subprocess.run(
            [node, str(path), str(package)],
            input=json.dumps({"op": op, "schemas": schemas}),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    if completed.returncode != 0:
        raise RuntimeError(
            f"prisma-schema-wasm failed: {completed.stderr.strip()[:300]}"
        )
    return json.loads(completed.stdout)


def _schemas(files, config):
    """Group schema files: each directory holding one datasource file takes
    the files under it; the rest are read on their own."""
    roots = {}
    for file in files:
        if config[file]:
            roots.setdefault(str(PurePosixPath(file).parent), []).append(file)
    groups, alone = {}, []
    for file in files:
        parents = [str(p) for p in PurePosixPath(file).parents]
        owner = next((p for p in parents if p in roots), None)
        if owner is None or len(roots[owner]) > 1:
            alone.append(file)
        else:
            groups.setdefault(owner, []).append(file)
    return sorted(groups.values()) + [[file] for file in sorted(alone)]


def _line(text, model):
    match = re.search(rf"(?m)^[ \t]*model[ \t]+{re.escape(model)}\b", text)
    return text.count("\n", 0, match.start()) + 1 if match else None


def read_schemas(root, texts, environ=None):
    """SchemaRead of the Prisma schema files ``texts`` ({relative path:
    text}) of the repository at ``root``."""
    environ = dict(os.environ if environ is None else environ)
    read = SchemaRead()
    node = shutil.which("node", path=environ.get("PATH"))
    npm = shutil.which("npm", path=environ.get("PATH"))
    if node is None or npm is None:
        missing = "node" if node is None else "npm"
        read.unread = {f: f"{missing} not found on PATH" for f in texts}
        return read
    by_build, builds = {}, {}
    for file in sorted(texts):
        version = project_version(root, file)
        if version is None:
            read.unread[file] = (
                "no Prisma version: no prisma or @prisma/client in a "
                "package.json or node_modules from this directory up"
            )
            continue
        kind, value = version
        try:
            if version not in builds:
                builds[version] = (
                    value if kind == "engines" else engines_build(npm, value, environ)
                )
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            builds[version] = error
        build = builds[version]
        if isinstance(build, Exception):
            read.unread[file] = f"Prisma {value}: {build}"
            continue
        by_build.setdefault(build, []).append(file)
    for build, files in sorted(by_build.items()):
        try:
            package = install(npm, build, environ)
            configs = _engine(node, package, "config", [[[f, texts[f]]] for f in files])
            config = {f: bool(c.get("datasources")) for f, c in zip(files, configs)}
            groups = _schemas(files, config)
            results = _engine(
                node, package, "dmmf", [[[f, texts[f]] for f in g] for g in groups]
            )
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
            for file in files:
                read.unread[file] = f"prisma-schema-wasm {build}: {error}"
            continue
        for group, result in zip(groups, results):
            if "error" in result:
                for file in group:
                    read.unread[file] = result["error"].strip()[:500]
                continue
            for file in group:
                read.engines[file] = build
            for model in result["models"]:
                located = next(
                    (
                        (file, line)
                        for file in group
                        if (line := _line(texts[file], model["name"]))
                    ),
                    (group[0], 1),
                )
                read.models.append(
                    PrismaModel(
                        located[0],
                        model["name"],
                        model["dbName"] or model["name"],
                        model["dbName"] is not None,
                        located[1],
                    )
                )
    read.models.sort(key=lambda m: (m.file, m.line, m.model))
    return read
