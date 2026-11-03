# Public tool evaluation

This first increment ports the independent `AnalysisTool` interface and registry
without changing the existing SDK. `deepthought.analysis_tools.TreeSitterTool`
uses the SDK's Python/TypeScript/TSX declaration extractor. Its capabilities are
syntax extraction and symbol indexing, not execution, call resolution, Go
analysis, control/data flow or architectural inference. Original interface and
contract attribution is retained in `NOTICE` and `licenses/`.

Tools return portable `StandardizedOutput` records and never require a database.
The registry selects a tool by name, language or capability; registering or
discovering it does not run analysis. Grammar dependencies remain optional.
Missing grammars and partial source results remain visible in tool metadata.

## AnalysisTool evaluation through Neo4j

The default CLI evaluation interface is **Neo4j**. Provision a dedicated test
instance and the normal `DEEPTHOUGHT_NEO4J_URI`, `DEEPTHOUGHT_NEO4J_USER`,
`DEEPTHOUGHT_NEO4J_PASSWORD` and `DEEPTHOUGHT_NEO4J_DATABASE` configuration. Keep
writer credentials, grading material and query logs outside the actor namespace.
Use database-enforced read-only access for the agent's query service after import.
See [graph setup and scope](graph-port.md).

```sh
pip install '.[python,typescript,neo4j,mcp]'
deepthought-eval prepare \
  evals/scenarios/tree_sitter/deepagents_backends.json \
  /path/to/existing/public/deepagents eval-results/tree-sitter
```

This runs the selected registered AnalysisTool once, imports its exact output
through the existing WriteCoordinator/GraphWriter, and reads back every stored
node and relationship. Counts, labels, identities, endpoints and properties must
match. A foreign graph in the database is rejected before import. A missing
connection never silently selects raw JSON. No target clone, build or execution
occurs; public Git revision and source hashes remain required.

Preparation writes:

- `baseline.actor.json`: task, selected source and shared answer instructions.
- `treatment.actor.json`: the same inputs plus the actual query-interface
  descriptor and selected repository/snapshot; **no graph dump or oracle**.
- `prepared.private.json`: grading material, extraction output and verified Neo4j
  readback identity. Give this file to neither agent.

Configure the actual `deepthought-mcp` server or `deepthought-graph` adapter for the
selected database, repository and snapshot. An optional `--tool-access` JSON file
can describe your configured transport; its text is never executed by the
harness. Fresh agents retain the same ordinary source/AST/search tools.

## Require actual query evidence

Enable the existing query service's audit hook for the treatment session. Set
these three variables on the evaluator-controlled MCP/CLI service process:

```text
DEEPTHOUGHT_EVAL_INPUT_ID=<prepared.private.json input_id>
DEEPTHOUGHT_EVAL_RUN_ID=<fresh treatment session identity>
DEEPTHOUGHT_EVAL_QUERY_LOG=<evaluator-owned existing empty JSONL file>
```

Use a new session identity and empty log for every actor. Run a separate MCP
service/recorder per actor; do not mix concurrent agents under one fixed run ID.
Keep the log outside
actor read/write access. These settings do not launch a server or grant access;
the caller still configures the real transport and source/oracle isolation.
Embedded callers can pass `QueryRecorder(input_id, run_id, path)` as the optional
`observer` to the same production `GraphQueryService`.

After a real database read and transaction rollback, the service records its
backend, database, repository, snapshot, stored-content hash, query/response hashes,
returned bytes/count and elapsed time. It does not log query parameters or
credentials. These are database-read measurements; one MCP context call may
perform multiple database reads. Native agent traces retain client failures and
transport overhead separately. Query time is already inside agent wall time.

Record native token counts, model/settings and completion state through the
existing `--telemetry` JSON handoff, then attach the actual query journal:

```sh
deepthought-eval record eval-results/tree-sitter/prepared.private.json \
  baseline baseline-answer.json baseline.record.json \
  --telemetry baseline.telemetry.json --run-id fresh-baseline

deepthought-eval record eval-results/tree-sitter/prepared.private.json \
  treatment treatment-answer.json treatment.record.json \
  --telemetry treatment.telemetry.json --run-id fresh-treatment \
  --graph-queries treatment.queries.jsonl

deepthought-eval compare baseline.record.json treatment.record.json comparison.json
```

A treatment must have at least one successful server-observed read bound to this
preparation, session and exact stored graph. Missing calls, failed connections,
wrong backends/databases/snapshots, reused session evidence or graph use in the
baseline cannot yield an accepted matched AnalysisTool comparison. Correct final
answers alone do not certify the interface. Costs and grades remain visible when
interface acceptance fails; do not discard failures from aggregate reporting.

`analysis_tool_evaluation_accepted` requires the verified interface plus the
existing completion, source, quality and usage guardrails. Inspect
`graph_interface` independently when a real graph-assisted answer is wrong or
usage is incomplete. An accepted integration is not proof of a performance gain:
`benefit_demonstrated` remains false for these caller-reported measurements.
Native/server receipts require independent review; hashes detect content changes,
not fabrication by a privileged evaluator or signed provider billing.

## Explicit controls

Raw graph injection is retained only as an explicitly selected control:

```sh
deepthought-eval prepare evals/scenarios/tree_sitter/declarations.json \
  evals/fixtures/tree_sitter_declarations eval-results/raw-control \
  --interface raw-context
```

The backwards-compatible Python `prepare()` function also remains a raw-context
control unless supplied a generic descriptor. Use
`deepthought.evaluation.neo4j.prepare_neo4j()` for actual AnalysisTool evaluations.
An unverified descriptor alone (`--interface tool-access --tool-access file.json`)
cannot certify a Neo4j interface. Comparisons label raw controls and generic
interfaces explicitly; neither is accepted as a Neo4j AnalysisTool evaluation.
Raw-control token ratios describe only raw injection.

Source-fact conformance and grader falsification remain valid offline checks;
they do not require Neo4j or an agent. Missing usage remains unknown. Cached input
is part of input, and reasoning output is part of output. Retain preparation,
server and native execution costs rather than claiming token counts are dollars.

## Public scenarios and extension

The bundled synthetic scenario exercises misleading strings, duplicate names,
nested declarations and Python/TypeScript distinctions. It is conformance and
calibration material, not an enterprise benchmark.

The `deepagents_backends.json` pack references two real public DeepAgents files
at an exact revision with SHA-256 source pins. Pass an existing checkout as the
workspace. The harness neither downloads repositories nor runs their code,
builds, package installers or services. This is a selected-file source-navigation
case, not an evaluation of the whole application's architecture.

Each scenario selects an analysis tool from `ToolRegistry`. A different tool
can contribute a separate JSON pack. For a different answer/fact format,
register a trusted `Grader` with `register_grader` and select its identifier in
the scenario. Graders supply expected-fact validation, answer scoring and actor
instructions. They are evaluator code, not functions an evaluated agent selects
or edits. Scenario labels must be authored/reviewed independently of tool output.
Register additional tools/graders in a trusted Python driver before calling the
SDK or CLI entry function; JSON packs do not import executable code and this MVP
does not auto-discover third-party entry points.

Evaluation-capable tools declare `metadata.custom_metrics.analysis_status`
(`succeeded` only for their complete supported scope). They may report a known
nonnegative `model_tokens` preparation count; absent accounting remains unknown.
Tree-sitter explicitly reports zero model tokens. These fields do not replace
the tool's coverage, diagnostics or the independent outcome grader.

Future scenario packs should separate known development cases from genuinely
held-out validation. Public contributions contain public inputs and independently
reviewed expected facts; private experiments and credentials belong outside the
project. A public URL in a manifest is provenance, not automatic proof that an
arbitrary local checkout is publicly authorized.

## Validation and limits

The [three-repository native pilot](../evals/results/tree_sitter_refactor_20261102/README.md)
contains twelve fresh agent sessions on 346 public production files and 107,456
lines. Treatments queried the actual MCP/Neo4j interface. Exact declaration
grading, independent source-parser comparisons and source-backed behavioral
reviews are reported separately, alongside native tokens, estimated credits,
time, failed requests and limitations. The pilot found more total tokens and
actor time with a near-tie credit estimate. It also exposed five omitted named
TypeScript function expressions. These results do not establish a consistent
efficiency advantage.

The [scenario instructions](../evals/scenarios/tree_sitter/refactor/README.md)
describe the trusted grader registration, native runner, isolated inputs and
independent conformance checkers. Their small falsification controls validate
the evaluator separately from the real-repository agent measurements.

Run the independent controls described in
[evaluation-validation.md](evaluation-validation.md). They test correct answers,
plausible wrong answers, missing/invalid measurements, changed inputs and
extensibility. Installed-artifact acceptance also exercises these controls with
the source checkout hidden and networking disabled.

These checks validate the initial harness and declaration profile. They do not
establish lower cost, broader language coverage or improved agent reasoning.
Actual performance claims need independently reviewed outcomes and retained
native usage evidence on repeated, comparable agent investigations, including
preparation and refresh costs.
