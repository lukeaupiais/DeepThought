# SCIP response and binding refinement

Completed 3 November 2026. Changes remain uncommitted on `codex/scip-neo4j`,
based on OSS main `dec4607`. This report separates integration checks, agent
efficiency and a failed independent semantic control.

## Result

Compact responses with native-symbol binding used **45.13% fewer total tokens**,
**30.88% fewer estimated Standard credits** and **12.72% less actor time** than
the freshly rerun expanded interface. Against the source/compiler baseline they
still used **27.91% more tokens**, **13.75% more estimated credits** and **8.21%
more actor time**. A baseline efficiency advantage remains unproven.

Each condition contains four fresh sessions. All sixteen requested answers were
exact, giving 92/92 scored reference occurrences per condition. These are 46
distinct source locations repeated twice.

| Measure | Source/compiler | Expanded v1 | Compact v1 | Compact v2 |
| --- | ---: | ---: | ---: | ---: |
| Correct requested occurrences | 92/92 | 92/92 | 92/92 | 92/92 |
| Input tokens | 332,419 | 783,344 | 451,776 | 426,765 |
| Cached input subset | 255,232 | 652,672 | 356,480 | 339,072 |
| Uncached input | 77,187 | 130,672 | 95,296 | 87,693 |
| Output tokens | 7,780 | 9,746 | 7,654 | 8,384 |
| Total tokens | 340,199 | 793,090 | 459,430 | 435,149 |
| Estimated Standard credits | 6.44243 | 10.60178 | 7.56950 | 7.32833 |
| Summed actor seconds | 159.52 | 197.76 | 150.56 | 172.61 |
| Sessions with observed graph use | 0/4 | 4/4 | 4/4 | 4/4 |
| Successful graph reads | 0 | 12 | 8 | 8 |
| Canonical graph response bytes | 0 | 124,322 | 57,115 | 49,734 |
| Rejected MCP requests | 0 | 3 | 0 | 0 |
| Shell commands | 18 | 19 | 17 | 17 |
| Shell output bytes | 115,360 | 129,385 | 106,503 | 119,788 |
| Independent compiler reference scans | 4 | 4 | 4 | 4 |

Input already includes cached input. Output already includes reasoning output.
Four fresh baselines are shared across the three treatment comparisons within
their blocks and counted once in aggregates. Standard credit estimates use the
frozen rate card in `cost-model.json`. Invoiced dollars, marginal subscription
spend, coordinator/reviewer usage and infrastructure charges remain unknown.

## What changed

- The shared query service groups definition declarations by native identity,
  scopes local identities by document, and projects useful declaration fields.
  Expanded stored-node evidence remains available on request.
- Reference responses factor common native identity, declaration targets, roles,
  coordinate encoding and resolution into `reference_common`. Every original
  reference property can be reconstructed. Budgeting occurs on the formatted
  response before publication and evidence hashing. Explicit cursors continue
  bounded pages. A first occurrence that cannot fit fails explicitly.
- MCP and CLI use compact defaults. Direct service callers retain expanded
  defaults. MCP reference pages default to at most 100 occurrences, with clearer
  limit documentation. Compact responses identify their format version.
- Composer contract 2 distinguishes native binding from declaration multiplicity.
  All declarations remain candidates. A file dependency requires a unique
  destination repository and file. Multiple provider scopes stay ambiguous.
  The SDK default and existing serialized contract 1 remain supported.
- Coverage reports indexed file count, binding semantics and source-check time.
  Historical queries retain explicit unverified live-workspace freshness.

The real Neo4j preflight reproduced all 20 getNamedType and 26 typeFromAST source
ranges in one compact page per symbol. Canonical reference responses measured
5,791 and 7,237 bytes under v2. Definition responses measured 6,966 and 4,873
bytes. All 44 cross-file reference occurrences now contribute dependency
witnesses, compared with zero under the legacy declaration-count rule. The two
same-file recursive references appropriately create no cross-file dependency.

## Actual agent behavior and attribution

Every graph agent used the actual production MCP service and Neo4j query service.
All 28 server reads match native-client response hashes. Actual answers, native
terminal usage, answer hashes and transcript hashes were audited for all sixteen
sessions. Private graph dumps and gold answers were excluded from actor mounts.

All sixteen agents still reconstructed references using the TypeScript compiler.
Smaller responses removed overhead without eliminating that repeated semantic
investigation. Compact v2 still consumed 94,950 more tokens than the baseline.

Compact v1 used 42.07% fewer tokens than expanded v1 on the same historical graph.
Compact v2 used a further 5.29% fewer tokens than compact v1, but these small
samples do not isolate that difference causally. One compact-v1 agent explicitly
requested expanded definition evidence, adding 7,377 canonical response bytes.
All three rejected expanded-interface requests exceeded the declared 100-element
limit. Their costs remain included. Provider caching and actor choices varied.

Per-tool text payload receipts are now retained in `measurement.json`. The native
CLI emits session-terminal usage only. These traces do not support exact
request-level token allocation. Actor wall times include shared-workstation and
provider variation. Local tests and correctness controls also ran during the
batch. No dedicated-hardware latency or statistical generalization claim is made.

## Independent semantic control that failed

A separate owned alias/re-export control compiled successfully under TypeScript
6.0.3. Source-symbol traversal found eight semantic references to an overloaded
export. scip-typescript 0.6.0 assigned seven to that native identity and represented
the imported `outward` name in `c.ts` as undefined `local 2`. The raw index contains
no relationship connecting that occurrence to the exported function.

The adapter faithfully retains that unresolved occurrence and partial graph
status. The independent compiler-reference completeness check nevertheless
**failed at 7/8**. A repeat with analysis outputs outside the source root confirmed
unchanged source binding and the same gap. This limitation predates the response
refinement. It prevents claiming complete alias-transitive semantic coverage.
No binding was invented from spelling or proximity, and the performance questions
and expected answers were unchanged after this finding.

`alias-control` retains the failed comparison and source oracle. The actual
producer index, bound source fixture and regression test are under
`tests/fixtures/scip_alias_reexport`. That regression verifies faithful uncertainty
preservation. It does not turn the failed semantic qualification into a pass.
The small control is excluded from all agent-performance measurements.

## Protocol and preparation

The pinned public corpus and two tasks match the earlier pilot. GraphQL.js
revision `c719a39547635991e621518d056b5e91026bdcda` supplies 176 production
TypeScript files with 43,148 lines, three configuration inputs, installed public
dependencies and TypeScript 6.0.3. ERP and all private derived material are excluded.

Each task used baseline–expanded-v1–compact-v1–compact-v2–compact-v2–compact-v1–
expanded-v1–baseline order. Native Codex 0.166.0 ran gpt-6.1-sol with high effort,
a 420-second deadline, fresh sessions and no automatic model retries. Ordinary
source/compiler access and the original task/prompt remained available equally.
Apps, plugins and memories were disabled. No graph use was forced and no
independent checking was suppressed. All sixteen sessions completed.

Expanded v1 uses the archived original installed server. Compact v1 uses the
current server against exactly the same historical graph and actor context.
Compact v2 uses the current server and a separately prepared v2 graph. The shared
historical context mentions native identity in node properties, while compact
MCP documentation describes its group-level location. That possible migration
friction is retained and declared in the frozen plan. Original artifacts and
their package-file hashes remain available locally for audit.

The source-bound SCIP index was reused and source freshness passed before
dispatch. New v2 extraction, import and complete graph readback took 116.39 and
86.73 seconds for the two cases. The second case reimports unchanged graph
contents. These are preparation costs, without an incremental-refresh claim.

The first read-only restart failed because indexes required rebuilding. The
failure and initial unsuccessful preflight are retained locally. Recovery waited
for every index to become online, allowed a 120-second graceful stop, then
re-established enforced read-only operation. Recovery took 61.86 seconds.
Node counts and the publication hash matched after recovery. The exact initial
failed-startup duration remains unknown. All recovery happened before model
dispatch and affected only the owned v2 service.

Actor time excludes indexing, preparation, service startup, grading and recovery.
It includes graph reads and compiler work. Complete economic break-even remains
unestablished. The graphs used identical resource limits, with 3 GiB containers,
1.5 GiB maximum heaps, a 1 GiB transaction-memory cap and 128 MiB page caches.

## Evidence and acceptance

`summary.json` and `runs.csv` retain every observation and aggregate. Per-run
directories contain answers, graded records, native usage receipts, actual MCP
evidence, server query receipts and matching audits. `plan.json` froze conditions
before dispatch. `conformance.json` contains exact graph/source checks and package
hashes. `validation.json` separates installed/live checks, offline checks and the
failed alias qualification. Full native transcripts remain local under
`/home/luke/.cache/deepthought-scip-v2`, identified by published hashes.

The internal runner labels `raw`, `tree` and `scip` mean expanded-v1, compact-v1
and compact-v2 in this four-condition mode. Hosted CI has not run for these
uncommitted changes. The engine changes passed their bounded reference and
dependency checks. Agent efficiency against the strong baseline and general
semantic completeness have no acceptance from this iteration.
