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

For bounded machine results, use the query interface on a composed graph:

```python
from deepthought import QueryRequest, query_graph

result = query_graph(graph, QueryRequest("references", "my-project", symbol="helper", limit=20))
print(result.to_json())
```

The installed CLI accepts a graph artifact:

```sh
deepthought compose ./analysis.json > graph.json
deepthought query graph.json --repository-id my-project --operation references --symbol helper --limit 20
```

Query schema 1 returns source and graph snapshot IDs, analysis/graph/query status,
coverage counts, diagnostics and exact match totals. `repository_id` scopes the
origin of returned facts; reference targets may belong to other repositories in
the supplied graph. Display-name lookup selects candidates, while only native
identities resolve references. Definitions retain their graph identity and a
nested `definition` with the source location. References retain resolution and
target IDs; dependencies retain their reference witness IDs.

Every query exposes `workspace_freshness: "unverified"` in the SDK and JSON.
`status` describes the supplied facts and their coverage; it does not establish
that source files still match the snapshot. Queries perform no workspace I/O.
A source edit changes the snapshot on reanalysis; a file rename changes its file
identity, and a deletion removes its definitions. Saved graphs continue describing
their original facts. Source capture is not an atomic whole-workspace transaction.

SCIP ingestion reads only the supplied index, including when document text is
embedded. Its coverage digests identify serialized SCIP documents, not source-file
bytes. An unchanged index yields the same facts after source edits, renames or
deletions. Neither snapshot IDs, modification times, successful resolution nor
embedded text alone are a workspace-freshness check. Hosts must obtain separate
source-consistency evidence before presenting results as verified against a
current workspace or using locations for navigation. This field is an additive
query-schema-1 qualification; analysis and graph schemas are unchanged.

The default result limit is 20, with a maximum of 100. Results follow canonical
graph order and report `truncated` when only a prefix fits. Each match includes
at most 20 target/witness IDs with its full count and a separate truncation flag.
At most 10 diagnostics are returned, with their full count and truncation flag.
`--max-output-bytes` (SDK: `max_output_bytes`) accepts 1024–32768 bytes, defaulting
to 32768, and includes the CLI newline. Metadata and at least the first match must
fit; otherwise the SDK raises `ValueError` and the CLI exits 2 without stdout.
No evidence strings are silently shortened. These bounds cover the response;
queries still scan the in-memory graph to count matches. Hosts retain execution
deadlines, input budgets and workspace authorization.

Definition queries can succeed on complete syntax-only input even though the
graph reports partial reference coverage. Reference/dependency queries without
the source reference capability return `unavailable` (CLI exit 1). Partial,
failed and cancelled source states remain visible. Existing graph lookup methods
continue returning their full tuples; query results do not change graph artifacts.

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
