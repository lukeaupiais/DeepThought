"""Fail when a commit names an AI tool as its author, committer or co-author.

Checks every commit in the git revision range given (for example
``BASE..HEAD``): the author and committer name and email, every trailer line
(``Co-Authored-By:``, ``Claude-Session:`` and the like) and any
"Generated with/by" line. The commit's other text is not checked, since the
project integrates with tools of those names.

Usage:
  python tools/check_attribution.py BASE..HEAD [--repo PATH]

Exit status 1 lists each offending commit and what matched.
"""

import argparse
from pathlib import Path
import re
import subprocess
import sys

NAMES = re.compile(r"claude|anthropic", re.I)
TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:\s")
GENERATED = re.compile(r"generated (with|by)\b", re.I)
# Fields separated by NUL, commits by RS (no commit text contains either).
FORMAT = "%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x1e"


def commits(repo, revisions):
    out = subprocess.run(
        ["git", "-C", str(repo), "log", f"--format={FORMAT}", *revisions],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if record:
            sha, *identity, body = record.split("\x00")
            yield sha, identity, body


def problems(identity, body):
    found = [f"identity {value!r}" for value in identity if NAMES.search(value)]
    for line in body.splitlines():
        line = line.strip()
        if (TRAILER.match(line) and NAMES.search(line)) or (
            GENERATED.search(line) and NAMES.search(line)
        ):
            found.append(f"line {line!r}")
    return found


def check(repo, revisions):
    return {
        sha: found
        for sha, identity, body in commits(repo, revisions)
        if (found := problems(identity, body))
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("revisions", nargs="+", help="git log revision arguments")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    failures = check(args.repo, args.revisions)
    for sha, found in failures.items():
        print(f"{sha}: " + "; ".join(found))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
