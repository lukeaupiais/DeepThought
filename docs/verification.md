# Verification

The engine's tests show that each extraction rule does what its author meant.
Verification checks the engine from the outside, against properties and
oracles that do not depend on how a rule is written. Each check below is
re-runnable, and its numbers come from a command anyone can repeat.

## Determinism

Same input, same graph, byte for byte. `tools/check_determinism.py` copies a
workspace twice, once in sorted and once in reverse file-creation order under a
different absolute root, and analyzes each copy in a fresh interpreter with a
different `PYTHONHASHSEED` and working directory. Each run emits the analysis
artifact, every call, import, supertype and impl resolution, the composed
reference graph and the standardized output the graph store receives. Only the
store output's wall-clock fields (`execution_time_seconds`, `timestamp`) are
removed. The dumps must be byte-identical; otherwise the tool prints the first
differing JSON path.

```sh
python tools/check_determinism.py WORKSPACE --language rust          # 4 runs
```

`tests/test_determinism.py` runs two variants on a polyglot workspace that
links calls in all six languages, so CI fails on any order- or hash-dependent
output.

Results on real repositories (4 runs each, all byte-identical):

| Repository | Commit | Language | Dump bytes |
|---|---|---|---|
| spf13/cobra | `adbc881` | go | 6,608,612 |
| google/gson | `d9b5079` | java | 46,577,768 |
| dtolnay/anyhow | `c89c9b8` | rust | 2,192,242 |
| BurntSushi/ripgrep | `4a51093` | rust | 25,031,175 |
| pallets/click | `08b803c` | python | 14,929,192 |
| sindresorhus/execa | `6f856d5` | javascript | 31,415,744 |
| DeepThought `src/` | `040a3e1` | python | 5,943,195 |

## Ground-truth corpus

Pinned real repositories with expected call edges derived from a compiler or
language server, checked in under `evals/ground_truth/`, so the engine is
scored against a yardstick its own rules cannot move.

- `manifest.json` pins each repository to a commit and records the oracle, its
  version and the command that produced its SCIP index.
- `calls/NAME.tsv` is the truth, one row per target:
  `file line name target_file target_line kind`. A call is a reference
  occurrence the source text shows is applied to an argument list (`name(`,
  `name::<T>(`, `name<T>(`) and is not a declaration. Its kind says where the
  oracle says the name leads: `callable` (an in-repository function, method or
  type, at each defining line; a constructor also accepts its class), `value`
  (another in-repository symbol, such as `const f = () => ...`), `local` (a
  parameter, loop variable or local function, at its binding line) or
  `external` (a library or builtin). `# indexed` rows list the files the
  oracle indexed.
- `baseline.json` is the current score of each corpus.

Java has no SCIP indexer in this setup, so `derive-java` takes the truth from
javac's own attribution (`tools/JavaCallTruth.java`, JDK only): every method
invocation and `new` expression javac binds, at the line of the called name,
targeting the line of the declaration's name. A `new` expression accepts its
constructor and its class. Calls javac cannot bind (a missing library) are left
out.

An engine link matches a target when it is on the target's line or is the
innermost declaration whose span holds that line: the engine records a Java
declaration at its first annotation or modifier, the oracles at its name.

Every engine call resolution is judged at its source position:

| Outcome | Meaning |
|---|---|
| confirmed | linked to a target the oracle names |
| contradicted | linked, and the oracle names other targets (including `local` or `external`) |
| unjudged | linked where the oracle records no call (for example code under a `#[cfg]` the oracle did not enable) |
| not_indexed | linked in a file the oracle did not index |
| missed | an expected call (one with a `callable` target) without a confirmed link, broken down by the engine's outcome at that position (`no_call_site` means no call site was extracted there) |

Precision is confirmed / (confirmed + contradicted). Recall is the share of
expected calls with a confirmed link; `value` and `local` links count toward
precision only, so confirmed can exceed expected.

```sh
python tools/ground_truth.py check                   # fetch, score, compare with baseline
python tools/ground_truth.py check --corpus click    # one corpus
python tools/ground_truth.py check --update-baseline # accept a new score
python tools/ground_truth.py score WORKSPACE TRUTH --language python
python tools/ground_truth.py derive WORKSPACE INDEX > TRUTH   # after the oracle command
```

`check` exits 1 when a corpus has more contradictions or fewer confirmed links
than its baseline, or when its truth file no longer matches the baseline's
expected count. An improvement is accepted by updating the baseline in the
same change. The whole corpus fetches and scores in about ten seconds.

Scores are also broken down by the evidence rule that made each link, and
`check` fails when any single rule loses a confirmed link or gains a
contradiction, even if the totals hold. Its `rule_coverage` output lists, for
every call rule, confirmed and contradicted links across all corpora, and
names the rules with no confirmed link: breaking one of those would go
unnoticed here. Today there are none; every call rule has at least 8
confirmed links:

| Rule | Confirmed | Contradicted |
|---|---|---|
| tree-sitter.call-lexical-name | 969 | 1 |
| tree-sitter.call-imported-name | 484 | 0 |
| tree-sitter.call-module-attribute | 18 | 0 |
| tree-sitter.call-self-method | 165 | 0 |
| tree-sitter.call-package-name | 566 | 0 |
| tree-sitter.rust-call-path | 1,831 | 0 |
| tree-sitter.rust-call-associated | 1,123 | 0 |
| tree-sitter.rust-call-self-method | 580 | 0 |
| tree-sitter.rust-call-local-item | 35 | 0 |
| tree-sitter.java-enclosing-type-method | 1,529 | 0 |
| tree-sitter.java-type-name | 2,379 | 0 |
| tree-sitter.java-type-member | 391 | 0 |
| tree-sitter.java-inherited-method | 19 | 0 |
| tree-sitter.java-static-import | 8 | 0 |

Before a contradiction is reported as an engine defect it is checked against
the source: the oracle can be misconfigured too. For example, scip-python
resolves `click` in click's own tests to an installed copy unless the
repository is installed in the indexing environment, which the manifest's
command does.

Baseline (calls):

| Corpus | Language | Expected | Confirmed | Contradicted | Precision | Recall |
|---|---|---|---|---|---|---|
| click | python | 3,313 | 927 | 1 | 0.9989 | 0.2756 |
| ky | typescript | 143 | 225 | 0 | 1.0 | 0.2587 |
| cobra | go | 2,197 | 1,044 | 0 | 1.0 | 0.4752 |
| anyhow | rust | 444 | 223 | 0 | 1.0 | 0.491 |
| ripgrep | rust | 11,676 | 3,346 | 0 | 1.0 | 0.2865 |
| gson | java | 11,139 | 4,332 | 0 | 1.0 | 0.3824 |

Recall is low by design where calls need a receiver's type (`x.m()`,
`c.Root().Name()`), which name-based resolution does not infer. The largest
missed groups are:

- click: 1,717 calls of a package attribute (`click.echo(...)`) whose
  declaration is re-exported by the package's `__init__.py`
  (`module_attribute_not_declared`); 541 method calls on values.
- cobra: 1,115 method calls on values (`attribute_call`).
- ky: 97 calls with no extracted call site, mostly `this.#private()` methods.
- ripgrep and anyhow: calls with no extracted call site (5,944 and 154),
  mostly method chains and calls inside macro invocations such as
  `assert_eq!(...)`, and 2,152 and 65 method calls on values.

The one contradiction is a real defect: in `examples/imagepipe/imagepipe.py`
line 35, `processor(stream)` calls the loop variable of
`for processor in processors`, and the engine links it to the module-level
function `processor` defined on line 42.

## Metamorphic checks

An edit whose effect on the graph is known in advance must change exactly
that. `tools/check_metamorphic.py` applies a transformation to a copy of a
workspace, analyzes both, and compares every call, import, supertype and impl
resolution (keyed by the position and text of its fact) with the original
mapped through the predicted effect:

| Transformation | Prediction |
|---|---|
| `shift FILE N`: prepend N blank lines | positions in FILE and targets declared in FILE move down N lines; nothing else changes |
| `rename OLD NEW`: replace a called function or method name everywhere, same length | the original graph with OLD replaced by NEW |
| `add FILE`: a file whose declarations use names found nowhere else | every original resolution is unchanged; an import of a Go package also links the new file |

```sh
python tools/check_metamorphic.py WORKSPACE --language go --trials 9 --seed 0
```

`tests/test_metamorphic.py` runs all three on the polyglot workspace with three
seeds and checks that a shift predicted wrongly is caught.

Results (every resolution as predicted):

| Repository | Language | Resolutions | Trials |
|---|---|---|---|
| click | python | 7,679 | 9 |
| ky | typescript | 13,275 | 9 |
| cobra | go | 4,301 | 9 |
| gson | java | 21,531 | 6 |
| anyhow | rust | 997 | 6 |
| ripgrep | rust | 11,843 | 6 |

Rust has no `add` trial: a new module needs a `mod` line elsewhere, so it is
not an unrelated edit.
