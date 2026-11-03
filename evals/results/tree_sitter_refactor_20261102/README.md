# TreeSitterTool through Neo4j — public agent pilot

Completed 2026-11-03 UTC, corresponding to 2026-11-02 local time. The results were reviewed before the user authorized merging PR #8. This report evaluates the actual declaration tool and agent outcomes separately from transport acceptance.

## Result

The pilot did not establish a meaningful efficiency improvement. The graph-assisted agents used **8.45% more total tokens** and **11.47% more actor time**. Their estimated credit usage was **0.68% lower** with uncontrolled provider caching. All requested declaration locations were correct in both conditions. The half-point semantic difference came from one baseline answer omitting an explicit SDL/type-info distinction, with no material behavioral false claim found in that review.

Each condition below contains six fresh native sessions. Total tokens include cached input. Reasoning output is already part of output.

| Measure | Source/search/parser baseline | Baseline plus TreeSitterTool graph |
| --- | ---: | ---: |
| Input tokens | 1,777,027 | 1,925,900 |
| Cached input subset | 1,414,144 | 1,594,752 |
| Output tokens | 29,195 | 32,945 |
| Total tokens | 1,806,222 | 1,958,845 |
| Declaration locations | 52/52 | 52/52 |
| Source-backed semantic rubric | 47.5/48 | 48/48 |
| Estimated Standard Codex credits | 28.97826 | 28.78053 |
| Summed actor seconds | ~720.11 | 802.74 |
| Native shell commands | 87 | 90 |
| Native shell output bytes | 919,253 | 996,035 |
| Successful Neo4j reads | 0 | 13 |
| Rejected DeepThought requests | 0 | 3 |

The first baseline completed successfully at the native turn level, but an accounting-parser exception happened before its return code and monotonic duration were saved. Its portable record keeps wall_seconds null. The aggregate uses a separately labeled 115.077384-second filesystem elapsed estimate. All other actor times are measured with a monotonic timer. Full native token accounting and the original answer were retained. No model rerun was used to recover this record.

## Tasks and corpus

| Public repository | Selected production files | Lines | Task |
| --- | ---: | ---: | --- |
| DeepAgents | 53 | 25,569 | Assess mandatory backend deletion, removing async fallback and capability gates. Locate explicit methods and trace compatibility, routing, state, permissions and errors. |
| LangChain Google | 117 | 38,739 | Assess consolidation of GenAI and Vertex tool/schema plumbing. Locate six entrypoints and distinguish actual provider dispatch, conversions, packing and error contracts. |
| GraphQL.js | 176 | 43,148 | Assess combining validation passes into one AST walk. Locate nine overload/implementation facts and reason about skip/BREAK/edit ordering, type stacks and validation integration. |

Combined scope is **346 files and 107,456 lines** from three pinned public repositories. Python checkouts were analyzed in place. A public GraphQL.js checkout was acquired for the TypeScript case. The GraphQL manifest excludes all __* directories, including test utilities. Neither condition received test suites, expected answers or rubrics. Private repositories and derived private data were excluded. No target builds, module execution or dependency installation occurred.

## Per-case outcomes

Each row sums two independent sessions with the same task. Quality scores are out of 16 semantic points. The exact tuple scorer checks file, lexical name, kind and 1-based declaration start line.

| Case | Condition | Declaration tuples | Semantic points | Total tokens | Estimated credits | Actor seconds |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| deepagents | baseline | 22/22 | 16/16 | 763,522 | 10.15430 | ~223.78 |
| deepagents | treatment | 22/22 | 16/16 | 862,025 | 11.39141 | 240.15 |
| google | baseline | 12/12 | 16/16 | 544,272 | 10.21632 | 286.73 |
| google | treatment | 12/12 | 16/16 | 610,763 | 9.65155 | 338.65 |
| graphql | baseline | 18/18 | 15.5/16 | 498,428 | 8.60764 | 209.60 |
| graphql | treatment | 18/18 | 16/16 | 486,057 | 7.73757 | 223.93 |

## Actual graph use

The registered TreeSitterTool ran through the existing SDK extractor. Its exact StandardizedOutput was imported through WriteCoordinator/GraphWriter and fully read back from Neo4j. Each case then used database-enforced read-only operation, including a rejected write probe. The treatment used production deepthought.graph_mcp.create_server over Streamable HTTP and the same GraphQueryService used by the engine. Raw graph JSON was excluded from actor contexts.

All six treatment sessions made meaningful declaration queries. Every treatment has a successful queryGraph native response whose query and response hashes match an actual server-observed Neo4j read bound to the expected input, run, repository, snapshot and content hash. Thirteen reads returned 36,937 canonical JSON bytes and took 6.794 seconds inside the actor intervals. Additional context reads account for more server events than top-level MCP calls.

All six matched-pair transport/structural comparisons were accepted. That acceptance does not determine semantic quality or establish benefit. Public per-run mcp-evidence.json files retain successful and failed DeepThought requests. Native receipts retain complete terminal usage and full-trace hashes. Full raw traces remain local for audit. Account connector inventory contents were excluded from the public export.

## Independent tool correctness

| Corpus | Independent source reference | Exact graph matches | Findings |
| --- | ---: | ---: | --- |
| DeepAgents, CPython ast | 840 | 840 | No missing or extra declaration tuples. |
| Google, CPython ast | 1,094 | 1,094 | No missing or extra declaration tuples. |
| GraphQL.js, TypeScript compiler 5.9.3 | 1,592 | 1,587 | Five named function expressions omitted. No extra graph facts or line mismatches after source review. |

The TypeScript reference intentionally includes named function expressions, a broader named-symbol set than the frozen extractor’s declaration profile. The five omissions are Queue.forEachBatch.wrapper, the three memoizeN.memoized functions, and createInputObjectDefaultValueCircularRefsValidator.validateInputObjectDefaultValueCircularRefs. Their exact source locations are retained in graphql/independent-conformance-final.json. All nine task targets remained unchanged and exact. A succeeded declaration profile therefore does not imply complete named-symbol coverage.

The initial independent compiler checker missed 29 valid computed member names. Source review confirmed them and the checker was corrected generically. Original, reviewed and final receipts are preserved. Temporary checker controls also verified source spellings and repeated same-line tuples. These corrections changed neither the production extractor nor the locked tasks.

## Evaluation validation

- Independent reviewers authored source-backed targets and eight semantic criteria per task before reading tool output or agent answers. The default declaration scorer was reused through a trusted registered grader.
- Perfect controls passed and missing, wrong-line, fabricated and unsupported-relationship controls failed for every task.
- The semantic scorer detected the plausible Vertex json_schema alias error in a scripted source-cited answer. The otherwise correct control scored 8/8 and the altered control 7/8. Scripted controls are excluded from native usage and performance totals.
- Reviewers received shuffled answer aliases without performance data or mappings. Answer prose disclosed graph availability, so complete arm blinding was not achieved. Per-point excerpts, deductions and source anchors remain available.
- One original DeepAgents rubric summary repeated an overbroad docstring assertion about symlinks. All four answers correctly identified the implementation qualification. The reviewer documented the source correction and applied the same criterion to every answer without penalizing the correct qualification. The locked original rubric is preserved.
- An independent audit reconciled record IDs, answer hashes, native usage, review mappings, cost arithmetic and all six native/server query matches.
- The final driver rejects raw-context preparations and mismatched resume settings before spending model tokens. Its two failure-path controls passed.
- All 31 installed production modules matched the working branch by SHA-256. All 209 installed/live tests passed with Neo4j and public-source opt-ins. Offline tests passed with 24 expected opt-in skips. Ruff lint/format and JavaScript syntax checks passed.

## Cost and time accounting

Credit-equivalent estimates use the documented Standard GPT-6.1 Sol rates of 50 credits per million uncached input tokens, 2.5 per million cached input tokens and 250 per million output tokens. Formula and assumptions are preserved in cost-model.json. Invoiced dollars, marginal subscription spend, reviewer/coordinator usage and infrastructure charges were unavailable. These estimates apply only to the twelve measured actor sessions. [Official Codex pricing](https://learn.chatgpt.com/docs/pricing#token-rates)

Graph preparation, including source verification, extraction, import and readback, took 15.48 seconds for DeepAgents, 11.48 for Google and 14.88 for GraphQL. Those costs were shared by the two treatment sessions in each case and used zero analysis-model tokens. The table reports actor wall time separately. Database/container provisioning, read-only restarts, MCP startup and grading are excluded from actor time. The database query time is already inside actor time and must not be added again.

## Failure points and interpretation

- The declaration graph helped locate symbols but supplied no runtime semantics, resolved call chains, inheritance proof or data flow. Agents still read source for the behavioral review. Graph-assisted runs issued 90 shell commands and returned 996,035 shell-output bytes, compared with 87 commands and 919,253 bytes in the baseline. These counts do not show a reduction in source investigation.
- Two valid read queries were rejected because the conservative keyword scan matched the string literal .delete. A third request used reserved snapshot parameters. Both searchCodeElements requests for delete also returned no matches. All failures and subsequent work remain in usage/time totals. Their individual contribution to overall cost was not isolated.
- The first driver version rejected the native cache_write_input_tokens receipt field after a completed baseline. The parser was corrected to retain full native receipts and forward only supported portable usage fields. Recovery preserved the same answer/model/settings, with the timing limitation above. A recovery-only metadata mismatch was caught by the matched-input guard and corrected without another agent call.
- Three baseline sessions made six ambient MCP catalog-discovery calls. They returned connector inventory metadata rather than task code or graph facts. Costs remain included. Both conditions had the same native ambient discovery capability. The actor filesystem boundary does not suppress those native metadata tools.
- Provider caching was uncontrolled. The treatment’s larger cached-input share explains why more total tokens can coexist with a near-tie credit estimate. The 0.68% observed credit difference does not establish stable cost savings.
- This is a known public pilot with three tasks and two sessions per condition per task. The rubric is near its ceiling. Semantic scores cover the locked criteria and do not certify every additional proposition in lengthy answers. Tool-use references in the answers weaken reviewer blinding.
- The conformance checks cover declaration tuples and storage readback. They do not establish semantic equivalence of a refactor, microservice architecture coverage, TSX effectiveness or general performance across applications/languages.

## Evidence and reproduction

Use summary.json for structured totals and runs.csv for all individual measurements. Each case directory contains records, actual answers, native usage receipts, MCP/server evidence, matched comparisons, source conformance and per-criterion review. review-mapping.json records the post-review unblinding. The original pre-run rubrics and scenario manifests are under evals/scenarios/tree_sitter/refactor. The public runner and independent checkers are under tools. See the scenario README for exact preparation and invocation instructions.

The original source-search baseline remained available with Python ast, ripgrep and the pinned TypeScript compiler. All sessions used native Codex 0.166.0, gpt-6.1-sol, high effort, a 420-second deadline, the same read-only source namespace and B–T–T–B order per task. The installed SDK and graph implementation were preserved.
