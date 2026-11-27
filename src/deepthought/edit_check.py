"""What a proposed edit breaks, from the language's own type checker.

A unified diff is applied to a scratch copy of the workspace and the
checker of each edited language runs on the workspace as it is and on the
copy: go/types over every package of each Go module and its tests
(``gocheck/main.go``; unlike ``go build`` or ``go vet``, a package with
errors does not stop its importers from being checked), ``tsc --noEmit``
of each TypeScript project, and ``pyright`` over every Python file of each
Python project (not only the files its configuration includes). A
diagnostic that only the edited copy has is a break. Each break is reported
at its line in the unedited source, with the declaration that holds it (or
``(top level of file)``), so it can be looked up in the graph by file and
line. Diagnostics on lines the edit itself wrote are listed separately: they
are mistakes in the edit, not code it breaks. Nothing of the workspace runs.

Which projects are checked follows from the edited files: the Go modules
that hold them, and the modules of the workspace that require one of those;
the nearest tsconfig.json of each edited file and the projects that
reference it; the nearest Python project (pyrightconfig.json, pyproject.toml,
setup.cfg or setup.py) of each edited file, else the workspace. A check that
cannot run is reported with its reason and the result is marked incomplete;
breaks in a language with no check are not known.

The checkers are the language's own tools. TypeScript is the release the
project installs or pins (installed into the cache when only pinned or
declared); ``DEEPTHOUGHT_TYPESCRIPT`` names a typescript package directory
to use instead. Pyright is ``DEEPTHOUGHT_PYRIGHT``, else ``pyright`` on
``PATH``, else the pinned release installed into the cache. Go is
``DEEPTHOUGHT_GO``, else ``go`` on ``PATH``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile

from .prisma_schema import _cache_directory, _directories, _json, _npm_view

PYRIGHT_RELEASE = "1.1.409"
TOP_LEVEL = "(top level of file)"
_TIMEOUT = 900
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_TS_DIAGNOSTIC = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+),(?P<column>\d+)\): error (?P<code>TS\d+): (?P<message>.*)$"
)
_SKIPPED = {".git", "node_modules"}
_WRAPPER = ".deepthought-check."
_GO_SUFFIXES = (".go",)
_TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_PY_SUFFIXES = (".py", ".pyi")
_PY_PROJECT = ("pyrightconfig.json", "pyproject.toml", "setup.cfg", "setup.py")
_RELEASE = re.compile(r"\d+\.\d+\.\d+[0-9A-Za-z.+-]*")


# -- the diff ------------------------------------------------------------------


@dataclass
class FileEdit:
    """One file of a unified diff. ``lines`` maps each line of the edited
    file inside a hunk to its unedited line, or to None for a line the edit
    wrote; ``hunks`` are (old start, old length, new start, new length)."""

    old_path: str | None
    new_path: str | None
    hunks: list = field(default_factory=list)
    lines: dict = field(default_factory=dict)
    # Unedited lines the edit removed or replaced, and the unedited lines
    # each insertion falls between (the line before, the line after).
    removed: set = field(default_factory=set)
    inserted: set = field(default_factory=set)
    # The text of each removed or replaced unedited line.
    removed_text: dict = field(default_factory=dict)

    def changed_lines(self):
        """Where the edit changes the unedited file, for a lookup of the
        declarations it changes: ``{"start", "end", "inside"}`` ranges of
        unedited lines; ``inside`` ranges are insertions, which change a
        declaration only when it holds both lines around them."""
        ranges, run = [], None
        for line in sorted(self.removed):
            if run and line == run[1] + 1:
                run[1] = line
            else:
                run = [line, line]
                ranges.append(run)
        found = [{"start": a, "end": b, "inside": False} for a, b in ranges]
        found += [
            {"start": before, "end": before + 1, "inside": True}
            for before in sorted(self.inserted)
            if before not in self.removed and before + 1 not in self.removed
        ]
        return found

    @property
    def path(self):
        return self.new_path or self.old_path

    def unedited_line(self, line):
        """(unedited line, False) of line ``line`` of the edited file, or
        (None, True) for a line the edit wrote."""
        if self.old_path is None:
            return None, True
        if line in self.lines:
            old = self.lines[line]
            return (None, True) if old is None else (old, False)
        shift = 0
        for old_start, old_length, new_start, new_length in self.hunks:
            if line < new_start:
                break
            shift = (old_start + old_length) - (new_start + new_length)
        return line + shift, False


def _path(header):
    path = header.split("\t")[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return str(PurePosixPath(path))


def parse_diff(text):
    """The FileEdits of a unified diff. Hunk lengths are counted from the
    hunk's lines, so a hand-written diff with wrong counts still reads."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("the edit must be a non-empty unified diff")
    edits, edit, hunk = [], None, None
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if (
            line.startswith("--- ")
            and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ ")
        ):
            edit = FileEdit(_path(line[4:]), _path(lines[index + 1][4:]))
            edits.append(edit)
            hunk = None
            index += 2
            continue
        match = _HUNK.match(line)
        if match and edit is not None:
            old = int(match[1])
            new = int(match[3])
            # A zero-length side names the line before the hunk.
            if match[2] == "0":
                old += 1
            if match[4] == "0":
                new += 1
            hunk = [old, 0, new, 0, old, new]
            edit.hunks.append(hunk)
        elif (
            hunk is not None
            and line[:1] in {" ", "-", "+", ""}
            and not line.startswith(("diff ", "index "))
        ):
            if line.startswith("-"):
                edit.removed.add(hunk[4])
                edit.removed_text[hunk[4]] = line[1:]
                hunk[4] += 1
                hunk[1] += 1
            elif line.startswith("+"):
                edit.inserted.add(hunk[4] - 1)
                edit.lines[hunk[5]] = None
                hunk[5] += 1
                hunk[3] += 1
            else:
                edit.lines[hunk[5]] = hunk[4]
                hunk[4] += 1
                hunk[5] += 1
                hunk[1] += 1
                hunk[3] += 1
        elif not line.startswith("\\"):
            hunk = None
        index += 1
    if not edits:
        raise ValueError("the diff has no file headers (--- a/path, +++ b/path)")
    for edit in edits:
        edit.hunks = [tuple(h[:4]) for h in edit.hunks]
        for path in (edit.old_path, edit.new_path):
            if path is not None and (
                path.startswith("/") or ".." in PurePosixPath(path).parts
            ):
                raise ValueError(f"the diff names a path outside the workspace: {path}")
    return edits


def _copy(root, target):
    def ignore(directory, names):
        return [name for name in names if name in _SKIPPED]

    shutil.copytree(root, target, symlinks=True, ignore=ignore)
    # Installed packages are inputs to the checkers, not edited: link them.
    for directory, names, _ in os.walk(root):
        names[:] = [n for n in names if n != ".git"]
        if "node_modules" in names:
            names.remove("node_modules")
            source = Path(directory) / "node_modules"
            destination = Path(target) / source.relative_to(root)
            if destination.parent.is_dir() and not destination.exists():
                destination.symlink_to(source.resolve(), target_is_directory=True)


def apply_edit(root, diff, target):
    """A copy of ``root`` at ``target`` with ``diff`` applied."""
    _copy(root, target)
    completed = subprocess.run(
        ["git", "apply", "--recount", "--whitespace=nowarn", "--unidiff-zero", "-"],
        input=diff.encode(),
        cwd=target,
        capture_output=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise ValueError(
            "the edit does not apply to the workspace: "
            + completed.stderr.decode(errors="replace").strip()[:500]
        )


# -- checkers ------------------------------------------------------------------


@dataclass
class Check:
    language: str
    checker: str
    project: str
    status: str = "checked"  # checked | failed | unavailable
    reason: str | None = None
    release: str | None = None
    before: list = field(default_factory=list)
    after: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    unchecked: list = field(default_factory=list)  # files no checker read
    # Files the checker read (None: every file of the project), and the
    # import aliases the project maps to its own files (tsconfig paths).
    files: set | None = None
    aliases: list = field(default_factory=list)


def _relative(path, root):
    return PurePosixPath(os.path.relpath(path, root)).as_posix()


def _nearest(root, file, names):
    for directory in _directories(root, file):
        for name in names:
            if (directory / name).exists():
                return directory
    return None


def _walk(root, name):
    for directory, names, files in os.walk(root):
        names[:] = sorted(n for n in names if n not in _SKIPPED and n != "vendor")
        if name in files:
            yield Path(directory) / name


def _go_module_path(go_mod):
    try:
        match = re.search(r"^module\s+(\S+)", go_mod.read_text(errors="replace"), re.M)
    except OSError:
        return None
    return match[1].strip('"') if match else None


def _go_projects(root, files):
    edited = {_nearest(root, f, ("go.mod",)) for f in files} - {None}
    paths = {_go_module_path(directory / "go.mod") for directory in edited} - {None}
    projects = set(edited)
    for go_mod in _walk(root, "go.mod"):
        try:
            text = go_mod.read_text(errors="replace")
        except OSError:
            continue
        if any(
            re.search(rf"^\s*(require\s+)?{re.escape(p)}\s", text, re.M) for p in paths
        ):
            projects.add(go_mod.parent)
    return sorted(projects)


def _go_run(go, root, module, timeout):
    """(diagnostics, unchecked packages) of one module, from gocheck: every
    package and its tests type-checked from source with go/types. A package
    with errors does not stop its importers from being checked (``go build``
    and ``go vet`` stop there and report nothing for them). The module's
    dependencies are downloaded into the module cache first, through a copy
    of its go.mod, so nothing of the workspace is written."""
    from .go_facts import (
        _environment,
        _info,
        build,
        download,
        module_files,
        module_toolchain,
    )

    with tempfile.TemporaryDirectory(prefix="deepthought-gocheck-") as scratch:
        modfile = module_files(module, scratch)
        download(go, module, modfile)
        command = module_toolchain(go, _info(go), (module / "go.mod").read_text())
        binary, goroot = build(command, program="gocheck")
        completed = subprocess.run(
            [str(binary), str(module)],
            cwd=module,
            env=_environment(command, goroot, modfile),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
    found, unchecked = [], []
    for line in completed.stdout.splitlines():
        item = json.loads(line)
        if item["kind"] == "error" and item.get("file"):
            path = os.path.normpath(os.path.join(module, item["file"]))
            found.append(
                (
                    _relative(path, root),
                    item["line"],
                    item["column"],
                    "go",
                    item["message"],
                )
            )
        elif item["kind"] == "unchecked":
            unchecked.append(
                f"{item['directory']} ({item['package']}): {item['message']}"
            )
    return found, unchecked


def _go_unbuilt(root, module):
    """Go files of the module in directories the go tool does not build
    (a leading ``_`` or ``.``, such as ``_examples``): no build checks them,
    but they may call what the edit changes. testdata and vendor hold
    fixtures and copies of dependencies, and nested modules are checked on
    their own."""
    found = []
    for directory, names, files in os.walk(module):
        here = Path(directory)
        names[:] = sorted(
            n
            for n in names
            if n not in {"testdata", "vendor", "node_modules", ".git"}
            and not (here / n / "go.mod").exists()
        )
        if any(part.startswith(("_", ".")) for part in here.relative_to(module).parts):
            found.extend(
                _relative(here / f, root) for f in sorted(files) if f.endswith(".go")
            )
    return found


def _check_go(root, edited_root, files, timeout):
    from .go_facts import toolchain

    go = toolchain()
    checks = []
    for module in _go_projects(root, files):
        check = Check("go", "go/types", _relative(module, root) or ".")
        if go is None:
            check.status, check.reason = (
                "unavailable",
                "go is required to check Go edits",
            )
        else:
            try:
                check.before, _ = _go_run(go, root, module, timeout)
                check.after, unchecked = _go_run(
                    go,
                    edited_root,
                    Path(edited_root) / module.relative_to(root),
                    timeout,
                )
                check.unchecked = _go_unbuilt(root, module)
                if unchecked:
                    check.status = "failed"
                    check.reason = (
                        "packages not type-checked: " + "; ".join(unchecked)[:300]
                    )
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                check.status, check.reason = "failed", str(error)[:300]
        checks.append(check)
    return checks


def _jsonc(text):
    text = re.sub(
        r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/',
        lambda m: m[1] or "",
        text,
        flags=re.S,
    )
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _tsconfig(path):
    try:
        data = json.loads(_jsonc(path.read_text(errors="replace")))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _references(config):
    found = []
    for reference in _tsconfig(config).get("references") or []:
        target = config.parent / (
            reference.get("path", "") if isinstance(reference, dict) else ""
        )
        target = target / "tsconfig.json" if target.is_dir() else target
        if target.is_file():
            found.append(target.resolve())
    return found


def _typescript_projects(root, files):
    root = Path(root).resolve()
    projects = set()
    for file in files:
        directory = _nearest(root, file, ("tsconfig.json",))
        if directory is None:
            continue
        config = (directory / "tsconfig.json").resolve()
        data = _tsconfig(config)
        if (
            data.get("references")
            and data.get("files") == []
            and not data.get("include")
        ):
            # A solution file checks nothing itself: check what it references.
            projects.update(_references(config))
        else:
            projects.add(config)
    # Projects that reference an edited project see its declarations.
    configs = [c.resolve() for c in _walk(root, "tsconfig.json")]
    changed = True
    while changed:
        changed = False
        for config in configs:
            if config not in projects and set(_references(config)) & projects:
                data = _tsconfig(config)
                if not (data.get("files") == [] and not data.get("include")):
                    projects.add(config)
                    changed = True
    return sorted(projects)


def _typescript_release(root, project):
    """(package directory or None, release or range or None)."""
    relative = _relative(project.parent, root)
    file = f"{relative}/tsconfig.json" if relative != "." else "tsconfig.json"
    for directory in _directories(root, file):
        installed = directory / "node_modules" / "typescript"
        manifest = _json(installed / "package.json")
        if isinstance(manifest, dict) and manifest.get("version"):
            return installed, manifest["version"]
    for directory in _directories(root, file):
        lock = _json(directory / "package-lock.json")
        if isinstance(lock, dict):
            entry = (lock.get("packages") or {}).get("node_modules/typescript")
            if isinstance(entry, dict) and entry.get("version"):
                return None, entry["version"]
    for directory in _directories(root, file):
        manifest = _json(directory / "package.json")
        if not isinstance(manifest, dict):
            continue
        for section in ("devDependencies", "dependencies", "peerDependencies"):
            declared = manifest.get(section)
            if isinstance(declared, dict) and isinstance(
                declared.get("typescript"), str
            ):
                return None, declared["typescript"]
    return None, None


def _npm_install(npm, name, release, environ):
    """The directory of npm package ``name`` at ``release``, installed into
    the cache when missing (no package scripts run)."""
    if not re.fullmatch(r"[0-9A-Za-z.+-]+", release):
        raise RuntimeError(f"not a {name} release: {release!r}")
    target = _cache_directory(environ) / name / release
    package = target / "node_modules" / name
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
                f"{name}@{release}",
            ],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            env=environ,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"npm install {name}@{release} failed: {completed.stderr.strip()[:300]}"
            )
        try:
            os.replace(scratch, target)
        except OSError:
            if not (package / "package.json").is_file():
                raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return package


def _typescript_package(root, project, environ):
    configured = environ.get("DEEPTHOUGHT_TYPESCRIPT")
    if configured:
        manifest = _json(Path(configured) / "package.json") or {}
        return Path(configured), manifest.get("version")
    installed, release = _typescript_release(root, project)
    if installed is not None:
        return installed, release
    if release is None:
        raise RuntimeError(
            "no typescript release: none installed, locked or declared from the project up"
        )
    npm = shutil.which("npm", path=environ.get("PATH"))
    if npm is None:
        raise RuntimeError("npm is required to install the project's typescript")
    if not _RELEASE.fullmatch(release):
        listed = _npm_view(npm, f"typescript@{release}", "version", environ=environ)
        release = listed[-1] if isinstance(listed, list) else listed
    return _npm_install(npm, "typescript", release, environ), release


def _type_package_installed(directory, entry):
    parts = entry.split("/")
    name = "/".join(parts[:2]) if entry.startswith("@") else parts[0]
    types = "@types/" + name.lstrip("@").replace("/", "__")
    for folder in (directory, *directory.parents):
        modules = folder / "node_modules"
        if (modules / name).exists() or (modules / types).exists():
            return True
    return False


def _project_config(node, package, project, timeout):
    """(missing, types, aliases) of a project as tsc resolves it: the
    entries of compilerOptions.types that are not installed (tsc stops
    before checking any file when one is missing), the installed ones, and
    the import patterns compilerOptions.paths maps to the project's own
    files."""
    completed = subprocess.run(
        [node, str(package / "lib" / "tsc.js"), "-p", str(project), "--showConfig"],
        cwd=project.parent,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    try:
        config = json.loads(completed.stdout or b"{}")
    except ValueError:
        return [], None, []
    options = config.get("compilerOptions") or {}
    aliases = sorted(options.get("paths") or {}) if isinstance(options, dict) else []
    types = options.get("types") if isinstance(options, dict) else None
    if not isinstance(types, list):
        return [], None, aliases
    missing = [t for t in types if not _type_package_installed(project.parent, str(t))]
    return missing, [t for t in types if t not in missing], aliases


def _package_name(specifier):
    parts = specifier.split("/")
    return "/".join(parts[:2]) if specifier.startswith("@") else parts[0]


def _installed(directory, name):
    return any(
        (folder / "node_modules" / name / "package.json").is_file()
        for folder in (directory, *directory.parents)
    )


def _missing_extends(config, root, seen=None):
    """{package: config directory} of the packages the ``extends`` chain of
    ``config`` names that are not installed where tsc looks for them."""
    seen = seen if seen is not None else set()
    if config in seen or not config.is_file():
        return {}
    seen.add(config)
    extends = _tsconfig(config).get("extends")
    found = {}
    for specifier in extends if isinstance(extends, list) else [extends]:
        if not isinstance(specifier, str) or not specifier:
            continue
        if specifier.startswith((".", "/")):
            target = (config.parent / specifier).resolve()
            if target.is_dir():
                target = target / "tsconfig.json"
            elif not target.suffix:
                target = target.with_suffix(".json")
            found.update(_missing_extends(target, root, seen))
        else:
            name = _package_name(specifier)
            if not _installed(config.parent, name):
                found[name] = config.parent
    return found


def _declared(root, directory, name):
    """The release or range of npm package ``name`` that the nearest
    package.json from ``directory`` up declares, or None."""
    for folder in (directory, *directory.parents):
        manifest = _json(folder / "package.json")
        if isinstance(manifest, dict):
            for section in ("devDependencies", "dependencies", "peerDependencies"):
                declared = manifest.get(section)
                if isinstance(declared, dict) and isinstance(declared.get(name), str):
                    return declared[name]
        if folder == root:
            break
    return None


def _link_package(directory, name, package):
    """Link ``package`` as ``directory``/node_modules/``name`` in a scratch
    copy. A node_modules (or scope directory) that links to the workspace's
    own is replaced by a directory linking each of its entries, so nothing
    is written into the workspace."""
    current = directory / "node_modules"
    for part in ["", *name.split("/")[:-1]]:
        current = current / part if part else current
        if current.is_symlink():
            original = current.resolve()
            current.unlink()
            current.mkdir()
            for entry in original.iterdir():
                (current / entry.name).symlink_to(entry, entry.is_dir())
        current.mkdir(parents=True, exist_ok=True)
    target = directory / "node_modules" / name
    if not target.exists():
        target.symlink_to(package, target_is_directory=True)


def _install_extends(config, root, environ):
    """Install into the cache, and link into the scratch copy, each package
    the project's ``extends`` chain names that is not installed (tsc stops
    before checking any file when an extended config is missing). The
    package.json release or range is used, else the newest release. The
    names installed."""
    missing = _missing_extends(config, root)
    if not missing:
        return []
    npm = shutil.which("npm", path=environ.get("PATH"))
    if npm is None:
        raise RuntimeError(
            "npm is required to install extended tsconfig packages: "
            + ", ".join(missing)
        )
    for name, directory in sorted(missing.items()):
        spec = _declared(root, directory, name) or "latest"
        if not _RELEASE.fullmatch(spec):
            listed = _npm_view(npm, f"{name}@{spec}", "version", environ=environ)
            spec = listed[-1] if isinstance(listed, list) else listed
        _link_package(directory, name, _npm_install(npm, name, spec, environ))
    return sorted(missing)


_MANIFESTS = (
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "yarn.lock",
    ".npmrc",
    ".yarnrc.yml",
)


def _manifests(top):
    """The package manifests and lockfiles under ``top`` (outside installed
    packages), relative to it: what an install of its dependencies reads."""
    found = []
    for directory, names, files in os.walk(top):
        names[:] = sorted(n for n in names if n not in _SKIPPED)
        here = Path(directory)
        found.extend(
            _relative(here / name, top) for name in sorted(files) if name in _MANIFESTS
        )
        if len(found) > 5000:
            break
    return found


def _workspace_packages(top):
    """{package name: directory relative to ``top``} of the packages the
    workspace holds itself (a monorepo's own packages)."""
    found = {}
    for manifest in _manifests(top):
        if PurePosixPath(manifest).name != "package.json":
            continue
        data = _json(Path(top) / manifest)
        name = data.get("name") if isinstance(data, dict) else None
        directory = str(PurePosixPath(manifest).parent)
        if isinstance(name, str) and name and name not in found and directory != ".":
            found[name] = directory
    return found


def _package_top(root, project):
    """The outermost directory from ``project``'s up to ``root`` with a
    package.json: where the workspace's dependencies are installed."""
    relative = _relative(project.parent, root)
    file = f"{relative}/tsconfig.json" if relative != "." else "tsconfig.json"
    top = None
    for directory in _directories(root, file):
        if (directory / "package.json").is_file():
            top = directory
    return top


def _has_node_modules(root, project):
    relative = _relative(project.parent, root)
    file = f"{relative}/tsconfig.json" if relative != "." else "tsconfig.json"
    return any((d / "node_modules").exists() for d in _directories(root, file))


def _install_command(top, environ):
    """The package manager command that installs the dependencies under
    ``top`` as its lockfile pins them (no package scripts run), or None."""
    path = environ.get("PATH")
    npm = shutil.which("npm", path=path)
    quiet = ["--ignore-scripts"]
    if (top / "pnpm-lock.yaml").is_file() and shutil.which("pnpm", path=path):
        return [shutil.which("pnpm", path=path), "install", "--frozen-lockfile", *quiet]
    if (top / "yarn.lock").is_file() and shutil.which("yarn", path=path):
        return [shutil.which("yarn", path=path), "install", *quiet]
    if npm is None:
        return None
    flags = [*quiet, "--no-audit", "--no-fund", "--prefer-offline"]
    if (top / "package-lock.json").is_file() or (top / "npm-shrinkwrap.json").is_file():
        return [npm, "ci", *flags]
    return [npm, "install", *flags, "--no-package-lock", "--legacy-peer-deps"]


def _install_dependencies(top, environ, timeout):
    """A cache directory holding the workspace's manifests with their
    dependencies installed, keyed by the manifests' content so a later
    check reuses it. Raises RuntimeError when the install fails (the
    failure is kept, so the same manifests are not retried every check)."""
    import hashlib

    manifests = _manifests(top)
    digest = hashlib.sha256()
    for manifest in manifests:
        digest.update(manifest.encode() + b"\0")
        try:
            digest.update((Path(top) / manifest).read_bytes())
        except OSError:
            pass
    key = digest.hexdigest()[:20]
    base = _cache_directory(environ) / "node-dependencies"
    target = base / key
    failed = base / f"{key}.failed"
    if (target / ".installed").is_file():
        return target
    if failed.is_file():
        raise RuntimeError(failed.read_text(errors="replace"))
    command = _install_command(Path(top), environ)
    if command is None:
        raise RuntimeError("npm is required to install the project's packages")
    base.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(dir=base, prefix=".install-"))
    try:
        for manifest in manifests:
            destination = scratch / manifest
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(top) / manifest, destination)
        try:
            completed = subprocess.run(
                command,
                cwd=scratch,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**environ, "CI": "1"},
            )
        except subprocess.TimeoutExpired:
            completed = None
        if completed is None or completed.returncode != 0:
            detail = (
                f"timed out after {timeout}s"
                if completed is None
                else (completed.stderr or completed.stdout).strip()[-300:]
            )
            message = f"{PurePosixPath(command[0]).name} {command[1]} failed: {detail}"
            failed.write_text(message)
            raise RuntimeError(message)
        (scratch / ".installed").write_text(" ".join(command[1:]))
        try:
            os.replace(scratch, target)
        except OSError:
            if not (target / ".installed").is_file():
                raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return target


def _link_installed(installed, copy_top):
    """Link each node_modules of the cache directory ``installed`` into the
    scratch copy at the same place."""
    linked = []
    for directory, names, _ in os.walk(installed):
        names[:] = sorted(n for n in names if n not in {".git"})
        if "node_modules" in names:
            names.remove("node_modules")
            source = Path(directory) / "node_modules"
            destination = Path(copy_top) / source.relative_to(installed)
            if destination.parent.is_dir() and not destination.exists():
                destination.symlink_to(source, target_is_directory=True)
                linked.append(destination)
    return linked


def _link_workspace_packages(copy_top, packages):
    """Make each of the workspace's own packages resolvable by name from
    the scratch copy, pointing at the copy's own directory (so the edited
    copy sees edits to a sibling package). A name an install linked to a
    manifest-only directory of the cache is relinked."""
    copy_top = Path(copy_top)
    linked = []
    for name, directory in sorted(packages.items()):
        package = (copy_top / directory).resolve()
        target = copy_top / "node_modules" / name
        if target.is_symlink():
            real = target.resolve()
            if real == package or "node_modules" in real.parts:
                continue
            _expand(copy_top / "node_modules", name)
            target.unlink()
        elif target.exists():
            continue
        _link_package(copy_top, name, package)
        linked.append(name)
    return linked


def _expand(node_modules, name):
    """Replace links to directories outside the copy on the way to
    ``node_modules``/``name`` by directories of links, so its entry can be
    replaced without writing outside the copy."""
    current = node_modules
    for part in ["", *name.split("/")[:-1]]:
        current = current / part if part else current
        if current.is_symlink():
            original = current.resolve()
            current.unlink()
            current.mkdir()
            for entry in original.iterdir():
                (current / entry.name).symlink_to(entry, entry.is_dir())


def _provision_typescript(root, edited_root, project, environ, timeout):
    """Set up what tsc needs to read the project in both scratch copies:
    with no node_modules from the project up, the workspace's dependencies
    are installed into the cache (as its lockfile pins them) and linked in;
    the workspace's own packages are linked by name. Notes of what was done
    and what could not be, so a check with packages missing says so."""
    root, edited_root = Path(root), Path(edited_root)
    top = _package_top(root, project)
    if top is None:
        return []
    notes = []
    relative = top.relative_to(root)
    if not _has_node_modules(root, project):
        try:
            installed = _install_dependencies(top, environ, timeout)
        except (RuntimeError, OSError) as error:
            notes.append(
                "packages not installed (no node_modules, and installing them"
                f" failed: {str(error)[:200]}); imports of packages are unresolved"
            )
        else:
            for copy in (root, edited_root):
                _link_installed(installed, copy / relative)
            notes.append(
                "node_modules missing: the project's packages were installed"
                " into the cache and linked in"
            )
    packages = _workspace_packages(top)
    linked = []
    for copy in (root, edited_root):
        linked = _link_workspace_packages(copy / relative, packages) or linked
    if linked:
        notes.append("workspace packages linked by name: " + ", ".join(linked[:10]))
    return notes


def _tsc_run(node, package, root, project, timeout):
    """(diagnostics, errors with no file, missing type packages, path
    aliases). An error
    with no file (a bad option) stops tsc before it checks any file, so a
    type package that is not installed is left out of compilerOptions.types
    and reported. The check runs from a wrapper config beside the project
    (in a scratch copy), which extends it, so every path the project leaves
    to its config's directory (include, rootDir, typeRoots) stays the same.
    """
    missing, types, aliases = _project_config(node, package, project, timeout)
    options = {"noEmit": True, "incremental": False, "composite": False}
    if missing:
        options["types"] = types
    wrapper = project.parent / f"{_WRAPPER}{project.name}"
    wrapper.write_text(
        json.dumps({"extends": f"./{project.name}", "compilerOptions": options})
    )
    try:
        completed = subprocess.run(
            [
                node,
                str(package / "lib" / "tsc.js"),
                "-p",
                str(wrapper),
                "--pretty",
                "false",
            ],
            cwd=project.parent,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    finally:
        wrapper.unlink(missing_ok=True)
    found, general = [], []
    for line in completed.stdout.decode(errors="replace").splitlines():
        match = _TS_DIAGNOSTIC.match(line.strip())
        if match and not match["file"].endswith(_TS_SUFFIXES):
            # An error in a config file (an extends that does not resolve, a
            # bad option) stops tsc before it checks any source file.
            general.append(line.strip())
        elif match:
            path = os.path.normpath(os.path.join(project.parent, match["file"]))
            found.append(
                (
                    _relative(path, root),
                    int(match["line"]),
                    int(match["column"]),
                    match["code"],
                    match["message"],
                )
            )
        elif line.startswith("error TS"):
            general.append(line.strip())
    return found, general, missing, aliases


def _tsc_files(node, package, root, project, timeout):
    """The files of the workspace that tsc reads for ``project``, relative
    to ``root``: an edited file the project does not include is not
    checked, though its tsconfig.json is the nearest one. None when tsc
    cannot list them."""
    completed = subprocess.run(
        [node, str(package / "lib" / "tsc.js"), "-p", str(project), "--listFilesOnly"],
        cwd=project.parent,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    listed = set()
    real_root = Path(root).resolve()
    for line in completed.stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        path = Path(os.path.normpath(os.path.join(project.parent, line)))
        for base in (Path(root), real_root):
            if path.is_relative_to(base):
                listed.add(_relative(path, base))
                break
    return listed or None


def _check_typescript(root, edited_root, files, timeout):
    environ = dict(os.environ)
    node = shutil.which("node", path=environ.get("PATH"))
    root = Path(root).resolve()
    checks = []
    for project in _typescript_projects(root, files):
        check = Check("typescript", "tsc --noEmit", _relative(project, root))
        try:
            if node is None:
                raise RuntimeError("node is required to check TypeScript edits")
            package, check.release = _typescript_package(root, project, environ)
            edited_project = Path(edited_root) / project.relative_to(root)
            check.notes.extend(
                _provision_typescript(root, edited_root, project, environ, timeout)
            )
            installed = _install_extends(project, root, environ)
            _install_extends(edited_project, Path(edited_root), environ)
            if installed:
                check.notes.append(
                    "extended configs installed into the cache: " + ", ".join(installed)
                )
            check.before, general, missing, check.aliases = _tsc_run(
                node, package, root, project, timeout
            )
            check.after, general_after, _, _ = _tsc_run(
                node, package, edited_root, edited_project, timeout
            )
            check.files = _tsc_files(
                node, package, edited_root, edited_project, timeout
            )
            if missing:
                check.notes.append(
                    "type packages not installed, left out of the check: "
                    + ", ".join(missing)
                )
            library = [d for d in check.before + check.after if d[3] in _TS_LIBRARY]
            if general or general_after:
                check.status = "failed"
                check.reason = (general or general_after)[0][:300]
            elif library:
                # Without its standard library tsc types nothing: every
                # break would be missed, so the check did not happen.
                check.status = "failed"
                check.reason = (
                    "TypeScript could not load its standard library"
                    f" ({library[0][3]} {library[0][4][:120]}), so the types it"
                    " reports cannot be trusted"
                )
        except RuntimeError as error:
            check.status, check.reason = "unavailable", str(error)[:300]
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            check.status, check.reason = "failed", str(error)[:300]
        checks.append(check)
    if not checks and files:
        checks.append(
            Check(
                "typescript",
                "tsc --noEmit",
                ".",
                "unavailable",
                "no tsconfig.json above the edited files",
            )
        )
    return checks


def _pyright(environ):
    configured = environ.get("DEEPTHOUGHT_PYRIGHT") or shutil.which(
        "pyright", path=environ.get("PATH")
    )
    if configured:
        return [configured], None
    node = shutil.which("node", path=environ.get("PATH"))
    npm = shutil.which("npm", path=environ.get("PATH"))
    if node is None or npm is None:
        raise RuntimeError(
            "pyright, or node and npm to install it, is required to check Python edits"
        )
    package = _npm_install(npm, "pyright", PYRIGHT_RELEASE, environ)
    return [node, str(package / "index.js")], PYRIGHT_RELEASE


def _python_files(project):
    """Every Python file under ``project``, outside virtual environments,
    installed packages and hidden or cache directories. A project's pyright
    ``include`` often leaves out tests and examples; they call the code an
    edit changes too, so they are checked."""
    found = []
    for directory, names, files in os.walk(project):
        names[:] = sorted(
            n
            for n in names
            if not n.startswith(".")
            and n not in {"node_modules", "__pycache__", "site-packages"}
            and not (Path(directory) / n / "pyvenv.cfg").exists()
        )
        found.extend(
            str(Path(directory) / f) for f in sorted(files) if f.endswith(_PY_SUFFIXES)
        )
    return found


def _pyright_run(command, root, project, timeout):
    """pyright's errors in every Python file of the project (see
    _python_files), with the project's own configuration for everything but
    which files are checked."""
    found = []
    files = _python_files(project)
    for start in range(0, len(files), 500):
        found.extend(
            _pyright_files(command, root, project, files[start : start + 500], timeout)
        )
    return found


def _pyright_files(command, root, project, files, timeout):
    completed = subprocess.run(
        [*command, "--outputjson", *files],
        cwd=project,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    try:
        report = json.loads(completed.stdout or b"{}")
    except ValueError as error:
        raise RuntimeError(
            "pyright gave no report: "
            + completed.stderr.decode(errors="replace").strip()[:300]
        ) from error
    found = []
    for item in report.get("generalDiagnostics", []):
        if item.get("severity") != "error":
            continue
        start = item["range"]["start"]
        found.append(
            (
                _relative(item["file"], root),
                start["line"] + 1,
                start["character"] + 1,
                item.get("rule") or "pyright",
                item.get("message", "").splitlines()[0],
            )
        )
    return found


def _python_environment(root, project, environ):
    """The Python interpreter whose installed packages the project's imports
    resolve to: ``DEEPTHOUGHT_PYTHON``, else a virtual environment in the
    project or the workspace (a directory holding pyvenv.cfg), else None
    (pyright then uses the ``python`` on PATH). A project whose pyright
    configuration names its own environment keeps it."""
    configured = environ.get("DEEPTHOUGHT_PYTHON")
    if configured:
        return Path(configured)
    if (project / "pyrightconfig.json").is_file():
        return None
    for base in dict.fromkeys((project, Path(root))):
        for name in (".venv", "venv", "env", ".env"):
            venv = base / name
            if (venv / "pyvenv.cfg").is_file():
                for python in ("bin/python", "Scripts/python.exe"):
                    if (venv / python).exists():
                        return venv / python
    return None


def _check_python(root, edited_root, files, timeout):
    environ = dict(os.environ)
    root = Path(root).resolve()
    projects = sorted({_nearest(root, f, _PY_PROJECT) or root for f in files})
    checks = []
    for project in projects:
        check = Check("python", "pyright", _relative(project, root))
        try:
            command, check.release = _pyright(environ)
            interpreter = _python_environment(root, project, environ)
            if interpreter is not None:
                command = [*command, "--pythonpath", str(interpreter)]
                check.notes.append(
                    "imports resolved through "
                    + (
                        _relative(interpreter, root)
                        if Path(interpreter).is_relative_to(root)
                        else str(interpreter)
                    )
                )
            check.before = _pyright_run(command, root, project, timeout)
            check.after = _pyright_run(
                command,
                Path(edited_root).resolve(),
                Path(edited_root) / project.relative_to(root),
                timeout,
            )
        except RuntimeError as error:
            check.status, check.reason = "unavailable", str(error)[:300]
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            check.status, check.reason = "failed", str(error)[:300]
        checks.append(check)
    return checks


_CHECKERS = (
    ("go", _GO_SUFFIXES, _check_go),
    ("typescript", _TS_SUFFIXES, _check_typescript),
    ("python", _PY_SUFFIXES, _check_python),
)


# -- environment diagnostics ---------------------------------------------------

# tsc codes that mean its standard library did not load (a missing or wrong
# lib or target, often from a tsconfig it extends that is not installed).
_TS_LIBRARY = {"TS2318", "TS2468", "TS2583", "TS2584", "TS2585", "TS2697", "TS2705"}
# Type definitions that are not installed (test runners, node, a package
# installed without types).
_TS_MISSING_TYPES = {
    "TS2580",
    "TS2581",
    "TS2582",
    "TS2591",
    "TS2592",
    "TS2593",
    "TS2688",
    "TS7016",
}
_TS_MISSING_MODULE = {"TS2307", "TS2792"}
# tsc says a test runner's types are missing only for the names it knows
# (test, describe, it); the runner's other globals then read as unknown
# names (TS2304) in the same file.
_TS_RUNNER_TYPES = {"TS2582", "TS2593"}
_TS_RUNNER_GLOBALS = re.compile(
    r"^Cannot find name '(expect|jest|vi|beforeEach|afterEach|beforeAll|afterAll"
    r"|describe|it|test|fit|xit|xdescribe|fdescribe|suite|context|assert)'\.$"
)
_MODULE = re.compile(r"""module ['"]([^'"]+)['"]""")
_PY_IMPORT = re.compile(r'Import "([^"]+)"')


def _aliased(specifier, aliases):
    for pattern in aliases:
        if "*" in pattern:
            if specifier.startswith(pattern.split("*")[0]):
                return True
        elif specifier == pattern:
            return True
    return False


def _local_module(root, dotted):
    """Whether the top package of a Python import is the workspace's own."""
    top = dotted.split(".")[0]
    root = Path(root)
    bases = [root, root / "src"]
    try:
        for child in sorted(root.iterdir()):
            if child.is_dir() and child.name not in _SKIPPED:
                bases.extend((child, child / "src"))
    except OSError:
        pass
    return any((b / top).is_dir() or (b / f"{top}.py").is_file() for b in bases)


def environment_reason(
    language, code, message, *, root=".", aliases=(), runner_missing=False
):
    """Why a diagnostic comes from what the checker could not find (a
    package or type definitions that are not installed, a standard library
    that did not load) rather than from the code, or None.
    ``runner_missing``: tsc said the file's test runner types are missing."""
    if language == "typescript":
        if runner_missing and code == "TS2304" and _TS_RUNNER_GLOBALS.match(message):
            return "test runner type definitions not installed"
        if code in _TS_LIBRARY:
            return "TypeScript's standard library did not load"
        if code in _TS_MISSING_TYPES:
            return "type definitions not installed"
        if code in _TS_MISSING_MODULE:
            match = _MODULE.search(message)
            if (
                match
                and not match[1].startswith((".", "/"))
                and not _aliased(match[1], aliases)
            ):
                return f"package {_package_name(match[1])} not installed"
    elif language == "python":
        if code in ("reportMissingImports", "reportMissingModuleSource"):
            match = _PY_IMPORT.search(message)
            if (
                match
                and not match[1].startswith(".")
                and not _local_module(root, match[1])
            ):
                return f"package {match[1].split('.')[0]} not installed"
    elif language == "go" and "could not import" in message:
        return "package not available to the checker"
    return None


_LANGUAGE_NAMES = {"go": "Go", "typescript": "TypeScript", "python": "Python"}


def _covers(check, file):
    """Whether ``check``'s project holds ``file`` and its checker read it."""
    if check.files is not None:
        return file in check.files
    project = check.project
    if check.language == "typescript":
        project = str(PurePosixPath(project).parent)
    return project in (".", "") or file.startswith(project.rstrip("/") + "/")


def _file_coverage(root, edits, checks):
    """For each edited file that still exists: whether a checker that ran
    read it, else why not; and the packages its imports need that were
    not found, so what flows through them went unchecked."""
    languages = {lang: suffixes for lang, suffixes, _ in _CHECKERS}
    found = {}
    runner_missing = {d[0] for c in checks for d in c.after if d[3] in _TS_RUNNER_TYPES}
    for edit in edits:
        file = edit.new_path
        if file is None:
            continue
        language = next(
            (lang for lang, suffixes in languages.items() if file.endswith(suffixes)),
            None,
        )
        if language is None:
            found[file] = {
                "checked": False,
                "reason": "no type checker for this kind of file",
            }
            continue
        mine = [c for c in checks if c.language == language]
        ran = [c for c in mine if c.status == "checked" and _covers(c, file)]
        if ran:
            missing = sorted(
                {
                    reason
                    for c in ran
                    for f, _, _, code, message in c.after
                    if f == file
                    and (
                        reason := environment_reason(
                            c.language,
                            code,
                            message,
                            root=root,
                            aliases=c.aliases,
                            runner_missing=file in runner_missing,
                        )
                    )
                }
            )
            entry = {"checked": True}
            if missing:
                entry["unresolved"] = missing
            found[file] = entry
            continue
        failed = [c for c in mine if c.status != "checked"]
        if failed:
            reason = f"{_LANGUAGE_NAMES[language]} check {failed[0].status}: " + (
                failed[0].reason or "no reason given"
            )
        elif mine:
            reason = (
                f"no {_LANGUAGE_NAMES[language]} project includes it"
                f" (checked: {', '.join(c.project for c in mine)})"
            )
        else:
            reason = f"no {_LANGUAGE_NAMES[language]} project found for it"
        found[file] = {"checked": False, "reason": reason[:300]}
    return found


# -- declarations --------------------------------------------------------------


def _declarations(root, file, cache):
    """[(start line, end line, name)] of the declarations in ``file``."""
    if file in cache:
        return cache[file]
    from .engine import _source_language
    from .syntax import LANGUAGES, extract, make_parser, parse_source, script_source

    found = []
    language = _source_language(file, tuple(LANGUAGES))
    if language is not None:
        try:
            # A Vue component is read as its script blocks.
            suffix, content = script_source(file, (Path(root) / file).read_bytes())
            tree = parse_source(
                make_parser(language, suffix), language, suffix, content
            )
            found = [
                (d.line, d.end_line, d.symbol)
                for d in extract(tree, content, "edit", file)
            ]
        except (OSError, ImportError, ValueError):
            found = []
    cache[file] = found
    return found


def _enclosing(root, file, line, cache):
    best = None
    for start, end, name in _declarations(root, file, cache):
        if start <= line <= end and (best is None or end - start < best[1] - best[0]):
            best = (start, end, name)
    return (best[2], best[0]) if best else (TOP_LEVEL, None)


# -- the check -----------------------------------------------------------------


def check_edit(workspace, diff, *, timeout=_TIMEOUT):
    """What ``diff`` breaks in ``workspace``: a JSON-ready dict with each
    check that ran, the breaks at their unedited lines with the declaration
    that holds them, and the diagnostics on lines the edit wrote."""
    root = Path(workspace).resolve()
    edits = parse_diff(diff)
    by_path = {edit.new_path: edit for edit in edits if edit.new_path}
    edited = sorted({edit.path for edit in edits})
    checks = []
    with tempfile.TemporaryDirectory(prefix="deepthought-edit-") as scratch:
        # Both sides run on copies: the checkers write wrapper configs and
        # caches, and nothing is written into the workspace.
        unedited_root = Path(scratch) / "unedited"
        edited_root = Path(scratch) / "edited"
        _copy(root, unedited_root)
        apply_edit(root, diff, edited_root)
        for _, suffixes, checker in _CHECKERS:
            files = [f for f in edited if f.endswith(suffixes)]
            if files:
                checks.extend(checker(unedited_root, edited_root, files, timeout))
        # Messages that name a file name it in its copy: name it relative.
        for check in checks:
            check.before = _relative_messages(check.before, unedited_root)
            check.after = _relative_messages(check.after, edited_root)
    breaks, in_edit, environment, declarations = [], [], [], {}
    for check in checks:
        if check.status != "checked":
            continue
        unedited = Counter(
            (f, line, code, message) for f, line, _, code, message in check.before
        )
        runner_missing = {d[0] for d in check.after if d[3] in _TS_RUNNER_TYPES}
        for file, line, column, code, message in sorted(set(check.after)):
            edit = by_path.get(file)
            old_line, written = edit.unedited_line(line) if edit else (line, False)
            key = (
                edit.old_path if edit and edit.old_path else file,
                old_line,
                code,
                message,
            )
            if not written and unedited[key] > 0:
                unedited[key] -= 1
                continue
            entry = {"language": check.language, "code": code, "message": message}
            reason = environment_reason(
                check.language,
                code,
                message,
                root=root,
                aliases=check.aliases,
                runner_missing=file in runner_missing,
            )
            if reason:
                # Not the code's fault: what the checker could not find.
                environment.append(
                    {"file": file, "line": line, **entry, "reason": reason}
                )
                continue
            if written:
                in_edit.append({"file": file, "line": line, **entry})
                continue
            unedited_file = key[0]
            declaration, declaration_line = _enclosing(
                root, unedited_file, old_line, declarations
            )
            breaks.append(
                {
                    "file": unedited_file,
                    "line": old_line,
                    "column": column,
                    "declaration": declaration,
                    "declaration_line": declaration_line,
                    **entry,
                }
            )
    coverage = _file_coverage(root, edits, checks)
    complete = bool(checks) and all(check.status == "checked" for check in checks)
    unchecked = sorted(
        {f for f in edited if not any(f.endswith(s) for _, s, _ in _CHECKERS)}
        | {f for check in checks for f in check.unchecked}
        | {f for f, c in coverage.items() if not c["checked"]}
    )
    breaks, in_edit = _unique(breaks), _unique(in_edit)
    complete = complete and not unchecked
    report = {
        "verdict": _verdict(breaks, in_edit, complete),
        "edited_files": edited,
        "changed_lines": {
            edit.old_path: edit.changed_lines()
            for edit in edits
            if edit.old_path and edit.changed_lines()
        },
        "checks": [
            {
                "language": c.language,
                "checker": c.checker,
                "project": c.project,
                "release": c.release,
                "status": c.status,
                "reason": c.reason,
                "notes": c.notes,
                "diagnostics_before": len(c.before),
                "diagnostics_after": len(c.after),
            }
            for c in checks
        ],
        "complete": complete,
        "unchecked_files": unchecked,
        "files": coverage,
        "breaks": breaks,
        "edit_diagnostics": in_edit,
        "environment": _unique(environment),
        # The text of each line the edit changes, by unedited file and line.
        "changed_text": {
            edit.old_path: {str(k): v for k, v in sorted(edit.removed_text.items())}
            for edit in edits
            if edit.old_path and edit.removed_text
        },
    }
    report["summary"] = summary_line(report)
    return report


def _verdict(breaks, in_edit, complete):
    if breaks or in_edit:
        return "breaks"
    return "no_breaks" if complete else "not_fully_checked"


# -- the report as text --------------------------------------------------------


def _count(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _listed(items, shown=5):
    more = len(items) - shown
    return ", ".join(items[:shown]) + (f" and {more} more" if more > 0 else "")


def summary_line(report):
    """The report's verdict as one plain sentence: what the edit breaks,
    and what could not be checked, never "no breaks" for an edit whose
    files were not all checked."""
    breaks = len(report.get("breaks") or [])
    in_edit = len(report.get("edit_diagnostics") or [])
    files = report.get("files") or {}
    unchecked = [f for f, c in files.items() if not c["checked"]] or list(
        report.get("unchecked_files") or []
    )
    failed = [c for c in report.get("checks") or [] if c["status"] != "checked"]
    parts = []
    if breaks or in_edit:
        said = []
        if breaks:
            said.append(_count(breaks, "break") + " in code the edit did not touch")
        if in_edit:
            said.append(_count(in_edit, "error") + " on lines the edit wrote")
        parts.append("BREAKS: " + " and ".join(said) + ".")
    if unchecked or failed or not report.get("checks"):
        if unchecked:
            reasons = sorted(
                {
                    files[f]["reason"]
                    for f in unchecked
                    if f in files and not files[f]["checked"]
                }
            )
            text = (
                "NOT CHECKED: "
                + _listed(unchecked)
                + (" (" + "; ".join(reasons[:2]) + ")" if reasons else "")
                + ". Edits there are unverified"
            )
        else:
            text = "NOT FULLY CHECKED: " + "; ".join(
                f"{c['language']} {c['project']} {c['status']} ({c['reason']})"
                for c in failed[:2]
            )
        if not (breaks or in_edit):
            text += ", and nothing that was checked breaks"
        parts.append(text + ".")
    elif not (breaks or in_edit):
        parts.append(
            "No breaks: every edited file was type-checked and the edit adds no errors."
        )
    partly = [f for f, c in files.items() if c.get("unresolved")]
    if partly:
        parts.append(
            "Imports not resolved (packages or type definitions not installed) in "
            + _listed(partly, 3)
            + ": code using them is not type-checked."
        )
    environment = len(report.get("environment") or [])
    if environment:
        parts.append(
            _count(environment, "new diagnostic")
            + " came from missing packages or type definitions, not from the edit"
            " (listed under environment)."
        )
    return " ".join(parts)


def _diagnostic_line(row, with_declaration=True):
    where = f"{row['file']}:{row['line']}"
    if with_declaration and row.get("declaration"):
        where += " in " + (
            row["declaration"]
            if row.get("declaration_line") is None
            else f"{row['declaration']} (line {row['declaration_line']})"
        )
    return f"- {where}: {row['code']} {row['message']}"


def report_text(report):
    """A checkEdit report as short lines: the verdict first, then each
    break, each error the edit wrote, each check and what it could not
    cover, environment noise apart, and the report's added sections
    (sibling calls, tests)."""
    if report.get("note"):
        return report["note"][:1].upper() + report["note"][1:] + "."
    lines = [report.get("summary") or summary_line(report)]
    breaks = report.get("breaks") or []
    if breaks:
        total = report.get("total_breaks", len(breaks))
        lines.append(
            f"Breaks ({total}, at their line in the unedited source):"
            if total == len(breaks)
            else f"Breaks ({len(breaks)} of {total} shown):"
        )
        lines.extend(_diagnostic_line(row) for row in breaks)
    if report.get("edit_diagnostics"):
        lines.append("Errors on lines the edit wrote:")
        lines.extend(_diagnostic_line(row, False) for row in report["edit_diagnostics"])
    for check in report.get("checks") or []:
        head = f"{_LANGUAGE_NAMES.get(check['language'], check['language'])} {check['project']}"
        if check.get("release"):
            head += f" ({check['checker']} {check['release']})"
        else:
            head += f" ({check['checker']})"
        if check["status"] == "checked":
            text = (
                f"Checked {head}: {check['diagnostics_before']} errors before,"
                f" {check['diagnostics_after']} after."
            )
        else:
            text = f"NOT CHECKED {head}: {check['status']}, {check['reason']}."
        if check.get("notes"):
            text += " " + "; ".join(check["notes"]) + "."
        lines.append(text)
    for file, coverage in sorted((report.get("files") or {}).items()):
        if not coverage["checked"]:
            lines.append(f"Not checked: {file} ({coverage['reason']}).")
        elif coverage.get("unresolved"):
            lines.append(
                f"Partly checked: {file} ("
                + "; ".join(coverage["unresolved"][:5])
                + ")."
            )
    environment = report.get("environment") or []
    if environment:
        lines.append("Environment, not the edit (what the checker could not find):")
        lines.extend(
            _diagnostic_line(row, False) + f" [{row['reason']}]"
            for row in environment[:10]
        )
        if len(environment) > 10:
            lines.append(f"- and {len(environment) - 10} more")
    for line in report.get("sibling_lines") or []:
        lines.append(line)
    tests = report.get("tests") or {}
    if tests.get("tests"):
        lines.append(
            "Existing tests that run the changed code (nearest first):"
            + (" " + "; ".join(tests["run"]) if tests.get("run") else "")
        )
        lines.extend(
            f"- {t['test']} ({t['file']}:{t['line']}) reaches {t['reaches']}"
            f" ({t['reaches_at']}), {_count(t['links_away'], 'link')} away"
            for t in tests["tests"][:10]
        )
    elif tests.get("status") == "succeeded" and tests.get("changed"):
        lines.append("No existing test in the graph reaches the changed declarations.")
    return "\n".join(lines)


def _relative_messages(diagnostics, root):
    prefixes = sorted({f"{root}/", f"{Path(root).resolve()}/"}, key=len, reverse=True)

    def relative(message):
        for prefix in prefixes:
            message = message.replace(prefix, "")
        return message

    return [
        (f, line, column, code, relative(m)) for f, line, column, code, m in diagnostics
    ]


def _unique(rows):
    """Rows in file and line order, each once: projects that share files
    (a build and a test config) report the same diagnostic."""
    found = {json.dumps(row, sort_keys=True): row for row in rows}
    return sorted(
        found.values(),
        key=lambda r: (
            r["file"],
            r["line"],
            r.get("column", 0),
            r["code"],
            r["message"],
        ),
    )


# -- the working copy's own edits ----------------------------------------------


def _git(workspace, git_dir):
    """The git command line for ``workspace``: its own repository, or the
    separate ``git_dir`` it is the work tree of."""
    if git_dir is None:
        return ["git", "-C", str(workspace)]
    return ["git", "--git-dir", str(git_dir), "--work-tree", str(workspace)]


def working_copy_diff(workspace, *, git_dir=None, timeout=120):
    """The workspace's uncommitted edits against its HEAD commit, new files
    included, as a unified diff (``git diff`` of a scratch index, so the
    repository's own index is untouched). Installed packages and nested
    repositories are left out."""
    command = _git(workspace, git_dir)
    with tempfile.TemporaryDirectory(prefix="deepthought-index-") as scratch:
        environ = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        excluded = [f":(exclude,glob)**/{name}/**" for name in sorted(_SKIPPED)]
        for step in (
            ["read-tree", "HEAD"],
            ["add", "-A", "--", ".", *excluded],
        ):
            completed = subprocess.run(
                command + step,
                env=environ,
                capture_output=True,
                timeout=timeout,
            )
            if completed.returncode != 0:
                raise ValueError(
                    "the workspace's edits cannot be read from git: "
                    + completed.stderr.decode(errors="replace").strip()[:500]
                )
        completed = subprocess.run(
            command
            + ["diff", "--cached", "--no-color", "--no-ext-diff", "--no-renames"]
            + ["HEAD"],
            env=environ,
            capture_output=True,
            timeout=timeout,
        )
    if completed.returncode != 0:
        raise ValueError(
            "the workspace's edits cannot be read from git: "
            + completed.stderr.decode(errors="replace").strip()[:500]
        )
    return completed.stdout.decode(errors="replace")


def check_working_copy(workspace, *, git_dir=None, timeout=_TIMEOUT):
    """What the workspace's uncommitted edits break: ``check_edit`` of
    ``working_copy_diff`` against the workspace as its HEAD commit has it.
    The report also carries the diff's ``edited_files``; with no edits it
    says so and checks nothing."""
    diff = working_copy_diff(workspace, git_dir=git_dir)
    if not diff.strip():
        return {
            "verdict": "no_edits",
            "summary": "The workspace has no edits since its HEAD commit.",
            "edited_files": [],
            "checks": [],
            "complete": True,
            "unchecked_files": [],
            "files": {},
            "breaks": [],
            "edit_diagnostics": [],
            "environment": [],
            "note": "the workspace has no edits since its HEAD commit",
        }
    root = Path(workspace).resolve()
    with tempfile.TemporaryDirectory(prefix="deepthought-head-") as scratch:
        # The unedited side: the workspace with its own edits taken out.
        head = Path(scratch) / "head"
        _copy(root, head)
        completed = subprocess.run(
            ["git", "apply", "-R", "--whitespace=nowarn", "-"],
            input=diff.encode(),
            cwd=head,
            capture_output=True,
            timeout=120,
        )
        if completed.returncode != 0:
            raise ValueError(
                "the workspace's edits cannot be taken out again: "
                + completed.stderr.decode(errors="replace").strip()[:500]
            )
        return check_edit(head, diff, timeout=timeout)
