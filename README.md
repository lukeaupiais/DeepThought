# DeepThought
Standalone code intelligence and analysis engine for IDEs, development tools, and AI agents.

This local alpha extracts named declarations from Python and TypeScript/TSX,
and ingests definitions from supplied SCIP artifacts. It reports source ranges,
file coverage, diagnostics and snapshot identity. Reference/call resolution and
graph queries are not supported by this alpha.

Provision dependencies separately:

```sh
pip install '.[python,typescript,scip]'
deepthought analyze ./workspace --repository-id my-project --language python
deepthought ingest-scip ./index.scip --repository-id my-project
```

For an index rooted below the repository root, use `--path-prefix src/package`
on `ingest-scip` (SDK: `path_prefix="src/package"`). Paths in index metadata
are never used to locate source files.

The SDK exports `analyze_repository`, `ingest_scip` and `AnalysisResult`. Results
support `to_json()` and `AnalysisResult.from_json()`. Exit codes: 0 for complete
extraction, 1 for partial/failed/cancelled analysis, 2 for invalid command input.
SCIP source columns retain their declared encoding; Tree-sitter columns are
UTF-8 byte offsets. Lines are one-based and end positions are exclusive.
An index that omits its coordinate encoding produces partial results with an
explicit diagnostic; the engine does not guess an encoding.

The initial filesystem runtime targets Linux. Analysis does not install tools,
run repository scripts, invoke models or contact external services. Selected
extras must already be installed; missing grammars produce unavailable coverage.
The SDK accepts file/byte budgets, include/exclude globs and a cancellation
event. Cancellation is cooperative between files, not a hard per-parse deadline.
Outputs go to stdout; source files are not modified and symlinks are not followed.
Byte budgets count rejected oversized inputs too; one additional byte is read
to detect exhaustion. Snapshot identity includes completeness and diagnostics.
Definition IDs survive relocation and line shifts while symbol identity remains
unchanged. Repeated same-symbol declarations use source-order ordinals; inserting
or removing one can change the IDs of later declarations with that symbol.

Run the tests with the selected extras installed:

```sh
python -m unittest discover -s tests -v
```

For offline acceptance on Linux, provision `bubblewrap` and install the built
wheel into a virtualenv outside this checkout, then run:

```sh
python tools/verify_offline.py --python /path/to/venv/bin/python
```

CI builds the source archive and wheel, verifies identical rebuilds, and tests
full and core-only installations on Python 3.10–3.14 with networking disabled,
the checkout hidden and fixtures read-only. It retains build artifacts without
publishing a package release.

Apache-2.0. Retained source attribution and licences are in `NOTICE` and `licenses/`.
