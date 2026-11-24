import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import sqlc_table_labels  # noqa: E402

GENERATED = """package db

const createEntry = `-- name: CreateEntry :one
INSERT INTO entries (account_id) VALUES ($1) RETURNING id
`

func (q *Queries) CreateEntry(ctx context.Context, accountID int64) (Entry, error) {
	row := q.db.QueryRow(ctx, createEntry, accountID)
	var i Entry
	return i, row.Scan(&i.ID)
}

const listEntries = `-- name: ListEntries :many
SELECT id FROM entries WHERE account_id = $1
`

func (q *Queries) ListEntries(ctx context.Context, accountID int64) ([]Entry, error) {
	rows, err := q.db.Query(ctx, listEntries, accountID)
	return nil, err
}
"""


def table(name):
    return {"catalog": "", "schema": "", "name": name}


QUERIES = [
    {
        "name": "CreateEntry",
        "text": "INSERT INTO entries (account_id) VALUES ($1) RETURNING id",
        "insert_into_table": table("entries"),
        "columns": [{"name": "id", "table": table("entries")}],
        "params": [],
    },
    {
        "name": "ListEntries",
        "text": "-- a comment\nSELECT id FROM entries WHERE account_id = $1",
        "columns": [{"name": "id", "table": table("entries")}],
        "params": [{"column": {"name": "account_id", "table": table("entries")}}],
    },
    {
        "name": "DeleteJoined",
        "text": "DELETE FROM a USING b WHERE a.id = b.id AND b.x = $1",
        "columns": [],
        "params": [
            {"column": {"table": table("b")}},
            {"column": {"table": table("a")}},
        ],
    },
]


class SqlcTableLabelsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "db/sqlc").mkdir(parents=True)
        (self.root / "db/sqlc/entry.sql.go").write_text(GENERATED, "utf-8")

    def test_each_query_is_labelled_at_the_call_that_runs_it(self):
        self.assertEqual(
            sqlc_table_labels.call_sites(self.root, "db/sqlc"),
            {
                "CreateEntry": ("db/sqlc/entry.sql.go", 8),
                "ListEntries": ("db/sqlc/entry.sql.go", 18),
            },
        )
        with mock.patch.object(
            sqlc_table_labels, "analyze", return_value=[("db/sqlc", q) for q in QUERIES]
        ):
            result = sqlc_table_labels.labels(self.root, "sqlc", "sqlc.yaml")
        self.assertEqual(
            result["accesses"],
            [
                {
                    "file": "db/sqlc/entry.sql.go",
                    "line": 8,
                    "query": "CreateEntry",
                    "tables": [["entries", "WRITES"]],
                },
                {
                    "file": "db/sqlc/entry.sql.go",
                    "line": 18,
                    "query": "ListEntries",
                    "tables": [["entries", "READS"]],
                },
            ],
        )
        # No generated call, and a write whose target is not single.
        self.assertEqual(
            result["unlabelled"], [{"query": "DeleteJoined", "reason": "no call site"}]
        )
        self.assertFalse(sqlc_table_labels._WRITE.match("SELECT 1"))
        self.assertTrue(sqlc_table_labels._WRITE.match("\n  update t set x = 1"))


if __name__ == "__main__":
    unittest.main()
