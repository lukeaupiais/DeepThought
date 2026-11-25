"""Historical PR replay: does impact analysis anticipate what commits touched?

For each of a repository's recent commits that edits two or more source
files, the parent revision is analyzed and, for every edited file F, the
declarations the commit's diff touches in F seed an impact query (dependents
reached backwards over CALLS, INHERITS_FROM, IMPLEMENTS and DISPATCHES_TO; see
tools/check_impact.py). The files of those dependents are the prediction; the
other files the commit edited are the outcome. A second line,
``impact_fixtures``, also follows USES_FIXTURE from tests to the pytest
fixtures they use and USES_DEPENDENCY from endpoints to their FastAPI
dependencies, as get_impact does; ``impact`` keeps the code relations
alone so earlier results stay comparable. A plain
text search is scored the same way as the baseline: every file that contains
a seed's name as a whole word.

Co-changed files are not the same thing as impacted files: a commit can touch
unrelated files, and a dependent may need no edit. The replay measures how
well each predictor anticipates real edits, not whether a link is correct
(the ground-truth corpus measures that).

Usage:
  python tools/replay_history.py REPO --language python [--commits 40] \\
      [--revision REV] [--cases]

REPO is a clone with history. Nothing in it is modified; each parent revision
is exported to a temporary directory.
"""

import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_impact import CODE_RELATIONS, RELATIONS  # noqa: E402
from check_impact import expected as dependents  # noqa: E402

EXTENSIONS = {
    "python": (".py",),
    "typescript": (".ts", ".tsx"),
    "javascript": (".js", ".jsx", ".mjs", ".cjs"),
    "go": (".go",),
    "java": (".java",),
    "rust": (".rs",),
}
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", re.M)


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def candidates(repo, language, count, max_files, revision="HEAD"):
    """Non-merge commits up to ``revision`` editing 2..max_files existing
    source files, newest first."""
    suffixes = EXTENSIONS[language]
    chosen = []
    for commit in git(
        repo, "rev-list", "--no-merges", "--first-parent", revision
    ).split():
        rows = git(repo, "diff-tree", "--no-commit-id", "-r", "--name-status", commit)
        modified = [
            line.split("\t")[1]
            for line in rows.splitlines()
            if line.startswith("M\t") and line.endswith(suffixes)
        ]
        if 2 <= len(modified) <= max_files:
            chosen.append((commit, sorted(modified)))
        if len(chosen) == count:
            break
    return chosen


def touched_lines(repo, parent, commit, file):
    """Lines of ``file`` in ``parent`` the commit replaces or deletes, or
    the line before an insertion."""
    diff = git(repo, "diff", "-U0", parent, commit, "--", file)
    lines = set()
    for match in _HUNK.finditer(diff):
        start, length = int(match.group(1)), int(match.group(2) or 1)
        lines.update(range(start, start + length) if length else (start,))
    return lines


def export(repo, revision, target):
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", revision],
        check=True,
        capture_output=True,
    ).stdout
    with tempfile.TemporaryFile() as stream:
        stream.write(archive)
        stream.seek(0)
        with tarfile.open(fileobj=stream) as tar:
            tar.extractall(target, filter="data")


def replay(repo, language, count=40, max_files=12, max_depth=3, revision="HEAD"):
    from deepthought.analysis_tools import TreeSitterTool

    suffixes = EXTENSIONS[language]
    predictors = ("impact", "impact_fixtures", "search")
    totals = {name: Counter() for name in predictors}
    cases, skipped = [], Counter()
    for commit, edited in candidates(repo, language, count, max_files, revision):
        parent = f"{commit}^"
        with tempfile.TemporaryDirectory() as scratch:
            export(repo, parent, scratch)
            output = asyncio.run(
                TreeSitterTool(Path(scratch)).analyze("replay", languages={language})
            )
            texts = {}
            for path in Path(scratch).rglob("*"):
                if path.is_file() and path.suffix in suffixes:
                    relative = path.relative_to(scratch).as_posix()
                    texts[relative] = path.read_text("utf-8", "replace")
        symbols = [n.properties for n in output.nodes if "Symbol" in n.labels]
        file_of = {s["qualified_name"]: s["file_path"] for s in symbols}
        for file in edited:
            lines = touched_lines(repo, parent, commit, file)
            seeds = [
                s
                for s in symbols
                if s["file_path"] == file
                and any(s["start_line"] <= n <= s["end_line"] for n in lines)
            ]
            # The innermost declarations touched; their containers add
            # every sibling, which the diff did not touch.
            seeds = [
                s
                for s in seeds
                if not any(
                    o is not s
                    and s["start_line"] <= o["start_line"]
                    and o["end_line"] <= s["end_line"]
                    for o in seeds
                )
            ]
            if not seeds:
                skipped["no_declaration_touched"] += 1
                continue
            actual = set(edited) - {file}
            impact, with_fixtures = set(), set()
            for seed in seeds:
                for found, relations in (
                    (impact, CODE_RELATIONS),
                    (with_fixtures, RELATIONS),
                ):
                    for dependent in dependents(
                        output, seed["qualified_name"], max_depth, relations
                    ):
                        found.add(file_of.get(dependent))
            impact -= {file, None}
            with_fixtures -= {file, None}
            names = {s["name"] for s in seeds}
            pattern = re.compile(
                r"\b(?:" + "|".join(map(re.escape, sorted(names))) + r")\b"
            )
            search = {
                f for f, text in texts.items() if f != file and pattern.search(text)
            }
            case = {"commit": commit[:12], "file": file, "actual": len(actual)}
            for name, predicted in zip(predictors, (impact, with_fixtures, search)):
                hits = len(predicted & actual)
                totals[name].update(
                    {"hits": hits, "predicted": len(predicted), "actual": len(actual)}
                )
                case[name] = {"predicted": len(predicted), "hits": hits}
            cases.append(case)
    summary = {}
    for name, t in totals.items():
        summary[name] = {
            "recall": round(t["hits"] / t["actual"], 4) if t["actual"] else None,
            "precision": round(t["hits"] / t["predicted"], 4)
            if t["predicted"]
            else None,
            "mean_predicted_files": round(t["predicted"] / len(cases), 2)
            if cases
            else None,
            **dict(t),
        }
    return {
        "cases": len(cases),
        "skipped": dict(skipped),
        **summary,
        "per_case": cases,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=Path)
    parser.add_argument("--language", required=True, choices=sorted(EXTENSIONS))
    parser.add_argument("--commits", type=int, default=40)
    parser.add_argument("--max-files", type=int, default=12)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--revision", default="HEAD", help="newest commit to replay")
    parser.add_argument("--cases", action="store_true", help="print every case")
    args = parser.parse_args(argv)
    report = replay(
        args.repo,
        args.language,
        args.commits,
        args.max_files,
        args.max_depth,
        args.revision,
    )
    if not args.cases:
        report.pop("per_case")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
