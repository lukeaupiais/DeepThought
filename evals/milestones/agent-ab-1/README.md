# agent-ab-1 task scenarios

One `deepthought.evaluation` scenario per frozen task of milestone agent-ab-1,
graded by `tools/ab_graders.py`. Rebuild from pinned checkouts with

```sh
python tools/ab_tasks.py evals/milestones/agent-ab-1.json --cache <gtcache> --out evals/milestones/agent-ab-1
```

The builder reads blobs at each corpus pin and never runs repository code. Every
hand-checked line it relies on is asserted against the source text, so a moved
line fails the build instead of shifting the truth. Truth never comes from engine
output.

| Task | Grader | Truth | Scored rows |
| --- | --- | --- | --- |
| U1 | affected-set-v1 | contract labels for `hipstershop.CartService/GetCart`: both clients and the server | 3 |
| U2 | affected-set-v1 | contract labels for `GET /product/{sku}`: four clients (PHP client spans the sprintf at :27 to curl_init at :32) | 4 |
| U3 | affected-set-v1 | `impact/cobra.tsv`, seed command.go:1744 InheritedFlags, depth 1 to 3 | 34 |
| P1 | change-plan-v1 | `impact/chi.tsv`, seed mux.go:295 Mount: seed and depth 1 must be edited, depth 2 to 3 retested | 11 + 20 |
| P2 | change-plan-v1 | `impact/hono.tsv`, seed src/utils/html.ts:53 stringBufferToString, split as P1 | 6 + 28 |
| P3 | change-plan-v1 | contract labels for `hipstershop.CurrencyService/Convert`: both clients and the handler, all must edit | 3 |
| C1 | change-check-v1 | call truth for command.go:1219 InitDefaultHelpFlag; `random.Random(1).sample(sites, 2)` | 2 omissions, 6 covered |
| C2 | change-check-v1 | call truth for source/utils/is.ts:13 isError; `random.Random(2).sample(sites, 1)` | 1 omission, 4 covered |
| C3 | change-check-v1 | contract labels for `GET /product/{sku}`; the patch updates catalogue, cart and web only | 2 omissions, 2 covered |
| K1 | exact-fact-v1 | command.go:1084 `func (c *Command) ExecuteC() (cmd *Command, err error)` | 3 facts |
| K2 | exact-fact-v1 | exported functions of hono src/utils/url.ts, from the TypeScript AST | 12 functions |
| K3 | exact-fact-v1 | ky source/utils/normalize.ts:26 `limit: 2` | 3 facts |

Call sites for C1 and C2 are sorted as (file, line) tuples before sampling.

## Rows that are neither right nor wrong

The seed of U3 is the change itself, so it is unjudged. Contract tasks mark as
unjudged the labels' don't-care sites, the .proto file, protoc output, and for U1
the cart stores and the callers of the getCart wrappers. For P3 they also mark
the address wiring (`currencySvc*` lines) and every deployment manifest that sets
`CURRENCY_SERVICE_ADDR`, because repointing touches them too. For U2 and C3 the
catalogue handler and the nginx proxy config are unjudged. In K2,
`getQueryParam` (a cast of the module-private `_getQueryParam`) and
`decodeURIComponent_` (an alias of the global) are optional.

## Seeded patches

`patches/C*.diff` are the diffs the check tasks show the agent; the scenario's
`metadata.patch_sha256` pins each one. Each was checked at its pin:

- C1 (cobra): `go vet ./...` with the patch fails at exactly command_test.go:934 and
  doc/man_docs.go:204, and passes once those two lines are renamed.
- C2 (ky): `tsc --noEmit -p tsconfig.json` passes at the pin, fails with the patch
  at exactly source/core/Ky.ts:785 (TS2554), and passes once that call gets its
  second argument. Ky's tests do not call `isError`.
- C3 (robot-shop): no compiler checks a URL. After the patch, the only references
  to the old route outside the AngularJS page routes are the two omissions,
  ratings/html/src/Service/CatalogueService.php:27-32 and load-gen/robot-shop.py:68.
