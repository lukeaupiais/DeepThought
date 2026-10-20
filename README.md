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

### Recorded source consistency (SDK)

```python
from deepthought import AnalysisResult, check_source_consistency

source = AnalysisResult.from_json(saved_analysis_json)
check = check_source_consistency(workspace, source, max_files=1000,
                                 max_total_bytes=10_000_000)
print(check.to_json())
```

This separate, read-only operation compares recorded Tree-sitter SHA-256 source
hashes with files under a host-selected workspace. Its result identifies the
repository, source snapshot, and `scope: "recorded_files"`. Each file reports
`unchanged`, `changed` (digest mismatch or missing), or `unverified` with a reason.
The aggregate is `changed` if any comparison establishes a difference; otherwise
it is `unchanged` only when nonempty recorded coverage was fully compared and all
files match. Empty coverage, missing source hashes, unsupported analyzers, limits,
read errors, detected concurrent mutation, and cancellation cannot establish an
unchanged result. An aggregate difference may coexist with unverified files;
inspect individual checks, `recorded_files`, and `cancelled` for completeness.
SCIP document hashes are never treated as source hashes, even with embedded text.

This is **not a workspace freshness certificate**: new files are not discovered,
analysis coverage/status is not upgraded, and comparisons are sequential rather
than an atomic workspace view. Source can change again after each comparison.
Historical queries continue to return `workspace_freshness: "unverified"`.
Hosts retain responsibility for trusted artifact provenance, repository-to-root
assignment, complete capture, source navigation authorization, isolation and hard
deadlines. The checker neither executes an indexer nor reads repository config.

The first implementation supports Linux descriptor-relative reads. Every source
path component is opened without following symlinks; only regular files are read.
The workspace root is pinned for the check and must be a directory, not a symlink;
its parent path and authorization are the host's responsibility. Invalid limits
or an inaccessible eligible workspace root raise `ValueError`/`OSError`.
`max_files` bounds returned checks (compare their count with `recorded_files`),
`max_file_bytes` bounds each read and `max_total_bytes` bounds total content reads,
with at most one additional sentinel byte to detect growth past a byte limit.
Cancellation is cooperative between reads. This SDK operation introduces no new
mandatory dependency or changes to analysis/graph artifact schemas.


### Recorded source consistency (CLI)

```sh
deepthought check-source ./workspace ./analysis.json --repository-id my-project
```

The CLI loads one saved **analysis** artifact (not a composed graph), verifies
that its repository ID matches the required `--repository-id`, and invokes the
SDK checker. This ID check does not authorize a workspace or authenticate an
artifact; hosts still assign the trusted root and artifact. The result is exactly
the SDK's source-consistency JSON plus a newline, retaining its recorded-files
scope and snapshot identity. Historical query freshness remains unverified.

Exit codes for `check-source`: **0** means all nonempty recorded coverage matched;
**1** means changed or unverified (including cooperative cancellation); **2** means
invalid input, unreadable artifact/root, repository mismatch, or an output-budget
failure. SIGINT and SIGTERM request cooperative cancellation, with `cancelled`
retained in the result; cancellation does not erase differences already found.
Previous signal handlers are restored before returning from the command.

Defaults are `--max-artifact-bytes 50000000`, `--max-files 10000`,
`--max-file-bytes 2000000`, `--max-total-bytes 100000000`, and
`--max-output-bytes 32768`. All input/read limits must be positive; the output cap
accepts 1024–32768 bytes and counts UTF-8 bytes plus the final newline. File and
source-byte limits retain the SDK's incomplete-check semantics and one-byte growth
sentinel. If the complete JSON cannot fit, the command exits 2 with stderr and no
stdout; it does not truncate evidence. The input/read/output limits do not provide
a hard memory or execution-time bound. The command works without optional grammar
or SCIP dependencies and performs no indexer, model, or network calls.
