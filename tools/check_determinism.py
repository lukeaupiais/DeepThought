"""Check that analysis is deterministic across processes, paths and file order.

The workspace is copied twice: once in sorted order, once in reverse order
under a deeper directory (so directory listing order, inode order and the
absolute root differ). Each copy is analyzed in a fresh interpreter with a
different PYTHONHASHSEED and working directory. Every run emits one canonical
dump: the analysis artifact, every call, import, supertype and impl
resolution, the composed reference graph, and the standardized output the
graph store receives, with wall-clock timing fields removed. The dumps must be
byte-identical.

Usage:
  python tools/check_determinism.py WORKSPACE [--language L ...] [--runs N]

Exit status 0 when every run agrees, 1 with the first differing path
otherwise.
"""

import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

# Fields that record how long a run took, not what it found.
TIMING_KEYS = frozenset({"execution_time_seconds", "timestamp"})
SEEDS = ("0", "1", "4242", "987654321")


def _strip_timing(value):
    if isinstance(value, dict):
        return {k: _strip_timing(v) for k, v in value.items() if k not in TIMING_KEYS}
    if isinstance(value, list):
        return [_strip_timing(v) for v in value]
    return value


def dump(workspace, languages, repository_id="determinism"):
    """The canonical bytes of everything the engine derives from WORKSPACE."""
    import deepthought
    from deepthought.analysis_tools import TreeSitterTool
    from deepthought.calls import resolve_calls
    from deepthought.imports import resolve_imports
    from deepthought.inheritance import resolve_rust_impls, resolve_supertypes

    result = deepthought.analyze_repository(
        workspace, repository_id=repository_id, languages=languages
    )
    from deepthought.indexers import required_indexes

    tool = TreeSitterTool(Path(workspace))
    with tempfile.TemporaryDirectory() as scratch:
        _, indexes = required_indexes(workspace, scratch, languages, repository_id)
        output = asyncio.run(
            tool.analyze(repository_id, languages=set(languages), scip_indexes=indexes)
        )
    document = {
        "analysis": json.loads(result.to_json()),
        "calls": [asdict(r) for r in resolve_calls(result)],
        "imports": [asdict(r) for r in resolve_imports(result)],
        "supertypes": [asdict(r) for r in resolve_supertypes(result)],
        "impls": [asdict(r) for r in resolve_rust_impls(result)],
        "graph": json.loads(deepthought.compose_graph([result]).to_json()),
        "standardized_output": _strip_timing(json.loads(output.to_json())),
    }
    return json.dumps(
        document, sort_keys=True, ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def copy_tree(source, target, reverse):
    """Copy files one at a time so creation order follows ``reverse``."""
    files = sorted(
        p
        for p in Path(source).rglob("*")
        if p.is_file()
        and not p.is_symlink()
        and ".git" not in p.relative_to(source).parts
    )
    for path in reversed(files) if reverse else files:
        destination = Path(target) / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)


def first_difference(left, right, path="$"):
    if type(left) is not type(right):
        return path, left, right
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                return f"{path}.{key}", left.get(key), right.get(key)
            found = first_difference(left[key], right[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(left, list):
        for i, (a, b) in enumerate(zip(left, right)):
            found = first_difference(a, b, f"{path}[{i}]")
            if found:
                return found
        if len(left) != len(right):
            return f"{path}.length", len(left), len(right)
        return None
    return None if left == right else (path, left, right)


def run_variants(workspace, languages, runs=2):
    """Dump WORKSPACE under ``runs`` (2..4) variants; return (digests, dumps)."""
    script = Path(__file__).resolve()
    dumps = []
    with tempfile.TemporaryDirectory() as scratch:
        for index in range(runs):
            reverse = index % 2 == 1
            copy = Path(scratch, *(["nested"] * index), f"copy{index}")
            copy_tree(workspace, copy, reverse)
            environment = {**os.environ, "PYTHONHASHSEED": SEEDS[index]}
            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--dump",
                    str(copy),
                    *[a for lang in languages for a in ("--language", lang)],
                ],
                cwd=scratch if reverse else copy,
                env=environment,
                capture_output=True,
                check=True,
            )
            dumps.append(completed.stdout)
    return [hashlib.sha256(d).hexdigest() for d in dumps], dumps


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--language", action="append", required=True)
    parser.add_argument("--runs", type=int, default=4, choices=(2, 3, 4))
    parser.add_argument("--dump", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    languages = tuple(sorted(set(args.language)))
    if args.dump:
        sys.stdout.buffer.write(dump(args.workspace, languages))
        return 0
    digests, dumps = run_variants(args.workspace.resolve(), languages, args.runs)
    report = {"runs": len(digests), "sha256": digests, "bytes": len(dumps[0])}
    if len(set(digests)) > 1:
        odd = next(i for i, d in enumerate(digests) if d != digests[0])
        where, left, right = first_difference(
            json.loads(dumps[0]), json.loads(dumps[odd])
        )
        report["first_difference"] = {
            "run": odd,
            "path": where,
            "run0": left,
            f"run{odd}": right,
        }
    print(json.dumps(report, indent=2, default=str))
    return 0 if len(set(digests)) == 1 else 1


if __name__ == "__main__":
    sys.exit(main())
