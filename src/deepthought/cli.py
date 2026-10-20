"""Machine-readable local analysis; 0 success, 1 incomplete, 2 invalid input."""

import argparse
import os
import signal
import stat
import sys
from threading import Event

from .consistency import check_source_consistency
from .engine import analyze_repository
from .scip import ingest_scip
from .graph import GraphSnapshot, compose_graph
from .query import QueryRequest, query_graph
from .models import AnalysisResult


def _read_artifact(path, max_bytes):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("Artifact must be a regular file")
        content = source.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError("Artifact byte budget exceeded")
    return content


def load_artifacts(args):
    if min(args.max_artifacts, args.max_artifact_bytes, args.max_total_bytes) < 1:
        raise ValueError("Artifact limits must be positive")
    if len(args.artifacts) > args.max_artifacts:
        raise ValueError("Too many analysis artifacts")
    results, total_bytes = [], 0
    for path in args.artifacts:
        content = _read_artifact(
            path, min(args.max_artifact_bytes, args.max_total_bytes - total_bytes)
        )
        total_bytes += len(content)
        results.append(AnalysisResult.from_json(content.decode("utf-8")))
    return results


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
    compose = subcommands.add_parser(
        "compose",
        help="Compose supplied analysis JSON artifacts into a local reference graph",
    )
    compose.add_argument("artifacts", nargs="+")
    compose.add_argument("--max-artifact-bytes", type=int, default=50_000_000)
    compose.add_argument("--max-total-bytes", type=int, default=100_000_000)
    compose.add_argument("--max-artifacts", type=int, default=100)
    query = subcommands.add_parser("query", help="Query a supplied graph JSON artifact")
    query.add_argument("graph")
    query.add_argument("--repository-id", required=True)
    query.add_argument(
        "--operation",
        required=True,
        choices=("definitions", "references", "dependencies"),
    )
    query.add_argument("--symbol")
    query.add_argument("--file")
    query.add_argument("--limit", type=int, default=20)
    query.add_argument("--max-output-bytes", type=int, default=32_768)
    query.add_argument("--max-artifact-bytes", type=int, default=50_000_000)
    check = subcommands.add_parser(
        "check-source", help="Compare recorded source files with a workspace"
    )
    check.add_argument("workspace")
    check.add_argument("analysis")
    check.add_argument("--repository-id", required=True)
    check.add_argument("--max-artifact-bytes", type=int, default=50_000_000)
    check.add_argument("--max-files", type=int, default=10_000)
    check.add_argument("--max-file-bytes", type=int, default=2_000_000)
    check.add_argument("--max-total-bytes", type=int, default=100_000_000)
    check.add_argument("--max-output-bytes", type=int, default=32_768)
    args = parser.parse_args(argv)
    cancel = Event()
    previous = {}
    if args.command in {"analyze", "check-source"}:
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
        elif args.command == "ingest-scip":
            result = ingest_scip(
                args.index,
                repository_id=args.repository_id,
                max_index_bytes=args.max_index_bytes,
                path_prefix=args.path_prefix,
            )
        elif args.command == "check-source":
            if (
                min(
                    args.max_artifact_bytes,
                    args.max_files,
                    args.max_file_bytes,
                    args.max_total_bytes,
                )
                < 1
            ):
                raise ValueError("Artifact, file and byte limits must be positive")
            if not 1024 <= args.max_output_bytes <= 32_768:
                raise ValueError("Output byte limit must be between 1024 and 32768")
            content = _read_artifact(args.analysis, args.max_artifact_bytes)
            try:
                source = AnalysisResult.from_json(content.decode("utf-8"))
            except RecursionError as exc:
                raise ValueError(
                    "Analysis artifact nesting exceeds parser limits"
                ) from exc
            if (
                not isinstance(source.snapshot_id, str)
                or not source.snapshot_id.strip()
            ):
                raise ValueError("Analysis snapshot identity must be a nonempty string")
            if source.repository_id != args.repository_id:
                raise ValueError("Analysis repository does not match --repository-id")
            result = check_source_consistency(
                args.workspace,
                source,
                max_files=args.max_files,
                max_file_bytes=args.max_file_bytes,
                max_total_bytes=args.max_total_bytes,
                cancel_event=cancel,
            )
        elif args.command == "compose":
            result = compose_graph(load_artifacts(args))
        else:
            request = QueryRequest(
                args.operation,
                args.repository_id,
                symbol=args.symbol,
                file=args.file,
                limit=args.limit,
                max_output_bytes=args.max_output_bytes,
            )
            if args.max_artifact_bytes < 1:
                raise ValueError("Artifact byte limit must be positive")
            content = _read_artifact(args.graph, args.max_artifact_bytes)
            result = query_graph(
                GraphSnapshot.from_json(content.decode("utf-8")), request
            )
        output = result.to_json()
        if (
            args.command == "check-source"
            and len(output.encode("utf-8")) + 1 > args.max_output_bytes
        ):
            raise ValueError("Consistency output exceeds max_output_bytes")
    except (ValueError, OSError) as exc:
        print(f"deepthought: {exc}", file=sys.stderr)
        return 2
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(output)
    successful_status = "unchanged" if args.command == "check-source" else "succeeded"
    return 0 if result.status == successful_status else 1
