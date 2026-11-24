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
  oracle indexed. scip-python gives every later declaration of an
  `@overload` group (the implementation too), and a redefinition of a name
  in one scope, the first declaration's symbol, so a call to that symbol
  lists each of those `def` lines as a target: a link to the implementation
  confirms.
- `baseline.json` is the current score of each corpus.

When `GITHUB_STEP_SUMMARY` is set, as it is in the CI job, `check` also
writes a table of every corpus and layer (calls, inheritance, imports,
impact, declarations, contracts) to the run's summary page, with each score
that moved since the baseline shown with its difference. A PR that raises
call or impact recall shows by how much there, not only that the gate
passed.

Java has no SCIP indexer in this setup, so `derive-java` takes the truth from
javac's own attribution (`tools/JavaCallTruth.java`, JDK only): every method
invocation and `new` expression javac binds, at the line of the called name,
targeting the line of the declaration's name. A `new` expression accepts its
constructor and its class. Calls javac cannot bind (a missing library) are left
out, and so is a call of an overloaded method or constructor with an
argument whose type javac cannot resolve. gson's proto tests use classes built
from `.proto` files that the corpus does not contain, so for
`toJson(null, TestAllTypes.class)` javac's error recovery picked
`toJson(JsonElement, Appendable)`. Leaving those calls out dropped 102 rows,
nearly all in `proto/`.

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
# A name that is in no manifest list fails instead of checking nothing.
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
| click | python | 3,313 | 2,878 | 0 | 1.0 | 0.8639 |
| ky | typescript | 143 | 331 | 0 | 1.0 | 1.0 |
| hono | typescript | 3,009 | 5,775 | 0 | 1.0 | 0.5274 |
| cobra | go | 2,197 | 1,986 | 0 | 1.0 | 0.9026 |
| chi | go | 1,223 | 949 | 0 | 1.0 | 0.6868 |
| preact | javascript | 3,884 | 4,427 | 0 | 1.0 | 0.8671 |
| anyhow | rust | 444 | 223 | 0 | 1.0 | 0.491 |
| ripgrep | rust | 11,676 | 3,346 | 0 | 1.0 | 0.2865 |
| gson | java | 11,050 | 9,342 | 0 | 1.0 | 0.8363 |

Recall is low by design where calls need a receiver's type (`x.m()`,
`c.Root().Name()`), which name-based resolution does not infer. The largest
missed groups are:

- click: 1,717 calls of a package attribute (`click.echo(...)`) whose
  declaration is re-exported by the package's `__init__.py`
  (`module_attribute_not_declared`); 541 method calls on values.
- cobra: 1,115 method calls on values (`attribute_call`).
- ky: 97 calls with no extracted call site, mostly `this.#private()` methods.
- preact: 467 calls of an imported name the engine cannot follow to a
  declaration (`imported_name_not_declared`), down from 2,812 once re-exports
  and the jsconfig.json `paths` aliases (`import { render } from 'preact'`)
  are read.
- ripgrep and anyhow: calls with no extracted call site (5,944 and 154),
  mostly method chains and calls inside macro invocations such as
  `assert_eq!(...)`, and 2,152 and 65 method calls on values.

The one contradiction is a real defect: in `examples/imagepipe/imagepipe.py`
line 35, `processor(stream)` calls the loop variable of
`for processor in processors`, and the engine links it to the module-level
function `processor` defined on line 42.

preact's former 6 contradictions were one defect, fixed by block-scoped
bindings: a call bound to a `let` or `const` of the same name declared in a
sibling function scope (`click(1)` in `test/browser/events.test.jsx` line
418).

preact is a JavaScript project with hand-written TypeScript declarations
beside its sources (`src/index.d.ts`). Indexed as it is, TypeScript binds a
call through `src/index` to the declaration in the typings, not to the
function that runs. The oracle command deletes the 19 `.d.ts` files first,
writes a tsconfig.json from jsconfig.json with `allowJs`
(`tools/javascript_tsconfig.py`), and maps `preact`, `preact/hooks` and the
other entry points to their `src/index.js`, as the repository's
vitest.config.mjs aliases them for its tests. The `.ts` and `.tsx` test files
are dropped from the truth, since the corpus analyzes JavaScript only.
scip-typescript names the constructor of a class declared in a function
without defining it; `derive` binds such a construction to the class.

`check` refuses to score (exit status 2) when a grammar or the YAML reader a
selected corpus needs is not installed. Without them the engine reports files
unavailable and gateway routes unread instead of failing, so the scores would
drop as if it had regressed. Install the package with its `languages` extra.

### Imports

`derive-imports` reads an index's references to module and package symbols.
On a line of an import statement, the rightmost module reference (the
innermost module of a dotted path) names what the statement imports: the
files that define it, their directories for Go packages, or `external` when
the repository defines none. Indexer quirks are normalized: scip-python names
a relative import's module without its package (rebuilt from the leading dots
and the importing module) and gives some references the installed version
instead of the commit (versions are ignored). An engine import is judged by
the truth lines it spans: *confirmed* when it links an expected target,
*contradicted* when it links elsewhere or links a module the oracle places
outside the repository. Recall is over import lines with a repository target.

| Corpus | Expected | Confirmed | Contradicted | Precision | Recall |
|---|---|---|---|---|---|
| click | 331 | 331 | 0 | 1.0 | 1.0 |
| ky | 98 | 98 | 0 | 1.0 | 1.0 |
| hono | 1,007 | 1,003 | 0 | 1.0 | 0.996 |
| cobra | 12 | 12 | 0 | 1.0 | 1.0 |
| chi | 31 | 31 | 0 | 1.0 | 1.0 |
| preact | 442 | 435 | 0 | 1.0 | 0.9842 |

Every external import is left unresolved (click 320, ky 1, cobra 174). ky's
tests are outside its TypeScript project, so the index does not cover them.
preact's package aliases (`'preact'`, `'preact/hooks'`) resolve through its
jsconfig.json `paths`. The oracle maps only the entry points its vitest
config aliases, so `'preact/compat/scheduler'`, which the engine leaves
ambiguous between `compat/scheduler.js` and `compat/scheduler.mjs`, is not
judged.
Rust is not covered: rust-analyzer references the item a `use` names, not a
module, and the engine links items too, so it needs its own derivation.

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
| rack (Ruby) | 1014 | 1014 | 0 | 0 | 0 |
| Slim (PHP) | 876 | 876 | 0 | 0 | 0 |
| laravel/framework (PHP) | 38104 | 38104 | 0 | 0 | 0 |

The PHP oracle also reads a method of an anonymous class written after an
attribute or modifier (`new #[Queue('x')] class`, `new readonly class`) as a
method. Before, it listed four such methods in laravel as functions.

#### PHP imports

The two PHP corpora also score `use` imports (layer `imports` under each
declarations entry). `tools/php_import_truth.php` reads every top-level
`use` with `token_get_all`, leaving out trait uses and closure `use (...)`.
It resolves each class-like name through Composer's own class map
(`composer dump-autoload --optimize --no-scripts --no-plugins` into a
temporary vendor directory). A name Composer does not map falls back to the
files whose tokens declare it, or that declare something in a namespace of
that name. A `use function` resolves to the files declaring the function.
Anything else is external, and `use const` is not listed.

| Corpus | Repository imports | Confirmed | Contradicted | External |
|---|---|---|---|---|
| Slim | 306 | 306 | 0 | 510 |
| laravel/framework | 9682 | 9682 | 0 | 5005 |

The gate fails on fewer confirmed or more contradicted imports, or a
changed truth.

#### Ruby requires

rack also carries require truth from `tools/ruby_require_truth.rb`. Prism
finds each `require`, `require_relative` and `autoload` whose path is one
literal string. A `require_relative` resolves against the requiring file's
directory. A `require` or `autoload` searches the gemspec's `require_paths`
first (`lib` by default), as `gem` and `bundle exec` do. Otherwise
`$LOAD_PATH.resolve_feature_path` decides: a standard library or installed
gem is external, and a feature Ruby cannot find here (`simplecov`, Rails in
`test.rb`) is not listed. Of 340 rows, 287 name a repository file. The
engine does not resolve Ruby requires yet, so the baseline is 0 confirmed.

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
| online-boutique | grpc | 20 / 20 / 20 | 12 / 12 / 14 |
| online-boutique | http | 9 / 9 / 9 | 1 / 1 / 1 |
| robot-shop | http | 4 / 4 / 42 | 3 / 3 / 17 |
| robot-shop | messaging | 1 / 1 / 1 | 1 / 1 / 1 |
| apollo fullstack-tutorial | graphql | 7 / 7 / 7 | 1 / 1 / 1 |
| apollo spotify-showcase | graphql | 46 / 46 / 46 | 2 / 2 / 2 |
| packt Chapter07 | messaging | 0 / 0 / 3 | 0 / 0 / 3 |
| packt Chapter23 | messaging | 0 / 0 / 3 | 0 / 0 / 3 |
| grpc-go route_guide | grpc | 4 / 4 / 4 | 1 / 1 / 1 |
| grpc-java route_guide | grpc | 4 / 4 / 4 | 1 / 1 / 1 |
| grpc-node route_guide | grpc | 10 / 10 / 10 | 1 / 1 / 1 |

Most unmatched labels are browser or load-generator calls that reach a backend
through a gateway route. The one wrong call site is the developer test client
`AdServiceClient.java` inside adservice, which calls its own service.

The two GraphQL corpora are labelled by graphql-core, not by hand
(`tools/graphql_client_oracle.py`, see the fixtures README). A prediction is a
REQUESTS edge to a GraphqlField, placed at the request line and at the
field's declaration. The fullstack tutorial's `start/` client, whose own
server declares nothing, must not link to `final/server`; since #121 a
client only matches the server scopes nearest to it.

The two Packt corpora (`Microservices-with-Spring-Boot-and-Spring-Cloud`,
4th edition, at db9e0fa) are Spring Cloud Stream apps labelled by hand. Each is
one chapter of the book's repository, analyzed on its own (`root` in the
manifest), so paths are relative to `Chapter07` and `Chapter23`. Chapter23
keeps its configuration in `config-repo/`, which the Compose units mount as
`SPRING_CONFIG_LOCATION`. The composite publishes all three topics from one
`streamBridge.send(bindingName, message)` in a private helper, called with
literal binding names, so the three labelled links share that send site.
Main reads 0 of the 3 in each: the binding is a parameter.

Two WebSocket corpora label the browser side of Spring STOMP apps: the
spring-guides STOMP guide (45228a1) and an Angular port of it,
chriswhite199/spring-boot-angular2-stomp (78ade07). Both were labelled by
hand from the guide text and the code, and built by
`evals/fixtures/service_contracts/build_websocket.py`, from repositories
the WebSocket reader's own validation did not use.

- **Handshake:** the connect is an `http` link, `GET /path`, to the
  `addEndpoint` line.
- **Frames:** STOMP frames are `messaging` links. One goes from the client's
  `publish`/`send` to `/app/x` to the `@MessageMapping("/x")` method. The
  other goes from a `@SendTo("/topic/y")` method to the client's
  `subscribe('/topic/y')`.

Main reads 0 of the 2 handshakes and 0 of the 4 frame links, with no false
positives. The clients are `new StompJs.Client({brokerURL: 'ws://…'})` and
ng2-stomp-service's `configure({host: '/stomp-websocket'})`. They connect
through a config object, never through a `WebSocket` or `SockJS` constructor,
and the browser's `publish`, `send` and `subscribe` calls are not read. The
server side of each frame, `@MessageMapping` and `@SendTo`, is read already.

jhipster/jhipster-sample-app-websocket (410a632) adds a generated
Angular and Spring app, labelled by hand from every `HttpClient`,
`httpResource` and STOMP call and every handler. It has 24 in-scope `http`
links (REST calls and the SockJS handshake) and 3 STOMP frames. Two more
clients have no handler and are listed under `unmatched_clients`:
`PATCH /api/admin/users/{login}` and `GET /api/users/{id}`. Calls to
Actuator endpoints and to Spring Security's login and logout filters have
no handler method, so they are out of scope.

Main reads none of them, with no false positives. Every REST URL is
`${serverApiUrl}api/...`, and `serverApiUrl` is a build-time define that
angular.json sets to `''`. The connect is `webSocketFactory: () =>
SockJS(this.buildUrl())`. Before #176 the whole contract analysis of this
repository failed on a comment inside import braces.

19EB/microservices-base-kafka (9facb7f) is a Node and kafkajs app with three
services, each of which pings the other two. It is labelled by hand in
`build_node_messaging.py`, from a repository the kafkajs reader's own
validation (#168) did not use.

Each service's `broker.ts` wraps `producer.send({topic})` in
`KafkaPublish(kafka, topic, payload)` and subscribes to its own constant
topic. The six labelled links run from that shared `send` line, with the
`KafkaPublish(kafka, '<topic>')` call as the producer invocation, to the
other service's `subscribe`. Main reads all three subscriptions but none of
the sends, because the topic is a parameter fixed only at the wrapper's
callers. So it links 0 of 6, with no false positives.

grpc's route_guide example has one rpc of each streaming kind (`GetFeature`
unary, `ListFeatures` server, `RecordRoute` client, `RouteChat` bidi). Its
Go, Java and Node copies are labelled by hand in `build_grpc_route_guide.py`,
from the code and the .proto, without the gRPC readers' code or tests (#162,
#164). Each corpus is rooted at the example (`root` in the manifest); other
examples in the same directory and protoc output are don't-care. Java's client
and server share a package, so a service's root may be one file. Node has two
copies, `static_codegen` (protoc output required as `services`) and
`dynamic_codegen` (proto-loader): both clients dial the same address, so each
copy's client file belongs to one client service and each server file to one
server service.

For gRPC the gate also scores two levels besides call sites and pairs:
`servers` (labelled server lines with a `HANDLES` edge for the contract) and
`streaming` (labelled rpcs whose `GrpcMethod` carries the labelled kind; a
different kind is wrong, none is unread). Main reads every link, server and
kind in all three: Go 4/4, Java 4/4, Node 10/10, streaming 4/4 each. Online
Boutique's servers are 15 of 20: the 5 misses are the C# cartservice, whose
servers are not read (C# is not a contract language yet).

### Routes and GraphQL fields

The contract corpora score links between services, so a route the engine
stops reading only shows up there if some labelled client calls it. The
manifest's `routes` corpora score the server side directly, against labels
the code does not produce:

| Corpus | Oracle | Expected | Found | Extra |
|---|---|---|---|---|
| nestjs-realworld-example-app (NestJS HTTP) | the RealWorld backend API spec | 19 | 19 | 0 |
| nestjs-prisma-starter (NestJS GraphQL, code-first) | its committed generated `schema.graphql` | 13 | 13 | 0 |
| howtographql/graphql-js (Apollo, resolver modules) | schema and resolver modules read by hand | 7 | 7 | 0 |

An HTTP route matches on method and path, with `{var}` for any parameter; the
global prefix (`setGlobalPrefix('api')`) has to be applied to match. A
GraphQL root field counts only when a resolver `HANDLES` it. Routes the app
declares beyond the spec (`GET /api`, `DELETE /api/users/{var}`, checked in
its controllers) are listed as neutral. graphql-js joins resolver modules
(`module.exports = { feed }`) into its map by name, which the engine
follows since #105; `Query.info` is declared with no resolver, so a link to
it would count as extra. The gate fails on fewer found, more
extra, or changed labels.

### ActiveRecord model tables

The data-contracts tool maps each Rails model to its table. The manifest's
`tables` corpora label that mapping with ActiveRecord's own naming code:
`tools/rails_table_oracle.rb` finds the model classes with Prism (nesting,
superclass, literal `self.table_name`, abstract classes, literal
`table_name_prefix` methods and assignments, and the app's literal
inflections). It rebuilds them on `ActiveRecord::Base` and asks each one its
`table_name`. It never loads or runs the app. Models whose table it cannot
know are set aside as `not_counted`: a computed `table_name` or prefix, or a
class declared only under `spec/` or `test/`.

| App | Pin | Models | Found | Wrong | Missed | Not counted |
|---|---|---|---|---|---|---|
| lobsters | d23941f | 42 | 42 | 0 | 0 | 0 |
| rubygems.org | 6f51e32 | 45 | 45 | 0 | 0 | 0 |
| mastodon | fe9d150 | 276 | 275 | 1 | 0 | 0 |
| forem | 1f13196 | 134 | 134 | 0 | 0 | 0 |
| discourse | 5fc74b5 | 402 | 398 | 0 | 4 | 27 |

The engine runs with its defaults on Ruby files only. The scores are:

- mastodon's one wrong model is `Mastodon::MigrationHelpers::Grant`, whose
  table is `information_schema.role_table_grants`. The engine drops the schema.
- discourse has 13,285 Ruby files, past the engine's default 10,000-file
  limit, so the models in the last files read (`Poll`, `PollOption`,
  `PollVote`, `ReviewableLiveUser`) are missed.
- forem's prefixed models (`GDPRDeleteRequest`, `Users::Setting` and three
  more) were found only after #126; before it, forem was 129 found, 1 wrong
  and 4 missed.

A model is found when the engine maps it to the labelled table and to no
other, and wrong when it maps it anywhere else. The gate fails on fewer
found, more wrong or extra, or changed labels. The labels are built offline,
`ruby tools/rails_table_oracle.rb CHECKOUT` with Ruby 3.3 and activerecord
7.2.2, and committed. `tests/test_rails_table_oracle.py` skips without them.

### Doctrine entity tables

The same gate covers PHP entities (`"language": "php"` on a `tables` corpus).
`tools/doctrine_table_oracle.php` reads every class under `src/` with PHP's
tokenizer. A class carrying `#[ORM\Entity]` (or `@ORM\Entity`) is an
entity. Its table is the literal `ORM\Table` name, positional or `name:`,
with Doctrine's backtick quoting removed. Without one, the table is the short
class name under the naming strategy named on the command line, as the
repository configures it in config/packages/doctrine.yaml. Literal
`ORM\JoinTable` names in its body are its join tables, which the engine may
also map the entity to.

An entity with `InheritanceType`, or one below such a root, is not counted,
because its table depends on the hierarchy. A label is then checked against
the schema the repository's own migrations build: `CREATE`, `RENAME` and
`DROP TABLE` statements and schema calls in each migration's `up()` (or
`mySQLUp()`), in version order, as Doctrine's schema tool wrote them from
the same mapping. A label whose table is not in that schema is reported, and
a corpus is committed only when every label and join table is there. This
check caught two oracle mistakes while the oracle was being built: a
positional `Table('`footprints`')` and backtick-quoted names.

| App | Pin | Naming | Entities | Found | Wrong | Missed | Not counted | Migrated tables with no entity |
|---|---|---|---|---|---|---|---|---|
| kimai | d46d79e | underscore_number_aware | 29 | 29 | 0 | 0 | 0 | 1 (`kimai2_sessions`, Symfony's session table) |
| Part-DB | 04aeda7 | underscore_number_aware | 28 | 28 | 0 | 0 | 54 (inheritance) | 8 (4 inheritance roots, 4 OAuth2 bundle tables) |

### Mongoose model collections

A `tables` corpus with `"language": "mongoose"` scores Mongoose models on
JavaScript and TypeScript. A model has no class, so it is named
`file::Model`, after the file that declares it and the name it is declared
under. `tools/mongoose_collection_oracle.cjs` finds the declarations with the
TypeScript compiler's parser:

- **Declarations:** `model('Name', schema[, collection])` called on the
  mongoose module, or on a connection made from it.
  - The module can be a default, namespace or `require` binding, or a
    parameter typed `typeof import('mongoose')`.
  - A connection is made by `createConnection`, `connection` or `useDb`.
  - The `model` named import counts too.
- **Collection:** the literal (or `const`) third argument, else the schema's
  literal `collection` option, else the model name through Mongoose's
  `toCollectionName`.
  - The schema can be `new Schema(...)` or `Schema(...)`, written inline,
    held in a variable, or imported through relative paths and tsconfig
    `paths`.
- **Discriminators:** a discriminator of a base declared in the same file
  takes the base's collection.
- **Default names:** the pluralize rules and uncountable words are read as
  text from the pluralize module of the Mongoose version the app's lockfile
  pins. That is `lib/helpers/pluralize.js` from Mongoose 6 on, and
  `mongoose-legacy-pluralize` before. Neither Mongoose nor the app is ever
  run.
- **Not counted:** a declaration whose collection the oracle can't fix is
  listed under `not_counted` with its reason. Examples are spread or
  non-literal options, a schema built by a helper function, a receiver that
  is not a known mongoose binding, and test sources. A global
  `mongoose.pluralize(...)` override would set aside every default name.

| App | Pin | Mongoose | Models | Found | Wrong | Missed | Not counted |
|---|---|---|---|---|---|---|---|
| Ackee | 7004370 | 9.11.2 | 6 | 6 | 0 | 0 | 0 |
| node-express-boilerplate | 179ae84 | 5.13.20 | 2 | 0 | 0 | 2 | 8 (tests) |
| LibreChat | 4700c44 | 8.24.9 | 69 | 0 | 0 | 69 | 95 (93 tests, 2 not fixed) |

The engine maps no model wrongly, but it misses two common forms:

- **node-express-boilerplate** builds both schemas with `mongoose.Schema(...)`
  and no `new`. Mongoose accepts this, but the engine leaves the model
  unresolved.
- **LibreChat** declares every model in a factory,
  `createXModel(mongoose: typeof import('mongoose'))`, so the receiver is a
  parameter rather than an import. The engine reads none of those models.

`tests/test_mongoose_collection_oracle.py` checks the oracle on a small app
when `TYPESCRIPT_DIR` points at a TypeScript package.

### MongoDB driver collections

Code also reaches MongoDB without a model, `<handle>.collection('name')`, and
the driver uses the name exactly as written. `tools/mongodb_driver_labels.py`
lists every such line outside tests and comments, and labels its access by the
driver's semantics: READS for `find`, `findOne`, `countDocuments`, `aggregate`
and the legacy `count`; WRITES for inserts, updates, deletes, bulk operations,
index changes and the legacy `insert`, `update`, `remove` and `save`; both for
`findOneAndUpdate` and `findAndModify`. A collection held in a variable takes
the methods called on it. A line it cannot classify must be given by hand
(`--override`), and every label was read by hand. Manifest `tables` entries with
`"language": "mongodb"` are scored per line: *found* when the engine's
`data-contracts.mongodb-collection` edges at that line name the collection with
exactly the labelled accesses.

| Corpus | Commit | Lines | Found | Wrong | Missed | Extra |
|---|---|---|---|---|---|---|
| NodeBB | 0218015 | 84 | 0 | 0 | 84 | 0 |
| Countly | 0da4abf | 1,148 | 16 | 0 | 1,132 | 0 |

Neither app was used to build the reader (#190). It reads nothing wrongly, but:

- **NodeBB** keeps its database handle in a module field (`module.client`,
  set from `client.db(...)` in `src/database/mongo.js`), and most of its files
  do not import `mongodb`.
- **Countly** uses a shared handle, `common.db`, `countlyDb` or `db`, in files
  that import none of `mongodb`, `mongoose` or `mongoskin`, so the import gate
  leaves almost all of its 80 collections unread.

### Knex query-builder tables

Knex names a table as a string argument of its query builder.
`tools/knex_labels.py` parses every JavaScript and TypeScript file outside
tests with tree-sitter and lists each literal table a Knex call names:
`H('t')` on a handle, `from`, `into` and `table` on a builder started from a
handle, the `join` family (always READS), `batchInsert` (WRITES), and
`schema.createTable` / `createTableIfNotExists` (DECLARES). A handle is a
name or property `knex`, `trx`, `transaction`, `connection`, `db` or
`transacting` (`#` and `_` prefixes aside), a name the file declares with
the type `Knex` or `Knex.Transaction`, or the parameter of a callback passed
to a handle's `transaction(...)`; every file's use of those names was checked
by hand. The parameter of a callback passed to a builder method
(`.whereIn('id', (q) => q.from('t'))`, `.union(...)`, `.whereExists(...)`)
and a name typed `Knex.QueryBuilder` are builders. A table may be a string
literal or a top-level `const` string, the file's own or imported by name
from a relative module. Access follows Knex's execution model: one query per
execution, which writes the table when its chain calls `insert`, `update`,
`del`, `delete`, `increment`, `decrement`, `truncate`, `upsert` or `merge`
and reads it otherwise; a builder held in a variable takes the union over the
chains later called on it. A name the file gives a common table expression
(`.with('x', ...)`) is no table. Other schema calls on a table (`alterTable`,
`hasTable`, ...) and `columnInfo` are listed under `not_counted`. Manifest
`tables` entries with `"language": "knex"` are scored per line like the
MongoDB driver: *found* when the engine's `data-contracts.knex-*` edges at
that line name exactly the labelled (table, access) pairs.

| Corpus | Commit | Lines | Found | Wrong | Missed | Extra |
|---|---|---|---|---|---|---|
| Ghost | 7ada5b0 | 873 | 642 | 0 | 231 | 0 |
| Backstage | b3ed969 | 399 | 328 | 1 | 70 | 0 |

The labels were made without reading the engine's Knex reader. Ghost reaches
Knex through migration callbacks whose untyped parameter is named
`connection` or `transaction`, through `options.transacting`, and through
its data seeders' `this.transaction` field, as well as through `knex`
itself. Backstage hands its transactions to callbacks named `tx` and keeps most
table names in constants, some imported from a sibling `tables.ts`. A
schema qualifier is dropped (`app.users` names `users`), since tables join
across readers by name, and a database's own catalog (`information_schema.*`,
`pg_catalog.*`, `pg_*`, `sqlite_*`, ...) is listed under `not_counted`.

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

The second entry ("Java typed receivers and chi corpus, 2026-11-24") follows
Go receiver types (#112), Python local bindings (#134) and Java typed
receivers (#128), and adds chi.
Impact recall rose where calls on receivers now resolve: cobra 0.024 to 0.214
(precision 0.160 to 0.193, 2.63 files per case), click 0.020 to 0.048 (0.263
to 0.095), gson 0.036 to 0.071 (0.084 to 0.049, 6.0 files per case). chi has
57 cases: impact 0.148 at 0.264 against search 0.141 at 0.346. Impact now
recalls more co-edited files than text search on cobra, chi and gson, at
lower precision on chi and gson. Entries recorded before a corpus was pinned
leave it out; the latest entry covers every corpus.

The third entry ("JavaScript corpus preact, 2026-11-24") adds preact: 58
cases, impact 0.099 recall at 0.343 precision (1.16 files per case) against
search 0.150 at 0.055 (10.98 files per case).

The fourth entry ("TypeScript corpus hono, 2026-11-24") adds hono: 41
cases, impact 0.141 recall at 0.233 precision (1.46 files per case) against
search 0.515 at 0.091 (13.63 files per case).

### Impact ground truth

The replay scores impact against co-edits, which also measures whether
developers had to touch a dependent at all. `tools/impact_truth.py` isolates
the first question: for declarations real commits edited, which declarations
call them, directly or within three hops, according to the oracle?

Seeds are the innermost callables that hold a line one of the replay's
commits (the last 30 qualifying commits up to the pin) wrote and that `git
blame` at the pin still attributes to that commit. The oracle graph comes
from the corpus's SCIP call truth and the index's definition spans: a call
is an edge from the innermost callable whose body holds it to each callable
the index says it targets. Two indexer gaps are filled from the source:
scip-python gives a span only to the first `@overload` of a function, so its
other `def`s (implementation included) are folded into that node with spans
found by indentation; and scip-typescript records `const f = () => ...` as a
variable, so a variable whose initializer is a function counts as a callable.

`check` runs the same reverse search over the engine's linked CALLS edges at
the pin. An engine dependent the oracle also reaches is *confirmed*; one it
does not reach is *extra* (a wrong link, or a caller reached through one).
The gate fails on fewer confirmed, more extra, or changed seeds. TypeScript
and JavaScript truths also list every oracle callable (`# node` rows): an
engine dependent that is none of them, a declaration the index does not name,
is *unjudged* rather than extra, and the search still goes through it.
An engine dependent in a file the call oracle did not index (a Go file behind
a build tag the index was not built with) is unjudged for the same reason:
the oracle never saw its calls.

Engine declarations are matched to oracle nodes by name and span. The truth
file lists only seeds and their dependents, so a declaration with no node
there falls back to the one node of its name in the file (an overload the
oracle folds into its first declaration), but never to a node another
declaration already spans: a function `run` is not merged with a method
`C.run`, nor one struct's `new` with another's.

| Corpus | Seeds | Oracle dependents | Confirmed | Extra | Recall | Precision |
|---|---|---|---|---|---|---|
| click | 144 | 1080 | 535 | 0 | 0.495 | 1.0 |
| ky | 38 | 175 | 159 | 0 | 0.909 | 1.0 |
| hono | 40 | 108 | 100 | 0 | 0.926 | 1.0 |
| cobra | 107 | 1764 | 1275 | 0 | 0.723 | 1.0 |
| chi | 87 | 399 | 316 | 0 | 0.792 | 1.0 |
| preact | 28 | 73 | 69 | 0 | 0.945 | 1.0 |
| gson | 164 | 5070 | 3515 | 0 | 0.693 | 1.0 |
| anyhow | 45 | 171 | 97 | 9 | 0.567 | 0.915 |
| ripgrep | 277 | 1903 | 453 | 1 | 0.238 | 0.998 |

Recall falls with depth because each missing link cuts off everything behind
it.
Outside Python, a call on a callable's name line belongs to it: Go's
`func (ns nodes) Sort() { sort.Sort(ns); ns.tailSort() }` calls `tailSort`
from `Sort`. Python keeps that line for the enclosing function, because it
holds decorators and default values, so a Python one-line body is not judged.
chi is pinned with `_examples/` excluded, since the Go toolchain and scip-go
ignore directories that start with `_`. Without the exclusion, the engine's
callers there would count as extra with nothing to judge them by.
scip-typescript makes a function declared inside another one a local
symbol (`const onSuspensionComplete = () => ...` in a method of preact's
`compat/src/suspense.js`), so the oracle has no node for it and counts its
calls as the enclosing function's. For TypeScript and JavaScript corpora the
scorer reads such an engine declaration, one that matches no oracle node, as
the innermost engine declaration around it when that one is a node of the
truth. This took ky from 5 extra to 0 and preact from 3 extra to 0 (2 more
confirmed), with no truth change.

The oracle also counts as callables a variable or class property whose
initializer is a function even when it starts on a later line or has nested
type parameters (`export const hc = <T extends Hono<any, any, any>>(`), and
counts a tagged template (`html\`...\``) as a call of its tag. hono showed
all three. The ky and preact truths derive unchanged.

hono's 46 unjudged dependents are declarations the index does not name:
functions local to an anonymous test callback (`const anotherApp = ...`
inside `describe(...)`), methods of an object literal (`websocket.open`) and
the class `ClientRequestImpl`. Before the `# node` rows they counted as 49
extra. A type annotation or type parameter list is read with its brackets
balanced, so `export const validator = <..., VF extends (v) => any = ...>(`
and `export const form: FC<...> = (...) =>` are callables. Its 23 call
contradictions are the sibling-scope binding fixed by #159.
A decorator call runs where the decorated function is declared, not in its
body. scip-python's range for a function starts at its first decorator, so
the lines from there to the `def` line belong to the enclosing function: a
test that runs `@cli.command()` on a nested def depends on `Group.command`,
and the nested def does not.
ripgrep's one extra is a caller of the `#[cfg(windows)]` `from_path`, which
the Linux index does not analyze, and 84 of its seeds are test functions
that macros expand to, which the engine does not declare. scip-go
records no definition spans, so cobra's come from Go's own parser
(`go run tools/go_function_spans.go WORKSPACE`, passed as `--spans`); its
low recall is mostly method calls on a receiver, such as `c.ExecuteC()` in
`Execute`, left unresolved. gson's call truth comes from javac, not an
index, so its callables are the method and constructor spans of javac's
parser (`java tools/JavaMethodSpans.java WORKSPACE`, passed with `-` as the
index). The call truth leaves out `this(...)` and `super(...)`, which name
no declaration, but a constructor that delegates depends on the one it
calls (`new Gson()` runs `Gson(GsonBuilder)` through `this(...)`), so
`derive-java --chains` lists them and they join the impact graph as
`--extra-calls`. A call javac bound by error recovery (the proto classes
are missing) is not ruled on: `derive-java --recovered` lists each one against
every overload of the name it bound, the impact truth is derived a second time
with those calls added (`--possible-calls`), and a dependent only that wider
graph reaches is written as an `# unjudged` row for its seed. An engine
dependent on such a row counts as unjudged, not extra; 238 rows cover gson's
proto tests, and 4 engine dependents land on them: the test methods at
`LegacyProtoTypeAdapterFactoryTest.java:342` and `:418`, as dependents of
`Gson.toJson(Object, Type)` and `JsonElement.newJsonStringBuilder()`.
A field initializer or initializer block runs when its class is constructed
or loaded, and is in no method or constructor span. `JavaMethodSpans.java
--initializers` lists them, and impact_truth.py (`--initializers`) makes the
class the caller of their calls, unless a method inside them (an anonymous
class's) holds the call. A class is only ever a caller: never a seed or a
target. This adds 18 dependents to gson's truth, such as `miniGson.getAdapter(...)`
at `StreamingTypeAdaptersTest.java:43` making the class a dependent of
`Gson.getAdapter`. The engine reads that one already, and
`gson.getAdapter(Object.class)` at `ObjectTypeAdapterTest.java:33`; they were
gson's last extras.
