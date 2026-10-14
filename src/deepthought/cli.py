"""Machine-readable local analysis; 0 success, 1 incomplete, 2 invalid input."""

import argparse
import signal
import sys
from threading import Event

from .engine import analyze_repository
from .scip import ingest_scip


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="deepthought")
    subcommands = parser.add_subparsers(dest="command", required=True)
    analyze = subcommands.add_parser(
        "analyze", help="Extract named source declarations locally"
    )
    analyze.add_argument("workspace")
    analyze.add_argument("--repository-id", required=True)
    analyze.add_argument(
        "--language", action="append", choices=("python", "typescript")
    )
    analyze.add_argument("--include", action="append")
    analyze.add_argument("--exclude", action="append", default=[])
    analyze.add_argument("--max-files", type=int, default=10_000)
    analyze.add_argument("--max-file-bytes", type=int, default=2_000_000)
    analyze.add_argument("--max-total-bytes", type=int, default=100_000_000)
    ingest = subcommands.add_parser(
        "ingest-scip", help="Read a supplied SCIP artifact; no indexer execution"
    )
    ingest.add_argument("index")
    ingest.add_argument("--repository-id", required=True)
    ingest.add_argument("--max-index-bytes", type=int, default=50_000_000)
    ingest.add_argument(
        "--path-prefix",
        default="",
        help="Map index-relative paths under this workspace-relative directory",
    )
    args = parser.parse_args(argv)
    cancel = Event()
    previous = {}
    if args.command == "analyze":
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda _sig, _frame: cancel.set())
    try:
        if args.command == "analyze":
            result = analyze_repository(
                args.workspace,
                repository_id=args.repository_id,
                languages=tuple(args.language or ("python", "typescript")),
                include=tuple(args.include or ("*",)),
                exclude=tuple(args.exclude),
                max_files=args.max_files,
                max_file_bytes=args.max_file_bytes,
                max_total_bytes=args.max_total_bytes,
                cancel_event=cancel,
            )
        else:
            result = ingest_scip(
                args.index,
                repository_id=args.repository_id,
                max_index_bytes=args.max_index_bytes,
                path_prefix=args.path_prefix,
            )
    except (ValueError, OSError) as exc:
        print(f"deepthought: {exc}", file=sys.stderr)
        return 2
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(result.to_json())
    return 0 if result.status == "succeeded" else 1
