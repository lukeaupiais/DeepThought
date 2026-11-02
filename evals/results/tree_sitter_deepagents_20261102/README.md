# Completed real-agent calibration — 2026-11-02

Four fresh native Codex agent sessions completed on the pinned public DeepAgents
scenario. This validates the first TreeSitterTool-to-agent-to-grader integration.
**It did not demonstrate a token-efficiency improvement.**

The target was two Python backend files (1,882 lines, 78 independently labelled
declarations), not a whole-system architectural investigation. Source was read
from an existing checkout in place, without downloading, cloning, copying,
building or executing the target. No private repository or data was used.

## Prospective protocol

- Scenario: [`deepagents_backends.json`](../../scenarios/tree_sitter/deepagents_backends.json).
- Source: public `langchain-ai/deepagents`, revision
  `6a5d93f9ba7391189f65e6b999b724253577085d`; both file hashes verified against
  the working files and pinned Git blobs before preparation and during recording.
- Model: `gpt-6.1-sol`, reasoning effort `high`; native `codex-cli 0.166.0`.
- Order fixed before running: baseline, treatment, treatment, baseline (two pairs).
- Fresh ephemeral sessions, 300-second budget each, no automatic retries.
- Both arms received identical task, answer schema vocabulary and ordinary source
  tools, including Python AST and search. Only treatment received the actual raw
  `TreeSitterTool` StandardizedOutput. No context compression or special hint was
  added after observing outcomes.
- Linux Bubblewrap exposed only the selected public source files read-only,
  one actor context and one writable output directory per session, plus runtime
  libraries, ordinary commands, TLS trust roots and native-client authentication.
  Grading labels, scenario pack, worktree, other answers and private preparations
  were outside the actor namespace. Observed commands were reviewed independently.
- Agents returned final JSON without native `--output-schema`; the public grader
  validated those answers afterward. Every accepted run required exit code zero,
  a parseable final answer and exactly one native `turn.completed` usage receipt.
- Declaration labels were independently cross-checked with Python AST. A reviewer
  independently checked every final answer, native receipt, file hash and total.

The native invocation inside each isolated namespace was equivalent to:

```sh
codex --no-daemon -a never exec --ignore-user-config --ephemeral --json \
  --model gpt-6.1-sol -c 'model_reasoning_effort="high"' \
  --sandbox read-only --skip-git-repo-check --cd "$PUBLIC_SOURCE" \
  -o /work/output/answer.json -
```

The stdin instruction was identical for every run:

> Read /work/context.json and complete its task and instructions. The selected
> public files are available read-only at the listed workspace. Use normal source
> reading/search or Python ast parsing as useful; do not execute repository
> modules, build, install dependencies, contact application services or modify
> source. Do not inspect authentication, unrelated files or previous runs. Return
> the complete structured JSON answer as your final response, without Markdown
> fences or a summary. The supplied tool output, if present, is declaration
> evidence, not runtime proof.

Regenerate actor contexts with `deepthought-eval prepare` as described in
[the evaluation guide](../../../docs/evaluation-validation.md#minimal-agent-handoff),
then supply exactly one context per isolated session. Do not expose this result
folder or scenario/oracle to evaluated agents. Follow the same `record` and
`compare` handoff using native receipts. For these actual runs, the installed
CLI reproduced all four API records and both comparisons exactly.

## Results

| Run | Correct | Input | Cached input (subset) | Output | Total | Wall seconds |
|---|---:|---:|---:|---:|---:|---:|
| Pair 1 baseline | 78/78 | 48,201 | 41,216 | 2,933 | 51,134 | 40.7 |
| Pair 1 treatment | 78/78 | 70,923 | 52,480 | 2,915 | 73,838 | 35.4 |
| Pair 2 treatment | 78/78 | 71,077 | 64,128 | 3,006 | 74,083 | 81.6 |
| Pair 2 baseline | 78/78 | 48,155 | 41,216 | 2,899 | 51,054 | 36.0 |

All answers had zero missing, extra or duplicate facts, incorrect citations or
unsupported relationships. Both paired comparisons reported no token reduction.

Across both repetitions, baseline used **102,188** tokens and treatment
**147,921**: treatment used **44.8% more**. Uncached input plus output was
19,756 versus 31,313 (**58.5% more**); this is not a dollar-cost estimate.
Cached input is already included in input; reasoning is already included in
output. Cache state was not controlled, so report the individual counts rather
than infer monetary savings. Aggregate wall time was 76.7 versus 116.9 seconds
(**52.5% more**), with substantial variability and no reliable latency conclusion
from just two repetitions. Preparation took 0.120 seconds and zero model tokens;
charge it to cold treatment use, separately from actor time.

Both baseline sessions solved the inventory with ordinary Python AST. Both
treatment sessions read the graph and independently parsed source as well. On
this task the raw graph added context without removing that work. This is a
negative result for the tested delivery profile, not evidence of improved
architectural understanding, nor a general conclusion about all Tree-sitter use.
Do not tune this known pack and subsequently present it as held-out validation.

## Inspectable evidence and limits

`summary.json` contains exact counts, source identity, order, preparation cost,
prior failure history and hashes of the retained execution driver/namespace helper.
Each `*.answer.json` preserves the **exact native final-answer bytes**. Each
`*.receipt.json` includes the native terminal event, process exit receipt, graded
quality, context identity and SHA-256 fingerprints of answer/stdout/stderr.
Native events are observed receipts, not provider-signed attestations.

Raw local stdout/stderr and full preparations were retained for independent
review but are not vendored here: command streams include the public target's
source and large tool payloads. Hashes bind the inspected local evidence; a hash
alone cannot authenticate an unpublished log. The framework intentionally keeps
its `benefit_demonstrated` flag false for these caller-reported measurements.

Earlier attempts remain failures: an initial pair had TLS-trust failure; a second
pair exited without final answers or terminal usage, and its return codes were
not retained. Their usage is **unknown**, not zero, and is outside the completed
pair totals. The successful execution health probe used 28,474 total tokens and
is also outside comparison totals. The new runner removed the arbitrary file
size cap and native output-schema mode and retained exact exit codes; this does
not establish which change resolved the earlier unexplained exits.

This is real public-source, real-agent **calibration**, with four completed
sessions and an independently checked grader. It is not statistical, unseen,
large-repository, multi-language, refactoring or enterprise validation. Those
claims remain unestablished.
