# Alias and re-export control

Owned source and a real scip-typescript 0.6.0 index exercise an overloaded
export, imported aliases, a renamed re-export and an unrelated same-named
helper. The index and its receipt were written by `deepthought
index-typescript` from the root `/fixtures/scip_alias_reexport`; the receipt
binds the index bytes to the five source and configuration files.

`tools/typescript_reference_oracle.cjs` (TypeScript 6.0.3) finds eight
references to the exported `alpha`. The index assigns seven of them to that
symbol. The `outward` import name in `c.ts` is recorded as an undefined
`local 2`, while the aliased consumer binding and call in `c.ts` refer to
`alpha`. DeepThought must keep that import unresolved and the graph status
partial; it must not link it by name. The fixture checks that uncertainty is
preserved, not that the index covers every reference.
