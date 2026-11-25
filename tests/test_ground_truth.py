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
import subprocess
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

    def test_javac_leaves_out_overloads_chosen_by_error_recovery(self):
        sources = {
            "src/a/G.java": (
                "package a;\n"
                "public class G {\n"
                "  public void put(Object o, Class<?> c) {}\n"
                "  public void put(String s, Appendable w) {}\n"
                "  public void only(Class<?> c) {}\n"
                "  void use() {\n"
                "    put(null, Missing.class);\n"
                "    only(Missing.class);\n"
                "    put(null, String.class);\n"
                "  }\n"
                "}\n"
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            for name, content in sources.items():
                path = Path(directory, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            expected, _ = ground_truth.derive_java(directory)
        # `Missing` is in no source: the overloaded put on line 7 is left
        # out, the single `only` is kept, and line 9 resolves normally.
        self.assertEqual(
            expected,
            {
                ("src/a/G.java", 8, "only"): {("src/a/G.java", 5, "callable")},
                ("src/a/G.java", 9, "put"): {("src/a/G.java", 3, "callable")},
            },
        )

    def test_a_call_on_an_erroneous_chain_is_a_possible_call(self):
        sources = {
            "src/a/B.java": (
                "package a;\n"
                "public class B {\n"
                "  public B with(Class<?> c) { return this; }\n"
                "  public String build() { return null; }\n"
                "  public String build(int n) { return null; }\n"
                "  void use() {\n"
                "    new B().with(Missing.class).build();\n"
                "  }\n"
                "}\n"
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            for name, content in sources.items():
                path = Path(directory, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            expected, _ = ground_truth.derive_java(directory)
            possible, _ = ground_truth.derive_java(directory, recovered=True)
        # `Missing` makes the chain erroneous, so javac leaves build()
        # unbound: not a call it rules on, but a possible call to the
        # build of B's declared type with no arguments.
        self.assertNotIn(("src/a/B.java", 7, "build"), expected)
        self.assertEqual(
            possible[("src/a/B.java", 7, "build")],
            {("src/a/B.java", 4, "callable")},
        )

    def test_javac_binds_constructor_chains(self):
        sources = {
            "src/a/Base.java": "package a;\npublic class Base {\n"
            "  public Base(int x) {}\n}\n",
            "src/b/Square.java": "package b;\nimport a.Base;\n"
            "public class Square extends Base {\n"
            "  Square() { this(1); }\n"
            "  Square(int x) { super(x); }\n"
            "  static Square make() { return new Square(); }\n}\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            for name, content in sources.items():
                path = Path(directory, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            chains, _ = ground_truth.derive_java(directory, chains=True)
        # Only this(...) and super(...), never the `new Square()` call site.
        self.assertEqual(
            chains,
            {
                ("src/b/Square.java", 4, "this"): {
                    ("src/b/Square.java", 5, "callable")
                },
                ("src/b/Square.java", 5, "super"): {("src/a/Base.java", 3, "callable")},
            },
        )


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


class ScipPythonRepairTest(unittest.TestCase):
    PKG = "scip-python python pkg 1.0 `pkg._models`/"
    MODELS = [
        '__all__ = ["Request", "Response"]',
        "class Request: ...",
        "class Response: ...",
    ]

    def setUp(self):
        self.defined = {
            self.PKG + "Request#": {("pkg/_models.py", 2, "callable")},
            self.PKG + "Response#": {("pkg/_models.py", 3, "callable")},
            self.PKG + "helper().": {("pkg/_models.py", 9, "callable")},
        }
        self.files = {"pkg/_models.py": self.MODELS, "use.py": ["import pkg"]}

    def repair(self, native, written, file="use.py"):
        return ground_truth._star_export(
            native, written, file, self.defined, lambda f: self.files.get(f, [])
        )

    def test_a_star_export_names_the_sibling_it_spells(self):
        # scip-python 0.6.8 records `pkg.Response(...)` as `_models/Request#`.
        self.assertEqual(
            self.repair(self.PKG + "Request#", "Response"), self.PKG + "Response#"
        )

    def test_matching_names_and_aliases_keep_their_symbol(self):
        self.assertEqual(
            self.repair(self.PKG + "Request#", "Request"), self.PKG + "Request#"
        )
        self.files["use.py"] = ["from pkg._models import Request as R"]
        self.assertEqual(self.repair(self.PKG + "Request#", "R"), self.PKG + "Request#")

    def test_a_name_it_cannot_place_is_unjudged(self):
        # Not exported by the symbol's module: a re-export through two stars.
        self.assertIsNone(self.repair(self.PKG + "Request#", "Transport"))
        self.assertIsNone(self.repair(self.PKG + "helper().", "other"))

    def test_external_and_member_symbols_are_left_alone(self):
        builtin = "scip-python python python-stdlib 3.11 builtins/set#"
        self.assertEqual(self.repair(builtin, "frozen"), builtin)
        member = self.PKG + "Request#send()."
        self.assertEqual(self.repair(member, "other"), member)

    def test_an_attribute_is_not_a_builtin(self):
        builtin = "scip-python python python-stdlib 3.11 builtins/set#"
        match = ground_truth._ATTRIBUTE_BUILTIN.search
        for written in ("        self.", "    more_cookies.", "f().", "x[0].", "a.b."):
            self.assertTrue(match(written + "\0" + builtin), written)
        for written in ("    x = ", "    builtins.", "y = builtins ."):
            self.assertFalse(match(written + "\0" + builtin), written)
        self.assertFalse(
            match(
                "        self.\0scip-python python python-stdlib 3.11 builtins/str#join()."
            )
        )


class LocalConstructorTest(unittest.TestCase):
    def test_finds_the_constructor_of_the_class_body(self):
        lines = [
            "describe('x', () => {",
            "  class Test extends Base {",
            "    name = '{ not a brace';",
            "    method() { return `}`; }",
            "    constructor(text) {",
            "      super(text);",
            "    }",
            "  }",
            "  const t = new Test('a');",
            "});",
        ]
        self.assertEqual(ground_truth._local_constructor(lines, 2), 5)

    def test_ignores_nested_and_absent_constructors(self):
        lines = [
            "class Outer {",
            "  make() {",
            "    return class { constructor() {} };",
            "  }",
            "}",
            "class Next {",
            "  constructor() {}",
            "}",
        ]
        self.assertIsNone(ground_truth._local_constructor(lines, 1))
        self.assertEqual(ground_truth._local_constructor(lines, 6), 7)


class AppliedTest(unittest.TestCase):
    def test_argument_lists_after_generics(self):
        for rest in ("(x)", " (x)", "::<u8>(x)", "<T>(x)", "<Map<K, V>>()"):
            self.assertTrue(ground_truth._applied(rest), rest)
        for rest in ("", ".x()", "!(x)", " = f(x)", "[0](x)"):
            self.assertFalse(ground_truth._applied(rest), rest)

    def test_a_tagged_template_calls_its_tag_in_javascript(self):
        self.assertTrue(ground_truth._applied("`<p>${x}</p>`", template=True))
        self.assertFalse(ground_truth._applied("`raw`"))

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
        report, samples = ground_truth.score_imports(result, truth, self.root)
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
        report, samples = ground_truth.score_imports(result, wrong, self.root)
        self.assertEqual(report["contradicted"], 1)
        self.assertEqual(samples["missed"], [["pkg/a.py", 1]])


class ScriptImportScoreTests(unittest.TestCase):
    def test_script_imports_are_scored_as_the_compiler_resolves_them(self):
        from deepthought.ts_modules import TypeScriptUnavailable, locate_typescript

        try:
            locate_typescript()
        except TypeScriptUnavailable:
            self.skipTest("Install node and the pinned scip-typescript")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.js").write_text('import { f } from "./lib";\nf();\n')
            (root / "lib.d.ts").write_text("export declare function f(): void;\n")
            (root / "lib.js").write_text("export function f() {}\n")
            result = ground_truth.analyze(root, ["javascript", "typescript"])
            indexed = {"a.js", "lib.d.ts", "lib.js"}
            # The compiler takes the declaration file; the .js beside it is
            # what a JavaScript resolver would take.
            for target, confirmed in (("lib.d.ts", 1), ("lib.js", 0)):
                truth = ({("a.js", 1, "import"): {(target, 0, "module")}}, indexed)
                report, _ = ground_truth.score_imports(result, truth, root)
                self.assertEqual(
                    (report["confirmed"], report["contradicted"]),
                    (confirmed, 1 - confirmed),
                )


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

    def test_missing_indexer_of_a_scip_table_corpus_is_reported(self):
        from unittest import mock

        manifest = {
            "corpora": [],
            "tables": [
                {"name": "orm", "language": "python-orm"},
                {"name": "rails", "language": "ruby"},
            ],
        }
        with (
            mock.patch("importlib.util.find_spec", return_value=object()),
            mock.patch("deepthought.indexers.locate", return_value=None),
        ):
            missing = ground_truth.missing_requirements(manifest)
            self.assertEqual(len(missing), 1)
            self.assertTrue(missing[0].startswith("scip-python "), missing)
            self.assertEqual(ground_truth.missing_requirements(manifest, {"rails"}), [])

    def test_only_scip_table_corpora_are_indexed(self):
        # No indexer runs for a corpus whose rules read no SCIP index, or
        # without a cache to build the indexes in.
        self.assertEqual(
            ground_truth.table_indexes({"language": "ruby"}, "/w", "/cache"), ()
        )
        self.assertEqual(
            ground_truth.table_indexes({"language": "python-orm"}, "/w", None), ()
        )

    def test_missing_indexer_of_a_calls_corpus_is_reported_with_scip(self):
        from unittest import mock

        manifest = {
            "corpora": [
                {"name": "go", "languages": ["go"]},
                {"name": "rust", "languages": ["rust"]},
            ]
        }
        with (
            mock.patch("importlib.util.find_spec", return_value=object()),
            mock.patch("deepthought.indexers.locate", return_value=None),
        ):
            # Without --scip the parser alone is scored: no indexer needed.
            self.assertEqual(ground_truth.missing_requirements(manifest), [])
            missing = ground_truth.missing_requirements(manifest, scip=True)
            self.assertEqual(len(missing), 1)
            self.assertTrue(missing[0].startswith("scip-go "), missing)
            # A language the engine indexes with no SCIP indexer needs none.
            self.assertEqual(
                ground_truth.missing_requirements(manifest, {"rust"}, scip=True), []
            )

    def test_check_refuses_to_score_without_them(self):
        from unittest import mock

        with (
            mock.patch.object(
                ground_truth, "missing_requirements", return_value=["yaml"]
            ),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(ground_truth.main(["check", "--corpus", "click"]), 2)
        self.assertIn("not installed: yaml", stderr.getvalue())


class ScipRegressionTests(unittest.TestCase):
    def test_scip_calls_and_their_impact_are_compared(self):
        base = {
            "expected": 3,
            "confirmed": 3,
            "contradicted": 0,
            "impact": {"seeds": 1, "expected": 2, "confirmed": 2, "extra": 0},
        }
        worse = {
            "expected": 3,
            "confirmed": 2,
            "contradicted": 1,
            "impact": {"seeds": 1, "expected": 2, "confirmed": 1, "extra": 0},
        }
        self.assertEqual(ground_truth.scip_regressions(base, base), [])
        self.assertEqual(
            ground_truth.scip_regressions(worse, base),
            [
                "contradicted 0 -> 1",
                "confirmed 3 -> 2",
                "impact: confirmed 2 -> 1",
            ],
        )


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


class UnknownCorpusTests(unittest.TestCase):
    def test_a_misspelled_corpus_name_fails_instead_of_checking_nothing(self):
        with tempfile.TemporaryDirectory() as cache:
            with self.assertRaisesRegex(ValueError, "no such corpus: clik"):
                ground_truth.check(["clik"], Path(cache))
        with redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(ground_truth.main(["check", "--corpus", "clik"]), 2)
        self.assertIn("no such corpus: clik", stderr.getvalue())


@unittest.skipUnless(
    importlib.util.find_spec("tree_sitter_typescript"), "the TypeScript grammar"
)
class RouteTruthTests(unittest.TestCase):
    FILES = {
        "package.json": '{"name": "app"}',
        "src/main.ts": (
            "import { NestFactory } from '@nestjs/core';\n"
            "async function boot() {\n"
            "  const app = await NestFactory.create(AppModule);\n"
            "  app.setGlobalPrefix('api');\n"
            "}\n"
        ),
        "src/cats.controller.ts": (
            "import { Controller, Get, Post } from '@nestjs/common';\n"
            "@Controller('cats')\n"
            "export class CatsController {\n"
            "  @Get(':id')\n  one() {}\n"
            "  @Post()\n  add() {}\n"
            "  @Get('extra')\n  extra() {}\n"
            "}\n"
        ),
        "src/schema.graphql": "type Query {\n  cat: String\n  dog: String\n}\n",
        "src/cats.resolver.ts": (
            "import { Query, Resolver } from '@nestjs/graphql';\n"
            "@Resolver()\n"
            "export class CatsResolver {\n"
            "  @Query(() => String)\n  cat() {}\n"
            "}\n"
        ),
    }

    def test_routes_and_handled_fields_against_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            for name, text in self.FILES.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text, "utf-8")
            labels = Path(tmp) / "labels.json"
            labels.write_text(
                json.dumps(
                    {
                        "http": [["GET", "/api/cats/{var}"], ["DELETE", "/api/cats"]],
                        "neutral_http": [["GET", "/api/cats/extra"]],
                        "graphql": ["Query.cat", "Query.dog"],
                    }
                ),
                "utf-8",
            )
            report, samples = ground_truth.score_routes({"labels": labels}, root)
        # POST /api/cats is read but not labelled; DELETE is labelled, not read.
        self.assertEqual(report["http"], {"expected": 2, "found": 1, "extra": 1})
        self.assertEqual(samples["http"]["extra"], [("POST", "/api/cats")])
        # Query.dog is declared but has no resolver, so it is not found.
        self.assertEqual(report["graphql"], {"expected": 2, "found": 1, "extra": 0})
        baseline = {"http": {"expected": 2, "found": 2, "extra": 0}}
        self.assertEqual(
            ground_truth.route_regressions(report, baseline),
            ["http found 2 -> 1", "http extra 0 -> 1"],
        )


@unittest.skipUnless(importlib.util.find_spec("tree_sitter_ruby"), "the Ruby grammar")
class TableTruthTests(unittest.TestCase):
    FILES = {
        "app/models/application_record.rb": (
            "class ApplicationRecord < ActiveRecord::Base\n"
            "  self.abstract_class = true\nend\n"
        ),
        "app/models/user.rb": "class User < ApplicationRecord\nend\n",
        "app/models/post.rb": (
            "class Post < ApplicationRecord\n  self.table_name = 'articles'\nend\n"
        ),
        "app/models/tag.rb": "class Tag < ApplicationRecord\nend\n",
    }

    def test_models_against_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            for name, text in self.FILES.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text, "utf-8")
            labels = Path(tmp) / "labels.json"
            labels.write_text(
                json.dumps(
                    {
                        "models": [
                            {"class": "User", "table": "users"},
                            {"class": "Post", "table": "posts"},
                            {"class": "Comment", "table": "comments"},
                        ],
                        "not_counted": [{"class": "Tag", "reason": "test_only"}],
                    }
                ),
                "utf-8",
            )
            report, samples = ground_truth.score_tables({"labels": labels}, root)
        # Post maps to a table the labels do not give; Comment is not read;
        # Tag is set aside, so it is not extra.
        self.assertEqual(
            report, {"expected": 3, "found": 1, "wrong": 1, "missed": 1, "extra": 0}
        )
        self.assertEqual(samples["wrong"], [["Post", "posts", ["articles"]]])
        baseline = {"expected": 3, "found": 2, "wrong": 0, "missed": 1, "extra": 0}
        self.assertEqual(
            ground_truth.table_regressions(report, baseline),
            ["found 2 -> 1", "wrong 0 -> 1"],
        )


DOCTRINE = {
    "src/Entity/Team.php": (
        "<?php\nnamespace App\\Entity;\nuse Doctrine\\ORM\\Mapping as ORM;\n"
        "#[ORM\\Entity]\n#[ORM\\Table(name: '`teams`')]\nclass Team {\n"
        "    #[ORM\\ManyToMany(targetEntity: Team::class)]\n"
        "    #[ORM\\JoinTable(name: 'team_links')]\n    private $links;\n}\n"
    ),
    "src/Entity/BlogPost2.php": (
        "<?php\nnamespace App\\Entity;\nuse Doctrine\\ORM\\Mapping as ORM;\n"
        "#[ORM\\Entity]\nclass BlogPost2 {}\n"
    ),
    "src/Entity/Node.php": (
        "<?php\nnamespace App\\Entity;\nuse Doctrine\\ORM\\Mapping as ORM;\n"
        "#[ORM\\Entity]\n#[ORM\\InheritanceType('SINGLE_TABLE')]\n"
        "abstract class Node {}\n"
    ),
    "src/Entity/Leaf.php": (
        "<?php\nnamespace App\\Entity;\nuse Doctrine\\ORM\\Mapping as ORM;\n"
        "#[ORM\\Entity]\nclass Leaf extends Node {}\n"
    ),
    "migrations/Version1.php": (
        "<?php\nfinal class Version1 {\n"
        "  public function up($schema): void {\n"
        "    $this->addSql('CREATE TABLE teams (id INT)');\n"
        "    $this->addSql('CREATE TABLE team_links (a INT)');\n"
        "    $this->addSql('CREATE TABLE blog_post2 (id INT)');\n  }\n"
        "  public function down($schema): void {\n"
        "    $this->addSql('DROP TABLE teams');\n  }\n}\n"
    ),
    "migrations/Version2.php": (
        "<?php\nfinal class Version2 {\n  public function mySQLUp($schema): void {\n"
        "    $this->addSql('RENAME TABLE blog_post2 TO blog_post_2');\n  }\n}\n"
    ),
}


class DoctrineTableTests(unittest.TestCase):
    def oracle(self, root, naming):
        out = subprocess.run(
            ["php", str(ROOT / "tools/doctrine_table_oracle.php"), str(root), naming],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(out.stdout), out.stderr

    @unittest.skipUnless(shutil.which("php"), "php is required")
    def test_oracle_reads_entities_and_checks_them_against_migrations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, text in DOCTRINE.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text, "utf-8")
            labels, stderr = self.oracle(root, "underscore_number_aware")
            # The plain underscore strategy keeps the digit joined, which the
            # migrations do not: the label is reported as not migrated.
            _, plain = self.oracle(root, "underscore")
        self.assertEqual(
            [(m["class"], m["table"], m["join_tables"]) for m in labels["models"]],
            [
                ("App\\Entity\\BlogPost2", "blog_post_2", []),
                ("App\\Entity\\Team", "teams", ["team_links"]),
            ],
        )
        self.assertEqual(
            labels["not_counted"],
            [
                {"class": "App\\Entity\\Leaf", "reason": "inheritance"},
                {"class": "App\\Entity\\Node", "reason": "inheritance"},
            ],
        )
        self.assertEqual(
            labels["migrated_tables"], ["blog_post_2", "team_links", "teams"]
        )
        self.assertEqual(stderr, "")
        self.assertIn("not migrated\tApp\\Entity\\BlogPost2\tblog_post2", plain)

    @unittest.skipUnless(
        importlib.util.find_spec("tree_sitter_php"), "PHP grammar is required"
    )
    def test_join_tables_do_not_make_an_entity_wrong(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            for name, text in DOCTRINE.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text, "utf-8")
            labels = Path(tmp) / "labels.json"
            labels.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "class": "App\\Entity\\Team",
                                "table": "teams",
                                "join_tables": ["team_links"],
                            }
                        ],
                        "not_counted": [
                            {"class": "App\\Entity\\BlogPost2", "reason": "x"},
                            {"class": "App\\Entity\\Leaf", "reason": "inheritance"},
                            {"class": "App\\Entity\\Node", "reason": "inheritance"},
                        ],
                    }
                ),
                "utf-8",
            )
            report, samples = ground_truth.score_tables(
                {"labels": labels, "language": "php"}, root
            )
        self.assertEqual(
            report, {"expected": 1, "found": 1, "wrong": 0, "missed": 0, "extra": 0}
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


class JavaScriptTruthTests(unittest.TestCase):
    def test_tsconfig_from_jsconfig_with_aliases(self):
        import javascript_tsconfig

        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "jsconfig.json").write_text(
                '{"compilerOptions": {"checkJs": true, "jsx": "react",'
                ' "types": ["vitest/globals"]}}'
            )
            config = javascript_tsconfig.tsconfig(
                directory, [("pkg", "src/index.js"), ("pkg/x", "x/src/index.js")]
            )
        options = config["compilerOptions"]
        self.assertEqual(
            (options["allowJs"], options["checkJs"], options["jsx"]),
            (True, False, "react"),
        )
        self.assertNotIn("types", options)
        self.assertEqual(
            options["paths"], {"pkg": ["src/index.js"], "pkg/x": ["x/src/index.js"]}
        )

    @unittest.skipUnless(
        READY and shutil.which("scip-typescript"), "scip-typescript is required"
    )
    def test_a_local_class_construction_binds_the_class_or_its_constructor(self):
        import subprocess

        import javascript_tsconfig

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.js").write_text(
                "export function f() {\n"
                "  class Local {\n    constructor(x) { this.x = x; }\n  }\n"
                "  return new Local(1);\n}\n"
            )
            javascript_tsconfig.main([str(root)])
            subprocess.run(
                ["scip-typescript", "index"], cwd=root, check=True, capture_output=True
            )
            expected, _ = ground_truth.derive(root, root / "index.scip")
        # scip-typescript names the constructor of a local class without
        # defining it; the construction binds the class or the constructor
        # the class body declares.
        self.assertEqual(
            expected[("a.js", 5, "Local")], {("a.js", 2, "local"), ("a.js", 3, "local")}
        )


class GrpcScorerTests(unittest.TestCase):
    def test_a_service_root_may_be_one_file(self):
        import check_contracts_ground_truth as contracts

        roots = [("client", "pkg/Client.java"), ("server", "pkg/Server.java")]
        self.assertEqual(contracts._service_of("pkg/Client.java", roots), "client")
        self.assertEqual(contracts._service_of("pkg/Server.java", roots), "server")
        self.assertIsNone(contracts._service_of("pkg/Client.javax", roots))

    def test_streaming_kinds_against_the_labels(self):
        from types import SimpleNamespace

        import check_contracts_ground_truth as contracts

        def method(kind):
            return SimpleNamespace(streaming=kind)

        tool = SimpleNamespace(
            last_grpc=SimpleNamespace(
                index=SimpleNamespace(
                    methods={
                        ("p.S", "A"): [method("server")],
                        ("p.S", "B"): [method("unary")],
                        ("p.S", "C"): [method(None)],
                    }
                )
            )
        )
        labelled = [
            {"contract": "p.S/A", "streaming": "server"},
            {"contract": "p.S/A", "streaming": "server"},
            {"contract": "p.S/B", "streaming": "bidi"},
            {"contract": "p.S/C", "streaming": "client"},
            {"contract": "p.S/D", "streaming": "unary", "in_scope_static": False},
        ]
        self.assertEqual(
            contracts._grpc_streaming(tool, labelled),
            {
                "labelled": 3,
                "true_positive": 1,
                "wrong": [{"contract": "p.S/B", "label": "bidi", "read": ["unary"]}],
                "unread": ["p.S/C"],
            },
        )


@unittest.skipUnless(
    importlib.util.find_spec("tree_sitter_rust"), "Rust grammar is required"
)
class RustPatternTest(unittest.TestCase):
    def test_a_name_opening_a_pattern_is_found_and_a_call_is_not(self):
        source = b"""fn f(r: Result<u8, E>) -> Option<u8> {
    match r {
        Ok(v) => Some(v),
        Err(E::Io(e)) => None,
    }
}
fn g(p: P) { let P { x } = p; }
"""
        self.assertEqual(
            ground_truth._rust_pattern_paths(source),
            {(3, 8), (4, 8), (4, 15), (7, 17)},
        )


class MethodShorthandTest(unittest.TestCase):
    def test_a_method_definition_is_not_a_call(self):
        shorthand = ground_truth._method_shorthand
        for before, rest in (
            ("  ", "(ws, code) {"),
            ("    async ", "() {"),
            ("  get ", "() {"),
            ("  ", "<T>(x: T): Promise<T> {"),
            ("  ", "(data: string | Uint8Array<ArrayBuffer>, _options) {"),
        ):
            self.assertTrue(shorthand(before, rest), (before, rest))
        for before, rest in (
            ("  ws.", "(code) {"),
            ("  ", "(code)"),
            ("  ", "(() => {"),
            ("  return ", "(x) {"),
            ("  ", "(x): void => {"),
        ):
            self.assertFalse(shorthand(before, rest), (before, rest))
