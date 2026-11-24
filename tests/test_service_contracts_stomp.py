"""STOMP destinations: Spring message-handling methods and templates, and
the JavaScript STOMP clients that send to and subscribe to them."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.analysis_tools.evidence import get_rule

READY = (
    importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_java")
    and importlib.util.find_spec("tree_sitter_javascript")
)
JAVA = "src/main/java"
CHAT = "ChatController.java"

CONFIG = """
package chat;

import org.springframework.messaging.simp.config.MessageBrokerRegistry;

public class BrokerConfig {
    public void configureMessageBroker(MessageBrokerRegistry registry) {
        registry.enableSimpleBroker("/topic");
        registry.setApplicationDestinationPrefixes("/app", "/event/");
    }
}
"""

CONTROLLER = """
package chat;

import org.springframework.messaging.handler.annotation.MessageMapping;
import org.springframework.messaging.handler.annotation.SendTo;
import org.springframework.messaging.simp.annotation.SendToUser;
import org.springframework.messaging.simp.annotation.SubscribeMapping;
import org.springframework.messaging.simp.SimpMessagingTemplate;

@MessageMapping("/chat")
public class ChatController {
    private SimpMessagingTemplate template;

    @MessageMapping("/send")
    @SendTo("/topic/messages")
    public String send(String message) { return message; }

    @SubscribeMapping("/history")
    public String history() { return ""; }

    @MessageMapping("/private")
    @SendToUser("/queue/reply")
    public String reply(String message) { return message; }

    // Joined to the class path with a slash, as AntPathMatcher.combine does.
    @MessageMapping("rooms")
    public void rooms() {}

    void notifyAll(String room) {
        template.convertAndSend("/topic/notices", "hi");
        template.convertAndSend("/topic/" + room, "hi");
        template.convertAndSendToUser("bob", "/queue/x", "hi");
    }
}
"""

# Another module sets no prefix: its mappings are the destinations.
PLAIN = """
package plain;

import org.springframework.messaging.handler.annotation.MessageMapping;

public class PlainController {
    @MessageMapping("/ping")
    public void ping() {}

    // No slash and no class path: AntPathMatcher would not match it.
    @MessageMapping("pong")
    public void pong() {}
}
"""

CLIENT = """
import { Client } from '@stomp/stompjs';
import * as webstomp from 'webstomp-client';

const client = new Client({ brokerURL: 'ws://chat/ws' });
let legacy;

function connect() {
    legacy = Stomp.over(new SockJS('/ws'));
    legacy.send('/app/chat/send', {}, 'hello');
    legacy.subscribe('/topic/messages', () => {});
    client.publish({ destination: '/event/chat/send', body: 'hello' });
    client.subscribe('/app/chat/history', () => {});
    client.subscribe('/topic/' + room, () => {});
    other.send('/app/chat/send');
}

// Rebound to something else: not a STOMP client.
let other = Stomp.over(null);
other = {};
"""

ANGULAR = """
angular.module('app', ['ngStomp']).controller('c', function ($stomp) {
    $stomp.send('/ping', {});
});
"""

FILES = {
    "chat/pom.xml": "<project/>",
    f"chat/{JAVA}/chat/BrokerConfig.java": CONFIG,
    f"chat/{JAVA}/chat/ChatController.java": CONTROLLER,
    "plain/pom.xml": "<project/>",
    f"plain/{JAVA}/plain/PlainController.java": PLAIN,
    "web/package.json": '{"name": "web"}',
    "web/src/client.js": CLIENT,
    "web/src/angular.js": ANGULAR,
}


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class StompTests(unittest.TestCase):
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

    def test_edges(self):
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
                ("/app/chat/history", "CONSUMES", "stompjs", "client.js", 13),
                # A subscription is answered by the method's return value.
                ("/app/chat/history", "PUBLISHES", "spring-stomp", CHAT, 18),
                ("/app/chat/private", "CONSUMES", "spring-stomp", CHAT, 21),
                ("/app/chat/rooms", "CONSUMES", "spring-stomp", CHAT, 26),
                ("/app/chat/send", "CONSUMES", "spring-stomp", CHAT, 14),
                ("/app/chat/send", "PUBLISHES", "stompjs", "client.js", 10),
                ("/event/chat/history", "PUBLISHES", "spring-stomp", CHAT, 18),
                ("/event/chat/private", "CONSUMES", "spring-stomp", CHAT, 21),
                ("/event/chat/rooms", "CONSUMES", "spring-stomp", CHAT, 26),
                ("/event/chat/send", "CONSUMES", "spring-stomp", CHAT, 14),
                ("/event/chat/send", "PUBLISHES", "stompjs", "client.js", 12),
                ("/ping", "CONSUMES", "spring-stomp", "PlainController.java", 7),
                ("/ping", "PUBLISHES", "stompjs", "angular.js", 3),
                ("/topic/messages", "CONSUMES", "stompjs", "client.js", 11),
                ("/topic/messages", "PUBLISHES", "spring-stomp", CHAT, 15),
                ("/topic/notices", "PUBLISHES", "spring-stomp", CHAT, 30),
            ],
        )

    def test_unread(self):
        unread = self.output.metadata.custom_metrics["unread"]
        # convertAndSendToUser and @SendToUser.
        self.assertEqual(unread["publish:user_destination"], 2)
        # "rooms", and "/topic/" + room on the server and the client.
        self.assertEqual(unread["consume:destination_not_literal"], 2)
        self.assertEqual(unread["publish:destination_not_literal"], 1)

    def test_rules(self):
        for rule_id in (
            "service-contracts.spring-stomp-message-mapping",
            "service-contracts.spring-stomp-send",
            "service-contracts.stompjs-send",
            "service-contracts.stompjs-subscribe",
        ):
            self.assertEqual(get_rule(rule_id).evidence_type, "pattern_matched")
