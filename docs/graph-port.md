# Core graph storage and query port

This ports the original DeepThought graph-storage and query responsibilities
into the OSS package. The existing SDK extractor, SCIP ingestion and portable
GraphSnapshot stay unchanged. Neo4j and MCP are optional extras.

## Original implementation mapping

Upstream paths are relative to Code_to_Graph/DeepThought; original MIT attribution
is retained in NOTICE and licenses/deepthought-original-MIT.txt.

| Original | OSS implementation | Retained responsibility |
|---|---|---|
| libs/storage/.../graph/connection.py | deepthought.storage.graph.connection | Neo4j driver lifecycle |
| libs/storage/.../graph/writer.py | deepthought.storage.graph.writer | Tool-independent node/relationship batching and write statistics |
| libs/analysis/.../engine/write_coordinator.py | deepthought.storage.graph.write_coordinator | Repository management and ordered primary/supplemental tool contributions |
| libs/storage/.../graph/schema.py | deepthought.storage.graph.schema | Constraints, indexes and code_search fulltext index |
| libs/analysis/.../engine/data_access/graph_repository.py | deepthought.graph_queries | Calls/callers, imports/exports, hierarchy, paths, statistics and symbol lookup |
| libs/core/.../context/context_service.py | deepthought.graph_queries | File/line context, neighbouring relationships, fulltext search and file navigation |
| applications/unified-server/src/main.py MCP tools | deepthought.graph_mcp | getCodeContext (now with paged declaration source), getFileStructure and searchCodeElements |
| libs/storage/.../graph/updater.py and utils/git_utils.py `Repository.last_commit` | deepthought.revisions + WriteCoordinator.link_commit + GraphQueryService.list_snapshots | Each immutable snapshot lists the commits it was analyzed at, instead of one mutable last commit per repository (see below) |
| libs/storage/.../graph/updater.py file-level incremental update (`get_changed_files`, file hashes) | deepthought.engine `previous=` + `deepthought analyze --previous` + `deepthought-graph index --previous-artifact` | Unchanged files reuse an earlier analysis's facts; the result is a new snapshot equal to a full run, not an in-place update (see below) |
| libs/storage/.../graph/updater.py AST change detection (`apply_ast_diff`) | GraphQueryService.diff_snapshots | Read-only comparison of two immutable snapshots instead of mutating one graph (see below) |
| libs/analysis/.../engine/data_access/graph_repository.py calls/imports (not exposed to agents in the original) | deepthought.graph_mcp + graph_cli | getFunctionCallers, getFunctionCalls, getFileImports, getFileImporters and explainEdge (see below) |
| applications/unified-server/src/main.py `/api/v1/analysis/impact` (a chat prompt to the model) | GraphQueryService.get_impact/get_file_impact + graph_cli/graph_mcp | Bounded traversal of stored edges with each dependent's path and evidence; no model is asked (see below) |
| applications/unified-server/src/main.py /api/v1/graph/query | deepthought.graph_queries + graph_cli/graph_mcp | Cypher execution, adapted to CLI and optional queryGraph MCP transport |
| Original analyzer repository/file projection | deepthought.analysis_tools.tree_sitter | Repository -> CONTAINS -> File -> DEFINES -> declaration |
| libs/analysis/.../parsers/builders/relationship_builder.py edge `confidence` and `resolution_status` | deepthought.analysis_tools.evidence | Replaced by evidence type plus rule ID on every relationship (see below) |
| libs/analysis/.../parsers/builders/import_processor.py import tracking | deepthought.syntax.extract_imports + deepthought.imports | Static import facts, then File -> IMPORTS -> File by declared rules (see below) |
| libs/analysis/.../parsers/builders/relationship_builder.py CALLS | deepthought.syntax.extract_call_sites + deepthought.calls | Named call sites, then CALLS by name-binding rules, all `name_matched` (see below) |
| libs/analysis/.../parsers/utils/base/core.py `create_containment_relationships` (parent `CONTAINS` child by `parent_qname`) | deepthought.analysis_tools.tree_sitter + GraphQueryService.get_members/get_owner | Declaration -> CONTAINS -> nested declaration from parse-tree spans under a registered `resolved` rule (see below) |
| libs/analysis/.../parsers/utils/base/core.py `extract_inheritance_relationships` (INHERITS via `file_context.resolve_symbol`) | deepthought.syntax.extract_supertypes + deepthought.inheritance + GraphQueryService.get_base_types/get_subtypes | Declared bases, then INHERITS_FROM by the call name-binding rules, all `name_matched`; `get_class_hierarchy` follows classes and interfaces (see below) |
| libs/analysis/.../parsers/core/language_detector.py and import_processor.py JavaScript/JSX handling | deepthought.syntax `LANGUAGES["javascript"]` + deepthought.imports JavaScript rules | Opt-in JavaScript/JSX declarations, ES module imports and calls; relative specifiers resolve only to analyzed files (see below) |
| libs/analysis/.../parsers/utils/base/config.py `LanguageConfig["go"]` | deepthought.syntax `LANGUAGES["go"]` | Opt-in Go functions, receiver-named methods, named types and import facts; imports are not resolved to files (see below) |
| libs/analysis/.../tools/scip (indexing/indexer_runner.py, parsing/occurrence_processor.py) | deepthought.scip_indexing + deepthought.analysis_tools.scip + GraphQueryService.get_references/get_file_dependencies | Source-bound TypeScript index, Reference nodes, REFERENCES and file DEPENDS_ON edges with registered `resolved` rules instead of `confidence: 1.0` (see below) |
| libs/analysis/.../tools/scip/transformation/enhanced_relationship_mapper.py `is_implementation` | deepthought.analysis_tools.scip + GraphQueryService.get_implementations/get_supertypes | Symbol -> IMPLEMENTS -> Symbol under a registered `resolved` rule; the original's doc-keyword EXTENDS guess and TYPE_OF mapping are not ported (see below) |
| libs/analysis/.../tools/api_contract (string-pattern endpoint detector with confidence tiers; client matching never implemented) | deepthought.service_contracts + deepthought.analysis_tools.service_contracts | HttpRoute nodes with HANDLES and REQUESTS edges from parsed Spring annotations and client calls, linked by declared path-template rules instead of confidence scores (see below) |
| libs/analysis/.../tools/service_detector (weighted Dockerfile/compose/k8s/serialization/modularity score and `confidence`) | deepthought.components + deepthought.analysis_tools.components + GraphQueryService.list_components/get_file_components/get_component_files | Components that build manifests declare and File -> PART_OF -> Component by nearest root, all `resolved`; no score, port guess or clustering (see below) |

The CLI/MCP adapters call this shared query implementation. They do not implement
another analyzer or a separate query engine. The original hosted authentication,
Kubernetes, chat/model providers, UI, billing, other analyzers and vector/cache
backends are outside this port. It does not merge unrelated pending OSS PRs.

## Deliberate compatibility adaptations

The original orchestrator writes primary and supplemental tools into one shared
repository graph. `WriteCoordinator.write_outputs(outputs, repo_url,
snapshot_id=graph_revision)` retains that mechanism: shared nodes merge labels,
configured arrays, longer documentation, non-null types and later ordinary
properties; relationships resolve against all contributions. Each tool's metadata
and original source snapshot remain recorded. A single output uses
`write_tool_output(tool_name, output, repo_url)`.

Publication is now an atomic graph revision rather than exposing partially
written contributions. All data is scoped by repository/revision and queries
require a complete AnalysisSnapshot publication marker. Reimporting identical
facts is idempotent; conflicting content under the same revision is rejected.
A new revision preserves old facts for explicit selection, so removed definitions
cannot leak into queries for the new revision. Applications choose revision
retention; this port does not silently delete prior graphs. Failed or partial
analysis rejects the publication, and any write error rolls back the transaction.

Neo4j internal IDs and global caches no longer define portable identity. Original
qualified names remain primary identities within repository/revision. Nested
portable JSON properties use reversible encoding; native scalar fields stay
queryable. Complete imports and original metadata are independently readable.
APOC is not required. Merge policies are applied before persistence rather than
through the old APOC-dependent writer path.

The ambiguous alternate legacy MCP query direction is resolved to the original
shared ContextService schema: File -> DEFINES -> declaration. Typed query results
are structured bounded envelopes with scope, capabilities and truncation rather
than unbounded lists. No relationship means only that the selected analysis did
not supply it; declaration-only extraction does not establish absence of calls.

## Edge evidence

Every relationship records how its endpoints were linked, never how likely the
link is. `RelationshipData` and the storage writer both require two properties:

- `evidence_type`: `resolved` (exact identity, such as an indexer symbol or a
  parsed declaration inside a file), `pattern_matched` (a declared pattern such as
  a route string matched) or `name_matched` (equal names without proof of
  identity).
- `rule_id`: the registered rule that produced the edge, written
  `<producer>.<rule>` in lowercase, for example
  `tree-sitter.file-defines-declaration`.

Each rule is registered once with `register_rule(EvidenceRule(...))` and declares
a single evidence type. An edge whose `evidence_type` differs from its rule's is
rejected, so a tool cannot label a name match as resolved edge by edge. Unknown
rules are rejected, as are `confidence` and `probability` properties: the
original builder's confidence floats and its `resolved_method` label for a bare
method-name match are not carried over. The writer repeats the check because
records are mutable after construction.

TreeSitterTool registers `tree-sitter.repository-contains-file` and
`tree-sitter.file-defines-declaration`, both `resolved`. Typed queries already
return relationship properties, so results carry the evidence and rule of each
edge. Edge evidence is part of the snapshot's content fingerprint: a database
holding a revision published before this contract rejects a re-import of the
same source under the same revision as conflicting content. Select a new
revision with `--snapshot` or use a fresh database.

## File imports

Tree-sitter extraction records each static import statement as an `Import` fact
(analysis schema 3): Python `import` and `from ... import`, and TypeScript and
JavaScript `import ... from` and `export ... from`. Dynamic loads such as `require()`,
`import()` and `importlib` are not facts here. `deepthought.imports.resolve_imports`
then matches specifiers against the analyzed files of the same result, without
reading files, and TreeSitterTool writes File -> IMPORTS -> File for each link
with the specifier, names, line, import ID and rule:

| Rule | Evidence | Links |
|---|---|---|
| `tree-sitter.python-relative-module` | resolved | `.m`, `..m`, `.`: the package `__init__.py` (preferred) or module file at that path; climbing above the importing directory requires a package |
| `tree-sitter.python-absolute-module-path` | name_matched | dotted name equals a file path under the repository root or `src/`; the runtime import path is not checked |
| `tree-sitter.python-from-import-submodule` | name_matched | `from pkg import x` where `pkg/x.py` exists; the package may define `x` instead |
| `tree-sitter.typescript-relative-module` | resolved | `./x`, `../x`: `x.ts`, `x.tsx`, `x.d.ts`, then `x/index.*`; `.js`/`.jsx` map to their TypeScript sources; when JavaScript is analyzed, JavaScript files are tried after every TypeScript candidate, as with `allowJs`; `package.json` is not read |
| `tree-sitter.javascript-relative-file` | resolved | a JavaScript file's `./x.js`, `./x.jsx`, `./x.mjs`, `./x.cjs`: the analyzed file at exactly that path |
| `tree-sitter.javascript-extensionless-module` | pattern_matched | a JavaScript file's `./x`: `x.js`, `x.jsx`, `x.mjs`, `x.cjs`, then `x/index.js`, `x/index.jsx`, the bundler/CommonJS convention; Node ES modules would reject the specifier, and bundler configuration is not read |
| `tree-sitter.go-module-package` | resolved | a Go import path inside the importing file's own module (nearest `go.mod`): the directory at that path below the module root, linked to every non-test file of its package; `main` packages are not importable, and `vendor/`, `replace` and `go.work` are not read |

Anything else gets no edge. Non-relative TypeScript and JavaScript specifiers (packages, `paths`
aliases), Python imports matching several roots, imports beyond the repository
root, self-imports and modules outside the analyzed files are reported instead:
each File node lists its `unresolved_imports` and `ambiguous_imports`
specifiers, and tool metadata counts linked, ambiguous and unresolved imports.

An IMPORTS edge says which file a statement imports, not where an imported name
is defined; a package re-export links to the package's `__init__.py` or index
file. Schema 2 artifacts, including SCIP ingestion output, serialize exactly as
before, so their snapshot and graph identities do not change.

## Calls

Tree-sitter extraction records each call whose callee is a plain or dotted name
(`f()`, `a.b.f()`, `self.m()`, `new Box()`) as a `CallSite` with its innermost
enclosing declaration. Calls on other expressions (`f()()`, `x[0]()`, optional
chains) are not recorded. `deepthought.calls.resolve_calls` matches names only,
so every CALLS edge it yields is `name_matched`; shadowing at runtime, rebinding,
inheritance and dynamic dispatch are not checked. Resolved call edges come from
SCIP. The original builder's `confidence` floats and its `resolved_method` label
for a bare `obj.method` match are not carried over, and `obj.method()` on an
arbitrary object gets no edge.

| Rule | Links |
|---|---|
| `tree-sitter.call-lexical-name` | `f()` to a function or class declared in an enclosing scope of the same file; class bodies are not enclosing scopes |
| `tree-sitter.call-imported-name` | `f()` to the top-level declaration that an import binding (aliases applied) names in its linked module file |
| `tree-sitter.call-module-attribute` | `mod.f()` where `mod` is a module import binding (`import m`, `import a.b as m`, `import * as m`) |
| `tree-sitter.call-self-method` | `self.m()` / `this.m()` to a method declared in the enclosing class body |
| `tree-sitter.call-package-name` | Go: `f()` to a function declared at top level by another file of the same package (same directory and package clause); build constraints are not evaluated |

A name bound both by an import and a local declaration, or declared more than
once, is ambiguous and gets no edge. CALLS edges start at the calling
declaration, or at the File for module-level calls, and carry the call ID,
callee text, line and column. Tool metadata counts call resolutions by status
and reason. The `calls` capability is not advertised, so an empty callers or
callees result still says the relation was unsupported or unobserved.

`tools/compare_calls_with_scip.py` checks linked calls against SCIP references
at the callee name.

## Declaration nesting

Each declaration is linked from the innermost other declaration whose span
encloses it in the same file, as Symbol -> CONTAINS -> Symbol under
`tree-sitter.declaration-contains-declaration` (`resolved`): a class contains
its methods and nested classes, a function its nested functions, a namespace
its members. The edge carries the inner declaration's line. Repository ->
CONTAINS -> File edges are unchanged; the source label tells them apart.
`deepthought-graph members|owner --symbol QUALIFIED_NAME` (MCP getMembers and
getOwner) return direct nesting.

The span rule agrees with the extractor's name scopes everywhere it was
checked: on Django 5.1.4 `django/` (7,932 edges), rxjs 7.8.1 `src/` (260) and
preact 10.25.4 (32), every inner name is its owner's name plus one or more
segments, every declaration whose name prefix is declared in the same file has
an owner, and none has two.

## Declaration source

Each tree-sitter Symbol node stores its declaration's text as analyzed:
`source_code`, `source_sha256`, and the text's span
(`source_start_line`/`source_start_column` to `source_end_line`/
`source_end_column`). The text runs from the declaration's decorators or
`export` keyword to its end, so a decorated Python method starts at its first
`@`. The name's position is stored as `[line, column, end line, end column]`
three times, with UTF-8, UTF-16 and UTF-32 code-unit columns
(`name_range_utf8`, `name_range_utf16`, `name_range_utf32`), for clients and
indexes that count columns differently. The projection reads each analyzed
file again and stores text only if the file's sha256 still equals the digest
the analysis recorded; files that changed in between are listed in the
`source_context` metric and get no text. Files reused by incremental
re-indexing get text like parsed ones.

`get_code_context` (CLI `context`, MCP getCodeContext) returns a page of each
declaration's text: `max_source_characters` (0 to 8,192, default 1,200)
Unicode code points from `source_offset`, with `source_characters` giving the
full length; `definition_id` selects one declaration. Other queries that
return whole nodes leave the text, its hash, its span and the name positions
out, so results such as class hierarchy paths keep their size; graph diffs
leave `source_code` out and list the `source_sha256` change.
Nested declarations repeat their text in their owner's, which costs 1.62 times
the analyzed bytes on Django 5.1.4 `django/` and 0.58 times on rxjs 7.8.1
`src/`.

Checked against independent parsers:

| Corpus | Parser | Declarations | Same text, span and name position | Differences |
|---|---|---|---|---|
| Django 5.1.4 `django/` | CPython `ast` (`tools/compare_declaration_sources_with_ast.py`) | 10,994 | 10,974 | 20 keep comments that end the body, which `ast` omits |
| rxjs 7.8.1 `src/` | TypeScript 5 parser (`tools/compare_declaration_sources_with_typescript.py`) | 937 | 880 | 57 overload signatures without the `;` the compiler's node ends with |

The TypeScript check finds each declaration by its UTF-16 name position, so it
also confirms those positions.

### Indexer symbols and parsed declarations

Tree-sitter File nodes carry `file_sha256`, the digest of the analyzed bytes
(so a graph diff lists a File whose content changed). When one revision
combines SCIP and tree-sitter output (`write_outputs`, or `index` with both
tools), each SCIP definition whose occurrence covers exactly a parsed
declaration's name range, counted in SCIP's column encoding, in a file with
the same sha256, gets Symbol -> HAS_AST_DECLARATION -> Symbol under
`graph.indexer-symbol-declaration-name` (`resolved`). Different bytes never
bind, and a range two declarations share binds neither; the
`declaration_binding` metric counts bound, ambiguous and unmatched SCIP
definitions. `get_code_context` returns a bound SCIP symbol as its parsed
declaration, with the declaration's text and the SCIP symbols in
`native_ids`; `native_symbol` (CLI `--native-symbol`) selects by SCIP symbol.

On rxjs 7.8.1 with its scip-typescript index, 924 of 5,358 SCIP definitions
bind, none ambiguously, and no declaration binds twice. Every SCIP class (33),
interface (83), method (161), static method (7), constructor (31), function
(509), getter (2), type alias (37) and enum (1) binds; 60 of 566 constants
bind (arrow functions assigned to constants). Parameters, type parameters,
properties, variables, enum members and modules have no parsed declaration.
924 of the 937 tree-sitter declarations bind; the other 13 are 12
object-literal methods and one computed-name method, for which
scip-typescript records no definition.

## Impact

The original impact endpoint passed "Analyze the impact of changes to: ..." to
the chat model. The port answers from stored edges only:

- `get_impact(qualified_name)` (`deepthought-graph impact --symbol`, MCP
  getImpact): the change set is the declaration and every declaration it
  CONTAINS; dependents are found backwards over CALLS, INHERITS_FROM,
  IMPLEMENTS and REQUESTS, and forwards over HANDLES. A route a changed
  handler HANDLES is affected, and so is every declaration (or file) that
  REQUESTS that route, so impact crosses service boundaries along the HTTP
  contracts the `service-contracts` tool records (see "Service contracts").
  Those hops are `pattern_matched`, and the path's evidence says so. The
  traversal is a quantified path pattern, so this query needs Neo4j 5.9 or
  later.
- `get_file_impact(path)` (`deepthought-graph file-impact --file`, MCP
  getFileImpact): dependent files backwards over IMPORTS and DEPENDS_ON.

Both take `--max-depth` (1 to 5, default 3) and an optional evidence filter
that limits which edges may be crossed. Each dependent is listed once, with
the declaration or file it reaches in the change set, its depth, the edges of
its path (type, edge ID, evidence type, rule ID) and the path's evidence,
which is its weakest edge. When several paths reach a dependent, the one with
the strongest evidence wins, then the shortest, then the first by edge IDs, so
the answer is the same on every run. Results are ordered by depth, evidence
and name; a response that exceeds its byte budget drops whole items and says
it was truncated. The answer is a lower bound: dynamic dispatch, reflection,
unanalyzed files and relations no tool records are not seen, and a
`name_matched` path is only as good as its weakest name match.

`tools/check_impact.py` writes a workspace to Neo4j and compares `get_impact`
for a sample of declarations with a breadth-first recomputation over the
tool's own output that never reads Neo4j. Every compared seed matched exactly
(same dependents, depths and evidence) at `--max-depth 5`: 2,742 Django
5.1.4 declarations (every fourth; 3,213 dependents) and 925 of the 937 in rxjs 7.8.1 `src/` (627
dependents). 7 and 12 seeds with more than 100 dependents or 64 KB of output
were truncated and not compared.

## Components

The original service detector added weighted points for a Dockerfile (30), a
Kubernetes or Compose entry (10), serialization calls (10 to 20), schema files
(10), call-graph modularity (8 to 15), environment variables and config files,
then reported the total as a confidence; it named services after their
directory and typed them by port number. None of that is ported. The
`components` tool (`deepthought-graph index --tool components`) records only
what a build manifest declares:

| Manifest | Kind | Declared by |
|----------|------|-------------|
| `pyproject.toml` | `python_project` | `[project] name`, else `[tool.poetry] name`; tool configuration alone declares nothing |
| `package.json` | `node_package` | any JSON object, named by `name` when present (a package root without a name is still a root) |
| `go.mod` | `go_module` | the `module` directive |
| `Cargo.toml` | `cargo_package` | `[package] name`; a virtual workspace declares nothing |
| `pom.xml` | `maven_module` | the project's own `artifactId` |

Each is a Component node (root = the manifest's directory), and the manifest
File -> DECLARES -> Component (`components.manifest-declares`). When tool
outputs are combined into one graph revision, every File node from any tool
gets File -> PART_OF -> Component for the nearest enclosing root of each kind
(`components.nearest-root`): a Go file in a repository with a root
`pyproject.toml` belongs to its go.mod module and to that Python project. Both
rules are `resolved`: they state declarations and directory containment, not
whether a build includes the file (a manifest's excludes and Python package
discovery are not read). Directories the source analyzer skips (`.git`,
`.venv`, `node_modules`, `__pycache__`) are skipped here too. A manifest that
cannot be read makes the analysis `partial`, like a syntax error; exclude test
fixtures with `--exclude`.

`list_components` (`deepthought-graph components [--kind KIND]`, MCP
listComponents) lists components with their file counts,
`get_file_components` (`file-components --file`, getFileComponents) a file's
components with evidence, and `get_component_files` (`component-files
--component QUALIFIED_NAME [--after-path PATH]`, getComponentFiles) pages
through a component's files by path.

`tools/compare_components_with_build_tools.py` checks components and
membership against each format's own tool: `go list -m` names and `go list -e
./...` module directories, `cargo metadata --no-deps` packages and target root
files, Node's `module.findPackageJSON` for every JavaScript, TypeScript and JSON
file, and `mvn help:evaluate` artifact IDs. No contradictions:

| Repository | Components | Checked |
|------------|------------|---------|
| opentelemetry-go | 30 go.mod modules | 30/30 module paths, 1,396/1,396 Go files' modules |
| tokio | 12 Cargo packages | 12/12 packages, 307/307 target root files |
| babel | 207 npm packages | 29,128/29,128 files' package.json (2 deliberately invalid fixture manifests unchecked, reported as diagnostics) |
| guava | 14 Maven modules | 14/14 artifact IDs |
| spring-petclinic-microservices | 9 Maven modules | 9/9 artifact IDs, 34/34 files' package.json |

Python projects have no comparable resolver and are checked by tests only.
Deployment units (Compose services and Kubernetes workloads) and
cross-service contract edges are separate slices.

### Component dependencies

Each manifest's declared dependencies are read as written, and
Component -> REQUIRES -> Component links a declaration to the in-repository
component it names, with the manifest section (`dependencies`,
`dev-dependencies`, `require`, `project.optional-dependencies.EXTRA`, ...),
the declared name and any declared path on the edge:

| Rule | Evidence | Declaration |
|------|----------|-------------|
| `components.cargo-path-dependency` | resolved | Cargo dependency with `path` |
| `components.cargo-workspace-dependency` | resolved | `workspace = true`, whose nearest enclosing `[workspace.dependencies]` entry has a `path` |
| `components.cargo-patch-path` | resolved | a name an enclosing `[patch.*]` table replaces with a `path` |
| `components.go-replace-directory` | resolved | a `require`d module a `replace` points at a directory |
| `components.npm-file-dependency` | resolved | `file:` or `link:` specifier |
| `components.npm-workspace-protocol` | resolved | `workspace:` specifier naming the only package of that name |
| `components.python-path-source` | resolved | `[tool.uv.sources]` or Poetry `path` |
| `components.python-workspace-source` | resolved | uv `workspace = true` source naming the only project of that (PEP 503) name |
| `components.dependency-name` | name_matched | anything else whose name (Maven `groupId:artifactId`, Go module path, npm/Cargo/Python name) equals exactly one component of the same kind |

A name shared by several components is not linked (`ambiguous`), and one no
component declares is `external`; the counts are in the tool's
`dependency_resolution` metric. The name-matched rule is weak on purpose: a
registry may supply that name instead, at another version.

`get_component_dependencies` (`component-requires`/`component-required-by
--component`, MCP getComponentDependencies) lists either direction with the
declaration, and `get_component_impact` (`component-impact`, getComponentImpact)
follows REQUIRES backwards like the other impact queries.

The same comparison tool checks REQUIRES against `cargo metadata
--all-features` resolve graphs, `go mod edit -json` replacements and a Yarn
Berry `yarn.lock`: every resolved link must be one the build tool made, and
each in-repository link it made must be linked.

| Repository | Resolved component pairs confirmed | Build-tool pairs linked | Name matches |
|------------|--------------------------|------------------------|--------------|
| opentelemetry-go | 145/145 (go-replace-directory) | 145/145 | none |
| tokio | 25/25 (path and `[patch.crates-io]`) | 25/25 | none |
| babel (`--exclude '*/fixtures/*'`) | 786/786 (workspace: and link:) | 786/786 | 12, all refuted: the root's `@babel/*` dev dependencies resolve to registry versions |

The dependencies of the `link:` package in babel are not installed by Yarn
and stay unchecked; Maven name matches (guava: 5, plus 12 ambiguous between
`guava` and `android/guava`) have no oracle here.

### Compose services

Compose files (`compose.yaml`, `docker-compose.yml` and variants such as
`docker-compose.prod.yml`) declare `compose_service` components, one per
`services` entry (needs PyYAML: the `components` or `languages` extra). Each
file is read on its own, as `docker compose -f FILE` reads it: several `-f`
files, `extends` and `include` are not merged, and `${VARIABLE}`
interpolation is not performed. A service's root is its build context
(`build: DIR` or `build.context`); one that only names an `image`, builds from
a remote URL or needs interpolation has no root and no files. Files under a
build context are PART_OF every service built from it.

| Edge | Rule | Evidence |
|------|------|----------|
| service -> REQUIRES -> service | `components.compose-depends-on` (depends_on names a service of the same file) | resolved |
| service -> DEPLOYS -> package component | `components.compose-build-context` (build context is the component's root) | resolved |
| service -> DEPLOYS -> package component | `components.compose-image-name` (no build; the image's last path segment, without tag or digest, equals the name or last `/` segment of exactly one package component) | name_matched |

The weighted detector's port-number service types (80 -> HTTP API, 6379 ->
cache, and so on) are not ported.

`--compose` adds `docker compose -f FILE --profile '*' config --format json`
to the comparison tool: the services each file declares (all profiles), their
build contexts and depends_on. No contradictions:

| Repository | Compose files | Services, contexts and depends_on confirmed |
|------------|---------------|---------------------------------------------|
| docker/awesome-compose | 39 | 39/39 files' service sets, 81/81 contexts, 81/81 depends_on |
| example-voting-app | 3 | 3/3, 17/17, 17/17 |
| spring-petclinic-microservices | 1 | 1/1, 11/11, 11/11; its 8 image-only services DEPLOY their Maven modules by image name |

### Kubernetes workloads

With PyYAML, every other `.yaml`/`.yml` file is read for Kubernetes objects
(including `List` items) and Skaffold configs; a file that holds neither, or
does not parse (an unrendered Helm template, for instance), is not a manifest
and is skipped without a diagnostic. Each file is read as written: Helm charts
and Kustomize overlays are not rendered, so render them first when their
output is what should be analyzed.

- `k8s_workload`: Deployment, StatefulSet, DaemonSet, ReplicaSet, Job, CronJob
  and Pod, named `Kind/namespace/name` (or `Kind/name`), with the pod
  template's labels and the container images. Its root is the build context
  of the Skaffold artifacts its images name, when they name exactly one
  context; otherwise it has no files.
- `k8s_service`: Service, with its selector.

| Edge | Rule | Evidence |
|------|------|----------|
| Service -> SELECTS -> workload | `components.k8s-service-selector`: the nonempty selector is a subset of the pod template labels, same namespace as written, same directory | resolved |
| workload -> DEPLOYS -> package component | `components.k8s-skaffold-context`: the Skaffold context is the component's root | resolved |
| workload -> DEPLOYS -> package component | `components.k8s-image-name`: one image and no Skaffold context; the image's last path segment equals exactly one package name | name_matched |

Selection is limited to one directory because repositories often keep
alternative manifest sets (raw, release bundle, Kustomize base) for the same
application; mixing them would link every Service to every copy.

`tools/kubernetes_selector_oracle` is a Go program that decodes every YAML
file with the Kubernetes API types (client-go scheme) and matches selectors
with apimachinery's `labels.SelectorFromSet`, under the same namespace and
directory scope. With `--kubernetes-oracle BINARY` the comparison tool checks
SELECTS in both directions. No contradictions:

| Repository | Workloads / Services | SELECTS confirmed | Oracle pairs linked |
|------------|----------------------|-------------------|---------------------|
| GoogleCloudPlatform/microservices-demo | 39 / 39 | 38/38 | 38/38 |
| kubernetes/examples | 89 / 56 | 39/39 | 39/39 |

In microservices-demo, 12 workloads take their root from `skaffold.yaml`, and
the one Service whose workload lives in another directory
(`.github/release-cluster`) is not linked.

## Declared bases

Tree-sitter extraction records each base a class or interface names as a
`Supertype` fact (analysis schema 3, `supertypes` capability): Python base
classes and TypeScript/JavaScript `extends` entries are `extends`, a TypeScript
class's `implements` entries are `implements`. Only plain or dotted names are
recorded, without type arguments (`Generic[T]` and `Base<T>` name `Generic`
and `Base`); keyword arguments such as `metaclass=`, splats and call
expressions such as mixins are not. `deepthought.inheritance.resolve_supertypes`
applies the call name-binding rules, so every INHERITS_FROM edge is
`name_matched`:

| Rule | Links |
|---|---|
| `tree-sitter.base-lexical-name` | `Base` to a class or interface declared in the scope enclosing the declaration (a Python class body included), then in enclosing function and module scopes of the same file |
| `tree-sitter.base-imported-name` | `Base` to the top-level class or interface that an import binding (aliases applied) names in its linked module file |
| `tree-sitter.base-module-attribute` | `mod.Base` where `mod` is a module import binding |

A name that is both imported and declared locally, or declared twice, is
ambiguous; builtins and library bases stay unresolved. Neither gets an edge.
INHERITS_FROM edges carry the supertype ID, the base as written, its
`heritage` kind, line and column. `deepthought-graph bases|subtypes --symbol
QUALIFIED_NAME` (MCP getBaseTypes and getSubtypes) return direct edges with an
optional evidence filter, and `get_class_hierarchy` follows them through
classes and interfaces. The original tool emitted the same relation for names
its symbol resolver found; it is now limited to these rules and marked with
its evidence. Compiler-backed subtype facts are the SCIP IMPLEMENTS edges.
Rust impl blocks and supertraits give `resolved` INHERITS_FROM edges of their
own (see "Rust impls").

`tools/compare_supertypes_with_scip.py` checks each base against the
declaration SCIP references at its last identifier:

| Corpus | Bases | Bases with an in-repository SCIP target | Linked | Confirmed | Contradicted |
|---|---|---|---|---|---|
| Django 5.1.4 `django/` (scip-python 0.6.8) | 1,819 | 1,612 | 1,363 | 1,361 | 0 |
| rxjs 7.8.1 `src/` (scip-typescript 0.6.0) | 55 | 47 | 47 | 47 | 0 |

The two Django links SCIP leaves unresolved are correct by inspection (a base
in an `os.name == "nt"` branch and one imported from `smartif`). Of the 251
Django bases with a SCIP target and no edge, 140 are `module.Base` through a
`from package import module` binding, which the rules do not follow, and 102
are names re-exported by a package `__init__`. Full and incremental analyses
of Django give identical facts.

## JavaScript

JavaScript and JSX sources (`.js`, `.jsx`, `.mjs`, `.cjs`) are analyzed when
`javascript` is selected (`--language javascript`, or `languages=` in Python)
and the `javascript` extra is installed. The language is opt-in, so analyses
that select no language keep their Python/TypeScript output byte for byte, and
TypeScript repositories that ship compiled `.js` next to their sources are not
analyzed twice by default. Declarations, call sites and call rules are the same
as for TypeScript. ES module `import ... from` and `export ... from` statements
are import facts. CommonJS `require()` and dynamic `import()` are not, so a
CommonJS module has declarations and calls but no IMPORTS edges.

The original import processor returned `<specifier>.ts` for every relative
JavaScript or TypeScript specifier without checking that the file existed. Here
a specifier links only to an analyzed file, by the rules in the imports table
above. An explicit extension names one file and is `resolved`. An extensionless
specifier is `pattern_matched`, because whether it resolves depends on the
bundler or on CommonJS, and Node ES modules reject it.

Checked against scip-typescript 0.6.2 indexes (allowJs) of two public npm
packages with `tools/compare_imports_with_scip.py` and
`tools/compare_calls_with_scip.py`:

| Corpus | Files | IMPORTS file pairs confirmed by SCIP | Linked calls confirmed by SCIP | Contradicted |
|---|---|---|---|---|
| execa 9.5.2 `index.js`, `lib/` (Node ES modules, `.js` specifiers) | 105 | 257 / 257 | 684 / 684 | 0 |
| preact 10.25.4 `*src/` (bundler, extensionless specifiers) | 37 | 62 / 67 | 173 / 174 | 0 |

Each of the five preact pairs SCIP does not confirm goes to a `.js` file with a
`.d.ts` beside it. The compiler links to the declaration file, while the IMPORTS
edge names the module that runs. The unconfirmed call is `suspended()` in
`compat/src/suspense-list.js`. `suspense.d.ts` does not declare `suspended`, so
SCIP has no target for it. The call links to the function in `suspense.js`. SCIP found no file pair that
Tree-sitter missed in either corpus. Tarball sha256: execa
`9629a8a1dfd74f7c7c7b3b1bbf3a0f6b20ae29bbfae8c25256ad6025b4e62a3b`, preact
`93d4a374795c9b15b8b5edad9294a24cf210e99f3f0fb2b98d00bdb401e7c892`. The SCIP
indexes used a tsconfig with `allowJs`, `checkJs: false` and `nodenext`
(execa) or `bundler` (preact) module resolution. `tests/fixtures/javascript`
and its committed index repeat the check in CI.

## Go

Go sources (`.go`) are analyzed when `go` is selected (`--language go`) and the
`go` extra is installed; like JavaScript, the language is opt-in. Declarations
are functions, methods, named types and interface method elements. A method is
named by its receiver type (`Server.Start` for `func (s *Server) Start()`,
`Box.Get` for `func (b Box[T]) Get()`), because Go declares methods at top
level, not inside the type. A named type's kind is `Struct`, `Interface` or
`Type` from its underlying type, and `type A = B` is a `TypeAlias`. Interface
method elements and types declared inside functions are nested by CONTAINS.

Import specs are facts (path, alias, `.` as `*`, `_` binds nothing). When Go
is selected, each `go.mod` file is read too: its `module` directive (line or
block form) becomes a `packages` fact of kind `module` and a `module` property
on its File node; a `go.mod` without one is `partial` with a `syntax_error`.
An import resolves by `tree-sitter.go-module-package` only inside the
importing file's own module, the nearest enclosing `go.mod`: the path after
the module path (whole path elements only) names a directory below that
`go.mod`, and the import links to every non-test file of the package there.
Other outcomes stay unresolved or ambiguous, with a reason:

| Reason | When |
|---|---|
| `outside_module` | the path is not under any module in the repository (standard library, third-party) |
| `other_repository_module` | another `go.mod` in the repository owns the path, including a nested module below the importer's; reaching it needs `replace`, `go.work` or the module cache, which are not read |
| `no_analyzed_file` | no analyzed Go file of an importable package is in that directory |
| `main_package` | the directory holds only `package main`, which cannot be imported |
| `multiple_packages` (ambiguous) | non-test files in the directory declare more than one importable package |
| `go_module_path` | the importing file has no enclosing `go.mod` |

Calls are recorded as written, including selector calls such as `fmt.Println`.

Each Go file's `package` clause is a fact (`packages`, part of snapshot
identity) and a `package` property on its File node. A Go package is the files
of one directory that declare the same package, so an external test package
(`package x_test`) and a `//go:build ignore` generator declaring `package main`
are separate packages. A bare call `f()` links by
`tree-sitter.call-package-name` to a function another file of the same package
declares at top level. Build constraints are not evaluated: if
`plat_linux.go` and `plat_darwin.go` both declare `plat`, the call is ambiguous
and gets no edge.

A package-qualified call `pkg.F()` links by `tree-sitter.call-module-attribute`
when `pkg` is bound by a resolved import and a file of that package declares
`F` at top level. An aliased import binds its alias; an import without one
binds the imported package's declared name (from its `package` clause), not
its directory name. Imports outside the module bind nothing, and local
variables that shadow a package name are not checked (name_matched). Method
calls (`x.M()`) and conversions are not linked.

Checked against scip-go 0.1.26 indexes with
`tools/compare_go_declarations_with_scip.py` and
`tools/compare_calls_with_scip.py --language go`:

| Corpus | Declarations matched by scip-go (same file, line and name) | scip-go only | Receiver mismatches |
|---|---|---|---|
| cobra 1.8.1 | 580 / 585 | 0 | 0 |
| gorilla/mux 1.8.1 | 235 / 238 | 0 | 0 |

| Corpus | Linked calls confirmed (same file + package + qualified) | Contradicted | Calls with an in-repo SCIP target that are linked |
|---|---|---|---|
| cobra 1.8.1 | 998 / 998 (438 + 545 + 15) | 0 | 998 / 2,096 |
| gorilla/mux 1.8.1 | 404 / 404 (255 + 143 + 6) | 0 | 404 / 820 |
| go-chi/chi v5.0.12 | 416 / 416 (150 + 227 + 39) | 0 | 416 / 948 |
| spf13/viper 1.18.2 | 430 / 430 (113 + 286 + 31) | 0 | 430 / 1,061 |

A further 35 chi links are in `_examples/`, which scip-go does not index, so
nothing confirms or contradicts them.

The eight declarations scip-go has no global symbol for are five types declared
inside test functions, two interface method elements, and `preExecHook` in
`command_win.go`, which a Linux build excludes. The calls that stay unlinked
are mostly method calls and conversions.

Import links are compared per (file, package directory) pair across
directories with `tools/compare_imports_with_scip.py --language go`, against
directories scip-go saw a reference into:

| Corpus | Imports linked / in-module | Package pairs confirmed by scip-go | scip-go only |
|---|---|---|---|
| cobra 1.8.1 | 12 / 12 | 11 / 11 | 0 |
| gorilla/mux 1.8.1 | 4 / 4 | no cross-directory pairs | 0 |
| go-chi/chi v5.0.12 | 43 / 43 | 18 / 43 | 0 |
| spf13/viper 1.18.2 | 12 / 12 | 12 / 12 | 0 |

The 25 chi pairs scip-go lacks are all in `_examples/`, which the go tool, and
so scip-go, skips; they are ordinary imports of the chi packages. Every other
import in these corpora names the standard library or a third-party module
(`outside_module`).

## Java

Java sources (`.java`) are analyzed when `java` is selected (`--language java`)
and the `java` extra is installed; like JavaScript and Go, the language is
opt-in. Declarations are classes, interfaces, enums, records (`Record`),
annotation types (`Annotation`), methods, constructors (`Constructor`, named
after their class, so `Outer.Outer`; a record's compact constructor too) and
annotation elements (`Method`, as javac declares them). Names are lexical:
nested and local types and their members are qualified by every enclosing
named declaration, and a method of an anonymous class (including an enum
constant body) belongs to the enclosing scope, since the class has no name.
Overloads are separate declarations with distinct identities. Lambdas and
fields are not declarations. A declaration's text starts at its first
annotation or modifier; a Javadoc comment before it is not part of it.

Each file's `package a.b;` declaration is a `packages` fact and a `package`
property on its File node. Imports are facts: `import a.b.C;` imports type
`a.b.C` and binds `C`; `import static a.b.C.m;` imports member `m` of type
`a.b.C`; `import a.b.*;` imports package `a.b` (or the member types of a type
`a.b`), named `*`. A file's source root is its directory above the path its
package names (`src/main/java` for `src/main/java/a/b/C.java` in package
`a.b`); files outside such a path or in the unnamed package cannot be found by
name and are not import targets. An import links to the file declaring the
type it names (top-level or nested; local classes are not importable), and a
package wildcard to every file declaring a type in that package
(`package-info.java` declares none):

| Rule | Evidence | When |
|---|---|---|
| `tree-sitter.java-package-member` | resolved | exactly one source root declares the package or type |
| `tree-sitter.java-same-source-root` | name_matched | several roots declare it (flavors, copies); the importer's own root is chosen, since which root a build puts on the class path is not read |

Other outcomes: `not_in_repository` (JDK, libraries), ambiguous
`multiple_source_roots` when several roots declare it and none is the
importer's, and ambiguous `multiple_declarations` when two files of one root
declare the same type. Build files (Maven, Gradle) are not read.

Java calls are method invocations (`m()`, `this.m()`, `T.m()`, `a.b.T.m()`)
and `new T()` expressions, with the number of arguments written; Java methods
and constructors record their parameter count and whether the last parameter
is variable-arity. Calls link by Java's name lookup, without types, so every
link is `name_matched`:

| Rule | Callee |
|---|---|
| `tree-sitter.java-enclosing-type-method` | `m()`: the innermost enclosing named type that has a method `m`, declared or inherited (Java stops at the first such type), when that type declares it |
| `tree-sitter.java-static-import` | `m()` declared by no enclosing type: a method of a type imported with `import static`, by name or `*` |
| `tree-sitter.call-self-method` | `this.m()`: the innermost enclosing type's `m` |
| `tree-sitter.java-type-member` | `T.m()`: `T` names a type by Java's type lookup (below), and that type declares `m` |
| `tree-sitter.java-type-name` | `new T()`: the type `T` names, by the same lookup |
| `tree-sitter.java-inherited-method` | `m()`, `this.m()` or `T.m()` where the type found does not declare `m` but inherits it through its declared bases |

A type name is looked up as Java does, without types: member types of each
enclosing type from the innermost out, declared or inherited through the
type's declared bases (a member declared along two paths is one type);
local classes; then a single-type or static import, the package, wildcard
imports, and finally a fully qualified name (`a.b.T.Inner`). A single-type
import binds its name even to a type outside the repository (`import
java.util.concurrent.atomic.LongAdder;` hides a `LongAdder` in the
package). A type found in the importer's own source root is chosen; one
declared only in several other roots is ambiguous, since only the build
says which is on the class path.

A call inside an anonymous class body (`new T() {...}`, or an enum
constant's body, whose base is the enum) looks in that class first: the
methods its body declares, then those its base has. Anonymous and named
classes around a call are searched innermost first, by how their spans nest,
and `this.m()` means the innermost of them. An anonymous class whose base is
outside the repository (`new Runnable() {...}`) contributes only its own
methods, and the lookup goes on outward. Each Java call records the
anonymous classes around it (`anonymous`: base, the scope its methods are
named in, first and last line); other calls do not have the field.

A type's methods named `m` are the ones it declares, then those its
repository bases have, followed in source order (a class's superclass
first) through every level. A nearer method with the same parameter count
overrides an inherited one, and a method declared in a class (the
superclass chain) also implements a same-shaped interface method; methods of
two interfaces with the same shape are both kept, since only types say which
wins. Bases outside the repository (`AbstractList`, `TestCase`) are not
read, so their methods stay unresolved.

Among same-named methods, only those whose parameters accept the argument
count are candidates (a variable-arity method accepts one fewer or more);
several left are ambiguous, none is `no_matching_arity`. Calls on variables
or expressions (`list.add(x)`) need types and stay unresolved
(`attribute_call`).

Checked against javac's own parser with
`tools/compare_java_declarations_with_javac.py`, which runs
`tools/JavaDeclarationOracle.java` (javac 21, parse only). A declaration matches
when file, qualified name, kind, start and end (UTF-16 offsets) are equal, and
every stored name range covers the declared name:

| Corpus | Declarations matched | javac only | Tree-sitter only |
|---|---|---|---|
| google/gson `d9b5079` | 4,360 / 4,360 | 0 | 0 |
| apache/commons-lang `1880038` | 13,105 / 13,108 | 3 | 0 |
| google/guava `b8eedaa` | 66,678 / 66,678 | 0 | 0 |
| junit-team/junit5 `68e59c0` | 19,441 / 19,447 | 6 | 4 |

The three commons-lang declarations are in `ClassUtilsOssFuzzTest.java`, which
the grammar cannot parse; the file is `partial` with a `syntax_error`. The
junit5 differences are two Java 25 compact source files: javac wraps their
top-level methods in an implicit class named after the file, while here they
are top-level methods. Guava's annotated varargs (`Object @Nullable ...`) make
13 files `partial`, but every declaration in them still matches.

Imports are checked against javac's own import resolution with
`tools/compare_java_imports_with_javac.py`. It runs
`tools/JavaImportOracle.java` once per source root: javac parses and enters
that root's files with the root first on the source path and every other root
after it (sorted), and reports the source files each import binds to.

| Corpus | Source roots | Links confirmed by javac | Contradicted | Ambiguous, javac's file among candidates | Neither in repository |
|---|---|---|---|---|---|
| google/gson | 12 | 1,115 / 1,115 | 0 | 0 | 1,655 |
| apache/commons-lang | 3 | 790 / 790 | 0 | 0 | 3,880 |
| junit-team/junit5 | 49 | 9,893 / 9,893 | 0 | 0 | 8,117 |
| google/guava | 25 | 8,596 / 8,597 | 1 | 8,457 | 22,189 |

No import javac binds to a repository file is left unlinked. In junit5, 21
links (27 imports) are not compared, because two imports share a line or javac
saw the file under two nested source roots. The guava
contradiction is an artifact of the oracle's source path order: `guava-tests`
imports `UnsignedBytes.ArraysCompareUnsignedComparator`, a nested type only
`guava/src` declares, and is built against `guava/src`; javac, given
`android/guava/src` first, finds that root's `UnsignedBytes`, which lacks it.
Guava's 8,457 ambiguous imports are test roots importing a package that both
`guava/src` and `android/guava/src` declare; only the Maven build says which.
The same-source-root rule is confirmed only for javac given the importer's
root first, as it is.

Calls are checked against javac's attribution with
`tools/compare_java_calls_with_javac.py`, which runs
`tools/JavaCallOracle.java` per source root in the same way. A link is
confirmed when javac binds the call to the same declaration (file, name and
line); a `new` binds the class it creates.

| Corpus | Links confirmed | Contradicted | javac bound none | Ambiguous, javac's among candidates | Missed (unresolved, javac bound) |
|---|---|---|---|---|---|
| google/gson | 4,107 / 4,110 | 0 | 3 | 121 | 5,340 |
| apache/commons-lang | 18,032 / 19,217 | 2 | 1,183 | 9,691 | 7,815 |
| google/guava | 48,177 / 48,285 | 35 | 73 | 39,124 | 37,067 |

Most missed calls are calls on variables (`attribute_call`: 5,333, 7,813 and
36,756) and methods inherited from library types (`unbound_name`). Most of guava's
ambiguous calls are test roots calling a type that both `guava/src` and
`android/guava/src` declare. Five ambiguous calls (one in commons-lang, four
in guava) leave out javac's method: an inherited overload, or a GWT
super-source file javac binds elsewhere. javac binds none
for 1,183 commons-lang links because those test calls pass lambdas to
JUnit-typed parameters, and JUnit is not on javac's class path; the links go
to the statically imported `LangAssertions` methods of the same name. The 37
contradictions (two in commons-lang, 35 in guava; under 0.1% of links) are
mostly overloads with the same arity elsewhere in the hierarchy that fit the
argument types better (`putBytes`, `hashBytes`, `append`), which a count of
arguments cannot tell apart. junit5 is not reported: javac
stops attributing most of its roots without their libraries, leaving
nothing to compare.

Each base in a class, interface, enum or record declaration (`extends`,
`implements`, and an interface's `extends`) is a `supertypes` fact with that
clause as its kind; type arguments are dropped (`Base<String>` is `Base`).
A base is looked up in the scope that encloses its declaration, by the same
type lookup as calls, and links with `tree-sitter.java-base-type`
(`name_matched`); one that names no repository type (`Object`,
`java.io.Serializable`) is `type_not_declared`. The same comparison with
`--bases` checks bases against the supertypes javac attributes:

| Corpus | Bases confirmed by javac | Contradicted | Ambiguous, javac's among candidates | Missed | Neither in repository |
|---|---|---|---|---|---|
| google/gson | 210 / 210 | 0 | 0 | 0 | 61 |
| apache/commons-lang | 588 / 588 | 0 | 0 | 0 | 159 |
| google/guava | 3,336 / 3,336 | 0 | 594 | 0 | 2,301 |

Guava's 594 ambiguous bases are test roots extending a type both
`guava/src` and `android/guava/src` declare.

## Rust

Rust sources (`.rs`) are analyzed when `rust` is selected (`--language rust`)
and the `rust` extra is installed; like JavaScript, Go and Java, the language
is opt-in. Declarations are functions, structs (`Struct`), enums, unions
(`Union`), traits (`Trait`), type aliases and associated types (`TypeAlias`),
modules (`Module`, both `mod name { ... }` and `mod name;`) and `macro_rules!`
macros (`Macro`). A function in an `impl` or trait body is a `Method`; other
functions, including foreign functions in an `extern` block and functions
inside function bodies, are `Function`s. Names are lexical: items are
qualified by the inline modules, traits and functions around them, and an
`impl` block qualifies its items by the type it implements for, the last path
segment without references, pointers or type arguments (`impl<'a> Draw for
&'a outer::Shape` names `Shape.draw`). An impl for a type with no name (a
tuple, slice, array or trait object) adds no qualifier. A raw identifier
(`r#match`) names the bare word. Items in a file module are not qualified by
the module name, as a file's items in the other languages are not. Constants,
statics, fields, enum variants and closures are not declarations, and items
written inside a macro invocation (`ffi_fn! { fn f() {} }`) are not parsed,
except in item macros (see "Rust item macros" below).
A `mod name;` declaration is also an import fact: it includes a file as a
module (see "Rust module files" below), as are `Cargo.toml` crate roots and
`use` declarations. Calls and impl blocks are facts (see "Rust calls" and
"Rust impls" below).

Checked against rust-analyzer's SCIP index (`rust-analyzer scip`, rust-analyzer
1.97.0) with `tools/compare_rust_declarations_with_scip.py`. A declaration
matches when rust-analyzer defines a global symbol of the same name on the
same line of the same file, and a method's SCIP symbol must name the type or
trait it was qualified by. Every difference is classified from the parse tree,
and none is left unexplained:

| Corpus | Matched | Owner differs | Tree-sitter only: `mod name;` / inactive `#[cfg]` / file in no crate | SCIP only: file module / inside a macro invocation / macro-generated |
|---|---|---|---|---|
| BurntSushi/ripgrep `4a51093` | 3,511 / 3,636 | 2 | 84 / 30 / 11 | 83 / 843 / 7 |
| rust-lang/regex `fde4946` | 4,945 / 5,436 | 32 | 208 / 190 / 93 | 196 / 147 / 10 |
| serde-rs/serde `5dfc8b0` | 2,635 / 3,017 | 11 | 52 / 28 / 302 | 57 / 174 / 10 |
| tokio-rs/tokio `6bbeecf` | 7,994 / 10,577 | 15 | 536 / 1,091 / 956 | 402 / 413 / 1 |

rust-analyzer indexes one configuration: items behind an inactive `#[cfg]` and
files no crate builds (fuzz targets, compile-fail tests) have no SCIP
definition, while the declarations here cover every configuration. A
`mod name;` is declared where it is written; rust-analyzer defines that module
at line 1 of its file. Owner differences are methods of impls for unnamed
types (38), which have no qualifier here, and impls written through a type
alias (22: `impl OwnedDFA` in regex, `impl From<SignalKind> for c_int` in
tokio), which are qualified by the alias as written while rust-analyzer names
the aliased type. Items read from an item macro are classified in the body
as parsed, and count as behind an inactive `#[cfg]` when the macro adds one
(tokio's `cfg_rt!` without the `rt` feature). rust-analyzer leaves a `target`
directory with build output in the root; remove it before comparing.

### Rust item macros

Many crates gate groups of items with a macro that emits each item it is
given, adding attributes (`cfg_rt! { mod runtime; pub use runtime::Runtime; }`
in tokio, where `cfg_rt!` adds `#[cfg(feature = "rt")]`). Before extracting,
the analysis reads every `macro_rules!` definition in the selected Rust
sources. A macro is an item macro when each of its definitions has one rule
whose matcher is `($($i:item)*)`, optionally after `#![$m:meta]`, and whose
transcriber is `$( #[...]* $i )*`: the items, each after attributes, and
nothing else. The names are recorded as the analyzer input
`rust-item-macros`, and a change to them parses every Rust file again on an
incremental run.

An invocation of an item macro at module level (in a file or an inline
module, by its name or a path ending in it) is read as the items in its
body: the body is parsed on its own, in place, so positions are those in the
file, and the items count as written where the invocation is, for
declarations, `mod` and `use` facts and declaration text. Invocations inside
item macros are read the same way. A body that does not parse as items, an
invocation in a function body and a macro with any other definition are left
unread. The attributes a macro adds are not applied: as for `#[cfg]` written
out, every configuration is analyzed.

### Rust module files

Each `mod name;` (a module without a body) is an import of kind `module`
whose specifier is the module's path from the declaring file's module
directory (`outer/name` inside `mod outer { ... }`); with a `#[path = "..."]`
attribute it is of kind `module_path`, the specifier is the attribute's path
and `names` are the inline modules around it. The first `#[path]` counts, as
in rustc. Resolution follows Cargo's module layout and links the one file it
names with rule `tree-sitter.rust-module-file`:

- A crate root (a file a `Cargo.toml` target names, `lib.rs`, `main.rs`,
  `build.rs`, a file directly in `src/bin`, or a file directly in `tests`,
  `benches` or `examples` outside any `src` directory that no `mod`
  declaration names) and a `mod.rs`
  declare their modules beside them; any other file `a.rs` declares them in
  `a/`. Inline modules add a directory each.
- A module `m` is `m.rs` or `m/mod.rs` there. When both exist the import is
  ambiguous (`module_file_ambiguous`); when neither does it is unresolved
  (`module_file_missing`).
- A `#[path]` outside inline modules is read from the declaring file's
  directory, inside them from the inline modules' directory.

`#[cfg_attr(..., path = "...")]` is not read, a module declared inside a
macro invocation other than an item macro is not seen, and paths through
symbolic links are not followed (the analysis does not read linked files).

### Cargo manifests

When Rust is selected, each `Cargo.toml` is read (with `tomllib`, or `tomli`
on Python 3.10) and its package's crates become import facts from the
manifest to the crate root file, linked with rule
`tree-sitter.rust-crate-root` when that file exists:

- `crate_library`: the package's library, `[lib]` `path` (default
  `src/lib.rs`), named by `[lib]` `name` or the package name with `-` as
  `_`. A package without a library has an unresolved default
  (`crate_root_missing`).
- `crate_target`: each `[[bin]]`, `[[test]]`, `[[example]]` and `[[bench]]`
  that gives a `path`. Targets Cargo discovers by layout are not written in
  the manifest and are not facts; the module layout above covers them.

The specifier is the path from the manifest's directory, `names` are the
package name and target kind, and `local_names` the crate name; the fact is
located at the target's table header (`[package]` for an implied library).
A manifest without `[package]` (a virtual workspace) declares no crates, and
one that is not valid TOML is `partial` with a `syntax_error` diagnostic.

Checked with `tools/compare_rust_crates_with_scip.py`: rust-analyzer defines a
`crate/` symbol, named by the package, at line 1 of each crate root it
indexes.

| Corpus | Manifests | Confirmed (lib / other targets) | Package differs | Not indexed | No library | SCIP crate roots found by layout / unexplained |
|---|---|---|---|---|---|---|
| ripgrep | 11 | 9 / 3 | 0 | 1 | 2 | 8 / 0 |
| regex | 8 | 6 / 4 | 0 | 8 | 2 | 1 / 0 |
| serde | 7 | 4 / 0 | 0 | 0 | 2 | 25 / 0 |
| tokio | 13 | 7 / 38 | 0 | 2 | 5 | 260 / 0 |

Not indexed targets are fuzz binaries outside the workspace. Every crate root
rust-analyzer indexes is either named by a manifest here or sits where
Cargo's target discovery looks (`src/main.rs`, `src/bin`, `tests`,
`examples`, `benches`, `build.rs` of a package).

Checked against rust-analyzer's SCIP index with
`tools/compare_rust_modules_with_scip.py`: rust-analyzer references the
module's symbol on the declaration and defines that symbol at line 1 of the
file it includes, so a link is confirmed when it reaches that file.

| Corpus | Declarations | Confirmed | Contradicted | Missed | Not indexed (linked / unresolved) | Not compared |
|---|---|---|---|---|---|---|
| ripgrep | 84 | 83 | 0 | 0 | 1 / 0 | 0 |
| regex | 208 | 178 | 0 | 0 | 12 / 0 | 18 shared symbols, ours among them |
| serde | 52 | 48 | 0 | 1 `cfg_attr` path | 1 / 2 | 0 |
| tokio | 536 | 394 | 0 | 0 | 115 / 0 | 26 `#[cfg]` variants, 1 shared symbol |

Not indexed declarations are behind an inactive `#[cfg]` or in files no crate
builds; serde's two unresolved ones are `#[cfg(docsrs)]` paths through a
symbolic link. rust-analyzer gives a package's library and its test crates the
same module symbols (`regex-automata/src/dfa/mod.rs` and
`regex-automata/tests/dfa/mod.rs` are both `dfa/`), so a reference names two
files; the file linked here is one of them in all 18 cases. A module declared
twice in one file under different `#[cfg]`s (tokio's `process::imp`, unix and
`#[path = "windows.rs"]`) has one symbol for both declarations and is not
compared. Resolutions are identical across repeated runs, and every rule
above is covered by a test that fails when it is removed.

### Rust use declarations

Each leaf of a `use` tree is an import of kind `use` (a `path::*` glob is
`use_glob`), and `extern crate name [as alias];` is `extern_crate`. The
specifier is the full path joined by `::` (`a::{self}` is `a`, and a leading
`::` is kept), `names` are the inline modules around the declaration, and
`local_names` the name it binds, alias applied.

Resolution builds each crate's module tree. Crate roots are the files
`Cargo.toml` targets name, and `lib.rs`, `main.rs` and `build.rs` files and
undeclared files in Cargo target directories; every file reached from a root
through resolved `mod` declarations has a module path, and inline modules
extend it. A path starts at the crate root (`crate`), the declaring module
(`self`, `super`), a module or type declared in the declaring module, or a
workspace library by crate name (`name` or `::name`), and goes through
modules one segment at a time. A name in a module can be a child module, a
declared item, a name a use there binds, or, when nothing else, a name a glob
use supplies; declarations behind different `#[cfg]`s can give it several
meanings, and as every configuration is analyzed, each meaning is followed and
linked (a module declared once per `#[cfg]` links both files). A path that
continues past a name goes on only through modules and types. Each link is
to the file of the last module reached:

- `tree-sitter.rust-use-declared` when the path ends there or its next name
  is declared in that module (a function, type, trait, macro or module);
- `tree-sitter.rust-use-reexport` when the module binds the name with a use
  of its own (an `extern crate` or an explicit `use`, or the glob `use`s
  that supply it when there is none) and following those uses reaches the
  file that declares it, or a module the rest of the path continues from. A
  chain of uses in a cycle is not followed;
- `tree-sitter.rust-use-module` when the next name is neither declared nor
  bound there: a constant or static, or an item a macro generates. The link
  names the module the path goes through, not where the item is defined;
- `tree-sitter.rust-use-exported-macro` when the path ends at a crate root
  with the name of a `#[macro_export]` `macro_rules!` defined in any file of
  that crate (in any module), which the attribute places at the root. The
  link is to the file that defines it. Each Rust macro declaration records
  whether it carries the attribute (`macro_export`).

`extern crate` links to the library root (`tree-sitter.rust-extern-crate`),
and an `extern crate x as y;` in a crate root lets paths start with `y`.
A file in several crates is resolved in each and is ambiguous
(`use_context_ambiguous`) when they differ. Other outcomes: `external_crate`
(no workspace library has the name, as for `std`), `crate_ambiguous` (two
workspace libraries share it), `module_missing` (a module whose file is
unknown, or a module inside a function body), `super_outside_crate`, and
`module_context_unknown` (a file no crate root reaches, usually because its
`mod` declaration is written inside a macro invocation). A name that a `use`
brings into scope does not start another path, a `use` written in a function
body binds its name in that body only, and dependency renames in
`Cargo.toml` are not read.

Checked with `tools/compare_rust_uses_with_scip.py`: rust-analyzer has an
occurrence of the symbol each leaf names on the declaration's lines, defined
in some file.

| Corpus | Uses | Declared: confirmed / contradicted / not indexed | Re-export: confirmed / contradicted / not indexed | Exported macro: confirmed / contradicted / not indexed | Module: same file / defined elsewhere / not indexed or outside | `extern crate` | External: outside the repository / no symbol | Missed |
|---|---|---|---|---|---|---|---|---|
| ripgrep | 1,083 | 505 / 0 / 20 | 73 / 0 / 5 | 0 / 0 / 0 | 21 / 0 / 0 | 6 | 429 / 23 | 1 |
| regex | 1,808 | 900 / 0 / 135 | 183 / 0 / 27 | 1 / 0 / 0 | 28 / 0 / 6 | 0 | 491 / 37 | 0 |
| serde | 981 | 187 / 0 / 20 | 7 / 0 / 0 | 0 / 0 / 0 | 29 / 49 / 7 | 1 | 316 / 13 | 159 |
| tokio | 7,554 | 1,197 / 0 / 376 | 1,278 / 0 / 193 | 167 / 0 / 16 | 206 / 11 / 225 | 0 | 3,346 / 500 | 14 |

A use with several links (one per `#[cfg]` meaning) counts as confirmed when
rust-analyzer's file, from the one configuration it indexes, is among them
(regex 2, tokio 80).

Uses whose file no crate reaches and for which rust-analyzer has no
definition in the repository either are not counted above (serde 193, tokio
25).

No declared, re-export or exported-macro link is contradicted. Following re-exports moved
ripgrep's links to a re-exporting module from 73 to 0 (its `grep` crate
re-exports the others with `pub extern crate`) and regex's from 183 to 1 (a
`#[macro_export]` macro). Reading item macros moved tokio's misses from
1,311 to 14 and its links to a module the name is not declared in from 968 to
178, and placing `#[macro_export]` macros at the crate root moved them to
11 (and regex's last one to 0). The "defined elsewhere" links left are, in
tokio, macros defined inside another macro's invocation (`join!`,
`try_join!`) and a module declared in a macro that is not an item macro
(`runtime::signal`), and in serde, modules and re-exports inside
`crate_root!()`, a macro without arguments that is not an item macro. serde's misses are
`module_context_unknown` for the same reason: its modules are declared in
`crate_root!()`, so those files have no crate. The rest are a path through a
name another `use` bound (8, tokio), a file whose `mod` declaration is in a
macro that is not an item macro (6, tokio), a `use` of a type declared in a
function body (4, serde), a module chosen by `#[cfg_attr(..., path)]` (1,
serde), and one leaf name imported twice in one declaration whose other
occurrence the comparison matched (1, ripgrep).

### Rust calls

Each Rust call expression whose callee is a path is a call site, its callee
the path joined by `::` (`a::b::f`, `Type::new`, `::krate::f`; generic
arguments such as `f::<T>` and `Vec::<u8>::new` are dropped). A method call on
a receiver that is `self`, a name or a field of one is recorded as
`receiver.name` (`self.write`, `x.y.len`); other callees (`(f)()`,
`a().b()`, `<T as Trait>::f`) and macro invocations are not call sites. Calls
inside item macro bodies are read as other calls are.

A call is resolved as a use path would be, from the caller's module, after
the names the function bodies around it bind:

- `tree-sitter.rust-call-local-item` when a single name is a function or
  tuple struct declared in a function body around the call, innermost first;
- `tree-sitter.rust-call-path` when the path names a function or tuple
  struct. A `use` written in a block binds its name in that block only, and
  ahead of the module's names (its explicit uses first, then the names its
  globs supply); then the path is read from the caller's module through
  child modules, declared items, uses and globs, re-exports and `#[cfg]`
  meanings, as for `use` declarations.

Calls of functions declared in impls read the impl facts (see "Rust impls"):

- `tree-sitter.rust-call-associated` when a path `Type::f` names a type (read
  as a call's path is) or `Self::f` names the type of the impl around the
  call, and the impls written for that type declare `f`: an inherent impl's
  first, else a trait impl's, or a provided method of a trait the type
  implements that the impl does not override;
- `tree-sitter.rust-call-self-method` when `self.m()` is written in an impl
  (read as `Self::m`), or in a trait body, where it names the trait's own
  method `m` (supertraits are not read).

All four are `resolved`: Rust has no overloading, so a path names one
declaration in each configuration. A function declared once per `#[cfg]`, or
by two trait impls, is `ambiguous` (`multiple_declarations`). Impls for a type
alias are keyed by the alias, since the aliased type is not read: a call
through an alias is `type_alias`, and so is a call that would otherwise take
a trait's function when an impl for some alias declares the same name. Other
outcomes: `method_call` (a receiver other than `self`: receiver types are not
inferred), `associated_item` (a name inside a type that no impl declares, such
as an enum variant or a derived method), `unbound_name` (a single
name with no declaration or use in scope: the prelude, such as `Some`, or a
local closure), `external_crate` (the first name is not in scope and names no
workspace crate: a crate outside the repository, a standard type or a type
parameter), `not_declared` (the module reached does not declare the name),
`module_context_unknown` and `module_missing` as for uses. Each Rust `use`
written in a block records the block's span (`block`).

Checked with `tools/compare_rust_calls_with_scip.py`: rust-analyzer has an
occurrence of the symbol the callee's last name refers to on the call's line,
defined on the line of a declaration.

| Corpus | Call sites | Linked: confirmed / contradicted / not indexed or outside | Ambiguous, rust-analyzer's among the candidates | Missed: method call / associated item / type alias / other |
|---|---|---|---|---|
| ripgrep | 10,215 | 3,335 / 0 / 17 | 18 | 2,194 / 191 / 13 / 52 |
| regex | 17,373 | 5,689 / 0 / 503 | 48 | 4,102 / 303 / 26 / 69 |
| serde | 7,829 | 845 / 0 / 388 | 10 | 967 / 95 / 0 / 347 |
| tokio | 30,100 | 6,317 / 0 / 2,026 | 101 | 5,136 / 294 / 19 / 459 |

Seven tokio links differ from rust-analyzer's and are not counted as
contradicted. In five, rust-analyzer's occurrence names a module of the same
name that a `pub use m::m;` re-export brings in with the function
(`tokio_stream::once`), and a call cannot name a module. In one, its
declaration is written in a macro that forwards to another macro, which is
not read. In the last, it is another declaration of the same name in the same
module under a different `#[cfg]`. Missed calls are mostly method calls on
receivers other than `self`, which need receiver types, and associated
functions of types whose impls are generated (derives, `pin_project!`) or
not read (impls for a type alias, in regex's `impl OwnedDFA`). The other missed
calls are paths through a type parameter or a standard type that the
occurrence's line shares with a repository function (`external_crate`),
serde's files in no crate (`module_context_unknown`, 298), and enum variants
that a glob of an enum brings into scope (`unbound_name`). Resolutions are
identical across repeated runs.

### Rust impls

Each Rust `impl` block whose type is a path is an impl fact (`impls`, a fact
kind of its own): the type's path and the trait's path (None for an inherent
impl), each joined by `::` without type arguments, references or pointers (a
leading `::` kept), the impl's type parameter names (`generics`) and the
block's span. Impls written in item macro bodies are read as other impls are.
An impl for a tuple, slice, array, trait object or primitive type, an impl
whose trait is a qualified path (`<T as Trait>::Assoc`) and a negative impl
(`impl !Send for T`) are not facts.

The type and the trait are each resolved as a call's path is (see "Rust
calls"): from the impl's module, after the items and uses of the function
bodies around the impl. The type must name a struct, enum, union or type
alias, and the trait a trait, so a function sharing either name is not
taken. A type that is one of the impl's own type parameters (`impl<T> Trait
for T`) is `generic_parameter`; the other outcomes are those of calls. When
both resolve, an INHERITS_FROM edge runs from the type to the trait, with
`heritage` `impl`, the trait path as written (`base`), the impl's ID
(`impl_id`), line and column, and rule `tree-sitter.rust-impl-trait`, which is
`resolved`. The tool's `supertype_resolution` metric counts impls with a trait
by the trait's outcome, or by the type's (`type_...`) when the type is not
linked. `deepthought-graph bases|subtypes` and impact queries follow these
edges; `get_class_hierarchy` follows classes and interfaces only. Inherent
impls give no edge; calls of the functions impls declare read them (see "Rust
calls").

Checked with `tools/compare_rust_impls_with_scip.py`: rust-analyzer has an
occurrence of the symbol the last name of the type, and of the trait, refers
to on the impl's first line, defined on the line of a declaration. When the
name also occurs elsewhere on that line (`impl From<Instant> for Instant`),
the trait's occurrence is the last one before `for` and the type's the first
one after it.

| Corpus | Impls (with a trait) | Type: confirmed / contradicted / not indexed or outside / ambiguous | Trait: confirmed / contradicted / not indexed | Edges | Missed: type / trait |
|---|---|---|---|---|---|
| ripgrep | 439 (382) | 423 / 0 / 6 / 0 | 134 / 0 / 0 | 128 | 0 / 0 |
| regex | 902 (537) | 825 / 0 / 18 / 27 | 70 / 0 / 1 | 52 | 0 / 0 |
| serde | 408 (339) | 115 / 0 / 0 / 0 | 16 / 0 / 0 | 13 | 212 / 238 |
| tokio | 2,122 (1,727) | 1,496 / 0 / 252 / 40 | 316 / 0 / 56 | 242 | 4 / 0 |

Every ambiguous type is declared once per `#[cfg]`, with rust-analyzer's
declaration among the candidates. Most traits are outside the repository
(`Debug`, `From`, `Future`). The missed impls are in files whose module is
not known: serde's modules that `crate_root!()` declares and its tests that
reach `serde_core` through them, and two tokio files whose `mod` declarations
are written in `cfg_signal_internal_and_unix!`, which is not read as an item
macro. Another 203 tokio types are declared inside `pin_project!`
invocations, which are not read, so those impls stay unresolved
(`unbound_name`). Impls and resolutions are identical across repeated runs.

A Rust trait's supertraits are supertype facts of kind `extends`, owned by
the trait: the bounds after its name and the bounds of a `where Self: ...`
predicate, each a path joined by `::` without type arguments (a
higher-ranked bound `for<'a> Tr<'a>` names `Tr`); lifetimes and `?Sized` are
not. Each is resolved as an impl's trait is, from the trait's module, to a
trait (`tree-sitter.rust-supertrait`, `resolved`), and gives an
INHERITS_FROM edge with `heritage` `extends`. Checked with
`tools/compare_supertypes_with_scip.py --language rust`: rust-analyzer's
reference at the bound's last name confirms all 10 linked supertraits in
tokio (37 named), and the other corpora's are outside the repository (7 in
ripgrep, 28 in regex, 16 in serde, of which one, in a serde module that
`crate_root!()` declares, is `module_context_unknown`).

## C

C sources (`.c` and `.h`) are analyzed when `c` is selected (`--language c`)
and the `c` extra is installed (tree-sitter-c 0.24.1); the language is
opt-in. Declarations are those at file scope, in every preprocessor branch:
functions (`Function`, definitions and prototypes alike), structs, unions and
enums that are named and have a body (`Struct`, `Union`, `Enum`, including
those declared inside a struct's members, which C does not scope), typedef
names (`TypeAlias`) and macros (`Macro`, wherever they are defined, since a
macro holds for the rest of the file). Names are not qualified. Variables,
enumerators, fields, forward declarations, unnamed types and types and
prototypes declared in a function body are not declarations. Imports, calls
and other facts are not recorded for C yet.

Four shapes the grammar gives to code it does not fully read are read
deterministically. A function declared after a macro the grammar does not
know (`API T f(...)`) has its name in an error node before the parameters.
`API int (f)(...)` parses as a function declarator applied to `int(f)`, which
C does not allow without parentheses, so the name is that inner list's one
type name. `namespace n {` in a C++ branch parses as a definition with no
parameters, and its body is read as file scope, as is text in an error node.
When the grammar cannot parse a stretch (braces opened in both branches of an
`#if`), a function's declarator can stand alone in the error node, with its
type and body as separate items: a function declarator found there declares a
function, while one inside a pointer (a pointer to a function) does not.
A macro that contains an error node (a comment in a continued line can make
the grammar take a later word as the name) is left out.

Checked against clang with `tools/compare_c_declarations_with_clang.py`
(libclang 18.1.1): every `.c` and `.h` file is parsed by clang as a
translation unit, with each directory holding a header, and each one above
it, on the include path. A translation unit with a fatal error (an include
not found, usually a header generated at build time) is left out, since what
clang recovers after one is not the program. A declaration matches when
clang reports one of the same kind and name at its name's line in the same
file, and every difference is classified. A difference is put down to a
grammar error only when the file-scope item holding it contains an error,
when it lies in an error node, or (clang only) when the item before it does,
since the grammar can end that item with a missing token and read the
declaration as an expression:

| Corpus | Translation units (fatal) | clang | Matched | Tree-sitter only: inactive `#if` / file not reported / clang error / name is a macro / grammar error | clang only: macro-generated / type in a function body / clang error / grammar error |
|---|---|---|---|---|---|
| lua/lua `c42d820` | 68 (0) | 2,909 | 2,904 | 322 / 0 / 0 / 0 / 0 | 0 / 0 / 0 / 5 |
| jqlang/jq `5c001b1` | 79 (2) | 3,017 | 2,671 | 502 / 191 / 4 / 122 / 0 | 171 / 5 / 130 / 40 |
| redis/redis `f1d498b` | 848 (278) | 19,610 | 18,284 | 1,723 / 6,564 / 12 / 3 / 161 | 1,024 / 19 / 182 / 101 |

Inactive `#if` branches are those clang skipped in every translation unit;
the declarations here cover every branch. A file is not reported when every
translation unit that reaches it has a fatal error (most of redis's vendored
jemalloc, which needs generated headers). A clang error on the line means the
translation unit is missing a type from such a header. A name that is a macro
(`#define yyrestart jq_yyrestart` before `void yyrestart(...)`, written by
flex) is declared by clang under the macro's expansion. Grammar errors are
regions tree-sitter-c does not parse, mostly macros that wrap declarations
(`JEMALLOC_ALWAYS_INLINE`); those declarations are missed (clang only) or may
be misread (Tree-sitter only, 161 in redis). Macro-generated declarations
(clang only) are written by a macro invocation, as in jq's `LIBM_DD(...)`.

## SCIP references

The `scip` AnalysisTool stores compiler-backed references from a
scip-typescript index. It never runs the indexer itself.
`deepthought index-typescript WORKSPACE --node NODE --indexer SCIP_TYPESCRIPT_MAIN`
runs a provisioned scip-typescript 0.6.0 and writes `index.scip` with a
`index.scip.sources.json` receipt: the index digest plus the sha256 of every
TypeScript, JavaScript and JSON input, taken before and after indexing. The tool
refuses an index whose bytes or inputs no longer match the receipt, before and
after it reads the index, so a stored reference always belongs to the source it
was computed from.

```sh
deepthought index-typescript ./checkout --node "$(command -v node)" \
  --indexer node_modules/@sourcegraph/scip-typescript/dist/src/main.js
deepthought-graph index ./checkout --tool scip --repository-id public-project
deepthought-graph references --repository-id public-project --snapshot REVISION \
  --symbol 'scip-typescript npm . . src/`api.ts`/handler().'
deepthought-graph dependencies --repository-id public-project --snapshot REVISION --file src/api.ts
deepthought-graph dependents --repository-id public-project --snapshot REVISION --file src/api.ts
```

Each definition occurrence becomes a Symbol node with its SCIP `native_id`
(returned by `search`), and every other occurrence becomes a Reference node.
All edges are `resolved`:

| Rule | Edge |
|---|---|
| `scip.repository-contains-file` | Repository -> CONTAINS -> File for each indexed document |
| `scip.file-defines-symbol` | File -> DEFINES -> Symbol for each definition occurrence |
| `scip.file-contains-reference` | File -> CONTAINS_REFERENCE -> Reference for each other occurrence |
| `scip.native-symbol-reference` | Reference -> REFERENCES -> Symbol when both carry the same native symbol; local symbols match only within their document |
| `scip.reference-dependency` | File -> DEPENDS_ON -> File when a reference's symbol is declared only in the other file, with the reference IDs as witnesses |

A symbol declared at several sites, such as an overloaded function or a merged
declaration, is one symbol: its references are `resolved` and link to every
site. The original tool wrote the same relationships with `confidence: 1.0`.
A native symbol defined by two different analyses is still `ambiguous` and gets
no edge, and an occurrence whose symbol has no indexed definition (a package,
or a name the indexer could not bind) is kept `unresolved` with no edge. The
queries return the stored facts only: `get_references` pages by `reference_id`
and needs `file_path` for `local` symbols, and both queries fail on a snapshot
that has no SCIP contribution instead of returning an empty answer.

`tools/typescript_reference_oracle.cjs` is an independent check. It runs the
TypeScript checker on the same workspace, without reading SCIP, and lists every
identifier whose symbol (aliases followed) is declared in the project.
`tools/compare_references_with_typescript.py` then compares each identifier
with the definitions its REFERENCES edges point to. On rxjs 7.8.1 `src/`
(251 files, TypeScript 6.0.3, scip-typescript 0.6.0):

| Outcome | Identifiers |
|---|---|
| Same declarations as the compiler | 8,951 |
| Extra declarations: one SCIP symbol for a parameter or type parameter across overload signatures (1,703), and `static now` and instance `now` of `Scheduler` sharing one symbol (10) | 1,713 |
| Fewer declarations: the compiler merges several declarations into one symbol and the index names one of them (an exported type and value of one name, 52; a property declared in several object types, 12) | 64 |
| `new X()` linked to the `constructor` instead of the class | 103 |
| Namespace import linked to the module's anchor instead of its file range | 10 |
| Overlapping (the `Scheduler.now` collision) | 1 |
| No SCIP occurrence, or occurrence left unresolved | 0 |

So every compiler-bound identifier has an edge. The 11 `Scheduler.now` links
are wrong at member level because the index gives static and instance members
of one name a single symbol; every other difference names the same declaration
at another granularity. These are properties of the index that the graph
reproduces faithfully, not corrections DeepThought makes. A further 1,533 linked
occurrences have no compiler identifier: module specifier strings (1,220),
shorthand properties and destructuring names that are their own declaration.
All 41,014 edges round-trip through Neo4j with their rule.

The work-in-progress branch also had a second reference source: a pinned
TypeScript 6.0.3 compiler run at index time that stored its own alias-closure
reference evidence beside SCIP and checked the current files again at query
time. It is not ported. The table above shows SCIP already gives every
compiler-bound identifier in rxjs an edge; the compiler adds only references
through a renamed re-export's import name, such as `outward` in the
`scip_alias_reexport` fixture, which the graph keeps unresolved rather than
guessed. A second compiler pipeline in the engine, pinned to one compiler
version, costs more than that gap. The compiler stays an offline oracle
(`tools/typescript_reference_oracle.cjs`).

## SCIP implementations

The same tool turns each SCIP `is_implementation` relationship into
Symbol -> IMPLEMENTS -> Symbol under `scip.symbol-implementation` (`resolved`)
when both symbols are declared in the analyzed files. A symbol declared at
several sites links from and to every site, and a `local` symbol matches only
within its document. A relationship whose target is not declared in the
analyzed files (a library type, or a file left out of the selection) gets no
edge and is counted in `implementation_relationships.external_target`.

```sh
deepthought-graph supertypes --repository-id public-project --snapshot REVISION \
  --symbol 'scip-typescript npm . . src/`dog.ts`/Dog#'
deepthought-graph implementations --repository-id public-project --snapshot REVISION \
  --symbol 'scip-typescript npm . . src/`base.ts`/Base#greet().'
```

The MCP tools are findImplementations and getSupertypes. Both queries need
`file_path` for `local` symbols and refuse a snapshot with no SCIP
contribution.

What scip-typescript 0.6.0 records decides what the edge means, so it is
stated here rather than refined by guessing:

- One relationship covers `extends` and `implements`, and includes indirect
  supertypes (a class implementing `Pet` also gets `Animal` when
  `Pet extends Animal`). IMPLEMENTS therefore means "is a subtype of", not
  "names in its own heritage clause".
- A class member that overrides or implements a supertype's member of the
  same name gets the same relationship.
- An interface gets none, so `interface A extends B` produces no edge.
- No `is_type_definition` is emitted, so the original tool's TYPE_OF mapping
  has no source. The original also guessed EXTENDS from words such as
  "extends" in hover documentation; that is not deterministic and is not
  ported. Direct heritage edges belong to a syntax rule instead.

`tools/typescript_supertype_oracle.cjs` is an independent check. It runs the
TypeScript checker on the workspace, without reading SCIP, and lists each class
and interface's transitive heritage supertypes and the same-named members of
those supertypes. `tools/compare_implementations_with_typescript.py` compares
them with the IMPLEMENTS edges. On rxjs 7.8.1 `src/` (251 files, 116 classes
and interfaces, TypeScript 6.0.3, scip-typescript 0.6.0):

| Outcome | Pairs |
|---|---|
| Same type pair as the compiler | 112 |
| Same member pair as the compiler | 88 |
| Missing because the source is an interface (20 type, 2 member pairs) | 22 |
| Any other missing pair | 0 |
| Edge the compiler does not list | 0 |

Every class-sourced pair matches and nothing is added; the 22 missing pairs
are the interface case above. All 200 edges round-trip through Neo4j with
their rule, and two runs produce identical edges.

## Service contracts

The `service-contracts` tool (`deepthought-graph index --tool tree-sitter
--tool service-contracts --language java --snapshot REV`) links services
through the HTTP contracts their code declares. The original `api_contract`
tool scored string literals that looked like API paths (0.95 for `/api/v1/x`,
0.75 for `/x/{id}`) and inferred the method from nearby words; it never matched
clients to servers. None of it is carried over.

Facts are read from Java sources outside test source sets (`src/test/`):

- **Routes.** A method carrying `@GetMapping`, `@PostMapping`, `@PutMapping`,
  `@DeleteMapping`, `@PatchMapping` or `@RequestMapping` imported from
  `org.springframework.web.bind.annotation`, in a `@RestController`/`@Controller`,
  a class with `@RequestMapping`, or an interface. Paths are joined with the
  type's `@RequestMapping` path and the service's context path; each path and
  type path gives its own route, and `@RequestMapping` without `method` accepts
  any method. Each route is an `HttpRoute` node linked from its handler by
  `HANDLES`.
- **Requests.** Methods of a `@FeignClient` interface (its `name`, `path` and
  `url`, then each method's mapping), `RestTemplate`/`RestOperations` calls
  (`getForObject`, `postForEntity`, `exchange` with an `HttpMethod` argument,
  and the rest) on a name the file declares with that type only, and WebClient
  `.get()/.post()/.method(HttpMethod.X)...uri(...)` chains that start at a name
  declared as `WebClient` or `WebClient.Builder`, or at `WebClient` itself.
- **URL values.** String literals, `+`, `String.format` with `%s`/`%d`, String
  fields of the file with an initializer, no `@Value` injection and no
  assignment anywhere in the file, String locals declared once and never
  assigned, and calls to a method of the file whose body is a single `return`
  (arguments substituted). A local may also take its value from `=`
  assignments that come before the use: one statement in the declaration's own
  block replaces the value, and assignments in different branches of an `if`
  that ends before the use each give one alternative URL, plus the initializer
  when the branches do not cover every path (a `null` initializer gives none).
  Each alternative is its own request, and its edge records
  `url_alternatives`; more than 8 alternatives are unknown text. Everything
  else is unknown text. A route with
  unknown text is not read; a request keeps it as a placeholder, and URI
  template variables (`{id}`) are unknown text too.
- **Services.** A file belongs to the build module of its nearest `pom.xml` or
  `build.gradle`. The module's `spring.application.name` and
  `server.servlet.context-path` (or `server.context-path`) come from
  `src/main/resources/{bootstrap,application}.{yml,yaml,properties}`, plus the
  `<name>.*` and `application.*` files in the `classpath:` native search
  locations of any Spring Cloud Config server module in the repository. A key
  with several values or a `${...}` value gives none. Profile files,
  environment variables and remote config repositories are not read.

Each request is resolved by `deepthought.service_contracts.resolve`:

| Rule | Evidence | Links |
|---|---|---|
| `service-contracts.spring-route-mapping` | pattern_matched | handler -> HANDLES -> HttpRoute |
| `service-contracts.http-service-path` | pattern_matched | the request names a service (a Feign client without `url`, or the URL's literal host) equal to exactly one service's `spring.application.name`; only that service's routes are candidates |
| `service-contracts.http-path` | pattern_matched | the request names no known service; every route in the repository is a candidate |

The URL's path is compared segment by segment with each candidate's
template, and the method must be accepted. A literal segment equal to a route
literal beats a route variable, unknown text fits a route variable better than
a route literal, `**` matches any rest, and an exact method beats a route that
accepts any method; ranks are compared from the left, as Spring orders its own
routes. A single best route is linked by `REQUESTS` from the calling
declaration (the Feign method, or the declaration around a call) or the file;
a tie is ambiguous and a request with no route is unresolved. Neither gets an
edge: File nodes list them in `ambiguous_http_requests` and
`unresolved_http_requests`, and tool metadata counts resolutions and the
mappings that could not be read. A URL that begins with unknown text (a base
URL variable) is assumed to add no path segments; gateways, path rewriting and
routes registered in code are not followed.

Handlers and callers carry the tree-sitter declaration identities, so in one
revision with tree-sitter output the routes join the code graph; run alone,
the tool emits just the declarations and files its edges need.

### Validation

`tools/check_contracts_ground_truth.py CORPUS.json CHECKOUT` scores the tool
against labelled corpora kept in `evals/fixtures/service_contracts/`. They were
built independently of the extractor from each project's source, docs and the
microSecEnD dataset (Schneider et al., MSR 2023), pinned by commit, with every
client and server line checked against the checkout (see that README). A
predicted link is a true positive when its client and server lines are within
3 lines of a labelled link; predictions at labelled don't-care sites (tests,
external APIs) count for neither side. Direct service-to-service calls:

| Corpus | Labelled direct HTTP links | Predicted | Correct | Missed |
|---|---|---|---|---|
| piggymetrics (Feign, config-server context paths) | 3 | 3 | 3 | 0 |
| apssouza22/java-microservice (Feign; one client has no server) | 3 | 3 | 3 | 0 |
| spring-petclinic-microservices (WebClient) | 2 | 1 | 1 | 1: the base URL field has a setter, so its value is not fixed |

No prediction was wrong. The remaining labelled HTTP links in these corpora go
through a gateway or come from browser code, and the Python, JavaScript, Go
and gRPC links of Online Boutique and robot-shop belong to languages this tool
does not read yet; both are left for later.

Train-ticket (FudanSELab, commit `313886e9`, 41 Spring services using
RestTemplate) has no labelled corpus, so its predictions were audited instead
(`evals/fixtures/service_contracts/train-ticket-audit/`). A reviewer who did
not see the extractor checked a random sample of 40 links against the source by
hand and found all 40 correct, then checked every unlinked request. Ten build
their URL in an `if`/`else` with a literal in each branch; with branch
alternatives all ten now link to exactly the two handlers the reviewer named
(20 links). Of 157 requests outside tests, 153 link. The other four are rightly
unlinked: one calls an external host, one a path no service serves, and one
call site posts to `.../order/{id}` in two services that accept only GET and
DELETE there, which leaves the request ambiguous among POST routes.

## Commit-linked snapshots

The original engine kept one mutable `last_commit` on the Repository node and
updated the graph in place. Here a snapshot is identified by its content and never
changes, so the commit is recorded beside it: the AnalysisSnapshot node lists every
commit it was analyzed at in `source_commits`. The same content analyzed at two
commits (for example when only a README changed) is one snapshot linked to both.
Links are outside the content fingerprint, so adding one never changes a snapshot.

`deepthought-graph index` reads HEAD before analysis and links the snapshot only
when:

- HEAD did not move while files were read;
- every analyzed file is tracked by Git (untracked and ignored files are not part
  of any commit);
- no analyzed file differs from that commit, staged or not.

Changes to files that were not analyzed do not matter. The index result reports
`source_revision` with the commit, whether it was linked and, if not, why
(`not_a_git_checkout`, `head_moved_during_analysis`, `untracked_files_analyzed`,
`uncommitted_changes`, `git_status_unavailable` or `no_analyzed_files`). An
unlinked snapshot may still equal some commit; it is just not claimed.

`list_snapshots(repo, commit=None)` (CLI `snapshots`, MCP `listSnapshots`) lists
complete snapshots, newest first, with their content hash, tool, counts and
commits; with a commit, only snapshots linked to it. Commit names must be full
lowercase hex. Queries still select one snapshot explicitly.

## Incremental re-indexing

The original updater found changed files from Git history and patched one graph
in place. Here a re-index produces a complete new snapshot, but files whose bytes
have not changed keep the facts an earlier analysis recorded instead of being
parsed again. The result is byte-identical to a full analysis, so it has the same
snapshot ID and an unchanged re-index is reused at publication.

A file's facts are reused only when the earlier analysis:

- is a schema 3 tree-sitter analysis of the same repository identity by the same
  engine version (`deepthought-analysis-engine` is now recorded in
  `analyzer_versions`);
- recorded the same SHA-256 for the file, in the same language, parsed by the same
  tree-sitter and grammar package versions;
- fully parsed the file (`analyzed`, or `partial` with only syntax errors).

Every file is still read, hashed and checked against the limits. Import and call
resolution and the graph projection always run over the whole result, because
they cross files. The earlier artifact is trusted input, like a SCIP index: a
matching hash proves the file is unchanged, not that the recorded facts are right.

```sh
deepthought analyze . --repository-id public-project > before.json
deepthought analyze . --repository-id public-project --previous before.json > after.json
deepthought-graph index . --repository-id public-project --write-artifact before.json
deepthought-graph index . --repository-id public-project --previous-artifact before.json
```

`analyze --previous` prints reuse counts on stderr; `index` reports them under
`reuse`, with `previous_rejected` naming why a whole earlier analysis was not used.

`tools/benchmark_incremental.py WORKSPACE` times full against incremental analysis
on a copy of a workspace and fails if any artifact differs. Medians of 5 runs on a
4-CPU cloud container (Python 3.13), extraction only:

| Corpus | Parsed files | Changed | Full | Incremental |
|---|---|---|---|---|
| Django 5.1.4 sdist (Python) | 2,788 | none | 11.59 s | 0.47 s |
| | | 1 file | 11.23 s | 0.40 s |
| | | 279 files (10%) | 11.51 s | 1.55 s |
| rxjs 7.8.1 `src/` (TypeScript) | 251 | none | 0.31 s | 0.02 s |
| | | 1 file | 0.30 s | 0.02 s |
| | | 26 files (10%) | 0.27 s | 0.05 s |

End to end, indexing Django's `django/` package (879 files) took 4.0 s to analyze
and 5.8 s to publish to Neo4j; an incremental analysis took 1.3 s, and publishing
is unchanged because a snapshot is always written whole. These are single-machine
measurements, not a performance guarantee.

## Graph diff

`diff_snapshots(repo, base_snapshot_id=..., head_snapshot_id=...)` (CLI `diff`,
MCP `diffSnapshots`) compares two complete snapshots of one repository:

- Nodes are matched by qualified name. Declaration identities do not depend on
  source positions, so a declaration that only moved is `changed` (for example its
  `start_line`), not removed and added. A node is changed when its labels or any
  stored property differ; each changed node lists the differing properties with
  their base and head values.
- Edges are matched by `edge_id`, the content hash of their type, endpoints and
  properties. An edge whose properties changed, such as a call that moved to
  another line, is listed as removed and added.

Every category (`nodes.added`, `nodes.removed`, `nodes.changed`, `edges.added`,
`edges.removed`) is always counted in `summary`; `changes` (CLI: repeat
`--change`) selects which categories list items. Listings are ordered and bounded
by `limit` and the output byte budget; counts are not. Together with
`list_snapshots(commit=...)`, this compares the graphs of two commits.

## Edge queries

Callers, callees, imports and importers return each stored edge with its
properties, including `evidence_type`, `rule_id` and a stable `edge_id`. The
`edge_id` is the content hash the writer computes for the edge within its
snapshot; Neo4j's `element_id` is internal to one database and is not an identity.
Results are ordered by endpoint, line, column and `edge_id`, so the same snapshot
gives the same page.

Pass `evidence_types` (CLI: repeat `--evidence`) to keep only edges with those
evidence types, for example only `resolved` edges when name matches are not
good enough. An empty filtered result still reports `unsupported_or_unobserved`:
it shows what was stored, not that no such edge exists.

`explain_edge(edge_id)` (MCP `explainEdge`, CLI `explain-edge`) returns one edge
of the selected snapshot with:

- its source and target declarations or files;
- its location: the source file, plus the line and column the edge records (or
  the declaration's start line for DEFINES);
- its evidence type and what that type does and does not establish;
- its rule ID and the rule's description;
- `rule_status`: `registered`, `unregistered` (the installed version does not
  know the rule) or `conflicts_with_registry` (the stored evidence type differs
  from the one the rule now declares);
- a one-line `statement`, for example
  `CALLS from run to helper at main.py:15:11. Evidence: name_matched, by rule
  tree-sitter.call-imported-name: ...`.

Snapshots store the evidence type and rule ID, not the rule text. The description
comes from the rules registered by the installed version, which is why
`rule_status` is reported. An unknown `edge_id` gives status `not_found`.

## Provision and use

```sh
pip install '.[python,typescript,neo4j,mcp]'
export DEEPTHOUGHT_NEO4J_URI=bolt://localhost:7687
export DEEPTHOUGHT_NEO4J_USER=neo4j
export DEEPTHOUGHT_NEO4J_PASSWORD='your-local-database-password'
export DEEPTHOUGHT_NEO4J_DATABASE=neo4j

deepthought-graph index /path/to/public/checkout \
  --repository-id public-project --language python --include 'src/*.py'
```

Index initializes the required schema and returns the graph revision and actual
write statistics. Select that revision explicitly for subsequent operations:

```sh
deepthought-graph search --repository-id public-project --snapshot REVISION --query Service
deepthought-graph context --repository-id public-project --snapshot REVISION --file src/service.py --line 20
deepthought-graph structure --repository-id public-project --snapshot REVISION --file src
deepthought-graph importers --repository-id public-project --snapshot REVISION --file src/service.py
deepthought-graph callers --repository-id public-project --snapshot REVISION \
  --symbol QUALIFIED_NAME --evidence resolved
deepthought-graph explain-edge --repository-id public-project --snapshot REVISION --edge-id EDGE_ID
deepthought-graph snapshots --repository-id public-project --commit FULL_COMMIT_HASH
deepthought-graph diff --repository-id public-project --base BASE_REVISION --head HEAD_REVISION
```

Repeat `--tool NAME` for registered tool contributions and provide `--snapshot`
with the chosen graph revision. This port ships TreeSitterTool (declarations,
file imports, and name-matched calls and bases), the SCIP tool (TypeScript references
and file dependencies) and the components tool (build-manifest components); it does not pretend other original analyzers have already been
ported. The embedded Python API can
register tools through the existing registry.

For an agent, configure the stdio command `deepthought-mcp` with the same database
environment. The three original context/search/file tools and the edge tools
(`getFunctionCallers`, `getFunctionCalls`, `getBaseTypes`, `getSubtypes`, `getMembers`, `getOwner`, `getImpact`, `getFileImpact`, `getFileImports`, `getFileImporters`,
`findReferences`, `findImplementations`, `getSupertypes`, `getFileDependencies`, `getFileDependents`, `explainEdge`), the component tools (`listComponents`, `getFileComponents`, `getComponentFiles`, `getComponentDependencies`, `getComponentImpact`), `listSnapshots` and `diffSnapshots` are available by default. CLI and MCP results carry actual stored facts and bounded relationships;
they do not dump the complete graph into the agent prompt.

## Cypher scope and access

Arbitrary Cypher cannot be safely made repository-scoped by appending a filter.
Typed queries enforce repository/revision scope. The raw Cypher surface instead
requires explicit opt-in and verifies that the selected database contains only
the selected complete repository/revision. It rejects procedures, administration,
updates and multiple statements, checks the Neo4j EXPLAIN read-only query type,
bounds transaction time/result rows/serialized bytes, and always rolls back.
These restrictions are intentional; it is not unrestricted Neo4j administration.

**Database privileges or a read-only database configuration must enforce read-only
access for untrusted agents. Driver read routing is not access control.** See the
[Neo4j transaction documentation](https://neo4j.com/docs/python-manual/current/transactions/)
and [read-only database configuration](https://neo4j.com/docs/operations-manual/current/database-administration/standard-databases/configuration-parameters/).
Never point an agent at a privileged shared application database.

```sh
deepthought-graph cypher --repository-id public-project --snapshot REVISION \
  --allow-database-scope \
  --query 'MATCH (n:Symbol) WHERE n.name=$name RETURN n.name,n.file_path,n.start_line' \
  --parameters '{"name":"Service"}'
```

For MCP, enable `DEEPTHOUGHT_ALLOW_DATABASE_SCOPE_CYPHER=1` to advertise `queryGraph`.
The same dedicated-database checks and bounds apply. Reserved parameters
`repo_url`, `snapshot_id` and `fetch_limit` are supplied by the query service.

## Verification and validation

Default tests never start/download databases or call models. Actual graph-port
integration tests opt in through the database environment above. They operate on
unique test-owned repositories and clean their records. Use a dedicated empty
test database: raw Cypher tests deliberately require a single selected snapshot.
The CI graph-port job supplies an isolated Neo4j service and tests the installed
wheel, actual imports/readback, rollback/idempotence/shared-tool merges, queries,
CLI journey and MCP stdio transport.

The public evaluation harness supports `prepare(..., tool_access={"command": ...,
"help": ...})` or `deepthought-eval prepare --tool-access descriptor.json`.
This retains extraction output privately and supplies only the query interface
to the treatment agent. The runner must independently verify database contents,
isolate oracle/source access and retain native agent/query receipts. The harness
does not certify the endpoint just because its descriptor is present.

The [completed public-agent validation](../evals/results/neo4j_query_deepagents_20261102/README.md)
retains exact answers, native/query receipts and failures. All four corrected
runs scored 78/78 and both treatment agents successfully queried Neo4j. Aggregate
total tokens increased 19.4%; this port has no demonstrated general efficiency
gain. Tests establish existing-query behaviour on supplied facts; declaration-only
TreeSitter output does not supply caller/callee or distributed-system semantics.

For AnalysisTool agent evaluations, prefer the verified Neo4j preparation in
[the evaluation guide](evaluation.md). A descriptor alone no longer qualifies a
run: actual query receipts must match the verified database/repository/snapshot.
Optional `DEEPTHOUGHT_EVAL_*` audit settings instrument this same CLI/MCP query
implementation without exposing credentials or the oracle to the actor.
