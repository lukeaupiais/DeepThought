"""Build the WebSocket / STOMP contract-link ground truth JSONs.

Labelled by reading each repository at its pinned commit, separately from
the WebSocket and STOMP readers (#151 and after): nobody looked at their code
or tests, and the repositories are not the ones their validation used.
Line numbers are located as in build.py: exact substring match, asserted
unique (or nth). Run `CORPORA=<clones> python3 build_websocket.py <outdir>`.
"""

import json
import os
import subprocess
import sys

C = os.environ.get("CORPORA", "corpora")
OUT = sys.argv[1]


def head(repo):
    return subprocess.check_output(
        ["git", "-C", f"{C}/{repo}", "rev-parse", "HEAD"], text=True
    ).strip()


class Corpus:
    def __init__(self, repo_dir, **meta):
        self.dir = repo_dir
        self.meta = meta
        self.links = []
        self.dont_care = []

    def loc(self, f, pat, n=None):
        lines = open(f"{C}/{self.dir}/{f}", encoding="utf-8").read().split("\n")
        hits = [i + 1 for i, line in enumerate(lines) if pat in line]
        if n is None:
            assert len(hits) == 1, (self.dir, f, pat, hits)
            return {"file": f, "line": hits[0]}
        assert len(hits) >= n, (self.dir, f, pat, hits)
        return {"file": f, "line": hits[n - 1]}

    def link(self, kind, frm, to, contract, client, server, evidence, reason=None, **extra):
        item = {
            "kind": kind,
            "from_service": frm,
            "to_service": to,
            "contract": contract,
            "client": client,
            "server": server,
            "evidence": evidence,
            "in_scope_static": reason is None,
        }
        if reason is not None:
            item["out_of_scope_reason"] = reason
        item.update({"hop": "direct", "label_confidence": "high", **extra})
        self.links.append(item)

    def dc(self, where, why):
        self.dont_care.append({**where, "reason": why})

    def write(self):
        m = dict(self.meta)
        m["commit"] = head(self.dir)
        for i, link in enumerate(self.links):
            link["id"] = f"{m['corpus']}#{i + 1}"
        summary = {
            k: {
                "in_scope": sum(1 for x in self.links if x["kind"] == k and x["in_scope_static"]),
                "out_of_scope": sum(
                    1 for x in self.links if x["kind"] == k and not x["in_scope_static"]
                ),
            }
            for k in ("http", "grpc", "messaging")
        }
        pairs = sorted(
            {(x["from_service"], x["to_service"], x["kind"]) for x in self.links if x["in_scope_static"]}
        )
        summary["service_pairs_in_scope"] = [list(p) for p in pairs]
        m["summary"] = summary
        m["links"] = self.links
        m["dont_care_call_sites"] = self.dont_care
        order = ["corpus", "repo", "commit", "license", "languages", "sources",
                 "services", "summary", "links", "dont_care_call_sites", "notes"]
        m = {k: m[k] for k in order if k in m} | {k: v for k, v in m.items() if k not in order}
        with open(f"{OUT}/{m['corpus']}.json", "w") as fh:
            json.dump(m, fh, indent=2)
            fh.write("\n")
        print(m["corpus"], m["commit"], len(self.links), "links")


STOMP_NOTES = [
    "A WebSocket connection opens with an HTTP GET handshake, so the client's "
    "connect is an http link to the endpoint path (contract GET /path); its "
    "server is the addEndpoint(...) line of registerStompEndpoints, with the "
    "method's declaration as an alternative.",
    "STOMP frames are messaging links, producer to consumer. A client SEND to "
    "/app/x (the application destination prefix from "
    "setApplicationDestinationPrefixes) is consumed by @MessageMapping(\"/x\"); "
    "a reply via @SendTo(\"/topic/y\") is produced by the annotated method and "
    "consumed by the client's subscribe('/topic/y'). Destinations are written "
    "in full, prefix included.",
]

# ------------------------------------------------- spring-guides STOMP guide
GS = "complete/src/main/resources/static/app.js"
GSJ = "complete/src/main/java/com/example/messagingstompwebsocket"
gs = Corpus(
    "gs-messaging-stomp-websocket",
    corpus="websocket-gs-messaging-stomp",
    repo="https://github.com/spring-guides/gs-messaging-stomp-websocket",
    license="Apache-2.0",
    languages=["Java (Spring WebSocket, STOMP)", "JavaScript (@stomp/stompjs)"],
    sources=[
        "README.adoc (the guide text: endpoint, prefixes and destinations it sets up)",
        "manual reading of complete/ at the pinned commit",
    ],
    services=[
        {"name": "browser", "root": "complete/src/main/resources/static"},
        {"name": "server", "root": "complete/src/main/java"},
    ],
    notes=STOMP_NOTES
    + [
        "The client is new StompJs.Client({brokerURL: 'ws://localhost:8080/gs-guide-websocket'}): "
        "the URL is a literal ws:// URL in a config object, not a WebSocket or SockJS constructor.",
        "initial/ is the guide's starting point and holds no WebSocket code; the "
        "integration test's connectAsync(\"ws://localhost:{port}/gs-guide-websocket\") is a test client.",
    ],
)
GS_ENDPOINT = gs.loc(f"{GSJ}/WebSocketConfig.java", 'registry.addEndpoint("/gs-guide-websocket");')
gs.link(
    "http", "browser", "server", "GET /gs-guide-websocket",
    gs.loc(GS, "brokerURL: 'ws://localhost:8080/gs-guide-websocket'"), GS_ENDPOINT,
    "@stomp/stompjs Client brokerURL (literal ws:// URL) -> registry.addEndpoint",
    server_alternatives=[gs.loc(f"{GSJ}/WebSocketConfig.java", "public void registerStompEndpoints")],
)
gs.link(
    "messaging", "browser", "server", "stomp-destination:/app/hello",
    gs.loc(GS, "stompClient.publish({"), gs.loc(f"{GSJ}/GreetingController.java", '@MessageMapping("/hello")'),
    "publish({destination: '/app/hello'}) -> @MessageMapping(\"/hello\") under prefix /app",
    client_url_def=gs.loc(GS, 'destination: "/app/hello"'),
)
gs.link(
    "messaging", "server", "browser", "stomp-destination:/topic/greetings",
    gs.loc(f"{GSJ}/GreetingController.java", '@SendTo("/topic/greetings")'),
    gs.loc(GS, "stompClient.subscribe('/topic/greetings'"),
    "@SendTo(\"/topic/greetings\") (simple broker /topic) -> subscribe('/topic/greetings')",
)
gs.dc({"file": "initial"}, "the guide's starting project")
gs.dc({"file": "complete/src/test"}, "integration test client")
gs.write()

# ------------------------------------------ Angular 2 port of the same guide
NG = "src/main/ng/src/app/app.component.ts"
NGJ = "src/main/java/com/example"
ng = Corpus(
    "spring-boot-angular2-stomp",
    corpus="websocket-spring-boot-angular2-stomp",
    repo="https://github.com/chriswhite199/spring-boot-angular2-stomp",
    license="Apache-2.0",
    languages=["Java (Spring WebSocket, STOMP, SockJS)", "TypeScript (Angular, ng2-stomp-service)"],
    sources=["README.md", "manual reading of src/ at the pinned commit"],
    services=[
        {"name": "browser", "root": "src/main/ng"},
        {"name": "server", "root": "src/main/java"},
    ],
    notes=STOMP_NOTES
    + [
        "The client is ng2-stomp-service's StompService, configured with "
        "stomp.configure({host: '/stomp-websocket'}) (a relative SockJS URL); "
        "send and subscribe are methods of the same injected service.",
    ],
)
NG_ENDPOINT = ng.loc(f"{NGJ}/WebSocketConfig.java", 'registry.addEndpoint("/stomp-websocket")')
ng.link(
    "http", "browser", "server", "GET /stomp-websocket",
    ng.loc(NG, "stomp.configure({host: '/stomp-websocket'});"), NG_ENDPOINT,
    "ng2-stomp-service configure({host}) (SockJS, relative URL) -> registry.addEndpoint(...).withSockJS()",
    server_alternatives=[ng.loc(f"{NGJ}/WebSocketConfig.java", "public void registerStompEndpoints")],
)
ng.link(
    "messaging", "browser", "server", "stomp-destination:/app/hello",
    ng.loc(NG, "this.stomp.send('/app/hello'"), ng.loc(f"{NGJ}/WebSocketController.java", '@MessageMapping("/hello")'),
    "StompService.send('/app/hello', ...) -> @MessageMapping(\"/hello\") under prefix /app",
)
ng.link(
    "messaging", "server", "browser", "stomp-destination:/topic/greetings",
    ng.loc(f"{NGJ}/WebSocketController.java", '@SendTo("/topic/greetings")'),
    ng.loc(NG, "this.stomp.subscribe('/topic/greetings'"),
    "@SendTo(\"/topic/greetings\") -> StompService.subscribe('/topic/greetings')",
)
ng.dc({"file": "src/main/ng/e2e"}, "Protractor end-to-end tests")
ng.write()

# ------------------------------------------- JHipster sample app (websocket)
W = "src/main/webapp/app"
R = "src/main/java/io/github/jhipster/sample/web/rest"
ACC, AUTH, USR, PUB = f"{R}/AccountResource.java", f"{R}/AuthorityResource.java", f"{R}/UserResource.java", f"{R}/PublicUserResource.java"
TRK = f"{W}/core/tracker/tracker.service.ts"
ACT = "src/main/java/io/github/jhipster/sample/web/websocket/ActivityService.java"
UM = f"{W}/entities/admin/user-management/service/user-management.service.ts"
AS = f"{W}/entities/admin/authority/service/authority.service.ts"
US = f"{W}/entities/user/service/user.service.ts"
jh = Corpus(
    "jhipster-sample-app-websocket",
    corpus="jhipster-sample-app-websocket",
    repo="https://github.com/jhipster/jhipster-sample-app-websocket",
    license="Apache-2.0",
    languages=["Java (Spring MVC, Spring WebSocket, STOMP, SockJS)", "TypeScript (Angular HttpClient, httpResource, @stomp/rx-stomp)"],
    sources=[
        "manual reading of every HttpClient, httpResource and STOMP call in src/main/webapp and every "
        "@RestController, WebSocket and STOMP handler in src/main/java at the pinned commit",
        "angular.json and build-plugins/define-esbuild.ts: SERVER_API_URL is defined as '' at build time",
    ],
    services=[
        {"name": "webapp", "root": "src/main/webapp"},
        {"name": "server", "root": "src/main/java"},
    ],
    notes=STOMP_NOTES
    + [
        "Every REST URL is `${serverApiUrl}api/...` with serverApiUrl = SERVER_API_URL, which "
        "angular.json and build-plugins/define-esbuild.ts define as ''. The relative URL resolves "
        "against <base href=\"/\"> in index.html, so the path is /api/....",
        "Services keep the base in a field (resourceUrl, infoUrl); httpResource(() => ({url: "
        "this.resourceUrl, params})) is a GET of that URL. client.line is the line holding the "
        "method name (.get/.post/...), or httpResource<.",
        "The WebSocket URL is '/websocket/tracker', set in buildUrl() (client_url_def) and passed "
        "through Location.prepareExternalUrl (base href '/'); the connect is "
        "webSocketFactory: () => SockJS(this.buildUrl()), SockJS called without new.",
        "No application destination prefix is set, so a SEND to /topic/activity reaches "
        "@MessageMapping(\"/topic/activity\") (and the simple broker). /topic/tracker has two "
        "producers: @SendTo on sendActivity and convertAndSend in the disconnect listener.",
        "management/* calls go to Spring Boot Actuator endpoints and /api/authentication, "
        "/api/logout to Spring Security filters configured in SecurityConfiguration: there is "
        "no handler method, so they are out of scope.",
    ],
)


def rest(client, method, path, server, evidence=None, **extra):
    jh.link("http", "webapp", "server", f"{method} {path}", client, server,
            evidence or "Angular HttpClient, `${serverApiUrl}` + literal path", **extra)


rest(jh.loc(f"{W}/account/register/register.service.ts", "this.http.post("), "POST", "/api/register", jh.loc(ACC, '@PostMapping("/register")'))
rest(jh.loc(f"{W}/account/activate/activate.service.ts", "this.http.get("), "GET", "/api/activate", jh.loc(ACC, '@GetMapping("/activate")'))
rest(jh.loc(f"{W}/account/password-reset/finish/password-reset-finish.service.ts", "this.http.post("), "POST", "/api/account/reset-password/finish", jh.loc(ACC, '"/account/reset-password/finish"'))
rest(jh.loc(f"{W}/account/password-reset/init/password-reset-init.service.ts", "this.http.post("), "POST", "/api/account/reset-password/init", jh.loc(ACC, '"/account/reset-password/init"'))
rest(jh.loc(f"{W}/account/password/password.service.ts", "this.http.post("), "POST", "/api/account/change-password", jh.loc(ACC, '"/account/change-password"'))
SES = f"{W}/account/sessions/sessions.service.ts"
rest(jh.loc(SES, "this.http.get<"), "GET", "/api/account/sessions", jh.loc(ACC, '@GetMapping("/account/sessions")'), client_url_def=jh.loc(SES, "private resourceUrl ="))
rest(jh.loc(SES, "this.http.delete("), "DELETE", "/api/account/sessions/{series}", jh.loc(ACC, '@DeleteMapping("/account/sessions/{series}")'), client_url_def=jh.loc(SES, "private resourceUrl ="))
AUS = f"{W}/core/auth/account.service.ts"
rest(jh.loc(AUS, "this.http.post("), "POST", "/api/account", jh.loc(ACC, '@PostMapping("/account")'))
rest(jh.loc(AUS, "this.http.get<Account>"), "GET", "/api/account", jh.loc(ACC, '@GetMapping("/account")'))
SESS = f"{W}/core/auth/auth-session.service.ts"
rest(jh.loc(SESS, "this.http.get(`${serverApiUrl}api/account`)"), "GET", "/api/account", jh.loc(ACC, '@GetMapping("/account")'))
SEC = "src/main/java/io/github/jhipster/sample/config/SecurityConfiguration.java"
rest(jh.loc(SESS, "api/authentication"), "POST", "/api/authentication", jh.loc(SEC, '.loginProcessingUrl("/api/authentication")'),
     reason="Spring Security form-login filter (loginProcessingUrl), no handler method")
rest(jh.loc(SESS, "api/logout"), "POST", "/api/logout", jh.loc(SEC, 'logout.logoutUrl("/api/logout")'),
     reason="Spring Security logout filter (logoutUrl), no handler method")
for client, method, path, server in [
    (jh.loc(AS, "this.http.post<"), "POST", "/api/authorities", jh.loc(AUTH, '@PostMapping("")')),
    (jh.loc(AS, "this.http.get<IAuthority>("), "GET", "/api/authorities/{id}", jh.loc(AUTH, '@GetMapping("/{id}")')),
    (jh.loc(AS, "this.http.get<IAuthority[]>("), "GET", "/api/authorities", jh.loc(AUTH, '@GetMapping("")')),
    (jh.loc(AS, "httpResource<"), "GET", "/api/authorities", jh.loc(AUTH, '@GetMapping("")')),
    (jh.loc(AS, "this.http.delete<"), "DELETE", "/api/authorities/{id}", jh.loc(AUTH, '@DeleteMapping("/{id}")')),
    (jh.loc(UM, "this.http.post<"), "POST", "/api/admin/users", jh.loc(USR, '@PostMapping("/users")')),
    (jh.loc(UM, ".put<RestUserManagement>"), "PUT", "/api/admin/users/{login}", jh.loc(USR, '@PutMapping({ "/users", "/users/{login}" })')),
    (jh.loc(UM, ".get<RestUserManagement>("), "GET", "/api/admin/users/{login}", jh.loc(USR, '@GetMapping("/users/{login}")')),
    (jh.loc(UM, ".get<RestUserManagement[]>("), "GET", "/api/admin/users", jh.loc(USR, '@GetMapping("/users")')),
    (jh.loc(UM, "httpResource<"), "GET", "/api/admin/users", jh.loc(USR, '@GetMapping("/users")')),
    (jh.loc(UM, "this.http.delete<"), "DELETE", "/api/admin/users/{login}", jh.loc(USR, '@DeleteMapping("/users/{login}")')),
    (jh.loc(US, "this.http.get<IUser[]>("), "GET", "/api/users", jh.loc(PUB, '@GetMapping("/users")')),
    (jh.loc(US, "httpResource<"), "GET", "/api/users", jh.loc(PUB, '@GetMapping("/users")')),
]:
    rest(client, method, path, server)
for f, pat, method, path in [
    (f"{W}/admin/metrics/metrics.service.ts", "management/jhimetrics", "GET", "/management/jhimetrics"),
    (f"{W}/admin/metrics/metrics.service.ts", "management/threaddump", "GET", "/management/threaddump"),
    (f"{W}/admin/configuration/configuration.service.ts", "management/configprops", "GET", "/management/configprops"),
    (f"{W}/admin/configuration/configuration.service.ts", "management/env", "GET", "/management/env"),
    (f"{W}/admin/logs/logs.service.ts", "this.http.post(", "POST", "/management/loggers/{name}"),
    (f"{W}/admin/logs/logs.service.ts", "this.http.get<LoggersResponse>", "GET", "/management/loggers"),
    (f"{W}/admin/health/health.service.ts", "management/health", "GET", "/management/health"),
    (f"{W}/layouts/profiles/profile.service.ts", "this.http.get<InfoResponse>(this.infoUrl)", "GET", "/management/info"),
]:
    rest(jh.loc(f, pat), method, path, None, reason="Spring Boot Actuator endpoint, no handler in the repository")
jh.meta["unmatched_clients"] = [
    {"kind": "http", "from_service": "webapp", "contract": "PATCH /api/admin/users/{login}",
     "client": jh.loc(UM, ".patch<RestUserManagement>"),
     "reason": "UserResource maps PUT /users and /users/{login} but no PATCH; any link from it is a false positive"},
    {"kind": "http", "from_service": "webapp", "contract": "GET /api/users/{id}",
     "client": jh.loc(US, "this.http.get<IUser>("),
     "reason": "PublicUserResource maps only GET /api/users; any link from it is a false positive"},
]
jh.link(
    "http", "webapp", "server", "GET /websocket/tracker",
    jh.loc(TRK, "webSocketFactory: () => SockJS(this.buildUrl()),"),
    jh.loc("src/main/java/io/github/jhipster/sample/config/WebsocketConfiguration.java", '.addEndpoint("/websocket/tracker")'),
    "@stomp/rx-stomp webSocketFactory: SockJS(buildUrl()), url '/websocket/tracker' through Location.prepareExternalUrl",
    client_url_def=jh.loc(TRK, "let url = '/websocket/tracker';"),
    server_alternatives=[jh.loc("src/main/java/io/github/jhipster/sample/config/WebsocketConfiguration.java", "public void registerStompEndpoints")],
)
jh.link(
    "messaging", "webapp", "server", "stomp-destination:/topic/activity",
    jh.loc(TRK, "this.stomp.publish({"), jh.loc(ACT, '@MessageMapping("/topic/activity")'),
    "RxStomp.publish({destination: DESTINATION_ACTIVITY}) -> @MessageMapping(\"/topic/activity\")",
    client_url_def=jh.loc(TRK, "const DESTINATION_ACTIVITY = '/topic/activity';"),
)
jh.link(
    "messaging", "server", "webapp", "stomp-destination:/topic/tracker",
    jh.loc(ACT, '@SendTo("/topic/tracker")'), jh.loc(TRK, ".watch(DESTINATION_TRACKER)"),
    "@SendTo(\"/topic/tracker\") -> RxStomp.watch(DESTINATION_TRACKER)",
    client_url_def=jh.loc(TRK, "const DESTINATION_TRACKER = '/topic/tracker';"),
)
jh.link(
    "messaging", "server", "webapp", "stomp-destination:/topic/tracker",
    jh.loc(ACT, 'messagingTemplate.convertAndSend("/topic/tracker"'), jh.loc(TRK, ".watch(DESTINATION_TRACKER)"),
    "SimpMessageSendingOperations.convertAndSend(\"/topic/tracker\") on SessionDisconnectEvent -> RxStomp.watch",
)
jh.dc({"file": "src/test"}, "server tests")
jh.write()
