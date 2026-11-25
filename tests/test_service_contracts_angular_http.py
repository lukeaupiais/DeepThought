"""Angular HttpClient and httpResource requests, with URLs built from
constants imported through tsconfig paths, angular.json ``define`` globals
and ``this.field`` initializers."""

import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought import ts_facts
from deepthought.service_contracts.script_constants import angular_defines
from deepthought.syntax import make_parser

READY = (
    importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_typescript")
    and importlib.util.find_spec("tree_sitter_javascript")
)

SERVER = """
const express = require('express');
const app = express();
app.get('/api/users', list);
app.post('/api/users', create);
app.put('/api/users/:login', update);
app.get('/api/account', account);
app.get('/api/orders', orders);
app.post('/api/logs', log);
"""

USERS = """
import { HttpClient, httpResource } from '@angular/common/http';
import { inject } from '@angular/core';
import { ordersPath, serverApiUrl } from 'app/config';

export class UsersBase {
  readonly users = httpResource(() => {
    if (!this.ready) {
      return undefined;
    }
    return { url: this.resourceUrl, params: {} };
  });
  protected readonly resourceUrl = `${serverApiUrl}api/users`;
}

export class UserService extends UsersBase {
  protected readonly http = inject(HttpClient);

  create(user) {
    return this.http.post(this.resourceUrl, user);
  }

  update(user) {
    return this.http
      .put(`${this.resourceUrl}/${user.login}`, user);
  }

  // The server has no PATCH route and no GET by id.
  patch(user) {
    return this.http.patch(`${this.resourceUrl}/${user.login}`, user);
  }

  find(id) {
    return this.http.get(`${this.resourceUrl}/${id}`);
  }

  orders() {
    return this.http.request('GET', serverApiUrl + ordersPath);
  }
}
"""

LEGACY = """
import { HttpClient } from '@angular/common/http';

export class LegacyService {
  private logUrl = 'api/logs';
  private base = 'api/one';

  constructor(private http: HttpClient, private other: Other) {}

  log() {
    return this.http.post(this.logUrl, {});
  }

  // Assigned again: the field's value is not fixed.
  moved() {
    this.base = 'api/two';
    return this.http.get(this.base);
  }

  // Not an HttpClient.
  notHttp() {
    return this.other.get('api/account');
  }
}
"""

ANGULAR = {
    "projects": {
        "web": {
            "sourceRoot": "src",
            "architect": {
                "build": {
                    "options": {"define": {"SERVER_API_URL": "''"}},
                    "configurations": {"production": {}},
                }
            },
        }
    }
}

FILES = {
    "server/package.json": '{"name": "server"}',
    "server/app.js": SERVER,
    "web/package.json": '{"name": "web"}',
    "web/angular.json": json.dumps(ANGULAR),
    "web/tsconfig.json": (
        '{"compilerOptions": {\n'
        "  // aliases\n"
        '  "paths": {"app/config": ["./src/app/config/index.ts"]}\n'
        "}}"
    ),
    "web/src/declarations.d.ts": "declare const SERVER_API_URL: string;",
    "web/src/app/config/index.ts": (
        "export * from './endpoint.constants';\n"
        "export { ORDERS as ordersPath } from './paths';\n"
    ),
    "web/src/app/config/endpoint.constants.ts": (
        "export const serverApiUrl = SERVER_API_URL;\n"
    ),
    "web/src/app/config/paths.ts": "export const ORDERS = 'api/orders';\n",
    "web/src/app/user.service.ts": USERS,
    "web/src/app/legacy.service.ts": LEGACY,
}


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class AngularHttpTests(unittest.TestCase):
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

    def test_links(self):
        edges = sorted(
            (
                r.source_match["qualified_name"].split("::")[2],
                r.properties["request"],
                r.properties["client"],
                r.properties["rule_id"],
            )
            for r in self.output.relationships
            if r.type == "REQUESTS"
        )
        rule = "service-contracts.http-path"
        self.assertEqual(
            edges,
            [
                ("LegacyService.log", "POST /api/logs", "angular-http", rule),
                ("UserService.create", "POST /api/users", "angular-http", rule),
                ("UserService.orders", "GET /api/orders", "angular-http", rule),
                (
                    "UserService.update",
                    "PUT /api/users/{?}",
                    "angular-http",
                    rule,
                ),
                ("UsersBase", "GET /api/users", "angular-http", rule),
            ],
        )

    def test_unresolved(self):
        found = {
            n.properties["path"]: sorted(n.properties["unresolved_http_requests"])
            for n in self.output.nodes
            if n.properties.get("unresolved_http_requests")
        }
        self.assertEqual(
            found,
            {
                "web/src/app/legacy.service.ts": ["GET {?}"],
                "web/src/app/user.service.ts": [
                    "GET /api/users/{?}",
                    "PATCH /api/users/{?}",
                ],
            },
        )


def _compiled(files, defines=()):
    """{file: {source text of a name: value}} the TypeScript compiler gives."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text, encoding="utf-8")
        facts = ts_facts.collect(root, list(files), defines)
    return {
        file: {
            files[file].encode()[start:end].decode(): value
            for (start, end), value in values.items()
        }
        for file, values in facts.values.items()
    }


class CompilerValueTests(unittest.TestCase):
    """What the hand-written module reader did, now the compiler's."""

    def test_star_exports_that_disagree_export_nothing(self):
        found = _compiled(
            {
                "src/a.ts": "export const URL = 'a'; export const ONLY = 'x';",
                "src/b.ts": "export const URL = 'b';",
                "src/index.ts": "export * from './a';\nexport * from './b';",
                "src/use.ts": "import { URL, ONLY } from './index';\nf(URL, ONLY);",
            }
        )
        self.assertEqual(found["src/use.ts"], {"ONLY": "x"})

    def test_let_is_not_a_value_and_imports_shadow_globals(self):
        found = _compiled(
            {
                "src/a.ts": "export let URL = 'a'; export const API = 'api';",
                "src/g.d.ts": "declare const SERVER_API_URL: string;",
                "src/use.ts": (
                    "import { API as SERVER_API_URL, URL } from './a';\n"
                    "f(SERVER_API_URL, URL);"
                ),
            },
            [("src", {"SERVER_API_URL": ""})],
        )
        self.assertEqual(found["src/use.ts"], {"SERVER_API_URL": "api"})

    def test_enums_paths_and_const_objects(self):
        found = _compiled(
            {
                "tsconfig.base.json": (
                    '{"compilerOptions": {"baseUrl": ".", '
                    '"paths": {"@shop/shared": ["libs/shared/index.ts"]}}}'
                ),
                "apps/api/tsconfig.json": (
                    '{"extends": "../../tsconfig.base.json", "include": ["*.ts"]}'
                ),
                "libs/shared/index.ts": (
                    "export enum Resources { USERS = 'users' }\n"
                    "export const ROUTES = { health: '/health' } as const;\n"
                ),
                "apps/api/main.ts": (
                    "import { Resources, ROUTES } from '@shop/shared';\n"
                    "f(Resources.USERS, ROUTES.health);"
                ),
            }
        )
        self.assertEqual(
            found["apps/api/main.ts"],
            {"Resources.USERS": "users", "ROUTES.health": "/health"},
        )

    def test_angular_defines(self):
        workspace = {
            "projects": {
                "app": {
                    "root": "projects/app",
                    "architect": {
                        "build": {
                            "options": {
                                "define": {"A": "'x'", "B": "'1'", "C": "true"}
                            },
                            "configurations": {"prod": {"define": {"B": "'2'"}}},
                        }
                    },
                }
            }
        }
        self.assertEqual(
            angular_defines("web/angular.json", json.dumps(workspace).encode()),
            [("web/projects/app", {"A": "x", "B": None, "C": None})],
        )


THIS_FIELDS = """
class Base {
  url = 'base';
  shadowed = 'base';
}
class Use extends Base {
  own = `${this.url}/own`;
  a() { return this.own; }
  b() { return function () { return this.url; }; }
  c() { return () => this.url; }
  static d() { return this.url; }
  e() { return this.shadowed; }
}
class Sub extends Use {
  shadowed = 'sub';
}
"""


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class ThisFieldTests(unittest.TestCase):
    def test_this_fields(self):
        from deepthought.service_contracts.folding import Folder
        from deepthought.service_contracts.http import PLACEHOLDER

        content = THIS_FIELDS.encode()
        tree = make_parser("typescript", ".ts").parse(content)
        folder = Folder("typescript", tree.root_node, content)
        found = {}
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == "method_definition":
                name = content[
                    node.child_by_field_name(
                        "name"
                    ).start_byte : node.child_by_field_name("name").end_byte
                ].decode()
                members = []
                inner = [node]
                while inner:
                    n = inner.pop()
                    if n.type == "member_expression":
                        members.append(n)
                    inner.extend(n.named_children)
                found[name] = folder.evaluate(members[0])
            stack.extend(node.named_children)
        self.assertEqual(
            found,
            {
                "a": "base/own",
                # A function expression binds its own this; a static method's
                # this is the class.
                "b": PLACEHOLDER,
                "c": "base",
                "d": PLACEHOLDER,
                # A same-file subclass declares the field again.
                "e": PLACEHOLDER,
            },
        )


if __name__ == "__main__":
    unittest.main()
