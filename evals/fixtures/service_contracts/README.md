# Cross-service contract links: ground truth

This folder holds ground truth for checking a cross-service contract-link extractor:
HTTP client to route handler, gRPC stub call to rpc implementation, and messaging
producer to consumer. It was built separately from the extractor. Nobody involved
looked at extractor code, and the labels were not designed around what a pattern
matcher could find.

## Corpora

| File | Repo @ pinned commit | License | Languages | Kinds |
|---|---|---|---|---|
| online-boutique.json | GoogleCloudPlatform/microservices-demo @ c7cb6d5bfd6ea3aed768634ebbcb98cb7a523712 | Apache-2.0 | Go, C#, Node.js, Python, Java | gRPC, HTTP (Python Locust -> Go mux) |
| robot-shop.json | instana/robot-shop @ 55292e2199f2fb00a165b1f7d3045fe7f8922038 | Apache-2.0 | Node/Express, AngularJS, Python Flask/requests/pika/Locust, Java Spring + HttpClient, Go amqp, PHP | HTTP, RabbitMQ |
| msecend-ewolff-microservice-kafka.json | ewolff/microservice-kafka @ 6fd9cc6aba3109f0e9c031ce4555b1a144b7a0d0 | Apache-2.0 | Java Spring Kafka | Kafka |
| msecend-piggymetrics.json | sqshq/piggymetrics @ fd5ee3c555ea9cd6067eacf3f2a3e8b85fe4fe77 | MIT | Java Spring Cloud (Feign, Zuul, Hystrix stream) | HTTP (Feign), framework RabbitMQ |
| msecend-spring-petclinic-microservices.json | spring-petclinic/spring-petclinic-microservices @ 0ab2e953e0e23761593a80cd17d7525e3ae32f9d | Apache-2.0 | Java WebClient, Spring Cloud Gateway, AngularJS | HTTP |
| msecend-apssouza22-java-microservice.json | apssouza22/java-microservice @ 9274eeb9189dfea0722ac0263814822b94b83ecc | MIT | Java Feign, Spring Cloud Stream (Kafka) | HTTP (Feign), Kafka |

The `msecend-*` corpora come from the microSecEnD dataset (github.com/tuhh-softsec/microSecEnD,
Apache-2.0; Schneider, Özen, Chen, Scandariato, MSR 2023, doi:10.1109/MSR59073.2023.00030).
Each one is pinned to the commit the dataset's model (`dataset/<app>/<app>.py`) cites in its
artifact URLs. Clone each repository at its pinned commit into `$CORPORA/<dir>` to rerun `build.py`.

## Method

1. **Service-level facts** come from published sources: Online Boutique's `protos/demo.proto`,
   its README architecture table and the `*_SERVICE_ADDR` env vars in `kubernetes-manifests`;
   robot-shop's README, docker-compose and nginx template; the microSecEnD DFDs.
2. **Call-site facts** come from manually reading every outbound call and every route or
   consumer at the pinned commit. This includes the server context-path from config
   (piggymetrics), gateway route rewriting (petclinic StripPrefix=2, robot-shop nginx
   `/api/<svc>/`), class-level `@RequestMapping` prefixes, and stream bindings in properties
   files (apssouza22).
3. **Line numbers** are computed by `build.py` (kept with this README) using exact-substring
   matches against the checked-out files. Each match must be unique (or the nth match must be
   named), so every client and server line in the JSONs was checked against the pinned commit.
   Run `CORPORA=<clones> python3 build.py <outdir>` to regenerate.
4. **microSecEnD labels** were used only to cross-check service pairs. Links were derived again
   from source. Every entry in each `*_traceability.json` was resolved against the pinned
   checkout, and the application-level edges were read by hand. Disagreements are listed in
   each corpus's `notes`.

## JSON fields beyond the requested schema

- `hop`: how the call reaches its target.
  - `direct`: service code calls the service.
  - `frontend_via_gateway` / `frontend_via_proxy`: browser or load-generator JS/Python calls a
    gateway, which rewrites the path to a backend. `to_service` and `server` are the backend.
    The backend path is static (route plus literal) but only after route rewriting.
  - `gateway_route`: the route declaration itself. Its `server` is null.
- `label_confidence`: `high` or `ambiguous`.
- `contract_def`: the gRPC rpc line in the proto.
- `client_invocations`: for Feign and WebClient wrappers, the places in business code where the
  client method is invoked. `client` is the Feign method annotation or the WebClient `.uri(` line.
- `client_url_def`, `producer_invocation`, `consumer_binding`, `topic_config`: where a URL or
  destination is defined, when that is not the call line.
- `server_alternatives`: other server lines that also count as correct.
- `dont_care_call_sites`: call sites that are not labelled. Examples: developer test clients
  inside a service, external APIs, intra-service browser calls, unlabelled UI code. A predicted
  link from these sites is neither a true positive nor a false positive.
- `unmatched_clients` (apssouza22): a client with no server. Any link predicted from it is a
  false positive.
- `summary`: link counts per kind (in scope / out of scope) and the in-scope service pairs.

## How to score (recommended)

- **In-scope set:** links with `in_scope_static: true`. These count for recall. A predicted link
  that matches an out-of-scope link (`in_scope_static: false`) counts as neither a true positive
  nor a false positive.
- **Service-pair level:** distinct (`from_service`, `to_service`, `kind`) tuples. Map the
  extractor's service names to `services[].root` directories.
- **Call-site level:** a match needs the same kind, client file, client line within ±3, and a
  server file and line within ±3 (for messaging, the consumer). Also report HTTP method/path
  agreement after normalising path parameters (`{id}` = `:id` = `<id>` = `*`).
- Report results twice: (a) `hop == direct` only, and (b) all in-scope links. Gateway and proxy
  rewriting is much harder and should not hide direct-call performance.
- Online Boutique: `SearchProducts` and `grpc.health.v1` have servers but no callers, so a link
  to them is a false positive.

## Caveats

- **Line convention:** for a chained multi-line call, the client line is the line with the RPC
  method name, the `.uri(`, or the `$http(` token. The server line is the route annotation,
  decorator or registration line, or the method definition for gRPC.
- **One labeller:** the labels have not been checked by a second person. The most likely errors
  are omissions in robot-shop's `controller.js` (20 sites were enumerated by `$http({`) and in
  the Online Boutique frontend.
- **Out of scope by design:** framework-implicit traffic is labelled `in_scope_static: false`.
  This covers OAuth2 user-info and token calls, Hystrix/Turbine stream, Spring Cloud Bus, and
  service discovery and config-server traffic. Most infrastructure flows (config, Eureka, DBs,
  logging) are omitted entirely.
- **Environment hosts:** most host names come from environment variables. Where the code or the
  Dockerfile gives a literal default that equals the service name, the link is in scope. The
  Online Boutique frontend -> shoppingassistant link, which is env-only, is out of scope.
  gRPC links are identified by the typed stub, not the address.
- **PHP:** robot-shop `ratings` is PHP. If the extractor does not support PHP, score those links
  as unsupported rather than as misses.
- **Not labelled:** piggymetrics gateway static JS (`login.js` is a single CR-delimited line) is
  marked don't-care.
- **Online Boutique commit:** it is pinned to the HEAD at clone time (2026-10-27 commit). Other
  commits will change line numbers.
