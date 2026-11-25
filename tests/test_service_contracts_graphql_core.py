"""GraphQL schemas and operations parsed by graphql-core."""

import builtins
import unittest
from unittest import mock

from deepthought.service_contracts.graphql import (
    GraphqlCoreMissing,
    operation_fields,
    schema_fields,
)

SCHEMA = '''
"""
type Query { notAField: Int }
"""
type Query {
  "the books"
  books(filter: BookFilter = {title: "x"}): [Book!]! @deprecated(reason: "y")
  book(id: ID!): Book
}

extend type Query {
  author(id: ID!): Author
}

type Mutation {
  addBook(input: BookInput!): Book
}

type Book { id: ID! title: String }
'''

RENAMED = """
schema { query: RootQuery }
type RootQuery { books: [Book] }
"""

OPERATIONS = """
query Books($first: Int) @cached {
  first: books(first: $first) { id ...BookParts }
  __typename
  cartOpen @client
  ...RootParts
}
${BOOK_PARTS}
mutation { addBook(input: {title: "a"}) { id } }
fragment BookParts on Book { title }
"""


class GraphqlCoreTests(unittest.TestCase):
    def test_schema_root_fields(self):
        declared, unread = schema_fields(SCHEMA, "schema.graphql")
        self.assertEqual(
            [(d.line, d.operation, d.field) for d in declared],
            [
                (7, "Query", "books"),
                (8, "Query", "book"),
                (12, "Query", "author"),
                (16, "Mutation", "addBook"),
            ],
        )
        self.assertEqual(unread, [])

    def test_renamed_roots_are_unread(self):
        declared, unread = schema_fields(RENAMED, "schema.graphql")
        self.assertEqual(declared, [])
        self.assertEqual([u.reason for u in unread], ["schema_root_renamed"])

    def test_operation_root_fields(self):
        found, unread = operation_fields(
            OPERATIONS, "client.ts", line_offset=10, column_offset=20, caller_id="c"
        )
        self.assertEqual(
            [(o.line, o.column, o.operation, o.field, o.name) for o in found],
            [
                (13, 9, "Query", "books", "Books"),
                (19, 11, "Mutation", "addBook", None),
            ],
        )
        self.assertEqual(
            [(u.line, u.reason) for u in unread], [(16, "client_fragment_root")]
        )

    def test_unparsable_text_is_unread(self):
        found, unread = operation_fields(
            "query { books { id }", "x.ts", line_offset=4
        )  # unbalanced
        self.assertEqual(found, [])
        self.assertEqual(
            [(u.line, u.reason) for u in unread], [(5, "operation_not_parsed")]
        )
        declared, unread = schema_fields("type Query { books: ", "s.graphql")
        self.assertEqual(declared, [])
        self.assertEqual([u.reason for u in unread], ["schema_not_parsed"])

    def test_missing_graphql_core_fails_loudly(self):
        real = builtins.__import__

        def without_graphql(name, *args, **kwargs):
            if name == "graphql":
                raise ImportError(name)
            return real(name, *args, **kwargs)

        with mock.patch("builtins.__import__", without_graphql):
            with self.assertRaises(GraphqlCoreMissing):
                schema_fields(SCHEMA, "schema.graphql")


if __name__ == "__main__":
    unittest.main()
