"""Bind indexer symbols to the syntax declarations that state them.

When one graph revision combines an indexer (SCIP) and the tree-sitter
projection, a SCIP symbol's definition occurrence and a tree-sitter
declaration's name are the same text when they cover the same range of the
same file bytes. Ranges are compared in the indexer's own column encoding, and
files by their sha256, so different bytes never bind.
"""

from .contracts import RelationshipData
from .evidence import EvidenceRule, register_rule

BINDING_RULE = register_rule(
    EvidenceRule(
        "graph.indexer-symbol-declaration-name",
        "resolved",
        "An indexer symbol's definition occurrence covers exactly the name range "
        "of a parsed declaration in a file with the same sha256, compared in the "
        "indexer's column encoding.",
    )
)
_ENCODINGS = {"utf-8": "utf8", "utf-16": "utf16", "utf-32": "utf32"}


def bind_declarations(nodes, repo_url):
    """HAS_AST_DECLARATION edges for combined nodes, and how each symbol bound.

    Returns (relationships, counts); counts has bound, ambiguous and unmatched
    indexer symbols. Indexer symbols carry their file's digest; a parsed
    declaration takes its File node's.
    """
    nodes = list(nodes)
    # Parsed declarations' text comes from bytes with their File's digest.
    digests = {
        n.properties["path"]: n.properties["file_sha256"]
        for n in nodes
        if "File" in n.labels and n.properties.get("file_sha256")
    }
    declarations = {}
    for node in nodes:
        p = node.properties
        digest = digests.get(p.get("file_path"))
        if "Symbol" not in node.labels or p.get("native_id"):
            continue
        for encoding, suffix in _ENCODINGS.items():
            position = p.get("name_range_" + suffix)
            if position:
                key = (p["file_path"], digest, encoding, *position)
                declarations.setdefault(key, []).append(node)
    relationships, counts = [], {"bound": 0, "ambiguous": 0, "unmatched": 0}
    for node in sorted(nodes, key=lambda n: n.properties["qualified_name"]):
        p = node.properties
        if "Symbol" not in node.labels or not p.get("native_id"):
            continue
        if not p.get("file_sha256") or p.get("coordinate_encoding") not in _ENCODINGS:
            counts["unmatched"] += 1
            continue
        key = (
            p.get("file_path"),
            p["file_sha256"],
            p["coordinate_encoding"],
            p.get("start_line"),
            p.get("column"),
            p.get("end_line"),
            p.get("end_column"),
        )
        found = declarations.get(key, [])
        if len(found) != 1:
            counts["ambiguous" if found else "unmatched"] += 1
            continue
        counts["bound"] += 1
        target = found[0]
        relationships.append(
            RelationshipData(
                "HAS_AST_DECLARATION",
                sorted(node.labels),
                {"qualified_name": p["qualified_name"], "repo_url": repo_url},
                sorted(target.labels),
                {
                    "qualified_name": target.properties["qualified_name"],
                    "repo_url": repo_url,
                },
                {
                    "evidence_type": BINDING_RULE.evidence_type,
                    "rule_id": BINDING_RULE.rule_id,
                    "encoding": p["coordinate_encoding"],
                },
            )
        )
    return relationships, counts
