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
| libs/analysis/.../tools/api_contract (string-pattern endpoint detector with confidence tiers; client matching never implemented) | deepthought.service_contracts + deepthought.analysis_tools.service_contracts | HttpRoute nodes with HANDLES and REQUESTS edges from parsed Spring, Flask, FastAPI, Express and Go routes and client calls, linked by declared path-template rules instead of confidence scores (see below) |
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
| `tree-sitter.python-from-import-submodule` | name_matched | `from pkg import x` where `pkg/x.py` exists; the package may define `x` instead. When `pkg/__init__.py` neither declares nor imports `x`, calls and bases read `x` as that module (`x.f()`) |
| `tree-sitter.typescript-relative-module` | resolved | `./x`, `../x`: `x.ts`, `x.tsx`, `x.d.ts`, then `x/index.*`; `.js`/`.jsx` map to their TypeScript sources; when JavaScript is analyzed, JavaScript files are tried after every TypeScript candidate, as with `allowJs`; `package.json` is not read |
| `tree-sitter.javascript-relative-file` | resolved | a JavaScript file's `./x.js`, `./x.jsx`, `./x.mjs`, `./x.cjs`: the analyzed file at exactly that path |
| `tree-sitter.javascript-extensionless-module` | pattern_matched | a JavaScript file's `./x`: `x.js`, `x.jsx`, `x.mjs`, `x.cjs`, then `x/index.js`, `x/index.jsx`, the bundler/CommonJS convention; Node ES modules would reject the specifier, and bundler configuration is not read |
| `tree-sitter.typescript-path-alias` | resolved | a TypeScript file's bare specifier matching a `compilerOptions.paths` pattern of the nearest `tsconfig.json` (else `jsconfig.json` in the same directory): an exact pattern, else the wildcard pattern with the longest prefix; each target, below `baseUrl` or the config's directory, is tried in TypeScript's order (TypeScript extensions on the file, then the directory's `package.json` `typings`, `types` and `main`, then `index.*`; then the same with JavaScript extensions); the first target that reaches an analyzed file wins. A nearer config without `paths` shadows a farther one; a config that `extends` another without settling `paths` and `baseUrl` itself leaves the specifier unresolved (`path_alias_inherited`), as does one that is not JSON with comments (`script_config_unreadable`); `include`, `exclude`, `references`, `exports` and `baseUrl` without `paths` are not read |
| `tree-sitter.javascript-path-alias` | pattern_matched | a JavaScript file's bare specifier resolved by the same `paths` lookup; the runtime reaches that file only through a matching bundler or test-runner alias, which is not read |
| `tree-sitter.script-declaration-source` | pattern_matched | a `paths` lookup that reaches a directory whose `package.json` `typings` or `types` names a declaration file that is not analyzed: the JavaScript file of the same name beside it (`src/index.d.ts` -> `src/index.js`), which the declaration describes by convention |
| `tree-sitter.go-module-package` | resolved | a Go import path inside the importing file's own module (nearest `go.mod`): the directory at that path below the module root, linked to every non-test file of its package; `main` packages are not importable, and `vendor/`, `replace` and `go.work` are not read |

`tsconfig.json`, `jsconfig.json` and `package.json` files are read as
configuration facts (File nodes with no IMPORTS of their own) when TypeScript
or JavaScript is analyzed. A `paths` target that is a `.js` file with a
`.mjs` or `.cjs` twin beside it is left ambiguous (`module_format_twins`):
which build runs depends on the loader.

Anything else gets no edge. Package specifiers that no `paths` pattern maps,
Python imports matching several roots, imports beyond the repository
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
enclosing declaration. In Python, JavaScript and TypeScript (and Go, below), a
method called on a call's, construction's or element's result, on `super` or
through an optional chain is recorded too, its receiver written with arguments
and indexes dropped (`f().g`, `new T().m`, `xs[].m`, `super.m`, `a?.b`,
`f?.().m`); no name-binding rule reads these, so they stay unlinked unless a
SCIP index links them. Other calls (`f()()`, `x[0]()`, `(a as T).m()`) are not
recorded. `deepthought.calls.resolve_calls` matches names only,
so every CALLS edge it yields is `name_matched`; shadowing at runtime, rebinding,
inheritance and dynamic dispatch are not checked. Calls in files a SCIP index
covers take their targets from the index instead (below); the original's SCIP
tool did not write CALLS. The original builder's `confidence` floats and its `resolved_method` label
for a bare `obj.method` match are not carried over, and `obj.method()` on an
arbitrary object gets no edge.

A call site that no rule links to exactly one declaration is still stored: an
`UnresolvedCall` node with its `callee`, `name` (the callee's last name, `param`
for `c.req.param()`), file, line and column, `status` (`unresolved` or
`ambiguous`), the `resolver` that left it unlinked (`tree-sitter`) and the
`reason` it gives and, when ambiguous, the
`candidates`' qualified names. Its caller (the innermost enclosing declaration,
or the file) reaches it by `HAS_UNRESOLVED_CALL`, a structural edge with rule
`tree-sitter.call-site-unlinked`. Every CALLS edge also carries `name`. So one
query counts the linked, ambiguous and unresolved calls of a name:
`get_call_sites` (CLI `call-sites --name N [--file F]`, MCP `getCallSites`)
returns those counts and the call sites, each linked one with its target and
rule. Snapshots written before this change have no such nodes and answer
`unsupported_or_unobserved`. Calls made through another name (an alias, a
callback, dynamic dispatch) are not counted.

Python, JavaScript, TypeScript and Go call sites in a file that a SCIP index
covers are bound from the index (`deepthought.scip_calls.bind_calls`;
`TreeSitterTool.analyze(..., scip_indexes=[(ingested index, receipt digest)])`).
Each call site records where the last name of its callee starts
(`callee_at`); the SCIP reference there names a symbol, and the symbol's SCIP
definition is read back to the innermost parsed declaration whose span holds it
and whose own name is the one written at it. Columns are converted between the
index's encoding and UTF-8 bytes; documents that leave the encoding unspecified
are read as scip-python and scip-typescript write them (UTF-16 code units) and
otherwise as bytes (scip-go). These edges carry `resolved` evidence, the
indexer as `producer` (`name@version`) and the index's `receipt` digest.
Several indexes of one indexer (a second pass, one index per Go module) share
their global symbols, so a call in one links to a declaration another defines:

| Rule | Links |
|---|---|
| `scip.call-occurrence` | the call to the declaration of the symbol the index records at its name |
| `scip.call-local-symbol` | the same, for a symbol the index declares local to the file (a nested function) |
| `scip.call-constructor` | `new C()` resolved to the class, or a class and its constructor resolved together, to the one constructor (of overloaded constructors, the implementation) |
| `scip.call-overload-implementation` | a call resolved to overload signatures (TypeScript overloads, `@overload`) to the one implementation beside them |

A covered call the index does not link stays unlinked, with `resolver: scip`,
the producer and receipt, and a reason: `scip_no_occurrence` (no reference at
the name), `scip_external` (a symbol defined outside the index),
`scip_target_not_declaration` (a definition no parsed declaration names, such
as a variable) or `multiple_declarations` (more than one target, with the
candidates). The parser's rules are not used for it. Two scip-python 0.6.8
faults are guarded: in a Python file, a target whose name is not the name
written at the call, unless an import binds that name to it (the index may
follow a star re-export to the wrong function), and an external target for
`self.x()` when the enclosing class or a repository base declares `x` (the
index may name a builtin). Both are `scip_suspect_target`: the parser's link
stands if it has one, otherwise the call stays unlinked. Files no index covers
keep the parser's rules, which also run in shadow over covered files;
`tools/ground_truth.py score ... --scip INDEX --disagreements OUT.tsv` writes
the covered call sites where the two targets differ (file, line, callee, SCIP
target file and line, parser target file and line, reason, rule). The
tool's `call_resolution_by_resolver` metric counts call outcomes separately for
`scip` (the index decided, its "nothing here" included), `scip_suspect_parser`
(the parser's link kept over a suspect index target) and `tree-sitter` (files
no index covers).

| Rule | Links |
|---|---|
| `tree-sitter.call-lexical-name` | `f()` to a function or class declared in an enclosing scope of the same file; class bodies are not enclosing scopes. In JavaScript and TypeScript the scopes are blocks and functions as the language defines them (`let`, `const`, `class` and functions per block, `var` and parameters per function, `for` and `catch` bindings), so a declaration inside an anonymous callback is not seen from a sibling one. The call links only to the declaration its scopes give: a binding that declares no function (a parameter, `let f = make()`) leaves it `local_binding`, and a name no scope declares is `unbound_name`. Imports, `require`, `with`, functions declared in nested blocks (sloppy mode), names declared twice and files with syntax errors keep the name lookup |
| `tree-sitter.call-imported-name` | `f()` to the top-level declaration that an import binding (aliases applied) names in its linked module file. In JavaScript and TypeScript a default import (`import delay from './delay'`) names the function or class that module exports as `default`: written `export default function f`, `export default class C`, or a module-level declaration named by `export default f` or `export { f as default }` (`Definition.default_export`). An anonymous default (`export default () => {}`) declares no name and gets no edge. A module-level declaration that a local `export { f as g }` (no `from`) exports also counts as the module's top-level `g` (`Definition.export_aliases`), for imports, `mod.g`, re-exports and bases alike; a module that also declares `g` makes the name ambiguous. An imported name exported again this way follows `js-call-reexport` |
| `tree-sitter.call-module-attribute` | `mod.f()` where `mod` is a module import binding (`import m`, `import a.b as m`, `import * as m`); Python `import a.b.c` also binds `a` and `a.b` to the `__init__.py` of each package on the path |
| `tree-sitter.go-call-typed-receiver` | Go (`resolved`): `x.M()` or `x.f.g.M()` where `x` is declared in the function around the call or, when nothing in the function declares it, by exactly one top-level `var` of the package (a name declared twice, also in two files for different platforms, or a `var` with neither type nor value, gives no type). A variable of another file is named by `CallSite.receiver_variable`, and each Go file's `PackageClause.variables` lists its package-level variables with the type their declaration writes or a value `T{...}`, `&T{...}`, `new(T)`, `F()` or `pkg.F()` gives. `pkg.V.M()` follows the variable `V` of the package the file imports as `pkg`. The receiver may also be a call or index chain, `a.F().M()`, `a.f[i].M()` or `F().M()`: the call is then named after the chain (`CallSite.callee` `a.F().M`, `a.f[].M`) and each call or index is one receiver step. The receiver's type is followed from what is written. The innermost visible declaration of `x` gives a type: a parameter, receiver or named result (`c *Command`; a variadic `...T` is a slice of `T`), `var x T`, a typed `const`, or a value: `T{...}`, `&e`, `*e`, `new(T)`, `make(T, ...)`, `e.(T)`, another variable, `e.f` (the field's declared type), `e.M()` (the method's first declared result; `x, err := e.M()` too), `F()` or `pkg.F()` (the first declared result of the one function `F` of the caller's package, or of the package the file imports as `pkg`, when no variable in the function is named `F` or `pkg`; `CallSite.receiver_function` names it), `e[i]` and `for _, x := range e` (the element of a slice or array, or of a named slice type: `Definition.underlying_type` holds `[]T` for `type S []T`), `T(e)` (a conversion to the one non-alias type `T` of the package), `f()` where `f := func() T {...}` (the literal's first result). Fields and results are read from the declarations of the struct and method, so `Definition.fields` and `Definition.result_type` hold them, and the steps a call still needs are `CallSite.receiver_steps`. Each step must find one struct or method in the package that declares the type (the caller's, or for `pkg.T` the package that file imports as `pkg`). It links to the method `M` declared on the type, interface methods included. A step that finds nothing or more than one declaration, a map, channel or function type, or a type parameter leaves the call unresolved (`receiver_type_unknown`). Methods and fields promoted from embedded fields are not followed (`not_declared_on_type`), an embedded field by its own name is. A function that declares a local type of that name gets no link |
| `tree-sitter.python-call-reexport` | Python: `f()` or `mod.f()` where the named module does not declare `f` but binds it with exactly one module-level `from m import f` (as `click/__init__.py` does with `from .utils import echo`); that import is followed, step by step, to the module that declares `f` |
| `tree-sitter.js-call-reexport` | JavaScript/TypeScript: `f()` or `mod.f()` where the imported module does not declare `f` but re-exports it, as preact's `src/index.js` does with `export { render } from './render'`. A named `export { f } from` (aliases applied) is followed first, then every `export * from`; each step goes to the module that declares `f`. A name two star exports give is ambiguous, as in the language. A default re-exported by name (`export { default } from`, `export { f as default } from`) is followed like any name, and `export *` never gives `default`. An imported name exported again by a separate `export { f }`, `export { f as g }` or `export default f` (`Import.reexported_as`) is followed as the `export { ... } from` it amounts to, as preact's `compat/src/index.js` does with `import { render } from './render'` and `export { render }`. `export * as ns from`, a namespace import exported again, and cycles are not followed |
| `tree-sitter.call-self-method` | `self.m()` / `this.m()` to a method declared in the enclosing class body |
| `tree-sitter.python-call-super` | Python: `super().m()` written directly in a method of class `C` (not in a function nested in it) to the `m` that `C`'s declared bases declare or inherit, in the method resolution order after `C`; a class reached twice or a base that names no repository class leaves it unresolved (`diamond`, `base_not_resolved`) |
| `tree-sitter.python-call-self-inherited` | Python: `self.m()` where the class around it declares no `m`, to the `m` its declared bases declare or inherit, in the same order and with the same stops as `tree-sitter.python-call-typed-receiver`; a subclass's override is not followed |
| `tree-sitter.python-call-typed-receiver` | Python: `x.m()` where the binding of `x` the call sees names one repository class `T`: a parameter annotated `T`, `m.T`, `"T"`, `Optional[T]` or `T \| None`, or a local whose bindings in the function are all `x = T(...)` or `x: T = ...` for the same `T`, apart from assignments of `None`. The function must bind `x` in no other way and import nothing in its body. It links to `m` declared on `T` or inherited through its declared bases, depth first and left to right, which is the method resolution order when no class is reached twice; a class reached twice, or a base that names no repository class before `m` is found, leaves the call unresolved (`diamond`, `base_not_resolved`). A written `object` base is skipped. A name that is not one repository class (`str`, a factory function) leaves the call to the other rules |
| `tree-sitter.python-call-result-receiver` | Python: `x.m()` where the local `x`'s one binding in the function is `x = f(...)`, under the same conditions as `tree-sitter.python-call-typed-receiver`, and the call `f(...)` links to a function or method whose return annotation names one repository class `T` (as a parameter annotation does, looked up where that function is defined): `m` declared on `T` or inherited, with the same order and stops. Repeated while links are added, so `f(...)` may itself be such a call. On click this and allowing several same-class assignments confirmed 24 more calls (3,028), none contradicted, and raised impact from 742 to 750 of 1,080 |
| `tree-sitter.python-call-attribute-receiver` | Python: `x.a.m()` (any number of attributes) where `x` is the first parameter of an instance method directly in a class body (not a `staticmethod` or `classmethod`, not rebound), or has a class under the conditions of `tree-sitter.python-call-typed-receiver`. Each attribute's class comes from the first class in the method resolution order that declares that name: an annotation in the class body (`a: T`) or on `self.a: T = ...` decides it; without one, every assignment the class's instance methods make must be `self.a = p` from a parameter `p: T`, `self.a = T(...)`, or `None`, all naming the same `T`, as a type checker infers a member variable; anything else assigned, or an unannotated class-body assignment, leaves the attribute untyped. A class that declares anything else of that name, or a base that is no repository class reached first, stops the lookup. A generic annotation `C[...]` names `C` (for parameters too). On click this confirmed 18 more calls (3,046), none contradicted, and raised impact from 750 to 771 of 1,080 |
| `tree-sitter.js-call-static-method` | JavaScript/TypeScript: `C.m()` to the method `m` that the class `C` declares `static` (`Definition.static`), as ky's `Ky.create(...)` and `NonError.wrap(...)`. `C` is the class declared where the call's scopes see `C` (`CallSite.binding` records that declaration for `x.m` calls too), or the one class a named or default import binds to `C`, followed as for `call-imported-name`. A static method `C` inherits is not followed (`static_not_declared`), and a name a scope binds to anything else (a parameter, `const C = other`) leaves the call `attribute_call` |
| `tree-sitter.js-call-typed-receiver` | JavaScript/TypeScript: `x.m()` where the declaration of `x` the call sees (`CallSite.binding`) is a variable or parameter annotated `x: C`, `x: C<...>`, `x: C | undefined` or `x: C | null`, or a variable initialized with `new C(...)`. `C` is found as for `js-call-static-method`, from where `x` is declared (`CallSite.receiver_type` and `receiver_binding`), and must be one class, or one interface declared at the top level of the file or of the module an import of `C` names (re-exports are not followed for interfaces), that nothing else of that name merges with. For a class it links to the instance method `m` that `C` declares, or that it inherits through `extends`, class by class. For an interface it links to the method signature `m` that `C` declares, or else to those the interfaces it extends declare, all of them searched and one method reached twice counted once; an extended type that is no repository interface (a class, an external type) stops the lookup (`base_not_resolved`). An instance field or constructor parameter property named `m` on the way hides the method (`field_not_method`, `Definition.properties`), and a base that names no repository class stops the lookup (`base_not_resolved`), since it may declare `m`. A file that declares a type alias or type parameter named `C` anywhere, or an interface named `C` in a namespace or function, is not read, since that name may be the type. An interface is a type, so `C.m()` on one is not followed. Member chains and `this` follow `js-call-member-chain`. Receivers typed any other way (a function's result, a callback parameter typed by its context, a union of two types), private names `x.#m` and methods named on an object type stay `attribute_call` |
| `tree-sitter.js-call-member-chain` | JavaScript/TypeScript: `x.a.b.m()` where `x` is typed as for `js-call-typed-receiver`, or is `this` in an instance method or field of a class declaration (through arrow functions); and `y.m()` where `y` is declared from such a chain (`const y = x.a`) or destructured from it (`const { a } = x`, `const { a: y } = this`). `CallSite.receiver_type` holds the class (or `"this"`, the innermost class around the call) and `CallSite.receiver_steps` the property names. Each step reads `Definition.fields`: an instance field annotated with a class or initialized with `new` of one, a constructor parameter property annotated with one, or a getter whose return type is one, declared on the class or inherited through `extends`. The written name is the class's own scopes' declaration, or else what the file's import of it names, and must be one class or interface that nothing else of that name merges with. It links to the method `m` of the last class or interface, as `js-call-typed-receiver` does. A step that is untyped, a method, external, a property of an interface, an enum or a type parameter of the file leaves the call unresolved (`receiver_type_unknown`). Private names, computed or quoted keys, annotated patterns, static fields, `super`, a plain alias `const y = x` and the `this` of a function, a static method or a class expression are not followed |
| `tree-sitter.js-call-private-name` | JavaScript/TypeScript: `obj.#m()` to the private method `#m` that the innermost enclosing class declares, whatever `obj` is. A private name can only name a member of a class around it, so no type is needed; one the innermost class does not declare is `not_declared_in_class`, since a private field of that name in a class between would not be seen |
| `tree-sitter.call-package-name` | Go: `f()` to a function declared at top level by another file of the same package (same directory and package clause); build constraints are not evaluated |
| `tree-sitter.go-call-conversion` | Go: `T(x)` whose bare name is no function of the package but one type declared at top level by a file of the package (a named type, struct, interface or alias): a conversion, linked to that type. `pkg.T(x)` is not linked yet |

In JavaScript and TypeScript, a `new C(...)` that one of these rules links to
the class `C` links instead to the `constructor` that `C` declares, which is
the code the construction runs; the rule stays the one that found `C`. A class
that declares no constructor (one it inherits is not followed), or several
constructor overload signatures, keeps the link to the class.

A name bound both by an import and a local declaration, or declared more than
once, is ambiguous and gets no edge. Python `typing.overload` signatures
(`@overload`, `@typing.overload`, `@t.overload`, `@typing_extensions.overload`)
are recorded with `overload: true` and declare call forms only: when the
candidates are such signatures and one other declaration, the call links to
that declaration, the function that runs. TypeScript signatures without a
body (`f(a: string): void`) are recorded the same way when a function, or a
method of the same class body, of that name is implemented beside them. CALLS edges start at the calling
declaration, or at the File for module-level calls, and carry the call ID,
callee text, line and column. Tool metadata counts call resolutions by status
and reason. The `calls` capability is not advertised, so an empty callers or
callees result still says the relation was unsupported or unobserved.

`tools/compare_calls_with_scip.py` checks linked calls against SCIP references
at the callee name, matched as a whole identifier after the call's column. On
Django 5.1.4 `django/` (scip-python 0.6.8) the Python rules link 8,731 calls:
8,502 confirmed, 2 contradicted and 227 with no SCIP reference at the name.
`tree-sitter.python-call-typed-receiver` links 85, all confirmed.

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
  IMPLEMENTS, DISPATCHES_TO and REQUESTS, and forwards over HANDLES. A Go
  interface method DISPATCHES_TO each implementation (see "Interface
  implementations"), so a changed implementation reaches the interface
  method and its callers, but not the other implementations. A route a changed
  handler HANDLES is affected, and so is every declaration (or file) that
  REQUESTS that route, so impact crosses service boundaries along the HTTP
  contracts the `service-contracts` tool records (see "Service contracts").
  Message destinations and database tables work the same way: forwards over
  PUBLISHES and WRITES, then backwards over CONSUMES and READS, so a
  publisher or writer change reaches every consumer or reader (see
  "Database tables").
  Those hops are `pattern_matched`, and the path's evidence says so. The
  traversal is a quantified path pattern, so this query needs Neo4j 5.9 or
  later.
- `get_file_impact(path)` (`deepthought-graph file-impact --file`, MCP
  getFileImpact): dependent files backwards over IMPORTS and DEPENDS_ON.
- `get_contract_impact(qualified_name)` (`deepthought-graph contract-impact
  --contract`, MCP getContractImpact) starts from a contract instead: an HTTP
  route, gRPC method, GraphQL field, message destination or database table.
  The change set is the contract and its providers, the declarations or files
  that HANDLE, PUBLISH, WRITE, map (MAPS_TO) or DECLARE it, which the answer
  lists under `providers` (at most 10, `providers_truncated` when more).
  Dependents are then found as for `get_impact`: the code that REQUESTS,
  CONSUMES or READS the contract (direct), and the callers of that code, also
  through other contracts it handles (transitive). Providers and dependents
  share the byte budget, which must be at least 2048 here. Contract nodes are
  not declarations, so `search_contracts(query)` (`deepthought-graph
  contracts --query [--kind]`, MCP searchContracts) finds them: contracts
  whose name (`GET /api/x`), display name, path or qualified name contains
  the query, case-insensitively, optionally of one kind (`HttpRoute`,
  `GrpcMethod`, `GraphqlField`, `MessageDestination`, `DbTable`), each with
  its provider and client counts.

All three take `--max-depth` (1 to 5, default 3) and an optional evidence filter
that limits which edges may be crossed. Each dependent is listed once, with
the declaration or file it reaches in the change set, its depth, the edges of
its path (type, edge ID, evidence type, rule ID) and the path's evidence,
which is its weakest edge. When several paths reach a dependent, the one with
the strongest evidence wins, then the shortest, then the first by edge IDs, so
the answer is the same on every run.

Each dependent also says what the change asks of it. `impact` is `direct`
when any one-hop path reaches it (it names the changed declaration or file
itself, so a signature change may need an edit there, even when its
strongest path is longer) and `transitive` otherwise (its behaviour may
change, so it needs re-testing). `source_roles` lists `test` and `example`
by path (`deepthought.source_roles`: directories such as `tests/`,
`__tests__/`, `spec/`, `testdata/`, .NET `*.Tests/` projects,
`examples/`, `samples/`, Go's ignored `_examples/`, Maven's `src/test/`, and
test file names such as `*_test.go`, `test_*.py`, `FooTest.java`,
`FooTests.cs`, `*.spec.ts`; other directories such as `benchmarks/` stay
unclassified); `exclude_roles` (`--exclude-role`, repeatable) leaves those
dependents out in the query itself, before paging. Direct dependents come
first, then by depth, evidence and name; a response that exceeds its byte budget or its limit drops whole
items, says it was truncated and gives `next_offset`, where the next page
starts (`--offset`, MCP `offset`; a snapshot does not change, so pages do not
overlap or skip). Component impact pages the same way. The answer is a lower bound: dynamic dispatch, reflection,
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
follows REQUIRES, DEPLOYS and SELECTS backwards like the other impact queries:
a package change reaches the Compose services and Kubernetes workloads that
build or run it, and the Kubernetes Services that select those workloads,
each hop with its own evidence (a `name_matched` image-name deployment marks
the whole path `name_matched`).

When the `service-contracts` tool runs in the same revision, packages are also
joined by the contracts between them: a package USES_CONTRACT_OF another when
code in it REQUESTS an HTTP route that code in the other HANDLES, or CONSUMES a
message destination the other PUBLISHES to (`components.contract-consumer`,
`pattern_matched`, one edge per contract, with the contract's qualified name
on the edge). Each file counts in its nearest package, the package component
of any kind whose root is deepest above it, and contracts used within one
package make no edge. The edges are made when outputs are combined, like
PART_OF, since contracts and components come from different tools.
`get_component_impact` follows USES_CONTRACT_OF backwards, so a change to a
service's package lists the packages that use its routes and topics. On
spring-petclinic-microservices, its 2 linked requests give 2 edges,
api-gateway to customers-service and genai-service to vets-service, and both
match the calling code (`CustomersServiceClient` and
`VectorStoreController`).

Code in no package (a Python service with only `requirements.txt`, a C# or
Gradle service) counts in the Compose services and Kubernetes workloads
whose build context, the Compose `build` or Skaffold artifact context, is
deepest above it (`components.contract-consumer-build-context`). Copies of
one build context deployed by several manifests are one service and never
use each other. On GoogleCloudPlatform/microservices-demo `c7cb6d5` this
adds 5 component pairs to the 8 between its Go and Node packages:
loadgenerator to frontend (Locust HTTP), recommendationservice to
productcatalogservice, checkoutservice to emailservice, and frontend to
adservice and recommendationservice. Each one matches a gRPC stub or HTTP
call in the consuming service. `get_component_impact` from
productcatalogservice now lists the recommendationservice workload, and
from the emailservice workload it lists checkoutservice. Every edge on
those paths carries a rule ID. `get_impact` already crossed the same
contracts at declaration level: from currencyservice's Node `convert`
handler it reaches the `CurrencyService/Convert` method, then the Go
`convertCurrency` callers in checkoutservice and frontend and their own
callers.

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
| `tree-sitter.base-imported-name` | `Base` to the top-level class or interface that an import binding (aliases applied) names in its linked module file, including one a local `export { C as Base }` exports under that name |
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

A variable declared with an arrow function or an anonymous function
expression (`const walk = function* () {}`) is a `Function` declaration with
the variable's name, so calls inside it belong to it. A named function
expression keeps no declaration of its own.

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

### Interface implementations

Nothing in Go declares that a type implements an interface, so
`deepthought.go_interfaces` compares method sets
(`tree-sitter.go-interface-method-set`, `name_matched`). Each Go function,
method and interface method records `Definition.signature`, its parameter and
result types without names (`(string,[]byte)(error)`, `...T` for a variadic
parameter), and each interface records `Definition.embeds`, the elements it
embeds as written. A named type in a signature is tied to its package: a
package of the analyzed files by its directory, another by the one import of
the file whose alias, or conventional name (the last path element without a
`/vN` or `.vN` suffix or `go-` prefix), is the qualifier. `byte` and `uint8`,
`rune` and `int32`, and `any` and `interface{}` are the same type.

A struct or other named type (or a pointer to it) implements an interface of
its own module when, for every method of the interface, it declares exactly one
method of that name with the same signature; an unexported method matches only
in the interface's package. The interface's methods are its own and those of
what it embeds: an interface of the same module, `error`, or an exported
standard library interface listed in `src/deepthought/go_std_interfaces.json`
(`go list std | grep -v -e internal -e vendor | go run
tools/go_std_interfaces.go`, written from go/types). The type IMPLEMENTS the
interface, and each interface method DISPATCHES_TO the type's method, so
impact reaches the callers of an interface method from a changed
implementation (see "Impact").

Nothing is guessed. An interface is left out when an embedded element is a
type constraint, another module's or a third-party interface, or names no one
interface; when a method's signature names a type parameter, an instantiated
generic type, a struct or a non-empty interface literal; or when a named type
cannot be tied to a package (a qualifier no import binds, a bare name in a file
with a dot import). A type is left out when its name is declared more than
once in its package, as files for different platforms may. Methods promoted
from embedded fields are not counted, so a type that relies on them is missed,
not wrongly matched. `tools/go_interface_dispatch.go` lists the pairs go/types
gives, for comparison.

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
and constructors record their parameter count, whether the last parameter
is variable-arity, and each parameter's type as written, without type
arguments (`parameter_types`: `List`, `int[]`, `T`, `String...`). Calls link
by Java's name lookup, and by a type only where a declaration writes it, so
every link is `name_matched`:

| Rule | Callee |
|---|---|
| `tree-sitter.java-enclosing-type-method` | `m()`: the innermost enclosing named type that has a method `m`, declared or inherited (Java stops at the first such type), when that type declares it |
| `tree-sitter.java-static-import` | `m()` declared by no enclosing type: a method of a type imported with `import static`, by name or `*` |
| `tree-sitter.call-self-method` | `this.m()`: the innermost enclosing type's `m` |
| `tree-sitter.java-type-member` | `T.m()`: `T` names a type by Java's type lookup (below), and that type declares `m` |
| `tree-sitter.java-type-name` | `new T()`: the constructor of the type `T` names, by the same lookup, that the arguments select (overloads chosen as for methods, below). When `T` declares no constructor, or the arguments leave none or more than one, the link is to the type |
| `tree-sitter.java-inherited-method` | `m()`, `this.m()` or `T.m()` where the type found does not declare `m` but inherits it through its declared bases |
| `tree-sitter.java-call-typed-receiver` | `x.m()` or `this.x.m()` where the declaration of `x` in scope writes a class type `T` (below): `m` declared on `T` or inherited through its declared bases |
| `tree-sitter.java-call-chained-receiver` | `e.m()` where `e` is itself a call (`a.b().m()`, `T.f().m()`, `f().m()`) whose link reaches a method declaring a class return type `T`, or `new T(...)`: `m` declared on `T` or inherited through its declared bases, overloads chosen as for other Java calls; a type-parameter or primitive return leaves it unresolved |
| `tree-sitter.java-call-static-field-receiver` | `T.F.m()` where no variable or field named like `T`'s first name can be in scope (none declared around the call, no type around it that may inherit one) and the type `T` declares the static field or enum constant `F` with a written class type `U`: `m` declared on `U` or inherited through its declared bases |

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
repository bases have (a `private` method is not inherited, so it counts
only in the type that declares it; Java methods and constructors record
`private`), followed in source order (a class's superclass
first) through every level. A nearer method with the same parameter shape (below)
overrides an inherited one, and a method declared in a class (the
superclass chain) also implements a same-shaped interface method; methods of
two interfaces with the same shape are both kept, since only types say which
wins. Bases outside the repository (`AbstractList`, `TestCase`) are not
read, so their methods stay unresolved.

Among same-named methods, only those whose parameters accept the argument
count are candidates (a variable-arity method accepts one fewer or more);
none is `no_matching_arity`. When several are left, the argument types the
call writes choose among them as javac does (JLS 15.12.2). An argument's type
is known when it is a literal (`"x"` is a `String`, `1` an `int`, `1L` a
`long`, `1.5f` a `float`, `X.class` a `Class`, `null`), `new T(...)`, a cast
`(T) e`, a `+` with a `String` operand (concatenation, JLS 15.18.1), an
operator expression (a comparison, `!`, `&&`, `||` and `instanceof`
give `boolean`; arithmetic, bitwise and shift operators on primitive numbers
give the type numeric promotion gives, JLS 5.6; `&`, `|` and `^` on two
`boolean`s give `boolean`), a conditional `c ? x : y` whose operands have
one type (not `null`), or a
variable whose declaration writes its type (as for `receiver_type` below,
primitives such as `int i` and `boolean flag` included); `var`, arrays and
other expressions are not typed; `CallSite.argument_types` records them. An
argument that is itself a method call (`a.j(box.get(0))`, recorded by its
span in `CallSite.argument_calls`) has the return type of the method that
call links to (`Definition.result_type`: the class or primitive type a Java
method declares, read where that method is declared; none for `void`,
arrays and type parameters). An argument `X.f` whose `X` no variable,
parameter or field declared around the call in its file names (recorded in
`CallSite.argument_fields`) is the static field or enum constant `f` of the
type `X` denotes, with the type that type declares for it
(`Definition.fields`: each static field, interface field and enum constant
whose type is a class or primitive). Only overloads that the other types
leave open use it, and a link it decides carries
`tree-sitter.java-overload-static-field-argument` (name_matched): a field
named `X` that the enclosing class inherits would denote the variable
instead, and inherited fields are not read. The phases
are tried in order: without boxing, with boxing, then variable arity. In the
first phase where some method applies, the most specific one is linked:
each of its parameter types is a subtype of the other's. Subtypes are known
for repository types through their declared bases, for primitives by
widening, and for the `java.lang` types a literal or boxing gives (`String`,
`Class`, the box types), which are listed with every supertype. A type outside
the repository has no other known supertype than `Object`, and a type
parameter has no known bound, so a check against either is unknown, with one
exception: a type a single-type import names in a `java.` package
(`import java.util.Map;`) is no subtype of a repository type, since only the
JDK defines `java.` classes. A link only that rule decides carries
`tree-sitter.java-overload-platform-argument` (name_matched). Other imported
types are not ruled out: one in a package no analyzed file declares may still
be built with the repository (an excluded module, generated sources). An
unknown argument or check makes a method possibly applicable, and the call is
linked only when the choice holds for every outcome of the unknown checks:
one method possible from that phase on, or a method that applies and is more
specific than every other possible one. A `private` method is not a
candidate outside the top-level class that declares it. Otherwise the call
stays ambiguous among the arity candidates.

When the overloads are still not told apart, the JDK's own supertypes are
used: `src/deepthought/java_platform.json` lists every public type of 21 JDK
packages (`java.lang`, `java.io`, `java.util` and its subpackages,
`java.time`, `java.nio.file`, `java.sql` and others) with the simple names of
all its supertypes, taken from the JDK 21 classes themselves by
`java tools/JavaPlatformTypes.java > src/deepthought/java_platform.json`. A
type name that no repository type in scope denotes is looked up there when a
single-type import names it, when it is a canonical `java.` name, or when
`java.lang` and the file's on-demand imports, all in listed packages, give it
exactly once; a static import of the name, or an on-demand import of any
other package, leaves it unknown. A repository enum or record then also has
the supertypes of `java.lang.Enum` or `java.lang.Record`, and a type whose
bases are all repository or listed JDK types has a complete set. A link only
this decides carries `tree-sitter.java-overload-jdk-supertypes`: on gson it
confirmed 331 links (10,904 of 11,050 in all), none contradicted, and impact
rose from 3,955 to 4,421 of 5,070.

A method declared in an enum constant's body (`ONE { int read() {...} }`) is
named as the enum's members are, but `Definition.constant_body` marks it and
it is no member of the enum type: a call through the enum
(`Policy.ONE.read()`, `p.read()`) binds the method the enum declares or
inherits, as javac does, and only a call inside that body reaches it. On gson
this removed the 6 ambiguous calls whose candidates were constant-body
methods and left out javac's, and confirmed 32 more links (10,936).

An argument `e.getClass()` or `getClass()` (no arguments) is a
`java.lang.Class`: `Object` declares `getClass()` final (JLS 4.3.2), so no
type can declare another without parameters, whatever `e` is. With the JDK
supertypes of `Class` (`Type`, `GenericDeclaration`, `AnnotatedElement`,
`Serializable` and others), that tells apart overloads such as gson's
`toJson(Object, Type)` and `toJson(JsonElement, Appendable)`. A link only this
decides carries `tree-sitter.java-overload-getclass-argument`: on gson it
confirmed 5 more links (10,941), none contradicted, and impact rose from
4,421 to 4,763 of 5,070, since `Gson.toJson(Object)` reaches most of the
library's callers.

A variable, parameter or field declared with an array type (`int[] xs`,
`Elem[][] grid`) gives an argument that array type, element without type
arguments; an array of a type parameter, and the C-style `Foo x[]`, stay
unknown. An array is assignable to `Object`, `Cloneable` and `Serializable`,
to an array of a supertype of its element, and to nothing else that is no
array (JLS 4.10.3), so `toJson(Object)` is chosen over `toJson(JsonElement)`
for one. Array types are recorded only for arguments: an array is no
receiver type. On gson this confirmed 32 more links (10,973), none
contradicted, and impact rose to 4,836 of 5,070.

For a call `x.m()`, the analysis looks for the declaration of `x` that is in
scope at the call, from the innermost scope out, and records the type it
writes as the call's `receiver_type` (`Foo`, `Map.Entry`; type arguments
dropped). Declarations that count:

- a method, constructor or record parameter, or a lambda parameter with a
  written type (`(Foo a) -> ...`);
- a local variable declared in an enclosing block before the call, a `for`
  initializer, an enhanced `for` variable, a `try` resource, or a `catch`
  parameter with a single type;
- `var x = new Foo()`;
- a field of an enclosing class body (`this.x` names the innermost class's
  field only).

The first declaration found decides. It gives no type, and the call stays
`attribute_call`, when it writes none that the lookup can use: a primitive,
an array (`Foo[] a`, `Foo a[]`, `Foo... a`), `var` without `new`, an
inferred lambda parameter, a multi-catch, an enum constant, or a type
parameter of an enclosing method or class. A name bound by a pattern
(`o instanceof Foo g`, `case Foo g`) anywhere in the method also gives no
type, because a pattern variable's scope follows control flow. A field is
not looked for past a class body that may inherit one of the same name: an
anonymous class, an enum constant's body, or a type with a superclass or
superinterfaces. `T` is then looked up as a type name (above). When it names
no repository type (`String`), the call is `receiver_type_not_declared`.
Calls on other expressions (`a.b().c()`, `x.f.m()`) need types and stay
unresolved (`attribute_call`).

A nearer method overrides an inherited one only when parameter count,
variable arity and each parameter type as written agree. A simple name
agrees with a qualified one that ends in it (`Entry`, `Map.Entry`), and
type arguments are not compared. A class's type parameter agrees only with
another class's type parameter, and a method's own only with another
method's own. Otherwise both methods are candidates, since only the types
say whether the nearer one overrides or overloads: `LongRange.fit(long)`
and `Range.fit(T)` are overloads, and so are `getOrDefault(..., String)`
and `getOrDefault(..., T)` when `T` is bound to an enum, but
`write(JsonElement)` overrides `write(T)` in a `TypeAdapter<JsonElement>`.

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
| google/gson | 7,282 / 7,285 | 0 | 3 | 2,160 | 124 |
| apache/commons-lang | 23,855 / 25,038 | 0 | 1,183 | 11,306 | 380 |
| google/guava | 55,814 / 55,981 | 0 | 167 | 65,144 | 3,439 |
| junit-team/junit5 | 22,595 / 23,271 | 8 | 668 | 9,624 | 1,987 |

`tree-sitter.java-call-typed-receiver` accounts for 3,123, 5,057, 7,280 and
3,649 of the confirmed links, and none of its links is contradicted. Most
missed calls are calls on expressions or on variables whose declaration
writes no usable type (`attribute_call`: 117, 378, 3,028 and 1,958).
Calls through a typed variable become ambiguous where the hierarchy has
several methods that only types can choose between, and where a type is
declared in both `guava/src` and `android/guava/src`; most of guava's
ambiguous calls are of that second kind. A few ambiguous calls leave out
javac's method: an inherited overload, a GWT super-source file javac binds
elsewhere, or a method whose own type parameter javac infers. javac binds
none for 1,183 commons-lang links because those test calls pass lambdas to
JUnit-typed parameters, and JUnit is not on javac's class path; the links go
to the statically imported `LangAssertions` methods of the same name.
junit5's 657 links to its statically imported `PreconditionAssertions`
methods are the same case. 74 guava links of the new rule are to repository
methods javac leaves unbound; they were sampled, not checked one by one.
Deciding overrides by parameter types (above) removes the commons-lang
contradictions (`Range.fit(T)` and `LongRange.fit(long)`) and 33 of
guava's (`putBytes`, `hashBytes`, `append`); not inheriting private
methods removes the last two (`getUninterruptibly` in `AggregateFuture`
had linked to `AbstractFuture`'s private method instead of the statically
imported `Uninterruptibles.getUninterruptibly`). junit5's 8 (`getFullyQualifiedMethodName`) pick a method
by name and arity where javac picks another of the same arity by argument
types. junit5 indents with tabs; the oracle counts a tab as one
column, as the analysis does, rather than expanding it to the next tab stop
as javac's `LineMap.getColumnNumber` would.

Calls on a call's result (`new GsonBuilder().setPrettyPrinting().create()`)
follow the linked method's declared return type
(`tree-sitter.java-call-chained-receiver`). On the gson call corpus this
raised confirmed links from 9,342 to 10,457 of 11,050 with none
contradicted; 22 of its links land where javac bound no call, mostly in the
proto tests, whose receivers involve classes built from `.proto` files.
Calls on a type's static field or enum constant
(`DefaultDateTypeAdapter.DateType.DATE.createAdapterFactory(...)`,
`JsonElementTypeAdapter.ADAPTER.read(...)`) follow the field's written type
(`tree-sitter.java-call-static-field-receiver`): 50 more confirmed links on
gson (10,517 of 11,050), none contradicted.
A variable an anonymous class, an enum constant's body or a class with bases
captures from around it (`longAdapter.read(in)` inside `new TypeAdapter<>() {...}`)
binds its declaration only when no base of those types may give them a field
of that name: every base is a repository type, none declares one, and the same
holds for its own bases (`Definition.properties` lists a Java type's fields).
Otherwise the call is left unresolved (`receiver_may_be_inherited_field`).
This confirmed 44 more gson links (10,561), none contradicted.

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
| junit-team/junit5 | 1,370 / 1,370 | 0 | 0 | 1 | 158 |

Guava's 594 ambiguous bases are test roots extending a type both
`guava/src` and `android/guava/src` declare. junit5's one missed base is in a
default-package file (`DefaultPackageTest extends VintageTest`, imported from
`com.example.vintage`); why its import is not followed is not yet explained.

## Rust

Rust sources (`.rs`) are analyzed when `rust` is selected (`--language rust`)
and the `rust` extra is installed; like JavaScript, Go and Java, the language
is opt-in. Declarations are functions, structs (`Struct`), enums, unions
(`Union`), traits (`Trait`), type aliases and associated types (`TypeAlias`),
modules (`Module`, both `mod name { ... }` and `mod name;`), `macro_rules!`
macros (`Macro`) and enum variants (`EnumVariant`, named by their enum:
`Shape.A`). A function in an `impl` or trait body is a `Method`; other
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
`receiver.name` (`self.write`, `x.y.len`), and a method call on a call that
is itself a call site as that call's callee, `()` and the name
(`T::new().build`, `x.a().b`); other callees (`(f)()`, `x?.m()`,
`<T as Trait>::f`) and macro invocations are not call sites. Calls inside
item macro bodies are read as other calls are.

The arguments of a standard macro whose arguments are expressions (`assert!`,
`assert_eq!`, `assert_ne!`, their `debug_` forms, `dbg!`, `format!`,
`format_args!`, `print!`, `println!`, `eprint!`, `eprintln!`, `write!`,
`writeln!`, `panic!`, `todo!`, `unimplemented!`, `unreachable!` and `vec!`),
invoked by its name or through `std::`, `core::` or `alloc::`, are read as
expressions: the invocation's token tree is split on its top-level commas and
each argument is parsed on its own, in place, so its calls are call sites
like any other. Declarations around the invocation type receivers in it as
they would outside. An argument that does not parse as an expression is
skipped. `matches!` (a pattern argument), other macros, a macro of the same
name under another path, and an invocation with no `;` after it in the file
(the last tail expression of a file) are not read.

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
  method `m` (supertraits are not read);
- `tree-sitter.rust-call-typed-receiver` when `x.m()` is called on a local
  whose innermost declaration is a parameter (of a function or closure) or a
  `let` that binds `x` alone and writes a type: `A`, `m::A`, `A<T>`, `&A`,
  `&mut A` or `Self` (`CallSite.receiver_type`). The type is read as a call's
  path is, from the call (`Self` is the type of the impl around it), and `m`
  is looked up as for `Type::m`. A later untyped `let`, a pattern (`let (a,
  b)`, `W(a)`), `if let`, `while let`, `for` and match arms that may bind `x`
  give no type, nor do raw pointers, trait objects, `impl Trait` and a name
  the generic items around the call declare as a type parameter. A type with
  no impl declaring `m` (a derived, `Deref` or blanket-impl method) is
  `associated_item`, and one that names no workspace type is
  `receiver_type_unknown`.
- `tree-sitter.rust-call-result-receiver` when `x.m()` is called on the
  result of a call, written as the receiver (`T::new().m()`, `x.a().m()`) or
  bound by a `let` that is the innermost declaration of `x`, binds it alone
  and writes no type (`let x = T::new();`). `CallSite.receiver_call` holds
  that call's span. The call must link, by any rule (this one too, so builder
  chains are followed call by call), to a function with a body whose return
  type writes a type as for `rust-call-typed-receiver` (`Definition.result_type`):
  `Self` is the type of the impl around the function, and any other path is
  read where the function is declared, outside its own body. `m` is then
  looked up as for `Type::m`. A call that does not link, a return type that
  is a type parameter, a tuple, `impl Trait`, a trait's `Self` or a type of
  no workspace impl (`Option<T>`, `Result<T, E>`), and a value of any other
  kind (`T::new()?`, a struct expression) leave the call
  `receiver_type_unknown` or `method_call`.

- `tree-sitter.rust-call-enum-variant` when a path `E::V(...)` or
  `Self::V(...)` names an enum (read as for `Type::f`; `Self` is the type of
  the impl around the call) that declares the variant `V`: the call builds
  that tuple variant. A variant is named ahead of an associated function of
  the same name, as in Rust. A variant brought in by `use E::*` or `use E::V`
  is not read.

All seven are `resolved`: Rust has no overloading, so a path names one
declaration in each configuration. A function declared once per `#[cfg]`, or
by two trait impls, is `ambiguous` (`multiple_declarations`). Impls for a type
alias are keyed by the alias, since the aliased type is not read: a call
through an alias is `type_alias`, and so is a call that would otherwise take
a trait's function when an impl for some alias declares the same name. Other
outcomes: `method_call` (a receiver other than `self` whose declaration
writes no type: receiver types are not inferred), `associated_item` (a name inside a type that no impl declares, such
as a derived method), `unbound_name` (a single
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
prototypes declared in a function body are not declarations. A function
declared `static` records `Definition.static`, and a prototype (a function
declared without a body) records `Definition.prototype`. Other facts than
declarations, includes and calls are not recorded for C yet.

### C includes

Each `#include "path"`, in every preprocessor branch, is an import of kind
`include` whose specifier is the path as written. `#include <path>` and an
include computed from a macro are not recorded, since the compiler finds them
on the include path the build gives it. A compiler looks for a quoted path
first in the directory of the file that writes it, and that is the one place
resolution looks: the include links the file there with rule
`tree-sitter.c-include-relative` (`resolved`), and is unresolved
(`not_in_repository`) when no analyzed file is there. Include paths given to
the compiler (`-I`) are not known, so a header found only through one is
unresolved.

### C calls

Each call `f(...)` whose callee is a plain name is a call site, in every
preprocessor branch, its caller the function definition around it. Calls
through a field, a pointer expression or a parenthesized callee are not call
sites. `CallSite.shadowed` is True when the function's parameters or the
declarations in its body declare the name (a pointer to a function, or a
prototype written in the body), or when a declaration at file scope declares
it as a variable. A call is resolved as the linker binds it:

- `tree-sitter.c-call-static` when the calling file's translation unit
  declares a function `f` `static` (a prototype or a definition): the
  definition of `f` in that unit, since a later definition without `static`
  has internal linkage too;
- `tree-sitter.c-call-external` otherwise: the one definition of `f` without
  `static` among the repository's C files.

Both are `resolved`. Two definitions (one per program, such as a test's
`setUp`, or one per platform) are `ambiguous` (`multiple_declarations`), and
none (a C library function) is `not_defined`. A shadowed name is
`local_binding`, and a name any C file defines as a macro is `macro`, since
the macro may expand the call away.

A file's translation unit is the file and every file its resolved includes
reach, followed transitively, so a `static inline` function in an included
header or a `static` function in an included `.c` file binds there. A
definition in a file that declares its name `static` is never offered to
another unit, and an include that does not resolve adds nothing to the unit.

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

## C++

C++ sources (`.cc`, `.cpp`, `.cxx`, `.c++`, `.hh`, `.hpp`, `.hxx`, `.h++`,
`.ipp`, `.inl`) are analyzed when `cpp` is selected (`--language cpp`) and the
`cpp` extra is installed (tree-sitter-cpp 0.23.4); the language is opt-in. A
`.h` header is read as C++ when `cpp` is selected, since C++ code includes it
and the C++ grammar reads C declarations too, and as C otherwise.

Declarations are those outside function bodies, in every preprocessor branch:
namespaces (`Namespace`, both parts of `namespace a::b`), classes, structs,
unions and enums that are named and have a body (`Class`, `Struct`, `Union`,
`Enum`), functions (`Function`) and member functions (`Method`, including
constructors, destructors, operators and conversion functions), definitions
and prototypes alike, type aliases (`TypeAlias`, from `typedef` and `using`)
and macros (`Macro`, wherever they are defined). Templates declare what they
wrap, and a declaration's source text starts at its `template` line. Symbols
join the enclosing namespaces and classes and any scope written before the
name with `.` (`n.C.f`); anonymous namespaces and unnamed types add no scope,
and operators are spelled as clang spells them (`operator==`,
`operator new`). A function declared in a class body is a `Method`, and so is
one defined out of line with a written scope (`void C::f() {}`), unless the
scope's last part is a namespace the file opens (`void n::f() {}` in a file
with `namespace n {`). The written scope is not looked up further. A friend
function is declared in the namespace around its class. Variables, fields,
enumerators, forward declarations and items in function bodies are not
declarations. Imports, calls and other facts are not recorded for C++ yet.

Before parsing, a word between a class key and the class's name (`class
TINYXML2_LIB XMLNode {`, `struct LEVELDB_EXPORT Options :`), which C++ only
allows when the word is a macro, is replaced with spaces; offsets and every
other byte are kept, comments and literals are skipped, and `final` is not
such a word. An elaborated type with a braced initializer (`struct S s{1};`)
has the same shape and is misread as a definition of `s`. Three other shapes
the grammar gives to code it does not fully read are read deterministically:
a member function annotated with a macro (`void f() REQUIRES(mu);`) has its
declarator in an error node, and that declarator is the one declared; a type
after an export macro (`API Status Get();`) is in an error node and the
declarator is kept, so a field annotated the same way (`int x
GUARDED_BY(mu);`) reads as a function named after the macro; and a
function's declarator standing alone in an error node declares a function,
as in C. A line holding only an upper-case name with an underscore
(`FMT_BEGIN_EXPORT`, or `FMT_CONSTEXPR` above a declaration) is a macro and
is replaced with spaces too, since the grammar would read it as the type of
the declaration after it. The same name at the start of a line before a
constructor or destructor (`FMT_CONSTEXPR explicit checker(...) : a(1) {`,
`FMT_CONSTEXPR20 ~buffer()`) is replaced too: read as a return type, it turns
the member initializers into a parse error that ends the class early. A
declarator standing alone in an error node with nothing before it (`M(a,
b);`) is a macro's invocation, not a function, unless it is in a class and
names it (a constructor whose initializers hold an `#if`). Two shapes declare no function: a declarator after
a class body in the same declaration (C++ does not allow a class defined in a
return type, so this is text after a body the grammar closed early), and a
declarator after a bit-field width (a constructor's member initializers,
`: size_(n), cap_(n)`, after a macro). An enum whose name the grammar reports
as missing (`enum : unsigned {`) is unnamed.

`tools/compare_c_declarations_with_clang.py --language cpp` checks this
against clang (libclang 18.1.1, `-std=c++20`), as for C, with namespaces and
class bodies visited, friend functions included and a class template's
constructors named without its arguments. A conversion function is compared
by kind and line only, since clang spells its type canonically. A matched
declaration whose symbol is not clang's qualified name (its named semantic
parents joined with `.`) is counted again by why: a namespace opened by a
macro (`FMT_BEGIN_NAMESPACE`), or a grammar error in or before it. Because
the scope of a C++ declaration depends on every brace before it, a function
that one side calls a `Method` and the other a `Function` at the same name
and line is put down to a grammar error when the grammar could not parse some
text before it.

| Corpus | Translation units (fatal) | clang | Matched (symbol differs: macro namespace / grammar error) | Tree-sitter only: inactive `#if` / file not reported / clang error / name is a macro / grammar error / kind after grammar error / namespace-qualified | clang only: macro-generated / in a function body / clang error / grammar error / kind after grammar error / namespace-qualified / read as an expression |
|---|---|---|---|---|---|
| leethomason/tinyxml2 `8224e42` | 4 (0) | 683 | 683 (0 / 0) | 32 / 0 / 0 / 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 / 0 / 0 / 0 |
| google/leveldb `2946558` | 132 (37) | 1,804 | 1,773 (0 / 68) | 10 / 934 / 3 / 42 / 4 / 0 / 0 | 0 / 8 / 0 / 20 / 3 / 0 / 0 |
| fmtlib/fmt `3be6709` | 74 (2) | 10,886 | 7,036 (2,259 / 856) | 742 / 50 / 0 / 781 / 84 / 0 / 13 | 3,511 / 124 / 0 / 133 / 67 / 14 / 1 |

No difference is unexplained. Most of fmt's macro-generated declarations
and names that are macros come from its bundled GoogleTest (`TEST(...)`
reads as a function named `TEST`). Namespace-qualified functions are fmt's
`test::open` and similar, defined in a test file that never opens `test`.
The expression is `int(fileno)(FILE*)`, which C++ allows to read either way.
The grammar errors are mostly `#if` branches inside a class or function that
leave braces unbalanced; from the first one on, the scope of later
declarations in the file may be wrong.

## Ruby

Ruby sources (`.rb`) are analyzed when `ruby` is selected (`--language ruby`)
and the `ruby` extra is installed (tree-sitter-ruby 0.23.1); the language is
opt-in. Declarations are modules (`Module`), classes (`Class`) and methods.
Symbols join the enclosing modules and classes and the path written in a
name with `.` (`class B::C` in `module A` is `A.B.C`; `class ::C` starts at
the top). A `def` in a class or module body, in its `class << self` or in a
block written there (`included do`, `Class.new do`) is a `Method`, whether it
defines an instance or a singleton method (`def self.m`); so is a `def` with
a receiver anywhere (`def YAML.load`, `def self.helper` at the top level),
which defines a method of that object, and so is a `def` in `class << obj`.
Any other `def` at the top level or in a block outside any class is a
`Function`. A class whose
name is not a constant path (`class self::Dynamic`) and what it holds are not
recorded. A `def` inside a method body is recorded under the same scope as
the method around it, since it defines its method when that method runs
(Ruby allows no class or module there). Methods made by metaprogramming (`attr_reader`,
`define_method`), constants, imports (`require`), calls and superclasses are
not recorded for Ruby yet.

`tools/compare_ruby_declarations_with_prism.py` checks this against Prism
(0.19.0, with Ruby 3.3.6), Ruby's own parser: `tools/ruby_declarations_prism.rb`
applies the same scoping rules to Prism's tree, and a declaration matches when
both report the same file, line and column of its name, name, kind and symbol.
A difference on a line where Prism reports a syntax error, or after one in the
same file, is put down to it; every other difference is unexplained.

| Corpus | Files (with syntax errors) | Prism | Matched | Tree-sitter only: syntax error / after one | Prism only: syntax error / after one |
|---|---|---|---|---|---|
| rack/rack `b432304` | 95 (0) | 1,014 | 1,014 | 0 / 0 | 0 / 0 |
| sinatra/sinatra `2e3f791` | 147 (0) | 1,168 | 1,168 | 0 / 0 | 0 / 0 |
| heartcombo/devise `05811fb` | 197 (9) | 1,040 | 1,035 | 6 / 0 | 2 / 3 |
| jekyll/jekyll `97c3bdc` | 162 (0) | 1,406 | 1,406 | 0 / 0 | 0 / 0 |
| mastodon/mastodon `fe9d150` | 3,342 (0) | 11,593 | 11,593 | 0 / 0 | 0 / 0 |

No difference is unexplained. devise's syntax errors are its generator
templates, `.rb` files holding ERB tags (`<% if ... %>`) that are not Ruby
until they are rendered.

## PHP

PHP sources (`.php`) are analyzed when `php` is selected (`--language php`)
and the `php` extra is installed (tree-sitter-php 0.25.2, read with its PHP
grammar, which allows HTML around `<?php` tags); the language is opt-in.
Declarations are classes (`Class`), interfaces (`Interface`), traits
(`Trait`), enums (`Enum`), functions (`Function`) and the methods of all four
class-like kinds (`Method`, abstract and interface methods included). Symbols
join the namespace and the enclosing class-like declaration with `.`
(`App.Http.Controller.handle`): a `namespace A\B;` statement scopes the
statements after it up to the next one, and a braced `namespace A\B { }`
scopes its body (`namespace { }` is the global namespace). A declaration in a
conditional block (`if (!function_exists('f')) { function f() {} }`, common
in WordPress) is recorded, once for each branch that declares it. So is one
in a function or method body or a closure: PHP declares it in the current
namespace when that code runs, so its symbol is namespace-qualified
(`App.Http.helper`, not `App.Http.Controller.make.helper`). The methods of
an anonymous class are named by the path to the class
(`App.Http.Controller.make.handle` for one in `Controller::make`).
Namespaces themselves, constants, properties, enum cases, calls and bases are
not recorded yet.

`tools/compare_php_declarations_with_php_parser.py` checks this against
nikic/PHP-Parser (v5.4.0, run with PHP 8.3.6), the parser PHPStan, Psalm and
Rector are built on: `tools/php_declarations_php_parser.php` applies the same
scoping rules to its tree, and a declaration matches when both report the
same file, line and byte column of its name, name, kind and symbol. A
difference on a line where PHP-Parser reports a syntax error, or after one in
the same file, is put down to it, and one in or after a part of the file the
grammar could not parse is put down to the grammar; every other difference is
unexplained.

| Corpus | Files (with syntax errors) | PHP-Parser | Matched | PHP-Parser only: grammar error |
|---|---|---|---|---|
| Seldaek/monolog `6d4cf3a` | 230 (0) | 1,929 | 1,929 | 0 |
| guzzle/guzzle `ba6574f` | 149 (0) | 3,380 | 3,380 | 0 |
| composer/composer `edc2b0d` | 653 (0) | 5,968 | 5,968 | 0 |
| WordPress/WordPress `e1cb5b9` | 2,072 (0) | 13,731 | 13,730 | 1 |
| laravel/framework `817f2fc` | 3,205 (1) | 38,123 | 38,123 | 0 |

No difference is unexplained, and no declaration is reported by tree-sitter
alone. The grammar error is WordPress's `function readonly()`, a fallback for
PHP before 8.1 where `readonly` is not a keyword: the grammar reads `readonly`
as one and leaves the function out.

Each clause of a `use` declaration is an import fact: its specifier is the
imported name as written, without a leading backslash and with a group's
prefix (`use A\{B, C as D}` imports `A\B` and `A\C`), its kind is `use`,
`use_function` or `use_const` (a `function` or `const` keyword applies to
every clause of a declaration, or to one clause of a group), and it binds its
alias or the name's last part. A trait `use` in a class body and a closure's
`use ($x)` are not imports. PHP compares class, function and namespace names
ignoring ASCII case, and so does resolution:

| Rule | Evidence | When |
|---|---|---|
| `tree-sitter.php-use-declared` | resolved | `use` names a class, interface, trait or enum, or `use function` a function, that exactly one analyzed file declares (one file declaring it in both branches of an `if` counts once) |
| `tree-sitter.php-use-namespace` | resolved | `use` names no such type but a namespace that analyzed files declare types or functions in; the link reaches every such file |

Other outcomes: `not_in_repository` (Composer dependencies, PHP's own
classes, and declarations in function bodies, which are not recorded),
ambiguous `multiple_declarations` when several files declare the name (test
fixtures, polyfills), and unresolved `constant` for `use const`, since
constants are not declarations here. Composer's autoload configuration is not
read: a name declared once is linked to that file wherever Composer would
look.

`tools/compare_php_imports.py` checks the facts against PHP-Parser's `use`
nodes, and resolution against Composer's class map (`composer dump-autoload
--optimize` into a temporary vendor directory, which maps each class Composer
can autoload to its file by Composer's own scan) or, for a class Composer does
not map and for a function, against the declarations PHP-Parser reads:

| Corpus | Imports (PHP-Parser) | Class map: confirmed / ambiguous including it | Declared: confirmed / ambiguous agreeing / namespace | Functions confirmed | Unresolved, nothing declares it | Contradicted |
|---|---|---|---|---|---|---|
| Seldaek/monolog `6d4cf3a` | 633 (633) | 436 / 0 | 1 / 0 / 0 | 0 | 196 | 0 |
| guzzle/guzzle `ba6574f` | 877 (877) | 379 / 0 | 19 / 0 / 1 | 0 | 478 | 0 |
| composer/composer `edc2b0d` | 3,225 (3,225) | 2,234 / 8 | 214 / 11 / 5 | 0 | 753 | 0 |
| WordPress/WordPress `e1cb5b9` | 859 (859) | no `composer.json` | 750 / 0 / 0 | 0 | 109 | 0 |
| laravel/framework `817f2fc` | 14,687 (14,687) | 9,144 / 0 | 409 / 0 / 3 | 126 | 5,005 | 0 |

Every import fact matches PHP-Parser's. The ambiguous imports are of
Composer's `ClassLoader` and `InstalledVersions`, which composer's test
fixtures declare again. A link from source to a test stub is still possible:
guzzle's `src/Handler/StreamHandler.php` imports `Openssl\Session`, which the
repository declares only in a test file (the real class comes from an
extension), and it is linked there.

## C#

C# is an opt-in language (`--language csharp`, the `csharp` extra with
tree-sitter-c-sharp 0.24.0). Declarations only so far
(`syntax.extract_csharp`): classes, structs, interfaces, records and enums,
and their methods and constructors. Symbols join the namespace (a braced
`namespace A.B { }`, or a file-scoped `namespace A.B;` for the declarations
after it) and the enclosing types with `.`, without type parameters. A
declaration starts at its first attribute, as a Java declaration starts at
its first annotation. Local functions, lambdas, properties, operators,
delegates and enum members are not recorded; imports and calls are not read
yet.

`tools/compare_csharp_declarations.py` checks this against an independent walk of the same trees (each declaration's
namespace and types read from its ancestors rather than the extractor's
scope stack, and declarations inside bodies dropped), and a lexical count of
type keywords with comments and strings removed:

| Corpus | Files | Declarations | Differences | Types: lexical / extracted |
|---|---|---|---|---|
| dotnet-architecture/eShopOnWeb `4da8212` | 254 | 724 | 0 | 255 / 257 (the 2 are generic interfaces with a `where` clause the keyword count misses) |
| jasontaylordev/CleanArchitecture `f7f9771` | 110 | 357 | 0 | 124 / 124 (2 files have grammar errors) |
| GoogleCloudPlatform/microservices-demo `src/cartservice` | 9 | 40 | 0 | 8 / 8 |

## SCIP indexers

SCIP indexes are the engine's primary source of symbol facts for Python,
TypeScript, JavaScript and Go. Each language has one pinned indexer, and the
indexers are required dependencies, not optional tools:

| Languages | Indexer | Version | Override | Columns |
|---|---|---|---|---|
| Python | scip-python | 0.6.8 | `DEEPTHOUGHT_SCIP_PYTHON` | UTF-16 |
| TypeScript, JavaScript | scip-typescript | 0.6.2 | `DEEPTHOUGHT_SCIP_TYPESCRIPT` | UTF-16 |
| Go | scip-go | 0.1.26 | `DEEPTHOUGHT_SCIP_GO` | UTF-8 |

`deepthought indexers` lists them and whether each is installed (on `PATH`, or
at the override path); it installs nothing. `deepthought index-scip WORKSPACE
--output-dir DIR` runs each indexer the workspace's languages need and writes
`DIR/<indexer>.scip` with a `.sources.json` receipt (profile `<indexer>-v2`):
the index digest, the sha256 of every source and configuration input, the
producer and version, the indexer executable's digest and the arguments
without paths. `deepthought.indexers.validate_receipt` rejects an index whose
bytes or inputs changed. Run settings keep symbols stable: scip-python gets
`--project-version 0` and the workspace and `src` on `PYTHONPATH` (so a
src-layout package resolves without being installed), scip-go gets
`--module-version 0`, and scip-typescript gets `--pnpm-workspaces`,
`--yarn-workspaces` or `--infer-tsconfig` from the workspace's manifests.
Indexes are rewritten in a canonical order (documents, occurrences, symbols
and relationships sorted), since scip-go emits the same facts in a different
order from run to run.

The command reports every Python, TypeScript, JavaScript and Go source file
(outside `node_modules` and virtual environments) as `covered` or `uncovered`
with a reason:

| Reason | Meaning |
|---|---|
| `indexer_not_installed` | The pinned indexer is not on `PATH` or at its override |
| `indexer_failed`, `indexer_timeout` | The indexer exited non-zero, wrote nothing or ran past `--timeout` |
| `indexer_version_mismatch` | The index names another indexer version than the pin |
| `inputs_changed_during_indexing` | Inputs differed before and after the run |
| `no_go_module` | No `go.mod` in the file's directory or any directory above it |
| `build_constraints` | The go command builds the file on none of its platforms (`//go:build ignore`, a custom tag) |
| `not_in_index` | The indexer ran but has no document for the file |

scip-go indexes one module per run, so each `go.mod` in the workspace
(outside `vendor`, `testdata` and hidden directories) gets its own run from its
own directory, written to `scip-go.scip` for the root module and
`scip-go-<dir>.scip` (slashes as `__`) for a nested one. Document paths are
made workspace-relative and the receipt names the module. A Go file belongs to
the innermost module above it. Global symbols name the module, so a call from
one module into another binds across their indexes.

Go runs are pinned to `linux/amd64` with `CGO_ENABLED=0`, so the files a
module compiles do not depend on the machine or on a C toolchain; the receipt
records the platform and `go env GOVERSION`. A file the go command ignores on
that platform (`command_windows.go`) gets a scip-go pass for the first
platform in `go tool dist list` (first-class ports first) whose `go list`
compiles it, written to `scip-go[-<dir>]-<goos>-<goarch>.scip` with only those
files' documents; coverage says `pass: platform` and the platform. On cobra
this covers `command_win.go`.

TypeScript and JavaScript files outside every tsconfig.json of the workspace
(Deno and Bun scripts, benchmarks, build configs) get a second scip-typescript
pass, written to `scip-typescript-uncovered.scip`: one project of exactly
those files that extends the root tsconfig.json when there is one, with
`allowJs`, `allowImportingTsExtensions` and `noEmit`. Its receipt records that
project with workspace-relative paths, its coverage entries say
`pass: outside_tsconfig`, and it keeps only those files' documents, so the two
indexes never cover the same file. On hono it covers the 75 files the first
pass leaves out.

The indexers are required engine dependencies and there is no opt-out. It
exits 3 when a file is uncovered because an indexer is missing, failed, timed
out, is another version or saw its inputs change, so a missing or failing
indexer is never read as a file without references. A file no compiler can
index (`no_go_module`, `build_constraints`, `not_in_index`) is named on
stderr with its reason and does not fail the run. On the
pinned hono, click and cobra corpora, two runs give byte-identical indexes, and
the indexes give the committed call truth again: equal for cobra, and a
superset for hono (`--pnpm-workspaces` also indexes `adapters/`) and click
(external targets the earlier environment left unresolved).

`deepthought-graph index` always indexes, whichever tools the run selects. It
first runs the pinned indexers for the run's Python, TypeScript, JavaScript
and Go files, into a temporary directory or `--scip-output-dir DIR` to keep
them. The tree-sitter tool binds call sites in covered files through those
indexes (see Calls), and the `scip` tool reads them unless `--scip-index`
names stored indexes instead. Each analyzed file without an index is named on
stderr with its reason. When an indexer is missing or failed, the run stops
before importing anything; only files no compiler can index keep the parser's
call rules. The import report carries a
`scip` object: each indexer run, the covered file count and the uncovered
files with their reasons. The indexes a run writes are checked against their
receipts again before they are read, and are read whole: the 50 MB cap
`ingest_scip` keeps for an index from elsewhere does not apply to them
(scip-python writes over 100 MB for a large monorepo such as superset).
`--include` and `--exclude` limit which files must
be covered; a run whose languages have no indexer (`--language rust`) indexes
nothing.

The `scip` AnalysisTool reads these indexes, one per indexer, into one
snapshot (`deepthought-graph index WORKSPACE --tool scip --scip-index
DIR/scip-python.scip --scip-index DIR/scip-go.scip`; SDK
`ScipAnalysisTool(root, index_paths=[...])`). Each index is checked against its
receipt before and after reading, each File node records its language, and two
indexes that cover the same file are refused. Documents outside the workspace
(scip-go's generated test mains in the Go build cache) are dropped before the
index is written. The TypeScript v1 index from `index-typescript` still reads
as before.

## SCIP references

The `scip` AnalysisTool stores compiler-backed references from SCIP indexes
(see [SCIP indexers](#scip-indexers) for Python, JavaScript and Go). It never
runs an indexer itself.
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
--tool service-contracts --language java --language go --snapshot REV`) links services
through the HTTP contracts their code declares. The original `api_contract`
tool scored string literals that looked like API paths (0.95 for `/api/v1/x`,
0.75 for `/x/{id}`) and inferred the method from nearby words; it never matched
clients to servers. None of it is carried over.

Facts are read from Java sources outside test source sets (`src/test/`).
From test sources only client requests are read (see "Test sources"
below):

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
  and RestClient `.get()/.post()/.method(HttpMethod.X)...uri(...)` chains that
  start at a name declared as `WebClient`, `WebClient.Builder`, `RestClient`
  or `RestClient.Builder`, or at `WebClient` or `RestClient` itself (clients
  `web-client` and `rest-client`).
- **HTTP interfaces.** Methods of an interface annotated with
  `@HttpExchange`, `@GetExchange`, `@PostExchange`, `@PutExchange`,
  `@DeleteExchange` or `@PatchExchange` from
  `org.springframework.web.service.annotation` (client `http-interface`),
  placed on the method's annotation. The URL is the interface's
  `@HttpExchange` URL joined with the method's (`url` or `value`); the
  method is the annotation's, or `@HttpExchange`'s literal `method`, else the
  interface's, else unknown. The base URL the proxy gets from its
  `RestClient`/`WebClient` (`baseUrl`, set where `HttpServiceProxyFactory`
  creates it) is not read, so a URL without a scheme starts with unknown text,
  as does a `${...}` property. Exchange annotations on controllers, which
  Spring MVC also serves as routes, are not read.
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

Python, JavaScript/TypeScript and Go sources are read too
(`deepthought.service_contracts.web`), outside test files (`test_*.py`,
`*_test.py`, `*_test.go`, `*.test.js`, `*.spec.ts` and the like), from
which only client requests are read:

- **Test sources.** A request or gRPC stub call in a test source file is
  read like any other and its `REQUESTS` edge carries `test_source: true`,
  so impact analysis reaches the tests that call a contract. Nothing else
  is read from those files: their routes, servers, messaging, GraphQL and
  repository-wide facts (constants, mounts, prefixes) are left out, so a
  test's stub server never becomes a route. A test's request whose base is
  unknown is not kept from its own project's routes, since tests usually
  call their own service. The contract ground-truth scorer leaves
  `test_source` links out unless asked, because the labels cover
  application code.

- **Routes.** Flask and FastAPI route decorators (`route`, `api_route`, `get`,
  `post`, ...) on a `Flask`, `Blueprint`, `FastAPI` or `APIRouter` object
  imported from `flask` or `fastapi` and bound once in its scope, joined with a
  Blueprint's `url_prefix` or an APIRouter's `prefix` (Flask's `route` without
  `methods` is GET); Express `get`/`post`/.../`all` calls on an app or Router
  made from the `express` module and bound once; Go `HandleFunc`/`Handle` in a
  file importing `net/http` or `gorilla/mux`, with the methods of a chained
  `.Methods(...)` or a Go 1.22 `"METHOD /path"` pattern; Gin and Echo
  `GET`/`POST`/`PUT`/`PATCH`/`DELETE`/`HEAD`/`OPTIONS`, `Any`,
  `Handle`/`Add(method, path, ...)` and `Match([]string{...}, path, ...)`
  calls (rules `gin-route`, `echo-route`) on a router known in the file: one
  made by `gin.Default()`/`gin.New()`/`echo.New()`, a parameter, `var` or
  struct field typed `*gin.Engine`/`*echo.Echo` (the root), a `Group(prefix,
  ...)` of a known router (prefixes are joined), or Gin's `Use(...)`, which
  returns its router; a local name assigned more than once is not followed.
  A parameter typed `*gin.RouterGroup`, `gin.IRouter`, `gin.IRoutes` or
  `*echo.Group` is a group passed in. Its routes take, instead, the
  prefixes of the known routers that calls of the function pass in that
  position: calls in the same package directory, and `pkg.F(...)` calls
  whose import path names the package through a `go.mod` module path in
  the repository, followed through up to four levels of such functions;
  one route per distinct prefix. A group no call passes (or a method's
  parameter) gives an unknown base (`base_unknown`). Gin and Echo `:id` are route variables and `*rest`
  matches the rest of the path; the handler is the last argument in Gin and
  the first after the path in Echo. A method that is not literal leaves the
  route unread. chi routes (rule `chi-route`) are `Get`/`Post`/`Put`/
  `Patch`/`Delete`/`Head`/`Options`/`Connect`/`Trace(path, handler)`,
  `Method`/`MethodFunc(method, path, handler)` and `Handle`/`HandleFunc(path,
  handler)` (any method) calls on a router made by `chi.NewRouter()`/
  `chi.NewMux()`, typed `*chi.Mux`, returned by `With(...)` or
  `Route(prefix, ...)`, or the `chi.Router` parameter of a function literal
  passed to `Route(prefix, fn)` (the prefix is joined) or `Group(fn)` (no
  prefix); any other `chi.Router` parameter is a router passed in, as for
  Gin. chi's `{id}` and `{id:regex}` are route variables and a trailing `*`
  matches the rest of the path. A handler passed as a method value
  (`r.Route("/api", api.Routes)`) is not followed.
  Next.js API routes (rule `nextjs-route`) are files of a
  package whose `package.json` depends on `next`: `pages/api/**` (or
  `src/pages/api/**`) is one route for any method, handled by the default
  export, with `index` naming its directory; `app/**/route.{js,ts}` (or under
  `src/app`) is one route per HTTP verb the module exports (a function, a
  `const`, or `export { local as VERB }`), handled by that definition. Route
  groups `(name)` and parallel slots `@name` add no segment; `_private`
  directories and intercepting routes `(.)name` are not routes; `[x]` is a
  route variable and `[...x]`/`[[...x]]` match the rest of the path. A
  single literal `basePath` in `next.config.{js,mjs,cjs,ts}` prefixes every
  route; rewrites and middleware are not read. Flask `<id>`, Express
  `:id` and Go `{id}` are route variables; Flask `<path:x>`, FastAPI
  `{x:path}` and Go `{x...}` match the rest of the path. A Go path after a base
  path variable (`baseUrl + "/cart"`) is matched as if the base added no
  segments, as for requests, and its route records `base_unknown`. A handler
  that is a named function (or, in Go, a method name unique in the package
  directory) is linked by `HANDLES`; a closure's route hangs off its file.
  Python mounts compose across files: `parent.include_router(child,
  prefix=...)` joins the parent's prefix, the mount prefix and the router's
  own; `parent.register_blueprint(child, url_prefix=...)` joins the parent's
  prefix and the mount prefix, which replaces the blueprint's own. The child
  is a name bound once in its file, found through the file's imports
  (relative to its package, else under the nearest enclosing directory that
  holds the module, following re-exports). A router mounted twice gets a
  route per mount; one never mounted keeps its own prefix. Express mounts
  compose the same way: `parent.use([path,] ...routers)` joins the parent's
  prefixes with the path (each path of an array; a name bound to a string
  counts as a path). A mounted router is a name bound once to `express()`,
  `express.Router()` or an imported `Router()`, given directly, through
  `require(...)` or an import of a module's default (`module.exports = r`,
  `export default r`) or named (`exports.n = r`, `export const n = ...`)
  export. A path that is not literal is matched as if it added no segments,
  and the route records `base_unknown`. A destructured `require`
  (`const { Router } = require('express')`) binds each name to the module's
  export.
- **Requests.** Python `requests` and `httpx` module calls (`get`, ...,
  `request(method, url)`), the same calls on a client object (a
  `requests.Session`, `httpx.Client`/`AsyncClient` or `aiohttp.ClientSession`
  bound once to a name, named by an enclosing `with ... as`, or set once to
  `self.NAME` in the class), and Locust `self.client` calls in files importing
  `locust`. A literal `base_url` prefixes the path: httpx appends it to the
  base path and aiohttp joins it as a relative reference; an absolute URL, or
  a base that is not a literal, leaves the URL as written. JavaScript `fetch` (with `options.method`), `axios`, `request` and
  `superagent` (`.get(url)`, or `request({uri, method})`), and the same
  verb calls, `request({url, method})` or a call of the instance itself on an
  axios instance: a name bound once to `axios.create(config)`, `this.NAME`
  given that call as its one value in the class (a field initializer or a
  `this.NAME =` assignment), or a name imported from a repository module
  that exports one (`export default NAME`, `export const NAME =`, `export {
  NAME }` or `export default axios.create(...)`). The config's literal
  `baseURL` is joined to the request URL with one slash, unless the URL is
  absolute or protocol-relative, and a protocol-relative URL is read as
  http; a config that is not an object literal, or spreads one, gives an
  unknown base. Angular `HttpClient` and `httpResource` (below); Go `http.Get`,
  `http.Head`, `http.Post`, `http.PostForm` and `http.NewRequest[WithContext]`.
- **URL values.** String literals, `+`, template strings and f-strings,
  `str.format`, `%` and `fmt.Sprintf` (each field is unknown text), and names
  bound once in their function, else at module level, before the use (Python
  assignments, `const`/`let`/`var`, Go `var`/`const`/`:=`). A name bound more
  than once, assigned in another function, or declared `global` and assigned
  there is unknown text. A conditional (`c ? a : b`, `a if c else b`) whose
  two branches fold to the same text has that text, whatever the condition;
  otherwise it is unknown text. JavaScript and TypeScript also read
  `this.NAME` and constants shared between modules (below).

#### Angular HttpClient

Angular services call `HttpClient` through a field, and keep the API root in
a constant another module exports (`deepthought.service_contracts.web`,
`script_constants`, `folding`):

- **Clients.** `get`, `post`, `put`, `delete`, `patch`, `head`, `options`
  and `request(method, url)` on an `HttpClient`: `this.NAME` where NAME is a
  field set to `inject(HttpClient)` or typed `HttpClient` without a value, or
  a constructor parameter property typed `HttpClient`, or a name bound once
  to `inject(HttpClient)`. `HttpClient` and `httpResource` must be imported
  from `@angular/common/http` and `inject` from `@angular/core`.
  `httpResource(fn)` (and `.text`, `.blob`, `.arrayBuffer`) is a request for
  each value `fn` returns: a URL, or a request object's `url` and `method`
  (GET by default); `undefined` is skipped. Relative URLs are read against
  the site root, as for AngularJS. `request(HttpRequest)` is not read.
- **`this.NAME`.** The initializer of the instance field NAME of the
  enclosing class, or of a base class declared in the same file. It is
  unknown text when any code of the file assigns `this.NAME`, when a
  constructor parameter property is named NAME, when a class of the file
  that extends the enclosing class declares NAME again, or inside a
  `function` or a static member (where `this` is something else). A
  subclass in another file is not seen.
- **Constants across modules.** A module exports `export const NAME = value`
  (folded in its own module), `export { a as b }` of a name bound once there,
  and what it re-exports with `export { a } from` and `export * from` (a name
  two `export *` give differently is not exported, as in ECMAScript). An
  import by name takes the exported value when the module specifier resolves
  to an analysed file: a relative path, or a `compilerOptions.paths` alias of
  the nearest `tsconfig.json` (as for data contracts). `let`/`var` exports,
  default exports and namespace imports are not read.
- **Build-time globals.** A name no scope binds or imports
  (`SERVER_API_URL`) takes the value the Angular CLI `define` option gives it
  in `angular.json` (the project's `build` target, options and every
  configuration), for files under the project's `sourceRoot` (else `root`).
  Only a string literal is read; a name defined differently in two
  configurations is unknown text. `declare const` gives no value.

On jhipster/jhipster-sample-app (commit `6b000b5`) and
jhipster-sample-app-websocket (`410a632`), whose URLs are
`` `${serverApiUrl}api/...` `` with `serverApiUrl = SERVER_API_URL` exported
through a barrel and `SERVER_API_URL` defined as `''`, all 52 and 35
HttpClient and httpResource call sites are read. 42 and 23 link, every one
to the controller method with the same method and path. The rest stay
unresolved, correctly: Spring Boot Actuator `/management/*` endpoints and
Spring Security's login and logout filters have no controller, and two
clients have no server route (`PATCH /api/admin/users/{login}`, where the
server has only PUT, and `GET /api/users/{id}`, where it has only
`GET /api/users`). On darbyluv2code/fullstack-angular-and-springboot
(`e8832ee`, the auth0 order-history step), which injects `HttpClient` in the
constructor and keeps absolute URLs in fields, all 9 call sites are read;
the one `@RestController` route links, 7 Spring Data REST requests stay
unresolved because repository endpoints are not read, and one URL is a
method parameter. An `httpResource` request is placed on the
`httpResource` call. On the labelled contracts-jhipster-sample-app-websocket
corpus this scores http call sites 23 of 24 with no false positives (0
before); the miss is the SockJS handshake, whose URL goes through
`Location.prepareExternalUrl`. No edges changed
on the gated corpora.

These services have no `spring.application.name`, so their requests resolve
by path alone (`service-contracts.http-path`). A request path with no literal
text at all (`base + "/" + path`, or `"http://" + addr` with nothing after it)
is unresolved rather than matched against every route.

Each request is resolved by `deepthought.service_contracts.resolve`:

| Rule | Evidence | Links |
|---|---|---|
| `service-contracts.spring-route-mapping` | pattern_matched | handler -> HANDLES -> HttpRoute |
| `service-contracts.flask-route`, `service-contracts.fastapi-route`, `service-contracts.express-route`, `service-contracts.go-handle-func` | pattern_matched | handler (or file) -> HANDLES -> HttpRoute |
| `service-contracts.http-service-path` | pattern_matched | the request names a service (a Feign client without `url`, or the URL's literal host) equal to exactly one service's `spring.application.name` or deployment name, or to a host name only that service's deployments answer to (see "Services named by their deployments"); only that service's routes are candidates |
| `service-contracts.locust-host-service-path`, `-path`, `-gateway` | pattern_matched | as the rule after `locust-host-`, for a Locust request written without a host that takes its deployments' `--host` (see below) |
| `service-contracts.http-path` | pattern_matched | the request names no known service; every route in the repository is a candidate, except that a request whose URL starts with unknown text (a base URL variable, sent to a host configured elsewhere) does not match the routes of its own project (the nearest directory above it with a build manifest: `package.json`, `composer.json`, `pom.xml`, `build.gradle(.kts)`, `go.mod`, `pyproject.toml`, `setup.py`, `requirements.txt`, `Cargo.toml`, `Gemfile` or a `.csproj`); a file with no manifest above it, or under a `go.mod` that holds more than one `package main` directory, has no known project and excludes nothing |
| `service-contracts.http-gateway` | pattern_matched | the request has no literal host and its path is forwarded by a gateway route; the route's target service (when known) narrows the candidates |

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

#### Services named by their deployments

Routes whose language names no service (everything but Spring modules with
a `spring.application.name`) take the name of the deployments that run
their file (`service_contracts.deployed`), when those share one:

- A Compose service whose `build` context is the file's directory or an
  ancestor of it (the nearest one any deployment builds) is named after
  itself and answers to that name. When its Dockerfile publishes one .NET
  project, it runs that project's directory instead of the whole context,
  as Visual Studio's Dockerfiles build from the solution root. The
  Dockerfile is followed stage by stage: `WORKDIR` (a stage `FROM` an
  earlier stage keeps its directory and files), `COPY S D`/`ADD S D` of one
  context directory S (`.` for the whole context; not `--from`), and each
  `RUN dotnet publish [path/X.csproj]` (the project in the working directory
  when none is named), mapped back to the repository through those copies.
  Every publish must name the same directory, and it must hold a `.csproj`.
- A Kubernetes container whose image (without tag or digest) a Skaffold
  `build.artifacts` entry builds from exactly one context is named after
  its workload (`Deployment/frontend` gives `frontend`) and answers to the
  names of the Services in the same manifest directory and namespace whose
  selector its pod template labels match (`frontend`, `frontend-external`).
  Helm templates and manifests that are not plain YAML are not read; images
  that no Skaffold artifact builds (a registry path in a release manifest)
  place nothing.
- A .NET Aspire AppHost (a C# file calling
  `DistributedApplication.CreateBuilder`) names the projects it adds:
  `AddProject<Projects.X>("name")` runs the one `.csproj` whose file name,
  with characters other than letters, digits and `_` replaced by `_`, is
  `X`; `AddProject("name", "path")` and `AddDotnetProject("name", "path")`
  run the project at `path` (directory or `.csproj`, relative to the
  AppHost's directory). Each answers to its name (`catalog-api`). Project
  paths that are not literals, and the environment the AppHost sets, are
  not read.

A host name that deployments of two different services answer to names
neither. A request whose literal host is one of these names has only that
service's routes as candidates (`http-service-path`).

**Locust's `--host`.** A Locust request written without a host (`self.client
.get("/cart")`) takes the URL that `--host` (or `-H`) gives on the `locust`
command line of the deployments running its file: a Kubernetes container's
`command` and `args`, else the `ENTRYPOINT` and `CMD` of the Dockerfile in
its build context (`args` replace `CMD`). `${VAR}` and `$VAR` take the
literal value the deployment sets (container `env` `value`, Compose
`environment`); an unset or non-literal variable gives no host. All the
deployments must give the same host, which the link records as `host_from`
(the deployments' labels) under the `locust-host-*` rules. Shell scripts the
command runs, `host` attributes of the user class and `LOCUST_HOST` are not
read.

Online Boutique: `kubernetes-manifests/loadgenerator.yaml` sets
`FRONTEND_ADDR=frontend:80`, and `src/loadgenerator/Dockerfile` runs
`locust --host="http://${FRONTEND_ADDR}"`, so its `GET /`, which the
cartservice's `MapGet("/")` made ambiguous, links to the frontend again: 9
of 9 labelled HTTP call sites. The edge diff against `main` over
robot-shop, train-ticket, the C# repositories and 16 other validation
repositories changes only these 9 Locust links (from `http-path` to
`locust-host-service-path`).

#### Apache HttpClient and fields set by constructors

Java code that builds a request with Apache HttpClient (`new HttpGet(url)`,
`new HttpPost(url)`, ... from `org.apache.http.client.methods` or the 5.x
`org.apache.hc.client5.http.classic.methods`) is a request with that
method, placed on the `new` (`service_contracts.spring`). The URL is folded
as for RestTemplate, and one more kind of name is folded for every Java
client: a String field with no initializer and no annotation that only
constructors assign, each time from one of their parameters
(`this.baseUrl = baseUrl;`). Its values are that argument of every `new` of
the class in the repository with as many arguments, folded in the file that
writes it (`java_constructions`). With none, or when one `new` passes wholly unknown text, or when another assignment (a literal in a second constructor, a setter) sets the field, it is unknown: the request is unresolved rather than linked through the values the other constructions give. robot-shop's
shipping service builds `new CartHelper(CART_URL)` with
`String.format("http://%s/shipping/", ...)`, so `new HttpPost(baseUrl + id)`
is `POST http://{?}/shipping/{?}` and meets the cart service's route.

### PHP: Symfony routes and curl

PHP files are read for routes and requests (`service_contracts.php`):

- **Routes.** A method carries a `@Route` annotation in its docblock or a
  `#[Route]` attribute, where `Route` is imported from
  `Symfony\Component\Routing\Annotation` or `...\Attribute`. The path is
  the first argument or `path`, joined with the class's own `Route`; the
  methods are `methods` (any method without it). A path that is not a
  literal is unread. Routes in YAML, XML or PHP config files, and a
  kernel-level prefix, are not read (rule `service-contracts.symfony-route`).
- **Laravel and Lumen routes** (`service_contracts.laravel`, rule
  `service-contracts.laravel-route`), in projects whose nearest
  composer.json requires `laravel/framework` or `laravel/lumen-framework`.
  Calls on the `Route` facade (imported from `Illuminate\Support\Facades`,
  or unqualified in a Laravel project) or, in Lumen, on `$router`: `get`,
  `post`, `put`, `patch`, `delete`, `options`, `any`, `match`, `view` and
  `redirect` with a literal path, and the actions of `resource` /
  `apiResource` (and the array forms), with `only`/`except` and dotted
  nested names (parameters are `{var}`). Groups nest: `prefix(...)`,
  `group(['prefix' => ...], fn)`, `controller(C::class)` and `namespace`.
  A route file's own prefix and controller namespace come from the code
  that loads it: `Route::prefix('api')->namespace(...)->group(base_path(...))`
  (a `$this->namespace` property default is read), `withRouting(api: ...,
  apiPrefix: ...)` (`api` by default), a Lumen `$app->router->group([...],
  function () { require ...; })`, or `loadRoutesFrom`. A file no loader
  names has no prefix. The handler is the controller method an action
  names (`[C::class, 'm']`, `'C@m'`, invokable `C::class`, a name in a
  `controller` group, `['uses' => ...]`), found in the same composer
  project by its qualified name or else its unique short name; closures,
  views and redirects hang off the route file. `{id?}` is `{id}`. A path
  that is not literal is unread.
- **Requests.** `curl_init($url)` is a request placed on that call. The URL
  is folded in its function: literals, `"...$x..."` and `"...{$x}..."`
  (each variable folded as below; other interpolations are unknown text),
  `.`, `sprintf` with `%s`/`%d`, and a local variable assigned once before
  the use; properties are unknown. The method is GET, or what
  `curl_setopt`/`curl_setopt_array` in the same function sets on the handle
  with a literal `CURLOPT_CUSTOMREQUEST` (POST for `CURLOPT_POST`); any
  other value makes it unknown. A handle whose URL is set with
  `CURLOPT_URL` is not read yet.
- **Guzzle and Laravel HTTP client requests** (clients `guzzle`,
  `laravel-http`). Guzzle: `get`, `post`, `put`, `patch`, `delete`, `head`,
  `options` (and their `...Async` forms) and `request($method, $url)` on a
  `GuzzleHttp\Client`, where the receiver is a local variable assigned
  once in its function to `new Client([...])`, a `$this->x` property
  assigned once in its class to one, or a parameter, property or promoted
  constructor parameter typed `Client` or `ClientInterface`. A literal
  `base_uri` is resolved as Guzzle does (RFC 3986: a rooted path replaces
  the base path, a relative one joins the base's directory); an absolute
  URL is kept. A base that is not literal is taken to start with its
  origin and to add no path of its own: a rooted path follows the unknown
  origin (`{?}/books`), a relative one the base's literal directory
  (`'%env(URL)%/api/'` and `books` give `{?}/api/books`), and a relative
  one against a base with no literal directory stays glued to the unknown
  text, so it is not matched. Laravel:
  `Http::get(...)`, ... and `Http::send($method, $url)`, also at the end of
  a chain (`Http::withToken(...)->get(...)`), on the facade imported from
  `Illuminate\Support\Facades\Http` or unqualified in a Laravel project;
  a literal `baseUrl(...)` in the chain is prepended (one that is not
  literal gives unknown text, as for Guzzle). A method that is not
  a literal is unknown; a dynamic method name (`->$method(...)`) is read
  when it folds to one of the verbs, else the call is not read.
- **Symfony HttpClient requests** (client `symfony-http`).
  `request($method, $url)`, its only request method, on a client that is a
  parameter, property or promoted constructor parameter typed
  `Symfony\Contracts\HttpClient\HttpClientInterface`, or a local variable
  or `$this->x` property assigned once to `HttpClient::create([...])` or
  `HttpClient::createForBaseUri($base)`. A literal base (`base_uri`, or the
  `createForBaseUri` argument) is resolved as for Guzzle (RFC 3986). A
  scoped client declared under `framework.http_client.scoped_clients` in the
  composer project's `config/packages/*.yaml` is autowired by argument name,
  as Symfony parses it (`orders.client` is `$ordersClient`, and so is
  `$orders_client`), so an injected parameter or promoted property with that
  name takes the client's `base_uri` when it is literal; a parameter or
  `%env(...)%` in the base is unknown text, resolved as for Guzzle. A parameter attribute
  `#[Autowire(service: 'id')]`, `#[Autowire('@id')]` or `#[Target('id')]`
  names the client instead (any other `#[Autowire]` gives no base);
  `services.yaml` bindings are not read. For Guzzle and Symfony clients
  alike, a `$this->x` property assigned once from a typed parameter is that
  parameter's client.
- **Values passed to a wrapper method.** Gateways often send every request
  through one method that takes the method and URL as parameters
  (`performRequest($method, $requestUrl)`, often in a trait), so they are
  fixed only where it is called. When a Guzzle, Symfony or Laravel client
  site's method or URL changes with the parameters of the method or
  function around it, which nothing there assigns again, the site takes,
  instead, each value a call of it passes, folded in the calling file. Calls
  are `$this->m(...)` in a class that declares `m` or uses a trait that
  does, and `$this->p->m(...)` where `p` is a property typed with a class
  (a typed property, a promoted constructor parameter, or a property
  assigned once from a typed parameter) that declares `m` or uses a trait
  that does. A class or trait name declared more than once in the
  repository is not followed. Arguments bind by position, named arguments
  by name, a left-out argument takes the parameter's default, and
  arguments from a spread on are unknown. One request is added per
  distinct method and URL, with `argument_from` listing the calls
  (`file:line`) that pass it. Parent classes, static calls and a value
  passed through two wrappers are not followed.

robot-shop `55292e2` ratings (Symfony 4 with annotations): both routes,
`PUT /api/rate/{sku}/{score}` and `GET /api/fetch/{sku}`, are found, so the
web front end and load generator reach ratings through the nginx
`/api/ratings/` location. Its curl call to `sprintf('%s/product/%s', ...)`
meets the catalogue's `GET /product/:sku`. With the Java change above, the
corpus scores 42 of 42 labelled call sites and 17 of 17 service pairs, none
wrong (37 and 14 before).

### ASP.NET Core routes

C# files are read for routes (`service_contracts.aspnet`):

- **Controllers** (rule `service-contracts.aspnet-controller-route`). A
  class counts when it carries `[ApiController]`, derives from
  `Controller`, `ControllerBase` or an Ardalis.ApiEndpoints base
  (`EndpointBaseAsync...`), or is named `...Controller`; `[NonController]`
  excludes it. An action is a method with `[HttpGet]`, `[HttpPost]`, ...
  (optionally with a template) or `[Route]`. Its template is joined with
  each of the class's `[Route]` templates unless it starts with `/` or
  `~/`. `[controller]` is the class name without `Controller`, `[action]`
  the method name without an `Async` suffix, `[area]` the class's `[Area]`.
  An action with no template on either level is conventionally routed
  (`MapControllerRoute`) and not read.
- **Minimal APIs** (rule `service-contracts.aspnet-minimal-api-route`).
  `MapGet/MapPost/MapPut/MapDelete/MapPatch(template, ...)` and
  `MapMethods(template, new[] { "GET", ... }, ...)` with a literal
  template; the methods of `MapMethods` are string literals or
  `HttpMethods.X` constants, anything else leaves it unread
  (`methods_not_literal`). `MapGroup(prefix)` in the receiver chain, or a local bound
  once to one, adds its prefix; builder calls such as `WithTags` or
  `NewVersionedApi` pass through. A receiver that is a `RouteGroupBuilder`
  or `IEndpointRouteBuilder` parameter gets an unknown base, joined with
  any known group prefixes after it. A template that is not a literal is
  unread (`path_not_literal`).

Constraints and defaults (`{id:int}`, `{id?}`, `{*rest}`) are read as plain
parameters. As in ASP.NET Core, a request meets these routes' literal
segments without regard to case (`/api/Orders` meets `[controller]` read as
`orders`); routes of other frameworks stay case-sensitive.

Checked by hand against a lexical count of route attributes and `Map*`
calls: eShopOnWeb `4da8212` 33 of 33 (26 attribute routes, 7 minimal API),
dotnet/eShop `669682e` 31 of 31 with the group prefixes it builds
(`/api/catalog/items`, `/api/orders/cancel`), grpc-dotnet `7c1a477` 5 of 5.
CleanArchitecture `f7f9771` maps its endpoints by reflection through its own
`MapGet(handler, template)` overloads; all 15 are unread rather than
guessed.

online-boutique's cartservice maps `GET /` to a notice that it only speaks
gRPC. The load generator's `l.client.get("/")` written without a host meets
both that route and the frontend's `GET /`; the host its deployment gives
it picks the frontend (see "Services named by their deployments").

### C# HttpClient requests

C# files are read for `HttpClient` and Refit requests
(`service_contracts.csharp_http`, client `dotnet-httpclient`):

- **Clients.** A call on a value of type `HttpClient`: a parameter (primary
  constructor parameters included), field, property or local declared with
  that type, or `new HttpClient(...)`, looked up in the innermost enclosing
  member, function or type that declares the name. `GetAsync`,
  `GetStringAsync`, `GetFromJsonAsync`, `PostAsync`, `PostAsJsonAsync`,
  `PutAsync`, `PatchAsync`, `DeleteAsync` and their JSON variants fix the
  method; `SendAsync` takes it from `new HttpRequestMessage(HttpMethod.X,
  url)`, in place or through a local bound once to one.
- **URLs.** Folded from string literals, interpolated strings, `+`,
  `new Uri(...)`, locals assigned once, fields of the class that nothing
  assigns but their initializer, `TrimEnd`/`TrimStart`/`Trim` with
  character literals, and calls of a method of the file (the only one of
  its name) that returns one expression, with its parameters bound to the
  call's arguments. Anything else is unknown text.
- **Typed clients.** `AddHttpClient<T>(o => o.BaseAddress = new("..."))`
  (or `new Uri(...)`, or `AddHttpClient<I, T>`) anywhere in the repository
  gives class `T` a base address. `T` is the one class of that name whose
  namespace is in scope at the registration (enclosing namespaces, `using`
  directives, and the `global using`s of the registering file's project,
  the nearest `.csproj` directory). The base applies to the `HttpClient`
  fields, properties and constructor parameters of `T`, not to locals. A
  relative URL is resolved against it as a URI (`https+http://` service
  discovery addresses count as `http://`). A class registered with a base
  that is not literal, or with different bases, has none, and its relative
  URLs are paths under an unknown base. A `BaseAddress` set in a
  `.ConfigureHttpClient(...)` chained on the registration counts too.
- **Refit** (client `refit`). Each method of an interface carrying
  `[Get("/path")]`, `[Post(...)]`, `[Put]`, `[Delete]`, `[Patch]`, `[Head]`
  or `[Options]` from the `Refit` namespace (`using Refit;`, a
  `global using` of the project, or `Refit.Get`) is a request, placed on
  the attribute and sent from the method. `AddRefitClient<I>()` with a
  `BaseAddress` (as for typed clients) or `RestService.For<I>("url")` gives
  interface `I` a base. Refit appends the path to the base, base path
  included; with no literal base the path is read as written. A path that
  is not a string literal is unknown text.

Requests are resolved like any other (`http-service-path`, `http-path`,
`http-gateway`). Wrappers that take the URL as a parameter of a public
method (eShopOnWeb's `HttpService`), `IHttpClientFactory.CreateClient`
results assigned to `var`, and API version query parameters are not
read.

dotnet/eShop `669682e`: the WebApp's `CatalogService` and
`OrderingService` (bases `https+http://catalog-api` and
`https+http://ordering-api`, registered in another project) and the
WebhookClient's `WebhooksClient` give 10 of 11 labelled call sites and 3 of
3 service pairs of `contracts-eshop-http`, none wrong. The miss is
`GET /api/catalog/items?...`, which both API versions of Catalog.API serve
at the same path; the version is chosen by a query parameter the client
adds through a message handler, so it stays ambiguous.

### NestJS controllers

In TypeScript files that import from `@nestjs/common`, a class decorated
`@Controller(path)` (a string, an array of strings, `{ path }`, or nothing)
declares a route for each method decorator `@Get`, `@Post`, `@Put`,
`@Delete`, `@Patch`, `@Options`, `@Head` or `@All` imported from that
package, under each controller path and method path. `:id` is a variable
and a final `*name` segment matches the rest. The handler is the method.
The application's global prefix comes from `setGlobalPrefix("literal")` in
a TypeScript file of the same Node package (nearest `package.json`), and
applies to every route of that package. The rule is
`service-contracts.nestjs-route` (pattern_matched).

Versions are read when the package calls `enableVersioning` with a literal
options object: `type: VersioningType.URI`, `HEADER`, `MEDIA_TYPE` or
`CUSTOM`, an optional string `prefix` (`v` by default, none for `false`)
and an optional `defaultVersion` (a string, an array of strings or
`VERSION_NEUTRAL`). A route's versions are its method's `@Version(...)`,
else its controller's `version` option, else the default version. With URI
versioning each version adds a `{prefix}{version}` segment after the global
prefix, one route per version, and `VERSION_NEUTRAL` (or no version at all)
adds none. The other types select a version from the request's headers, so
they leave the path alone.

`RouterModule.register([...])` with a literal array of `{ path, module,
children }` entries gives each module class its path, children joined to
their parent's. A controller takes the path of the `@Module({ controllers:
[...] })` classes of its package that list it: the registered module's
path before the controller path (after the global prefix and version), or
none for a module the routes do not name.

Counted as unread instead of guessed, by reason:
- `version`: a controller or method whose version is not a literal (string,
  array of strings or `VERSION_NEUTRAL`), or that has a version in a
  package that does not enable versioning with literal options;
- `versioning`: every route of a package whose `enableVersioning` options
  are not a literal object of the form above, and `versioning_ambiguous`
  when the package enables versioning in two different ways;
- `router_module`: every route of a package whose `RouterModule` routes are
  not a literal array of literal paths and module classes, or that
  registers a module whose controllers are not a literal list; and a
  controller listed by modules with different paths;
- `router_module_ambiguous`: every route of a package that registers one
  module at two paths;
- `global_prefix_options`, `global_prefix_not_literal` and
  `global_prefix_ambiguous`: every route of a package whose global prefix
  has exclusions, is not a literal, or is set to two different values.

On the 39 samples of the NestJS repository (commit `fed38a71`), 81 routes
are read from 81 route decorators. The samples' own end-to-end tests make
83 requests with a literal path, leaving out GraphQL and Swagger JSON. 81
of them match a route of the same sample. The other two are not controller
routes: the Swagger UI page, and a test that expects 404 for `GET` on a
`POST` route.

### WebSocket endpoints

A WebSocket connection opens with an HTTP GET handshake. Each endpoint is
read as the `HttpRoute` of that handshake (`methods` GET), so clients are
linked by the usual HTTP resolution:

- **Spring.** In a method with a `StompEndpointRegistry` or
  `WebSocketHandlerRegistry` parameter (from
  `org.springframework.web.socket.config.annotation`), each literal path
  given to `addEndpoint(...)` or to `addHandler(handler, ...)` on that
  parameter is a route under the servlet context path, handled by the method
  (framework `spring-websocket`, rule `service-contracts.spring-websocket-endpoint`).
  SockJS fallback paths below the endpoint are not listed.
- **Jakarta / javax.** A class with `@ServerEndpoint` from
  `jakarta.websocket.server` or `javax.websocket.server` and a literal path is
  a route handled by the class (framework `jakarta-websocket`, rule
  `service-contracts.jakarta-websocket-endpoint`). Only a Spring module's
  context path is joined.
- **Clients.** In JavaScript and TypeScript, `new WebSocket(url)` and
  `new SockJS(url)` (or `SockJS(url)`, which SockJS accepts) are GET
  requests (client `websocket` or `sockjs`). The
  constructor is the browser's global when the file does not import it.
  Under any local name, it may also come from `ws`, `isomorphic-ws` or
  `sockjs-client`; a constructor imported from another module is not read.
  STOMP clients name their handshake in their options: `brokerURL` given
  to a `@stomp/stompjs` `Client` or `@stomp/rx-stomp` `RxStomp`
  constructor or `configure` call is a `websocket` request, and `host`
  given to an ng2-stomp-service `configure` call is a `sockjs` request (the
  library opens `new SockJS(host)`). The client is recognised as for STOMP
  destinations below.
  `ws://` and `wss://` URLs are read like `http://` ones. A relative URL is
  read against the site root.

A path that is not literal is `route:path_not_literal`. Scripts inside HTML
pages and `.vue` files are not parsed, so clients written there are not
found.

Results on 15 repositories (13 cloned for this slice, plus the
apssouza22/java-microservice corpus):

| Repository | Server endpoints | Clients in parsed JS/TS | Outcome |
|---|---|---|---|
| mark-dev/chessfactory `b54acdb` | 1 (`/wchess`) | 1 literal | Linked to the endpoint |
| camunda-community-hub/zeebe-simple-monitor `0f80785`, zeebe-simple-tasklist `e24fddb` | 1 each | 1 each, `base_path + 'notifications'` | Unresolved: the base comes from the page template |
| manusa/yakd `203f051` | 1 (`/api/v1/pods/{namespace}/{name}/exec/{container}`) | 1, `${getWsUrl()}/pods/...` | Unresolved: the unknown base holds `/api/v1` |
| CodeDefenders `135180c`, m-reza-rahman/jakartaee-javascript `9ae218e` | 1 each | 1 each, URL from a field or constant built at run time | Unresolved |
| ocinpp/springboot-sockjs-stomp-vue-sample, ygunayer/realtime-messaging, ben-rampel/Poker, leansd/cotrip, LianTianNo1/wiki_vue3, zhaxal/java-chess (2), cryostatio/cryostat, apssouza22/java-microservice | 9 | Clients in HTML or `.vue`, or none | Endpoints found; the cotrip path comes from a constant |
| MicroIoT/iotplatform `8fa6874` | path from configuration | none | `path_not_literal` |

All 15 literal endpoints are found, and the one literal client URL is
linked to the right endpoint. No link is wrong. On the other corpora, the
only new edge is apssouza22's `/socket-todos` endpoint.

### GraphQL root fields

Each root field (`Query.x`, `Mutation.x`, `Subscription.x`) of a GraphQL
server is one `GraphqlField` node (labels `GraphqlField`, `Contract`;
`protocol` `graphql`), scoped to the Node package (the nearest
`package.json`) of the files that declare or resolve it
(`deepthought.service_contracts.graphql`):

- Schema files (`*.graphql`, `*.gql`) declare the fields of `type Query`,
  `type Mutation` and `type Subscription`, including `extend type` blocks.
  Descriptions, comments, arguments and directives are skipped. The node
  records them as `declared_in`.
- NestJS resolvers: a method of a class decorated `@Resolver` that carries
  `@Query`, `@Mutation` or `@Subscription`, all imported from
  `@nestjs/graphql`, resolves the field named by the decorator's literal
  name (`@Query('cats')`), the `name` of its options, or else the method
  name. It links to the field by `HANDLES` (rule
  `service-contracts.nestjs-graphql-resolver`, pattern_matched).
- Resolver maps, in the JavaScript and TypeScript files of a Node package
  whose `package.json` depends on a server that takes them (`@apollo/server`,
  `apollo-server*`, `@apollo/subgraph`, `graphql-tools`,
  `@graphql-tools/schema`, `graphql-yoga`, `mercurius`): each member with a
  literal name of an object literal under a `Query`, `Mutation` or
  `Subscription` key resolves that field (rule
  `service-contracts.graphql-resolver-map`, pattern_matched). The map stands
  alone or is the server's `resolvers` option; the same keys nested under
  any other key (Apollo Client's `typePolicies` and `cacheRedirects`) are not
  resolvers. A member that is a function declared in the file
  (`{Query: {me}}`, `{tick: {subscribe: fn}}`) anchors the edge to that
  function, anything else to the file. A field's options object is read the
  same way whether its `subscribe` (preferred) or `resolve` option is a
  pair, a method (`subscribe() {...}`) or a shorthand (`{me}`), and
  `withFilter(fn, ...)` anchors to its first argument when that is a
  function or a name of one. A shorthand member bound to an options object
  of the same file (`const tick = {subscribe: fn}`) is followed once. A
  function expression passed as an argument has no definition of its own,
  so its edge stays on the file.
- A root key bound to a name is followed: to an object literal of the same
  file (`const Query = {...}`), or to a relative module of the repository
  bound by `require` (whole, destructured or `.Name`) or `import` (default,
  named or namespace). The module's `module.exports`, `export default`,
  `exports.Name` or `export const Name` object literal lists the fields,
  and a namespace import takes its exported functions and constants.
- In those packages, GraphQL templates declare fields as schema files do:
  templates tagged `gql` or `graphql`, marked by a `/* GraphQL */` comment,
  or starting with `#graphql`. A substitution (`${fragment}`) is not read.

Clients: in a Node package that depends on a GraphQL client
(`@apollo/client`, `apollo-client`, `apollo-boost`, `react-apollo`,
`@apollo/react-hooks`, `urql`, `@urql/core`, `graphql-request`,
`react-relay`, `relay-runtime`), the operations of its GraphQL templates
(as above) and of its `*.graphql`/`*.gql` files request the root fields
they select. An alias requests its field, and `__typename` and fields marked
`@client` (resolved by Apollo Client itself) are not requests. Each
requested field links by `REQUESTS` (rule
`service-contracts.graphql-client-operation`, pattern_matched; properties
`request`, `operation_name`, `line`, `column`) from the declaration that
holds the operation, or its file, to a field of the servers nearest the
client. Those are the server packages (packages that depend on a GraphQL
server library, or that resolve fields) whose common directory with the
client's package is deepest. A client in `start/client` thus belongs to
`start/server` and not to `final/server`, while a top-level `client/` sees
every server. Among them, the edge goes to the one that resolves the
field, or else to the one that declares it. A `.graphql` document with
requests gets a `File` node (language `graphql`) that the repository
`CONTAINS` (rule `service-contracts.repository-contains-graphql`).

Schemas and operations are parsed by graphql-core (`graphql-core`, a
required dependency), the reference GraphQL parser for Python, and by
nothing else: root fields are the fields of the parsed object type
definitions and extensions, and of the parsed operations' first selection
level, with the lines and columns of their names. Template substitutions
(`${...}`) are blanked before parsing. Without graphql-core the tool raises
`GraphqlCoreMissing` instead of reading GraphQL another way; a source
graphql-core rejects is unread. On the 32 GraphQL validation repositories
removing the tokenizer fallback changes no link; one deliberately invalid
gqlgen test schema is now counted as `graphql:schema_not_parsed`.

Counted as unread:
- `graphql:schema_not_parsed` and `graphql:operation_not_parsed`: a schema
  file or an operation document (a `.graphql` file or a `gql` template)
  that graphql-core does not parse;
- `graphql:schema_root_renamed`: a schema definition that names a root
  type other than `Query`, `Mutation` or `Subscription`;
- `graphql:field_not_literal`: a resolver name that is not literal;
- `graphql:resolver_map_not_literal`: a `Query` (or other root) key whose
  value is not an object literal (`{Query: queries}`);
- `graphql:resolver_spread`: a spread or other member of a resolver map
  whose fields are not listed in place (`{...more}`);
- `graphql:source_not_parsed`: a script of such a package, naming a root
  type, that the grammar could not fully parse (for example one with Flow
  annotations); service contracts skip such files;
- `graphql:resolution_unknown`: a declared field without a read resolver in
  a package that has any of the three above, where it may be resolved by
  code the tool does not read;
- `graphql:declared_not_resolved`: a declared field that no resolver
  handles, in a package whose resolver maps are all read;
- `graphql:resolved_not_declared`: a resolved field missing from a package
  that has schema files;
- `graphql:client_fragment_root`: a fragment spread or inline fragment among
  an operation's root fields;
- `graphql:client_field_unknown`: a requested field that none of the
  client's nearest servers resolves or declares (an outside API, or drift);
- `graphql:client_field_ambiguous`: a requested field that several of them
  resolve.

Field resolvers of object types, and fragments at the root of an
operation, are not read yet. Clients link by field name among their
nearest servers, not by the server URL they are configured with.

On the NestJS samples (commit `fed38a71`), 26 root fields are read in seven
GraphQL servers, 23 of them also declared in schema files. The two
code-first samples commit the `schema.gql` that Nest itself generates from
the decorators, and its fields are exactly the ones read from the
resolvers. In the schema-first samples, every declared field has a
resolver and every resolver has a declaration. The four root fields that
the samples' end-to-end tests query (other than introspection) are all
among them.

Resolver maps were checked on two Apollo servers, against the fields their
schemas declare and the resolver files read by hand:

- `apollographql/fullstack-tutorial` (commit `fca98de`): the server's six
  root fields are declared in a `gql` template and all six are resolved, by
  the right functions. The client package depends on `@apollo/client` only
  and adds nothing.
- `howtographql/graphql-js` (commit `e0a3a21`): the resolvers are CommonJS
  modules (`module.exports = { feed }`) joined in `src/index.js` as
  `{ Query, Mutation, Subscription }`. Of the 8 root fields in
  `src/schema.graphql`, the 7 with a resolver are read, each from its
  module, and `Query.info`, which has none, is the one
  `declared_not_resolved`.
- `withspectrum/spectrum` (commit `e8ebdcb`): 30 root fields are declared in
  `/* GraphQL */` templates, 26 of which have a resolver in the code (the
  other four are the `dummy` fields of the root types and a removed
  `invoice` query). 19 are read, each by its right resolver, and none is
  wrong. The seven missed are in three resolver files whose Flow type
  annotations the JavaScript grammar cannot parse; such files are left out
  of every contract. They count as `graphql:source_not_parsed`, so the 11
  declared fields without a read resolver are `graphql:resolution_unknown`
  rather than reported as unimplemented. Apollo Client's `cacheRedirects` in
  the shared client code is not read as a resolver map.

Clients were checked the same way. On `fullstack-tutorial` the two client
packages hold 10 operations. Two are `@client`-only and request nothing.
The other 8 are read. The 7 in the finished client each link to the server
field they name. The starter client's `GetLaunch` is
`graphql:client_field_unknown`: its nearest server, the starter's own, does
not declare `launch` yet. On `apollographql/spotify-showcase` (commit
`5a9afc5`) all 46 client root fields link to the right one of its two
subgraphs. On `spectrum` the
four client operations in files the grammar parses fully are read. Three
link to their mutations, and `subscribeWebPush` is
`graphql:client_field_unknown`: the server no longer declares it.

### Gateways

Browser and load-test code usually calls a gateway, not the service:
robot-shop's pages call `/api/cart/...`, which nginx forwards to the cart
service as `/...`. The tool reads four kinds of gateway route
(`deepthought.service_contracts.gateways`):

- **Spring Cloud Gateway.** `spring.cloud.gateway.routes` (or
  `spring.cloud.gateway.server.webflux.routes`) in the YAML configuration of
  a module (the same files as service names; profile documents are not
  read), with `Path=` predicates of literal segments ending in `/**`, a `uri`
  of `lb://service` or `http(s)://host`, and `StripPrefix` and `PrefixPath`
  filters, in shortcut or full form. A `RewritePath` that swaps a literal
  path prefix and keeps the rest (`/old/?(?<rest>.*), /new/${rest}` or
  `/old(?<rest>/?.*), /new${rest}`, optionally anchored, with `$\{rest}`,
  `${rest}` or `$1`) is read when it is the route's only path filter and its
  prefix starts the route's `Path=` prefix. A route with another rewrite
  (`RewritePath`, `SetPath`), a path pattern with wildcards before `/**`, or a
  `StripPrefix` longer than the prefix is counted as unread.
- **YARP.** The `ReverseProxy` section of `appsettings.json` (JSON with
  comments and trailing commas) in a .NET project with analyzed C# sources.
  A route's `Match.Path` must be literal segments followed by a catch-all
  (`/catalog-service/{**catch-all}`). Its path transforms apply in order:
  `PathRemovePrefix` (which must remove a prefix of the path so far),
  `PathPrefix`, and a `PathPattern` of literal segments followed by the same
  catch-all. Every destination of the route's cluster must have a literal
  `Address` with the same path, which YARP prepends; their host is the
  target when they share one. `PathSet`, other route values and other
  patterns leave the route unread. Environment overlays
  (`appsettings.Development.json`, ...) are not read, as with Spring
  profiles.
- **Ocelot.** `Routes` (or `ReRoutes`, before Ocelot 16) in a JSON file of
  a .NET project with analyzed C# sources that holds
  `UpstreamPathTemplate`s. The upstream and downstream templates must be
  literal segments followed by the same placeholders in the same order
  (`/merchant/api/{everything}` to `/api/{everything}`), and the upstream
  prefix is replaced by the downstream one. A template without placeholders
  takes only its own path; with placeholders, the last takes the rest of the
  path (Ocelot matches it with `.+`) and each other one segment. Query
  templates are ignored. `UpstreamHttpMethod` limits the methods the route
  takes; a request with another method, or a path the templates do not
  take, is not forwarded by it. The target is the `ServiceName` (service discovery), else the host
  every `DownstreamHostAndPorts` entry shares. Other templates (no literal
  prefix, a placeholder before literal text, reordered placeholders) are
  unread. A file `x.Env.json` beside `x.json` (`ocelot.Docker.json`) is an
  environment overlay and is not read, as for YARP.
- **nginx.** `location /prefix/ { proxy_pass http://host[:port][/uri]; }` in
  `*.conf` and `*.conf.template` files outside hidden, `node_modules` and
  `vendor` folders. A `proxy_pass` with a URI replaces the prefix with it, as
  nginx does; one without passes the path on. Exact (`=`) and regex
  locations, and `proxy_pass` URIs with variables, are counted as unread; a
  host with a variable (`${CART_HOST}`) leaves the target unknown.

A request with no literal host that names no known service, whose path
starts with the literal prefix of a gateway route, is forwarded: the longest
prefix wins (two routes of that length forwarding differently make the
request ambiguous), the prefix is replaced, and the route's target narrows
the candidates as a host would. The link uses rule
`service-contracts.http-gateway` and records the gateway route as
`gateway` (`file:line`). AngularJS `$http` requests are read too, and their
relative URLs (`api/vet/vets`) are read against the site root, as for a page
served there.

### OpenAPI spec-first Spring services

A spec-first Spring service keeps its routes in an OpenAPI document, and
`openapi-generator-maven-plugin` turns the document into Java interfaces at
build time. Its controllers implement those interfaces, so the repository
holds no mapping annotations for these routes
(`deepthought.service_contracts.openapi`). For each plugin execution in a
`pom.xml` with generator `spring` (library `spring-boot` or none), plugin
version 7 or later, `interfaceOnly` true and no `delegatePattern`, every
operation of the `inputSpec` file (OpenAPI 3 or Swagger 2, YAML or JSON) is
read as a method of a generated interface:

- the interface is `apiPackage.<Group><apiNameSuffix>`, once
  per group (Spring's `toApiName` takes no `apiNamePrefix`), where the groups are the operation's tags with `useTags`
  (`default` when it has none) and its first path segment without it, as
  the generator's `addOperationToGroup` does;
- the method is the `operationId` with a lower-case first letter. Without
  one, it is the name the generator makes (`getOrGenerateOperationId`): the
  path segments without braces and then the HTTP method, the first with a
  lower-case first letter and the rest camelized (`/policies/{id}` GET is
  `policiesIdGet`). That name is read only when the first segment is letters
  and digits and the others are letters and digits joined by hyphens.
  Anything else is `openapi:operation_id`;
- `apiPackage`, `apiNamePrefix` and `apiNameSuffix` are read from the
  execution's configuration or its `configOptions`, as the generator reads
  both. Two different values are `openapi:config_conflict`.

A `@RestController`/`@Controller` class that implements the interface and
declares exactly one method of that name handles the operation. The route is
the operation's path joined with the class's `@RequestMapping` path and the
servlet context path, since version 7's default `requestMappingMode` puts no
base path on the interface. Its `HttpRoute` node records the operation's
`declared_in` spec location. The rule is
`service-contracts.openapi-spring-route` (pattern_matched).

Counted as unread instead of guessed:
- an execution with another generator or library (`spring-cloud` is a Feign
  client), an older plugin, a generated controller, a base path on the
  interface (`requestMappingMode` `api_interface`), or a spec that is not a
  file in the repository;
- an `operationId` that is not a plain Java identifier;
- a method that is overloaded or not overridden (the generated default
  answers 501);
- `openapi:operation_not_handled`, for each operation that no controller in
  the repository handles.

A module inherits the plugin's version and configuration (with
`configOptions` merged entry by entry) from its parent poms in the
repository, found through `<parent><relativePath>` (default `../pom.xml`)
when the artifactId there is the declared parent's. Properties are inherited
the same way. A `configurationFile` (JSON or YAML) supplies settings under the
pom's own. Without `apiPackage`, the generator's default applies
(`org.openapitools.api` for spring, `org.openapitools.client.api` for java).

Gradle builds that apply `org.openapi.generator` are read the same way
(`deepthought.service_contracts.openapi_gradle`): the `openApiGenerate { }`
block and each task of type `GenerateTask` (Groovy `task x(type: ...)`,
Kotlin `tasks.register<GenerateTask>`, `tasks.register("x", GenerateTask::class)`
and `by tasks.registering(GenerateTask::class)`), with comments skipped.

A setting is read when it is written `name = value`, `name.set(value)` or
`name value` at the block's top level, and its value is one of:
- a string literal (`.toString()` allowed), with `$rootDir`, `$projectDir`
  and their `${...}`/`project.`/`rootProject.` forms replaced; the root is
  the nearest `settings.gradle(.kts)`, and a relative `inputSpec` is
  relative to the project directory;
- a boolean;
- for `configOptions`, a Groovy map literal or a Kotlin `mapOf(... to ...)`.

Any other value of a setting that matters (generator, spec, package,
library, name affixes, options) makes the task `openapi:config_not_literal`.
The plugin version comes from a `plugins { id ... version ... }` block in
the build or its settings, or from a buildscript
`openapi-generator-gradle-plugin` classpath, with `gradle.properties`
values from the root down to the project replaced.

Checked on three public builds; each one is read as written:

| Build | Read as | Outcome |
|---|---|---|
| Guardian-Development/contract-first-service (`7be7d87`) | spring, plugin 4.3.1 | `generator_version` |
| maschnetwork/openapi-code-generator (`d2ebb3a`) | kotlin-spring, a relative spec | `not_spring_server` |
| The plugin's own `samples/local-spec` (v7.4.0) | all 5 tasks, version from `gradle.properties` | kotlin, go and csharp are not read; the java client is read |

#### Generated Java clients

An execution with generator `java`, plugin version 4 or later and a library
whose API classes send each operation from a method named after it (the
default `okhttp-gson`, `native`, `resttemplate`, `webclient`, `restclient`,
`apache-httpclient`, `jersey2`, `jersey3`, `feign`) generates, per tag, the
class `apiPackage.<apiNamePrefix><Tag><apiNameSuffix>`. A call of
`<operation>`, `<operation>WithHttpInfo`, `<operation>Async` or
`<operation>WithHttpInfoAsync` on a name declared once in the file with that
class as its type, or on a `new` instance, is an `HttpRequest` with client
`openapi-java`. Its method and path are the operation's, and its
`declared_in` is the spec location. Its base URL is the spec's first server
URL as the generator reads it: server variables take their defaults,
`http://localhost` goes before a relative URL and is used when there is no
server, and Swagger 2 uses scheme, host and basePath. When any Java file of
the caller's module calls `setBasePath`, `setServers` or `setServerIndex`,
the base is unknown and only the path is matched, under the usual
assumption that the unknown base adds no path segments.

The request is resolved like any other. A server URL host that is one
service's `spring.application.name` narrows the candidates to that service.
The `REQUESTS` edge carries `declared_in` and one of
`service-contracts.openapi-client-service-path`, `openapi-client-path` or
`openapi-client-gateway` (pattern_matched). These mirror the HTTP rules and
also state the generated-client step. Libraries such as `rest-assured`,
`retrofit2`, `vertx` and `microprofile` are unread as
`openapi:client_library`, older plugins as `openapi:generator_version`, and
an unreadable configuration file as `openapi:config_not_read`.

Results on three repositories:

| Repository | Call sites found | Outcome |
|---|---|---|
| happyride (`17a507f`) | 1 of 1 in main code | POST `http://localhost:8501/` is ambiguous between five POST `/` routes in different services, so no link is made (the ports are only in dev-profile config) |
| spring-boot-api-first-approach (`ed987c1`) | 1 of 1 | Unresolved: the server is generated by plugin 6.5, which is not read |
| openapi-generator-maven-plugin-demo (`7a86bda`) | 1 of 1 | Linked to the right operation, using library `feign` from the configurationFile and the default apiPackage |

No link is wrong. spring-petclinic-rest still gives 36 routes.

#### Generated Feign clients

An execution with generator `spring`, library `spring-cloud`, plugin version
7 or later and `interfaceOnly` false generates, per group (as for Spring
servers), the interface `apiPackage.<Group><apiNameSuffix>` and the Feign
client `<that name>Client` that extends it (`SpringCodegen` and
`spring-cloud/apiClient.mustache`, checked at v7.4.0 and v7.16.0). A call of
`<operation>` on a name declared once in the file with the client as its
type is an `HttpRequest` with client `openapi-spring-cloud` and the spec
location as `declared_in`. The client's `@FeignClient` URL is the
`<api var>.url` property with the server URL as its default, or service
discovery by the `<api var>.name` property without `useFeignClientUrl`. Both
are run-time settings, so the base is unknown and only the path is matched.
The `REQUESTS` edge carries `service-contracts.openapi-feign-client-path` or
`openapi-feign-client-gateway` (pattern_matched). With `interfaceOnly` no
client is generated (`openapi:client_not_generated`). Calls on the plain
interface type are not read, because a server may implement it too.

| Repository | Call sites in main code | Outcome |
|---|---|---|
| TPE-TicketYa (`cdf3173`, 6 services, plugin 7.16) | 7 | 7 linked, each to the controller method of the operation (precision 7/7, recall 7/7) |
| oficina-agua-pagos (`0040a10`, plugin 7.21) | 2 | 2 requests found, unresolved: the bank and ERP servers are JSON mock definitions, not code in the repository |
| cf-kaizen (`449d8d0`, plugin 7.18) | 75 | 75 requests found, using generated operation names and `apiPackage` from `configOptions`. All are unresolved: the servers, cf-butler and cf-hoover, are other repositories |

On spring-petclinic-rest (commit `220f4ba8`), 36 of the spec's 37 operations
become routes, each on the controller method named by its operationId. The
37th, `failingRequest`, has no implementing controller and is reported as
not handled. The project's own MockMvc tests request 36 distinct method and
path pairs under `/api`, and they are exactly the 36 routes.

### OpenAPI Go clients (oapi-codegen)

oapi-codegen turns an OpenAPI 3 document into Go code. With `client` among
the kinds it generates, each operation gets a request builder,
`New<Operation>Request` (or `New<Operation>RequestWithBody` when the
operation has a body), which builds the request for the operation's method
and path under the server URL the client is given at run time. The builder's
own `http.NewRequest` call has an unknown URL, so the request is read from
the spec instead, through the `//go:generate` directive that runs the
generator (`service_contracts/oapi_codegen.py`):

- the program is `oapi-codegen`, `go tool oapi-codegen`, or `go run` of
  `github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen` or
  `github.com/deepmap/oapi-codegen[/v2]/cmd/oapi-codegen` at any version. A
  bare name counts as that generator only when the go.mod files it runs
  under pin one of those modules and no fork: for `go tool`, the nearest
  go.mod above the directive (or `-modfile`); for a program on `PATH`,
  every go.mod in the repository;
- `-config`, `-generate` and `-o` are read the way Go's flag package reads
  them, as are the config file's `generate`, `output` and
  `output-options.name-normalizer`. Paths are relative to the directive's
  directory, where `go generate` runs. Without `-generate` or a config
  `generate`, the generator's default kinds apply, and they include
  `client`;
- the spec is the first argument, a JSON or YAML file in the repository.

Each operation in the spec's `paths` is named the way the generator names it
(`ToCamelCase` of its operationId, or of the method and path segments joined
by `-`, with `N` before a name that starts with a digit). It is read only
where the generated output, which must carry the generator's `Code
generated by` header, declares that operation's builder. The request is an
`HttpRequest` with client `oapi-codegen` at the builder function, with
`declared_in` set to the spec location and an unknown base. The generic
`go-net-http` requests in that output file are dropped. Requests are
resolved like any other. The `REQUESTS` edge carries one of the
`service-contracts.oapi-codegen-client-*` rules (pattern_matched), which
mirror the HTTP rules and also state the generator step.

These are counted as unread under `oapi_codegen:<reason>`:

- `generator`: another generator or a fork, such as `oapi-codegen-dd`;
- `name_normalizer`: a name normalizer other than the default;
- `config_file`, `spec_file`, `output_file`: a file that is not in the
  repository, or not readable; output written to standard output also
  counts as `output_file`;
- `ref`: an operation in a `$ref` path item;
- `not_generated`: an operation that the output does not declare, such as
  one left out by tag or operationId filters.

Results on twelve repositories that use oapi-codegen. No existing edge
changed on the rest of the validation corpus.

| Repository | Requests read | Outcome |
|---|---|---|
| flightctl (`7d09c1b`) | 153 | 131 linked, each to the route the server generated from the same spec serves |
| databasus (`543545d`) | 121 | 117 linked to the module servers generated from the same specs |
| kubev2v/migration-planner (`d0c6361`) | 66 | 62 linked to the generated servers of the same specs |
| guacsec/guac (`e130b87`) | 11 | 11 linked to the chi strict server |
| 0xERR0R/blocky (`bb16004`) | 7 | 7 linked to the chi strict server |
| l7mp/stunner (`ea262a0`) | 4 | 4 linked to the generated server |
| MarioCarrion/todo-api-microservice-example | 5 | Unresolved: the server's routes take a base path from options, which is not read |
| sablier (`c4040f6`) | 0 | The directive generates types only |
| cvhariharan/flowctl (`288d9ae`) | 0 | `name_normalizer` (ToCamelCaseWithInitialisms) |
| kenn-io/roborev (`b83047d`) | 0 | `generator` (the oapi-codegen-dd fork) |
| sksmith/go-micro-example | 0 | The client is generated from a Makefile, not a go:generate directive, so it is not read |
| oapi-codegen itself (`977b0d4`) | 1549 | 2 linked; see below |

In the generator's own repository, the authenticated-api example clients
send `GET /things`. Their example servers are in the same Go module, so
under `http-path` they are not candidates (a request with an unknown base
goes to another program). The one other `GET /things` route belongs to an
unrelated test fixture, and both requests are linked to it. Those are the
only links made outside a real system, and they follow from `http-path`
itself, not from this reader.

### gRPC

The same tool reads gRPC contracts from `.proto` files and from Go, Python,
Java, JavaScript and C# sources (`deepthought.service_contracts.grpc`).

- **Methods.** Every `rpc` of a `service` in a `.proto` file (directories
  `node_modules`, `vendor` and hidden ones skipped) is a `GrpcMethod` node
  with labels
  `["GrpcMethod", "Contract"]` and qualified name
  `{repo}::grpc::{package}.{Service}/{Method}`. The identity is the fully
  qualified protobuf name, which is what goes on the wire, so the copies of
  one proto that each service vendors give one node; `declared_in` lists
  every `file:line`, and `file_path`/`line` name the first. Proto files are
  not part of the analysis coverage; their SHA-256 digests are recorded in
  the `proto_files` metric. `streaming` is `unary`, `client`, `server` or
  `bidi`, from `stream` on the request or response of the `rpc`. It is left
  out when the copies of the proto disagree.
- **Reading .proto files.** protoc reads them, from `grpcio-tools` (the
  `grpc` extra, also in `languages`;
  `deepthought.service_contracts.protoc`): each file compiles to a
  `FileDescriptorSet` with source info, and the services, rpcs, streaming
  kinds and the lines of the rpc names come from the descriptor. protoc
  needs the files a proto imports, under include roots the repository does
  not name: an import `a/b.proto` is matched against the repository's
  `.proto` paths, and what is left in front of it is a root; the roots the
  imported files' own imports need are added too, since protoc opens
  them as well (Envoy and xDS protos vendored in grpc-java import across
  five roots). A file that
  another file imports takes that importer's root as its own, else its
  directory; the well-known types come from `grpcio-tools`. Files under the
  same root compile in one run, split in halves when it fails, so a broken
  file costs only itself. protoc is the only reader of `.proto` files:
  `grpcio-tools` and `protobuf` are required, and without them the
  analysis fails with `ProtocMissing` instead of reading nothing. A file
  protoc does not compile (an import the repository and the include
  directories do not hold, or a syntax error) declares nothing and is
  counted as `grpc_proto:not_compiled`. The `proto_readers` metric counts
  compiled and rejected files. On the 441 `.proto` files of the 26 gRPC
  validation repositories, protoc compiles 437: three are proto-loader and
  reflection test inputs, and Prisma's gRPC example names an rpc and a
  message `Post`, which protoc rejects (protobuf.js loads it at run time),
  so that example's 14 server links are no longer drawn.
- **HTTP annotations.** An rpc's `google.api.http` option, read from its
  protoc descriptor (with `googleapis-common-protos` supplying
  `google/api/annotations.proto` when the repository does not vendor it),
  is an `HttpRoute` (framework `grpc-http-annotation`) per binding,
  `additional_bindings` included: grpc-gateway and Kratos generate an HTTP
  handler for each. `{x}` and `{x=*}` are variables, `{x=**}` the rest of
  the path, `{x=shelves/*}` its own segments, and a `:verb` suffix stays
  literal. The `GrpcMethod` `HANDLES` the route (rule
  `service-contracts.grpc-http-annotation-route`), so impact runs from the
  route to the rpc and on to its servers. The route is served by the Go
  files that call the generated `RegisterXHTTPServer` (Kratos) or
  `RegisterXHandler[FromEndpoint|Server|Client]` (grpc-gateway) of its
  service, which matters for loopback requests (below).
- **Loopback ports.** A request whose literal host is `localhost`,
  `127.0.0.1`, `0.0.0.0` or `[::1]` and whose literal port a program of the
  repository is configured to listen on goes to that program: only routes
  served from files under its directory are candidates (rule
  `service-contracts.http-local-port`), else it is unresolved as
  `no_route_on_port`. Ports come from Kratos configuration
  (`configs/*.yaml` with `server.http.addr: host:port`; the program is the
  directory holding `configs`). A port no program declares changes nothing.
  In go-kratos/beer-shop the admin frontend calls `localhost:8100`, the
  admin service; its stale `/v1/login` call is left unresolved instead of
  linking to the shop service's `/v1/login` on port 8000, while its
  `/admin/v1/...` calls link to the admin rpcs.
- **Servers.** Each rule ties a declaration to an rpc by `HANDLES`:

  | Rule | Server |
  |---|---|
  | `service-contracts.grpc-go-register` | `pb.RegisterXServer(s, impl)` where `impl` is `&T{}`, `new(T)` or a name bound once to one of those; the methods of `T` named as X's rpcs, declared in the same package directory |
  | `service-contracts.grpc-go-register-constructor` | `pb.RegisterXServer(s, f(...))` (or a name bound once to that call), where `f` is declared in one file of the same package directory and its only result is `T` or `*T` for a struct type `T` of that package; the methods of `T` as above |
  | `service-contracts.grpc-python-servicer` | methods named as X's rpcs of a class inheriting `XServicer`, or of a class of the same file inheriting such a class |
  | `service-contracts.grpc-java-impl-base` | methods named `lowerCamel(rpc)` of a class extending `XGrpc.XImplBase` |
  | `service-contracts.grpc-csharp-service-base` | methods named as X's rpcs of a C# class deriving from `X.XBase` (written with or without its namespace; Grpc.Tools nests `XBase` in the static class `X`), or of a class of the same file deriving from such a class |
  | `service-contracts.grpc-node-add-service` | `server.addService(pkg.X.service, {key: handler})`; `key` names the rpc (lowerCamel accepted) and `handler` is a function or `Class.method` (also `.bind(this)`) declared in the same file |
  | `service-contracts.grpc-node-static-add-service` | `server.addService(m.XService, {key: handler})` for grpc-tools static codegen: `m` is bound by `require`, a default or a namespace import to a `*_grpc_pb` module, or `XService` is imported by name from one; handlers as above |

  C# servers, against a lexical reference (classes written `: ....X.XBase`,
  their `override` methods named as an rpc of X in the repository's protos):
  grpc/grpc-dotnet `7c1a477` `examples` 49 of 49 handlers, none extra;
  online-boutique's cartservice 3 of 3, which takes its labelled servers to
  20 of 20 and service pairs to 14 of 14.

- **Clients.** `service-contracts.grpc-stub-call` links the calling
  declaration (or the file) by `REQUESTS` when a method is called on a stub of
  service X: built in place by its generated factory (`pb.NewXClient(conn)`,
  `pkg_pb2_grpc.XStub(channel)`, `XGrpc.newStub/newBlockingStub/newFutureStub`,
  `new pkg.X(addr, creds)`, and grpc-tools' `new m.XClient(addr, creds)` for a
  `*_grpc_pb` module `m` or an `XClient` imported from one), or a name
  (local, field or attribute) bound in
  the same file only to stubs of X. For JavaScript, where any `new a.B()`
  looks the same, `B` must be a service some proto declares. The call is
  placed at the method's name.

  C# stubs are values of the generated client type `X.XClient` (with or
  without its namespace, or through a `using A = ...;` alias):
  `new X.XClient(channel)`, a parameter (primary constructor parameters
  included), field, property or local declared with that type (`var` takes
  the type of a `new`), `this.x` for a field or property, or a call to a
  method of the same file whose return type is `X.XClient`. A name is looked
  up in the innermost enclosing member, function or type that declares it,
  so two locals named `client` in different methods can hold different
  services; a name declared there with another type, or with clients of
  two services, is not a stub. `MAsync` denotes rpc `M` when the proto has
  no rpc `MAsync`. Clients registered with `AddGrpcClient<X.XClient>` and
  injected into a constructor are read through that parameter's type.
  Clients of a `using static` import (`XClient` alone) are not read.

  C# clients, against a lexical reference (names assigned
  `new ...X.XClient(` or declared with that type, then `name.M(`):
  grpc-dotnet `7c1a477` 82 of 131 reference sites extracted plus 1 the
  reference missed; each of the 49 others is a `client` local assigned
  from `TestClientFactory.Create(...)`, which the reference's file-wide
  name match takes for a stub. 72 sites link; 11 are unresolved because
  their service (`Counter`, `Racer`, `ServerReflection`) is declared by several
  proto packages of the repository. dotnet/eShop `669682e`: the WebApp's
  injected `Basket.BasketClient` and the MAUI ClientApp's
  `GetBasketClient()` give 6 of 6 calls, linked to `BasketApi.Basket`.

A simple service name must belong to one proto package and the written
method must be one of its rpcs (exactly, or as the only lowerCamel match);
otherwise there is no edge, and File nodes list the call in
`unresolved_grpc_calls` (metrics count `grpc_call_resolution` by reason:
`no_proto_service`, `several_proto_packages`, `no_proto_method`). A server
that cannot be tied to a declaration is counted in `unread` as
`grpc_server:implementation_not_read`, `handler_not_declared` and so on.
Generated code (`*.pb.go`, `*_pb2_grpc.py`, `*_grpc_pb.js` and the like) is
neither server nor client; test source files (Go `_test.go`, Python
`test_*.py`, `src/test/` and the like) are clients only, their calls marked
`test_source`. The channel's target address is never read, so a stub links to
the method wherever it is served; interceptors, reflection and dynamically
loaded service definitions are not followed. Every edge is `pattern_matched`.

On Online Boutique (`evals/fixtures/service_contracts/online-boutique.json`,
20 labelled gRPC links, all direct; `--kind grpc` scores a call site by client
location and contract, and servers separately):

| Level | Predicted | Correct | Labelled | Precision | Recall |
|---|---|---|---|---|---|
| Call sites | 21 | 20 | 20 | 0.95 | 1.00 |
| Service pairs | 12 | 12 | 14 | 1.00 | 0.86 |
| Servers at labelled locations | | 15 | 20 | | 0.75 |

The five missed servers and both missed service pairs are `cartservice`,
written in C#. The one wrong call site is adservice's own developer client
(`AdServiceClient.java:66`), which the corpus marks as a don't-care site at
line 60, outside the 3-line tolerance. Four more
calls at don't-care sites (test clients inside currencyservice,
recommendationservice and emailservice) are linked and count for neither side.

Streaming rpcs link like unary ones in every language, since servers and
stubs name them the same way. On the route_guide examples of grpc-go
`9ad6d94`, grpc-java `6336333`, grpc (Python) `b7bc645` and grpc-node
`ca7f83f` (dynamic codegen), each of the 4 rpcs (one of each kind) is linked
to its server and its client call. Before the constructor rule, the Go server
was unread because it is registered as `newServer()`. Across all grpc-go
examples, the rule adds 6 correct `HANDLES` edges, from 2 registrations. The
3 registrations still unread are a library health server from another
package (2) and a loop variable (1).

grpc-tools static codegen (`*_grpc_pb.js`) is read too. In grpc-node's
`examples/` (`ca7f83f`), the 4 files that use it (route_guide and helloworld,
servers and clients) give 11 new edges: 5 `HANDLES` and 6 `REQUESTS`, all
correct. Destructured requires (`const {XClient} = require(...)`) are not
read.

### Message destinations

The same tool reads where code publishes messages and where it consumes them
(`deepthought.service_contracts.messaging`). Each destination is one
`MessageDestination` node (labels `MessageDestination`, `Contract`;
`protocol` is `kafka` or `amqp`), and publishers link to it by `PUBLISHES`
and consumers by `CONSUMES`, from the enclosing declaration (a listener
method is its own) or the file. Publishers and consumers of one destination
meet at its node: the link between them is the shared broker and name, not
a traced value, and consumer groups, brokers and clusters are not checked.

| Client | Publish | Consume |
|---|---|---|
| Spring Kafka | `KafkaTemplate.send(topic, ...)` or `send(new ProducerRecord<>(topic, ...))` on a name declared only as `KafkaTemplate` | `@KafkaListener(topics = ...)` |
| Spring AMQP | `convertAndSend`/`send(exchange, key, ...)`, or `(key, ...)` with the template's exchange | `@RabbitListener(queues = ...)` |
| kafka-python | `send(topic, ...)` on a name bound once to `KafkaProducer(...)` | `KafkaConsumer(topic, ...)` |
| pika | `basic_publish(exchange, routing_key)` | `basic_consume(queue)` |
| Go `amqp` (streadway, amqp091-go) | `Publish(exchange, key, ...)` | `Consume(queue, ...)` |
| kafkajs | `send({topic})` or `sendBatch({topicMessages: [{topic}]})` on a name every assignment binds to `producer()` of a kafkajs `Kafka` | `subscribe({topic})` or `subscribe({topics: [...]})` on a `consumer()` of one |
| amqplib (promise and callback APIs) | `publish(exchange, key, ...)` or `sendToQueue(queue, ...)` on a channel | `consume(queue, ...)` on a channel |
| segmentio/kafka-go | a `Writer`, `WriterConfig` or `Message` literal with `Topic` | a `ReaderConfig` literal with `Topic` or `GroupTopics` |
| sarama (IBM, Shopify) | a `ProducerMessage` literal with `Topic` | `ConsumePartition(topic, ...)`, or a group's `Consume(ctx, []string{...}, handler)` |
| confluent-kafka-go | a `TopicPartition` literal with `Topic` (`&name` is read as `name`) | `Subscribe(topic, ...)`, `SubscribeTopics([]string{...}, ...)` |

Each publish rule is `service-contracts.<client>-send`/`-publish` and each
consume rule `-listener`/`-consumer`/`-consume`; all are pattern_matched.

An AMQP destination is `exchange/routing-key`. A consumed queue `q` receives
`/q` (every broker binds each queue to the default exchange by its name) and
`x/k` for each `queue_bind`/`QueueBind(q, k, x)`/`bindQueue(q, x, k)` in
the same file; a Go
queue may be named by `q.Name` of a `q, err := ch.QueueDeclare("q", ...)`.
Binding keys with `*` or `#` (topic exchange patterns) are counted as unread,
not matched. A Spring template given only a routing key sends to
`spring.rabbitmq.template.exchange` from the module's configuration, or to
the default exchange when that key is not set; a module whose Java code calls
`setExchange` has no fixed template exchange.

Destinations are folded like URLs. In addition, Java names a constant of
another class (`Queues.queueName`) through the file's imports or package
when exactly one file declares that class with a `static final String` the
file fixes, and Python reads `self.NAME` for a class attribute assigned
once in the class body and never through an attribute assignment. A
destination with unknown text is counted as unread.

In Node, the `Kafka` class must be imported from `kafkajs`, by `import` or
by a `require` (destructured too). An amqplib channel is a name, in a file
that imports `amqplib` or `amqplib/callback_api`, of one of these kinds:

- a name that every assignment binds to `createChannel()` or
  `createConfirmChannel()`, awaited or not;
- the channel parameter of the callback of either call;
- the parameter of its `.then`.

A channel passed in from another file is not read. A kafkajs topic given as
a regular expression is `consume:topic_pattern`. A `Kafka` may also be a
name typed `Kafka` (a parameter `kafka: Kafka`, say).

#### Values passed to a wrapper function

Services often wrap a send, a subscribe or an HTTP request in a function that
takes the destination or URL as a parameter (`publishEvent(topic, payload)`,
`api(path, options)`), so it is fixed only where the function is called
(`script_constants.ScriptCalls`, `fold_through_calls`). This covers every
JavaScript/TypeScript messaging client (kafkajs, amqplib, the STOMP clients,
NestJS `ClientProxy`) and every JavaScript/TypeScript HTTP client site
(fetch, axios, superagent, request, `$http`, Angular `HttpClient` and
`httpResource`, WebSocket and SockJS handshakes). When the destination, URL
or method at a site is built from parameters of the module-level function
around it (a `function` declaration, or a top-level `const` whose value is
the function) that the function never assigns again, the site takes,
instead, each value a call of that function passes:

- **Calls.** Calls in the function's own module, and in modules that import
  it by name (`import { publishEvent as send } from './broker'`) or as a
  namespace (`ns.publishEvent(...)`), resolved as for constants across
  modules. A local name that shadows the function is not a call of it. A
  class method (not static, not an accessor) takes the calls
  `this.method(...)` in its own class body, where `this` is the instance:
  directly in a method or in an arrow function, not in a plain `function`
  or a nested class (`private getProducts(url)` of an Angular service). A
  function reached any other way (a subclass or another object calling the
  method, a default-exported object's member, a value passed around) is not
  followed.
- **Values.** Each argument is folded in the calling module, with its own
  constants; arguments after a spread are unknown. An argument the call
  leaves out takes the parameter's default (`method = 'GET'`). An object
  literal argument with fixed keys gives its properties to `options.key`,
  and `options.key ?? fallback` (or `||`) takes the fallback when the
  literal has no such key (`api(path, { method: 'POST' })`,
  `options.method ?? 'GET'`).
- **Edges.** One site is added per distinct value, with `argument_from`
  listing the calls (`file:line`) that give it. A message site takes rule
  `service-contracts.destination-argument` (its own rule is kept as
  `site_rule`); an HTTP request keeps its path-matching rule and gains
  `argument_from`. A call that leaves the value as unknown as before is not
  listed; a call passing unknown text adds an unread site, and a function
  that no call reaches keeps its site unread as before.

| Repository | Wrapper calls | Outcome |
|---|---|---|
| 19EB/microservices-base-kafka `9facb7f` (labelled corpus) | 6 `KafkaPublish(kafka, 'x-topic', ...)` in 3 services | 6 of 6 labelled links, none wrong (none before) |
| simbolmina/nodejs-microservices-demo `7734f36` | 8 `publishEvent('...')` in the same module | 6 topics read from the 8 calls, all correct. A 9th call through a default-imported object passes a parameter, so it would be unknown anyway |
| Rabin18Glan/gantabya `f90b2bc` | 2 `produceEvent('trip-events', ...)` in a controller | The send takes `trip-events` and meets the consumer |
| Yash-op7/Distributed-Payments-Processing-System `8e4a6f5` | the only call is commented out | Unread, as before |
| Dario213987/TPE-TicketYa `cdf3173` (front end) | 36 `api<T>(path, { method, body })` calls of one fetch helper | 32 requests linked to the right Spring route and method, all checked by hand (none before). The other 4 append a query string built at run time and stay unresolved |
| darbyluv2code/fullstack `e8832ee` (45 copies of an Angular `ProductService`) | `this.getProducts(searchUrl)` twice per copy | Each copy's `GET {?}` becomes the two Spring Data REST search URLs (`/api/products/search/findByCategoryId?id={?}`, `.../findByNameContaining?name={?}`), 90 in all, every one the URL the caller builds. They stay unresolved because Spring Data REST repository endpoints are not read yet |

The Go Kafka clients are read in files that import them. A producer or
consumer is placed where the literal or call is written, which is not always
where messages are sent: a kafka-go `Writer` built in a constructor publishes
from that constructor. A confluent-kafka-go topic that starts with `^` is a
pattern (`consume:topic_pattern`). Kafka has no empty topic, so a topic that
folds to `""` (a package variable that a flag sets at run time, say) is
`empty_topic` for every client.

| Repository | Literal sites | Outcome |
|---|---|---|
| rabbitmq/rabbitmq-tutorials `6deb41d` `javascript-nodejs` (callback API) | 14 publish and consume calls | 8 read, and `hello`, `task_queue` and `rpc_queue` meet. The 6 unread take the routing key from arguments, reply to `msg.properties.replyTo`, or consume a server-named queue (`q.queue`) |
| redpanda-data/redpanda-labs `77f0ca6` | 3 sends, 2 subscribes | The chat-room producers and consumers (docker and cloud variants) meet on `chat-room`, and the OIDC client's send is found. The 2 sites with a topic variable are unread |
| n8n-io/n8n `9254759` Kafka and RabbitMQ nodes | topics and channels from node parameters | Nothing linked, which is correct |
| tulios/kafkajs `55b0b41` `examples` | | Not read: they import the library from `../index` |

No edge is wrong. On the corpora, nothing changes.

| Go repository (`examples/`) | Outcome |
|---|---|
| segmentio/kafka-go `2e0b396` | 4 sites, all with topics from environment variables, so unread |
| IBM/sarama `66eb733` | 2 `ProducerMessage` topics read, both correct. 2 flag-set topics are `empty_topic` and 6 are not literal |
| confluentinc/confluent-kafka-go `eb0eb14` | 6 sites read (5 produces, 1 subscribe), all correct. 40 take their topic from arguments or flags and are unread |

No Go edge is wrong. On the corpora, nothing changes.

#### NestJS microservices

NestJS microservice patterns are read with broker `nestjs`:

- **Consumers.** A method of a class decorated `@Controller` (from
  `@nestjs/common`) that carries `@MessagePattern(pattern)` or
  `@EventPattern(pattern)` from `@nestjs/microservices` (rule
  `service-contracts.nestjs-message-pattern`).
- **Publishers.** `send(pattern, ...)` (a message) and `emit(pattern, ...)`
  (an event), on a name the file declares only with a client type from
  `@nestjs/microservices`: `ClientProxy`, `ClientKafka`, `ClientRMQ`,
  `ClientNats`, `ClientRedis`, `ClientMqtt` or `ClientTCP` (rule
  `service-contracts.nestjs-client-send`). The name can be a field, a
  constructor parameter property such as `private client: ClientProxy`, or a
  local.

The destination is `message:<route>` or `event:<route>`. Nest matches messages
and events separately, so `emit` never reaches a `@MessagePattern`. The route
is the pattern as Nest's `transformPatternToRoute` writes it:

- A string or an integer is kept as written.
- An object becomes `{"key":value,...}`, with string values quoted and `"`
  and `\` escaped. A `const` object declared once in the file counts as
  written in place.
- Nest sorts keys with `localeCompare`. Only keys of ASCII letters and
  digits, whose order does not depend on case, are read, because that is
  where code-point order is known to agree. Other keys are
  `destination_not_literal`.

The transport (TCP, Kafka, RabbitMQ and so on) is configured elsewhere and is
not checked.

| Repository | Sites | Outcome |
|---|---|---|
| Denrox/nestjs-microservices-example `112c1d3` | 13 patterns, 15 client sends | All 28 read, and every one of the 13 patterns has a sender and a handler. The gateway reaches the user, task, token and permission services, and the user service reaches the mailer. supertest's `.send()` in the e2e tests is not read |
| nestjs/nest `sample/03-microservices` | 1 pattern, 1 send | The send (a `const pattern = { cmd: 'sum' }`) meets its handler |

No edge is wrong. On the corpora, nothing changes.

### STOMP destinations

STOMP over WebSocket is read as messaging with broker `stomp`. A destination
is the full STOMP destination a client names, and publishers and consumers
meet at the `MessageDestination` of the same name:

- **Consumers (Spring).** A method with `@MessageMapping` (from
  `org.springframework.messaging.handler.annotation`) consumes each literal
  path. The path is joined with the class's `@MessageMapping` path as
  `AntPathMatcher.combine` joins slash paths, then put under each prefix that
  the module's `MessageBrokerRegistry.setApplicationDestinationPrefixes`
  sets (rule `service-contracts.spring-stomp-message-mapping`). A module that
  sets no prefix uses the path itself. When the prefixes are not literal or
  are set in more than one file, the mapping is `consume:application_prefix`.
  A path that does not start with a slash is `consume:destination_not_literal`.
- **Publishers (Spring).** `@SendTo` destinations, `@SubscribeMapping`
  methods (they answer a subscription to the prefixed path), and
  `convertAndSend(destination, ...)` on a name declared only as
  `SimpMessagingTemplate`, `SimpMessageSendingOperations` or
  `SimpMessageOperations` (rule `service-contracts.spring-stomp-send`).
  `@SendToUser` and `convertAndSendToUser` are `publish:user_destination`. A
  `@MessageMapping` method's default reply destination, used without
  `@SendTo`, is not read.
- **Clients (JavaScript/TypeScript)** (`deepthought.service_contracts.stomp_clients`).
  The call must be on a name (`x`, or `this.x` for a class member) that
  every declaration and assignment in the file gives the same client kind.
  A value gives a kind when it is a factory call or `new` of the client
  class. In TypeScript a declaration typed with the class also gives the
  kind: a variable or field without a value, a parameter (and `this.x` for
  a constructor parameter property), or a getter's return type.
  - `stompjs`: `Stomp.over`, `Stomp.client`, `webstomp.over`/
    `webstomp.client`, a `@stomp/stompjs` `Client`, the browser bundle's
    global `StompJs.Client` (when the file does not bind `StompJs`), or
    AngularJS ngStomp's `$stomp`. `send` and `subscribe` with a destination
    first, and `publish({destination})` (rules
    `service-contracts.stompjs-send` and `stompjs-subscribe`).
  - `rx-stomp`: a `@stomp/rx-stomp` `RxStomp` (or the global
    `RxStomp.RxStomp`). `publish({destination})` and `watch(destination)`
    or `watch({destination})` (rules `service-contracts.rx-stomp-publish`
    and `rx-stomp-watch`).
  - `ng2-stomp-service`: an ng2-stomp-service `StompService`, usually an
    injected constructor parameter. `send(destination, body)` and
    `subscribe(destination, callback)` (rules
    `service-contracts.ng2-stomp-service-send` and
    `ng2-stomp-service-subscribe`).

Destination patterns with `{variables}` are kept as written. A client
destination with unknown text is not read, so it never meets them.

| Repository | Literal STOMP sites | Outcome |
|---|---|---|
| mark-dev/chessfactory `b54acdb` | 14 server mappings (2 prefixes), 10 client sends and subscribes | 9 of the 10 client sites meet their handler or publisher. The 10th subscribes to `/user/queue/chesslobby`, whose publisher is a `convertAndSendToUser` (unread). The 7 client sites built from variables are unread |
| ben-rampel/Poker `025a147` | 1 mapping, 1 ngStomp send | Linked. The topic is built from a variable on both sides, so it is unread |
| spring-guides/gs-messaging-stomp-websocket `45228a1` (labelled corpus) | 1 mapping, 1 `@SendTo`, `StompJs.Client` bundle global | Handshake, send and subscribe all link: 3 of 3 labelled links, none wrong (none before) |
| chriswhite199/spring-boot-angular2-stomp `78ade07` (labelled corpus) | 1 mapping, 1 `@SendTo`, injected ng2-stomp-service | 3 of 3 labelled links, none wrong (none before) |
| jhipster/jhipster-sample-app-websocket `410a632` | 1 mapping, 2 publishers, RxStomp behind a typed getter | `watch('/topic/tracker')` meets both server publishers and `publish('/topic/activity')` meets the mapping. The SockJS handshake URL goes through `Location.prepareExternalUrl`, so it is unknown |
| leansd/cotrip, ygunayer/realtime-messaging | 1 mapping + 1 `@SendTo` each | Found; the clients are in HTML |
| ocinpp/springboot-sockjs-stomp-vue-sample | 2 `convertAndSend` | Found; the client is in HTML |
| apssouza22/java-microservice (corpus) | 1 mapping, 1 `@SendTo`, 1 `convertAndSend` | Found; its messaging ground-truth score is unchanged |

No meeting is wrong. On the other corpora, apssouza22's 3 edges are the only
change.

### GraphQL subscription pubsub

GraphQL subscriptions are usually fed through a pubsub object: a mutation
publishes to a topic, and the subscription resolver returns an iterator over
that topic. These are read as messaging:

- **In-memory `PubSub`.** The class comes from `graphql-subscriptions`, or the
  copy that `apollo-server`, `apollo-server-express`, `graphql-yoga` or
  `@nestjs/graphql` re-exports. `publish(topic, ...)` publishes (rule
  `service-contracts.graphql-pubsub-publish`). `asyncIterator(topic)`,
  `asyncIterableIterator(topic)` and `subscribe(topic, ...)` consume, and an
  array of topics names each one (rule `graphql-pubsub-subscribe`). The
  broker is `graphql-pubsub`. An in-memory PubSub delivers only to listeners
  on the same instance, so the destination is `<file>#<name>/<topic>`, and two
  PubSub objects never meet even when their topics match.
- **`RedisPubSub`** from `graphql-redis-subscriptions` uses the same calls.
  Its destination is the Redis channel, with broker `redis`, so publishers and
  subscribers in different services meet (rules `graphql-redis-pubsub-publish`
  and `graphql-redis-pubsub-subscribe`).

The call must be on a name (`x` or a class field `this.x`) that every
assignment in the file binds to a `new` of one of these imported classes. A
topic with unknown text is `publish:destination_not_literal` or
`consume:destination_not_literal`. These are not read:

- a PubSub that is created in one file and imported or passed in elsewhere,
  such as `export default new PubSub()` or `context.pubsub`;
- a PubSub injected by Nest's dependency injection;
- other pubsub APIs, such as mercurius's `publish({topic, payload})`.

| Repository | Literal pubsub sites | Outcome |
|---|---|---|
| notiz-dev/nestjs-prisma-starter `225e5a9` (corpus) | 1 publish, 1 subscribe | Linked |
| nestjs/nest `fed38a7` samples 12, 22 and 23 | 3 publishes, 3 subscribes | All 3 pairs linked |
| nestjs/nest sample 33 (mercurius) | 1 publish | Not read: a different API |
| the-road-to-graphql/fullstack-apollo-express-postgresql-boilerplate `77875c6` | 1 publish, 1 subscribe | Not read: the PubSub is a default export from another file |
| howtographql/graphql-js (corpus) `e0a3a21` | 2 publishes, 2 subscribes | Not read: they go through `context.pubsub` |
| davidyaha/graphql-redis-subscriptions `537c79d` | none (the library itself) | Nothing read, which is correct |

No edge is wrong. Of the 15 literal sites, 8 are read and all 8 are linked
correctly. Of the others, 6 use a PubSub passed in from another file and 1 is
mercurius. Across all corpora, nestjs-prisma-starter's 2 edges are the only
change.

### Spring Cloud Stream

Spring Cloud Stream code names a binding and each service's configuration
names its destination (`deepthought.service_contracts.cloud_stream`). Read
sites, in Java:

- consume: a `@StreamListener` method whose value or `target` names a
  binding (`Sink.INPUT` and `Processor.INPUT` are `input`);
- publish: `send(...)` on `x.output()` where `x` is declared only as
  `Source` or `Processor`, or on a name assigned only such a call; `@SendTo`
  on a stream listener; `StreamBridge.send("binding", ...)`.

The code runs in its own Maven module when that module names a service
(`spring.application.name`), else in every repository module whose `pom.xml`
depends on that module's artifact and names a service. Each such service
gives one endpoint: its destination is
`spring.cloud.stream.bindings.<binding>.destination` from the service's
configuration (the config-server sources included), or the binding name when
the key is not set. The broker is Kafka or RabbitMQ by the
`spring-cloud-stream-binder-*` (or `spring-cloud-starter-stream-*`)
dependency of the service module or a repository module it depends on. The
Kafka binder's destination is a topic; the RabbitMQ binder publishes to a
topic exchange named by the destination and binds each consumer group's
queue to it with `#`, so both sides meet at `destination/#`. Rules are
`service-contracts.spring-cloud-stream-output` and `-listener`.

Unread, by reason: `no_target` (a listener bound by `@Input` parameters),
`binding_not_literal`, `no_service`, `binder_unknown` (neither binder or
both) and `destination_not_literal` (a placeholder in the configured
destination).

The functional style is read too. A `@Bean` method (from
`org.springframework.context.annotation`) that returns a
`java.util.function` type is a binding site:

- `Consumer<T>` consumes `<bean>-in-0`, and `BiConsumer` also consumes
  `<bean>-in-1`;
- `Supplier<T>` publishes `<bean>-out-0`;
- `Function<T, R>` does both, and `BiFunction` consumes two inputs.

The bean is the method name, or the literal `name`/`value` of `@Bean`. It is
bound in a service that runs it when the service's
`spring.cloud.function.definition` (or the older
`spring.cloud.stream.function.definition`) lists it. Without that key, it
is bound only when it is the service's only functional bean, as Spring
Cloud Stream does. `spring.cloud.stream.function.bindings.<binding>` renames
a binding before its destination is looked up as above. Rules are
`service-contracts.spring-cloud-function-input` and `-output`. Further
unread reasons:

- `function_not_bound`: not in the definition, or one of several beans
  without one;
- `function_composed`: part of a composition such as `a|b`;
- `function_outputs`: a `KStream[]` result, whose branch bindings are not
  read;
- `function_curried`: a function returning a function;
- `function_definition_not_literal`, and `bean_name` for a `@Bean` name
  that is not a literal.

A functional bean in a module without a stream binder binds nothing and is
not counted. When the build that would show a binder is not fully read,
for example a Gradle `project(':x')` the settings do not lead to, the bean
is `build_not_read`. A bean in a stream module that no service runs (no
`spring.application.name`) is `no_service`, like any other site.

Gradle builds are read like Maven ones. A `build.gradle(.kts)` without a
`pom.xml` gives its quoted `group:artifact` coordinates and its
`project(':path')` dependencies, resolved from the nearest
`settings.gradle`; comments are skipped.

A service with both binders uses, for each binding, the binder named by
`spring.cloud.stream.bindings.<binding>.binder` or else by
`spring.cloud.stream.default-binder` (`defaultBinder`). These are read only
from configuration documents that activate on no profile, and a name is
mapped to its broker by `spring.cloud.stream.binders.<name>.type` or the
standard names (`rabbit`, `kafka`, `kstream`). Without such a name the
site is `binder_unknown`. This applies to every Spring Cloud Stream site,
not only the functional ones.

Checked on `spring-cloud/spring-cloud-stream-samples` (commit `2ff1168`):
I labelled by hand every binding of the nine function-based applications
that name a service (`spring.application.name`), 24 in all. The 19 read
all have the configured destination. The other 5 are unread, not wrong:

- the three branch outputs of `kafka-streams-branching`, one
  `function_outputs` fact;
- two bindings of `kafka-streams-to-rabbitmq`, which uses both binders.
  The other three name their binder per binding and are read. The
  remaining `process` function names none, so it is `binder_unknown`.

Samples without a `spring.application.name` are not services here and give
nothing.

A `StreamBridge.send(binding, ...)` whose binding is a parameter of a
`private` method takes the literal value of the argument that each direct
call in the file passes (`m(...)` or `this.m(...)`, inside lambdas too). The
send site gets one binding per distinct value, and a caller's own parameter
is followed the same way, at most three methods deep. The binding stays
`binding_not_literal` when the method:
- is not private;
- assigns the parameter, or declares a local that shadows it;
- is used as a method reference;
- has an overload of another arity;
- is never called;
- gets any argument that is not literal.

On the Packt chapters this resolves product-composite's `sendMessage("products-out-0", ...)`
helper. With the Compose identity below, `contracts-packt-chapter07` and
`contracts-packt-chapter23` each score 3/3 labelled links with no false ones.

#### Services that Compose builds

A Compose service whose `build` context is a module's directory runs that
module (`deepthought.service_contracts.compose`). Each Compose file is a
deployment of its own, and every file name of the `compose*.yml` /
`docker-compose*.yml` family counts, `docker-compose-kafka.yml` included.
Such a module is a running service even without its own
`spring.application.name`, and its name is, in this order:

1. its own `spring.application.name`;
2. the name every Compose service that builds it gives it:
   `SPRING_APPLICATION_NAME`, or `spring.application.name` read under the
   service's active profiles in its `SPRING_CONFIG_LOCATION` files;
3. the Compose services' name, when they all have the same one;
4. the module's directory name, when they have several (for example the
   replicas `product` and `product-p1`).

Message edges of Spring Cloud Stream sites record which step applied as
`service_identity_rule`: `service-contracts.service-spring-application-name`,
`service-compose-name` or `service-module-name`. They also record the
Compose services that deploy them as `deployed_as` (`file:service`).

`SPRING_CONFIG_LOCATION` entries are followed to repository files.
- A `file:` entry goes through the service's bind mounts. A relative
  source is relative to the Compose file, and `$PWD` is taken to be the
  Compose file's directory.
- A `classpath:` entry is looked up in the module's resources.
- An `optional:` entry that does not exist is skipped.

When every Compose service that builds the module sets a location, the
module's own config files are not read, as at run time.

The binder keys are then read per Compose service, with its
`SPRING_PROFILES_ACTIVE`: the last document that applies decides. So a
binding gives one endpoint per broker its deployments use, each with its
own `deployed_as`. A service is `binder_unknown` when:
- its profiles or location are not literal;
- a profile expression decides the value;
- its location cannot be followed.

Environment overrides of other keys are not read.

On PacktPublishing/Microservices-with-Spring-Boot-and-Spring-Cloud-Fourth-Edition (commit `db9e0fa`)
(Chapter07 and Chapter23), the three core services had been `no_service`,
since neither chapter sets a name in the module config. Each now consumes
its destination twice:
- on RabbitMQ, in `docker-compose.yml` and `docker-compose-partitions.yml`;
- on Kafka, in `docker-compose-kafka.yml`, whose `kafka` profile switches
  the default binder.

Chapter23 takes the names (`product`, `recommendation`, `review`) and
destinations from `config-repo/` through the mounted config location.
Chapter07 has replicas under two names, so its services are named after
their module directories. No edges changed on the gated corpora,
spring-petclinic-rest, train-ticket or the stream samples, apart from the
two new properties.

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
| Online Boutique (Locust to Go gorilla/mux under a `baseUrl` variable) | 9 | 9 | 9 | 0 |
| robot-shop (Express `request`, Python `requests` with `.format`) | 6 | 4 | 4 | 2: shipping's Apache HttpClient call takes its whole URL from a constructor argument, and ratings is PHP |

No prediction was wrong. gRPC links are scored separately. Online
Boutique's frontend calls the shopping assistant at `"http://" + addr`, which
fixes no path, so it stays unresolved.

Links through a gateway (the `all` score, which adds labelled browser and
load-test calls through the gateway or proxy):

| Corpus | Labelled HTTP links | Predicted | Correct | Missed |
|---|---|---|---|---|
| robot-shop (AngularJS and Locust through nginx) | 42 | 36 | 36 | 6: ratings is PHP, and shipping's HttpClient URL |
| spring-petclinic-microservices (AngularJS through Spring Cloud Gateway) | 14 | 13 | 13 | 1: the WebClient base URL with a setter |

Again no prediction was wrong. On train-ticket, 32 AngularJS requests of the
dashboard link through its Spring Cloud Gateway; a sample of ten was checked
by hand against the gateway routes and controllers.

Labelled message links (`--kind messaging`):

| Corpus | Labelled direct links | Predicted | Correct | Missed |
|---|---|---|---|---|
| ewolff/microservice-kafka (Spring Kafka, two consumers) | 2 | 2 | 2 | 0 |
| robot-shop (pika publish to Go `amqp` consumer) | 1 | 1 | 1 | 0 |
| apssouza22/java-microservice (Spring Cloud Stream, RabbitMQ binder, library publisher) | 1 | 1 | 1 | 0 |

Train-ticket has no labelled messages; its four publish-to-consume pairs
(food to delivery on `food_delivery`, and preserve, preserve-other and
notification itself on `email`, each through a `Queues` constant of its
own service) were checked by hand against the constants and listeners.

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

## Database tables

Neither the original engine nor the port linked code to the database tables
it uses. The `data-contracts` tool (`deepthought-graph index --tool
tree-sitter --tool data-contracts --language java --language python --language typescript --language go --language ruby --language php --snapshot
REV`) records
tables as `["DbTable", "Contract"]` nodes, so they join impact and the
component roll-up like HTTP routes and message destinations:

| Edge | Rule | Evidence | Read from |
|---|---|---|---|
| File -[:DECLARES]-> DbTable | `data-contracts.sql-create-table` | resolved | `CREATE TABLE` in a `.sql` file, parsed by sqlglot (PostgreSQL, MySQL, SQLite, T-SQL, Oracle, ClickHouse, then generic) one statement at a time, split by sqlglot's tokenizer; temporary tables, comments, strings and function bodies skipped. A CREATE TABLE no dialect parses is read from sqlglot's tokens (name after the modifiers, `TABLE` and `IF NOT EXISTS`) and counted as `tokens_not_parsed` in the `ddl_readers` metric |
| Symbol -[:MAPS_TO]-> DbTable | `data-contracts.jpa-table-annotation` | pattern_matched | a JPA `@Entity` (jakarta or javax.persistence) with `@Table(name = ...)`, folded like contract strings |
| Symbol -[:MAPS_TO]-> DbTable | `data-contracts.jpa-default-table-name` | name_matched | an `@Entity` without a table name, named by Spring Boot's default physical naming (`LineItem` is `line_item`) |
| Symbol -[:MAPS_TO]-> DbTable | `data-contracts.jpa-join-table` | pattern_matched | a field or getter of an entity with `@JoinTable(name = ...)` or `@CollectionTable(name = ...)`; the edge names the member |
| Symbol -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.spring-data-repository` | pattern_matched | an interface extending a Spring Data CRUD repository (READS and WRITES) or paging-and-sorting repository (READS) whose entity type argument is a mapped entity |
| Symbol -[:MAPS_TO]-> DbTable | `data-contracts.django-db-table` | pattern_matched | a Django model (a `django.db.models.Model` base, directly or through repository models named by unique class name) whose own `Meta.db_table` names the table |
| Symbol -[:MAPS_TO]-> DbTable | `data-contracts.django-default-table-name` | pattern_matched | a concrete model without `db_table`: `{app_label}_{class name lower case}`, the app label being `Meta.app_label`, else the literal `label` of the app's `AppConfig` in `apps.py`, else the package holding the models module; a proxy uses its concrete base's table, an abstract model has none |
| Symbol -[:MAPS_TO]-> DbTable | `data-contracts.django-many-to-many-table` | pattern_matched | a `ManyToManyField` without `through` on the model or an abstract model it extends: its `db_table`, else `{model table}_{field}`; the edge names the field |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.django-manager-call` | pattern_matched | a `Model.objects...` call chain with a reading (`filter`, `get`, `all`, `count`, ...) or writing (`create`, `update`, `delete`, `bulk_create`, ...) QuerySet method, `Model` being the one model of the repository with that class name; instance `save()`/`delete()`, other managers and raw SQL are not read |
| Symbol -[:MAPS_TO]-> DbTable | `data-contracts.sqlalchemy-tablename` | pattern_matched | a class in a module importing SQLAlchemy, Flask-SQLAlchemy or SQLModel that sets `__tablename__` (plain or annotated) to a string literal |
| File -[:DECLARES]-> DbTable | `data-contracts.sqlalchemy-table` | pattern_matched | a call of SQLAlchemy's `Table` (imported from `sqlalchemy`, or through an imported SQLAlchemy module such as `sa.Table` or `db.Table`) with a literal name |
| Symbol\|File -[:READS]-> DbTable | `data-contracts.sqlalchemy-session-query` | pattern_matched | `.query(Model)` or `.query(Model.column)`, `Model` being the one mapped class of the repository with that name; the receiver is not checked to be a session |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.sqlalchemy-statement` | pattern_matched | SQLAlchemy's `select` (READS) or `insert`, `update`, `delete` (WRITES) on a mapped class |
| Symbol -[:MAPS_TO]-> DbTable | `data-contracts.sqlmodel-default-table` | pattern_matched | a class with the `table=True` class keyword and no `__tablename__` that inherits from `sqlmodel.SQLModel` (bases followed through a SCIP index, or by name in the same file): SQLModel's default name, the class name lowercased |
| Symbol\|File -[:READS]-> DbTable | `data-contracts.flask-sqlalchemy-model-query` | pattern_matched | Flask-SQLAlchemy's `Model.query` (an attribute read, not a call) on the one mapped class of the repository with that name |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.sqlalchemy-scip-query`, `-scip-statement`, `-scip-model-query`, `-scip-get` | resolved | the same `.query(Model)`, statement and `Model.query` forms, and `.get(Model, key)` or `.select_from(Model)`, where a scip-python index resolves the written name to a mapped class; they replace the name rules in the files the index covers |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.sqlalchemy-scip-instance` | resolved | `session.add(x)`, `delete(x)`, `merge(x)` (WRITES) or `refresh(x)` (READS) with one variable argument, where the scip-python index resolves the method to SQLAlchemy's `Session`, `scoped_session`, `AsyncSession` or `async_scoped_session` and `x` is a mapped class: the type scip-python documents for the local, else its declared type (a parameter's or variable's annotation, through `Annotated`, `Optional`, `T \| None` and module-level aliases, the return annotation of the function it is assigned from, or `Session.get(Model, ...)`), every name resolved through the index; there is no name rule for it |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.spring-data-query` | pattern_matched | a method's Spring Data `@Query`: JPQL entity names resolve to the one entity with that name (`@Entity(name)` or class name), `nativeQuery = true` SQL names tables |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.jpa-query` | pattern_matched | `createQuery` (JPQL) or `createNativeQuery` (SQL) text in a file importing `jakarta.persistence` or `javax.persistence` |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.jdbc-sql` | pattern_matched | SQL passed to `JdbcClient.sql` or a JdbcTemplate `query*`, `update`, `batchUpdate` or `execute` call on a field or local, in a file importing `org.springframework.jdbc` |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.jpa-entity-manager` | pattern_matched | on a variable or field declared as a jakarta or javax `EntityManager`: `persist`, `merge` or `remove` (WRITES) of a parameter, local or field declared as a mapped entity, or `find`/`getReference` (READS) of its class literal |
| Symbol\|File -[:WRITES]-> DbTable | `data-contracts.jdbc-insert-table` | pattern_matched | `withTableName(...)` of a `SimpleJdbcInsert` in such a file |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.mybatis-mapper-xml` | pattern_matched | a `<select>`, `<insert>`, `<update>` or `<delete>` of a MyBatis mapper XML file (root `<mapper namespace>`), with every dynamic branch (`<if>`, `<choose>`, `<foreach>`, `<where>`, `<set>`, `<trim>`) kept, `<sql>` fragments of the same mapper included and `<selectKey>` left out; `#{}` is a bind marker and `${}` text the code does not fix; the edge starts at the mapper interface method that the namespace and id name, else at the XML file |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.mybatis-annotation` | pattern_matched | a mapper method's `@Select`, `@Insert`, `@Update` or `@Delete` (`org.apache.ibatis.annotations`), array strings joined with spaces and a `<script>` value read as mapper XML |
| Symbol -[:MAPS_TO]-> DbTable | `data-contracts.gorm-table-name` | pattern_matched | a Go type of the same package whose `TableName() string` method returns a string literal (GORM's `Tabler`), in a file importing a Go SQL package |
| Symbol -[:MAPS_TO]-> DbTable | `data-contracts.gorm-default-table-name` | name_matched | a GORM model without `TableName`, that is a struct embedding `gorm.Model` or passed as `&T{}` to `AutoMigrate` or `Model` in a file importing `gorm.io/gorm`: GORM 1.25's `NamingStrategy` name, the snake case of the type name (common initialisms such as `ID`, `HTTP` kept together) pluralized by jinzhu/inflection 1.0.0 (`UserModel` is `user_models`, `Human` is `humen`); none when the repository configures a `NamingStrategy` |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.gorm-call` | pattern_matched | `First`, `Last`, `Take`, `Find`, `FirstOrInit` (READS), `Create`, `Save`, `Delete` (WRITES) or `FirstOrCreate`, or `Model(x)` chained with a reading (`Count`, `Pluck`, `Find`, ...) or writing (`Update`, `Updates`, `Delete`, ...) method before any `Association`, whose model argument is a GORM model: `&T{}`, `T{}`, `[]T{}`, `new(T)`, `make([]T, n)`, or a parameter or local of the function declared with such a type; a qualified `pkg.T` resolves through `go.mod` module paths; the receiver is not checked to be a `*gorm.DB` |
| Symbol -[:MAPS_TO]-> DbTable | `data-contracts.gorm-table-name-types` | resolved | the Go type checker finds a `TableName()` method of the type with one return of a constant string (a literal, a named constant, a constant expression), in a module whose `go.mod` requires `gorm.io/gorm` or `github.com/jinzhu/gorm` |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.go-sql-types` | pattern_matched | the Go type checker resolves a call to a method of the `go-sql` list declared in a database/sql, sqlx, pgx, GORM or bun package (also when promoted from an embedded field), or to an interface method returning one of their types (sqlc's `DBTX`), and computes SQL text for one of its first three arguments: a constant, a local whose value is followed through the function's statements (`=`, `:=`, `+=`, `if`/`switch`/`select` branches; each path's text kept), `+` joins (other operands unfixed) or sqlx's `Rebind`; one of its texts starts with a reading or writing verb, and the call links the tables of every such text |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.go-sql` | pattern_matched | SQL starting with a reading or writing verb in one of the first three arguments of a database/sql, sqlx, pgx, GORM (`Raw`, `Exec`) or bun query method (`Query*`, `Exec*`, `Prepare*`, `Select`, `Get`, `Named*`, `MustExec*`, `NewRaw`), in a file importing one of those packages or a SQL driver: string literals, `+` joins (other operands unfixed) and `x.Rebind(literal)` |
| File -[:DECLARES]-> DbTable | `data-contracts.laravel-migration-table` | resolved | `Schema::create('name', ...)` or the target of `Schema::rename(..., 'name')` (`Illuminate\Support\Facades\Schema`) with a fixed string, in PHP outside `test/`, `tests/` and `spec/` |
| Symbol -[:MAPS_TO]-> DbTable | `data-contracts.eloquent-table-name` | pattern_matched | an Eloquent model (a class whose `extends` chain, resolved through `namespace` and `use` statements, reaches `Illuminate\Database\Eloquent\Model` or `Illuminate\Foundation\Auth\User`) with a fixed `protected $table`, its own or the nearest ancestor's in the repository |
| Symbol -[:MAPS_TO]-> DbTable | `data-contracts.eloquent-default-table-name` | name_matched | an Eloquent model without `$table`: Laravel's `Str::snake(Str::pluralStudly(class_basename))`, the last word pluralized by doctrine/inflector 2.0's English rules (`PlaylistFolder` is `playlist_folders`, `Person` is `people`); a `Pivot` without `$table` is left unresolved, because Laravel names it from the relation |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.eloquent-call` | pattern_matched | a static call `Model::method(...)` on an Eloquent model, reading (`find`, `where`, `all`, `first`, `count`, ...) or writing (`create`, `insert`, `upsert`, `destroy`, `updateOrCreate`, ...); a reading start whose chain reaches `update`, `delete`, `increment`, ... also WRITES |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.laravel-db-table` | pattern_matched | `DB::table('name')` (`Illuminate\Support\Facades\DB`) with a fixed string, its alias (`as x`) and schema dropped, READS, and WRITES when the chain reaches a writing method |
| File -[:DECLARES]-> DbTable | `data-contracts.sequelize-migration-table` | pattern_matched | `createTable('name', ...)` or `createTable({ tableName })`, or the new name of `renameTable`, called on `queryInterface` or `getQueryInterface()` with a fixed name, as Sequelize migrations do; the receiver is not type-checked |
| Symbol\|File -[:MAPS_TO]-> DbTable | `data-contracts.sequelize-table-name` | pattern_matched | a Sequelize model with a fixed `tableName` option: a class whose `extends` chain reaches `Model` of `sequelize`, `@sequelize/core` or `sequelize-typescript` and that is decorated with `@Table` or initialized by `X.init(attributes, options)` in its file, or a `define('Name', attributes, options)` call in a file importing Sequelize or on a receiver named `sequelize`; the edge starts at the class, else at the declaration holding the `define` |
| Symbol\|File -[:MAPS_TO]-> DbTable | `data-contracts.sequelize-default-table-name` | name_matched | a Sequelize model without `tableName`: Sequelize 6's name, the model name (`modelName`, else the class or `define` name) pluralized by the inflection package 1.13.4 (`Person` is `People`), underscored with `underscored` (`user_profiles`) and kept with `freezeTableName`, the model's options over the `define` options of `new Sequelize` or `config/config.json`; options that are not literals, spread, or set differently in several places leave the name unresolved |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.sequelize-call` | pattern_matched | a static query (`findAll`, `findOne`, `findByPk`, `count`, ...) or persistence (`create`, `bulkCreate`, `update`, `destroy`, `upsert`, ...) method, `findOrCreate` both, on a model named by a local or imported binding (`User.findAll`, `db.User.create`, `sequelize.models.user.count`): the one model with that model, class or variable name in the caller's namespace, else in the repository |
| Symbol\|File -[:MAPS_TO]-> DbTable | `data-contracts.doctrine-table` | pattern_matched | a Doctrine entity (`#[ORM\Entity]` attribute, `@ORM\Entity` docblock annotation, or `<entity>` of a `.orm.xml` mapping, which takes precedence over the class) with a fixed `Table` name; a subclass in a `SINGLE_TABLE` hierarchy takes its root entity's table, a `JOINED` subclass its own, and mapped superclasses and embeddables give none. Also a fixed `JoinTable` name on a property. Names resolve through `namespace` and `use` statements |
| Symbol\|File -[:MAPS_TO]-> DbTable | `data-contracts.doctrine-default-table` | name_matched | a Doctrine entity without a `Table` name: its short class name under the configured naming strategy, read from the `naming_strategy` of the Symfony `doctrine` configuration or a bootstrap's `setNamingStrategy(new UnderscoreNamingStrategy(...))`. The default strategy keeps the name (`BlogPost`), `underscore` snake-cases it (`blog_post`), number aware (`oauth2_client`) with `underscore_number_aware` or from ORM 3, whose version comes from `composer.lock`, else `composer.json`. A custom strategy, or a `loadClassMetadata` listener that calls `setPrimaryTable`/`setTableName`, leaves the repository's Doctrine tables unresolved |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.doctrine-call` | pattern_matched | READS: a repository's own `createQueryBuilder` and finders (the repository's entity from `parent::__construct($registry, X::class)` or the entity's `repositoryClass`), finders (`find`, `findBy`, `findOneBy`, `findAll`, `count`, `matching`, magic `findByX`) on a property or parameter typed with the repository class, unless the repository declares that method, `getRepository(X::class)` finders, `find(X::class, ...)` and query builder `from(X::class, ...)`. WRITES: `update(X::class, ...)`, `delete(X::class, ...)`, and `persist`/`remove` of a variable typed with the entity (a parameter, a property, or `new X()` in the method) |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.doctrine-dql` | pattern_matched | DQL passed to `createQuery` or `setDQL`, read as PHP text is (literals, heredocs, `.` joins, local variables) with `X::class` parts resolved: the entities its FROM and JOIN clauses name are read, the target of UPDATE or DELETE is written. Names are fully qualified or `Bundle:Entity` aliases, which resolve through the bundle class of that name in the repository (`AppBundle:Post` is `App\Entity\Post` when `AppBundle` is in `App`); association joins (`JOIN p.tags t`) are not followed |
| Symbol\|File -[:MAPS_TO]-> DbTable | `data-contracts.mongoose-collection` | pattern_matched | a Mongoose model (`model('Name', schema)` on mongoose, a connection from `createConnection`/`useDb`, the `model` import of mongoose, or a parameter typed `typeof import('mongoose')`, `typeof mongoose` or `Mongoose` whose name no other parameter or variable of the file declares; declared anywhere, including a factory's `return`) with a fixed `collection` argument (a literal or a `const` of the file), or whose schema (`new Schema(fields, options)` or `Schema(fields, options)` in the file, or imported from a repository file) sets a fixed `collection` option; options are read through literals, spreads, constants of the file or imported from a repository file, and `Object.assign`/lodash merges. NestJS models are read too: `MongooseModule.forFeature([{name, schema, collection}])` declares a model, and a schema made by `SchemaFactory.createForClass(C)` takes the `collection` of `C`'s `@Schema(options)`. A discriminator (`Base.discriminator('Name', schema)`) maps to its base's collection and carries `discriminator_of`. A MongoDB collection is a DbTable |
| Symbol\|File -[:MAPS_TO]-> DbTable | `data-contracts.mongoose-default-collection` | name_matched | a Mongoose model without a collection: the model name lowercased and pluralized by mongoose's default pluralize (`UserHistory` is `userhistories`, `Person` is `people`), the same under every mongoose major the `package.json` files allow (mongoose 6 added `goose`, 7 added `human`); a name the allowed majors disagree on, a collection or schema the code does not fix, or a custom `mongoose.pluralize(fn)` leaves the model unresolved |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.mongoose-call` | pattern_matched | a static query (`find`, `findOne`, `findById`, `countDocuments`, `aggregate`, ...) or write (`create`, `insertMany`, `updateOne`, `deleteMany`, `bulkWrite`, ...) method, `findOneAndUpdate` and its kin both, on a model: a binding of it in the file, an import or `require` that resolves (through `module.exports`, `export`, re-exports and namespace members) to the file exporting it, `mongoose.model('Name')`, `mongoose.models.Name`, `this` inside the schema's statics (`schema.statics`, `schema.static()`, static methods of a class given to `schema.loadClass()`), or `this.prop` where `prop` is a constructor parameter or field of the class decorated `@InjectModel(name)` (a string or `X.name`) from `@nestjs/mongoose` |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.mongoose-repository-call` | pattern_matched | a model held in a field of a repository class: a subclass's constructor passes the model to `super(...)` and a base class sets the field from that constructor parameter (`constructor(protected model: Model<T>)` or `this.f = model`). It links `this.field.find()` in the subclass, and a method the subclass inherits, called on `this` or on a property whose declared type is the class (a typed constructor parameter or field, or `= new X()`), with the reads and writes of that method's body and of the methods it calls on `this`, dispatched from the subclass. `const doc = new this.field(data)` followed by `doc.save()` is a write. The base class's own calls are not linked, since they act on every subclass's model; a method the subclass declares is not linked at its callers, because its body carries the edges |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.mongodb-collection` | pattern_matched | a MongoDB driver collection opened by its literal name, `<db>.collection('name')`, in a file importing `mongodb`, `mongoose` or `mongoskin` (or typing `typeof import('mongoose')`) or below a `package.json` that depends on one (Node resolves the module from there; NodeBB keeps its manifest in `install/package.json` and stays unread), on a database handle: `x.db`, `x.db(...)`, `x.connection`, `a \|\| b` or `a ?? b` of handles, or a name the file binds (declarations and plain assignments; `let db;` alone binds nothing) only to handles or types as mongodb's `Db`. The method on the collection, directly, through a variable holding it in the same function, or through `this.f` anywhere in the file when the collection is assigned to `this.f`, gives READS (`find`, `aggregate`, `watch`, `stats`, ...) or WRITES (`insertOne`, `updateMany`, `drop`, `ensureIndex`, `initializeUnorderedBulkOp`, ...), and any other use both. On Countly (labels of #197) this links 577 of 1,148 accesses with 0 wrong, up from 16; the rest go through function parameters and names bound to values the file cannot see (`countlyDb = dbs[0]`). The collection is the same DbTable as a Mongoose model's |
| File -[:DECLARES]-> DbTable | `data-contracts.knex-create-table` | pattern_matched | `K.schema.createTable('t', ...)` or `createTableIfNotExists` (also after `withSchema`) on a Knex instance as `data-contracts.knex-query` defines one; `K.schema.table` (an alter) is not a declaration |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.kysely-scip` | resolved | a scip-typescript index resolves the called method to Kysely's own `selectFrom`, a join (`innerJoin`, `leftJoin`, `rightJoin`, `fullJoin`, `crossJoin`, lateral joins), `insertInto`, `replaceInto`, `updateTable`, `deleteFrom` or `mergeInto`; its first argument is a string literal (alias dropped) or an array of them; selects and joins READ, the others WRITE |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.knex-query` | pattern_matched | in a file importing `knex` or below a `package.json` depending on it, a Knex query names the table by a literal, or by a name bound to one by a module-level `const` of the file or imported by name from the one project module that declares it (when no function around the use declares that name again): `K('t')` (type arguments allowed), `K.table('t')`, `K.batchInsert('t', ...)`, or `table`, `from`, `into` and the `*join` methods in a builder chain rooted at a Knex instance outside `K.schema`. An instance is a name `knex` or a `.knex`/`.#knex` property, a name bound to a call of the imported knex factory, a parameter, property or constructor parameter property typed `Knex`, `Knex.Transaction`, `Knex.QueryBuilder`, a type alias the file declares once for one of them, or a union of them (and a name destructured as such a property, or cast `as Knex.Transaction`), or the parameter of a callback passed to an instance's `transaction()`. Alias (`t as x`) and schema are dropped, and names of common table expressions the file defines (`.with('x', ...)`) are not tables. `insert`, `update`, `del`/`delete`, `truncate`, `increment`, `decrement`, `upsert` or `merge` on the chain (or on a variable holding the query, in the same function) gives WRITES, plus READS with `select`, `first`, `pluck` or an aggregate; anything else gives READS, and a joined table is always READS. Query-builder callbacks (`function () { this.from('t') }`, `qb => qb.from('t')`) and builders kept in other object fields (Ghost's seeders' `this.transaction`) are not read. The position is the table argument's |
| File -[:DECLARES]-> DbTable | `data-contracts.prisma-model` | resolved | a model of a `.prisma` schema as Prisma's own schema engine reads it (`get_dmmf` of the `@prisma/prisma-schema-wasm` build the project's Prisma pins): its `@@map` name, else the model name. A schema with no Prisma version up its tree, a build npm cannot fetch, or a schema the engine rejects is unread and named in a `prisma_schema_unread` warning, and the run is partial |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.prisma-client` | pattern_matched | `<client>.<model>.<method>(...)` in TypeScript or JavaScript, `<model>` being a model's client name (first letter lower case) and `<method>` a reading (`findMany`, `findUnique`, `count`, ...) or writing (`create`, `update`, `upsert`, `delete`, ...) method; the model of the caller's own namespace wins, else the one model of the repository with that name |
| Symbol -[:MAPS_TO]-> DbTable | `data-contracts.typeorm-entity-name` | pattern_matched | a class decorated with TypeORM's `@Entity("name")` or `@Entity({ name })` (imported from `typeorm`, aliases followed) |
| Symbol -[:MAPS_TO]-> DbTable | `data-contracts.typeorm-default-table-name` | name_matched | `@Entity()` without a name: TypeORM's DefaultNamingStrategy name, the snake case of the class name (`HTTPRequestLog` is `http_request_log`); custom naming strategies are not read |
| Symbol -[:MAPS_TO]-> DbTable | `data-contracts.typeorm-child-entity` | pattern_matched | a class decorated with `@ChildEntity` (single-table inheritance): the table of the entity it extends by name, through any number of child levels |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.typeorm-repository` | pattern_matched | `getRepository(Entity)` or `@InjectRepository(Entity)` in a file importing `typeorm` or `@nestjs/typeorm`, resolved like Prisma models by class name. A repository called with a read method (`find`, `findOne`, `count` and the like) only READS, one called with a write method (`save`, `update`, `delete`, `remove` and the like) only WRITES, and any other use does both. Not used where a scip-typescript index resolves the `getRepository` or `@InjectRepository` name (`typeorm_name_rule_replaced` counts them), where `typeorm-scip` reads each call; where the index covers the file but records no symbol, or only a local one, there (its imports did not resolve) it read nothing, and the use is counted as `typeorm_index_unresolved` |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.typeorm-scip` | resolved | each call whose method the scip-typescript index resolves to a read (READS) or write (WRITES) method of TypeORM's `Repository`, `TreeRepository`, `MongoRepository` or `EntityManager`, at the method's name; the entity is the receiver's declared type `Repository<E>` (a property, constructor parameter property, parameter or variable, or `this` in a class extending it), TypeORM's `getRepository(E)` called on a `DataSource`, `Connection` or `EntityManager` (or a variable holding it), or an entity manager method's first argument, every name resolved through the index. `create`, `merge` and other methods that touch no table give no edge. The metric `typeorm_scip` counts `linked`, `no_scip_index`, `not_typeorm`, `entity_unknown` and `not_an_entity` |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.typeorm-repository-wrapper` | pattern_matched | a project's own `getRepository`, called with more arguments than TypeORM's or in a file that does not import `typeorm`, where exactly one argument is an imported or declared class that is an entity (vendure's `connection.getRepository(ctx, Product)`); reads and writes as above. Kept where an index covers the file: the entity of a project's generic wrapper needs the type checker's inference, which the index does not record |
| File -[:DECLARES]-> DbTable | `data-contracts.drizzle-table` | pattern_matched | a variable holding a Drizzle table: `pgTable`, `mysqlTable`, `sqliteTable` or `singlestoreTable` imported from drizzle-orm's core modules, a schema's `.table`, or a `*TableCreator` whose function only wraps the name in fixed text (`` (name) => `app_${name}` ``), called with a literal name |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.drizzle-query` | pattern_matched | `from` and the joins (READS), `insert`, `update` and `delete` (WRITES) with a table variable, and `db.query.<name>.findMany`/`findFirst` (READS); the variable is followed through the file's declarations and its imports (relative, tsconfig `paths` aliases and workspace packages), aliases, re-exports and `export *` included. A module that holds the name but no table (a client-side collection called `posts`) gives no edge. An import the reader cannot follow to its end (an outside package, or a file it could not parse), a property of an imported object (`schema.users`) or a query key resolves to the one table variable of that name in the caller's namespace, else in the repository |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.python-sql` | pattern_matched | SQL starting with a reading or writing verb passed to `execute`, `executemany`, `executescript`, `exec_driver_sql`, `fetch`, `fetchrow`, `fetchval` or `raw`, in a module importing a DB-API driver, asyncpg, SQLAlchemy or `django.db`: a literal, implicit concatenation, an f-string or a `%`/`format` template (interpolated parts unfixed), or SQLAlchemy `text(...)` of one |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.script-sql` | pattern_matched | SQL starting with a reading or writing verb passed to `query`, `execute`, `raw`, `prepare`, `exec`, `run`, `all`, `get`, `each` or a pg-promise query method, in a JavaScript or TypeScript file importing a Node SQL driver (`pg`, `mysql`, `mysql2`, `mariadb`, `sqlite3`, `better-sqlite3`, `mssql`, `oracledb`, `@libsql/client`, ...) or `knex`: a literal, a template string or `+` concatenation (unfixed parts dropped), or pg's `{ text }` config. Tagged templates (`sql\`...\``) are not read |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.php-sql` | pattern_matched | SQL starting with a reading or writing verb passed to a PDO or mysqli `query`, `prepare` or `exec` method, a Doctrine DBAL `execute*`, `fetch*` or `iterate*` method, a `mysqli_*` or `pg_*` query function, or Laravel's `DB::select`, `DB::statement` and the like (also on `DB::connection(...)`): string literals, heredocs and nowdocs, `.` joins and `sprintf` templates (interpolated parts unfixed), or a local variable built from them; a variable's text is its last assignment in a block enclosing the call, or of an `if`/`else` whose every branch assigns it, plus every later assignment in a branch (each an alternative) and every `.=` append; the receiver is not type-checked |
| File -[:DECLARES]-> DbTable | `data-contracts.rails-schema-table` | resolved | `create_table` (and Scenic's `create_view`) with a literal name in a Rails schema file, `db/schema.rb` or `db/<name>_schema.rb` |
| Symbol -[:MAPS_TO]-> DbTable | `data-contracts.active-record-table-name` | pattern_matched | an ActiveRecord model whose own `self.table_name`, or its STI base class's, is a literal (schema prefix dropped) |
| Symbol -[:MAPS_TO]-> DbTable | `data-contracts.active-record-default-table-name` | name_matched | a concrete model without `self.table_name`: ActiveRecord's `compute_table_name`, the demodulized class name underscored and pluralized by ActiveSupport 8.0's English rules plus the app's literal `acronym`, `irregular`, `uncountable` and string `plural`/`singular` inflections; prefixed as `full_table_name_prefix` does: with the literal `table_name_prefix` of the innermost enclosing module that defines one or of the innermost enclosing model, else with the class's own (a `self.table_name_prefix` method or literal assignment, inherited from its superclasses), or with the singular table name of an enclosing model; an STI subclass uses its base class's table |
| Symbol\|File -[:READS]-> / -[:WRITES]-> DbTable | `data-contracts.active-record-call` | pattern_matched | `Model.<method>` with an ActiveRecord query (`where`, `find`, `pluck`, `count`, ...) or persistence method (`create`, `insert_all`, `update_all`, `destroy_all`, ...), `Model` resolved by Ruby's lexical constant lookup (`Module.nesting`, then the top level) to a model of the repository; instance methods, associations and scopes are not read |

Tables are told apart by name (case-insensitive, without schema or quotes)
within a namespace. A module whose Spring configuration
(`src/main/resources/application.*`, as for service names) fixes one
`spring.datasource.url` uses that URL as its namespace, so services that
point at the same persistent database share its tables. Otherwise, and for
in-memory URLs (`:mem:`, `:memory:`), which give each process its own
database, a file whose deepest enclosing Compose build context is built
by services that name exactly one database in their `environment` uses that
database as its namespace: a connection URL (`jdbc:` dropped) whose host is a
service of the same Compose file running a database image (`postgres`,
`postgis`, `timescaledb`, `mysql`, `mariadb`, `percona-server`, `mongo`,
`cockroach`, `libsql-server`), keyed by the Compose file's directory, that
service and the URL's database. On openstatus (3836510) this puts the Go
`private-location` service's SQL (`DB_URL=http://libsql:8080`) on the same
`monitor`, `private_location` and `private_location_to_monitor` tables as the
TypeScript Drizzle schema the `server`, `dashboard` and `workflows` services
use (`DATABASE_URL=http://libsql:8080`); before, it read tables of its own
`go.mod`. `env_file`, interpolated values, host/port/name variables split
across several keys and URLs fixed in code are not read. Otherwise the
namespace is the nearest package. One service's private
`owners` is therefore not another's. URLs written with `${...}`, profiles and
environment variables are not resolved, and migrations kept in a separate
package from their entities meet them only through a shared datasource URL.
SQL and JPQL text is read where the code fixes it: literals, text blocks,
concatenation and the constants and single-assignment locals that service
contract paths are folded through. In Python only the call's own argument is read,
not variables holding SQL, and driver parameter markers (`%s`, `%(name)s`)
never name a table. Fixed SQL text is parsed by sqlglot (a required
dependency), trying the PostgreSQL, MySQL, SQLite, T-SQL and Oracle dialects
and then the generic one; JPA's `?1` markers are read as `?`. The targets of
`INSERT`, `UPDATE`, `DELETE` (with its `USING` tables, aliases resolved),
`MERGE` and `TRUNCATE` are written and every other table the parse names is
read. Common table expressions, `dual`, catalog names, functions in `FROM`
and the `FOR UPDATE OF` lock list are not tables. The hand-written token
reader is kept only where no parser can read the text: SQL holding a part the
code does not fix (that name is dropped), JPQL, and statements sqlglot cannot
parse (PostgreSQL 18's `RETURNING WITH`, SQLite's `VARYING CHARACTER`). The
`sql_text` metric counts each reader (`sqlglot`, `tokens` for holes and JPQL,
`tokens_not_parsed` for statements no dialect parses), so a fall to the token
reader shows in every run. The token reader names tables after `FROM`, `JOIN`,
`USING`, `INSERT`/`MERGE`/`REPLACE INTO`, a statement's `UPDATE`, `DELETE
FROM` and `TRUNCATE`, writing the targets of the last five and skipping
common table expressions, functions in `FROM`, comments and string literals. SQL built at run time, entities passed
through expressions other than a declared variable, named queries and other persistence frameworks
are not read yet.
Test sources (`src/test/`, Go's `_test.go` files, and for Ruby and PHP `test/`,
`tests/`, `Tests/` and `spec/`) and vendored code (`vendor/`, for Ruby and PHP) are skipped.
The tool reads at most 10,000 source files by default (`graph index --tool
data-contracts --max-files N` raises it). When a file count or byte limit
stops the walk, the run is `partial`, the `source_limits` metric names the
limit, and a warning says tables, models and calls in the unread files are
missing.

ActiveRecord models are the classes whose superclass chain reaches
`ActiveRecord::Base` through classes of the repository; the superclass and
`Model.method` constants resolve the way Ruby looks constants up, so in
`class Admin::Panel` the name `User` is `Admin::Panel::User` or `::User`,
never `Admin::User`. A class reopened in several files is one class, and is
left out when its declarations disagree on the superclass or table name.
Abstract classes (`self.abstract_class = true`, `primary_abstract_class`)
give no table. A model whose name the code does not fix is left out and
counted under `active_record_unresolved`: a `table_name` that is not a
literal, a `table_name_prefix` that is not a literal, or
`table_name_suffix`/`pluralize_table_names` assigned on its chain, or an application whose `config/` sets those or
changes inflection in a way that is not read (regular expression rules,
`clear`). Rails apps have no package manifest that the components scan
reads, so their tables share the nearest enclosing package's namespace.

`get_impact` goes forwards over WRITES and backwards over READS, so a change
to a repository that writes a table reaches the repositories that read it.
The component roll-up treats WRITES as providing a table and READS as using
it.

Shared-database coupling has no API contract to show it, so
`get_shared_tables` (`shared-tables`, getSharedTables) lists it directly:
every table that code in two or more packages declares, maps, reads or
writes, with each package's roles and edge counts, tables written from
several packages first. A package is the nearest one enclosing the code, as
for `USES_CONTRACT_OF`, so the snapshot needs the components tool too. Two
packages share a table only within one namespace (a common persistent
datasource URL), so services pointed at one database through unresolved
configuration are not joined.

On spring-petclinic-microservices the tool finds 7 tables, declared by 14
`CREATE TABLE` statements in the hsqldb and mysql schema files of three
services. All 6 entities map to a table that their own service's DDL
declares, with no unmatched mapping. Vet's `@JoinTable` maps
`vet_specialties`, so all 7 declared tables are mapped. All 4 repositories
resolve to their entity.

On spring-framework-petclinic, which implements its repositories three
times (JDBC, JPA and Spring Data JPA), the tool reads all 24 statements that
`grep` finds (13 `JdbcClient.sql`, 5 `createQuery`, 3 `@Query` and 3
`withTableName` calls) with no unfixed text and no unknown entity. Each
links to the tables its text names: for instance the JDBC
`loadPetsAndVisits` reads `pets` and `visits`, the owner and pet `save`
methods write their tables, and the constructors that set up a
`SimpleJdbcInsert` write `owners`, `pets` and `visits`. All 7
`EntityManager` calls of the JPA variant resolve: `find` reads `pets` and
each `save` writes its entity's table.

On spring-petclinic-microservices the 2 `@Query` methods of
`PetRepository` read `types`, which its CRUD repository of pets did not
reach.

On Superset's `superset/` package the Python rule reads 34 statements.
12 build their table names at run time (`f"SELECT {cols} FROM {table_name}"`)
and are left unlinked; the other 22 name what their text names, from the
`tasks` heartbeat `UPDATE` to the catalog queries of the Postgres,
Snowflake and DuckDB engine specs, and statements such as `SELECT 1` name
none. In Django's own `django/` package, 71 statements are read: 23 are
built at run time, and the rest name the system catalogs the backends'
introspection queries (`pg_class`, `sqlite_master`, `user_constraints`,
...).

On prisma/prisma-examples, outside `node_modules`, the tool reads all 147
`model` blocks of its 83 schemas. It links 490 client calls outside the
test sources (548 with them) to their
example's own model and leaves 6 whose name several models of the repository
share outside the caller's package.

Prisma's own schema engine now reads those schemas, each with the
prisma-schema-wasm build its example's Prisma pins (7 builds). At `45d6bf6` it reads 129 models of 75 schemas, the same model and
table names the earlier pattern read for them, and leaves 8 schemas unread,
each named in a warning: 5 `contract.prisma` files of Prisma 8 release
candidates, whose `@prisma/engines` no longer pins a prisma-schema-wasm
build, and 3 schemas whose datasource has no `url`, which the Prisma they
pin (6.9, 6.15, 6.19) rejects as `prisma validate` 6.19.0 does. The calls
of those examples are no longer linked (23 counted as
`prisma_schema_unread`, none sent to another example's model of the same
name), so 467 calls are linked, each also linked before. Against a `grep` for
`x.model.method(` it finds 554 calls to grep's 525: the extra ones are
chains split over lines or with a call as receiver
(`this.prismaService.extendedPrismaClient().post.create`), and the 9 grep
hits it does not take are not Prisma Client calls (`db.orm.public.User`,
or a property that names no model). On nestjs/nest's `sample/` apps it maps
both TypeORM entities (`user`, `photo`) and links both
`@InjectRepository` services and the 5 Prisma calls of the GraphQL sample.

On vendure-ecommerce/vendure `a67242a`, most repository calls go through
vendure's own `TransactionalConnection.getRepository(ctx, Entity)`, whose
first argument is the request context. Reading those wrappers, the
`@ChildEntity` classes and the abstract `@Entity` roots (`Session`,
`AuthenticationMethod`, `OrderLineReference`) takes the linked calls from
66 to 979 and maps 100 entities and 16 child entities, each child to its
root's table (`Country` to `region`, `Sale` to `stock_movement`). Of the
701 lines outside test files that a `grep` for `getRepository(` finds, 636
are linked. The other 65 are comments, a variable or expression argument
(`entityType`, `metadata.target`), the CLI's code templates, and one
string entity name (`'Order'`). 20 sampled wrapper links named the right
table and the right read or write. Seven other TypeScript corpora give the
same edges as before.

On three Rails applications, every table that the tool names for a model is
one that the application's schema file declares, except models of tables
that no longer exist:

| Corpus | Models | In the schema | Not in the schema | Calls linked / not a model |
|---|---|---|---|---|
| lobsters/lobsters `d23941f` | 42 | 42 | 0 | 324 / 5 |
| rubygems/rubygems.org `6f51e32` | 45 | 45 | 0 | 167 / 9 |
| mastodon/mastodon `fe9d150` | 276 | 272 | 4 | 970 / 43 |

Mastodon's 4 are two `StreamEntry` classes declared inside old migrations
and a CLI maintenance task's `AccountIdentityProof`, whose tables later
migrations dropped, and a migration helper mapped to
`information_schema.role_table_grants`. rubygems.org's acronyms (`GitHub`,
`OIDC`) and module prefixes give `admin_github_users` and
`oidc_trusted_publisher_github_actions`; without the acronyms they would be
`git_hub`. The schema tables no model maps are gems' own tables
(Solid Queue, GoodJob, Doorkeeper, Blazer, Flipper), join tables and tables
whose model was removed. Against a single-line `grep` for
`Model.method` with the mapped class names, outside `test/` and `spec/`, the
tool takes every line `grep` finds, plus the calls split over lines
(`Account\n  .where(...)`). The inflector reproduces all 181 singular/plural,
underscore and table-name cases of Rails' own `inflector_test_cases.rb`
(8.0.2).

On openstatusHQ/openstatus `3836510`, a Drizzle monorepo with its schema in
`packages/db` and its queries across `apps/` and `packages/`, the tool reads
all 62 table declarations that `grep` for `= *Table(` finds. It links 867
query-builder calls outside the test sources; every one of the 2,050 edges
that skipping JavaScript and TypeScript tests removed came from a test
source. Before that skip it linked 2,917 calls: a single-line `grep` for those methods with a table
variable name finds 2,888 lines; the tool takes all but 24: 16 are in
`src/test/` directories, which are skipped as test sources, 6 are in a test
file that tree-sitter-typescript does not parse
(`original<typeof import(...)>()`), and 2 name a table through
`await import(...)`. Of the 53 calls `grep` misses, 30 put the table on the
next line and 23 use an imported alias (`page as pageTable`). On
jackblatch/OneStopShop `31208da` the tool reads all 6 tables and its 59
calls are exactly the lines `grep` finds.

On mx-space/core `06e732e`, whose app re-exports its Drizzle schema from a
workspace package (`export * from '@mx-space/db-schema/schema'`), the tool
reads all 69 `pgTable` declarations. A single-line `grep` for the
query-builder methods with a table variable finds 821 lines outside the
test sources. The tool links 778 of them. Following the workspace
re-export added 14 edges that it missed before, and no edge went away. The
others are 30 calls in scripts that load the schema through
`await import(...)`, and 13 `tx.update(posts, ...)` calls in the admin
frontend on client-side collections named like tables, which are rightly
not linked. Openstatus, OneStopShop, the nest samples and prisma-examples
are unchanged.

On mybatis/jpetstore-6 `b7433c2` the tool reads all 25 mapper statements,
ties each to its mapper interface method, and every table they name is one
that the application's DDL declares. On macrozheng/mall `dcaa93b`, 906
statements in 104 mapper files (MyBatis Generator's dynamic
`Example_Where_Clause` fragments included) are all read with fixed tables;
904 belong to a mapper method, and the other 2 are statements that only a
`resultMap`'s nested `select` names. The 76 tables they name are all in
`mall.sql`. In mybatis/spring-boot-starter's samples, the 11 annotation
statements outside comments are read; 8 name `city`, and 3 hold the path
of a FreeMarker, Velocity or Thymeleaf template, which is not read.

On jmoiron/sqlx the Go rule reads 166 statements, every one with fixed
table names. A single-line `grep` for a query method followed by SQL finds
25 lines the tool links no table from: 7 are in comments, 12 are statements
that name no table (`SELECT 'null' AS raw`), and 6 pass the text through a
local alias of `Rebind` (`r := db.Rebind`), which is not followed. The tool
also reads the statements that span lines.

**Go facts from the type checker.** Go SQL calls and GORM table names are read from the Go type checker first.
`src/deepthought/gofacts/main.go` type-checks every package of each Go
module from source with the standard library's `go/types`, and prints the
method calls (the package declaring each method, the packages of its
result types, the string value of its first arguments) and the `TableName`
methods returning a constant. It is built once per toolchain into
`~/.cache/deepthought` (or `DEEPTHOUGHT_CACHE`) with the `go` on `PATH` (or
`DEEPTHOUGHT_GO`). It builds and runs nothing of the analyzed repository and
reads dependencies only from the module cache: `GOPROXY=off`,
`-mod=readonly`, `GOTOOLCHAIN=local`, `GOWORK=off`. A dependency that is not
in the cache (run `go mod download` first), or language features newer than
the toolchain, leave some calls untyped, and the package is reported as
partly checked. A module whose `go.mod` asks for a newer Go (its
`toolchain` line, else its `go` line) is checked with that release when the
module cache already holds it under `golang.org/toolchain`, where `go`
with `GOTOOLCHAIN=auto` (as `go mod download` runs) puts it; nothing is
downloaded. The `toolchains` count of the metric says which ran.

The tree-sitter rules (`go-sql`, `gorm-table-name`) read only what the type
checker could not: files of no module, or with no Go toolchain, and calls
it could not type in partly checked packages. `TableName` string literals
in files importing a SQL package are still read everywhere. The
`go_type_facts` metric counts modules, checked, partly checked and
unchecked files (with the reason: `go_not_installed`, `no_go_module`,
`gofacts_build_failed`, `gofacts_failed`, `gofacts_timeout`,
`not_checked` for files excluded by build constraints or not parsed).

Why not hand rules: the misses were name resolution, which the compiler
does exactly. A file that runs SQL through a field typed in another file
imports no SQL package (miniflux's storage, sqlc's `DBTX`), SQL text sits in
constants and locals, and `TableName` returns constants. `go/ssa` def-use
chains were considered for locals; instead gofacts follows each local string
through the function's statements itself, on the `go/types` objects and
constant values, which needs no module outside the standard library. Each
`=`, `:=` and `+=` sets the variable's texts on the current path; the paths
of an `if`, `switch` or `select` are merged as alternatives (a `break`,
`return` or `panic` ends its path); a variable assigned inside a loop, whose
address is taken or which a func literal assigns is unknown; a function
with `goto` or labels is not followed. More than 16 alternatives fold to
their common prefix and suffix around the unfixed text. A call is linked to
the tables of every alternative that is SQL, since any of those paths may
run: miniflux's `category.go` builds `UPDATE`/`SELECT` text with `+=`
under `if`, and `nav_metadata.go` picks a query in a `switch`.

Measured on four Go apps outside the corpora (Go 1.26.10, dependencies
downloaded, every package fully checked), against the tree-sitter rules
alone; no link was lost but two reads of `information_schema.columns`,
now left out as a catalog:

| App | SQL statements | Files with READS/WRITES | GORM `TableName` links |
|---|---|---|---|
| miniflux/v2 | 42 → 140 | 8 → 17 | 0 → 0 |
| techschool/simplebank (sqlc) | 0 → 20 | 0 → 6 | 0 → 0 |
| flipped-aurora/gin-vue-admin | 2 → 19 | 2 → 30 | 2 → 23 |
| gothinkster/golang-gin-realworld-example-app | unchanged | unchanged | unchanged |

Calls are read anywhere in a file, including the func literals of a
package variable (miniflux's migrations). With Go 1.24.7 on `PATH`,
miniflux and gin-vue-admin, which require Go 1.26, give the same result:
their `go mod download` left go1.26.0 and go1.26.10 in the module cache, and
those check them. Without those releases most of their packages are partly
checked and keep the tree-sitter rule at their untyped calls. gin-vue-admin's new
`TableName` links let 247 GORM calls through the existing `gorm-call` rule;
its other models get no default name, since it configures a
`NamingStrategy`. jmoiron/sqlx is unchanged: its statements are all in test
files.

On gothinkster/golang-gin-realworld-example-app the tool maps its 7 GORM
models (`user_models`, `follow_models`, `article_models`, ...) and links 32
calls. The default-name port gives GORM's own result for all 1,786 struct
and word names tried (the type names of several Go corpora and GORM itself,
and the camel-cased words of Rails' inflector cases), checked against
`schema.NamingStrategy{}.TableName` of gorm.io/gorm 1.25.12. A single-line
`grep` for the GORM methods outside tests finds 36 lines: the tool links
every one except two `Save(data)` calls whose argument is an
`interface{}`, two `Association` chains (`Replace`, and a `Find` into a
struct field) and `query := db.Model(...)`, whose chain continues in
another statement. Four grep lines are calls the tool reports at the line
where their chain starts, and it also links one chain that `grep` misses
because it spans lines.

On outline/outline `fd66b5e`, all 42 sequelize-typescript `@Table` models
are mapped. Their superclasses (`ParanoidModel`, `IdModel`, and a local
`Model` over sequelize-typescript's) resolve through relative imports and
the `@server/*` alias of `tsconfig.json`; a second `ParanoidModel` in the
frontend is never taken. The 41 tables (`Document` and `Template` share
`documents`) are all created by the baseline `schema.sql` or a
`createTable` migration. A single-line `grep` for the mapped class names
followed by a Sequelize method finds 1,854 lines, 821 of them outside the
test sources, and the tool links all 821; it links 837 calls in all, the
others split over lines or written with type arguments
(`Import.findAll<Import<any>>(`). The 96 calls not linked are on other
classes (`TextSelection.create`, `Metrics.count`, ...). On
TonyMckes/conduit-realworld-example-app `5e127d8` the 4 `Model.init` models
get their default names (`Users`, `Articles`, ...), and each is one its
migrations create; its 23 calls are the lines `grep` finds. In
cirosantilli/node-express-sequelize-nextjs-realworld-example-app `b7f8b80`,
the `define: { freezeTableName: true }` given to `new Sequelize` keeps the 6
`define` model names as tables, and its 65 calls are the lines `grep`
finds. The `through` tables of `belongsToMany`, instance methods, and
`beforeDefine` hooks are not read. The pluralization port gives the
inflection package's own result for all 45 `pluralize` and `underscore`
cases of its test suite.

On kimai/kimai `d46d79e` (ORM 2.20, attributes) the tool maps all 29
entities and 4 join tables, and every one of those 33 tables is one its
migrations create or alter. It links 193 repository and EntityManager
calls; one finder on a non-entity repository is not linked. On
symfony/demo `f42980c` (ORM 3.7) the 4 entities and the post-tag join table
are mapped. On partkeepr/PartKeepr `f6176c3` (docblock annotations, no
naming strategy) 48 entities take the default strategy's class names, and
the 4 tables its migrations alter (`Part`, `PartKeeprUser`, `PartParameter`,
`ProjectPart`) carry those names. wallabag/wallabag `5dcb21b` prefixes every
table in a `loadClassMetadata` subscriber, so its 16 entities are left
unresolved instead of named without the prefix. Sylius declares its models
as mapped superclasses that its resource bundle turns into entities at
run time; only their join tables are read. On PartKeepr the tool reads
the DQL of all 37 `createQuery` calls outside its tests, most of them
through `PartKeeprPartBundle:Part`-style aliases. Entities whose type is not
declared where they are persisted, PHP and YAML mapping drivers and custom
repository base classes are not read yet. Symfony's `Tests/` directories
are skipped with the other PHP test trees.

Node SQL text is read where a file imports a driver. On
dockersamples/example-voting-app the result service's
`SELECT ... FROM votes` is read. On mx-space `06e732e` it reads 22
statements in migrations, upgrade checks and scripts outside tests (22 more
than before), and links them to the same tables as the Drizzle schema
(`accounts`, `options`, `api_keys`). The 3 it leaves unfixed name their table
through a `${...}` substitution (`${table}`, `${MIGRATIONS_TABLE}`). Every
literal `query('SELECT|INSERT|UPDATE|DELETE ...')` line that a `grep` finds
in its pg-importing files outside tests is read, and those with no table
(`SELECT pg_advisory_lock($1)`) link nothing. vendure (2 load-test reads) and
openstatus (1 probe script) gain those edges only, and librechat, nest,
prisma-examples, awesome-compose, OneStopShop and mastodon are unchanged.

The MongoDB driver reader finds the collections that scripts and migrations
open by name, bypassing the models. On habitica it links 44 of the 80
`.collection('...')` lines `grep` finds outside tests, and the driver's
`users` lands on the same DbTable as the `User` model, so a migration that
rewrites users shows up in the model's impact. The other 36 are commented-out
lines and archived migrations whose handle is an untyped callback parameter
or a reassigned `let`, which the reader does not guess. On LibreChat it links
11 of 14. The other 3 are a template-string name and two scripts that take
`db` as an untyped parameter. hackathon-starter links 2 of 2, and novu,
mx-space, nest and builderbook give the same edges as before.

The verification suite's Mongoose corpora score every model with no wrong
or extra collection: electerious/Ackee 6 of 6, node-express-boilerplate 2
of 2 (`mongoose.Schema(...)` without `new`) and LibreChat 69 of 69 (models
returned by factories that take mongoose as a typed parameter). On nest's
samples the only new edge is the `cats` collection of the
`useFactory: (mongoose: Mongoose) => mongoose.model('Cat', CatSchema)`
provider, and novu, mx-space, habitica and builderbook give the same edges.

On HabitRPG/habitica `02edf1a` all 18 `mongoose.model` models and the 4
`Task` discriminators are mapped, and the default names of the 7 that the
repository's raw-driver migrations open by name (`users`, `groups`, `tasks`,
`challenges`, `coupons`, `emailunsubscriptions`, `inboxes`) are those names.
It links 447 calls. Of the lines a single-line `grep` finds for the model
bindings, the 14 the tool misses are raw-driver collections bound to a
model's name, `this.constructor` in an instance method, and archived
migrations whose relative imports no longer resolve because the files
moved. The extra lines it finds are calls split over
lines and calls in statics. On builderbook/builderbook `a71c72e`, 57 models
across the book's chapter copies map in each copy's own namespace, and the
tool links 317 of the 318 lines `grep` finds; the other is commented out.
On sahat/hackathon-starter `926460c` it links the 28 lines `grep` finds
outside the tests, plus one call in a static. Mongoose's own test suite
holds almost no pluralize cases, so the rule tables are checked against
these names. Instance methods (`doc.save()`), query chains held in
variables, Typegoose and the raw MongoDB driver are not read yet.

Repository classes are followed across files and across workspace
packages. An import of a sibling package by name (`@novu/dal`) resolves
when the importing file's nearest `package.json` depends on it through the
`workspace:` protocol and exactly one `package.json` of the repository
carries the name. The package's entry points (`exports["."]`, `types`,
`main`, `module`) give its file, either directly or, for a built entry
under a tsconfig's `outDir` in the package's directory, as the source under
`rootDir` that the compiler writes it from (`dist/index.js` from
`src/index.ts`). `export * from` is followed, and a name that two star
exports both give is no export, as in ES modules. The Mongoose, Drizzle and Sequelize
readers resolve workspace packages; a subpath (`@scope/db/schema`) goes
through the package's `exports` map when it has one. On novuhq/novu `d688aea` the tool links
1,409 repository calls, where it linked none before. Of the 1,335
`this.<x>Repository.<base method>(` lines a single-line `grep` finds
outside the tests, 1,033 are linked. The others are methods the subclass
declares (116, whose bodies are linked), helpers that touch no collection
(52, such as `withTransaction`), and calls on classes that are not
Mongoose repositories here (130: wrappers that delegate to an injected
interface, and ClickHouse repositories), plus 4 whose repository comes from a factory
(`getEERepository<UserRepository>(...)`). All 68 `this.MongooseModel` and `this._model` calls in
the subclasses are linked, and a sample of 15 links named the repository's
own collection each time. TypeScript's `await x.f<T>(...)`, which
tree-sitter reads as a call of `await x.f`, is now read as a call of
`x.f`.

On brocoders/nestjs-boilerplate `9620f15` the 4 NestJS models are declared
by `forFeature`, without collections, so they take default names such as
`userschemaclasses`, and the tool links 19 calls. Every line a single-line
`grep` finds for `this.<injected model>.` is one of them; the other lines
`grep` finds for those method names are on services or TypeORM
repositories. On novuhq/novu `d688aea` all 44 models resolve, several
through schema options imported from a shared file. Its calls go through a
`BaseRepository` that holds the model in a field set by a subclass, so
none are linked yet. When several files declare a model of the same name
for the same collection, as NestJS feature modules do, it is one model.

JavaScript and TypeScript test sources (`test/`, `tests/`, `__tests__/`,
`spec/`, `*.test.*`, `*.spec.*`) are skipped by every script reader
(Prisma, TypeORM, Sequelize, Drizzle and Mongoose), as Ruby, PHP
and Go tests are. A test's fixture calls are not the application's data
access, and on outline/outline `fd66b5e` they were 1,047 of the 1,887
Sequelize call edges. On outline and prisma/prisma-examples every edge that
goes away comes from a test source, and on ten other JavaScript and
TypeScript corpora nothing changes.

On koel/koel `f0d5bc7` the tool maps 25 Eloquent models, and every table
they map is one its migrations create. One `Pivot` without `$table` is left
unresolved, and classes extending vendor bases other than Eloquent's are not
models. It links 317 static calls; the 375 static calls it does not link are
on facades and helpers (`Arr`, `Route`, `Cache`, ...). A single-line `grep`
for `Model::` calls on the mapped classes finds 313 lines, and the tool links
all of them, plus 2 calls through an aliased `use ... as Episode`. The
default-name port gives doctrine/inflector 2.0.10's own plural for all 351
English words of its `EnglishFunctionalTest` samples. Raw SQL in
`DB::select` and `DB::statement`, query builders held in variables and
relations are not read yet.

On Django 5.1.4's `django/contrib` apps (admin, auth, contenttypes, sessions,
sites, redirects, flatpages) the tool finds the 13 tables those apps create
in every Django database, and no others: `auth_group`,
`auth_group_permissions`, `auth_permission`, `auth_user`,
`auth_user_groups`, `auth_user_user_permissions`, `django_admin_log`,
`django_content_type`, `django_flatpage`, `django_flatpage_sites`,
`django_redirect`, `django_session` and `django_site`. These come from
9 concrete models. The abstract `AbstractUser`, `PermissionsMixin` and
`AbstractBaseSession` give none, and `auth_user_groups` comes from the
abstract `PermissionsMixin`'s field. 16 manager calls are linked, with none
unresolved. Over the whole repository, including Django's test apps, 1,580
models give 1,697 tables in 33 s. 3,578 manager calls are linked, and 7,472
are left unlinked because the class name is not a model or names several
models.

On FreshRSS/FreshRSS `fa16ffd` the PHP rule reads 82 statements: 64 name
fixed tables (`_entry`, `_feed`, `_category`, `_tag`, `_entrytag`), 16 are
in a bundled SimplePie cache that joins a configured prefix to every name,
and 2 name no table (`SELECT 1`). A single-line `grep` for `->query(`,
`->prepare(`, `->exec(` and the other methods finds 127 lines, every one of
the 82 among them. Of the 45 the tool does not take, 12 are XPath queries
and the rest run DDL or session statements (`ALTER`, `CREATE`, `PRAGMA`,
`ANALYZE`, `SET`), or SQL that an application helper builds
(`sqlIgnoreConflict($sql)`, `str_replace`) or that arrives as a parameter.
On pixelfed and koel the 5 and 4 `DB::select`/`DB::statement` statements
with SQL verbs are read; their other `DB::statement` calls are DDL. Tables
qualified with a schema give the bare name, as everywhere in the SQL reader.

The SQL reader leaves out database system catalogs, which are the
database's tables, not the application's: any name in the
`information_schema`, `pg_catalog`, `sys` or `performance_schema` schema,
names starting `pg_` (PostgreSQL's catalog prefix) or `sqlite_` (reserved by
SQLite), and Oracle's `all_`, `user_` and `dba_` dictionary views by name
(`all_tables`, `user_tab_columns`, ...; `user_settings` stays a table).
Before, `information_schema.columns` became a table named `columns`, which
would join any real `columns` table; gin-vue-admin's code generator reads 27
such catalog names.

On Apache Superset's `superset/` package, outside its Alembic migrations,
the tool maps 39 classes through `__tablename__`. `grep` for
`__tablename__ =` finds 38; the extra one is the annotated
`__tablename__: str = ...` of the audit coordination model. `Table()` calls
resolve only where SQLAlchemy's `Table` is meant, so Superset's own `Table`
dataset class is not taken for a table declaration. 151 `query()` and
statement calls are linked to a table; 919 are left unlinked, mostly
`.query()` calls on names that are not mapped classes. Declarative
`__table__`, imperative mappers, `session.add`/`delete` on instances and
textual SQL are not read.

**Python model names through SCIP.** Where a scip-python index covers a
file (`scip_indexes`, the same pairs of ingested index and receipt digest
the tree-sitter tool takes), a model name written in `.query(...)`,
`select`/`insert`/`update`/`delete`, `Model.query`, `.get(Model, key)` or
`.select_from(Model)` links to the table of the class the index resolves
it to (`deepthought.scip_calls.ScipReferences`), and to nothing when that
class is not mapped. Each access edge's `line` and `column` are where the
model name is written, which in a statement spanning lines can be after
the call's first line. A resolved declaration must carry the written name,
as in call binding. Files no index covers keep the name rules, which link
only when one mapped class of the repository has the name; `.get` and
`.select_from` are linked only through an index, since `get` is the
method of every mapping. An instance passed to a session (`session.add(user)`) is linked only
through the index: the method must resolve to one of SQLAlchemy's session
classes, and the variable to a local whose type scip-python documents
(`app.models.User`, as `Definition.value_type`), which names the class
through that class's own SCIP symbol. scip-python documents no type for
parameters or for values pyright infers as a union (`User | None`), so for
those the declared type is read where the index defines the variable: a
parameter's or annotated variable's annotation, or for `x = f(...)` the
return annotation of the function the index resolves `f` to, or the model
of SQLAlchemy's `Session.get(Model, key)`. `Annotated[T, ...]` and
`Optional[T]` (resolved to `typing`'s own symbols), `T | None` and a
module-level alias (`CurrentUser = Annotated[User, Depends(...)]`, as
FastAPI writes them) give `T`; every name in them is resolved through the
index. Types only inference gives (`db.exec(select(User)).first()`) are not
linked. The metric
`sqlalchemy_access_resolution` counts `linked`, `linked_scip`,
`not_a_mapped_class`, `scip_not_a_mapped_class`, and for instances
`instance_no_scip_index`, `instance_method_unresolved` (the index resolves
the method to nothing outside the file, as when the app's dependencies
were not installed for the indexer), `instance_not_session` and
`instance_type_unknown`.

Why not hand rules: the misses were name resolution. Superset's migrations
redefine `Slice`, `Dashboard` and other models under the same names, so the
name rule found several classes and linked none, and Flask-SQLAlchemy
models are imported across modules. A type checker resolves both exactly.

Measured with scip-python 0.6.8, against main, on three apps outside the
corpora (the index built without installing the apps' dependencies; no
link made by name was lost or moved to another table):

| App | READS/WRITES links | Mapped classes |
|---|---|---|
| apache/superset | 428 → 2,984 | 238 → 238 |
| getredash/redash (Flask-SQLAlchemy) | 6 → 92 | 19 → 19 |
| fastapi/full-stack-fastapi-template (SQLModel) | 0 → 28 | 0 → 2 |

Without an index, Redash gets the same 92 links by name and the SQLModel
template 16 of its 28. 30 sampled new Superset links and every `get` call
whose receiver is not named like a session were read in the source and are
right. Still not read: `cls.query` in a model's class methods, and
Superset's index is 124 MB, past `ingest_scip`'s default 50 MB cap.

**Kysely.** Kysely queries name tables as string literals typed against
the database interface: `db.selectFrom('asset as a').innerJoin('user', ...)`,
`insertInto('album')`. A call counts only when the scip-typescript index
(the `scip_indexes` the SQLAlchemy reader takes) resolves the method to
Kysely's own (`scip-typescript npm kysely ... QueryCreator#selectFrom().`,
`SelectQueryBuilder#innerJoin().`, ...), so the compiler checks the
receiver. The index is required: in a file that imports `kysely` and that
no index covers, no call is read, the `kysely` metric counts the calls
under `no_scip_index`, and the run is `partial` with a warning naming the
missing index. Common table expressions are not tables: a read of a name
that a `with`, `withRecursive` (or `with((cte) => cte('name'), ...)`) of
the same statement declares is left out, except inside a non-recursive
CTE's own body, where the name is the table. A write always names a table,
since SQL writes no CTE. The metric also counts `calls_scip` and
`not_kysely` (a method the index resolves to another declaration).

Why not hand rules: whether `x.selectFrom` is Kysely's is a question of the
receiver's type, which the compiler answers; the table names themselves
are literals.

The TypeScript grammar fails on a tagged template whose type arguments
hold an object or array type (``sql<{ a: string }>`...` ``,
``sql<string[]>`...` ``), Kysely's raw SQL helper, and dropped the whole
file. `parse_source` now reads such type arguments as blanks (line breaks
kept, so every offset stays) on rows an error spans, and keeps that tree
when it has fewer errors, as it does for generic call signatures. On
immich-app/immich this parses 9 of the 11 TypeScript files that failed; the
other 2 fail for other reasons. Files still parsed only partly are read for
Kysely calls, since the index confirms each call.

On immich-app/immich (server, scip-typescript 0.6.2 with the workspace's
dependencies installed by `pnpm install --ignore-scripts`), main links no
Kysely query; the tool now links 754 calls in 45 files (542 READS, 212
WRITES), and every linked name is a table its schema declares (`@Table`) or
Kysely's own `kysely_migrations`. Not read: table names passed as variables
(`selectFrom(table)`, `db.dynamic.table(...)`), and test sources.

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
`findReferences`, `findImplementations`, `getSupertypes`, `getFileDependencies`, `getFileDependents`, `explainEdge`), the component tools (`listComponents`, `getFileComponents`, `getComponentFiles`, `getComponentDependencies`, `getComponentImpact`), `getSharedTables`, `getCallSites`, `listSnapshots` and `diffSnapshots` are available by default. CLI and MCP results carry actual stored facts and bounded relationships;
they do not dump the complete graph into the agent prompt.

`searchCodeElements` (CLI `search`) queries the `code_search` fulltext index.
The index keeps a member's dotted name (`Command.Flags`) as one token, so a
query that is one identifier also returns, first and marked `name_match`, the
declarations named by it or by `.` and it. `Flags` finds `Command.Flags`; `lags`
and `Command.Flags` use only the index.

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
