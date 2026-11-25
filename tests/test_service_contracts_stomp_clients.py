"""STOMP clients known by their declared types and browser globals:
@stomp/rx-stomp RxStomp, ng2-stomp-service StompService and the StompJs
bundle, with the WebSocket handshakes their options name."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.analysis_tools.evidence import get_rule
from typescript_ready import needs_typescript

READY = (
    importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_java")
    and importlib.util.find_spec("tree_sitter_javascript")
    and importlib.util.find_spec("tree_sitter_typescript")
)
JAVA = "server/src/main/java/app"

CONFIG = """
package app;

import org.springframework.messaging.simp.config.MessageBrokerRegistry;
import org.springframework.web.socket.config.annotation.StompEndpointRegistry;

public class Config {
    public void registerStompEndpoints(StompEndpointRegistry registry) {
        registry.addEndpoint("/ws").withSockJS();
        registry.addEndpoint("/raw");
    }

    public void configureMessageBroker(MessageBrokerRegistry registry) {
        registry.setApplicationDestinationPrefixes("/app");
    }
}
"""

CONTROLLER = """
package app;

import org.springframework.messaging.handler.annotation.MessageMapping;
import org.springframework.messaging.handler.annotation.SendTo;

public class Controller {
    @MessageMapping("/hello")
    @SendTo("/topic/greetings")
    public String hello(String name) { return name; }
}
"""

TRACKER = """
import { RxStomp } from '@stomp/rx-stomp';
import SockJS from 'sockjs-client';

export class Tracker {
  private rxStomp?: RxStomp;

  setup(): void {
    this.rxStomp = new RxStomp();
  }

  get stomp(): RxStomp {
    return this.rxStomp!;
  }

  watch() {
    return this.stomp.watch('/topic/greetings');
  }

  watchOptions() {
    return this.stomp.watch({ destination: '/topic/greetings' });
  }

  send() {
    this.stomp.publish({ destination: '/app/hello', body: '' });
  }

  connect() {
    this.stomp.configure({ webSocketFactory: () => SockJS('/ws') });
  }
}
"""

NG2 = """
import { StompService } from 'ng2-stomp-service';

export class AppComponent {
  constructor(private stomp: StompService) {
    stomp.configure({ host: '/ws' });
  }

  connect() {
    this.stomp.subscribe('/topic/greetings', data => data);
  }

  send(name: string) {
    this.stomp.send('/app/hello', { name });
  }
}
"""

# The library's browser bundle, loaded by a script tag.
BUNDLE = """
const stompClient = new StompJs.Client({
    brokerURL: 'ws://localhost:8080/raw'
});

function send() {
    stompClient.publish({ destination: '/app/hello', body: '' });
}
"""

# StompJs is bound here, so StompJs.Client is not the bundle's; and a name
# typed as two different clients is neither.
OTHER = """
import { Client } from '@stomp/stompjs';
import { RxStomp } from '@stomp/rx-stomp';
import * as StompJs from './local';

const local = new StompJs.Client({ brokerURL: '/raw' });
local.publish({ destination: '/app/hello' });

class Mixed {
  constructor(private client: Client) {}
}
class Other {
  constructor(private client: RxStomp) {}
  run() { this.client.publish({ destination: '/app/hello' }); }
}
"""

FILES = {
    "server/pom.xml": "<project/>",
    f"{JAVA}/Config.java": CONFIG,
    f"{JAVA}/Controller.java": CONTROLLER,
    "web/package.json": '{"name": "web"}',
    "web/src/tracker.ts": TRACKER,
    "web/src/app.component.ts": NG2,
    "web/src/other.ts": OTHER,
    "web/static/app.js": BUNDLE,
}


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
@needs_typescript
class StompClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        for name, text in FILES.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.output = asyncio.run(ServiceContractTool(root).analyze("r"))

    def tearDown(self):
        self.directory.cleanup()

    def test_messages(self):
        edges = sorted(
            (
                r.target_match["qualified_name"].split("::")[-1],
                r.type,
                r.properties["client"],
                r.source_match["qualified_name"].split("::")[1].rpartition("/")[2],
                r.properties["line"],
            )
            for r in self.output.relationships
            if r.type in ("PUBLISHES", "CONSUMES")
        )
        self.maxDiff = None
        self.assertEqual(
            edges,
            [
                ("/app/hello", "CONSUMES", "spring-stomp", "Controller.java", 8),
                (
                    "/app/hello",
                    "PUBLISHES",
                    "ng2-stomp-service",
                    "app.component.ts",
                    14,
                ),
                ("/app/hello", "PUBLISHES", "rx-stomp", "tracker.ts", 25),
                ("/app/hello", "PUBLISHES", "stompjs", "app.js", 7),
                (
                    "/topic/greetings",
                    "CONSUMES",
                    "ng2-stomp-service",
                    "app.component.ts",
                    10,
                ),
                ("/topic/greetings", "CONSUMES", "rx-stomp", "tracker.ts", 17),
                ("/topic/greetings", "CONSUMES", "rx-stomp", "tracker.ts", 21),
                ("/topic/greetings", "PUBLISHES", "spring-stomp", "Controller.java", 9),
            ],
        )

    def test_handshakes(self):
        edges = sorted(
            (
                r.source_match["qualified_name"].split("::")[1].rpartition("/")[2],
                r.properties["client"],
                r.properties["request"],
                r.properties["line"],
            )
            for r in self.output.relationships
            if r.type == "REQUESTS"
        )
        self.assertEqual(
            edges,
            [
                ("app.component.ts", "sockjs", "GET /ws", 6),
                ("app.js", "websocket", "GET ws://localhost:8080/raw", 3),
                # SockJS called without new.
                ("tracker.ts", "sockjs", "GET /ws", 29),
            ],
        )

    def test_rules(self):
        for rule_id in (
            "service-contracts.rx-stomp-publish",
            "service-contracts.rx-stomp-watch",
            "service-contracts.ng2-stomp-service-send",
            "service-contracts.ng2-stomp-service-subscribe",
        ):
            self.assertEqual(get_rule(rule_id).evidence_type, "pattern_matched")


if __name__ == "__main__":
    unittest.main()
