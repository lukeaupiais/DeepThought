# Core graph port: completed agent integration validation

Four fresh native agent sessions completed against the actual ported Neo4j query
service using pinned public DeepAgents code. Both treatment agents successfully
called the graph service. All four answers scored 78/78 with no extra claims,
wrong citations, duplicates or unsupported relationships. The original source
checkout was used in place; no target repository was cloned, copied or built.
No private source or evaluation data was used.

This validates the extraction -> persistence -> query -> agent -> grading chain.
It does not demonstrate a general performance or architectural-understanding gain.

## What was ported and exercised

The installed `deepthought-graph index` ran TreeSitterTool and the ported
WriteCoordinator/GraphWriter. Actual database readback independently matched all
81 graph nodes and 80 CONTAINS/DEFINES relationships, including labels, properties,
identities and endpoints. These represent 78 declarations, two files and their
repository. The database was then restarted with
`server.databases.default_to_read_only=true`; a direct CREATE was rejected by
Neo4j. The complete snapshot was independently checked again in read-only mode.

Separate installed-package tests exercise multi-tool merges, rollback,
idempotence, repository/revision isolation, the retained query methods, the CLI
journey and actual MCP stdio discovery/calls. Those are verification tests;
these four native sessions are the real-agent integration validation.

## Prospectively declared corrected profile

- Public `langchain-ai/deepagents` revision
  `6a5d93f9ba7391189f65e6b999b724253577085d`; selected file hashes and independent
  oracle remain in [the scenario](../../scenarios/tree_sitter/deepagents_backends.json).
- Two Python backend files, 1,882 source lines, 78 declarations. Known calibration
  labels were independently cross-checked with Python AST.
- `codex-cli 0.166.0`, `gpt-6.1-sol`, high reasoning effort, fresh ephemeral sessions,
  300-second budget each, no automatic retries.
- Fixed order: treatment, baseline, baseline, treatment (two matched pairs).
- Same task, explicit kind/citation rubric and ordinary source/AST/search tools.
  Baseline received no graph interface. Treatment received only a command/help
  descriptor; the actual graph and oracle stayed outside its context/namespace.
- The actor command `/work/graph OPERATION JSON_ARGUMENTS` called a local evaluation
  bridge over the actual GraphQueryService. Database credentials remained outside
  the actor. The bridge exposed search, context, structure and read-only Cypher for
  the selected public database; it did not answer from the oracle or cached labels.
- Both arms used native `--sandbox danger-full-access` **inside** the same outer
  Bubblewrap namespace. The outer namespace enforced read-only selected source
  and hid implementation, oracle, other answers and database credentials. This
  configuration permitted local query sockets while preserving source isolation.
  Source/AST tools remained available. An HTTP request from the outer actor
  namespace succeeded; private implementation paths were independently absent.
- The integration profile explicitly required treatment to query the graph at least
  once. Successful answers alone were insufficient: a matching successful server
  event was also required. This is required-use integration, not voluntary tool
  adoption or evidence of architectural reasoning.
- Every accepted run required process exit zero, final parseable JSON and exactly
  one native completion/usage receipt. Independent review checked those artifacts,
  source labels, graph identity, actual command/server responses and arithmetic.

The generic harness remains provider-neutral. It prepares query-access contexts
using `prepare(..., tool_access={command, help, ...})` or CLI `prepare --tool-access`.
The local runner/bridge were caller provisioning, not a new engine or an automatic
provider launcher in the package. Driver/namespace/bridge hashes are retained in
`summary.json`; the full local logs and scripts were available for independent
review. Hashes alone do not authenticate an unpublished log or provider receipt.

## Actual outcomes

| Run | Accuracy | Input | Cached input | Output | Total tokens | Seconds | Successful graph calls |
|---|---:|---:|---:|---:|---:|---:|---:|
| Pair 1 treatment | 78/78 | 106,540 | 82,048 | 3,250 | 109,790 | 39.80 | 1 |
| Pair 1 baseline | 78/78 | 68,881 | 38,784 | 2,961 | 71,842 | 34.13 | 0 |
| Pair 2 baseline | 78/78 | 68,931 | 48,640 | 2,953 | 71,884 | 34.99 | 0 |
| Pair 2 treatment | 78/78 | 58,588 | 45,312 | 3,206 | 61,794 | 38.23 | 2 |

Pair 1 treatment first requested a limit of 200, which was rejected, then issued
valid parameterized Cypher returning all 78 projected file/symbol/kind/line rows.
Pair 2 treatment queried the two file-structure counts (44 and 34 declarations);
it did not retrieve declaration rows through Cypher. Both treatment agents also
used source and Python AST. Actual access succeeded, but the graph was not their
exclusive source of evidence.

Across both pairs, total tokens were **143,726 baseline versus 171,584 treatment**
(**19.4% more**). One pair regressed 52.8%; the other used 14.0% fewer tokens. Two
known-case repetitions with different agent strategies do not establish a stable
effect. Uncached input plus output was 56,302 versus 44,224 (**21.5% less**), but
cache warmth was uncontrolled and this is not a dollar-cost estimate. Cached input
is already part of input; reasoning output is already part of output.

Combined agent wall time was 69.12 versus 78.03 seconds (**12.9% more**). The query
bridge observed 11,129 response bytes and 0.539 seconds across all four treatment
requests, including the rejected request; query time is already inside actor wall
time and must not be added twice. Component preparation timings are recorded:
source extraction 0.114 seconds, writer 4.100 seconds, and a separate framework
preparation measurement. Schema/driver startup and database provisioning were not
timed, so these are not complete cold-start costs. Upstream extraction used zero
model tokens. Database/server CPU, memory and monetary cost are not inferred.

## Failed pilot retained

An earlier four-run pilot produced correct final answers but its inner native
read-only sandbox blocked the graph command's socket requests (EPERM). Both
treatment agents fell back to AST. Those runs **failed graph integration** and
are excluded from the corrected comparison. Their known native token counts are
preserved individually in `summary.json`: **279,056 tokens**, not zero or unknown.
The corrected four runs consumed 315,310 tokens; all eight native evaluation
sessions consumed **594,366 tokens**. This excludes engineering/coordinator-agent
usage. No cross-profile comparison is used to claim a gain.

## Evidence and limits

Each `*.answer.json` preserves exact native final-answer bytes. Each receipt
contains the native terminal event, process receipt, independently checked grade,
actor-context identity, raw-log/answer hashes, acceptance gate and actual agent
query request/result events. Public exports contain declaration facts and query
results, not private source, oracle files or raw target source bodies. Full raw
local logs/preparations are retained separately. Native events are not signed
provider billing attestations.

This pack establishes a working core port on public declaration facts and actual
agent query access. It does not establish call extraction, distributed flows,
refactoring, huge-system understanding, unseen-task performance, multi-language
agent benefit or enterprise readiness. The existing declaration-only producer
must not acquire those claims merely because richer query methods were ported.
