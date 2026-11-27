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
            # A file that failed to parse has no facts to cover, and a
            # language with no other file has no record.
            ("bad.go", "go", "failed"),
            ("bad.rs", "rust", "failed"),
        )
        calls = family(
            [
                ("p1", "a.py", "linked"),
                ("p2", "a.py", "unresolved"),
                ("p3", "b.py", "ambiguous"),
                ("g1", "g.go", "linked"),
                # One site of g.go is the index's, one the parser's.
                ("g2", "g.go", "linked"),
                ("t1", "t.ts", "linked"),
                ("t2", "u.ts", "linked"),
            ],
            {
                "p1": "scip-python@0.6.8",
                "p2": "pyright@1.1.409",
                "p3": "scip-python@0.6.8",
                "t1": "scip-typescript@0.3",
                "g2": "scip-go@0.1",
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
                "external": 0,
                "unresolved_reasons": {"unknown": 2},
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
                "resolver": ["scip-go@0.1", PARSER],
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
                "external": 0,
                "unresolved_reasons": {},
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

    def test_calls_into_libraries_are_external_not_unresolved(self):
        analyzed = result(("a.go", "go", "analyzed"), ("a.py", "python", "analyzed"))
        sites = [
            ("g1", "a.go", "linked"),
            ("g2", "a.go", "unresolved"),  # fmt.Println
            ("g3", "a.go", "unresolved"),  # len
            ("g4", "a.go", "unresolved"),  # a method of an unknown type
            ("p1", "a.py", "unresolved"),  # json.dumps
            ("p2", "a.py", "ambiguous"),
        ]
        calls = (
            [(i, f) for i, f, _ in sites],
            {i: s for i, _, s in sites},
            {i: "scip-go@0.1" for i in ("g1", "g2", "g3", "g4")}
            | {"p1": "pyright@1", "p2": "pyright@1"},
            {
                "g2": "scip_external",
                "g3": "scip_no_occurrence",
                "g4": "scip_target_not_declaration",
                "p1": "pyright_external",
                "p2": "multiple_declarations",
            },
        )
        records = {
            (r.family, r.language): r.to_dict()
            for r in edge_coverage(analyzed, PARSER, calls, ([], {}, {}))
        }
        go = records[("CALLS", "go")]
        self.assertEqual(
            {k: go[k] for k in ("status", "linked", "external", "unresolved")},
            {"status": "linked", "linked": 1, "external": 1, "unresolved": 2},
        )
        self.assertEqual(
            go["unresolved_reasons"],
            {"scip_no_occurrence": 1, "scip_target_not_declaration": 1},
        )
        python = records[("CALLS", "python")]
        self.assertEqual(
            (python["external"], python["ambiguous"], python["unresolved"]),
            (1, 1, 0),
        )
        self.assertEqual(python["unresolved_reasons"], {"multiple_declarations": 1})


if __name__ == "__main__":
    unittest.main()
