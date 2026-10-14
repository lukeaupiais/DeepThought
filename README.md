# DeepThought
Standalone code intelligence and analysis engine for IDEs, development tools, and AI agents.

This local alpha extracts named declarations from Python and TypeScript/TSX,
ingests definitions and references from supplied SCIP artifacts, and composes
portable reference graphs. It retains ranges, native symbol identities, role
flags, coverage, diagnostics and source snapshots. It does not infer calls,
instantiations, inheritance or architectural roles from reference occurrences.

Provision dependencies separately:

```sh
pip install '.[python,typescript,scip]'
deepthought analyze ./workspace --repository-id my-project --language python
deepthought ingest-scip ./index.scip --repository-id my-project
deepthought compose ./analysis.json ./dependency-analysis.json
```

For an index rooted below the repository root, use `--path-prefix src/package`
on `ingest-scip` (SDK: `path_prefix="src/package"`). Paths in index metadata
are never used to locate source files.

The SDK exports `analyze_repository`, `ingest_scip`, `compose_graph`,
`AnalysisResult` and `GraphSnapshot`:

```python
from deepthought import compose_graph, ingest_scip

source = ingest_scip("index.scip", repository_id="my-project")
graph = compose_graph([source])
for definition in graph.lookup_definitions("helper"):
    references = graph.references_to(definition.id)
dependencies = graph.dependencies("my-project", "src/main.py")
print(graph.resolution_counts)
```

Graph lookup accepts an exact display name or full native symbol. Resolution
uses only full native symbols; local symbols are scoped to their repository and
document. Multiple targets remain explicitly ambiguous. Dependency edges link
different files only when a reference has exactly one target, and retain their
reference witness IDs. They describe references, not proven execution or calls.

Select one full analysis artifact per repository; multiple artifacts with the
same repository ID are rejected to avoid silently mixing snapshots. Syntax-only
analysis does not resolve references or claim reference coverage. Unresolved or
ambiguous references, incomplete source results, or missing reference capability
prevent a graph from reporting complete success. Queries currently scan the
in-memory graph; composition and serialization retain supplied facts locally.

Results support `to_json()` and their respective `from_json()` methods. Analysis
schema 2 adds references; schema 1 imports as definitions-only. Graph schema 1
includes source artifacts and validates projections against those facts on import.
Composer contract 1 is recorded explicitly and is independent of package versions.
Exit codes: 0 for complete
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
SCIP local identities also depend on the producing indexer's IDs. References
absent from a supplied index cannot be reconstructed by graph composition.

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
