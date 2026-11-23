"""Time full against incremental analysis of one workspace, and check they agree.

Usage: python tools/benchmark_incremental.py WORKSPACE [--language L] [--runs N]

The workspace is copied to a temporary directory so it is never modified. For
each scenario the script times a full analysis and an incremental analysis that
reuses the previous result, reports the median of N runs, and fails if the two
artifacts differ in any byte. Scenarios: nothing changed, one file changed, and
10% of files changed (every tenth file in path order gets a new declaration).
"""

import argparse
import json
from pathlib import Path
import shutil
import statistics
import sys
import tempfile
import time

from deepthought.engine import analyze_repository
from deepthought.syntax import LANGUAGES


def timed(root, languages, **options):
    started = time.perf_counter()
    result = analyze_repository(
        root, repository_id="benchmark", languages=languages, **options
    )
    return time.perf_counter() - started, result


def touch(path, marker):
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        text += f"\n\ndef benchmark_marker_{marker}():\n    pass\n"
    else:
        text += f"\nexport function benchmarkMarker{marker}() {{}}\n"
    path.write_text(text, encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--language", action="append", choices=sorted(LANGUAGES))
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args(argv)
    languages = tuple(sorted(args.language or LANGUAGES))
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "workspace"
        shutil.copytree(args.workspace, root, symlinks=True)
        baseline = analyze_repository(
            root, repository_id="benchmark", languages=languages
        )
        parsed = sorted(
            c.file for c in baseline.coverage if c.status in {"analyzed", "partial"}
        )
        report = {
            "workspace": str(args.workspace),
            "languages": languages,
            "files": len(baseline.coverage),
            "parsed_files": len(parsed),
            "runs": args.runs,
            "scenarios": [],
        }
        scenarios = (
            ("unchanged", []),
            ("one_file_changed", parsed[:1]),
            ("ten_percent_changed", parsed[::10]),
        )
        for marker, (name, changed) in enumerate(scenarios):
            previous = analyze_repository(
                root, repository_id="benchmark", languages=languages
            )
            for file in changed:
                touch(root / file, marker)
            full_times, incremental_times = [], []
            for _ in range(args.runs):
                seconds, full = timed(root, languages)
                full_times.append(seconds)
                reuse = {}
                seconds, incremental = timed(
                    root, languages, previous=previous, reuse=reuse
                )
                incremental_times.append(seconds)
                if incremental.to_json() != full.to_json():
                    print(f"{name}: incremental differs from full", file=sys.stderr)
                    return 1
            report["scenarios"].append(
                {
                    "scenario": name,
                    "changed_files": len(changed),
                    "reused": reuse["reused"],
                    "parsed": reuse["parsed"],
                    "full_median_seconds": round(statistics.median(full_times), 4),
                    "incremental_median_seconds": round(
                        statistics.median(incremental_times), 4
                    ),
                    "identical": True,
                }
            )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
