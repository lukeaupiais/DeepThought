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

    def link(self, kind, frm, to, contract, client, server, evidence, **extra):
        self.links.append(
            {
                "kind": kind,
                "from_service": frm,
                "to_service": to,
                "contract": contract,
                "client": client,
                "server": server,
                "evidence": evidence,
                "in_scope_static": True,
                "hop": "direct",
                "label_confidence": "high",
                **extra,
            }
        )

    def dc(self, where, why):
        self.dont_care.append({**where, "reason": why})

    def write(self):
        m = dict(self.meta)
        m["commit"] = head(self.dir)
        for i, link in enumerate(self.links):
            link["id"] = f"{m['corpus']}#{i + 1}"
        summary = {
            k: {
                "in_scope": sum(1 for x in self.links if x["kind"] == k),
                "out_of_scope": 0,
            }
            for k in ("http", "grpc", "messaging")
        }
        pairs = sorted({(x["from_service"], x["to_service"], x["kind"]) for x in self.links})
        summary["service_pairs_in_scope"] = [list(p) for p in pairs]
        m["summary"] = summary
        m["links"] = self.links
        m["dont_care_call_sites"] = self.dont_care
        order = ["corpus", "repo", "commit", "license", "languages", "sources",
                 "services", "summary", "links", "dont_care_call_sites", "notes"]
        m = {k: m[k] for k in order if k in m}
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
