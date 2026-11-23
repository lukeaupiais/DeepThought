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
