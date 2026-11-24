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

`check` refuses to score (exit status 2) when a grammar or the YAML reader a
selected corpus needs is not installed. Without them the engine reports files
unavailable and gateway routes unread instead of failing, so the scores would
drop as if it had regressed. Install the package with its `languages` extra.

### Inheritance

`inherits/NAME.tsv` holds expected declared supertypes in the same format, keyed
by each subtype's defining position and name, with kind `type` (an
in-repository supertype), `external`, or `unknown` (a symbol of the
repository's own package the index never defines, such as a class local to a
function, which is not judged). Python and TypeScript truth comes from the SCIP
index's implementation relationships (`derive-inherits`), reduced to direct
edges because indexers differ in listing ancestors; Java truth comes from javac
(`derive-java --bases`). Go (scip-go records implicit interface satisfaction,
which the engine does not model) and Rust (rust-analyzer records no
relationships) have none yet.

Every INHERITS_FROM link whose subtype appears in the truth is confirmed or
contradicted. An expected edge counts as found when the engine's links reach
it, since an indexer can omit an intermediate edge (scip-typescript records no
interface `extends`).

| Corpus | Expected | Confirmed | Contradicted | Precision | Recall |
|---|---|---|---|---|---|
| click | 92 | 43 | 0 | 1.0 | 0.4674 |
| ky | 5 | 5 | 0 | 1.0 | 1.0 |
| gson | 210 | 210 | 0 | 1.0 | 1.0 |

The 49 bases missed on click are all `class X(click.Group)`-style bases named
through the package's `__init__.py` re-exports, the same gap as for calls.

### C and C++ declarations

The engine reads C and C++ declarations but no calls yet, so these corpora
check declarations. `tools/c_declaration_truth.py` runs clang (18.1.3) on each
translation unit and records every named function, method, record, enum,
typedef, alias and namespace written in a repository file, at the line of its
name and the line it begins. It leaves out implicit declarations, template
instantiations, forward declarations of records and declarations local to a
function body. A declaration is *found* when the engine declares the same name
in the same file at either line with the same kind of thing; *kind_mismatch*
when only the kind differs. An engine declaration with no clang counterpart in
a file clang read is *extra*: a preprocessor branch clang did not take, or a
misread. `check` fails when `found` drops, or `kind_mismatch` or `extra`
grows.

| Corpus | Expected | Found | Kind mismatch | Missed | Extra |
|---|---|---|---|---|---|
| cJSON (C) | 261 | 261 | 0 | 0 | 3 |
| fmt (C++) | 1346 | 785 | 99 | 462 | 101 |

fmt's misses are mostly the engine not recognizing declarations that follow
a macro: namespaces opened by `FMT_BEGIN_NAMESPACE`, and class templates such
as `basic_string_view` whose members are then read as free functions (the
kind mismatches). Its extras include member initializers read as functions
(`size_(sz)` in `buffer`'s constructor) and an anonymous enum with an empty
name. cJSON's extras are three functions in a preprocessor branch clang did
not take.

### Ruby and PHP declarations

The same scorer covers Ruby and PHP, with each language's own parser as the
oracle. `tools/ruby_declaration_truth.rb` walks Ripper's syntax tree: a
`def` in a class, module or `class << self` body is a `method`, as is any
singleton `def x.m` (on `self` or another object); any other `def` is a
`function`. `tools/php_declaration_truth.php` reads `token_get_all`: a named
function in a class, interface, trait, enum or anonymous class body is a
`method`, other named functions are `function`s, and a declaration begins at
its first attribute or modifier. Closures, `X::class` and `use function`
imports are not declarations. Files that do not parse (`php -l`) are left
unindexed.

| Corpus | Expected | Found | Kind mismatch | Missed | Extra |
|---|---|---|---|---|---|
| rack (Ruby) | 1014 | 950 | 60 | 4 | 0 |
| Slim (PHP) | 876 | 863 | 0 | 13 | 0 |

rack's kind mismatches are all singleton methods defined outside a class
body (`def self.x` at the top of a file, `def object.close` in a test
block), which the engine reads as functions; its misses are singleton
methods on an instance variable or nested in another method. Slim's misses
are the methods of anonymous classes and three functions declared inside a
method body.

### Service contracts

The `contracts` entries in the manifest gate cross-service links the same way.
Their labels are the hand-labelled corpora in `evals/fixtures/service_contracts`
(see its README), built from the source without looking at the extractor.
`check` fetches each pinned repository, scores every link kind the labels use
with `tools/check_contracts_ground_truth.py` (all hops, call sites and service
pairs), and fails when a kind has fewer correct links or more wrong ones than
`baseline.json` records, or when its labels changed without a baseline update.

| Corpus | Kind | Call sites correct / predicted / labelled | Service pairs correct / predicted / labelled |
|---|---|---|---|
| apssouza22 java-microservice | http | 3 / 3 / 3 | 2 / 2 / 2 |
| apssouza22 java-microservice | messaging | 0 / 0 / 1 | 0 / 0 / 1 |
| ewolff microservice-kafka | messaging | 2 / 2 / 2 | 2 / 2 / 2 |
| piggymetrics | http | 3 / 3 / 3 | 3 / 3 / 3 |
| spring-petclinic-microservices | http | 1 / 1 / 14 | 1 / 1 / 3 |
| online-boutique | grpc | 20 / 21 / 20 | 12 / 12 / 14 |
| online-boutique | http | 9 / 9 / 9 | 1 / 1 / 1 |
| robot-shop | http | 4 / 4 / 42 | 3 / 3 / 17 |
| robot-shop | messaging | 1 / 1 / 1 | 1 / 1 / 1 |

Most unmatched labels are browser or load-generator calls that reach a backend
through a gateway route. The one wrong call site is the developer test client
`AdServiceClient.java` inside adservice, which calls its own service.

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

## Mutation testing

A suite that never fails proves little. `tools/mutate_resolution.py` makes one
small change at a time to a resolution module (`==` to `!=`, `<` to `<=`, `in`
to `not in`, `and` to `or`, dropping a `not`) and checks whether anything
notices: the ground-truth corpus first (any rule with fewer confirmed or more
contradicted links, a crash or a timeout), then the unit tests of call
resolution. Analyses are computed once; each mutant only re-resolves.

```sh
python tools/mutate_resolution.py --module calls     # about 20 minutes
```

Results for `src/deepthought/calls.py` (187 mutants):

| Outcome | Mutants |
|---|---|
| killed by the corpus | 107 |
| killed by an error on the corpus | 49 |
| killed by unit tests only | 17 |
| survived | 14 |

173 of 187 (92.5%) are killed. Eight survivors change a position comparison
from `<` to `<=` (or back) where the two positions cannot coincide, such as a
call starting at the same column as the declaration that contains it, so they
are equivalent. Six are gaps nothing checks:

| Line | Function | Mutation | What is unchecked |
|---|---|---|---|
| 190 | `_JavaScope.__init__` | `and` to `or` | package facts outside a source root |
| 200 | `_JavaScope.__init__` | `and` to `or` | supertypes outside a source root or without an owner |
| 259 | `_JavaScope.type` | `and` to `or` | top-level type lookup when the scope prefix is empty |
| 273 | `_JavaScope.type` | `and` to `or` | static member-type imports after a type was found |
| 683 | `_RustPaths._declared` | `or` to `and` | a module path read in another crate root's context |
| 864 | `_RustPaths.resolve` | `not declared` to `declared` | the `not_declared` reason of unresolved Rust paths |

## Historical replay

`tools/replay_history.py` asks whether impact analysis anticipates what real
commits edited. For each recent non-merge commit that modifies 2 to 12 source
files, it analyzes the parent revision. For each edited file it seeds an
impact query with the innermost declarations the diff touches. That query
follows CALLS, INHERITS_FROM and IMPLEMENTS backwards to depth 3, the way
`get_impact` does. The files of the dependents it finds are the prediction,
and the commit's other edited files are the outcome. The baseline is a plain
text search: every file that contains one of the seed names as a whole word.

```sh
python tools/replay_history.py CLONE --language python --commits 30 --revision REV
```

Co-edited files are not the same as impacted files. A commit can touch
unrelated files, edit a callee rather than a caller, or edit tests that name
nothing it changed. A dependent may also need no edit. So the absolute numbers
are low for both predictors. The replay compares the two predictors on the
same edits; it does not judge whether a link is correct (the ground-truth
corpus does that). A commit whose diff touches no declaration in a file, such
as a test made of top-level calls, gives no case for that file.

Results for the last 30 qualifying commits up to each corpus pin:

| Repository | Language | Cases | Impact recall | Impact precision | Impact files per case | Search recall | Search precision | Search files per case |
|---|---|---|---|---|---|---|---|---|
| click | python | 76 | 0.020 | 0.263 | 0.25 | 0.052 | 0.117 | 1.46 |
| cobra | go | 71 | 0.024 | 0.160 | 0.35 | 0.083 | 0.073 | 2.72 |
| ky | typescript | 90 | 0.030 | 0.516 | 0.34 | 0.178 | 0.300 | 3.56 |
| gson | java | 102 | 0.036 | 0.084 | 1.75 | 0.043 | 0.062 | 2.83 |

In every repository impact analysis predicts fewer files at higher precision,
and text search finds more of the co-edited files. Impact recall is bounded by
call-resolution recall (0.26 to 0.48 on these corpora; see the baseline), so
it should rise as more calls resolve. The replay is a trend to track per
milestone, not a gate.

### Tracking per milestone

`tools/track_replay.py` replays every pinned ground-truth corpus (30 commits
each, up to the pinned commit) and appends one entry to
`evals/ground_truth/replay.json` with a label, the engine commit and each
corpus's scores. Run it when the graph reaches a milestone; entries are never
rewritten, so the file shows how impact recall and precision moved.

```sh
python tools/track_replay.py --label "milestone name" --cache .ground-truth-cache
```

The first entry ("graph port, 2026-11-23") adds the Rust corpora: anyhow 41
cases, impact 0.164 recall at 0.444 precision against search 0.370 at 0.202;
ripgrep 78 cases, impact 0.054 at 0.381 against search 0.331 at 0.090. A full
run takes about six minutes.
