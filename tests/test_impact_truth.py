"""Impact ground truth: oracle callers of the declarations commits edited."""

import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import impact_truth  # noqa: E402

READY = (
    shutil.which("git")
    and importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_python")
)

A = (
    "def base(x):\n    return x\n\n\n"
    "def mid(x):\n    return base(x)\n\n\n"
    "def top(x):\n    return mid(x)\n"
)
B = "from pkg.a import top\n\n\ndef user():\n    return top(1)\n"
# Lines 1-based; ranges in the index are 0-based [line, start, end] and
# enclosing [start_line, start_col, end_line, end_col].
DEFINITIONS = {
    "pkg/a.py": [("base", 0, 1), ("mid", 4, 5), ("top", 8, 9)],
    "pkg/b.py": [("user", 3, 4)],
}
CALLS = (
    "# file\tline\tname\ttarget_file\ttarget_line\tkind\n"
    "# indexed\tpkg/a.py\n# indexed\tpkg/b.py\n"
    "pkg/a.py\t6\tbase\tpkg/a.py\t1\tcallable\n"
    "pkg/a.py\t10\tmid\tpkg/a.py\t5\tcallable\n"
    "pkg/b.py\t5\ttop\tpkg/a.py\t9\tcallable\n"
)


def index_bytes():
    from deepthought._scip import scip_pb2

    index = scip_pb2.Index()
    for path, definitions in DEFINITIONS.items():
        document = index.documents.add(relative_path=path)
        module = path[:-3].replace("/", ".")
        for name, first, last in definitions:
            document.occurrences.add(
                range=[first, 4, 4 + len(name)],
                symbol=f"scip-python python t 0 `{module}`/{name}().",
                symbol_roles=1,
                enclosing_range=[first, 0, last, 16],
            )
    return index.SerializeToString()


class BlockTests(unittest.TestCase):
    def test_python_block_spans_a_multiline_signature(self):
        lines = [
            "def f(",
            "    a,",
            ") -> int:",
            "    x = 1",
            "",
            "    # note",
            "    return x",
            "def g():",
        ]
        self.assertEqual(impact_truth._python_block(lines, 1), 7)


@unittest.skipUnless(READY, "git and the Python grammar are required")
class ImpactTruthTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }

        def git(*args):
            subprocess.run(
                ["git", "-C", str(self.repo), *args],
                check=True,
                capture_output=True,
                env=env,
            )

        git("init", "-q")
        # The second commit edits base's body and user's body.
        for a, b, message in (
            (A, B, "first"),
            (A.replace("return x\n", "return x + 0\n", 1), B.replace("1", "2"), "edit"),
        ):
            (self.repo / "pkg").mkdir(exist_ok=True)
            (self.repo / "pkg/a.py").write_text(a, "utf-8")
            (self.repo / "pkg/b.py").write_text(b, "utf-8")
            git("add", "-A")
            git("commit", "-q", "-m", message)
        self.index = self.repo.parent / f"{self.repo.name}.scip"
        self.index.write_bytes(index_bytes())
        self.addCleanup(self.index.unlink)
        self.calls = self.repo.parent / f"{self.repo.name}.tsv"
        self.calls.write_text(CALLS, "utf-8")
        self.addCleanup(self.calls.unlink)

    def derive(self):
        rows, aliases, _, _ = impact_truth.derive(
            self.index, self.calls, self.repo, "python", "HEAD", commits=5
        )
        stream = io.StringIO()
        impact_truth.write(rows, stream, aliases)
        path = self.repo.parent / f"{self.repo.name}.impact.tsv"
        path.write_text(stream.getvalue(), "utf-8")
        self.addCleanup(path.unlink)
        return impact_truth.read(path)

    def test_seeds_are_edited_callables_and_dependents_their_callers(self):
        truth = self.derive()
        self.assertEqual(
            truth,
            {
                ("pkg/a.py", 1, "base"): {
                    ("pkg/a.py", 1, "base"): 0,
                    ("pkg/a.py", 5, "mid"): 1,
                    ("pkg/a.py", 9, "top"): 2,
                    ("pkg/b.py", 4, "user"): 3,
                },
                ("pkg/b.py", 4, "user"): {("pkg/b.py", 4, "user"): 0},
            },
        )

    def test_engine_reaches_every_oracle_caller(self):
        import deepthought

        result = deepthought.analyze_repository(
            self.repo, repository_id="t", languages=("python",)
        )
        report, _ = impact_truth.score(result, self.derive())
        self.assertEqual(
            (report["seeds"], report["expected"], report["confirmed"]), (2, 3, 3)
        )
        self.assertEqual((report["missed"], report["extra"]), (0, 0))


@unittest.skipUnless(READY, "git and the Python grammar are required")
class DecoratorTests(unittest.TestCase):
    def test_a_decorator_call_belongs_to_the_enclosing_function(self):
        # def outer at 1-6; its nested def inner at 4 is decorated at 2-3,
        # an index range that starts at the first decorator.
        nodes = {
            ("t.py", 1): ("outer", 1, 6),
            ("t.py", 4): ("inner", 2, 6),
            ("lib.py", 1): ("command", 1, 2),
            ("lib.py", 3): ("helper", 3, 4),
        }
        calls = {
            ("t.py", 2, "command"): {("lib.py", 1, "callable")},
            ("t.py", 5, "helper"): {("lib.py", 3, "callable")},
        }
        incoming = impact_truth.call_graph(nodes, calls)
        self.assertEqual(incoming[("lib.py", 1)], {("t.py", 1)})
        self.assertEqual(incoming[("lib.py", 3)], {("t.py", 4)})


class NameLineTests(unittest.TestCase):
    def test_a_one_line_body_belongs_to_its_function_outside_python(self):
        # Go: func (ns nodes) Sort() { ns.tailSort() } on line 1.
        nodes = {("t.go", 1): ("Sort", 1, 1), ("t.go", 3): ("tailSort", 3, 5)}
        calls = {("t.go", 1, "tailSort"): {("t.go", 3, "callable")}}
        self.assertEqual(
            impact_truth.call_graph(nodes, calls, name_line_inside=True),
            {("t.go", 3): {("t.go", 1)}},
        )
        self.assertEqual(impact_truth.call_graph(nodes, calls), {})


class InitializerTests(unittest.TestCase):
    def test_a_field_initializer_call_belongs_to_its_class(self):
        # class T at 1-9: a field initializer at 2 and one at 3-5 holding an
        # anonymous class whose method run() spans 4-4; a method m at 7-8.
        nodes = {
            ("T.java", 4): ("run", 4, 4),
            ("T.java", 7): ("m", 7, 8),
            ("L.java", 1): ("get", 1, 2),
        }
        regions = [("T.java", 1, 2, 2), ("T.java", 1, 3, 5)]
        calls = {
            ("T.java", 2, "get"): {("L.java", 1, "callable")},
            ("T.java", 4, "get"): {("L.java", 1, "callable")},
            ("T.java", 8, "get"): {("L.java", 1, "callable")},
        }
        incoming = impact_truth.call_graph(nodes, calls, None, True, regions)
        self.assertEqual(
            incoming[("L.java", 1)], {("T.java", 1), ("T.java", 4), ("T.java", 7)}
        )
        self.assertEqual(
            impact_truth.call_graph(nodes, calls, None, True)[("L.java", 1)],
            {("T.java", 4), ("T.java", 7)},
        )


class SameNameTests(unittest.TestCase):
    def test_a_function_is_not_folded_onto_a_method_of_the_same_name(self):
        import deepthought

        repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo)
        (repo / "m.py").write_text(
            "def base():\n    return 1\n\n\n"
            "class C:\n    def run(self):\n        return base()\n\n\n"
            "def run():\n    return 2\n\n\n"
            "def caller():\n    return run()\n",
            "utf-8",
        )
        result = deepthought.analyze_repository(
            repo, repository_id="t", languages=("python",)
        )
        # The truth names only base and its caller C.run; the function `run`
        # is in no row, so it must keep its own node, not take C.run's.
        truth = {("m.py", 1, "base"): {("m.py", 1, "base"): 0, ("m.py", 6, "run"): 1}}
        report, samples = impact_truth.score(result, truth)
        self.assertEqual((report["confirmed"], report["extra"]), (1, 0), samples)


class UnjudgedTests(unittest.TestCase):
    def test_a_dependent_listed_unjudged_is_not_extra(self):
        import deepthought

        repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo)
        (repo / "m.py").write_text(
            "def base():\n    return 1\n\n\n"
            "@staticmethod\ndef user():\n    return base()\n",
            "utf-8",
        )
        result = deepthought.analyze_repository(
            repo, repository_id="t", languages=("python",)
        )
        truth = {("m.py", 1, "base"): {("m.py", 1, "base"): 0}}
        plain, _ = impact_truth.score(result, truth)
        self.assertEqual((plain["extra"], plain["unjudged"]), (1, 0))
        # The row names the oracle's line for user (its name, line 6); the
        # engine's declaration may start at the decorator.
        stream = io.StringIO()
        impact_truth.write([], stream, unjudged=[("m.py", 1, "m.py", 6)])
        path = repo / "impact.tsv"
        path.write_text(stream.getvalue(), "utf-8")
        unjudged = impact_truth.read_unjudged(path)
        self.assertEqual(unjudged, {("m.py", 1, "m.py", 6)})
        report, _ = impact_truth.score(result, truth, unjudged=unjudged)
        self.assertEqual((report["extra"], report["unjudged"]), (0, 1))


@unittest.skipUnless(
    importlib.util.find_spec("tree_sitter_typescript"), "TypeScript grammar"
)
class NestedFunctionTests(unittest.TestCase):
    def test_a_nested_function_is_read_as_the_function_around_it(self):
        import deepthought

        repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo)
        (repo / "a.ts").write_text(
            "export function base() {\n  return 1\n}\n\n"
            "export function outer() {\n"
            "  const inner = () => base()\n  return inner()\n}\n",
            "utf-8",
        )
        result = deepthought.analyze_repository(
            repo, repository_id="t", languages=("typescript",)
        )
        # scip-typescript makes `inner` a local symbol: the oracle's caller
        # of base is outer, which the engine reaches through inner.
        truth = {("a.ts", 1, "base"): {("a.ts", 1, "base"): 0, ("a.ts", 5, "outer"): 1}}
        plain, _ = impact_truth.score(result, truth)
        folded, _ = impact_truth.score(result, truth, fold_nested=True)
        self.assertEqual((plain["confirmed"], plain["extra"]), (1, 1))
        self.assertEqual((folded["confirmed"], folded["extra"]), (1, 0))


class FunctionValueTests(unittest.TestCase):
    def test_initializers_that_hold_a_function(self):
        def holds(text):
            return impact_truth.holds_function(text)

        self.assertTrue(holds(" = <T extends Map<string, number>>(x: T) => x"))
        self.assertTrue(holds(" =\n  async (c) => {"))
        self.assertTrue(holds(": FC<\n  Props\n> = (props) => null"))
        self.assertFalse(holds(" = new Map<string, number>()"))
        # Type parameters with a function-typed bound and a default.
        self.assertTrue(
            holds(
                " = <\n  VF extends (v: unknown) => any = (v: unknown) => any,\n>(\n  t"
            )
        )
        self.assertTrue(
            holds(": FC<\n  P<{ r?: (e: E) => void }>\n> = (props) => null")
        )
        self.assertFalse(holds(": Map<string, () => void> = new Map()"))
        # A class property holding a function is named `C#m.`.
        self.assertEqual(
            impact_truth._TERM.search("src/`c.ts`/C#fetch.").group(1), "fetch"
        )


if __name__ == "__main__":
    unittest.main()

    def test_a_dependent_in_a_file_the_oracle_did_not_index_is_unjudged(self):
        import deepthought

        repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo)
        (repo / "m.py").write_text("def base():\n    return 1\n", "utf-8")
        (repo / "tagged.py").write_text(
            "from m import base\n\n\ndef user():\n    return base()\n", "utf-8"
        )
        result = deepthought.analyze_repository(
            repo, repository_id="t", languages=("python",)
        )
        truth = {("m.py", 1, "base"): {("m.py", 1, "base"): 0}}
        plain, _ = impact_truth.score(result, truth, indexed={"m.py", "tagged.py"})
        self.assertEqual((plain["extra"], plain["unjudged"]), (1, 0))
        report, _ = impact_truth.score(result, truth, indexed={"m.py"})
        self.assertEqual((report["extra"], report["unjudged"]), (0, 1))


@unittest.skipUnless(shutil.which("go"), "needs the go toolchain")
class InterfaceDispatchTests(unittest.TestCase):
    def test_interface_methods_map_to_their_implementations(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        files = {
            "go.mod": "module example.com/m\n\ngo 1.21\n",
            "r.go": (
                "package m\n"
                "\n"
                "type Router interface {\n"
                "\tMount(p string)\n"
                "}\n"
                "\n"
                "type Mux struct{}\n"
                "\n"
                "func (m *Mux) Mount(p string) {}\n"
                "\n"
                'func use(r Router) { r.Mount("/") }\n'
            ),
            # Another package of the module implements it too.
            "sub/s.go": (
                "package sub\n\ntype Sub struct{}\n\nfunc (Sub) Mount(p string) {}\n"
            ),
            # A file another platform builds is not read.
            "r_other.go": "//go:build plan9\n\npackage m\n\nfunc (m *Mux) Mount() {}\n",
            # Directories the go tool ignores are skipped.
            "_examples/e.go": "package e\n\ntype E struct{}\n\nfunc (E) Mount(p string) {}\n",
        }
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text, "utf-8")
        completed = subprocess.run(
            ["go", "run", str(ROOT / "tools/go_interface_dispatch.go"), str(root)],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "GOFLAGS": "-mod=mod", "GOWORK": "off"},
        )
        self.assertEqual(
            completed.stdout.splitlines(),
            ["r.go\t4\tr.go\t9", "r.go\t4\tsub/s.go\t5"],
        )
        path = root / "dispatch.tsv"
        path.write_text(completed.stdout, "utf-8")
        self.assertEqual(
            impact_truth.read_dispatch(path),
            {("r.go", 4): {("r.go", 9), ("sub/s.go", 5)}},
        )
