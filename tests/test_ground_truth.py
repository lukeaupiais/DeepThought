"""The ground-truth scorer judges call links against an oracle, not the engine.

Truth is derived from the checked-in SCIP fixtures (scip-python and
scip-typescript output) and the scorer must confirm the engine's links,
contradict a link the truth disagrees with, and flag regressions against a
baseline. The pinned real-repository corpus runs with
``python tools/ground_truth.py check`` (it needs network to fetch).
"""

from contextlib import redirect_stderr
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
import c_declaration_truth  # noqa: E402
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

    @unittest.skipUnless(
        importlib.util.find_spec("tree_sitter_typescript"), "TypeScript grammar"
    )
    def test_a_private_name_matches_the_index_name_without_its_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "a.ts").write_text(
                "class K {\n  #run() {}\n  go() { this.#run(); }\n}\n"
            )
            result = ground_truth.analyze(Path(directory), ("typescript",))
        truth = ({("a.ts", 3, "run"): {("a.ts", 2, "callable")}}, {"a.ts"})
        report, _ = ground_truth.score(result, truth)
        self.assertEqual((report["confirmed"], report["missed"]), (1, 0))

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


@unittest.skipUnless(READY, "tree-sitter and grammars are required")
class ImportTruthTests(unittest.TestCase):
    """Module references in a hand-built index give each import's target."""

    SOURCES = {
        "pkg/__init__.py": "",
        "pkg/b.py": "def f():\n    return 1\n",
        "pkg/a.py": "from .b import f\nimport os\nfrom pkg import b\nb.f()\n",
    }

    def setUp(self):
        from deepthought._scip import scip_pb2

        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        for name, text in self.SOURCES.items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(text)
        head = "scip-python python pkg "
        index = scip_pb2.Index()

        def document(path, *occurrences):
            doc = index.documents.add(relative_path=path)
            for line, column, symbol, roles in occurrences:
                doc.occurrences.add(
                    range=[line, column, column + 1], symbol=symbol, symbol_roles=roles
                )

        document("pkg/__init__.py", (0, 0, head + "abc `pkg`/__init__:", 1))
        document("pkg/b.py", (0, 0, head + "abc `pkg.b`/__init__:", 1))
        document(
            "pkg/a.py",
            (0, 0, head + "abc `pkg.a`/__init__:", 1),
            # scip-python names a relative import's module without its
            # package, and an installed package's version, not the commit.
            (0, 5, head + "1.0 b/__init__:", 0),
            (1, 7, "scip-python python python-stdlib 3.11 os/__init__:", 0),
            (2, 5, head + "1.0 `pkg`/__init__:", 0),
            (2, 16, head + "1.0 `pkg.b`/__init__:", 0),
            # A module used in code is not an import.
            (3, 0, head + "1.0 `pkg.b`/__init__:", 0),
        )
        self.index = self.root / "index.scip"
        self.index.write_bytes(index.SerializeToString())

    def test_derive_imports(self):
        expected, indexed = ground_truth.derive_imports(self.root, self.index, "python")
        self.assertEqual(indexed, set(self.SOURCES))
        self.assertEqual(
            expected,
            {
                ("pkg/a.py", 1, "import"): {("pkg/b.py", 0, "module")},
                ("pkg/a.py", 2, "import"): {("-", 0, "external")},
                ("pkg/a.py", 3, "import"): {("pkg/b.py", 0, "module")},
            },
        )

    def test_score_imports(self):
        truth = ground_truth.derive_imports(self.root, self.index, "python")
        result = ground_truth.analyze(self.root, ["python"])
        report, samples = ground_truth.score_imports(result, truth)
        self.assertEqual(report["expected"], 2)
        self.assertEqual(report["contradicted"], 0)
        self.assertEqual(report["recall"], 1.0)
        self.assertEqual(report["confirmed"], 2)
        self.assertEqual(samples["missed"], [])
        # A truth naming another module contradicts the engine's link.
        wrong = (
            {**truth[0], ("pkg/a.py", 1, "import"): {("pkg/__init__.py", 0, "module")}},
            truth[1],
        )
        report, samples = ground_truth.score_imports(result, wrong)
        self.assertEqual(report["contradicted"], 1)
        self.assertEqual(samples["missed"], [["pkg/a.py", 1]])


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
            if "imports" in entry:
                imports, _ = ground_truth.read_truth(corpus / entry["imports"])
                repository = sum(
                    any(t[2] == "module" for t in targets)
                    for targets in imports.values()
                )
                self.assertEqual(
                    repository, baseline[entry["name"]]["imports"]["expected"]
                )

    def test_every_contract_corpus_matches_its_labels_and_baseline(self):
        corpus = ROOT / "evals" / "ground_truth"
        manifest = json.loads((corpus / "manifest.json").read_text())
        baseline = json.loads((corpus / "baseline.json").read_text())["contracts"]
        self.assertTrue(manifest["contracts"])
        for entry in manifest["contracts"]:
            labels = json.loads((corpus / entry["labels"]).read_text())
            self.assertEqual(
                (labels["repo"], labels["commit"]), (entry["url"], entry["commit"])
            )
            self.assertEqual(
                entry["kinds"], sorted({link["kind"] for link in labels["links"]})
            )
            for kind in entry["kinds"]:
                in_scope = sum(
                    link["kind"] == kind and link.get("in_scope_static", True)
                    for link in labels["links"]
                )
                counts = baseline[entry["name"]][kind]["call_sites"]
                self.assertEqual(counts["labelled_in_scope"], in_scope)


class RequirementTests(unittest.TestCase):
    MANIFEST = {
        "corpora": [{"name": "a", "languages": ["python"]}],
        "contracts": [{"name": "contracts-b"}],
    }

    def test_missing_grammar_or_yaml_is_reported(self):
        from unittest import mock

        real = importlib.util.find_spec
        absent = {"yaml", "tree_sitter_python"}
        with mock.patch(
            "importlib.util.find_spec",
            side_effect=lambda name, *a: None if name in absent else real(name, *a),
        ):
            self.assertEqual(
                ground_truth.missing_requirements(self.MANIFEST),
                ["tree_sitter_python", "yaml"],
            )
            # Only what the selected corpora need.
            self.assertEqual(
                ground_truth.missing_requirements(self.MANIFEST, {"a"}),
                ["tree_sitter_python"],
            )

    def test_check_refuses_to_score_without_them(self):
        from unittest import mock

        with (
            mock.patch.object(
                ground_truth, "missing_requirements", return_value=["yaml"]
            ),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(ground_truth.main(["check", "--corpus", "x"]), 2)
        self.assertIn("not installed: yaml", stderr.getvalue())


class DeclarationRegressionTests(unittest.TestCase):
    def test_declaration_regressions(self):
        base = {"expected": 10, "found": 8, "kind_mismatch": 1, "extra": 2}
        regressions = ground_truth.declaration_regressions
        self.assertEqual(regressions(dict(base), base), [])
        self.assertEqual(
            regressions({**base, "found": 7, "extra": 3}, base),
            ["found 8 -> 7", "extra 2 -> 3"],
        )
        self.assertIn(
            "expected 10 -> 11", regressions({**base, "expected": 11}, base)[0]
        )

    def test_every_declaration_corpus_has_truth_and_baseline(self):
        corpus = ROOT / "evals" / "ground_truth"
        manifest = json.loads((corpus / "manifest.json").read_text())
        baseline = json.loads((corpus / "baseline.json").read_text())
        for entry in manifest["declarations"]:
            self.assertRegex(entry["commit"], r"^[0-9a-f]{40}$")
            rows, _ = c_declaration_truth.read(corpus / entry["truth"])
            self.assertEqual(
                len(rows), baseline["declarations"][entry["name"]]["expected"]
            )


class SummaryTests(unittest.TestCase):
    def test_changed_scores_show_their_difference(self):
        base = {
            "a": {
                "confirmed": 10,
                "contradicted": 0,
                "recall": 0.5,
                "impact": {"confirmed": 4, "extra": 1, "recall": 0.2, "precision": 0.8},
            },
            "declarations": {"d": {"found": 5, "kind_mismatch": 1, "extra": 0}},
        }
        reports = {
            "a": {
                "confirmed": 12,
                "contradicted": 0,
                "recall": 0.6,
                "impact": {
                    "confirmed": 6,
                    "extra": 1,
                    "recall": 0.3,
                    "precision": 0.8571,
                },
                "regressions": [],
            },
            "d": {
                "declarations": {"found": 5, "kind_mismatch": 2, "extra": 0},
                "regressions": ["kind_mismatch 1 -> 2"],
            },
        }
        text = ground_truth.summary(reports, base)
        self.assertIn(
            "| a | calls | confirmed 12 (**+2**), contradicted 0, recall 0.6 (**+0.1**) |",
            text,
        )
        self.assertIn("| a | impact | confirmed 6 (**+2**), extra 1,", text)
        self.assertIn("| d | **regressions** | kind_mismatch 1 -> 2 |", text)

    def test_route_corpora_show_each_kind(self):
        base = {"routes": {"r": {"http": {"expected": 3, "found": 2, "extra": 0}}}}
        reports = {
            "r": {
                "routes": {"http": {"expected": 3, "found": 3, "extra": 0}},
                "regressions": [],
            }
        }
        self.assertIn(
            "| r | routes (http) | found 3 (**+1**), extra 0, expected 3 |",
            ground_truth.summary(reports, base),
        )


class OverloadTargetTests(unittest.TestCase):
    """Every declaration of an @overload group is a target of its calls."""

    SOURCE = (
        "@overload\n"
        "def f(x: int) -> int: ...\n"
        "@overload\n"
        "def f(x: str) -> str: ...\n"
        "def f(x):\n"
        "    return x\n"
        "f(1)\n"
    )

    def test_overloads_and_implementation_are_targets(self):
        from deepthought._scip import scip_pb2

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        (root / "m.py").write_text(self.SOURCE)
        symbol = "scip-python python pkg 1.0 `m`/f()."
        index = scip_pb2.Index()
        index.metadata.tool_info.name = "scip-python"
        index.metadata.tool_info.version = "0.6.8"
        doc = index.documents.add(relative_path="m.py", position_encoding=1)
        # The first stub is the definition; the others, implementation
        # included, are references on their `def` lines; then the call.
        for line, roles in ((1, 1), (3, 8), (4, 8), (6, 8)):
            doc.occurrences.add(
                range=[line, 4 if line < 6 else 0, 5 if line < 6 else 1],
                symbol=symbol,
                symbol_roles=roles,
            )
        (root / "index.scip").write_bytes(index.SerializeToString())
        expected, _ = ground_truth.derive(root, root / "index.scip")
        self.assertEqual(
            expected,
            {
                ("m.py", 7, "f"): {
                    ("m.py", 2, "callable"),
                    ("m.py", 4, "callable"),
                    ("m.py", 5, "callable"),
                }
            },
        )


class ContractRegressionTests(unittest.TestCase):
    BASE = {
        "http": {
            "call_sites": {
                "true_positive": 3,
                "predicted": 4,
                "labelled_in_scope": 5,
            }
        }
    }

    def report(self, **counts):
        site = {**self.BASE["http"]["call_sites"], **counts}
        return {"http": {"call_sites": site}}

    def test_no_change_and_improvement_pass(self):
        regressions = ground_truth.contract_regressions
        self.assertEqual(regressions(self.report(), self.BASE), [])
        better = self.report(true_positive=4, predicted=4)
        self.assertEqual(regressions(better, self.BASE), [])

    def test_fewer_correct_or_more_wrong_links_fail(self):
        regressions = ground_truth.contract_regressions
        self.assertEqual(
            regressions(self.report(true_positive=2, predicted=3), self.BASE),
            ["http call_sites correct 3 -> 2"],
        )
        self.assertEqual(
            regressions(self.report(predicted=5), self.BASE),
            ["http call_sites wrong 1 -> 2"],
        )
        self.assertIn(
            "labelled 5 -> 6",
            regressions(self.report(labelled_in_scope=6), self.BASE)[0],
        )
        self.assertEqual(
            regressions({}, self.BASE), ["http call_sites: no longer scored"]
        )


if __name__ == "__main__":
    unittest.main()
