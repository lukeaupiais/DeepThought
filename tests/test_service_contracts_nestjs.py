"""NestJS controller routes."""

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

CATS = """
import {
  Controller, Get, Post, Delete, All, HttpCode, Param, Version,
} from '@nestjs/common';
import { Roles } from './roles';

const BASE = 'cats';

@Controller(BASE)
export class CatsController {
  @Post()
  @HttpCode(204)
  @Roles(['admin'])
  create() {}

  @Get(':id')
  findOne(@Param('id') id: string) {}

  @Delete([':id', 'by-name/:name'])
  remove() {}

  @All('files/*path')
  files() {}

  @Version('2')
  @Get('v')
  versioned() {}

  // Not a route decorator of Nest.
  @Roles(['x'])
  helper() {}
}

@Controller({ path: 'dogs', host: 'admin.example.com' })
class DogsController {
  @Get()
  list() {}
}

@Controller({ path: 'birds', version: '1' })
class BirdsController {
  @Get()
  list() {}
}

@Controller()
class RootController {
  @Get('health')
  health() {}
}
"""

# Same decorator names from another library are not Nest routes.
OTHER = """
import { Injectable } from '@nestjs/common';
import { Controller, Get } from 'routing-controllers';

@Controller('/other')
export class Other {
  @Get('/x')
  x() {}
}
"""

MAIN = """
import { NestFactory } from '@nestjs/core';
import { AppModule } from './app.module';

async function bootstrap() {
  const app = await NestFactory.create(AppModule);
  app.setGlobalPrefix('api');
  await app.listen(3000);
}
bootstrap();
"""

VERSIONED_MAIN = """
import { NestFactory } from '@nestjs/core';
import { VersioningType } from '@nestjs/common';

async function bootstrap() {
  const app = await NestFactory.create(AppModule);
  app.enableVersioning({ type: VersioningType.URI });
}
"""

ITEMS = """
import { Controller, Get } from '@nestjs/common';

@Controller('items')
export class ItemsController {
  @Get()
  list() {}
}
"""

ORDERS_MAIN = """
async function bootstrap(app) {
  app.setGlobalPrefix('v1', { exclude: ['health'] });
}
"""

ORDERS = """
import { Controller, Get } from '@nestjs/common';

@Controller('orders')
export class OrdersController {
  @Get()
  list() {}
}
"""

CLIENT = """
import axios from 'axios';

export async function load() {
  return axios.get('http://cats/api/cats/7');
}
"""

FILES = {
    "cats/package.json": "{}",
    "cats/src/main.ts": MAIN,
    "cats/src/cats.controller.ts": CATS,
    "cats/src/other.ts": OTHER,
    "items/package.json": "{}",
    "items/src/main.ts": VERSIONED_MAIN,
    "items/src/items.controller.ts": ITEMS,
    "orders/package.json": "{}",
    "orders/src/main.ts": ORDERS_MAIN,
    "orders/src/orders.controller.ts": ORDERS,
    "web/package.json": "{}",
    "web/src/client.ts": CLIENT,
}


def _write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class NestRouteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        _write(self.root, FILES)
        self.tool = ServiceContractTool(self.root)
        self.output = asyncio.run(self.tool.analyze("r"))
        _, self.routes, _, self.unread, self.resolutions = self.tool.last_facts

    def tearDown(self):
        self.directory.cleanup()

    def test_routes(self):
        found = sorted(
            (r.file.rpartition("/")[2], r.line, r.methods, r.path, bool(r.handler_id))
            for r in self.routes
            if r.framework == "nestjs"
        )
        self.assertEqual(
            found,
            [
                ("cats.controller.ts", 11, ("POST",), "/api/cats", True),
                ("cats.controller.ts", 16, ("GET",), "/api/cats/{var}", True),
                (
                    "cats.controller.ts",
                    19,
                    ("DELETE",),
                    "/api/cats/by-name/{var}",
                    True,
                ),
                ("cats.controller.ts", 19, ("DELETE",), "/api/cats/{var}", True),
                ("cats.controller.ts", 22, ("*",), "/api/cats/files/{*rest}", True),
                ("cats.controller.ts", 36, ("GET",), "/api/dogs", True),
                ("cats.controller.ts", 48, ("GET",), "/api/health", True),
            ],
        )
        self.assertEqual(
            sorted(
                (u.file.rpartition("/")[2], u.line, u.reason)
                for u in self.unread
                if u.kind == "route"
            ),
            [
                ("cats.controller.ts", 25, "version"),
                ("cats.controller.ts", 40, "version"),
                ("items.controller.ts", 6, "versioning"),
                ("orders.controller.ts", 6, "global_prefix_options"),
            ],
        )

    def test_graph(self):
        handles = [
            r
            for r in self.output.relationships
            if r.type == "HANDLES"
            and get_rule(r.properties["rule_id"]).rule_id
            == "service-contracts.nestjs-route"
        ]
        self.assertEqual(len(handles), 7)
        self.assertIn(
            "CatsController.findOne",
            {h.source_match["qualified_name"].split("::")[2] for h in handles},
        )
        linked = [r for r in self.resolutions if r.status == "linked"]
        self.assertEqual(len(linked), 1)


if __name__ == "__main__":
    unittest.main()
