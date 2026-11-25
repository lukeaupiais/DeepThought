"""Types a NestJS handler writes for its request body, response, message
payload and reply: the contract CARRIES the declaration the TypeScript
compiler resolves the written type to."""

import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from typescript_ready import needs_typescript

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_typescript"
)

FILES = {
    "package.json": json.dumps(
        {"dependencies": {"@nestjs/common": "10", "@nestjs/microservices": "10"}}
    ),
    "tsconfig.json": json.dumps(
        {"compilerOptions": {"experimentalDecorators": True, "baseUrl": "."}}
    ),
    "src/dto/index.ts": "export * from './order';\n",
    "src/dto/order.ts": (
        "export class CreateOrderDto { item: string; }\n"
        "export interface OrderView { id: string; }\n"
        "export type OrderId = string;\n"
    ),
    "src/orders.controller.ts": (
        "import { Body, Controller, Get, Param, Post } from '@nestjs/common';\n"
        "import { CreateOrderDto, OrderView } from './dto';\n"
        "@Controller('orders')\n"
        "export class OrdersController {\n"
        "  @Post()\n"
        "  create(@Body() body: CreateOrderDto): Promise<OrderView[]> {\n"
        "    return null as any;\n"
        "  }\n"
        "  @Get(':id')\n"
        "  one(@Param('id') id: string, @Body() x: number) {\n"
        "    return null;\n"
        "  }\n"
        "}\n"
    ),
    "src/events.controller.ts": (
        "import { Controller } from '@nestjs/common';\n"
        "import { EventPattern, MessagePattern, Payload } from "
        "'@nestjs/microservices';\n"
        "import { CreateOrderDto, OrderView } from 'src/dto';\n"
        "@Controller()\n"
        "export class EventsController {\n"
        "  @EventPattern('order_created')\n"
        "  created(data: CreateOrderDto) {}\n"
        "  @MessagePattern('get_order')\n"
        "  get(ctx: unknown, @Payload() order: CreateOrderDto | null): "
        "Observable<OrderView> {\n"
        "    return null as any;\n"
        "  }\n"
        "}\n"
    ),
}


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
@needs_typescript
class PayloadTypeTests(unittest.TestCase):
    def test_contracts_carry_the_types_their_handlers_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in FILES.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            output = asyncio.run(ServiceContractTool(root).analyze("r"))
        found = sorted(
            (
                r.source_match["qualified_name"].split("::")[-1]
                if "message-destination" in r.source_match["qualified_name"]
                else r.source_match["qualified_name"].split("::")[1],
                r.target_match["qualified_name"].split("::")[2],
                r.properties["direction"],
                r.properties["rule_id"],
                r.properties["evidence_type"],
            )
            for r in output.relationships
            if r.type == "CARRIES"
        )
        self.assertEqual(
            found,
            [
                (
                    "event:order_created",
                    "CreateOrderDto",
                    "request",
                    "service-contracts.nestjs-message-payload-type",
                    "resolved",
                ),
                (
                    "message:get_order",
                    "CreateOrderDto",
                    "request",
                    "service-contracts.nestjs-message-payload-type",
                    "resolved",
                ),
                (
                    "message:get_order",
                    "OrderView",
                    "response",
                    "service-contracts.nestjs-message-reply-type",
                    "resolved",
                ),
                (
                    "src/orders.controller.ts",
                    "CreateOrderDto",
                    "request",
                    "service-contracts.nestjs-body-type",
                    "resolved",
                ),
                (
                    "src/orders.controller.ts",
                    "OrderView",
                    "response",
                    "service-contracts.nestjs-response-type",
                    "resolved",
                ),
            ],
        )
        self.assertEqual(output.metadata.custom_metrics["payload_types"], 5)


if __name__ == "__main__":
    unittest.main()
