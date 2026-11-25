"""The TypeScript checker's view of method calls: the declaration each call
resolves to and the classes its receiver's type arguments name.

``tsmodules/receivers.js`` builds a program for each project, as
``deepthought.ts_modules`` does, and asks the checker for each call's
resolved signature and its receiver's type. Generic inference is the
checker's: ``connection.getRepository(ctx, Order).find()`` has a receiver of
type ``Repository<Order>`` whatever the wrapper's overloads, which no SCIP
index records. Nothing is emitted.
"""

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess

from .ts_modules import TYPESCRIPT_TOOL, locate_typescript

TOOL = TYPESCRIPT_TOOL
_HELPER = Path(__file__).with_name("tsmodules") / "receivers.js"


@dataclass(frozen=True)
class Site:
    """Where a declaration's name is written: workspace-relative ``file``
    (None outside the workspace), 1-based ``line``, UTF-16 ``character``."""

    file: str | None
    line: int
    character: int


@dataclass(frozen=True)
class CheckedCall:
    file: str
    line: int  # 1-based
    character: int  # UTF-16, 0-based, where the method's name starts
    owner: str  # the class or interface that declares the called method
    owner_package: str | None  # its installed package, None in the workspace
    receiver: tuple  # Sites of the receiver type's type arguments
    argument: tuple  # Sites of the class the first argument names


def checked_calls(root, calls, environ=None, timeout=1800):
    """The checker's facts for ``calls``: (workspace-relative file, 1-based
    line, UTF-16 character of the method name). Calls the checker does not
    resolve are left out. Raises ``TypeScriptUnavailable`` without the
    pinned compiler."""
    calls = sorted(set(calls))
    if not calls:
        return []
    node, typescript = locate_typescript(environ)
    root = Path(root).resolve()
    completed = subprocess.run(
        [node, str(_HELPER), typescript],
        input=json.dumps(
            {
                "root": str(root),
                "calls": [
                    {"file": f, "line": line - 1, "character": c}
                    for f, line, c in calls
                ],
            }
        ),
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=root,
    )
    if completed.returncode:
        raise RuntimeError("TypeScript checker failed: " + completed.stderr)
    return [
        CheckedCall(
            row["file"],
            row["line"] + 1,
            row["character"],
            row["owner"],
            _package(row["owner_file"]),
            tuple(_site(root, s) for s in row["receiver"]),
            tuple(_site(root, s) for s in row["argument"]),
        )
        for row in map(json.loads, completed.stdout.splitlines())
    ]


def _site(root, row):
    try:
        file = Path(row["file"]).relative_to(root).as_posix()
    except ValueError:
        file = None
    if file is not None and "node_modules" in file.split("/"):
        file = None
    return Site(file, row["line"] + 1, row["character"])


def _package(path):
    """The installed package a declaration file belongs to (``typeorm``,
    ``@scope/name``), or None for a workspace file."""
    parts = Path(path).parts
    if "node_modules" not in parts:
        return None
    at = len(parts) - 1 - parts[::-1].index("node_modules")
    rest = parts[at + 1 :]
    if not rest:
        return None
    if rest[0].startswith("@") and len(rest) > 1:
        return f"{rest[0]}/{rest[1]}"
    return rest[0]
