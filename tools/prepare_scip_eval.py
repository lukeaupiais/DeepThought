"""Prepare one frozen public reference case on a dedicated Neo4j database.

Provision the index separately. Credentials use DEEPTHOUGHT_NEO4J_* environment
settings. Run once per condition, then enforce database read-only before actors.
"""

import argparse
import json
from pathlib import Path

import scip_eval_grader as scip_eval_grader  # register the trusted scenario grader
from deepthought.evaluation.neo4j import prepare_neo4j
from deepthought.graph_cli import connection_settings


# MCP tools each condition's actors may call; all exist in deepthought.graph_mcp.
TREE_TOOLS = ("searchCodeElements", "getCodeContext", "getFileStructure", "queryGraph")
SCIP_TOOLS = TREE_TOOLS + ("findReferences", "getFileDependencies")


def reference_tool_access(condition):
    """Describe actual interface capabilities without task-specific answer hints."""
    if condition == "tree":
        return {
            "command": "mcp:deepthought",
            "help": (
                "TreeSitter declaration snapshot. Use searchCodeElements, "
                "getCodeContext, getFileStructure or queryGraph for source "
                "navigation. Semantic references are unavailable in this condition. "
                "Ordinary source and compiler tools remain available."
            ),
            "available_capabilities": ["definitions"],
            "tools": list(TREE_TOOLS),
        }
    return {
        "command": "mcp:deepthought",
        "help": (
            "Production MCP reads the selected Neo4j snapshot. searchCodeElements "
            "finds declarations and their native_id; getFileStructure lists a "
            "file's declarations. findReferences(native_symbol) "
            "returns SCIP reference occurrences; follow a truncated list with the "
            "last reference_id as after_reference_id. getFileDependencies returns "
            "reference-witnessed file links. getCodeContext returns stored "
            "declaration source by file_path with line_number, definition_id or "
            "native_symbol, paged by source_offset and max_source_characters "
            "(Unicode code points). Static references do not establish runtime "
            "calls or refactor safety. Ordinary source and compiler tools remain "
            "available."
        ),
        "available_capabilities": ["definitions", "references", "dependencies"],
        "tools": list(SCIP_TOOLS),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--condition", choices=("tree", "scip"), required=True)
    parser.add_argument("--verification-timeout", type=float, default=30)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists, retain previous attempts separately")
    scenario = json.loads(args.scenario.read_text())
    access = reference_tool_access(args.condition)
    prepared = prepare_neo4j(
        scenario,
        args.workspace,
        settings=connection_settings(),
        additional_tools=("scip",) if args.condition == "scip" else (),
        tool_access=access,
        verification_timeout=args.verification_timeout,
    )
    with args.output.open("x") as stream:
        json.dump(prepared, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(
        json.dumps(
            {"output": str(args.output), "verified": prepared["neo4j"]["verified"]}
        )
    )


if __name__ == "__main__":
    main()
