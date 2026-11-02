"""Port of the original FastMCP tools over the shared graph query service.

The optional MCP transport contains no Cypher construction or extraction logic.
The three original tool names are preserved; queryGraph adapts the original
HTTP Cypher capability. It runs on stdio; raw Cypher is opt-in and
requires a dedicated snapshot database and database-enforced read-only access.
"""

from contextlib import asynccontextmanager
import os

from .graph_cli import connection_settings


def create_server():
    from mcp.server.fastmcp import Context, FastMCP
    from .graph_queries import GraphQueryService
    from .storage.graph.connection import Neo4jConnectionManager

    @asynccontextmanager
    async def lifespan(server):
        settings = connection_settings()
        manager = Neo4jConnectionManager(**settings)
        driver = await manager.connect()
        try:
            yield GraphQueryService(driver, database=settings["database"])
        finally:
            await manager.close()

    server = FastMCP("DeepThought", lifespan=lifespan)

    @server.tool()
    async def getCodeContext(
        file_path: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        line_number: int | None = None,
        max_elements: int = 20,
    ) -> dict:
        """Get bounded stored declarations and relationships at a file/line."""
        return await ctx.request_context.lifespan_context.get_code_context(
            file_path,
            repository_id,
            snapshot_id=snapshot_id,
            line_number=line_number,
            limit=max_elements,
        )

    @server.tool()
    async def getFileStructure(
        file_path: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        max_elements: int = 20,
    ) -> dict:
        """Get indexed files/declaration counts for a path prefix."""
        return await ctx.request_context.lifespan_context.get_file_structure(
            file_path,
            repository_id,
            snapshot_id=snapshot_id,
            limit=max_elements,
        )

    @server.tool()
    async def searchCodeElements(
        query: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        max_elements: int = 20,
    ) -> dict:
        """Search the original code_search fulltext index; empty selects declarations."""
        return await ctx.request_context.lifespan_context.search_elements(
            query,
            repository_id,
            snapshot_id=snapshot_id,
            limit=max_elements,
        )

    if os.environ.get("DEEPTHOUGHT_ALLOW_DATABASE_SCOPE_CYPHER") == "1":

        @server.tool()
        async def queryGraph(
            query: str,
            parameters: dict,
            repository_id: str,
            snapshot_id: str,
            ctx: Context,
            max_elements: int = 20,
        ) -> dict:
            """Run bounded read-only Cypher in an explicitly dedicated snapshot database."""
            return await ctx.request_context.lifespan_context.query_cypher(
                query,
                parameters,
                repository_id,
                snapshot_id=snapshot_id,
                allow_database_scope=True,
                limit=max_elements,
            )

    return server


def main():
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
