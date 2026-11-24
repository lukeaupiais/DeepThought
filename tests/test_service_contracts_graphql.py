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


if __name__ == "__main__":
    unittest.main()
