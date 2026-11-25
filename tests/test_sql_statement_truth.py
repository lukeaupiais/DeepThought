import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import ground_truth  # noqa: E402
import sql_statement_truth as truth  # noqa: E402

SCHEMA = """
CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE books (id INTEGER PRIMARY KEY, author_id INTEGER, title TEXT);
CREATE VIEW titles AS SELECT title FROM books;
"""


class NamedQueriesTest(unittest.TestCase):
    def test_queries_split_at_sqlc_name_comments(self):
        text = (
            "-- name: GetAuthor :one\nSELECT * FROM authors WHERE id = ?;\n\n"
            "-- name: DeleteAuthor :exec\nDELETE FROM authors WHERE id = ?;\n"
        )
        self.assertEqual(
            [name for name, _ in truth.named_queries(text)],
            ["GetAuthor", "DeleteAuthor"],
        )


class SqliteTablesTest(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schema.sql"
            path.write_text(SCHEMA, "utf-8")
            self.db, self.views = truth.sqlite_schema([path])

    def test_sqlite_reports_reads_and_writes(self):
        self.assertEqual(
            truth.sqlite_tables(
                self.db,
                self.views,
                "INSERT INTO books (author_id, title) "
                "SELECT id, name FROM authors WHERE id = sqlc.arg(id)",
            ),
            (["authors"], ["books"]),
        )

    def test_a_query_naming_a_view_is_skipped(self):
        self.assertIsNone(
            truth.sqlite_tables(self.db, self.views, "SELECT title FROM titles")
        )

    def test_a_query_sqlite_rejects_is_skipped(self):
        self.assertIsNone(
            truth.sqlite_tables(self.db, self.views, "SELECT * FROM missing")
        )


@unittest.skipUnless(importlib.util.find_spec("pglast"), "pglast not installed")
class PostgresTablesTest(unittest.TestCase):
    def test_cte_names_and_locking_aliases_are_not_tables(self):
        sql = (
            "WITH moved AS (DELETE FROM queue q WHERE id = @id RETURNING *) "
            "INSERT INTO archive SELECT * FROM moved JOIN owners o ON true "
            "FOR UPDATE OF o"
        )
        self.assertEqual(truth.postgres_tables(sql), (["owners"], ["archive", "queue"]))

    def test_ddl_is_not_labelled(self):
        self.assertIsNone(truth.postgres_tables("CREATE TABLE t (id int)"))


class ScoreSqlStatementsTest(unittest.TestCase):
    def labels(self, engine, reads, writes):
        return {
            "statements": [
                {
                    "case": "c/query.sql",
                    "engine": engine,
                    "name": "Q",
                    "sql": "INSERT INTO books SELECT * FROM authors "
                    "LEFT JOIN extras ON true",
                    "reads": reads,
                    "writes": writes,
                }
            ]
        }

    def test_an_exact_match_is_found(self):
        report, _ = ground_truth.score_sql_statements(
            self.labels("postgresql", ["authors", "extras"], ["books"])
        )
        self.assertEqual((report["found"], report["wrong"]), (1, 0))

    def test_a_different_table_set_is_wrong(self):
        report, samples = ground_truth.score_sql_statements(
            self.labels("postgresql", ["authors"], ["books"])
        )
        self.assertEqual(report["wrong"], 1)
        self.assertEqual(samples["wrong"][0][0], "c/query.sql:Q")

    def test_sqlite_does_not_judge_a_read_the_planner_dropped(self):
        report, _ = ground_truth.score_sql_statements(
            self.labels("sqlite", ["authors"], ["books"])
        )
        self.assertEqual(report["found"], 1)


if __name__ == "__main__":
    unittest.main()
