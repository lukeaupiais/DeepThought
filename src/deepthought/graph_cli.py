"""CLI adapter for the original engine's graph write and context/query services.

No extraction or query logic is implemented here; both remain in their owning
AnalysisTool, WriteCoordinator and GraphQueryService. Credentials are environment
configuration and are never accepted as command-line arguments or emitted.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys


def connection_settings():
    uri = os.environ.get("DEEPTHOUGHT_NEO4J_URI")
    password = os.environ.get("DEEPTHOUGHT_NEO4J_PASSWORD")
    if not uri or not password:
        raise ValueError("Set DEEPTHOUGHT_NEO4J_URI and DEEPTHOUGHT_NEO4J_PASSWORD")
    database = os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j")
    if database == "system":
        raise ValueError("The system database is not a code graph")
    return dict(
        uri=uri,
        user=os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
        password=password,
        database=database,
    )


def query_observer():
    """Load optional evaluation instrumentation only when explicitly configured."""
    keys = (
        "DEEPTHOUGHT_EVAL_INPUT_ID",
        "DEEPTHOUGHT_EVAL_RUN_ID",
        "DEEPTHOUGHT_EVAL_QUERY_LOG",
    )
    if not any(os.environ.get(key) for key in keys):
        return None
    from .evaluation.neo4j import query_recorder_from_environment

    return query_recorder_from_environment()


def parser():
    p = argparse.ArgumentParser(
        description="DeepThought Neo4j graph import and queries"
    )
    commands = p.add_subparsers(dest="command", required=True)
    index = commands.add_parser(
        "index", help="Run an AnalysisTool and persist its graph"
    )
    index.add_argument("workspace", type=Path)
    index.add_argument(
        "--tool", action="append", help="Repeat to run registered tools in order"
    )
    index.add_argument(
        "--snapshot", help="Explicit graph revision, required for multiple tools"
    )
    index.add_argument("--repository-id", required=True)
    index.add_argument("--language", action="append", choices=("python", "typescript"))
    index.add_argument("--include", action="append")
    index.add_argument("--exclude", action="append")
    for name in ("search", "context", "structure", "callers", "callees", "cypher"):
        q = commands.add_parser(name)
        q.add_argument("--repository-id", required=True)
        q.add_argument("--snapshot", required=True)
        q.add_argument("--limit", type=int, default=20)
        q.add_argument("--max-output-bytes", type=int, default=16000)
        if name in {"search", "cypher"}:
            q.add_argument("--query", required=True)
        if name in {"context", "structure"}:
            q.add_argument("--file", required=True)
        if name == "context":
            q.add_argument("--line", type=int)
        if name in {"callers", "callees"}:
            q.add_argument("--symbol", required=True)
        if name == "cypher":
            q.add_argument("--parameters", default="{}", help="JSON parameter object")
            q.add_argument(
                "--allow-database-scope",
                action="store_true",
                help="Explicitly permit raw reads of a dedicated snapshot database",
            )
    return p


async def execute(args):
    from .storage.graph.connection import Neo4jConnectionManager

    config = connection_settings()
    manager = Neo4jConnectionManager(**config)
    driver = await manager.connect()
    try:
        if args.command == "index":
            from dataclasses import asdict
            from .analysis_tools import tool_registry
            from .storage.graph.write_coordinator import WriteCoordinator

            from .storage.graph.schema import setup_schema

            await setup_schema(driver, database=config["database"])
            names = args.tool or ["tree-sitter"]
            if len(names) != len(set(names)):
                raise ValueError("Each AnalysisTool may occur once")
            if len(names) > 1 and not args.snapshot:
                raise ValueError("Multiple tools require --snapshot graph revision")
            outputs = []
            for name in names:
                tool = tool_registry.get_tool(name, args.workspace)
                if tool is None:
                    raise ValueError("Unknown AnalysisTool: " + name)
                try:
                    if not await tool.check_availability():
                        raise ValueError("Requested AnalysisTool is unavailable")
                    outputs.append(
                        await tool.analyze(
                            args.repository_id,
                            languages=set(args.language) if args.language else None,
                            file_patterns=args.include,
                            skip_patterns=args.exclude,
                        )
                    )
                finally:
                    await tool.cleanup()
            coordinator = WriteCoordinator(
                driver, args.workspace, database=config["database"]
            )
            if len(outputs) == 1 and not args.snapshot:
                result = await coordinator.write_tool_output(
                    names[0], outputs[0], args.repository_id
                )
            else:
                result = await coordinator.write_outputs(
                    outputs, args.repository_id, snapshot_id=args.snapshot
                )
            if not result.success:
                raise ValueError(
                    "Graph import did not complete: " + "; ".join(result.errors)
                )
            return asdict(result)
        from .graph_queries import GraphQueryService

        service = GraphQueryService(
            driver,
            database=config["database"],
            observer=query_observer(),
        )
        kwargs = dict(
            snapshot_id=args.snapshot,
            limit=args.limit,
            max_output_bytes=args.max_output_bytes,
        )
        if args.command == "search":
            return await service.search_elements(
                args.query, args.repository_id, **kwargs
            )
        if args.command == "context":
            return await service.get_code_context(
                args.file, args.repository_id, line_number=args.line, **kwargs
            )
        if args.command == "structure":
            return await service.get_file_structure(
                args.file, args.repository_id, **kwargs
            )
        if args.command in {"callers", "callees"}:
            method = (
                service.get_function_callers
                if args.command == "callers"
                else service.get_function_calls
            )
            return await method(args.symbol, args.repository_id, **kwargs)
        parameters = json.loads(args.parameters)
        if not isinstance(parameters, dict):
            raise ValueError("Cypher parameters must be a JSON object")
        return await service.query_cypher(
            args.query,
            parameters,
            args.repository_id,
            allow_database_scope=args.allow_database_scope,
            **kwargs,
        )
    finally:
        await manager.close()


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        result = asyncio.run(execute(args))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True))
        return 0
    except ImportError:
        print(
            "Provision the deepthought-analysis-engine[neo4j] extra first.",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        # Do not emit driver exceptions: their connection configuration can carry secrets.
        message = (
            str(exc)
            if isinstance(exc, ValueError)
            else f"Graph operation failed ({type(exc).__name__})"
        )
        password = os.environ.get("DEEPTHOUGHT_NEO4J_PASSWORD")
        if password:
            message = message.replace(password, "[redacted]")
        print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
