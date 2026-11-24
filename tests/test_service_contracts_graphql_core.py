"""GraphQL schemas and operations parsed by graphql-core."""

import importlib.util
import unittest

from deepthought.service_contracts.graphql import (
    _operation_fields_text,
    _schema_fields_text,
    operation_fields,
    schema_fields,
)

READY = importlib.util.find_spec("graphql") is not None

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


@unittest.skipUnless(READY, "graphql-core is not installed")
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
        self.assertEqual(
            (declared, unread), _schema_fields_text(SCHEMA, "schema.graphql")
        )

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
        self.assertEqual(
            (found, unread),
            _operation_fields_text(OPERATIONS, "client.ts", 10, 20, "c"),
        )

    def test_unparsable_text_falls_back(self):
        source = "query { books { id }"  # unbalanced
        self.assertEqual(
            operation_fields(source, "x.graphql"),
            _operation_fields_text(source, "x.graphql"),
        )


if __name__ == "__main__":
    unittest.main()
