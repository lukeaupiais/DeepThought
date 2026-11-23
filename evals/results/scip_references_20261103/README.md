# SCIP through Neo4j — public agent pilot

Completed 3 November 2026. Implementation remains uncommitted on
`codex/scip-neo4j`, based on OSS main `dec4607`. Results are shared before any PR.

## Outcome

The source-bound SCIP integration worked and preserved the correct reference
locations. This pilot found **102.63% more total tokens**, **67.70% more estimated
Standard credits** and **21.11% more actor time** with TreeSitter+SCIP than with
ordinary source/compiler access. It demonstrated no efficiency advantage.

Each condition contains four fresh sessions across two tasks. All twelve
answers were exact. There are 46 distinct expected reference locations, repeated
twice per condition, giving 92 scored occurrences per condition.

| Measure | Source/compiler baseline | Baseline + TreeSitter | Baseline + TreeSitter + SCIP |
| --- | ---: | ---: | ---: |
| Exact reference locations | 92/92 | 92/92 | 92/92 |
| Input tokens | 336,261 | 327,805 | 688,050 |
| Cached input subset | 273,408 | 248,576 | 568,704 |
| Uncached input | 62,853 | 79,229 | 119,346 |
| Output tokens | 8,006 | 8,229 | 9,535 |
| Total tokens | 344,267 | 336,034 | 697,585 |
| Estimated Standard credits | 5.82767 | 6.64014 | 9.77281 |
| Summed actor seconds | 166.02 | 189.96 | 201.07 |
| Sessions with observed graph use | 0/4 | 1/4 | 4/4 |
| Successful Neo4j reads | 0 | 1 | 12 |
| Graph response bytes | 0 | 4,729 | 124,322 |
| Rejected MCP requests | 0 | 0 | 2 |
| Shell commands | 18 | 19 | 16 |
| Shell output bytes | 128,948 | 144,414 | 107,068 |

Cached input is already included in input. Reasoning is already included in
output. Baselines are shared within matched comparison blocks and counted once
in these totals. Three TreeSitter sessions chose source/compiler tools without
querying the graph. Their costs remain included as offered-access observations,
but they cannot establish graph-assisted effectiveness.

| Task | Condition | Total tokens | Estimated credits | Actor seconds |
| --- | --- | ---: | ---: | ---: |
| getNamedType | baseline | 194,812 | 2.96160 | 81.90 |
| getNamedType | TreeSitter | 183,575 | 3.75991 | 107.45 |
| getNamedType | TreeSitter + SCIP | 372,350 | 4.75746 | 107.53 |
| typeFromAST | baseline | 149,455 | 2.86607 | 84.12 |
| typeFromAST | TreeSitter | 152,459 | 2.88023 | 82.51 |
| typeFromAST | TreeSitter + SCIP | 325,235 | 5.01535 | 93.54 |

## Corpus and frozen protocol

GraphQL.js revision `c719a39547635991e621518d056b5e91026bdcda` supplied 176
production TypeScript files with 43,148 lines, plus three project configuration
files. All conditions received the same installed public dependencies and
TypeScript 6.0.3 compiler. Tasks requested complete semantic-reference inventories
for two public exports, including imports, re-exports and recursion. Own
declarations and unrelated same-spelling local symbols were excluded. Expected
locations and their counts were hidden from actors.

Each task used the fixed baseline–TreeSitter–SCIP–SCIP–TreeSitter–baseline order,
with two sessions per condition. Native Codex 0.166.0 ran gpt-6.1-sol with high
effort and a 420-second deadline. Apps, plugins and memories were disabled
equally. Actors had read-only selected sources and dependencies, ordinary search,
Python and Node, with a writable answer directory. Oracle data, SCIP artifacts,
private preparations and credentials for Neo4j remained outside actor mounts.
No private repository or derived private material was used. There were no model
timeouts, incomplete attempts or automatic model retries.

## Tool and evaluator validation

- `ScipAnalysisTool` reuses SDK SCIP ingestion and reference composition, then
  publishes through the existing shared writer. Actual production MCP tools
  queried `GraphQueryService` against database-enforced read-only Neo4j snapshots.
  Raw graph JSON was absent from actor contexts.
- The source-only compiler oracle selected the public export's semantic identity
  and traversed source occurrences without reading SCIP output. SCIP and Neo4j
  both matched all 20 getNamedType and 26 typeFromAST UTF-16 ranges exactly.
- Actual compiler programs accepted complete in-memory whole-project renames.
  Deliberately omitting a true call produced TS2304 for both targets. Sources
  were unchanged. The oracle and indexer share TypeScript 6.0.3, limiting their
  independence. These controls establish a bounded reference claim.
- The grader accepted perfect answers and rejected missing references, wrong
  locations, own declarations as extras, duplicates and malformed facts for
  both cases. Fresh public oracle scripts reproduced the frozen gold.
- All 13 server-observed reads match native-client response hashes. All twelve
  native usage receipts, answer hashes and transcript hashes were checked.
  Only the DeepThought MCP server appeared in completed native tool calls.
- The installed suite ran 220 tests with 219 passing and one opt-in public
  TreeSitter corpus test skipped. The same suite passed offline with 27 expected
  optional-service skips. Full and dependency-free installations passed
  source-hidden, network-disabled acceptance. All 34 installed package files
  matched the evaluated working source. Lint, formatting and JS syntax checks
  passed. Hosted CI has not run for these uncommitted changes.

The graph reports 34,347 resolved, 9,498 unresolved and 288 ambiguous references
among the selected files. Multiple overload declarations retain distinct
candidate targets. Broad graph status remains partial. The source profile hashes
11,793 source, configuration and dependency inputs and passed post-batch
freshness validation. Before/after hashing does not provide an atomic snapshot.

## Preparation, failures and costs

SCIP generation used explicitly provisioned scip-typescript 0.6.0 and Node
24.22.2. The initial index-only attempt took 9.08 seconds and peaked at 556,576
KiB child RSS. The subsequent bound generation recorded 10.03 seconds. Its timer
excluded the initial input capture. Current indexing code starts timing before
that capture. These were separate attempts.

The combined graph contains 54,546 nodes and 91,205 relationships, compared with
1,764 nodes and 1,763 relationships for TreeSitter alone. Full extraction,
publication and readback took 95.96 seconds for the first combined preparation
and 66.19 seconds for an unchanged reimport for the second task. TreeSitter
preparations took 19.66 and 1.94 seconds. Both tasks use the same graph contents
within each condition. The second import is preparation overhead, without a
source change or incremental-refresh speedup claim.

The first combined import exceeded the initial 358.4 MiB transaction-memory cap
in a 1 GiB container. Atomic rollback left zero nodes and zero snapshots.
Transient driver retries remain in the raw log. Both graph cohorts were then
given identical 3 GiB container limits, 1.5 GiB maximum heaps and a 1 GiB
transaction-memory limit before dispatch. The failed stage's elapsed timer was
not retained and remains unknown. This is a material preparation limitation.

Actor time includes source/compiler investigation and graph calls. It excludes
indexing, graph preparation, database provisioning, MCP startup and grading.
Successful SCIP queries consumed 1.33 seconds inside actor intervals. Query
latency alone cannot explain the observed overhead.

Credit estimates use 50 credits per million uncached input tokens, 2.5 per
million cached input tokens and 250 per million output tokens. Actual invoiced
dollars, subscription marginal spend, coordinator/reviewer consumption and
infrastructure charges are unavailable. Provider caching was uncontrolled.
Complete economic break-even is unestablished. [Official Standard token rates](https://learn.chatgpt.com/docs/pricing#token-rates)

## Interpretation and limits

SCIP reduced shell investigation slightly, while adding three graph reads per
session and rich definition/reference responses. Native identities, repeated
overload targets and bounded-response pagination added context. The two rejected
requests exceeded the declared 100-element limit and are included in all totals.
These are observed sources of overhead. Their individual causal contributions
have not been isolated by ablation.

The source/compiler baseline solved both tasks perfectly. This pilot cannot
establish improved accuracy, arbitrary refactor safety, runtime call semantics,
TSX effectiveness, cross-service impact or enterprise-scale architecture
understanding. Two known public tasks with two sessions per condition also
cannot establish statistical generality. No efficiency improvement is accepted
for this increment.

## Evidence

`summary.json` and `runs.csv` contain every observation and the aggregate
arithmetic. Each task directory retains actual answers, graded records, native
usage receipts, MCP evidence, server query receipts and pairwise comparisons.
`audit.json` records client/server matching. Root files retain the frozen plan,
source oracle, rename controls, falsification controls, installed-file hashes and
failed-preparation receipt. Full native transcripts remain local under
`/home/luke/.cache/deepthought-scip`, bound by published hashes.

The [scenario guide](../../scenarios/scip/references/README.md) documents public
provisioning, qualification, graph preparation and native-run commands.
