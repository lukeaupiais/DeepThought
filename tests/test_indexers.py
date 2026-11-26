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
    import json, os, sys
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
    documents = os.environ["FAKE_DOCUMENTS"].split(",")
    if argv[-1].endswith("tsconfig.json"):
        # A pass over chosen files: its project names them, absolute.
        with open(argv[-1]) as config:
            project = json.load(config)
        documents = [os.path.relpath(f) for f in project.get("files", [])] + documents
    # Only files under the directory it runs in, as a real indexer.
    documents = [path for path in documents if os.path.isfile(path)]
    # As scip-go: a file for another platform builds only there.
    documents = [
        path
        for path in documents
        if not path.endswith("_windows.go") or os.environ.get("GOOS") == "windows"
    ]
    for path in reversed(documents):
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

# Stands in for the go command: two platforms, and files whose name ends in
# _windows.go build only for windows/amd64; ignore.go builds nowhere.
FAKE_GO = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys
    argv = sys.argv[1:]
    platform = os.environ["GOOS"] + "/" + os.environ["GOARCH"]
    if argv[:2] == ["tool", "dist"]:
        print(json.dumps([
            {{"GOOS": "linux", "GOARCH": "amd64", "FirstClass": True}},
            {{"GOOS": "windows", "GOARCH": "amd64", "FirstClass": True}},
        ]))
    elif argv[:2] == ["env", "GOVERSION"]:
        print("go1.24.7")
    elif argv[0] == "list":
        names = sorted(n for n in os.listdir(".") if n.endswith(".go"))
        def builds(name):
            if name == "ignore.go":
                return False
            return name.endswith("_windows.go") == (platform == "windows/amd64")
        print(os.getcwd() + "\\t" + " ".join(n for n in names if builds(n))
              + "\\t" + " ".join(n for n in names if not builds(n)))
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
        # The source roots, then the scratch directory the project's
        # declared dependencies are installed into.
        *roots, site = python_path.split(os.pathsep)
        self.assertEqual(
            roots, [str(self.root.resolve()), str((self.root / "src").resolve())]
        )
        self.assertTrue(site.endswith("site-packages"), site)
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
        # The project is written outside the workspace, which may be
        # read-only, rather than inferred into it.
        argv = self.args_log.read_text().split("\n")[0].split(" ")
        self.assertNotIn("--infer-tsconfig", argv)
        self.assertTrue(argv[-1].endswith("tsconfig.json"), argv)
        self.assertFalse(Path(argv[-1]).is_relative_to(self.root.resolve()))

    def test_files_outside_every_tsconfig_get_a_second_pass(self):
        (self.root / "tsconfig.json").write_text("{}")
        (self.root / "web.ts").write_text("export const a = 1;\n")
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "deno.ts").write_text("export const b = 1;\n")
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
        self.assertEqual(report["uncovered"], 0)
        self.assertEqual(
            report["coverage"]["scripts/deno.ts"],
            {
                "language": "typescript",
                "status": "covered",
                "indexer": "scip-typescript",
                "pass": "outside_tsconfig",
            },
        )
        self.assertNotIn("pass", report["coverage"]["web.ts"])
        first, second = report["runs"]
        self.assertEqual((first["documents"], second["documents"]), (1, 1))
        receipt = indexers.validate_receipt(self.root, second["index"])
        self.assertEqual(receipt["documents"], ["scripts/deno.ts"])
        self.assertEqual(
            receipt["producer"]["project"],
            {
                "compilerOptions": {
                    "allowImportingTsExtensions": True,
                    "allowJs": True,
                    "noEmit": True,
                },
                "extends": "tsconfig.json",
                "files": ["scripts/deno.ts"],
            },
        )
        # The passes cover disjoint files, so the scip tool reads both.
        import asyncio

        from deepthought.analysis_tools.scip import ScipAnalysisTool

        tool = ScipAnalysisTool(
            self.root, index_paths=[first["index"], second["index"]]
        )
        self.assertEqual(asyncio.run(tool.analyze("repo")).metadata.files_processed, 2)
        with self.assertRaisesRegex(ValueError, "scip-typescript"):
            indexers.run_indexer(
                indexers.INDEXERS["scip-python"], self.root, self.out / "p", files=[]
            )

    def test_each_go_module_is_indexed_from_its_own_directory(self):
        for module in ("lib", "svc"):
            (self.root / module).mkdir()
            (self.root / module / "go.mod").write_text(f"module example.com/{module}\n")
        (self.root / "lib" / "lib.go").write_text("package lib\n")
        (self.root / "svc" / "main.go").write_text("package main\n")
        (self.root / "svc" / "testdata").mkdir()
        (self.root / "svc" / "testdata" / "go.mod").write_text("module fixture\n")
        (self.root / "tools").mkdir()
        (self.root / "tools" / "gen.go").write_text("package tools\n")
        # The testdata module holds no Go file, so it gets no run.
        self.assertEqual(indexers.go_modules(self.root), ["lib", "svc", "svc/testdata"])
        self.fake("scip-go")
        report = indexers.index_workspace(
            self.root,
            self.out,
            languages=("go",),
            environ=self.environ(
                FAKE_NAME="scip-go",
                FAKE_VERSION="0.1.26",
                FAKE_DOCUMENTS="lib.go,main.go",
            ),
        )
        self.assertEqual(
            [
                (run["module"], run["status"], run["documents"])
                for run in report["runs"]
            ],
            [("lib", "indexed", 1), ("svc", "indexed", 1)],
        )
        self.assertEqual(report["coverage"]["lib/lib.go"]["status"], "covered")
        self.assertEqual(report["coverage"]["svc/main.go"]["status"], "covered")
        self.assertEqual(report["coverage"]["tools/gen.go"]["reason"], "no_go_module")
        receipt = indexers.validate_receipt(self.root, self.out / "scip-go-svc.scip")
        self.assertEqual(receipt["documents"], ["svc/main.go"])
        self.assertEqual(receipt["producer"]["module"], "svc")

    def test_go_files_for_another_platform_get_a_pass_for_it(self):
        (self.root / "go.mod").write_text("module example.com/m\n")
        for name in ("main.go", "term_windows.go", "ignore.go"):
            (self.root / name).write_text("package main\n")
        self.fake("scip-go")
        go = self.bin / "go"
        go.write_text(FAKE_GO.format(python=sys.executable))
        go.chmod(go.stat().st_mode | stat.S_IXUSR)
        report = indexers.index_workspace(
            self.root,
            self.out,
            languages=("go",),
            environ=self.environ(
                FAKE_NAME="scip-go",
                FAKE_VERSION="0.1.26",
                FAKE_DOCUMENTS="main.go,term_windows.go",
            ),
        )
        coverage = report["coverage"]
        self.assertEqual(coverage["main.go"]["status"], "covered")
        self.assertEqual(
            coverage["term_windows.go"],
            {
                "language": "go",
                "status": "covered",
                "indexer": "scip-go",
                "pass": "platform",
                "platform": "windows/amd64",
            },
        )
        self.assertEqual(coverage["ignore.go"]["reason"], "build_constraints")
        receipt = indexers.validate_receipt(
            self.root, self.out / "scip-go-windows-amd64.scip"
        )
        self.assertEqual(receipt["documents"], ["term_windows.go"])
        self.assertEqual(receipt["producer"]["platform"], "windows/amd64")
        self.assertEqual(receipt["producer"]["go_version"], "go1.24.7")

    def test_own_indexes_are_read_whole_and_checked_against_their_receipts(self):
        from unittest import mock

        from deepthought import scip

        self.fake("scip-python")
        report = indexers.index_workspace(
            self.root, self.out, languages=("python",), environ=self.environ()
        )
        index = Path(report["runs"][0]["index"])
        with mock.patch.object(scip, "ingest_scip", wraps=scip.ingest_scip) as ingest:
            [(source, receipt)] = indexers.ingest_indexed(report, "repo", self.root)
        # No cap below the index's own size, however large the repository.
        self.assertEqual(
            ingest.call_args.kwargs["max_index_bytes"], index.stat().st_size
        )
        self.assertEqual(receipt, report["runs"][0]["receipt_sha256"])
        self.assertEqual([c.file for c in source.coverage], ["src/pkg/a.py"])
        (self.root / "src" / "pkg" / "a.py").write_text("def f():\n    return 2\n")
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            indexers.ingest_indexed(report, "repo", self.root)
        # An index that cannot be read stops the run instead of reading as
        # an index without facts.
        index.write_bytes(b"not an index")
        with self.assertRaisesRegex(ValueError, "could not be read: invalid_index"):
            indexers.ingest_indexed(report, "repo")

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
                **values,
            }
        )

    def test_default_index_fails_only_when_an_indexer_fails(self):
        from contextlib import redirect_stderr
        from io import StringIO

        from deepthought.graph_cli import index_scip

        old = os.environ.copy()
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        os.environ.update({"PATH": ""})
        os.environ.pop("DEEPTHOUGHT_SCIP_PYTHON", None)
        stderr = StringIO()
        with (
            redirect_stderr(stderr),
            self.assertRaisesRegex(ValueError, "2 source files have no SCIP index"),
        ):
            index_scip(self.index_args(), self.out)
        self.assertIn("indexer_not_installed", stderr.getvalue())
        # A file the installed indexer leaves out is named and parser-bound.
        self.fake("scip-python")
        os.environ.update(self.environ())
        stderr = StringIO()
        with redirect_stderr(stderr):
            report, indexes = index_scip(self.index_args(), self.out)
        self.assertIn("src/pkg/b.py: no SCIP index (not_in_index)", stderr.getvalue())
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
