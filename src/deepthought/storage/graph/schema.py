"""Focused port of storage/graph/schema.py: data identity and lookup indexes.

The original label/schema metadata catalogue is not required to persist arbitrary
AnalysisTool records. Composite identity adds explicit immutable snapshot scope.
"""

SCHEMA_QUERIES = (
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
)


async def setup_schema(driver, *, database="neo4j"):
    """Create required constraints/indexes; propagate failures to the caller."""
    async with driver.session(database=database) as session:
        for query in SCHEMA_QUERIES:
            result = await session.run(query)
            await result.consume()
        result = await session.run("CALL db.awaitIndexes(30)")
        await result.consume()
