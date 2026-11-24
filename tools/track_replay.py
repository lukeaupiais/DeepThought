"""Record historical-replay results per milestone, for every pinned corpus.

Runs tools/replay_history.py on each ground-truth corpus (the manifest's
``corpora``), replaying the last --commits qualifying commits up to the
corpus's pinned commit, and appends one entry to evals/ground_truth/replay.json:
the label, the engine commit, and each corpus's impact and text-search scores.
The corpus history is fetched to --depth commits under --cache.

Usage:
  python tools/track_replay.py --label NAME [--corpus NAME ...] \\
      [--cache DIR] [--commits 30] [--depth 600] [--dry-run]

Entries are never rewritten; compare a milestone's entry with earlier ones.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ground_truth import CORPUS, ROOT  # noqa: E402
import replay_history  # noqa: E402

RESULTS = CORPUS / "replay.json"
METRICS = ("recall", "precision", "mean_predicted_files")


def fetch_history(corpus, cache, depth):
    """A clone holding ``depth`` commits of history up to the pinned commit."""
    target = Path(cache) / f"{corpus['name']}-history"
    if not (target / ".git").is_dir():
        target.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        subprocess.run(
            ["git", "-C", str(target), "remote", "add", "origin", corpus["url"]],
            check=True,
        )
    subprocess.run(
        ["git", "-C", str(target), "fetch", "-q", f"--depth={depth}", "origin"]
        + [corpus["commit"]],
        check=True,
    )
    return target


def summary(report):
    return {
        "cases": report["cases"],
        **{
            name: {metric: report[name][metric] for metric in METRICS}
            for name in ("impact", "search")
        },
    }


def engine_commit():
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() or None


def track(label, names, cache, commits=30, depth=600):
    manifest = json.loads((CORPUS / "manifest.json").read_text("utf-8"))
    corpora = {}
    for corpus in manifest["corpora"]:
        if names and corpus["name"] not in names:
            continue
        clone = fetch_history(corpus, cache, depth)
        for language in corpus["languages"]:
            report = replay_history.replay(
                clone, language, count=commits, revision=corpus["commit"]
            )
            key = corpus["name"]
            if len(corpus["languages"]) > 1:
                key += f":{language}"
            corpora[key] = summary(report)
    return {
        "label": label,
        "engine_commit": engine_commit(),
        "commits_per_corpus": commits,
        "corpora": corpora,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--corpus", action="append", default=[])
    parser.add_argument("--cache", type=Path, default=Path(".ground-truth-cache"))
    parser.add_argument("--commits", type=int, default=30)
    parser.add_argument("--depth", type=int, default=600)
    parser.add_argument("--dry-run", action="store_true", help="print, do not record")
    args = parser.parse_args(argv)
    entry = track(args.label, set(args.corpus), args.cache, args.commits, args.depth)
    print(json.dumps(entry, indent=2))
    if not args.dry_run:
        history = json.loads(RESULTS.read_text("utf-8")) if RESULTS.exists() else []
        history.append(entry)
        RESULTS.write_text(json.dumps(history, indent=2) + "\n", "utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
