"""The ground-truth scorer judges call links against an oracle, not the engine.

Truth is derived from the checked-in SCIP fixtures (scip-python and
scip-typescript output) and the scorer must confirm the engine's links,
contradict a link the truth disagrees with, and flag regressions against a
baseline. The pinned real-repository corpus runs with
``python tools/ground_truth.py check`` (it needs network to fetch).
"""

import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import ground_truth  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
READY = (
    importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_python")
    and importlib.util.find_spec("google.protobuf")
)


@unittest.skipUnless(READY, "Python grammar and protobuf are required")
class GroundTruthTest(unittest.TestCase):
    def truth(self):
        return ground_truth.derive(FIXTURES / "python", FIXTURES / "scip/python.scip")

    def test_derived_calls_are_the_oracle_targets(self):
        expected, indexed = self.truth()
        self.assertEqual(
            expected,
            {
                ("main.py", 11, "Widget"): {("models.py", 1, "callable")},
                ("main.py", 15, "alternate"): {("other/helpers.py", 1, "callable")},
                ("main.py", 15, "helper"): {("lib/helpers.py", 1, "callable")},
                ("main.py", 19, "missing_function"): {("-", 0, "local")},
            },
        )
        self.assertIn("main.py", indexed)

    def test_truth_round_trips_through_tsv(self):
        stream = io.StringIO()
        ground_truth.write_truth(*self.truth(), stream)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "calls.tsv")
            path.write_text(stream.getvalue())
            self.assertEqual(ground_truth.read_truth(path), self.truth())

    def test_engine_links_are_confirmed(self):
        result = ground_truth.analyze(FIXTURES / "python", ("python",))
        report, _ = ground_truth.score(result, self.truth())
        self.assertEqual(
            (report["expected"], report["confirmed"], report["contradicted"]),
            (3, 3, 0),
        )
        self.assertEqual((report["precision"], report["recall"]), (1.0, 1.0))

    def test_a_link_the_oracle_disagrees_with_is_contradicted(self):
        expected, indexed = self.truth()
        expected[("main.py", 15, "helper")] = {("other/helpers.py", 1, "callable")}
        # A call the oracle binds locally must not link to a declaration.
        expected[("main.py", 11, "Widget")] = {("main.py", 3, "local")}
        result = ground_truth.analyze(FIXTURES / "python", ("python",))
        report, samples = ground_truth.score(result, (expected, indexed))
        self.assertEqual((report["confirmed"], report["contradicted"]), (1, 2))
        self.assertEqual(report["expected"], 2)
        self.assertEqual(
            sorted(s[:3] for s in samples["contradicted"]),
            [["main.py", 11, "Widget"], ["main.py", 15, "helper"]],
        )

    def test_regressions_against_the_baseline(self):
        baseline = {"expected": 10, "confirmed": 8, "contradicted": 0}
        self.assertEqual(ground_truth.regressions(dict(baseline), baseline), [])
        worse = {"expected": 10, "confirmed": 7, "contradicted": 1}
        self.assertEqual(
            ground_truth.regressions(worse, baseline),
            ["contradicted 0 -> 1", "confirmed 8 -> 7"],
        )
        better = {"expected": 10, "confirmed": 9, "contradicted": 0}
        self.assertEqual(ground_truth.regressions(better, baseline), [])

    def test_a_rule_that_gets_worse_is_named(self):
        baseline = {
            "expected": 10,
            "confirmed": 8,
            "contradicted": 0,
            "by_rule": {"a.x": {"confirmed": 5}, "a.y": {"confirmed": 3}},
        }
        # Same totals, but one rule lost a confirmed link another gained.
        report = {
            "expected": 10,
            "confirmed": 8,
            "contradicted": 0,
            "by_rule": {"a.x": {"confirmed": 6}, "a.y": {"confirmed": 2}},
        }
        self.assertEqual(
            ground_truth.regressions(report, baseline), ["a.y confirmed 3 -> 2"]
        )

    def test_scores_are_broken_down_by_rule(self):
        result = ground_truth.analyze(FIXTURES / "python", ("python",))
        report, _ = ground_truth.score(result, self.truth())
        self.assertEqual(
            sum(c.get("confirmed", 0) for c in report["by_rule"].values()), 3
        )
        coverage = ground_truth.rule_coverage({"fixture": report})
        self.assertIn("tree-sitter.call-imported-name", coverage["rules"])
        self.assertIn("tree-sitter.rust-call-path", coverage["unverified"])
        self.assertNotIn("tree-sitter.python-from-import-submodule", coverage["rules"])


JAVA_SOURCES = {
    "src/a/Base.java": "package a;\npublic class Base {\n  @Deprecated\n  public void run() {}\n}\n",
    "src/b/Square.java": "package b;\nimport a.Base;\n"
    "public class Square extends Base {\n"
    "  Square() { run(); helper(2); new Base(); String.valueOf(1); }\n"
    "  static int helper(int x) { return x; }\n}\n",
}


@unittest.skipUnless(
    READY and shutil.which("java") and importlib.util.find_spec("tree_sitter_java"),
    "A JDK and the Java grammar are required",
)
class JavaTruthTest(unittest.TestCase):
    def test_javac_binds_calls_at_their_name_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, content in JAVA_SOURCES.items():
                path = Path(directory, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            self.assertEqual(ground_truth.java_source_roots(directory), ["src"])
            truth = ground_truth.derive_java(directory)
            result = ground_truth.analyze(directory, ("java",))
        expected, indexed = truth
        self.assertEqual(
            expected,
            {
                # The name line, not the annotation line the engine records.
                ("src/b/Square.java", 4, "run"): {("src/a/Base.java", 4, "callable")},
                ("src/b/Square.java", 4, "helper"): {
                    ("src/b/Square.java", 5, "callable")
                },
                # An implicit constructor binds its class.
                ("src/b/Square.java", 4, "Base"): {("src/a/Base.java", 2, "callable")},
                ("src/b/Square.java", 4, "valueOf"): {("-", 0, "external")},
            },
        )
        self.assertEqual(indexed, set(JAVA_SOURCES))
        report, _ = ground_truth.score(result, truth)
        self.assertEqual((report["contradicted"], report["expected"]), (0, 3))
        self.assertEqual(report["confirmed"], 3)

    def test_javac_binds_declared_bases(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, content in JAVA_SOURCES.items():
                path = Path(directory, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            truth = ground_truth.derive_java(directory, bases=True)
            result = ground_truth.analyze(directory, ("java",))
        self.assertEqual(
            truth[0],
            {("src/b/Square.java", 3, "Square"): {("src/a/Base.java", 2, "type")}},
        )
        report, _ = ground_truth.score_inherits(result, truth)
        self.assertEqual((report["confirmed"], report["recall"]), (1, 1.0))


@unittest.skipUnless(
    importlib.util.find_spec("tree_sitter_typescript")
    and importlib.util.find_spec("google.protobuf"),
    "TypeScript grammar and protobuf are required",
)
class InheritsTruthTest(unittest.TestCase):
    WORKSPACE = FIXTURES / "scip_implementations"

    def test_direct_supertypes_from_relationships(self):
        expected, _ = ground_truth.derive_inherits(
            self.WORKSPACE, self.WORKSPACE / "index.scip"
        )
        self.assertEqual(expected[("a.ts", 11, "Square")], {("a.ts", 10, "type")})
        # scip-typescript records no interface `extends`, so Animal looks
        # like a direct supertype of Dog; the engine reaches it through Pet.
        self.assertEqual({t[1] for t in expected[("a.ts", 4, "Dog")]}, {1, 2, 3})
        result = ground_truth.analyze(self.WORKSPACE, ("typescript",))
        report, _ = ground_truth.score_inherits(result, (expected, set()))
        self.assertEqual(
            (report["confirmed"], report["contradicted"], report["recall"]),
            (4, 0, 1.0),
        )

    def test_a_wrong_supertype_is_contradicted(self):
        expected, indexed = ground_truth.derive_inherits(
            self.WORKSPACE, self.WORKSPACE / "index.scip"
        )
        expected[("a.ts", 11, "Square")] = {("a.ts", 3, "type")}
        result = ground_truth.analyze(self.WORKSPACE, ("typescript",))
        report, samples = ground_truth.score_inherits(result, (expected, indexed))
        self.assertEqual(report["contradicted"], 1)
        self.assertEqual(samples["contradicted"][0][2], "Shape")
        # An own-package supertype the index never defines is not judged.
        expected[("a.ts", 11, "Square")] = {("-", 0, "unknown")}
        report, _ = ground_truth.score_inherits(result, (expected, indexed))
        self.assertEqual(report["contradicted"], 0)


class AppliedTest(unittest.TestCase):
    def test_argument_lists_after_generics(self):
        for rest in ("(x)", " (x)", "::<u8>(x)", "<T>(x)", "<Map<K, V>>()"):
            self.assertTrue(ground_truth._applied(rest), rest)
        for rest in ("", ".x()", "!(x)", " = f(x)", "[0](x)"):
            self.assertFalse(ground_truth._applied(rest), rest)

    def test_declarations_are_not_calls(self):
        for before in ("def ", "    fn ", "func ", "func (r *T) ", "function "):
            self.assertTrue(ground_truth._DECLARES.search(before), before)
        for before in ("x = ", "return ", "self.", "await "):
            self.assertFalse(ground_truth._DECLARES.search(before), before)

    def test_utf16_columns(self):
        line = "é = f(x)"
        self.assertEqual(ground_truth._slice(line, 4, 5, "utf-16"), "f")
        self.assertEqual(ground_truth._slice(line, 5, 6, "utf-8"), "f")


class CorpusFilesTest(unittest.TestCase):
    def test_every_corpus_is_pinned_and_has_truth_and_baseline(self):
        corpus = ROOT / "evals" / "ground_truth"
        manifest = json.loads((corpus / "manifest.json").read_text())
        baseline = json.loads((corpus / "baseline.json").read_text())
        for entry in manifest["corpora"]:
            self.assertRegex(entry["commit"], r"^[0-9a-f]{40}$")
            self.assertTrue((corpus / entry["truth"]).is_file())
            self.assertIn(entry["name"], baseline)
            expected, _ = ground_truth.read_truth(corpus / entry["truth"])
            callable_keys = sum(
                any(t[2] == "callable" for t in targets)
                for targets in expected.values()
            )
            self.assertEqual(callable_keys, baseline[entry["name"]]["expected"])


if __name__ == "__main__":
    unittest.main()
