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

## Minimal workflow

Install the selected extras separately. From this checkout:

```sh
pip install '.[python,typescript]'
deepthought-eval prepare \
  evals/scenarios/tree_sitter/declarations.json \
  evals/fixtures/tree_sitter_declarations eval-results/calibration
```

Preparation runs the actual registered tool and writes:

- `baseline.actor.json`: task, selected source paths and response instructions.
- `treatment.actor.json`: the same inputs plus the actual tool output.
- `prepared.private.json`: source/tool identities and the independent oracle.

Give each fresh agent only its own actor context and identical read-only access
to the selected source. Keep the oracle, the other answer, grading tests and
previous runs outside the agent's accessible filesystem. The harness separates
handoff files but **does not provide an operating-system sandbox**. The operator
is responsible for isolation, budgets and normal-tool parity.

Record each answer independently:

```sh
deepthought-eval record eval-results/calibration/prepared.private.json \
  baseline baseline-answer.json eval-results/calibration/baseline.json
deepthought-eval record eval-results/calibration/prepared.private.json \
  treatment treatment-answer.json eval-results/calibration/treatment.json
deepthought-eval compare eval-results/calibration/baseline.json \
  eval-results/calibration/treatment.json eval-results/calibration/comparison.json
```

Answers for the declaration grader use `claims`, each containing `file`,
`symbol`, `kind` and a one-based `line`, plus an empty `relationships` list.
There is no inference from symbol references to execution or calls.

For a real agent run, `record --telemetry receipt.json` accepts:

```json
{
  "kind": "agent",
  "agent": {"model": "actual-model-id", "settings": {"effort": "actual-setting"}},
  "execution": {"status": "completed", "wall_seconds": 12.3},
  "usage": {
    "input_tokens": 1000,
    "cached_input_tokens": 800,
    "output_tokens": 100,
    "reasoning_output_tokens": 20
  }
}
```

These example numbers are not measured results. Usage is caller-reported, not
authenticated by the framework. Missing usage stays unknown; cached input and
reasoning output are subsets, never additional tokens. The default record kind
is `scripted_control`. Scripted answers can validate the grader but cannot
demonstrate an agent improvement. Comparisons retain failed outcomes and reject
incompatible inputs, settings or handoffs. A descriptive actor-token reduction
is separate from an independently demonstrated performance benefit.

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
