# Milestone agent-ab-1: gate for the first scored agent A/B

Decided 2026-11-24. The first scored agent A/B runs on Go and TypeScript/JavaScript
code and on multi-service systems whose contracts are gated. It starts only when
every criterion below is green. Machine-readable criteria and the frozen task set
are in [`evals/milestones/agent-ab-1.json`](../../evals/milestones/agent-ab-1.json).

```sh
python tools/milestone_status.py evals/milestones/agent-ab-1.json
```

prints PASS/FAIL per measurable criterion against the committed ground-truth
baseline and lists the manual items.

## Why this scope

A scored A/B is only informative when the graph is close to its target on the
code the tasks touch; otherwise it re-measures gaps the ground-truth gate already
shows. At engine commit 18cb028, Go impact recall is 0.98 (cobra) and 0.995
(chi) with precision 1.0, TypeScript/JavaScript is 0.79 to 0.95, and 14 of 16
contract corpora are perfect. Java (gson 0.57) and Rust (ripgrep 0.24) are not.
Earlier native-agent pilots in `evals/results/` showed the graph arm using 8% to
45% more tokens on declaration lookups that plain search already answers, so the
task set targets impact and cross-service questions and includes controls that
measure overhead.

A follow-up milestone reruns the same frozen task shapes on Java/Spring once a
held-out Spring repository reaches impact recall 0.90.

## Entry criteria

| Criterion | Threshold | At 18cb028 |
| --- | --- | --- |
| Impact, cobra and chi (Go) | recall >= 0.95, precision >= 0.99 | 0.98 / 0.995, 1.0 |
| Impact, ky and hono (TypeScript) | recall >= 0.90, precision >= 0.99 | 0.79 / 0.89, 1.0 |
| Impact, preact (JavaScript) | recall >= 0.90, precision >= 0.99 | 0.95, 1.0 |
| Service pairs, online-boutique | recall >= 0.95, no false pairs | 13/15 |
| Service pairs, robot-shop | recall >= 0.95, no false pairs | 14/18 |
| Other contract corpora | stay at 1.0 recall and precision on pairs | met |
| Held-out Go and TypeScript repos | impact recall >= 0.90, precision >= 0.99 | not labelled yet |
| MCP capabilities | getImpact, getFunctionCallers, getComponentImpact, diffSnapshots answer on a freshly indexed task repo; every returned edge has a rule ID | to verify |
| Graders | affected-set-v1, change-plan-v1, change-check-v1, exact-fact-v1 with positive and negative controls | missing |
| Dry run | one Go and one services task on the operator machine with graph read receipts in the treatment arm | not run |

With 15 and 18 labelled pairs, the 0.95 threshold means every pair must be found.

### Held-out repositories

One Go and one TypeScript repository, plus a reserve for each, were chosen on
2026-11-24 and sealed. Only the SHA-256 commitment is in the milestone file. The
verification maintainers unseal, pin and label impact truth with
`tools/impact_truth.py` after every other criterion is green; builders do not see
or tune on them. A reserve replaces a primary only if its oracle cannot run, with
the reason recorded.

## Frozen task set

Nine scored tasks across the three objectives plus three controls. Full prompts,
truth sources and seeded faults are in the milestone file.

| ID | Objective | Corpus | Question | Truth |
| --- | --- | --- | --- | --- |
| U1 | Understand | online-boutique | What must be reviewed if CartService/GetCart's response changes? | contract labels: 2 clients + server |
| U2 | Understand | robot-shop | Who calls catalogue's GET /product/{sku}, directly or via the proxy? | contract labels: 4 clients |
| U3 | Understand | cobra | What can change if InheritedFlags changes? | impact truth: 35 dependents |
| P1 | Plan | chi | Add a required parameter to Mount: must-edit and re-test sets | impact truth: 10 direct, 31 total |
| P2 | Plan | hono | Make stringBufferToString async: must-edit and re-test sets | impact truth: 5 direct, 34 total |
| P3 | Plan | online-boutique | Move CurrencyService/Convert to a new service | contract labels: 2 clients + handler |
| C1 | Check | cobra | Find call sites a rename patch misses | 2 seeded omissions |
| C2 | Check | ky | Find call sites a signature patch misses | 1 seeded omission |
| C3 | Check | robot-shop | Find clients a route-rename patch misses | 2 seeded omissions (PHP, Python) |
| K1-K3 | Control | cobra, hono, ky | Plain declaration or constant lookups | source at pin |

Graders score answers against existing ground truth, never against engine output:
`affected-set-v1` (recall, precision, F1 over file and function or call site),
`change-plan-v1` (must-edit recall and precision, re-test recall), `change-check-v1`
(omissions found, false alarms) and `exact-fact-v1`. Unjudged truth rows are
ignored, as in the gate.

## Run plan

- **Where:** the operator's machine (myarch) through Remote Control, with Codex CLI
  on the personal subscription. No paid API.
- **Runner:** a task-generic successor to `tools/run_tree_sitter_eval.py` with the
  same isolation (bubblewrap namespace, read-only source, ephemeral sessions,
  native receipts, read journal). It approves the impact, callers, component,
  snapshot and explain tools in the treatment arm and uses a 600 second budget.
- **Graph:** each corpus is indexed once at its pin with the frozen engine commit,
  imported into a read-only Neo4j snapshot, and its preparation time recorded
  separately from actor time.
- **Sessions:** 12 tasks x 2 arms x 3 repetitions = 72 fresh sessions, counterbalanced
  (B T T B B T for odd tasks, mirrored for even). Same CLI version, model and
  reasoning effort in both arms. About 12 to 18 sessions per day keeps usage
  inside subscription limits, so 4 to 6 days.
- **No retries:** incomplete runs are kept and reported. Tasks, truth and graders
  are not changed after the first scored run.
- **Metrics:** correctness per grader, wrong claims, total native tokens (input plus
  output), actor wall time, tool calls, graph reads, files read, preparation cost.
- **Decision rule** (to confirm before the first run, then frozen): on the nine
  scored tasks, treatment mean F1 at least 0.10 higher, wrong claims not higher, and
  median total tokens no more than 10% higher. Control-task overhead is reported;
  above 25% is recorded as a defect to fix.

## Open gaps at 18cb028

| Owner | Gap |
| --- | --- |
| Call resolution | ky impact 139/175: +19 dependents needed (call misses: attribute calls, unsupported bindings) |
| Call resolution | hono impact 96/108: +2 dependents needed (1,298 attribute-call misses) |
| Contracts | C# gRPC servers (cartservice `CartService.cs`): frontend and checkoutservice to cartservice |
| Contracts | PHP Symfony `@Route` docblock routes in ratings: web and load-gen to ratings via proxy |
| Contracts | PHP curl client with `sprintf` URL (ratings to catalogue) |
| Contracts | Java Apache HttpClient `new HttpPost(baseUrl + id)` (shipping to cart) |
| Impact queries | getImpact and getComponentImpact reach across contract edges, with rule IDs on every edge |
| Verification | four graders, seeded-fault patches for C1 to C3, control truth, runner, dry run, held-out labelling |
