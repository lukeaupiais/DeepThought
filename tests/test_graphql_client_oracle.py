"""The GraphQL client label builder: parser-based, independent of the engine."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

CLIENT = """import { gql } from '@apollo/client';
import { TILE } from './tile';

export const LAUNCHES = gql`
  query Launches {
    isLoggedIn @client
    launches {
      ...Tile
    }
    __typename
  }
  ${TILE}
`;

export const BOOK = gql`
  mutation Book($id: ID!) {
    bookTrip(id: $id)
    shared
  }
`;
"""
SERVER = """const typeDefs = gql`
  type Query {
    launches: [Launch]
    shared: Int
  }
  type Mutation {
    bookTrip(id: ID!): Boolean
    shared: Int
  }
`;
"""
OTHER = "extend type Mutation {\n  shared: Int\n}\n"


def _graphql_js():
    """Whether node can load graphql and typescript (NODE_PATH)."""
    node = os.environ.get("NODE") or shutil.which("node")
    if not node:
        return False
    probe = subprocess.run(
        [node, "-e", "require('graphql'); require('typescript')"],
        capture_output=True,
    )
    return probe.returncode == 0


@unittest.skipUnless(_graphql_js(), "node with graphql-js and typescript is required")
class OracleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        for path, text in (
            ("web/src/pages.tsx", CLIENT),
            ("api/src/schema.js", SERVER),
            ("api/src/test.js", "const Q = gql`query { launches { id } }`;\n"),
            ("other/schema.graphql", OTHER),
        ):
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(text, "utf-8")
        self.spec = {
            "corpus": "t",
            "repo": "r",
            "commit": "c",
            "license": "MIT",
            "languages": [],
            "servers": [
                {"name": "api", "root": "api", "schema": ["api/src/schema.js"]},
                {"name": "other", "root": "other", "schema": ["other/schema.graphql"]},
            ],
            "clients": [
                {"service": "web", "root": "web"},
                {"service": "api", "root": "api", "dont_care": "own tests"},
            ],
        }

    def label(self):
        import graphql_client_oracle

        return graphql_client_oracle.label(self.spec, self.root)

    def test_root_fields_link_to_the_one_server_that_declares_them(self):
        labels = self.label()
        self.assertEqual(
            [(x["contract"], x["client"], x["server"]) for x in labels["links"]],
            [
                (
                    "Query.launches",
                    {"file": "web/src/pages.tsx", "line": 7},
                    {"file": "api/src/schema.js", "line": 3},
                ),
                (
                    "Mutation.bookTrip",
                    {"file": "web/src/pages.tsx", "line": 17},
                    {"file": "api/src/schema.js", "line": 7},
                ),
            ],
        )
        # Declared by two scopes: no label, listed as ambiguous.
        self.assertEqual(
            labels["ambiguous_clients"],
            [
                {
                    "file": "web/src/pages.tsx",
                    "line": 18,
                    "contract": "Mutation.shared",
                    "servers": ["api", "other"],
                }
            ],
        )
        self.assertEqual(
            labels["dont_care_call_sites"],
            [{"file": "api/src/test.js", "line": 1, "reason": "own tests"}],
        )

    def test_a_client_only_reaches_its_own_servers(self):
        self.spec["clients"][0]["servers"] = ["other"]
        labels = self.label()
        self.assertEqual([x["contract"] for x in labels["links"]], ["Mutation.shared"])
        self.assertEqual(
            [x["contract"] for x in labels["unmatched_clients"]],
            ["Query.launches", "Mutation.bookTrip"],
        )


if __name__ == "__main__":
    unittest.main()
