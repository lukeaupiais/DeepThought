"""String values of JavaScript/TypeScript names from the TypeScript compiler.

``tsfacts/main.js`` builds the repository's programs with the TypeScript
compiler (one per tsconfig.json or jsconfig.json, which the compiler parses
with ``extends``, ``paths`` and ``baseUrl``, and one inferred program for the
other files) and prints, for each name or member access in the analyzed
files, the string value the checker resolves it to: an enum member's
constant, a const's initializer, a property of a const object literal, or a
single string literal type, through imports, re-exports, path aliases and
namespace imports. Module resolution, enum evaluation and symbol lookup are
the compiler's.

The compiler is a required dependency: ``typescript`` at the pinned
``TYPESCRIPT_VERSION`` is installed with npm into the cache directory on
first use (or taken from ``DEEPTHOUGHT_TYPESCRIPT``, a path to its package
directory), and Node.js is ``node`` on ``PATH`` (or ``DEEPTHOUGHT_NODE``).
Without them ``collect`` raises ``TypeScriptMissing``. Nothing of the
analyzed repository is built or run.
"""

from dataclasses import dataclass, field
from importlib import resources
import json
import os
from pathlib import Path
import shutil
import subprocess

TYPESCRIPT_VERSION = "5.9.3"


class TypeScriptMissing(RuntimeError):
    """Node.js or the TypeScript compiler is not available."""


@dataclass
class TsFacts:
    # {file: {(start byte, end byte): value}}
    values: dict = field(default_factory=dict)
    programs: dict = field(default_factory=dict)  # file -> tsconfig ("" inferred)
    modules: dict = field(default_factory=dict)  # (file, specifier) -> file
    failures: dict = field(default_factory=dict)  # file -> names not resolved
    rejected_configs: list = field(default_factory=list)


def _cache_directory():
    configured = os.environ.get("DEEPTHOUGHT_CACHE")
    if configured:
        return Path(configured)
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "deepthought"


def node():
    found = os.environ.get("DEEPTHOUGHT_NODE") or shutil.which("node")
    if not found:
        raise TypeScriptMissing("Node.js is required to read JavaScript/TypeScript")
    return found


def typescript(timeout=600):
    """The directory of the pinned typescript package, installed with npm
    into the cache directory when missing."""
    configured = os.environ.get("DEEPTHOUGHT_TYPESCRIPT")
    if configured:
        return Path(configured)
    prefix = _cache_directory() / f"typescript-{TYPESCRIPT_VERSION}"
    package = prefix / "node_modules" / "typescript"
    if (package / "package.json").is_file():
        return package
    npm = shutil.which("npm")
    if npm is None:
        raise TypeScriptMissing("npm is required to install the TypeScript compiler")
    prefix.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                npm,
                "install",
                "--prefix",
                str(prefix),
                "--no-save",
                "--no-audit",
                "--no-fund",
                "--ignore-scripts",
                f"typescript@{TYPESCRIPT_VERSION}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise TypeScriptMissing(
            f"could not install typescript@{TYPESCRIPT_VERSION}: {error}"
        ) from error
    if not (package / "package.json").is_file():
        raise TypeScriptMissing(f"typescript@{TYPESCRIPT_VERSION} was not installed")
    return package


def collect(root, files, defines=(), *, timeout=1800):
    """TsFacts of the JavaScript/TypeScript ``files`` (relative paths) of
    ``root``; ``defines`` is [(directory, {global: string value})] of the
    globals a build defines (``script_constants.angular_defines``)."""
    facts = TsFacts()
    files = sorted(files)
    if not files:
        return facts
    script = resources.files(__package__).joinpath("tsfacts/main.js")
    with resources.as_file(script) as path:
        run = subprocess.run(
            [
                node(),
                str(path),
                str(typescript()),
                str(Path(root).resolve()),
                json.dumps([[d, dict(v)] for d, v in defines]),
            ],
            input="\n".join(files) + "\n",
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
    for line in run.stdout.splitlines():
        item = json.loads(line)
        if "error" in item:
            facts.rejected_configs.append(item["error"])
        elif "failures" in item:
            facts.failures[item["file"]] = item["failures"]
        elif "module" in item:
            facts.modules[(item["file"], item["module"])] = item["resolved"]
        elif "program" in item:
            facts.programs[item["file"]] = item["program"]
        else:
            facts.values.setdefault(item["file"], {})[(item["start"], item["end"])] = (
                item["value"]
            )
    return facts
