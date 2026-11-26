"""Which repository files are generated, by GitHub Linguist's rules.

The rules are go-enry's port of Linguist's ``generated.rb``
(``enry.IsGenerated``). They look at a file's name and content: generator
headers such as "Code generated ... DO NOT EDIT.", protobuf, gRPC and Thrift
outputs, minified or bundled JavaScript, source maps, lock files and so on.
The ``genfiles`` program in this package runs them. It is built with the Go
toolchain like ``gofacts``, its go-enry release pinned by its go.mod and
go.sum; an offline host installs it ahead with ``go install`` from that
directory as ``deepthought-genfiles``.

A repository's own ``linguist-generated`` attribute in ``.gitattributes``
overrides the rules, as it does on GitHub. It is read with ``git
check-attr`` when the workspace is a git work tree; outside one there are no
attributes to read.

``generated_files(root, paths)`` maps each generated path to how it was
decided: ``"linguist"`` or ``"gitattributes"``.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from .go_facts import build

GENERATED_BY_LINGUIST = "linguist"
GENERATED_BY_ATTRIBUTES = "gitattributes"


def _go():
    go = os.environ.get("DEEPTHOUGHT_GO") or shutil.which("go")
    if not go:
        raise RuntimeError(
            "Generated-file detection needs the Go toolchain (go on PATH or "
            "DEEPTHOUGHT_GO) to build genfiles"
        )
    return go


def _binary(timeout):
    """``DEEPTHOUGHT_GENFILES``, else ``deepthought-genfiles`` on PATH (an
    installation built ahead, as offline hosts need), else genfiles built
    into the cache, which downloads its pinned go-enry module once."""
    installed = os.environ.get("DEEPTHOUGHT_GENFILES") or shutil.which(
        "deepthought-genfiles"
    )
    if installed:
        return Path(installed)
    return build(_go(), timeout=timeout, program="genfiles")[0]


def _linguist(root, paths, timeout):
    binary = _binary(timeout)
    completed = subprocess.run(
        [str(binary), str(root)],
        input=json.dumps(paths),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    result = json.loads(completed.stdout)
    unreadable = {p: v for p, v in result.items() if not isinstance(v, bool)}
    if unreadable:
        path, message = sorted(unreadable.items())[0]
        raise ValueError(f"genfiles could not read {path}: {message}")
    return {path for path, generated in result.items() if generated}


def _attributes(root, paths, timeout):
    """{path: True or False} for paths whose linguist-generated attribute is
    set or unset in the work tree; paths it leaves unspecified are absent."""
    git = shutil.which("git")
    if not git or not paths:
        return {}
    inside = subprocess.run(
        [git, "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return {}
    prefix = subprocess.run(
        [git, "-C", str(root), "rev-parse", "--show-prefix"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    ).stdout.strip()
    completed = subprocess.run(
        [git, "-C", str(root), "check-attr", "-z", "--stdin", "linguist-generated"],
        input="\0".join(paths) + "\0",
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    fields = completed.stdout.split("\0")
    values = {}
    for index in range(0, len(fields) - 2, 3):
        path, _, value = fields[index : index + 3]
        path = path.removeprefix(prefix)
        if value in ("set", "true"):
            values[path] = True
        elif value in ("unset", "false"):
            values[path] = False
    return values


def generated_files(root, paths, *, timeout=300):
    """{path: how it was decided} for the generated ``paths`` (relative,
    forward slashes) of ``root``."""
    root = Path(root)
    paths = sorted(set(paths))
    if not paths:
        return {}
    decided = dict.fromkeys(_linguist(root, paths, timeout), GENERATED_BY_LINGUIST)
    for path, generated in _attributes(root, paths, timeout).items():
        if generated:
            decided[path] = GENERATED_BY_ATTRIBUTES
        else:
            decided.pop(path, None)
    return dict(sorted(decided.items()))
