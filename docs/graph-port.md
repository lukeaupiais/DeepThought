# Core graph storage and query port

This ports the original DeepThought graph-storage and query responsibilities
into the OSS package. The existing SDK extractor, SCIP ingestion and portable
GraphSnapshot stay unchanged. Neo4j and MCP are optional extras.

## Original implementation mapping

Upstream paths are relative to Code_to_Graph/DeepThought; original MIT attribution
is retained in NOTICE and licenses/deepthought-original-MIT.txt.

| Original | OSS implementation | Retained responsibility |
|---|---|---|
| libs/storage/.../graph/connection.py | deepthought.storage.graph.connection | Neo4j driver lifecycle |
| libs/storage/.../graph/writer.py | deepthought.storage.graph.writer | Tool-independent node/relationship batching and write statistics |
| libs/analysis/.../engine/write_coordinator.py | deepthought.storage.graph.write_coordinator | Repository management and ordered primary/supplemental tool contributions |
| libs/storage/.../graph/schema.py | deepthought.storage.graph.schema | Constraints, indexes and code_search fulltext index |
| libs/analysis/.../engine/data_access/graph_repository.py | deepthought.graph_queries | Calls/callers, imports/exports, hierarchy, paths, statistics and symbol lookup |
| libs/core/.../context/context_service.py | deepthought.graph_queries | File/line context, neighbouring relationships, fulltext search and file navigation |
| applications/unified-server/src/main.py MCP tools | deepthought.graph_mcp | getCodeContext, getFileStructure and searchCodeElements |
| applications/unified-server/src/main.py /api/v1/graph/query | deepthought.graph_queries + graph_cli/graph_mcp | Cypher execution, adapted to CLI and optional queryGraph MCP transport |
| Original analyzer repository/file projection | deepthought.analysis_tools.tree_sitter | Repository -> CONTAINS -> File -> DEFINES -> declaration |

The CLI/MCP adapters call this shared query implementation. They do not implement
another analyzer or a separate query engine. The original hosted authentication,
Kubernetes, chat/model providers, UI, billing, other analyzers and vector/cache
backends are outside this port. It does not merge unrelated pending OSS PRs.

## Deliberate compatibility adaptations

The original orchestrator writes primary and supplemental tools into one shared
repository graph. `WriteCoordinator.write_outputs(outputs, repo_url,
snapshot_id=graph_revision)` retains that mechanism: shared nodes merge labels,
configured arrays, longer documentation, non-null types and later ordinary
properties; relationships resolve against all contributions. Each tool's metadata
and original source snapshot remain recorded. A single output uses
`write_tool_output(tool_name, output, repo_url)`.

Publication is now an atomic graph revision rather than exposing partially
written contributions. All data is scoped by repository/revision and queries
require a complete AnalysisSnapshot publication marker. Reimporting identical
facts is idempotent; conflicting content under the same revision is rejected.
A new revision preserves old facts for explicit selection, so removed definitions
cannot leak into queries for the new revision. Applications choose revision
retention; this port does not silently delete prior graphs. Failed or partial
analysis rejects the publication, and any write error rolls back the transaction.

Neo4j internal IDs and global caches no longer define portable identity. Original
qualified names remain primary identities within repository/revision. Nested
portable JSON properties use reversible encoding; native scalar fields stay
queryable. Complete imports and original metadata are independently readable.
APOC is not required. Merge policies are applied before persistence rather than
through the old APOC-dependent writer path.

The ambiguous alternate legacy MCP query direction is resolved to the original
shared ContextService schema: File -> DEFINES -> declaration. Typed query results
are structured bounded envelopes with scope, capabilities and truncation rather
than unbounded lists. No relationship means only that the selected analysis did
not supply it; declaration-only extraction does not establish absence of calls.

## Provision and use

```sh
pip install '.[python,typescript,neo4j,mcp]'
export DEEPTHOUGHT_NEO4J_URI=bolt://localhost:7687
export DEEPTHOUGHT_NEO4J_USER=neo4j
export DEEPTHOUGHT_NEO4J_PASSWORD='your-local-database-password'
export DEEPTHOUGHT_NEO4J_DATABASE=neo4j

deepthought-graph index /path/to/public/checkout \
  --repository-id public-project --language python --include 'src/*.py'
```

Index initializes the required schema and returns the graph revision and actual
write statistics. Select that revision explicitly for subsequent operations:

```sh
deepthought-graph search --repository-id public-project --snapshot REVISION --query Service
deepthought-graph context --repository-id public-project --snapshot REVISION --file src/service.py --line 20
deepthought-graph structure --repository-id public-project --snapshot REVISION --file src
```

Repeat `--tool NAME` for registered tool contributions and provide `--snapshot`
with the chosen graph revision. This port ships TreeSitterTool; it does not pretend
other original analyzers have already been ported. The embedded Python API can
register tools through the existing registry.

For an agent, configure the stdio command `deepthought-mcp` with the same database
environment. The three original context/search/file tools are available by
default. CLI and MCP results carry actual stored facts and bounded relationships;
they do not dump the complete graph into the agent prompt.

## Cypher scope and access

Arbitrary Cypher cannot be safely made repository-scoped by appending a filter.
Typed queries enforce repository/revision scope. The raw Cypher surface instead
requires explicit opt-in and verifies that the selected database contains only
the selected complete repository/revision. It rejects procedures, administration,
updates and multiple statements, checks the Neo4j EXPLAIN read-only query type,
bounds transaction time/result rows/serialized bytes, and always rolls back.
These restrictions are intentional; it is not unrestricted Neo4j administration.

**Database privileges or a read-only database configuration must enforce read-only
access for untrusted agents. Driver read routing is not access control.** See the
[Neo4j transaction documentation](https://neo4j.com/docs/python-manual/current/transactions/)
and [read-only database configuration](https://neo4j.com/docs/operations-manual/current/database-administration/standard-databases/configuration-parameters/).
Never point an agent at a privileged shared application database.

```sh
deepthought-graph cypher --repository-id public-project --snapshot REVISION \
  --allow-database-scope \
  --query 'MATCH (n:Symbol) WHERE n.name=$name RETURN n.name,n.file_path,n.start_line' \
  --parameters '{"name":"Service"}'
```

For MCP, enable `DEEPTHOUGHT_ALLOW_DATABASE_SCOPE_CYPHER=1` to advertise `queryGraph`.
The same dedicated-database checks and bounds apply. Reserved parameters
`repo_url`, `snapshot_id` and `fetch_limit` are supplied by the query service.

## Verification and validation

Default tests never start/download databases or call models. Actual graph-port
integration tests opt in through the database environment above. They operate on
unique test-owned repositories and clean their records. Use a dedicated empty
test database: raw Cypher tests deliberately require a single selected snapshot.
The CI graph-port job supplies an isolated Neo4j service and tests the installed
wheel, actual imports/readback, rollback/idempotence/shared-tool merges, queries,
CLI journey and MCP stdio transport.

The public evaluation harness supports `prepare(..., tool_access={"command": ...,
"help": ...})` or `deepthought-eval prepare --tool-access descriptor.json`.
This retains extraction output privately and supplies only the query interface
to the treatment agent. The runner must independently verify database contents,
isolate oracle/source access and retain native agent/query receipts. The harness
does not certify the endpoint just because its descriptor is present.

The [completed public-agent validation](../evals/results/neo4j_query_deepagents_20261102/README.md)
retains exact answers, native/query receipts and failures. All four corrected
runs scored 78/78 and both treatment agents successfully queried Neo4j. Aggregate
total tokens increased 19.4%; this port has no demonstrated general efficiency
gain. Tests establish existing-query behaviour on supplied facts; declaration-only
TreeSitter output does not supply caller/callee or distributed-system semantics.

For AnalysisTool agent evaluations, prefer the verified Neo4j preparation in
[the evaluation guide](evaluation.md). A descriptor alone no longer qualifies a
run: actual query receipts must match the verified database/repository/snapshot.
Optional `DEEPTHOUGHT_EVAL_*` audit settings instrument this same CLI/MCP query
implementation without exposing credentials or the oracle to the actor.
