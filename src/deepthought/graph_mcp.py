"""Port of the original FastMCP tools over the shared graph query service.

The optional MCP transport contains no Cypher construction or extraction logic.
The three original tool names are preserved; queryGraph adapts the original
HTTP Cypher capability. The call, base type, member/owner, impact, import, reference, implementation,
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
        definition_id: str | None = None,
        native_symbol: str | None = None,
        source_offset: Annotated[int, Field(ge=0, le=2_000_000)] = 0,
        max_source_characters: Annotated[int, Field(ge=0, le=8192)] = 1200,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Get stored declarations at a file/line with their source and relationships.

        Each declaration's source is paged: max_source_characters code points
        from source_offset, out of source_characters.
        """
        return await ctx.request_context.lifespan_context.get_code_context(
            file_path,
            repository_id,
            snapshot_id=snapshot_id,
            line_number=line_number,
            definition_id=definition_id,
            native_symbol=native_symbol,
            source_offset=source_offset,
            max_source_characters=max_source_characters,
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
        """Search declarations by name; empty selects declarations.

        A query that is one identifier also finds the members it names
        (``Flags`` finds ``Command.Flags``), listed first with
        ``name_match``; other hits come from the code_search fulltext index.
        """
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
    Roles = list[Literal["test", "example"]] | None
    ContractKind = (
        Literal[
            "DbColumn",
            "DbTable",
            "GraphqlField",
            "GrpcMethod",
            "HttpRoute",
            "MessageDestination",
        ]
        | None
    )

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
    async def getImpact(
        qualified_name: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        max_depth: Annotated[int, Field(ge=1, le=5)] = 3,
        evidence_types: Evidence = None,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0, le=100_000)] = 0,
        exclude_roles: Roles = None,
    ) -> dict:
        """Get what depends on a declaration and its members, transitively.

        Follows CALLS, INHERITS_FROM, IMPLEMENTS, DISPATCHES_TO (Go interface
        methods to implementations) and REQUESTS backwards and
        HANDLES forwards, so callers of a changed HTTP handler's route in
        other services count; each dependent has its depth, its path's edges
        and the path's weakest evidence. A truncated list continues with
        ``offset`` set to its ``next_offset``.

        Each dependent's ``impact`` is ``direct`` (depth 1: it uses the
        declaration itself, so a signature change may need an edit there) or
        ``transitive`` (re-test it); ``source_roles`` marks tests and
        examples, and ``exclude_roles`` leaves those roles out before paging.
        """
        return await ctx.request_context.lifespan_context.get_impact(
            qualified_name,
            repository_id,
            snapshot_id=snapshot_id,
            max_depth=max_depth,
            evidence_types=evidence_types,
            limit=max_elements,
            offset=offset,
            exclude_roles=exclude_roles,
        )

    @server.tool()
    async def searchContracts(
        query: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        kind: ContractKind = None,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Find contracts by name, path or qualified name (empty lists all).

        Contracts are HTTP routes (``GET /api/x``), gRPC methods, GraphQL
        fields, message destinations and database tables; each comes with
        its provider and client counts. Pass one's ``qualified_name`` to
        getContractImpact.
        """
        return await ctx.request_context.lifespan_context.search_contracts(
            query,
            repository_id,
            snapshot_id=snapshot_id,
            kind=kind,
            limit=max_elements,
        )

    @server.tool()
    async def getContractImpact(
        qualified_name: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        max_depth: Annotated[int, Field(ge=1, le=5)] = 3,
        evidence_types: Evidence = None,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0, le=100_000)] = 0,
        exclude_roles: Roles = None,
    ) -> dict:
        """Get what a contract change affects: its providers and, transitively, its clients and their callers.

        ``providers`` lists the handlers, publishers, writers and mappings of
        the contract (searchContracts finds it). Dependents are the code that
        requests, consumes or reads it, then their callers, also across
        services, as for getImpact.
        """
        return await ctx.request_context.lifespan_context.get_contract_impact(
            qualified_name,
            repository_id,
            snapshot_id=snapshot_id,
            max_depth=max_depth,
            evidence_types=evidence_types,
            limit=max_elements,
            offset=offset,
            exclude_roles=exclude_roles,
        )

    @server.tool()
    async def getFileImpact(
        file_path: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        max_depth: Annotated[int, Field(ge=1, le=5)] = 3,
        evidence_types: Evidence = None,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0, le=100_000)] = 0,
        exclude_roles: Roles = None,
    ) -> dict:
        """Get files that depend on a file, transitively, over IMPORTS and DEPENDS_ON.

        A truncated list continues with ``offset`` set to its ``next_offset``.
        Each file's ``impact`` is ``direct`` at depth 1, else ``transitive``;
        ``exclude_roles`` leaves out tests or examples (``source_roles``).
        """
        return await ctx.request_context.lifespan_context.get_file_impact(
            file_path,
            repository_id,
            snapshot_id=snapshot_id,
            max_depth=max_depth,
            evidence_types=evidence_types,
            limit=max_elements,
            offset=offset,
            exclude_roles=exclude_roles,
        )

    @server.tool()
    async def getMembers(
        qualified_name: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Get declarations directly nested in a declaration (CONTAINS), with evidence."""
        return await ctx.request_context.lifespan_context.get_members(
            qualified_name,
            repository_id,
            snapshot_id=snapshot_id,
            limit=max_elements,
        )

    @server.tool()
    async def getOwner(
        qualified_name: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Get the declaration a declaration is directly nested in, with evidence."""
        return await ctx.request_context.lifespan_context.get_owner(
            qualified_name,
            repository_id,
            snapshot_id=snapshot_id,
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
    async def listComponents(
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        kind: str | None = None,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """List components build manifests declare (Python projects, npm packages, Go modules, Cargo packages, Maven modules, Compose services) with file counts."""
        return await ctx.request_context.lifespan_context.list_components(
            repository_id, snapshot_id=snapshot_id, kind=kind, limit=max_elements
        )

    @server.tool()
    async def getFileComponents(
        file_path: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Get the nearest enclosing component of each kind for a file, with evidence and rule."""
        return await ctx.request_context.lifespan_context.get_file_components(
            file_path, repository_id, snapshot_id=snapshot_id, limit=max_elements
        )

    @server.tool()
    async def getComponentDependencies(
        qualified_name: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        incoming: bool = False,
        evidence_types: Evidence = None,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Get components a component's manifest requires (or, incoming, that require it), with the declaration, evidence and rule."""
        return await ctx.request_context.lifespan_context.get_component_dependencies(
            qualified_name,
            repository_id,
            snapshot_id=snapshot_id,
            incoming=incoming,
            evidence_types=evidence_types,
            limit=max_elements,
        )

    @server.tool()
    async def getComponentImpact(
        qualified_name: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        max_depth: Annotated[int, Field(ge=1, le=5)] = 3,
        evidence_types: Evidence = None,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0, le=100_000)] = 0,
    ) -> dict:
        """Get components that transitively require, deploy or select a component, each with its path and weakest evidence.

        A truncated list continues with ``offset`` set to its ``next_offset``.
        """
        return await ctx.request_context.lifespan_context.get_component_impact(
            qualified_name,
            repository_id,
            snapshot_id=snapshot_id,
            max_depth=max_depth,
            evidence_types=evidence_types,
            limit=max_elements,
            offset=offset,
        )

    @server.tool()
    async def getComponentFiles(
        qualified_name: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        after_path: str = "",
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Page through the files of a component, by path."""
        return await ctx.request_context.lifespan_context.get_component_files(
            qualified_name,
            repository_id,
            snapshot_id=snapshot_id,
            after_path=after_path,
            limit=max_elements,
        )

    @server.tool()
    async def getCallSites(
        name: str,
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        file_path: str | None = None,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """List the call sites of a name (the callee's last name, `param` for `c.req.param()`) and count them: linked ones with their target and rule, unresolved and ambiguous ones with the reason no rule linked them."""
        return await ctx.request_context.lifespan_context.get_call_sites(
            name,
            repository_id,
            snapshot_id=snapshot_id,
            file_path=file_path,
            limit=max_elements,
        )

    @server.tool()
    async def getSharedTables(
        repository_id: str,
        snapshot_id: str,
        ctx: Context,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """List database tables that code in more than one package declares, maps, reads or writes, with each package's roles (shared-database coupling)."""
        return await ctx.request_context.lifespan_context.get_shared_tables(
            repository_id,
            snapshot_id=snapshot_id,
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
