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
    index.add_argument(
        "--language", action="append", choices=("javascript", "python", "typescript")
    )
    index.add_argument("--include", action="append")
    index.add_argument("--exclude", action="append")
    index.add_argument(
        "--scip-index",
        type=Path,
        help="Source-bound SCIP index for --tool scip, default workspace/index.scip",
    )
    index.add_argument(
        "--previous-artifact",
        type=Path,
        help="Earlier tree-sitter analysis; unchanged files need no parsing",
    )
    index.add_argument(
        "--write-artifact",
        type=Path,
        help="Write this tree-sitter analysis here, for the next --previous-artifact",
    )
    snapshots = commands.add_parser(
        "snapshots", help="List complete snapshots and the commits they came from"
    )
    snapshots.add_argument("--repository-id", required=True)
    snapshots.add_argument("--commit", help="Full commit hash")
    snapshots.add_argument("--limit", type=int, default=20)
    snapshots.add_argument("--max-output-bytes", type=int, default=16000)
    diff = commands.add_parser("diff", help="Compare two complete snapshots")
    diff.add_argument("--repository-id", required=True)
    diff.add_argument("--base", required=True, help="Base snapshot ID")
    diff.add_argument("--head", required=True, help="Head snapshot ID")
    diff.add_argument(
        "--change",
        action="append",
        choices=(
            "nodes.added",
            "nodes.removed",
            "nodes.changed",
            "edges.added",
            "edges.removed",
        ),
        help="Repeat to list only these categories; all are always counted",
    )
    diff.add_argument("--limit", type=int, default=20)
    diff.add_argument("--max-output-bytes", type=int, default=16000)
    for name in (
        "search",
        "context",
        "structure",
        "callers",
        "callees",
        "bases",
        "subtypes",
        "members",
        "owner",
        "impact",
        "file-impact",
        "imports",
        "importers",
        "references",
        "implementations",
        "supertypes",
        "dependencies",
        "dependents",
        "explain-edge",
        "cypher",
    ):
        q = commands.add_parser(name)
        q.add_argument("--repository-id", required=True)
        q.add_argument("--snapshot", required=True)
        if name != "explain-edge":
            q.add_argument("--limit", type=int, default=20)
        q.add_argument("--max-output-bytes", type=int, default=16000)
        if name in {"search", "cypher"}:
            q.add_argument("--query", required=True)
        if name in {
            "context",
            "structure",
            "imports",
            "importers",
            "dependencies",
            "dependents",
        }:
            q.add_argument("--file", required=True)
        if name in {"implementations", "supertypes"}:
            q.add_argument("--symbol", required=True, help="SCIP native symbol")
            q.add_argument("--file", help="Document scope for local SCIP symbols")
        if name == "references":
            q.add_argument("--symbol", required=True, help="SCIP native symbol")
            q.add_argument(
                "--file", help="Document scope; required for local SCIP symbols"
            )
            q.add_argument(
                "--after-reference-id",
                default="",
                help="Continue after this reference_id",
            )
        if name == "context":
            q.add_argument("--line", type=int)
        if name in {
            "callers",
            "callees",
            "bases",
            "subtypes",
            "members",
            "owner",
            "impact",
        }:
            q.add_argument("--symbol", required=True, help="Qualified name")
        if name == "file-impact":
            q.add_argument("--file", required=True)
        if name in {"impact", "file-impact"}:
            q.add_argument("--max-depth", type=int, default=3, help="1 to 5 hops")
        if name in {
            "callers",
            "callees",
            "bases",
            "subtypes",
            "imports",
            "importers",
            "impact",
            "file-impact",
        }:
            q.add_argument(
                "--evidence",
                action="append",
                choices=("resolved", "pattern_matched", "name_matched"),
                help="Repeat to keep only edges with these evidence types",
            )
        if name == "explain-edge":
            q.add_argument("--edge-id", required=True)
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
            from .revisions import CommitLink, commit_link, head_commit

            # Read HEAD before any file so a commit that moves mid-run is caught.
            commit_before = head_commit(args.workspace)
            outputs = []
            if (
                args.previous_artifact or args.write_artifact
            ) and "tree-sitter" not in names:
                raise ValueError("Analysis artifacts belong to the tree-sitter tool")
            earlier = None
            if args.previous_artifact is not None:
                from .cli import _read_artifact
                from .models import AnalysisResult

                earlier = AnalysisResult.from_json(
                    _read_artifact(args.previous_artifact, 50_000_000).decode("utf-8")
                )
            analysis = reuse = None
            if args.scip_index is not None and "scip" not in names:
                raise ValueError("--scip-index belongs to the scip tool")
            for name in names:
                tool = tool_registry.get_tool(
                    name,
                    args.workspace,
                    **(
                        {"index_path": args.scip_index}
                        if name == "scip" and args.scip_index is not None
                        else {}
                    ),
                )
                if tool is None:
                    raise ValueError("Unknown AnalysisTool: " + name)
                options = {"previous": earlier} if name == "tree-sitter" else {}
                try:
                    if not await tool.check_availability():
                        raise ValueError("Requested AnalysisTool is unavailable")
                    outputs.append(
                        await tool.analyze(
                            args.repository_id,
                            languages=set(args.language) if args.language else None,
                            file_patterns=args.include,
                            skip_patterns=args.exclude,
                            **options,
                        )
                    )
                    if name == "tree-sitter":
                        analysis, reuse = tool.last_analysis, dict(tool.last_reuse)
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
            files = {
                node.properties["path"]
                for output in outputs
                for node in output.nodes
                if "File" in node.labels and "path" in node.properties
            }
            link = (
                commit_link(args.workspace, files, commit_before)
                if files
                else CommitLink(commit_before, False, "no_analyzed_files")
            )
            if link.linked:
                await coordinator.link_commit(
                    args.repository_id, result.snapshot_id, link.commit
                )
            if args.write_artifact is not None:
                args.write_artifact.write_text(analysis.to_json() + "\n")
            report = {**asdict(result), "source_revision": link.to_dict()}
            if earlier is not None:
                report["reuse"] = reuse
            return report
        from .graph_queries import GraphQueryService

        service = GraphQueryService(
            driver,
            database=config["database"],
            observer=query_observer(),
        )
        if args.command == "diff":
            return await service.diff_snapshots(
                args.repository_id,
                base_snapshot_id=args.base,
                head_snapshot_id=args.head,
                changes=args.change,
                limit=args.limit,
                max_output_bytes=args.max_output_bytes,
            )
        if args.command == "snapshots":
            return await service.list_snapshots(
                args.repository_id,
                commit=args.commit,
                limit=args.limit,
                max_output_bytes=args.max_output_bytes,
            )
        if args.command == "explain-edge":
            return await service.explain_edge(
                args.edge_id,
                args.repository_id,
                snapshot_id=args.snapshot,
                max_output_bytes=args.max_output_bytes,
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
        if args.command == "references":
            return await service.get_references(
                args.symbol,
                args.repository_id,
                file_path=args.file,
                after_reference_id=args.after_reference_id,
                **kwargs,
            )
        if args.command in {"impact", "file-impact"}:
            if args.command == "impact":
                method, subject = service.get_impact, args.symbol
            else:
                method, subject = service.get_file_impact, args.file
            return await method(
                subject,
                args.repository_id,
                max_depth=args.max_depth,
                evidence_types=args.evidence,
                **kwargs,
            )
        if args.command in {"members", "owner"}:
            method = (
                service.get_members if args.command == "members" else service.get_owner
            )
            return await method(args.symbol, args.repository_id, **kwargs)
        if args.command in {"implementations", "supertypes"}:
            method = (
                service.get_implementations
                if args.command == "implementations"
                else service.get_supertypes
            )
            return await method(
                args.symbol, args.repository_id, file_path=args.file, **kwargs
            )
        if args.command in {"dependencies", "dependents"}:
            method = (
                service.get_file_dependencies
                if args.command == "dependencies"
                else service.get_file_dependents
            )
            return await method(args.file, args.repository_id, **kwargs)
        if args.command in {
            "callers",
            "callees",
            "bases",
            "subtypes",
            "imports",
            "importers",
        }:
            method = {
                "callers": service.get_function_callers,
                "callees": service.get_function_calls,
                "bases": service.get_base_types,
                "subtypes": service.get_subtypes,
                "imports": service.get_file_imports,
                "importers": service.get_file_importers,
            }[args.command]
            subject = (
                args.file if args.command in {"imports", "importers"} else args.symbol
            )
            return await method(
                subject, args.repository_id, evidence_types=args.evidence, **kwargs
            )
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
