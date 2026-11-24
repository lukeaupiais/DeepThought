import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

from deepthought import indexers
from deepthought._scip import scip_pb2

SRC = Path(__file__).resolve().parents[1] / "src"

# Stands in for a pinned indexer: writes a SCIP index naming FAKE_NAME and
# FAKE_VERSION with one document per path in FAKE_DOCUMENTS.
FAKE = textwrap.dedent(
    """\
    #!{python}
    import os, sys
    sys.path.insert(0, {src!r})
    from deepthought._scip import scip_pb2
    argv = sys.argv[1:]
    output = argv[argv.index("--output") + 1]
    if "--infer-tsconfig" in argv:
        # As scip-typescript does: the inferred config lands in the workspace.
        with open("tsconfig.json", "w") as config:
            config.write("{{}}")
    index = scip_pb2.Index()
    index.metadata.tool_info.name = os.environ["FAKE_NAME"]
    index.metadata.tool_info.version = os.environ["FAKE_VERSION"]
    for path in reversed(os.environ["FAKE_DOCUMENTS"].split(",")):
        document = index.documents.add()
        document.relative_path = path
        for line in (0,):
            occurrence = document.occurrences.add()
            occurrence.range.extend([line, 0, 1])
            occurrence.symbol = "local " + str(line)
    with open(os.environ.get("FAKE_ARGS", os.devnull), "w") as log:
        log.write(" ".join(argv) + "\\n" + os.environ.get("PYTHONPATH", ""))
    with open(output, "wb") as stream:
        stream.write(index.SerializeToString())
    sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
    """
)


class IndexersTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name) / "workspace"
        self.root.mkdir()
        (self.root / "src" / "pkg").mkdir(parents=True)
        (self.root / "src" / "pkg" / "a.py").write_text("def f():\n    pass\n")
        (self.root / "src" / "pkg" / "b.py").write_text("def g():\n    pass\n")
        (self.root / "pyproject.toml").write_text("[project]\nname = 'pkg'\n")
        venv = self.root / "env"
        venv.mkdir()
        (venv / "pyvenv.cfg").write_text("home = /usr\n")
        (venv / "site.py").write_text("x = 1\n")
        self.bin = Path(scratch.name) / "bin"
        self.bin.mkdir()
        self.out = Path(scratch.name) / "out"
        self.args_log = Path(scratch.name) / "args.txt"

    def fake(self, name):
        path = self.bin / name
        path.write_text(FAKE.format(python=sys.executable, src=str(SRC)))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def environ(self, **values):
        return {
            "PATH": str(self.bin),
            "FAKE_NAME": "scip-python",
            "FAKE_VERSION": "0.6.8",
            "FAKE_DOCUMENTS": "src/pkg/a.py",
            "FAKE_ARGS": str(self.args_log),
            **values,
        }

    def test_missing_indexer_leaves_every_file_uncovered_with_a_reason(self):
        report = indexers.index_workspace(
            self.root, self.out, languages=("python",), environ={"PATH": ""}
        )
        self.assertEqual(report["uncovered"], 2)
        self.assertEqual(report["runs"][0]["reason"], "indexer_not_installed")
        self.assertEqual(
            {c["reason"] for c in report["coverage"].values()},
            {"indexer_not_installed"},
        )
        # Files inside a virtual environment are not workspace sources.
        self.assertNotIn("env/site.py", report["coverage"])

    def test_indexed_files_are_covered_and_others_say_why_not(self):
        self.fake("scip-python")
        report = indexers.index_workspace(
            self.root, self.out, languages=("python",), environ=self.environ()
        )
        run = report["runs"][0]
        self.assertEqual(run["status"], "indexed")
        self.assertEqual(run["position_encoding"], "utf-16")
        self.assertEqual(report["coverage"]["src/pkg/a.py"]["status"], "covered")
        self.assertEqual(report["coverage"]["src/pkg/b.py"]["reason"], "not_in_index")
        # Source roots go on PYTHONPATH so src-layout imports resolve.
        argv, python_path = self.args_log.read_text().split("\n")
        self.assertIn("--project-version 0", argv)
        self.assertEqual(
            python_path.split(os.pathsep),
            [str(self.root.resolve()), str((self.root / "src").resolve())],
        )
        receipt = indexers.validate_receipt(self.root, run["index"])
        self.assertEqual(receipt["profile"], "scip-python-v2")
        self.assertEqual(receipt["producer"]["python_path"], [".", "src"])
        self.assertNotIn(str(self.root), json.dumps(receipt["producer"]))
        (self.root / "src" / "pkg" / "b.py").write_text("def g():\n    return 1\n")
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            indexers.validate_receipt(self.root, run["index"])

    def test_wrong_version_is_a_failure_not_an_index(self):
        self.fake("scip-python")
        report = indexers.index_workspace(
            self.root,
            self.out,
            languages=("python",),
            environ=self.environ(FAKE_VERSION="0.6.7"),
        )
        self.assertEqual(report["runs"][0]["reason"], "indexer_version_mismatch")
        self.assertEqual(report["uncovered"], 2)
        self.assertFalse((self.out / "scip-python.scip").exists())

    def test_failing_indexer_is_reported(self):
        self.fake("scip-python")
        report = indexers.index_workspace(
            self.root,
            self.out,
            languages=("python",),
            environ=self.environ(FAKE_EXIT="2"),
        )
        self.assertEqual(report["runs"][0]["reason"], "indexer_failed")

    def test_go_without_a_module_is_unavailable(self):
        (self.root / "main.go").write_text("package main\n")
        self.fake("scip-go")
        report = indexers.index_workspace(
            self.root, self.out, languages=("go",), environ=self.environ()
        )
        self.assertEqual(report["coverage"]["main.go"]["reason"], "no_go_module")

    def test_an_inferred_tsconfig_does_not_stay_in_the_workspace(self):
        (self.root / "web.ts").write_text("export const a = 1;\n")
        self.fake("scip-typescript")
        report = indexers.index_workspace(
            self.root,
            self.out,
            languages=("typescript",),
            environ=self.environ(
                FAKE_NAME="scip-typescript",
                FAKE_VERSION="0.6.2",
                FAKE_DOCUMENTS="web.ts",
            ),
        )
        self.assertEqual(report["runs"][0]["status"], "indexed")
        self.assertEqual(report["coverage"]["web.ts"]["status"], "covered")
        self.assertFalse((self.root / "tsconfig.json").exists())

    def test_canonical_bytes_do_not_depend_on_emission_order(self):
        def build(order):
            index = scip_pb2.Index()
            for path in order:
                document = index.documents.add()
                document.relative_path = path
                for line in (2, 1) if order[0] == "a.go" else (1, 2):
                    document.occurrences.add(range=[line, 0, 1], symbol="s")
                info = document.symbols.add(symbol="t")
                for related in order:
                    info.relationships.add(symbol=related, is_implementation=True)
            return indexers.canonical_bytes(index)

        self.assertEqual(build(["a.go", "b.go"]), build(["b.go", "a.go"]))

    def test_scip_tool_composes_indexes_of_several_languages(self):
        import asyncio

        from deepthought.analysis_tools.scip import ScipAnalysisTool

        (self.root / "go.mod").write_text("module example.com/m\n")
        (self.root / "main.go").write_text("package main\n\nfunc main() {}\n")
        self.fake("scip-python")
        self.fake("scip-go")
        self.out.mkdir()
        python = indexers.run_indexer(
            indexers.INDEXERS["scip-python"],
            self.root,
            self.out / "py.scip",
            environ=self.environ(FAKE_DOCUMENTS="src/pkg/a.py,src/pkg/b.py"),
        )
        go = indexers.run_indexer(
            indexers.INDEXERS["scip-go"],
            self.root,
            self.out / "go.scip",
            environ=self.environ(
                FAKE_NAME="scip-go", FAKE_VERSION="0.1.26", FAKE_DOCUMENTS="main.go"
            ),
        )
        self.assertEqual((python.status, go.status), ("indexed", "indexed"))
        tool = ScipAnalysisTool(self.root, index_paths=[python.index, go.index])
        output = asyncio.run(tool.analyze("repo"))
        self.assertEqual(output.metadata.languages_processed, ["go", "python"])
        languages = {
            n.properties["path"]: n.properties["language"]
            for n in output.nodes
            if "File" in n.labels
        }
        self.assertEqual(
            languages,
            {"main.go": "go", "src/pkg/a.py": "python", "src/pkg/b.py": "python"},
        )
        producers = [
            item["producer"]["name"]
            for item in output.metadata.custom_metrics["indexes"]
        ]
        self.assertEqual(producers, ["scip-python", "scip-go"])
        only_go = asyncio.run(tool.analyze("repo", languages={"go"}))
        self.assertEqual(only_go.metadata.files_processed, 1)

    def test_cli_exits_nonzero_when_files_are_uncovered(self):
        env = {**os.environ, "PYTHONPATH": str(SRC), "PATH": ""}
        env.pop("DEEPTHOUGHT_SCIP_PYTHON", None)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "deepthought",
                "index-scip",
                str(self.root),
                "--output-dir",
                str(self.out),
                "--language",
                "python",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("indexer_not_installed", result.stderr)
        self.assertEqual(json.loads(result.stdout)["uncovered"], 2)

    def index_args(self, **values):
        from argparse import Namespace

        return Namespace(
            **{
                "workspace": self.root,
                "language": ["python"],
                "include": None,
                "exclude": None,
                "repository_id": "repo",
                "allow_uncovered": False,
                **values,
            }
        )

    def test_default_index_fails_naming_each_uncovered_file(self):
        from contextlib import redirect_stderr
        from io import StringIO

        from deepthought.graph_cli import index_scip

        self.fake("scip-python")
        stderr = StringIO()
        old = os.environ.copy()
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        os.environ.update(self.environ())
        with (
            redirect_stderr(stderr),
            self.assertRaisesRegex(ValueError, "1 source files have no SCIP index"),
        ):
            index_scip(self.index_args(), self.out)
        self.assertIn("src/pkg/b.py: no SCIP index (not_in_index)", stderr.getvalue())
        with redirect_stderr(StringIO()):
            report, indexes = index_scip(
                self.index_args(allow_uncovered=True), self.out
            )
        self.assertEqual(report["uncovered"], 1)
        self.assertEqual([i.coverage[0].file for i, _ in indexes], ["src/pkg/a.py"])
        # Files the run does not analyze are not counted.
        with redirect_stderr(StringIO()):
            report, _ = index_scip(self.index_args(include=["src/pkg/a.py"]), self.out)
        self.assertEqual(report["uncovered"], 0)
        # A run over languages no indexer serves indexes nothing.
        self.assertEqual(
            index_scip(self.index_args(language=["rust"]), self.out), (None, [])
        )


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-python"])
    and importlib.util.find_spec("tree_sitter_python"),
    "Install scip-python and the python extra",
)
class DefaultIndexTest(unittest.TestCase):
    def test_tree_sitter_calls_come_from_the_pinned_indexer(self):
        import asyncio
        from argparse import Namespace

        from deepthought.analysis_tools.tree_sitter import TreeSitterTool
        from deepthought.graph_cli import index_scip

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "work"
            (root / "pkg").mkdir(parents=True)
            (root / "pkg" / "__init__.py").write_text("")
            (root / "pkg" / "a.py").write_text("def helper():\n    return 1\n")
            (root / "pkg" / "b.py").write_text(
                "from pkg.a import helper as h\n\n\ndef run():\n    return h()\n"
            )
            args = Namespace(
                workspace=root,
                language=["python"],
                include=None,
                exclude=None,
                repository_id="repo",
                allow_uncovered=False,
            )
            report, scip_indexes = index_scip(args, Path(scratch) / "out")
            self.assertEqual(report["uncovered"], 0)
            output = asyncio.run(
                TreeSitterTool(root).analyze(
                    "repo", languages={"python"}, scip_indexes=scip_indexes
                )
            )
        calls = [r for r in output.relationships if r.type == "CALLS"]
        self.assertEqual(len(calls), 1)
        self.assertTrue(
            calls[0]
            .target_match["qualified_name"]
            .startswith("repo::pkg/a.py::helper::")
        )
        self.assertEqual(calls[0].properties["producer"], "scip-python@0.6.8")
        self.assertEqual(
            calls[0].properties["receipt"], report["runs"][0]["receipt_sha256"]
        )


if __name__ == "__main__":
    unittest.main()
