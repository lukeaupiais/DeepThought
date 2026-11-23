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
