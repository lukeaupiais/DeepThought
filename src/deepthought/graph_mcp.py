"""Port of the original FastMCP tools over the shared graph query service.

The optional MCP transport contains no Cypher construction or extraction logic.
The three original tool names are preserved; queryGraph adapts the original
HTTP Cypher capability. It runs on stdio; raw Cypher is opt-in and
requires a dedicated snapshot database and database-enforced read-only access.
"""

from contextlib import asynccontextmanager
import os
from typing import Annotated

from .graph_cli import connection_settings, query_observer, reference_validator


def create_server():
    from mcp.server.fastmcp import Context, FastMCP
    from pydantic import Field
    from .graph_queries import GraphQueryService
    from .storage.graph.connection import Neo4jConnectionManager

    @asynccontextmanager
    async def lifespan(server):
        settings = connection_settings()
        manager = Neo4jConnectionManager(**settings)
        driver = await manager.connect()
        try:
            yield GraphQueryService(
                driver,
                database=settings["database"],
                observer=query_observer(),
                reference_validator=reference_validator(),
            )
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
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
        definition_id: str | None = None,
        native_symbol: str | None = None,
        source_offset: Annotated[int, Field(ge=0, le=2000000)] = 0,
        max_source_characters: Annotated[int, Field(ge=1, le=8192)] = 1200,
    ) -> dict:
        """Get declaration source, AST ownership and semantic associations from the selected snapshot. Select by file/line, definition_id or native_symbol. Default source pages contain up to 1200 Unicode characters per declaration. Expand using source_next_offset as source_offset with the same definition_id. source_sha256 identifies the complete declaration body. source_scope identifies stored-snapshot code and source_verification reports current-input checks when configured. Follow response and per-source truncation flags. Legacy snapshots may lack stored source."""
        return await ctx.request_context.lifespan_context.get_code_context(
            file_path,
            repository_id,
            snapshot_id=snapshot_id,
            line_number=line_number,
            limit=max_elements,
            definition_id=definition_id,
            native_symbol=native_symbol,
            source_offset=source_offset,
            max_source_characters=max_source_characters,
        )

    @server.tool()
    async def getFileStructure(
        file_path: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
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
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Search the original code_search fulltext index; empty selects declarations."""
        return await ctx.request_context.lifespan_context.search_elements(
            query,
            repository_id,
            snapshot_id=snapshot_id,
            limit=max_elements,
        )

    @server.tool()
    async def findDefinitions(
        symbol: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
        include_evidence: bool = False,
    ) -> dict:
        """Find candidates by source name or native identity. Default items group declarations by symbol and expose native_id directly. Local identities remain document-scoped. Use native_id with findReferences. Set include_evidence for full stored nodes. max_elements is 1-100."""
        return await ctx.request_context.lifespan_context.find_symbol_by_name(
            symbol,
            repository_id,
            snapshot_id=snapshot_id,
            limit=max_elements,
            compact=not include_evidence,
        )

    @server.tool()
    async def findReferences(
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        native_symbol: str | None = None,
        export_name: str | None = None,
        export_file: str | None = None,
        file_path: str | None = None,
        after_reference_id: str = "",
        max_elements: Annotated[
            int,
            Field(
                ge=1,
                le=100,
                description="Maximum occurrences, 1-100. Follow next_cursor when truncated.",
            ),
        ] = 100,
        include_evidence: bool = False,
    ) -> dict:
        """Find references by native_symbol or public module export_name, max_elements 1-100. export_name uses source compiler export/alias identity and returns candidates if ambiguous; export_file qualifies the exporting module. file_path filters reference occurrences. With source evidence enabled, source_verification reports TypeScript symbol/alias resolution, zero compiler diagnostics, scoped source hashes, current source/config/compiler checks and SCIP differences. Verified requires matching inputs before and after the query. Scope hashes are independently checkable against local files. reference_common applies to every items[].reference. Follow next_cursor when truncated. Otherwise freshness is unverified. Local symbols require file_path. include_evidence=true selects the historical raw SCIP view by native_symbol. Compact defaults select compiler-backed source evidence when configured. Static bindings do not establish runtime execution."""
        return await ctx.request_context.lifespan_context.get_references(
            native_symbol,
            repository_id,
            snapshot_id=snapshot_id,
            file_path=file_path,
            after_reference_id=after_reference_id,
            limit=max_elements,
            compact=not include_evidence,
            export_name=export_name,
            export_file=export_file,
        )

    @server.tool()
    async def getFileDependencies(
        file_path: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Get cross-file reference dependencies with located-reference witness identities."""
        return await ctx.request_context.lifespan_context.get_file_dependencies(
            file_path,
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
            max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
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
