"""NestJS controller routes."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.analysis_tools.evidence import get_rule
from typescript_ready import needs_typescript

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
  // Options that are not a literal object leave routes unread.
  app.enableVersioning(versioningOptions);
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
@needs_typescript
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


URI_MAIN = """
import { NestFactory } from '@nestjs/core';
import { VersioningType, VERSION_NEUTRAL } from '@nestjs/common';

async function bootstrap() {
  const app = await NestFactory.create(AppModule);
  app.setGlobalPrefix('api');
  app.enableVersioning({ type: VersioningType.URI, defaultVersion: '1' });
}
"""

URI_USERS = """
import { Controller, Get, Post, Version, VERSION_NEUTRAL } from '@nestjs/common';

@Controller({ path: 'users', version: ['1', '2'] })
export class UsersController {
  @Get()
  list() {}

  @Version('3')
  @Post()
  create() {}

  @Version(VERSION_NEUTRAL)
  @Get('health')
  health() {}
}

@Controller('seats')
export class SeatsController {
  @Post('reserve')
  reserve() {}
}

@Controller({ path: 'odd', version: someVersion })
export class OddController {
  @Get()
  odd() {}
}
"""

HEADER_MAIN = """
import { VersioningType } from '@nestjs/common';

async function bootstrap(app) {
  app.enableVersioning({ type: VersioningType.HEADER, header: 'X-Version' });
}
"""

HEADER_FLIGHTS = """
import { Controller, Get } from '@nestjs/common';

@Controller({ path: 'flights', version: '1' })
export class FlightsController {
  @Get()
  list() {}
}
"""

PREFIX_MAIN = """
import { VersioningType } from '@nestjs/common';

async function bootstrap(app) {
  app.enableVersioning({ type: VersioningType.URI, prefix: 'version-' });
}
"""

PREFIX_TRIPS = """
import { Controller, Get } from '@nestjs/common';

@Controller({ path: 'trips', version: '2' })
export class TripsController {
  @Get()
  list() {}
}
"""

SEAT_CLIENT = """
import axios from 'axios';

export async function reserve() {
  return axios.post('http://users/api/v1/seats/reserve');
}
"""


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
@needs_typescript
class NestVersioningTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        _write(
            root,
            {
                "users/package.json": "{}",
                "users/src/main.ts": URI_MAIN,
                "users/src/users.controller.ts": URI_USERS,
                "flights/package.json": "{}",
                "flights/src/main.ts": HEADER_MAIN,
                "flights/src/flights.controller.ts": HEADER_FLIGHTS,
                "trips/package.json": "{}",
                "trips/src/main.ts": PREFIX_MAIN,
                "trips/src/trips.controller.ts": PREFIX_TRIPS,
                "web/package.json": "{}",
                "web/src/client.ts": SEAT_CLIENT,
            },
        )
        self.tool = ServiceContractTool(root)
        asyncio.run(self.tool.analyze("r"))

    def tearDown(self):
        self.directory.cleanup()

    def test_routes(self):
        _, routes, _, unread, _ = self.tool.last_facts
        self.assertEqual(
            sorted(
                (r.file.rpartition("/")[2], r.line, r.methods, r.path)
                for r in routes
                if r.framework == "nestjs"
            ),
            [
                # Header versioning leaves the path alone.
                ("flights.controller.ts", 6, ("GET",), "/flights"),
                # A custom URI prefix.
                ("trips.controller.ts", 6, ("GET",), "/version-2/trips"),
                # The controller's versions, one route each.
                ("users.controller.ts", 6, ("GET",), "/api/v1/users"),
                ("users.controller.ts", 6, ("GET",), "/api/v2/users"),
                # @Version overrides the controller's.
                ("users.controller.ts", 10, ("POST",), "/api/v3/users"),
                # VERSION_NEUTRAL adds no segment.
                ("users.controller.ts", 14, ("GET",), "/api/users/health"),
                # No version: the default version.
                ("users.controller.ts", 20, ("POST",), "/api/v1/seats/reserve"),
            ],
        )
        self.assertEqual(
            [(u.file.rpartition("/")[2], u.line, u.reason) for u in unread],
            [("users.controller.ts", 24, "version")],
        )

    def test_request_links_to_versioned_route(self):
        _, routes, requests, _, resolutions = self.tool.last_facts
        by_id = {r.id: r for r in routes}
        status = {r.request_id: r for r in resolutions}
        self.assertEqual(
            [by_id[status[q.id].route_id].path for q in requests],
            ["/api/v1/seats/reserve"],
        )


ROUTER_APP = """
import { Module } from '@nestjs/common';
import { RouterModule } from '@nestjs/core';
import { AdminModule } from './admin.module';
import { UsersModule } from './users.module';

@Module({
  imports: [
    AdminModule,
    UsersModule,
    RouterModule.register([
      { path: 'admin', module: AdminModule, children: [{ path: 'users', module: UsersModule }] },
    ]),
  ],
  controllers: [RootController],
})
export class AppModule {}
"""

ROUTER_MODULES = """
import { Module } from '@nestjs/common';

@Module({ controllers: [DashboardController, SharedController] })
export class AdminModule {}

@Module({ controllers: [UsersController, SharedController] })
export class UsersModule {}
"""

ROUTER_CONTROLLERS = """
import { Controller, Get } from '@nestjs/common';

@Controller('dashboard')
export class DashboardController {
  @Get()
  show() {}
}

@Controller('list')
export class UsersController {
  @Get()
  list() {}
}

@Controller()
export class RootController {
  @Get('health')
  health() {}
}

@Controller('shared')
export class SharedController {
  @Get()
  shared() {}
}
"""

DYNAMIC_ROUTER = """
import { Module } from '@nestjs/common';
import { RouterModule } from '@nestjs/core';

@Module({ imports: [RouterModule.register(routes)] })
export class AppModule {}
"""


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
@needs_typescript
class NestRouterModuleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        _write(
            root,
            {
                "api/package.json": "{}",
                "api/src/app.module.ts": ROUTER_APP,
                "api/src/modules.ts": ROUTER_MODULES,
                "api/src/controllers.ts": ROUTER_CONTROLLERS,
                "dynamic/package.json": "{}",
                "dynamic/src/app.module.ts": DYNAMIC_ROUTER,
                "dynamic/src/controllers.ts": ROUTER_CONTROLLERS,
            },
        )
        self.tool = ServiceContractTool(root)
        asyncio.run(self.tool.analyze("r"))

    def tearDown(self):
        self.directory.cleanup()

    def test_routes(self):
        _, routes, _, unread, _ = self.tool.last_facts
        self.assertEqual(
            sorted(
                (r.file.partition("/")[0], r.line, r.path)
                for r in routes
                if r.framework == "nestjs"
            ),
            [
                # A registered module's path, children joined to parents'.
                ("api", 6, "/admin/dashboard"),
                ("api", 12, "/admin/users/list"),
                # A controller of a module RouterModule does not name.
                ("api", 18, "/health"),
            ],
        )
        self.assertEqual(
            sorted((u.file.partition("/")[0], u.line, u.reason) for u in unread),
            [
                # Listed by two modules with different paths.
                ("api", 22, "router_module"),
                # Routes that are not a literal array leave the package unread.
                ("dynamic", 6, "router_module"),
                ("dynamic", 12, "router_module"),
                ("dynamic", 18, "router_module"),
                ("dynamic", 24, "router_module"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
