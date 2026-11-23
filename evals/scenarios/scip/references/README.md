# Public SCIP reference pilot

These two frozen cases investigate renaming public exports in GraphQL.js at
`c719a39547635991e621518d056b5e91026bdcda`. Each exposes 176 production TypeScript
files, three configuration files and the same installed public dependencies.
Own declarations, unrelated same-spelling helpers, comments and string examples
are excluded from reference answers. Imports and re-exports are included.

This is a bounded semantic-reference inventory pilot. It does not establish
whole-system architecture understanding or safe arbitrary refactoring.

## Provision and qualify

Use a caller-provisioned checkout at the pinned revision. Install its locked
dependencies with `npm ci --ignore-scripts`. Provision scip-typescript 0.6.0 and
a compatible Node runtime separately. The pilot used Node 24.22.2 for indexing
and TypeScript 6.0.3. DeepThought performs no automatic installation.

```sh
pip install '.[python,typescript,scip,neo4j,mcp]'
deepthought index-typescript "$PUBLIC_WORKSPACE" \
  --node "$INDEX_NODE" --indexer "$SCIP_TYPESCRIPT_MAIN_JS"
node tools/scip_source_oracle.cjs "$PUBLIC_WORKSPACE" "$PRIVATE_OUTPUT/oracle.json"
node tools/scip_rename_controls.cjs "$PUBLIC_WORKSPACE" "$PRIVATE_OUTPUT/rename-controls.json"
python tools/check_scip_eval.py "$PRIVATE_OUTPUT/oracle.json" \
  evals/scenarios/scip/references/*.scenario.json
```

The oracle reads the compiler's public export identity and source occurrences
without reading SCIP or DeepThought output. The second script compiles actual
in-memory whole-project renames, then deliberately omits a true call site.
Positive controls must compile and negative controls must fail. Both use the
same compiler family as the indexer, which limits their independence.
Neither script modifies the target sources.

Keep oracle outputs, scenarios, prepared files and grader code outside the
evaluated agent's filesystem. Scenarios contain private-to-the-agent gold facts.

## Prepare actual Neo4j conditions

Provision two dedicated databases with the same resource limits. Set
`DEEPTHOUGHT_NEO4J_*` credentials for the relevant database before each command.
Preparation verifies the full stored graph against the extraction output.
Repeat for the second case. Store every attempt and preparation duration.

```sh
python tools/prepare_scip_eval.py \
  evals/scenarios/scip/references/getNamedType.scenario.json \
  "$PUBLIC_WORKSPACE" "$PRIVATE_OUTPUT/tree.prepared.private.json" --condition tree
python tools/prepare_scip_eval.py \
  evals/scenarios/scip/references/getNamedType.scenario.json \
  "$PUBLIC_WORKSPACE" "$PRIVATE_OUTPUT/scip.prepared.private.json" --condition scip
```

After preparation, configure both databases to reject writes and verify that a
write probe fails. Keep credentials in operator-only environment files. The
runner exposes the production MCP query service with a read-only tool allowlist.
It depends on the dedicated database's enforced read-only state. Never run it
against a shared production graph.

## Run native agents

Use an installed DeepThought environment, Bubblewrap and a compatible native
Codex installation. `--native-root` names the platform vendor directory
containing `bin/codex`. `--typescript` names the checkout's TypeScript package.

```sh
python tools/run_scip_eval.py \
  --tree-prepared "$PRIVATE_OUTPUT/tree.prepared.private.json" \
  --scip-prepared "$PRIVATE_OUTPUT/scip.prepared.private.json" \
  --tree-env "$PRIVATE_OUTPUT/tree.env" --scip-env "$PRIVATE_OUTPUT/scip.env" \
  --output "$PRIVATE_OUTPUT/runs" --native-root "$CODEX_NATIVE_ROOT" \
  --typescript "$PUBLIC_WORKSPACE/node_modules/typescript"
```

The fixed order is baseline, TreeSitter, TreeSitter+SCIP, TreeSitter+SCIP,
TreeSitter, baseline. Every actor is a fresh session. Within each block the same
fresh baseline is compared to both graph conditions, so it counts once in
aggregate usage. The baseline retains search and the TypeScript compiler.
Apps, plugins and memories are disabled equally. Graph use is optional and
recorded. A completed treatment without observed graph reads cannot establish
graph-assisted effectiveness. Incomplete attempts stop the runner and remain
in the evidence. There are no automatic model retries.

Grade exact reference ranges, separately report input, cached input, output,
total tokens, estimated credits and elapsed time, and retain graph query
receipts. Include indexing, import, refresh and failure overhead separately.
Do not infer an enterprise-wide speedup from two tasks and two sessions per
condition. Do not count cached tokens or reasoning tokens twice.

## Compact-response and binding refinement

The optional four-condition mode reuses a frozen v1 graph for expanded and
compact response conditions. The fourth condition uses a separately prepared
composer-v2 graph. The order becomes baseline, expanded-v1, compact-v1,
compact-v2, compact-v2, compact-v1, expanded-v1, baseline. Both graph services must
reject writes, and all preparations must have exactly identical baseline
contexts. Each condition retains ordinary compiler access and optional graph use.

Add these options to the native-run command for the expanded control.

```sh
  --raw-prepared "$PRIVATE_OUTPUT/v1.prepared.private.json" \
  --raw-env "$PRIVATE_OUTPUT/v1.env" \
  --raw-server-python "$ARCHIVED_V1_VENV/bin/python"
```

For this mode, `--tree-prepared` and `--tree-env` also select that same frozen v1
SCIP graph, served by the current compact interface. `--scip-prepared` selects
composer v2. The legacy internal names `tree` and `scip` correspond to
compact-v1 and compact-v2 and are identified in each run plan. The expanded
control requires the archived original installed server artifact supplied by the
operator. It is identified by package-file hashes in the refinement evidence.
The baseline is shared within each four-condition block and counted once.

The runner records per-tool text payload sizes. Its current native CLI emits
session-terminal usage only. The runner marks that limitation explicitly and
makes no request-level token attribution claim.

## Source compiler evidence comparison

The optional reference-evidence profile augments the same SCIP AnalysisTool with
compiler-symbol/alias reference sets and module export membership. Provision the
TypeScript 6.0.3 package and explicit Node binary, then add these preparation
options for the new condition.

```sh
  --reference-node "$INDEX_NODE" \
  --reference-typescript "$PUBLIC_WORKSPACE/node_modules/typescript" \
  --verification-timeout 180
```

The longer verification budget accommodates full stored-graph readback. Every
node and relationship still has to match extraction output. Configure all five
`DEEPTHOUGHT_REFERENCE_*` bindings from the main README in the new condition's
operator-only environment file so the MCP server can check current inputs.

For a comparison against the previous compact interface, select its prepared
SCIP graph with `--tree-prepared` and its archived installed server with
`--tree-server-python`. Name the conditions explicitly using
`--tree-label compact-v2 --scip-label source-evidence-v4`. These are condition
labels for this driver. The underlying tools remain TreeSitter and SCIP.

The preparation handoff advertises direct public-export lookup, source evidence
and actual available capabilities. Compiler access, actor tasks, answer labels,
grading and source verification remain available in every applicable condition.
Report agent-authored compiler scans separately from model-free compiler work
performed by DeepThought during preparation. Include failed preparation attempts
and distinguish reused verified graph preparations from new work.
