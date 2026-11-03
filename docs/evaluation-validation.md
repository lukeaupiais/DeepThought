# Public evaluation: what the first increment establishes

This increment targets the portable `TreeSitterTool` declaration capability. It
prepares equivalent source-navigation tasks for an ordinary agent and an agent
with actual tool output, grades their structured answers against independent
labels, and compares supplied usage records. It is a file-handoff harness: it
does not launch or sandbox agents, authenticate provider receipts, or establish
agent-performance benefit merely because a comparison completed.

Only public scenario packs and source-owned conformance fixtures are included.
There is no private application source, scenario, answer, trace or derived graph.
External repositories are never downloaded, cloned, copied, checked out or built
by this harness. The caller supplies an existing local checkout.

## Packs

| Pack | Inputs and independent labels | What it establishes |
|---|---|---|
| `evals/scenarios/tree_sitter/declarations.json` | Two small owned Python/TypeScript files; 15 hand-authored declarations and 1-based start lines | Synthetic conformance and grader falsification only |
| `evals/scenarios/tree_sitter/deepagents_backends.json` | Public `langchain-ai/deepagents` revision `6a5d93f9ba7391189f65e6b999b724253577085d`; two backend files, 1,882 lines and 78 declarations | A reproducible real-public-source calibration, not a held-out or enterprise benchmark |

The public files are `libs/deepagents/deepagents/backends/protocol.py` and
`libs/deepagents/deepagents/backends/composite.py`. Their SHA-256 fingerprints are
in the pack; source is not vendored. Public checkout HEAD must match the pinned
revision, and selected file bytes must match before/after preparation and when
answers are recorded. Matching selected files does not establish whole-checkout
identity. No imports from those source files are executed.

Expected labels were independently enumerated from source declarations and
reviewed against numbered source lines, with Python's standard-library AST used
as a cross-check for the public Python pack. They were not generated from the
production Tree-sitter output. Labels are fixed reviewed inputs; do not regenerate
an oracle from whatever the tool currently emits.

Both packs deliberately ask for declarations only. Calls, actual execution,
implementation conformance, dependency edges and architectural behaviour are not
supported conclusions of this Tree-sitter profile. A correct declaration list
with an invented call relationship must fail.

## Reproduce the checks

Install the package's Python and TypeScript extras in an appropriate development
environment, then run from the checkout:

```sh
python -m unittest discover -s tests -p 'test_eval_validation.py' -v
```

The default suite uses owned fixtures and does not access the network or search
for an external checkout. To opt in to the pinned public source:

```sh
DEEPTHOUGHT_PUBLIC_EVAL_WORKSPACE=/path/to/existing/deepagents \
  python -m unittest discover -s tests -p 'test_eval_validation.py' -v
```

Supplying a different revision or changed selected bytes is a source-provenance
failure, not an agent quality failure or evidence of tool benefit.

## Minimal agent handoff

```sh
mkdir -p eval-results
python -m deepthought.evaluation prepare \
  evals/scenarios/tree_sitter/declarations.json \
  evals/fixtures/tree_sitter_declarations \
  eval-results/declarations --interface raw-context
```

Preparation writes:

- `baseline.actor.json`: task and the same source location/file selection.
- `treatment.actor.json`: the same task/source plus actual registered-tool output.
- `prepared.private.json`: preparation identity, source fingerprints and the
  independent grading material. Never give this file to either agent.

Run fresh agents against exactly one actor file each and the same read-only
source, with equivalent ordinary tools, model/settings and budgets. Retain each
agent's answer as JSON containing `claims` and `relationships`; declaration claims
have exactly `file`, `symbol`, `kind` and positive 1-based `line` fields. An optional
`limitations` list contains strings. Do not provide reference answers or the
scenario pack to the agents.

Example answer shape (intentionally incomplete, not a passing answer):

```json
{
  "claims": [
    {"file": "cases.py", "symbol": "build", "kind": "Function", "line": 4}
  ],
  "relationships": [],
  "limitations": ["Declaration evidence does not establish a call graph."]
}
```

Record each answer, then compare:

```sh
python -m deepthought.evaluation record \
  eval-results/declarations/prepared.private.json baseline baseline.answer.json \
  eval-results/declarations/baseline.record.json
python -m deepthought.evaluation record \
  eval-results/declarations/prepared.private.json treatment treatment.answer.json \
  eval-results/declarations/treatment.record.json
python -m deepthought.evaluation compare \
  eval-results/declarations/baseline.record.json \
  eval-results/declarations/treatment.record.json \
  eval-results/declarations/comparison.json
```

Without telemetry these are unmeasured scripted controls. For real agent runs,
`record --telemetry telemetry.json` accepts `kind: "agent"`, an `agent` object with
`model` and `settings`, an `execution` object with `status` and `wall_seconds`, and
`usage` counts. Input/output counts and their cached/reasoning subsets must come
from retained native receipts. Never invent counts from source character length.
The harness labels these records caller-reported, not authenticated. Do not
publish local outputs or source paths without reviewing their contents.

The complete prepared object contains the oracle. Separating fields is not a
security sandbox: an actor able to read arbitrary files might still find the
scenario or another answer. The caller must enforce actual input/output isolation.
Known public calibration labels are inherently inspectable and unsuitable for
claims of unseen evaluation. Future independent held-out packs must be kept
outside actor-readable inputs.

## Falsification and extensibility

The independent tests check that a complete source-supported answer and a
reordered equivalent answer pass, while these fail:

- Missing a nested declaration or replacing one lexical scope with another.
- Inventing a declaration from a comment/string, or adding an unsupported call.
- Duplicate claims, wrong file/line citations, escaping paths and malformed data.
- Changed source fingerprints before an actor context is issued.
- Declaring an incomplete or incorrect result an efficiency improvement.
- Invalid token subsets or replacing missing usage with zero.

A registered independent fixture tool exercises the same preparation interface
without editing the runner. It is a harness control, not a second implemented
analysis capability or an AI agent. Additional trusted grader registrations allow
tools with different answer shapes; declaration semantics are not a universal
quality rubric. Scenario and grader authorship/review must remain separate from
claims that a producing tool is correct.

Scripted positive and negative controls can validate grader decisions. Even a
scripted answer reporting much lower token counts cannot demonstrate agent
benefit. Record hashes identify content and detect changes; they do not establish
that an agent/provider genuinely generated a measurement.

## Interpretation

Verification asks whether selected tool facts and identities match independent
source truth. Agent validation requires actual matched investigations and
independent quality assessment. This scaffold establishes the first and supports
the second; its conformance tests alone do not establish improved AI performance.

Compare input plus output once; cached input is part of input and reasoning is
part of output. Preserve missing counts as unknown. Incomplete executions can
retain reported usage, but those observations are not necessarily complete run
costs. Count preparation separately, include failures, and never silently equate
warm reuse with first-use cost or tokens with billed dollars.

A descriptive pair on these known declaration tasks is calibration, not statistical
or enterprise validation. Confirmatory claims need independently maintained unseen
investigations, appropriate normal-tool baselines, counterbalanced repetitions,
quality guardrails and measured preparation/refresh costs. A successful Tree-sitter
lookup does not establish support for Go, framework effects, distributed control
flow, runtime architecture, refactoring or formal proofs.

## Completed native-agent calibration

The first real-agent calibration completed four fresh matched sessions on the
pinned public DeepAgents pack, in baseline/treatment/treatment/baseline order.
Every run returned a final answer, exited successfully, supplied native token
counts and scored 78/78 with no extras or unsupported relationships. The
installed evaluation CLI reproduced all records and comparisons.

This tested profile **regressed on token efficiency**: treatment used 147,921
tokens against baseline's 102,188 (+44.8%) across two pairs, with equal accuracy.
Both treatments read the raw graph and still used Python AST. These results
validate the end-to-end evaluation, not a performance benefit or enterprise
architecture capability. See the [protocol, exact answers, receipts and limits](../evals/results/tree_sitter_deepagents_20261102/README.md).

## On-demand graph access

Query-access mode supplies a trusted JSON command/help descriptor instead of raw
graph output; extracted facts remain in the private preparation. The caller must
verify actual service contents and successful agent query invocations separately.
A correct final answer with a disconnected or unused graph endpoint does not
validate integration. The [core graph port validation](../evals/results/neo4j_query_deepagents_20261102/README.md)
includes real Neo4j/Cypher access, readback, native token receipts and a failed
network-blocked pilot retained alongside the corrected runs.

## Enforced Neo4j interface profile

The default evaluation CLI now imports and verifies the selected tool's actual
output in Neo4j. Its actor handoff contains query access, never raw graph JSON.
The production query service/CLI/MCP can emit preparation/session-bound read
receipts; comparisons independently recompute their eligibility. Missing calls,
wrong graph/run identities, replayed or altered receipts, and baseline graph
contamination fail the acceptance gate without erasing measured costs.

`tests/test_neo4j_evaluation.py` exercises actual imports/readback, database
corruption detection, default CLI behaviour, real CLI/MCP audit emission and
negative controls against the isolated CI Neo4j service. These are conformance and
falsification controls; separately retained native-agent runs validate actual
agent use. Historical calibration artifacts remain reports of their original
profiles and are not retroactively certified by the new receipt gate.

The [native MCP interface-gate validation](../evals/results/neo4j_interface_gate_20261102/README.md)
retains the actual production-MCP-to-Neo4j query response and matched server-read
receipt, plus a blocked attempt that the new gate rejected despite 78/78 answer
quality. The corrected run passed the interface gate; baseline reuse and all
failed-attempt costs are explicit, with no performance-benefit claim.
