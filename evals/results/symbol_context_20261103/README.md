# Declaration source and ownership restoration

Completed 3 November 2026, local time. Changes remain uncommitted on
`codex/scip-neo4j`, based on OSS main `dec4607`. ERP and private derived inputs
are excluded.

## Outcome

Source and ownership restoration passed functional validation. The independent
TypeScript AST oracle matched all **1,587 selected declaration bodies and
ownership edges**, across 166 files containing declarations within the 176-file
GraphQL.js corpus. Actual Neo4j, CLI and MCP retrieval passed, including complete
source-page reassembly.

The matched agent comparison still fails the token-efficiency gate. All four
candidate agents used verified references and then reconstructed them with
TypeScript. **Zero agents requested getCodeContext.** Agent usefulness on tasks
requiring declaration bodies or ownership remains open.

| Four fresh sessions per condition | Source/compiler | Frozen source evidence v4 | Symbol context v1 |
| --- | ---: | ---: | ---: |
| Correct requested occurrences | 92/92 | 92/92 | 92/92 |
| Input tokens | 315,208 | 407,418 | 413,073 |
| Cached input subset | 254,336 | 344,576 | 351,104 |
| Uncached input | 60,872 | 62,842 | 61,969 |
| Output tokens | 8,217 | 7,930 | 7,508 |
| Total tokens | 323,425 | 415,348 | 420,581 |
| Estimated Standard credits | 5.73369 | 5.98604 | 5.85321 |
| Summed native actor seconds | 208.91 | 214.10 | 203.48 |
| Verified reference queries | 0 | 4 | 4 |
| Context queries | 0 | 0 | 0 |
| Agent-authored compiler scans | 4 | 4 | 4 |
| Explicit source-inspection commands | 11 | 6 | 8 |
| Shell output bytes | 122,521 | 101,594 | 109,632 |
| Canonical graph response bytes | 0 | 29,124 | 29,248 |
| MCP request errors | 0 | 0 | 0 |

Against baseline the candidate used **30.04% more tokens**, **2.08% more estimated
credits** and **2.60% less actor time**. Against frozen v4 it used 1.26% more
tokens, 2.22% fewer estimated credits and 4.96% less actor time. These small
samples establish observed comparisons. They leave statistical and general
performance claims unvalidated. Body retrieval had zero observed adoption, so
changes in time or source inspection cannot be attributed to consuming bodies.

Input includes cached input. Output includes reasoning output. The 92 scored
occurrences are 46 distinct locations repeated twice. Each fresh baseline is
shared across its two treatment comparisons and counted once in aggregates.
Estimated credits use the same frozen Standard rate card as the preceding
iterations. Invoiced dollars, marginal subscription spend, coordinator/reviewer
model usage and infrastructure charges remain unknown.

## What was restored

The original Code_to_Graph TreeSitter pipeline stored selected AST chunk source
in `source_code` and recorded nesting depth. Its parent-link construction read
`parent_chunk_id`, while the inspected extraction path left that field unset.
The OSS restoration captures actual AST ownership through declaration IDs.

- The existing parser captures source, source/name ranges, file/body hashes,
  parent definition IDs and `lod` in its existing parse. The standalone SDK
  artifact schema, definition identities and default result remain supported.
- The TreeSitter AnalysisTool stores the complete selected declaration source.
  Its richer projection has an explicit graph snapshot identity. Existing
  file-to-declaration `DEFINES` links remain available alongside AST `CONTAINS`
  ownership links. Top-level named declarations have depth zero.
- The shared coordinator associates SCIP declarations with AST declarations
  through `HAS_AST_DECLARATION`. Exact name coordinates and equal file hashes
  are mandatory. Missing or ambiguous matches retain explicit status. External
  symbols lacking physical source ranges remain unmatched. Multiple overload
  declarations stay distinct.
- `getCodeContext` resolves exact native or declaration identities within the
  file and returns source plus ownership metadata. Default pages contain at most
  1,200 Unicode characters per declaration. Explicit expansion supports up to
  8,192 characters per page. Source offsets count Unicode code points. Overall
  response byte limits and declaration/relationship truncation remain explicit.
- Returned source is identified as stored-snapshot content. When source bindings
  are configured, current inputs are checked before and after the query and
  body hashes/pages are compared with bound source bytes. Stale inputs report
  unverified freshness. Damaged cached bodies reject currently verified reads.
- Context results use one query/response evidence boundary. The final formatted
  response, including relationships and source verification, is hashed for the
  recorder. Relationship-node metadata retains the existing envelope, with
  source blobs omitted from neighbour nodes to keep bodies on the paged path.

Supported source bodies follow the current named Python and TypeScript/TSX
extractor. Arrow functions use variable-declarator AST ranges. Decorated Python
declarations include their decorators. Original heuristic calls, CFG/data-flow
analysis and additional language profiles remain outside this restoration.

## Functional validation

The installed/live suite ran **231 tests, with 230 passing and one optional
public-corpus skip**. Both model-candidate and final delivery runs are retained.
Checks cover nested classes/methods/functions, decorated Python, Unicode name
coordinates and source paging, overloads, duplicate names, aliases, stale inputs,
large bodies, ambiguous associations, source mismatches and damaged cached code.

Independent validation parses public source with TypeScript 6.0.3 rather than
DeepThought's TreeSitter extractor. It checks name coordinates, declaration body
boundaries, source hashes and nearest selected ancestor identities. It also
compares expected ownership edges with the actual prepared graph. All 1,587
selected declarations and ownership edges matched. The shared writer's full
readback separately verified all 63,381 nodes and 96,000 relationships.

Live reference preflight reproduced all 20 getNamedType and 26 typeFromAST
reference coordinates against the unchanged source oracle. Native-symbol source
retrieval returned all six and five corresponding AST declarations. Actual MCP
journeys exercised default-sized source responses and multi-page expansion with
complete-body SHA-256 checks. Source-hidden offline full acceptance passed with
31 optional integration skips. Dedicated core-only acceptance passed. Ruff and
whitespace checks passed. Hosted CI has not run.

## Agent experiment and audit

The corpus remains GraphQL.js revision
`c719a39547635991e621518d056b5e91026bdcda`, with 176 production TypeScript files,
43,148 lines, three configuration files and identical installed dependencies.
Tasks, gold reference ranges, actor prompt, model/effort, compiler access,
timeouts and grading were preserved. The fixed order per case was baseline,
frozen-v4, candidate, candidate, frozen-v4, baseline. Every actor was a fresh
session. Graph use remained optional. There were no automatic model retries.

All twelve native subprocesses returned exit zero and produced terminal usage,
answers, scores, receipts, summaries and comparisons. All eight successful graph
reads matched native-client response hashes. The outer orchestration tool later
reported exit 143 when resumed. The independent completion audit confirms all
native artifacts were complete before that report. This distinction is retained
in `completion-audit.json`, and no model rerun was performed.

Across the batch, native agents consumed 1,159,354 tokens, an estimated 17.57294
Standard credits and 626.49 actor seconds. Explicit source-inspection command
counts classify visible `rg`, `sed` and source/configuration `cat` commands.
They exclude context-file setup and implicit compiler file reads. They therefore
measure inspection commands rather than total filesystem accesses. Command
strings and output hashes are retained for review.

Native usage is session-terminal only. Exact request-level token attribution
remains unavailable. Query recorder time excludes pre-query source capture and
includes post-query validation. Native actor wall time includes the corresponding
waiting, while service provisioning, preparation and coordinator work are outside
that actor measure. Shared-host and provider variation limit latency conclusions.

## Compatibility checks after the native batch

The model-evaluated wheel and package manifest are retained separately from the
final delivery wheel. Final checks preserved legacy relationship-node envelopes
and sorted labels, with bounded neighbour metadata. The coordinator also learned
to leave external native symbols with missing location fields unmatched.
These changes affect two modules listed in `delivery-artifact.json`.

The final delivery package passed the complete installed/live suite and actual
MCP context journeys. Replaying the native reference requests through final MCP
produced identical canonical response hashes. Native agents had made zero context
calls, and their model runs were retained without repetition. Consequently the
reported agent measurements belong to the frozen evaluated wheel, while final
context compatibility has separate direct MCP and regression-test evidence.

## Preparation failures and resource conditions

The first preparation attempted to place the candidate beside the control
snapshot. The harness correctly rejected this single-snapshot isolation violation
after 60.64 seconds. The control snapshot fingerprint was verified and its service
restored to enforced read-only access. A dedicated candidate database was then
provisioned.

The second attempt exceeded the original 1 GiB transaction pool after 83.49
seconds. Atomic rollback was verified at zero graph nodes. Before model dispatch,
both graph conditions received matched 4 GiB container limits, 2,048 MiB maximum
heaps and 1,536 MiB transaction pools. The full node/relationship readback remained
mandatory. Both services rejected write probes, and the frozen control package
and snapshot fingerprint were checked.

Successful candidate preparations took 98.19 and 64.41 seconds. Both cases use
the same complete source snapshot. All recorded attempts total 306.73 seconds,
with zero preparation model tokens. Provisioning/restarts, installation,
standalone validation and coordinator work remain outside those elapsed receipts.
All failures are retained under `preparation`.

## Evidence

- [Measurements](summary.json), [per-session CSV](runs.csv) and [native/server audit](audit.json).
- [Prospective plan](frozen-plan.json) and [native completion audit](completion-audit.json).
- [Independent AST/body/ownership oracle](independent-context-oracle.json).
- [Live reference and body preflight](preflight.json) and [actual MCP journeys](mcp-journey.json).
- [Delivery tests](delivery-tests.log), [offline full acceptance](delivery-offline-full.log)
  and [core-only acceptance](offline-core.log).
- [Model package manifest](installed-artifact.json), [final delivery manifest](delivery-artifact.json)
  and [matched resource settings](resource-conditions.json).
- [Preparation accounting](preparation/summary.json) and [rollback proof](preparation/rollback.json).

Public GraphQL.js excerpts retain their upstream provenance and
[MIT licence](source-license.txt).

Engineering restoration passed. Token-efficiency and elimination of compiler
reconstruction remain failed gates for the existing reference-inventory pilot.
Changes remain uncommitted and have not been presented as a ready PR.
