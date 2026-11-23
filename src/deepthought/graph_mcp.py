"""Port of the original FastMCP tools over the shared graph query service.

The optional MCP transport contains no Cypher construction or extraction logic.
The three original tool names are preserved; queryGraph adapts the original
HTTP Cypher capability. The call, base type, import, reference, implementation,
dependency and explainEdge
tools expose the ported repository queries with each edge's evidence type and rule. It runs on stdio; raw Cypher is opt-in and
requires a dedicated snapshot database and database-enforced read-only access.
"""

from contextlib import asynccontextmanager
import os
from typing import Annotated, Literal

from .graph_cli import connection_settings, query_observer


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
    async def listSnapshots(
        repository_id: str,
        ctx: Context,
        commit: str | None = None,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """List complete snapshots of a repository and the commits each came from."""
        return await ctx.request_context.lifespan_context.list_snapshots(
            repository_id, commit=commit, limit=max_elements
        )

    @server.tool()
    async def diffSnapshots(
        repository_id: str,
        base_snapshot_id: str,
        head_snapshot_id: str,
        ctx: Context,
        changes: list[
            Literal[
                "nodes.added",
                "nodes.removed",
                "nodes.changed",
                "edges.added",
                "edges.removed",
            ]
        ]
        | None = None,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Count and list nodes and edges added, removed or changed between snapshots."""
        return await ctx.request_context.lifespan_context.diff_snapshots(
            repository_id,
            base_snapshot_id=base_snapshot_id,
            head_snapshot_id=head_snapshot_id,
            changes=changes,
            limit=max_elements,
        )

    Evidence = list[Literal["resolved", "pattern_matched", "name_matched"]] | None

    @server.tool()
    async def getFunctionCallers(
        qualified_name: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        evidence_types: Evidence = None,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Get stored CALLS edges into a declaration, each with evidence and rule."""
        return await ctx.request_context.lifespan_context.get_function_callers(
            qualified_name,
            repository_id,
            snapshot_id=snapshot_id,
            evidence_types=evidence_types,
            limit=max_elements,
        )

    @server.tool()
    async def getFunctionCalls(
        qualified_name: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        evidence_types: Evidence = None,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Get stored CALLS edges out of a declaration, each with evidence and rule."""
        return await ctx.request_context.lifespan_context.get_function_calls(
            qualified_name,
            repository_id,
            snapshot_id=snapshot_id,
            evidence_types=evidence_types,
            limit=max_elements,
        )

    @server.tool()
    async def getBaseTypes(
        qualified_name: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        evidence_types: Evidence = None,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Get the bases a class or interface names (INHERITS_FROM), with evidence."""
        return await ctx.request_context.lifespan_context.get_base_types(
            qualified_name,
            repository_id,
            snapshot_id=snapshot_id,
            evidence_types=evidence_types,
            limit=max_elements,
        )

    @server.tool()
    async def getSubtypes(
        qualified_name: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        evidence_types: Evidence = None,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Get classes and interfaces naming this one as a base, with evidence."""
        return await ctx.request_context.lifespan_context.get_subtypes(
            qualified_name,
            repository_id,
            snapshot_id=snapshot_id,
            evidence_types=evidence_types,
            limit=max_elements,
        )

    @server.tool()
    async def getFileImports(
        file_path: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        evidence_types: Evidence = None,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Get files this file's imports were linked to, with evidence and rule."""
        return await ctx.request_context.lifespan_context.get_file_imports(
            file_path,
            repository_id,
            snapshot_id=snapshot_id,
            evidence_types=evidence_types,
            limit=max_elements,
        )

    @server.tool()
    async def getFileImporters(
        file_path: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        evidence_types: Evidence = None,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Get files whose imports were linked to this file, with evidence and rule."""
        return await ctx.request_context.lifespan_context.get_file_importers(
            file_path,
            repository_id,
            snapshot_id=snapshot_id,
            evidence_types=evidence_types,
            limit=max_elements,
        )

    @server.tool()
    async def findReferences(
        native_symbol: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        file_path: str | None = None,
        after_reference_id: str = "",
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """List SCIP occurrences of a native symbol and the definitions they name.

        Get native_symbol from searchCodeElements. Local symbols need file_path.
        Continue a truncated list with the last reference_id.
        """
        return await ctx.request_context.lifespan_context.get_references(
            native_symbol,
            repository_id,
            snapshot_id=snapshot_id,
            file_path=file_path,
            after_reference_id=after_reference_id,
            limit=max_elements,
        )

    @server.tool()
    async def findImplementations(
        native_symbol: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        file_path: str | None = None,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Find symbols implementing, extending or overriding a SCIP symbol."""
        return await ctx.request_context.lifespan_context.get_implementations(
            native_symbol,
            repository_id,
            snapshot_id=snapshot_id,
            file_path=file_path,
            limit=max_elements,
        )

    @server.tool()
    async def getSupertypes(
        native_symbol: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        file_path: str | None = None,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Get the symbols a SCIP symbol implements, extends or overrides."""
        return await ctx.request_context.lifespan_context.get_supertypes(
            native_symbol,
            repository_id,
            snapshot_id=snapshot_id,
            file_path=file_path,
            limit=max_elements,
        )

    @server.tool()
    async def getFileDependencies(
        file_path: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Get files declaring symbols this file references (SCIP, resolved)."""
        return await ctx.request_context.lifespan_context.get_file_dependencies(
            file_path, repository_id, snapshot_id=snapshot_id, limit=max_elements
        )

    @server.tool()
    async def getFileDependents(
        file_path: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Get files that reference symbols declared in this file (SCIP, resolved)."""
        return await ctx.request_context.lifespan_context.get_file_dependents(
            file_path, repository_id, snapshot_id=snapshot_id, limit=max_elements
        )

    @server.tool()
    async def explainEdge(
        edge_id: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
    ) -> dict:
        """Explain one stored edge: endpoints, location, evidence type and rule."""
        return await ctx.request_context.lifespan_context.explain_edge(
            edge_id,
            repository_id,
            snapshot_id=snapshot_id,
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
