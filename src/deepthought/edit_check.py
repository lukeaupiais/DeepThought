"""What a proposed edit breaks, from the language's own type checker.

A unified diff is applied to a scratch copy of the workspace and the
checker of each edited language runs on the workspace as it is and on the
copy: ``go test -run ^$ -exec true -vet=off`` (compiles every package of the
module and its tests; ``-exec true`` runs no test binary), ``tsc --noEmit``
of each TypeScript project, and ``pyright`` of each Python project. A
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
_GO_DIAGNOSTIC = re.compile(
    r"^(?P<file>[^\s:][^:]*\.go):(?P<line>\d+):(?P<column>\d+): (?P<message>.*)$"
)
_TS_DIAGNOSTIC = re.compile(
    r"^(?P<file>.+?\.[cm]?[jt]sx?)\((?P<line>\d+),(?P<column>\d+)\): error (?P<code>TS\d+): (?P<message>.*)$"
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
                hunk[4] += 1
                hunk[1] += 1
            elif line.startswith("+"):
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
    env = dict(os.environ, GOTOOLCHAIN="local", CGO_ENABLED="0")
    completed = subprocess.run(
        [
            go,
            "test",
            "-count=1",
            "-run",
            "^$",
            "-exec",
            "true",
            "-vet=off",
            "-gcflags=-e",
            "./...",
        ],
        cwd=module,
        env=env,
        capture_output=True,
        timeout=timeout,
    )
    found = []
    for line in (
        (completed.stdout + completed.stderr).decode(errors="replace").splitlines()
    ):
        match = _GO_DIAGNOSTIC.match(line.strip())
        if match:
            path = os.path.normpath(os.path.join(module, match["file"]))
            found.append(
                (
                    _relative(path, root),
                    int(match["line"]),
                    int(match["column"]),
                    "go",
                    match["message"],
                )
            )
    return found


def _check_go(root, edited_root, files, timeout):
    go = os.environ.get("DEEPTHOUGHT_GO") or shutil.which("go")
    checks = []
    for module in _go_projects(root, files):
        check = Check("go", "go test -run ^$", _relative(module, root) or ".")
        if go is None:
            check.status, check.reason = (
                "unavailable",
                "go is required to check Go edits",
            )
        else:
            try:
                check.before = _go_run(go, root, module, timeout)
                check.after = _go_run(
                    go,
                    edited_root,
                    Path(edited_root) / module.relative_to(root),
                    timeout,
                )
            except (OSError, subprocess.SubprocessError) as error:
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
    """(missing, types) of a project as tsc resolves it: the entries of
    compilerOptions.types that are not installed (tsc stops before checking
    any file when one is missing), and the installed ones."""
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
        return [], None
    types = (config.get("compilerOptions") or {}).get("types")
    if not isinstance(types, list):
        return [], None
    missing = [t for t in types if not _type_package_installed(project.parent, str(t))]
    return missing, [t for t in types if t not in missing]


def _tsc_run(node, package, root, project, timeout):
    """(diagnostics, errors with no file, missing type packages). An error
    with no file (a bad option) stops tsc before it checks any file, so a
    type package that is not installed is left out of compilerOptions.types
    and reported. The check runs from a wrapper config beside the project
    (in a scratch copy), which extends it, so every path the project leaves
    to its config's directory (include, rootDir, typeRoots) stays the same.
    """
    missing, types = _project_config(node, package, project, timeout)
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
        if match:
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
    return found, general, missing


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
            check.before, general, missing = _tsc_run(
                node, package, root, project, timeout
            )
            edited_project = Path(edited_root) / project.relative_to(root)
            check.after, general_after, _ = _tsc_run(
                node, package, edited_root, edited_project, timeout
            )
            if missing:
                check.notes.append(
                    "type packages not installed, left out of the check: "
                    + ", ".join(missing)
                )
            if general or general_after:
                check.status = "failed"
                check.reason = (general or general_after)[0][:300]
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


def _pyright_run(command, root, project, timeout):
    completed = subprocess.run(
        [*command, "--outputjson"],
        cwd=project,
        capture_output=True,
        timeout=timeout,
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


def _check_python(root, edited_root, files, timeout):
    environ = dict(os.environ)
    root = Path(root).resolve()
    projects = sorted({_nearest(root, f, _PY_PROJECT) or root for f in files})
    checks = []
    for project in projects:
        check = Check("python", "pyright", _relative(project, root))
        try:
            command, check.release = _pyright(environ)
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


# -- declarations --------------------------------------------------------------


def _declarations(root, file, cache):
    """[(start line, end line, name)] of the declarations in ``file``."""
    if file in cache:
        return cache[file]
    from .engine import _source_language
    from .syntax import LANGUAGES, extract, make_parser, parse_source

    found = []
    language = _source_language(file, tuple(LANGUAGES))
    if language is not None:
        suffix = PurePosixPath(file).suffix
        try:
            content = (Path(root) / file).read_bytes()
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
    breaks, in_edit, declarations = [], [], {}
    for check in checks:
        if check.status != "checked":
            continue
        unedited = Counter(
            (f, line, code, message) for f, line, _, code, message in check.before
        )
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
    complete = bool(checks) and all(check.status == "checked" for check in checks)
    unchecked = sorted(
        {f for f in edited if not any(f.endswith(s) for _, s, _ in _CHECKERS)}
    )
    return {
        "edited_files": edited,
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
        "complete": complete and not unchecked,
        "unchecked_files": unchecked,
        "breaks": _unique(breaks),
        "edit_diagnostics": _unique(in_edit),
    }


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
