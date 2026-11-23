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


def reference_tool_access(condition, *, source_evidence=False):
    """Describe actual interface capabilities without task-specific answer hints."""
    if condition == "tree":
        return {
            "command": "mcp:deepthought",
            "help": (
                "TreeSitter declaration snapshot. Use searchCodeElements, "
                "findDefinitions, getCodeContext or queryGraph for source navigation. "
                "Semantic references are unavailable in this condition. Ordinary "
                "source and compiler tools remain available."
            ),
            "available_capabilities": ["definitions"],
        }
    help_text = (
        "Production MCP reads the selected Neo4j snapshot. "
        "findDefinitions(symbol) groups declarations and exposes native_id directly. "
        "findReferences(native_symbol) returns reference occurrences. "
        "reference_common applies to every items[].reference. "
        "Follow next_cursor as after_reference_id when truncated. "
        "include_evidence=true selects the historical raw SCIP view. "
        "getFileDependencies returns reference-witnessed file links. "
    )
    capabilities = ["definitions", "references", "dependencies"]
    if source_evidence:
        help_text += (
            "findReferences also accepts export_name directly, with export_file "
            "to distinguish different module exports sharing a name. "
            "Compact default results use source-references-v1. source_verification describes compiler-symbol and alias reference "
            "evidence across the selected files, compiler diagnostics and differences "
            "from raw SCIP. Verified status requires matching source, configuration, "
            "dependencies and compiler inputs before and after the query. "
            "Missing evidence, stale inputs and ambiguous exports are explicit. "
        )
        capabilities.append("verified_references")
    help_text += (
        "getCodeContext returns stored declaration bodies, AST parent relationships "
        "and exact SCIP-to-AST associations. Select file_path plus definition_id, "
        "native_symbol or line_number. Default source pages are bounded to 1200 "
        "Unicode characters. Follow source_next_offset for expansion with the same "
        "definition_id. Check source_scope, source_verification and truncation. "
        "Static references do not establish runtime calls or refactor safety. "
        "Ordinary source and compiler tools remain available."
    )
    return {
        "command": "mcp:deepthought",
        "help": help_text,
        "available_capabilities": capabilities,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--condition", choices=("tree", "scip"), required=True)
    parser.add_argument("--reference-node", type=Path)
    parser.add_argument("--reference-typescript", type=Path)
    parser.add_argument("--verification-timeout", type=float, default=30)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists, retain previous attempts separately")
    scenario = json.loads(args.scenario.read_text())
    access = reference_tool_access(
        args.condition,
        source_evidence=bool(args.reference_node and args.reference_typescript),
    )
    from deepthought.analysis_tools import tool_registry

    class ConfiguredRegistry:
        def get_tool(self, name, workspace):
            options = (
                {
                    "reference_node": args.reference_node,
                    "reference_typescript": args.reference_typescript,
                }
                if name == "scip"
                else {}
            )
            return tool_registry.get_tool(name, workspace, **options)

    prepared = prepare_neo4j(
        scenario,
        args.workspace,
        settings=connection_settings(),
        additional_tools=("scip",) if args.condition == "scip" else (),
        tool_access=access,
        registry=ConfiguredRegistry(),
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
