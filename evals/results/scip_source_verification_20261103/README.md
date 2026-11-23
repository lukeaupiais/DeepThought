# SCIP source evidence and agent reliance

Completed 3 November 2026. Implementation remains uncommitted on
`codex/scip-neo4j`, based on OSS main `dec4607`.

## Acceptance outcome

**The data and lookup defects were corrected. Agent reliance and baseline
efficiency still fail acceptance.** All four final graph-assisted agents used
one verified Neo4j reference query, then independently reconstructed references
with TypeScript. Every requested answer was exact.

| Final batch, four sessions per condition | Source/compiler | Archived compact v2 | Source evidence v4 |
| --- | ---: | ---: | ---: |
| Correct requested occurrences | 92/92 | 92/92 | 92/92 |
| Input tokens | 362,853 | 457,865 | 434,133 |
| Cached input subset | 299,136 | 365,184 | 351,104 |
| Uncached input | 63,717 | 92,681 | 83,029 |
| Output tokens | 8,521 | 8,380 | 7,952 |
| Total tokens | 371,374 | 466,245 | 442,085 |
| Estimated Standard credits | 6.06394 | 7.64201 | 7.01721 |
| Summed native actor seconds | 161.89 | 156.74 | 242.19 |
| Observed graph sessions | 0/4 | 4/4 | 4/4 |
| Successful graph reads | 0 | 8 | 4 |
| Verified source reference reads | 0 | 0 | 4 |
| Canonical graph response bytes | 0 | 49,734 | 29,124 |
| Agent-authored compiler reference scans | 4 | 4 | 4 |
| Rejected MCP requests | 0 | 0 | 0 |

Source evidence v4 used **19.04% more tokens**, **15.72% more estimated credits**
and **49.60% more actor time** than its fresh source/compiler baseline. Against
the freshly rerun compact-v2 control it used 5.18% fewer tokens and 8.18% fewer
estimated credits, with 54.52% more actor time. These small stochastic samples
support an observed comparison, with no general speedup or isolated causal claim.

Input includes cached input. Output includes reasoning output. Each condition
contains two sessions for each of two cases. The 92 scored occurrences are 46
distinct locations repeated twice. Shared fresh baseline controls count once.

## Findings from actual traces

The earlier interface supplied native SCIP occurrences while live source
freshness remained unverified. Its producer also missed an imported alias in
the separate re-export control. Those were legitimate data-contract limitations.

The first source-evidence batch exposed additional interface barriers.

- Its actor handoff still described the earlier native-symbol workflow and
  omitted the new verified-reference capability.
- One agent requested `include_evidence=true` together with direct export lookup.
  That combination selects the historical raw SCIP view and was rejected. It
  subsequently requested expanded definitions and retried compact export lookup.
- Another agent chose historical expanded references and never consumed the new
  source evidence. Its two raw reference pages and compiler reconstruction remain
  costed.
- The two other sessions consumed verified source evidence and still wrote
  TypeScript symbol scans. The evidence alone did not eliminate reconstruction.
- Legacy coverage metadata still called workspace freshness unverified alongside
  the new successful source check. That contradictory field was corrected.

The final handoff describes the actual compact shape, direct public-export
lookup, source verification, historical raw view and available capabilities.
All four final agents then reached the verified result in one graph call.
Nevertheless, all four also called `createProgram` and `getTypeChecker` to
re-enumerate the same semantic references. They also retained ordinary source
search and inspection. The missing-data explanation was incomplete.

The traces establish duplicate execution. They provide no private model
reasoning that establishes why a verified response was still rechecked.
Consequently, this report does not claim that trust, instructions or completeness
was the sole remaining cause. An efficiency gain from replacing agent-authored
semantic reconstruction remains unproven.

Each run retains its actual compiler command in `command-evidence.json`, MCP
arguments and full public responses in `mcp-evidence.json`, server read receipts,
answer, grading, native usage and trace hashes. The retained initial batch makes
the failed first iteration visible.

## Implemented changes

The existing SCIP AnalysisTool now has an explicitly selected source-evidence
profile using caller-provisioned Node and TypeScript 6.0.3.

- One generic compiler pass covers every selected file and supplied native
  declaration target. It follows compiler aliases and module export membership,
  requires zero compiler diagnostics and performs zero model calls. It reads
  source and compiler inputs without executing project modules.
- `ReferenceEvidence` nodes store compiler-derived reference sets, integrity
  hashes and differences from original SCIP occurrences.
  `EXPORTS_REFERENCE_SYMBOL` edges connect exporting files to compiler symbols.
  Both use the existing shared Neo4j writer.
- Compact typed queries accept `export_name`, with `export_file` resolving
  distinct symbols sharing a public name. Equivalent symbol selection is
  deterministic. Candidate truncation cannot turn ambiguity into a unique match.
- Explicit trusted source bindings check source, configuration, dependencies,
  index, Node and compiler inputs before and after a verified query. Missing
  bindings and stale inputs yield explicit unverified status. Damaged reference
  sets reject the query. Paging and actual reference coordinates are retained.
- Original SCIP nodes and historical expanded queries remain available. The SDK
  default composer and existing saved artifacts remain supported. Disabling the
  optional profile preserves the earlier metadata shape and snapshot identity.
- Preparation advertises actual output capabilities and supplies current
  interface help. The evaluation CLI exposes a longer full-readback timeout.
  Node and relationship verification remains mandatory.

This increment adds source facts and source validation to the existing
AnalysisTool. It adds no GraphAnalysisTool or scenario execution family.

## Verification strength and latency

The source, task, gold, grader, actor prompt, native model/effort, timeout and
ordinary compiler access stayed fixed. Graph use remained optional. There was no
instruction to skip independent checks, force graph use or suppress compiler
execution. Source and private evaluation material remained separated by the
existing actor namespace.

The stronger source guard has a measurable cost. It rehashes the generation
receipt's 11,793 captured inputs, together with index and runtime/compiler
bindings, around each query. A separate post-batch operator probe measured
32.92 seconds for one complete API call, including 31.50 seconds in its initial
capture. The second call measured 2.58 seconds, including 1.23 seconds in initial
capture and 1.28 seconds in post-query validation. Both returned verified exact
reference sets. These two local samples reveal variable input-validation cost.
They do not establish a latency distribution or exact native-session attribution.

`QueryRecorder.wall_seconds` begins after the initial capture and includes
post-query validation. Its 23.65 seconds summed across the final four source
reads therefore excludes pre-query capture. The complete native actor wall times
above include that waiting. Per-tool payload sizes are retained, while native
usage remains session-terminal only. Exact token allocation by request remains
unavailable.

One final getNamedType session also made unsuccessful guidance-file discovery
commands and extra source reads. Their cost remains included. Provider behavior,
filesystem state and shared-host activity varied. Native actor time excludes
preparation, graph-service provisioning and coordinator/reviewer work.

## Independent controls and scope

The public corpus remains GraphQL.js revision
`c719a39547635991e621518d056b5e91026bdcda`, with 176 production TypeScript files,
43,148 lines, three configuration files and identical installed dependencies.
The unchanged cases inventory public `getNamedType` and `typeFromAST` references.
The source oracle and rename controls predate this increment and were retained.
The grader still rejects missing, extra, wrong-location, duplicate and invalid
claims. No labels were derived from the new graph.

The separate actual-producer alias/re-export fixture now yields eight compiler
references while the original native SCIP data still contains seven bound
occurrences and one unresolved imported name. The added reference comes from
compiler symbol resolution. Its difference from raw SCIP remains explicit.
This small fixture is a regression control. Agent performance is measured on the
public production corpus.

The fully read-back source-evidence graph has 63,381 nodes and 92,894
relationships. Both case preparations resolve to the same source snapshot.
The installed/live suite ran 227 tests, with 226 passing and one opt-in public
corpus skip. Controls cover stale source/configuration/dependencies/runtime,
repository binding, damaged evidence, alias closure, paging, candidate ambiguity,
repeat-snapshot fingerprints and actual capability advertising. Source-hidden
offline full acceptance passed with 30 optional integration skips. Dedicated
core-only offline acceptance passed. Ruff and whitespace checks passed.
Hosted CI has not run.

The first profile supports TypeScript 6.0.3 and its explicitly captured root
configuration/dependency layout. External configuration, project references and
symlinked input layouts require further profiles. Input checks are point-in-time
checks. Concurrent edit-and-restore races and compiler defects remain possible.
Static reference evidence leaves runtime behavior, safe refactor execution,
other languages and whole-system architecture unvalidated.

## Retained initial iteration and economic accounting

| Initial batch, four sessions per condition | Source/compiler | Compact v2 | Source evidence v3 |
| --- | ---: | ---: | ---: |
| Correct requested occurrences | 92/92 | 92/92 | 92/92 |
| Total tokens | 325,547 | 449,596 | 510,070 |
| Estimated Standard credits | 5.88063 | 8.32424 | 9.38034 |
| Summed actor seconds | 165.33 | 162.62 | 193.62 |
| Agent-authored compiler scans | 4 | 4 | 4 |

Source evidence v3 exceeded that fresh baseline by 56.68% in tokens, 59.51% in
estimated credits and 17.11% in actor time. It is retained as a failed iteration.
The final batch reused unchanged, fully verified graph facts and separately
hashed the corrected actor handoff. It reran both controls with fresh sessions.
Changes in baseline behavior across batches prevent attributing all between-batch
variation to the interface corrections.

Two preparation failures were retained. The first exceeded the original
30-second full-readback budget after 217.24 seconds. Raising the explicit budget
to 180 seconds preserved all checks. The second exposed elapsed preparation time
inside semantic snapshot metadata. Repeated identical analysis then conflicted
with its existing snapshot. Removing that nondeterministic field restored stable
fingerprints, which are covered by an actual repeated-analysis regression test.
The second attempt consumed 125.41 seconds for its first successful case and
69.02 seconds for the conflicting case. Final successful preparations consumed
126.52 and 81.34 seconds. The sum of recorded attempt durations is 619.53 seconds,
with zero preparation model tokens. Additional setup/restart and validation work
is outside those receipts.

Across both batches, all 24 native attempts completed and consumed 2,564,917
tokens, an estimated 44.30837 Standard credits and 1,082.38 actor seconds.
All 29 successful server reads were matched to native-client response hashes.
One initial rejected MCP request remains included in actor cost. No model
attempt was automatically retried or discarded.

Credit estimates reuse the frozen rate card in `cost-model.json`. Invoiced
dollars, marginal subscription spend, coordinator/reviewer model usage,
infrastructure charges and complete setup cost remain unknown. ERP and derived
private repository material are excluded.

## Evidence

- [Final measurements](final/summary.json) and [per-session CSV](final/runs.csv).
- [Initial measurements](initial/summary.json) and [per-session CSV](initial/runs.csv).
- [Final native/server audit](final/audit.json) and [initial audit](initial/audit.json).
- [Final prospective plan](final/frozen-plan.json) and [initial plan](initial/frozen-plan.json).
- [Compiler alias control](alias-evidence.json), [source oracle](source-oracle.json)
  and [grader falsification controls](grader-controls.json).
- [Live reference conformance](final/preflight.json) and [query phase probe](query-phase-profile.json).
- [Preparation accounting](preparation/summary.json) and [overall actor accounting](overall-accounting.json).
- [Installed/live tests](installed-tests.log), [offline full tests](offline-full.log)
  and [core-only acceptance](offline-core.log).

The source-data and interface changes passed local integration checks. The
behavioral objective remains unmet, so this work has not been committed or
presented as a ready PR.
