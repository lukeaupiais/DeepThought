"""WebSocket endpoints of Spring and Jakarta servers, as the GET routes of
their handshakes, and the browser and Node clients that open them."""

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

STOMP = """
package chat;

import org.springframework.web.socket.config.annotation.*;

public class StompConfig implements WebSocketMessageBrokerConfigurer {
    private static final String ENDPOINT = "/notifications";

    @Override
    public void registerStompEndpoints(StompEndpointRegistry registry) {
        registry.addEndpoint("/ws", ENDPOINT).withSockJS();
        registry.addEndpoint(System.getenv("WS_PATH"));
    }
}
"""

HANDLERS = """
package chat;

import org.springframework.web.socket.config.annotation.WebSocketConfigurer;
import org.springframework.web.socket.config.annotation.WebSocketHandlerRegistry;

public class HandlerConfig implements WebSocketConfigurer {
    @Override
    public void registerWebSocketHandlers(WebSocketHandlerRegistry registry) {
        registry.addHandler(new EchoHandler(), "/echo");
    }

    // Not a Spring registry: not read.
    void other(Registry registry) {
        registry.addHandler(null, "/other");
    }
}
"""

ENDPOINT = """
package chat;

import jakarta.websocket.server.ServerEndpoint;

@ServerEndpoint("/rooms/{room}")
public class RoomEndpoint {
}

// Another package's annotation: not read.
@other.ServerEndpoint("/elsewhere")
class Elsewhere {
}
"""

CLIENT = """
import ReconnectingSock from 'sockjs-client';
import { WebSocket as NodeSocket } from 'ws';
import WebSocket from 'some-other-socket';

function connect(room) {
    new ReconnectingSock('/notifications');
    new NodeSocket('ws://chat:8080/echo');
    new WebSocket('ws://chat:8080/echo');
}
"""

BROWSER = """
function open(room) {
    const socket = new WebSocket('wss://' + location.host + '/rooms/' + room);
    const sock = new SockJS('app/ws');
}
"""

FILES = {
    "chat/pom.xml": "<project/>",
    "chat/src/main/resources/application.properties": (
        "spring.application.name=chat\nserver.servlet.context-path=/app\n"
    ),
    f"chat/{JAVA}/chat/StompConfig.java": STOMP,
    f"chat/{JAVA}/chat/HandlerConfig.java": HANDLERS,
    f"chat/{JAVA}/chat/RoomEndpoint.java": ENDPOINT,
    "web/package.json": '{"name": "web"}',
    "web/src/client.js": CLIENT,
    "web/src/browser.js": BROWSER,
}


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class WebSocketTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        for name, text in FILES.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.tool = ServiceContractTool(root)
        self.output = asyncio.run(self.tool.analyze("r"))

    def tearDown(self):
        self.directory.cleanup()

    def test_routes(self):
        _, routes, _, unread, _ = self.tool.last_facts
        self.assertEqual(
            sorted(
                (r.file.rpartition("/")[2], r.line, r.methods, r.path, r.framework)
                for r in routes
            ),
            [
                ("HandlerConfig.java", 10, ("GET",), "/app/echo", "spring-websocket"),
                (
                    "RoomEndpoint.java",
                    6,
                    ("GET",),
                    "/app/rooms/{room}",
                    "jakarta-websocket",
                ),
                (
                    "StompConfig.java",
                    11,
                    ("GET",),
                    "/app/notifications",
                    "spring-websocket",
                ),
                ("StompConfig.java", 11, ("GET",), "/app/ws", "spring-websocket"),
            ],
        )
        self.assertIn(
            ("StompConfig.java", 12, "path_not_literal"),
            [(u.file.rpartition("/")[2], u.line, u.reason) for u in unread],
        )

    def test_requests(self):
        _, routes, requests, _, resolutions = self.tool.last_facts
        status = {r.request_id: r for r in resolutions}
        by_id = {r.id: r for r in routes}
        found = sorted(
            (
                r.file.rpartition("/")[2],
                r.line,
                r.client,
                r.url.replace("\x00", "{?}"),
                status[r.id].status,
                by_id[status[r.id].route_id].path if status[r.id].route_id else None,
            )
            for r in requests
        )
        self.assertEqual(
            found,
            [
                # The unknown base is assumed to add no segments, so the
                # context path is not matched.
                (
                    "browser.js",
                    3,
                    "websocket",
                    "wss://{?}/rooms/{?}",
                    "unresolved",
                    None,
                ),
                # A relative URL is read against the site root.
                ("browser.js", 4, "sockjs", "/app/ws", "linked", "/app/ws"),
                ("client.js", 7, "sockjs", "/notifications", "unresolved", None),
                (
                    "client.js",
                    8,
                    "websocket",
                    "ws://chat:8080/echo",
                    "unresolved",
                    None,
                ),
            ],
        )

    def test_graph(self):
        handles = sorted(
            (
                r.properties["rule_id"],
                r.source_match["qualified_name"].split("::")[2],
            )
            for r in self.output.relationships
            if r.type == "HANDLES"
        )
        self.assertEqual(
            handles,
            [
                ("service-contracts.jakarta-websocket-endpoint", "RoomEndpoint"),
                (
                    "service-contracts.spring-websocket-endpoint",
                    "HandlerConfig.registerWebSocketHandlers",
                ),
                (
                    "service-contracts.spring-websocket-endpoint",
                    "StompConfig.registerStompEndpoints",
                ),
                (
                    "service-contracts.spring-websocket-endpoint",
                    "StompConfig.registerStompEndpoints",
                ),
            ],
        )
        requests = [
            (r.properties["client"], r.properties["rule_id"])
            for r in self.output.relationships
            if r.type == "REQUESTS"
        ]
        self.assertEqual(requests, [("sockjs", "service-contracts.http-path")])
        self.assertEqual(
            get_rule("service-contracts.spring-websocket-endpoint").evidence_type,
            "pattern_matched",
        )
