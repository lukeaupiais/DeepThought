"""AnalysisTool evaluation: prepare verified Neo4j access, record answers, compare."""

import argparse
import json
from pathlib import Path

from . import compare, finalize_record, prepare


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("scenario")
    p.add_argument("workspace")
    p.add_argument("output", type=Path)
    p.add_argument(
        "--interface",
        choices=("neo4j", "raw-context", "tool-access"),
        default="neo4j",
        help="Neo4j is the analysis-tool evaluation default; other modes are controls",
    )
    p.add_argument(
        "--tool-access",
        help="Trusted JSON descriptor with command/help; supplied, not executed",
    )
    p = commands.add_parser("record")
    p.add_argument("prepared")
    p.add_argument("arm", choices=("baseline", "treatment"))
    p.add_argument("answer")
    p.add_argument("output")
    p.add_argument(
        "--telemetry",
        help="JSON object: usage, agent, execution, kind; caller-reported",
    )
    p.add_argument(
        "--run-id", help="Fresh evaluator-owned identity for this actor session"
    )
    p.add_argument(
        "--graph-queries", help="Evaluator-owned GraphQueryService JSONL audit log"
    )
    p = commands.add_parser("compare")
    p.add_argument("baseline")
    p.add_argument("treatment")
    p.add_argument("output")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            access = read(args.tool_access) if args.tool_access else None
            if args.interface == "neo4j":
                from .neo4j import prepare_neo4j

                result = prepare_neo4j(
                    args.scenario, args.workspace, tool_access=access
                )
            else:
                if args.interface == "raw-context" and access is not None:
                    raise ValueError(
                        "Raw-context controls cannot supply a query interface"
                    )
                if args.interface == "tool-access" and access is None:
                    raise ValueError(
                        "Generic tool-access controls require --tool-access"
                    )
                result = prepare(args.scenario, args.workspace, tool_access=access)
            args.output.mkdir(exist_ok=False)
            write(args.output / "prepared.private.json", result)
            for arm, context in result["contexts"].items():
                write(args.output / (arm + ".actor.json"), context)
        elif args.command == "record":
            telemetry = read(args.telemetry) if args.telemetry else {}
            if args.run_id is not None:
                telemetry["run_id"] = args.run_id
            if args.graph_queries is not None:
                telemetry["graph_queries"] = [
                    json.loads(line)
                    for line in Path(args.graph_queries).read_text().splitlines()
                    if line.strip()
                ]
            write(
                args.output,
                finalize_record(
                    read(args.prepared), args.arm, read(args.answer), **telemetry
                ),
            )
        else:
            write(args.output, compare(read(args.baseline), read(args.treatment)))
    except (ValueError, OSError, KeyError, TypeError, ImportError) as exc:
        parser.exit(2, str(exc) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
