"""NestJS microservices: @MessagePattern/@EventPattern handlers and the
ClientProxy send/emit calls that reach them."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.analysis_tools.evidence import get_rule

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_typescript"
)

SERVICE = """
import { Controller } from '@nestjs/common';
import { EventPattern, MessagePattern } from '@nestjs/microservices';

@Controller()
export class MathController {
  @MessagePattern({ role: 'math', cmd: 'sum' })
  sum(data: number[]) { return 0; }

  @MessagePattern('ping')
  ping() {}

  @EventPattern('user_created')
  created() {}

  // Keys whose order depends on case: not read.
  @MessagePattern({ B: 1, a: 2 })
  mixed() {}
}

// Not a controller: Nest does not register its patterns.
export class Helper {
  @MessagePattern('ignored')
  helper() {}
}
"""

GATEWAY = """
import { Controller, Inject } from '@nestjs/common';
import { ClientKafka, ClientProxy } from '@nestjs/microservices';

@Controller()
export class Gateway {
  private readonly events: ClientKafka;

  constructor(@Inject('MATH') private readonly client: ClientProxy) {}

  run() {
    const pattern = { cmd: 'sum', role: 'math' };
    this.client.send(pattern, [1, 2]);
    this.client.send('ping', {});
    // An event is not a message: this meets no handler.
    this.client.emit('ping', {});
    this.events.emit('user_created', {});
    this.client.send(this.name(), {});
  }
}
"""

# Not a ClientProxy: send means nothing.
OTHER = """
import { Controller } from '@nestjs/common';
import { Transport } from '@nestjs/microservices';

export class Mailer {
  constructor(private readonly client: SmtpClient) {}
  run() { this.client.send('ping', {}); }
}
"""

FILES = {
    "math/package.json": '{"name": "math"}',
    "math/src/math.controller.ts": SERVICE,
    "gateway/package.json": '{"name": "gateway"}',
    "gateway/src/gateway.controller.ts": GATEWAY,
    "gateway/src/mailer.ts": OTHER,
}


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class NestMicroservicesTests(unittest.TestCase):
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
                r.source_match["qualified_name"].split("::")[2],
                r.properties["line"],
            )
            for r in self.output.relationships
            if r.type in ("PUBLISHES", "CONSUMES")
        )
        self.maxDiff = None
        sum_route = 'message:{"cmd":"sum","role":"math"}'
        self.assertEqual(
            edges,
            [
                ("event:ping", "PUBLISHES", "Gateway.run", 16),
                ("event:user_created", "CONSUMES", "MathController.created", 13),
                ("event:user_created", "PUBLISHES", "Gateway.run", 17),
                ("message:ping", "CONSUMES", "MathController.ping", 10),
                ("message:ping", "PUBLISHES", "Gateway.run", 14),
                # Keys sorted as Nest sorts them, from a const object too.
                (sum_route, "CONSUMES", "MathController.sum", 7),
                (sum_route, "PUBLISHES", "Gateway.run", 13),
            ],
        )

    def test_unread(self):
        self.assertEqual(
            self.output.metadata.custom_metrics["unread"],
            # { B: 1, a: 2 } and this.name().
            {
                "consume:destination_not_literal": 1,
                "publish:destination_not_literal": 1,
            },
        )

    def test_rules(self):
        for rule_id in (
            "service-contracts.nestjs-message-pattern",
            "service-contracts.nestjs-client-send",
        ):
            self.assertEqual(get_rule(rule_id).evidence_type, "pattern_matched")


if __name__ == "__main__":
    unittest.main()
