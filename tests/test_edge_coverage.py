"""Edge coverage per language and family (deepthought.edge_coverage)."""

from types import SimpleNamespace
import unittest

from deepthought.edge_coverage import edge_coverage
from deepthought.models import FileCoverage

PARSER = "tree-sitter@1"


def result(*files):
    return SimpleNamespace(
        coverage=tuple(
            FileCoverage(f, language, status) for f, language, status in files
        )
    )


def family(sites, producers):
    """Sites as (id, file, status); producers by site id."""
    return (
        [(i, f) for i, f, _ in sites],
        {i: status for i, _, status in sites},
        producers,
    )


class EdgeCoverageTests(unittest.TestCase):
    def test_statuses_resolvers_and_counts(self):
        analyzed = result(
            ("a.py", "python", "analyzed"),
            ("b.py", "python", "partial"),
            ("empty.py", "python", "analyzed"),
            ("g.go", "go", "analyzed"),
            ("t.ts", "typescript", "analyzed"),
            ("u.ts", "typescript", "analyzed"),
            ("j.js", "javascript", "analyzed"),
            # A file that failed to parse has no facts to cover.
            ("bad.go", "go", "failed"),
        )
        calls = family(
            [
                ("p1", "a.py", "linked"),
                ("p2", "a.py", "unresolved"),
                ("p3", "b.py", "ambiguous"),
                ("g1", "g.go", "linked"),
                ("t1", "t.ts", "linked"),
                ("t2", "u.ts", "linked"),
            ],
            {
                "p1": "scip-python@0.6.8",
                "p2": "pyright@1.1.409",
                "p3": "scip-python@0.6.8",
                "t1": "scip-typescript@0.3",
            },
        )
        imports = family([("i1", "a.py", "linked")], {})
        records = {
            (r.family, r.language): r.to_dict()
            for r in edge_coverage(analyzed, PARSER, calls, imports)
        }
        self.assertEqual(
            sorted(records),
            [
                (family, language)
                for family in ("CALLS", "IMPORTS")
                for language in ("go", "javascript", "python", "typescript")
            ],
        )
        # Every file with calls is decided by a deterministic resolver.
        self.assertEqual(
            records[("CALLS", "python")],
            {
                "language": "python",
                "family": "CALLS",
                "status": "linked",
                "resolver": ["pyright@1.1.409", "scip-python@0.6.8"],
                "files": 2,
                "files_linked": 2,
                "linked": 1,
                "ambiguous": 1,
                "unresolved": 1,
                "reason": None,
            },
        )
        # One file of two: partial; the parser decided the other.
        self.assertEqual(
            {
                k: records[("CALLS", "typescript")][k]
                for k in ("status", "resolver", "files", "files_linked", "reason")
            },
            {
                "status": "partial",
                "resolver": ["scip-typescript@0.3", PARSER],
                "files": 2,
                "files_linked": 1,
                "reason": "parser_rules",
            },
        )
        self.assertEqual(
            {
                k: records[("CALLS", "go")][k]
                for k in ("status", "resolver", "files", "files_linked", "reason")
            },
            {
                "status": "not_linked",
                "resolver": [PARSER],
                "files": 1,
                "files_linked": 0,
                "reason": "parser_rules",
            },
        )
        # No call or import in the language: nothing to link.
        self.assertEqual(
            records[("CALLS", "javascript")],
            {
                "language": "javascript",
                "family": "CALLS",
                "status": "no_sites",
                "resolver": [],
                "files": 0,
                "files_linked": 0,
                "linked": 0,
                "ambiguous": 0,
                "unresolved": 0,
                "reason": None,
            },
        )
        self.assertEqual(
            (
                records[("IMPORTS", "python")]["status"],
                records[("IMPORTS", "python")]["linked"],
                records[("IMPORTS", "go")]["status"],
            ),
            ("not_linked", 1, "no_sites"),
        )


if __name__ == "__main__":
    unittest.main()
