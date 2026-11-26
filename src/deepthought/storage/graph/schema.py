"""Focused port of storage/graph/schema.py: data identity and lookup indexes.

The original label/schema metadata catalogue is not required to persist arbitrary
AnalysisTool records. Composite identity adds explicit immutable snapshot scope.
"""

SCHEMA_QUERIES = (
    "CREATE INDEX deepthought_native_reference IF NOT EXISTS FOR (n:Reference) ON (n.repo_url,n.snapshot_id,n.native_id,n.reference_id)",
    "CREATE INDEX deepthought_native_symbol IF NOT EXISTS FOR (n:Symbol) ON (n.repo_url,n.snapshot_id,n.native_id)",
    (
        "CREATE CONSTRAINT deepthought_node_identity IF NOT EXISTS "
        "FOR (n:GraphNode) REQUIRE (n.repo_url,n.snapshot_id,n.qualified_name) IS UNIQUE"
    ),
    (
        "CREATE CONSTRAINT deepthought_snapshot_identity IF NOT EXISTS "
        "FOR (n:AnalysisSnapshot) REQUIRE (n.repo_url,n.snapshot_id) IS UNIQUE"
    ),
    (
        "CREATE INDEX deepthought_file_path IF NOT EXISTS "
        "FOR (n:File) ON (n.repo_url,n.snapshot_id,n.path)"
    ),
    (
        "CREATE FULLTEXT INDEX code_search IF NOT EXISTS "
        "FOR (n:Symbol) ON EACH [n.name,n.display_name,n.qualified_name,n.docstring]"
    ),
    (
        "CREATE INDEX deepthought_symbol_name IF NOT EXISTS "
        "FOR (n:Symbol) ON (n.repo_url,n.snapshot_id,n.name)"
    ),
    (
        "CREATE INDEX deepthought_unresolved_call_name IF NOT EXISTS "
        "FOR (n:UnresolvedCall) ON (n.repo_url,n.snapshot_id,n.name)"
    ),
    (
        "CREATE INDEX deepthought_call_name IF NOT EXISTS "
        "FOR ()-[r:CALLS]-() ON (r.name)"
    ),
)


async def setup_schema(driver, *, database="neo4j"):
    """Create required constraints/indexes; propagate failures to the caller."""
    async with driver.session(database=database) as session:
        for query in SCHEMA_QUERIES:
            result = await session.run(query)
            await result.consume()
        result = await session.run("CALL db.awaitIndexes(30)")
        await result.consume()


# How each relationship type reads as a sentence, for text query results:
# "<source> <verb> <target>", followed by "at line N" when the link has a line.
# One entry per relationship type the engine writes; a test checks that every
# type in an analyzed graph has one. Keep the verb in the reader's words.
RELATIONSHIP_SENTENCES = {
    "CALLS": "calls",
    "DISPATCHES_TO": "can dispatch to",
    "HAS_UNRESOLVED_CALL": "makes",
    "IMPORTS": "imports",
    "IMPORTS_PACKAGE": "imports the external package",
    "INHERITS_FROM": "inherits from",
    "IMPLEMENTS": "implements",
    "REFERENCES": "refers to",
    "CONTAINS": "contains",
    "CONTAINS_REFERENCE": "contains the reference",
    "DEFINES": "defines",
    "DECLARES": "declares",
    "HAS_AST_DECLARATION": "has the syntax-tree declaration",
    "DEPENDS_ON": "depends on",
    "PART_OF": "is part of",
    "REQUIRES": "requires",
    "DEPLOYS": "deploys",
    "REQUESTS": "sends a request to",
    "HANDLES": "handles",
    "PUBLISHES": "publishes to",
    "CONSUMES": "consumes from",
    "USES_CONTRACT_OF": "uses the contract of",
    "READS": "reads",
    "WRITES": "writes",
    "SELECTS": "selects from",
    "MAPS_TO": "maps to",
    "REFERENCES_TABLE": "references the table",
    "CARRIES_MESSAGE": "is a message type of",
    "CARRIES": "carries the payload type",
    "HAS_COLUMN": "has the column",
    "USES_FIXTURE": "uses the pytest fixture",
    "USES_DEPENDENCY": "uses the FastAPI dependency",
}
