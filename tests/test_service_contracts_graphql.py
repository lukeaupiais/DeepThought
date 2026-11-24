"""GraphQL root fields: schema declarations and NestJS resolvers."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.analysis_tools.evidence import get_rule
from deepthought.service_contracts.graphql import schema_fields

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_typescript"
)

SCHEMA = '''
"""The root query."""
type Query {
  "All cats."
  cats(first: Int = 10 @deprecated(reason: "use page(size)"), tags: [String!]): [Cat]
  cat(id: ID!): Cat
  # retired: Cat
  owners: [Owner] @deprecated(reason: "x")
}

type Mutation {
  createCat(input: CreateCatInput): Cat
}

extend type Query {
  stats: Stats
}

type Cat {
  id: ID
  name(format: String): String
}
'''

RESOLVER = """
import { Args, Mutation, Query, Resolver, ResolveField, Subscription } from '@nestjs/graphql';
import { Injectable } from '@nestjs/common';

const NAME = 'cat';

@Resolver('Cat')
export class CatsResolver {
  @Query('cats')
  getCats() {}

  @Query(NAME)
  findOne(@Args('id') id: string) {}

  @Mutation(returns => Cat, { name: 'createCat' })
  create() {}

  @Subscription(returns => Cat)
  catCreated() {}

  @Query(dynamicName())
  computed() {}

  @ResolveField()
  owner() {}

  @Query({ name: 'stats' })
  statistics() {}
}

// Not a resolver class: its decorators do not register fields.
@Injectable()
export class Helper {
  @Query('ghost')
  ghost() {}
}
"""

# Same names from another library are not Nest resolvers.
OTHER = """
import { Query, Resolver } from 'type-graphql';
import { Injectable } from '@nestjs/graphql';

@Resolver()
export class Other {
  @Query()
  other() {}
}
"""

FILES = {
    "cats/package.json": "{}",
    "cats/src/cats.graphql": SCHEMA,
    "cats/src/cats.resolver.ts": RESOLVER,
    "cats/src/other.ts": OTHER,
    # A second server in another package with a field of the same name.
    "dogs/package.json": "{}",
    "dogs/src/dogs.resolver.ts": """
import { Query, Resolver } from '@nestjs/graphql';

@Resolver()
class DogsResolver {
  @Query()
  cats() {}
}
""",
    "renamed/schema.graphql": "schema { query: Root }\ntype Root { a: Int }\n",
}


def _write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class SchemaTests(unittest.TestCase):
    def test_root_fields(self):
        declared, unread = schema_fields(SCHEMA, "s.graphql")
        self.assertEqual(
            [(d.line, d.operation, d.field) for d in declared],
            [
                (5, "Query", "cats"),
                (6, "Query", "cat"),
                (8, "Query", "owners"),
                (12, "Mutation", "createCat"),
                (16, "Query", "stats"),
            ],
        )
        self.assertEqual(unread, [])

    def test_schema_definition(self):
        # Default root names are not a rename; any other name is.
        source = "type Query { a: Int }\nschema { query: Query mutation: Mutation }\n"
        declared, unread = schema_fields(source, "s.graphql")
        self.assertEqual([(d.operation, d.field) for d in declared], [("Query", "a")])
        self.assertEqual(unread, [])
        declared, unread = schema_fields("\nschema { query: Root }", "s.graphql")
        self.assertEqual(declared, [])
        self.assertEqual(
            [(u.line, u.reason) for u in unread], [(2, "schema_root_renamed")]
        )


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        _write(self.root, FILES)
        self.output = asyncio.run(ServiceContractTool(self.root).analyze("r"))

    def tearDown(self):
        self.directory.cleanup()

    def test_fields(self):
        fields = {
            (n.properties["scope"], n.properties["name"]): n.properties
            for n in self.output.nodes
            if "GraphqlField" in n.labels
        }
        handled = {}
        for r in self.output.relationships:
            if r.type == "HANDLES" and r.target_match["qualified_name"].count(
                "::graphql::"
            ):
                handled.setdefault(r.target_match["qualified_name"], []).append(
                    r.source_match["qualified_name"].split("::")[2]
                )
                rule = get_rule(r.properties["rule_id"])
                self.assertEqual(
                    rule.rule_id, "service-contracts.nestjs-graphql-resolver"
                )
        found = sorted(
            (
                scope,
                name,
                "declared_in" in properties,
                sorted(handled.get(properties["qualified_name"], [])),
            )
            for (scope, name), properties in fields.items()
        )
        self.assertEqual(
            found,
            [
                ("cats", "Mutation.createCat", True, ["CatsResolver.create"]),
                ("cats", "Query.cat", True, ["CatsResolver.findOne"]),
                ("cats", "Query.cats", True, ["CatsResolver.getCats"]),
                ("cats", "Query.owners", True, []),
                ("cats", "Query.stats", True, ["CatsResolver.statistics"]),
                ("cats", "Subscription.catCreated", False, ["CatsResolver.catCreated"]),
                ("dogs", "Query.cats", False, ["DogsResolver.cats"]),
            ],
        )
        unread = self.output.metadata.custom_metrics["unread"]
        self.assertEqual(unread["graphql:declared_not_resolved"], 1)
        self.assertEqual(unread["graphql:resolved_not_declared"], 1)
        self.assertEqual(unread["graphql:field_not_literal"], 1)
        self.assertEqual(unread["graphql:schema_root_renamed"], 1)
        self.assertEqual(self.output.metadata.custom_metrics["graphql_fields"], 7)


APOLLO = {
    "api/package.json": '{"dependencies": {"@apollo/server": "4.10.0"}}',
    "api/src/schema.js": """
const gql = require('graphql-tag');

const typeDefs = gql`
  type Query {
    launches(pageSize: Int): [Launch]
    me: User
  }
  type Mutation {
    bookTrips(ids: [ID]!): Result
    login(email: String): String
  }
  ${extra}
`;
module.exports = typeDefs;
""",
    "api/src/server.js": """
const { ApolloServer } = require('@apollo/server');
const more = /* GraphQL */ `
  extend type Query { ping: String ${extraFields()} }
`;
const other = `#graphql
  extend type Mutation { reset: Boolean }
`;
const plain = `type Query { notASchema: Int }`;
function tickStream() {}
const server = new ApolloServer({
  typeDefs: [more, other],
  resolvers: {
    Query: { ping: () => 'pong' },
    Mutation: { reset: () => true },
    Subscription: { tock: { subscribe: tickStream } },
  },
  cacheRedirects: { Query: { ghost: () => null } },
});
""",
    "api/src/resolvers.js": """
function me(_, __, { user }) { return user; }

module.exports = {
  Query: {
    launches: async (_, { pageSize }) => [],
    me,
  },
  Mutation: {
    async bookTrips(_, { ids }) { return {}; },
    login: function (_, { email }) { return email; },
    ...more,
  },
  Subscription: {
    tick: { subscribe: () => ticks() },
  },
  Launch: {
    site: () => null,
  },
};
""",
    "api/src/other.js": "export const resolvers = { Query: queryResolvers };\n",
    # A client package: its cache settings are not a resolver map.
    "web/package.json": '{"dependencies": {"@apollo/client": "3.9.0"}}',
    "web/src/cache.js": """
const cache = { typePolicies: { Query: { fields: { me: {} } } } };
const notAServer = { Query: { field: () => 1 } };
""",
}


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class ResolverMapTests(unittest.TestCase):
    def test_resolver_maps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root, APOLLO)
            output = asyncio.run(ServiceContractTool(root).analyze("r"))
        names = {
            n.properties["qualified_name"]: n.properties
            for n in output.nodes
            if "GraphqlField" in n.labels
        }
        handled = {}
        for r in output.relationships:
            if (
                r.type == "HANDLES"
                and "::graphql::" in r.target_match["qualified_name"]
            ):
                self.assertEqual(
                    r.properties["rule_id"], "service-contracts.graphql-resolver-map"
                )
                handled.setdefault(r.target_match["qualified_name"], []).append(
                    (r.properties["line"], r.source_match["qualified_name"])
                )
        found = sorted(
            (
                p["name"],
                p.get("declared_in"),
                [line for line, _ in handled.get(q, [])],
            )
            for q, p in names.items()
        )
        self.assertEqual(
            found,
            [
                ("Mutation.bookTrips", ["api/src/schema.js:10"], [10]),
                ("Mutation.login", ["api/src/schema.js:11"], [11]),
                ("Mutation.reset", ["api/src/server.js:7"], [15]),
                ("Query.launches", ["api/src/schema.js:6"], [6]),
                ("Query.me", ["api/src/schema.js:7"], [7]),
                ("Query.ping", ["api/src/server.js:4"], [14]),
                ("Subscription.tick", None, [15]),
                ("Subscription.tock", None, [16]),
            ],
        )
        # The named function `me` anchors its field to the function itself.
        sources = dict(handled["r::graphql::api::Query.me"])
        self.assertIn("me", sources[7])
        sources = dict(handled["r::graphql::api::Subscription.tock"])
        self.assertIn("tickStream", sources[16])
        unread = output.metadata.custom_metrics["unread"]
        self.assertEqual(unread["graphql:resolver_spread"], 1)
        self.assertEqual(unread["graphql:resolver_map_not_literal"], 1)
        self.assertEqual(unread["graphql:resolved_not_declared"], 2)


if __name__ == "__main__":
    unittest.main()
