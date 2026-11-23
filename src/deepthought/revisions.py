"""Link analysis snapshots to the Git commit their source files came from.

A snapshot is identified by its content, so the same content analyzed at two
commits is one snapshot linked to both. A commit is linked only when every
analyzed file is tracked and unchanged from that commit, and HEAD did not move
while the files were read. Files that Git ignores are never part of a commit,
so analyzing one prevents the link.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess

_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class CommitLink:
    """Whether a snapshot may be linked to a commit, and why not if it may not."""

    commit: str | None
    linked: bool
    reason: str | None = None

    def to_dict(self):
        return {"commit": self.commit, "linked": self.linked, "reason": self.reason}


def is_commit(value) -> bool:
    return isinstance(value, str) and bool(_COMMIT.fullmatch(value))


def _git(workspace, *arguments):
    environment = dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), *arguments],
            capture_output=True,
            env=environment,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def head_commit(workspace) -> str | None:
    """The workspace's HEAD commit, or None outside a usable Git checkout."""
    output = _git(workspace, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    commit = output.decode().strip() if output else None
    return commit if is_commit(commit) else None


def _paths(output):
    return {item.decode() for item in output.split(b"\0") if item}


def commit_link(workspace, files, commit_before) -> CommitLink:
    """Decide whether analyzed ``files`` (workspace-relative) equal a commit.

    ``commit_before`` is HEAD as read before analysis started.
    """
    workspace = Path(workspace)
    if commit_before is None:
        return CommitLink(None, False, "not_a_git_checkout")
    if head_commit(workspace) != commit_before:
        return CommitLink(commit_before, False, "head_moved_during_analysis")
    tracked = _git(workspace, "ls-files", "-z", "--full-name", "--", ".")
    # diff paths are relative to the repository root, like ls-files --full-name.
    changed = _git(
        workspace,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        "--no-ext-diff",
        commit_before,
        "--",
        ".",
    )
    prefix = _git(workspace, "rev-parse", "--show-prefix")
    if tracked is None or changed is None or prefix is None:
        return CommitLink(commit_before, False, "git_status_unavailable")
    prefix = prefix.decode().strip()
    analyzed = {prefix + file for file in files}
    if analyzed - _paths(tracked):
        return CommitLink(commit_before, False, "untracked_files_analyzed")
    if analyzed & _paths(changed):
        return CommitLink(commit_before, False, "uncommitted_changes")
    return CommitLink(commit_before, True)
