# Verified Neo4j interface gate: native MCP validation

This validates that an AnalysisTool evaluation uses the real Neo4j interface.
The selected tool was TreeSitterTool on two public DeepAgents Python files
(1,882 lines, 78 independently labelled declarations), pinned to revision
`6a5d93f9ba7391189f65e6b999b724253577085d`. Source files were read in place;
no target clone, build, module execution or private repository was involved.

## Actual path

`prepare_neo4j` ran the registered tool once, imported its exact output using the
production WriteCoordinator/GraphWriter, and verified all 81 nodes and 80 edges,
including labels, endpoints and properties. The actor received query access,
not the graph dump or oracle. The database was then made read-only; an actual
CREATE was rejected by Neo4j.

The evaluated native client connected directly over MCP Streamable HTTP to the
installed `deepthought.graph_mcp.create_server()` implementation. There was no
custom query proxy. MCP `queryGraph` called the same production GraphQueryService
used by CLI and embedded callers, which emitted an evaluator-owned query receipt.
Its database/repository/snapshot/content hash and session/preparation identities
matched the prepared graph. Its response hash matched the native MCP tool result,
and all 78 returned declaration rows independently passed the source oracle.

Both actors used fresh ephemeral `codex-cli 0.166.0` sessions with
`gpt-6.1-sol`, high effort, 300-second limits and read-only native sandboxes inside
identical source-only Bubblewrap namespaces. Ordinary source/AST tools remained
available. The oracle, grading code, database credentials and audit journals were
outside actor access. Host/user Codex settings were not changed.

## Outcomes, failures retained

| Session | Answer | Actual graph access | Total tokens |
|---|---:|---|---:|
| Baseline | 78/78 | None | 70,728 |
| Initial treatment | 78/78 | MCP approval blocked; gate rejected | 101,283 |
| Corrected treatment | 78/78 | Actual MCP Cypher returned 78 rows; gate accepted | 103,285 |

The initial treatment was correctly rejected despite a correct answer: native
MCP required approval while the client was configured never to ask. The repair
approved only the four read-only DeepThought tools in the isolated client's
inline configuration. This uses the documented [per-tool MCP approval setting](https://learn.chatgpt.com/docs/config-file/config-reference).
The MCP schema now also exposes the actual 1–100 result bound. The corrected
agent still requested 200 first; schema validation rejected that request, and it
recovered with 100. Both MCP attempts and their costs are retained.

The unchanged baseline was reused for this **functional repair validation**.
This is not a fresh matched, repeated or statistical performance experiment.
The descriptive corrected comparison uses 46.0% more total tokens; it provides
no efficiency-gain claim. Cache warmth was uncontrolled. All three native sessions
consumed **275,296 tokens**, including the rejected attempt. These are evaluation
actor counts, not engineering/coordinator costs or billed dollars.

The successful DB read returned 10,245 canonical JSON bytes and took 1.046 seconds
inside the agent's elapsed time. These are internal query measurements, not all
MCP framing; do not add query time to actor time twice. Preparation timings are
in summary.json and include extraction plus database materialization/readback.
Database provisioning/resource costs are not inferred or treated as zero.

## Inspectable evidence

- Exact native answers: `baseline.answer.json`, `treatment.answer.json`.
- Native completion/usage hashes, grades, actual MCP call results and server-read
  receipts: the corresponding `*.receipt.json` files.
- Source identity, verified graph binding, accepted gate result, failed attempt
  costs and reuse limitation: `summary.json`.

The final installed harness regraded/recompared these retained actual records
under its receipt checks. It reports `verified_neo4j_access` and
`analysis_tool_evaluation_accepted: true`; `benefit_demonstrated` remains false.
Raw transcripts/preparations/scripts remain local with hashes retained; receipts
are observed events, not signed provider attestations. A privileged evaluator
could fabricate JSON, so independent source/native/server review remains required.
This result establishes interface integration, not architectural intelligence,
large-system coverage, additional language support or improved agent performance.
