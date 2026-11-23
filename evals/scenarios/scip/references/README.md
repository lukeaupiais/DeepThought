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

## Results

Run results are not stored in this repository. A run directory holds model
transcripts, graph query receipts and usage, and is kept with the operator's
evidence. Report a result only with its run directory, grader version
(`scip-reference-locations-v1`) and the exact package and model versions.
