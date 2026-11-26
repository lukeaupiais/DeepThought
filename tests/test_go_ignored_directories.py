import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from deepthought import indexers

FILES = {
    "go.mod": "module example.com/m\n\ngo 1.22\n",
    "lib/lib.go": 'package lib\n\nfunc Hello() string { return "hi" }\n',
    "_examples/hello/main.go": (
        'package main\n\nimport "example.com/m/lib"\n\n'
        "func main() { _ = lib.Hello() }\n"
    ),
    # Imports a module nobody declares: the go command cannot load it.
    "_examples/broken/main.go": (
        'package main\n\nimport "example.com/missing/x"\n\nfunc main() { x.Y() }\n'
    ),
    # A testdata package other packages import, as grpc-go's is.
    "testdata/data.go": (
        'package testdata\n\nimport "example.com/m/lib"\n\n'
        "func Path() string { return lib.Hello() }\n"
    ),
    "_tools/go.mod": "module example.com/tools\n\ngo 1.22\n",
    "_tools/t.go": "package tools\n\nfunc T() {}\n",
    # A module of its own below testdata, as sqlc's end-to-end packages are.
    "internal/e2e/testdata/go.mod": "module example.com/e2e\n\ngo 1.22\n",
    "internal/e2e/testdata/case/q.go": "package case1\n\nfunc Q() {}\n",
}


class GoModulesTest(unittest.TestCase):
    def test_a_go_mod_below_an_underscore_directory_is_a_module(self):
        with tempfile.TemporaryDirectory() as scratch:
            for name, text in FILES.items():
                (Path(scratch) / name).parent.mkdir(parents=True, exist_ok=True)
                (Path(scratch) / name).write_text(text)
            self.assertEqual(
                indexers.go_modules(scratch), [".", "_tools", "internal/e2e/testdata"]
            )


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-go"])
    and shutil.which("go")
    and importlib.util.find_spec("google.protobuf"),
    "Install scip-go, go and the scip extra",
)
class IgnoredDirectoriesTest(unittest.TestCase):
    def test_packages_the_wildcard_skips_get_a_pass_of_their_own(self):
        from deepthought._scip import scip_pb2

        with tempfile.TemporaryDirectory() as scratch:
            root, out = Path(scratch) / "work", Path(scratch) / "out"
            for name, text in FILES.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text)
            environ = {**os.environ, "GOPROXY": "off", "GOFLAGS": "-mod=mod"}
            report = indexers.index_workspace(
                root, out, languages=("go",), environ=environ
            )
            coverage = report["coverage"]
            self.assertEqual(coverage["_tools/t.go"]["status"], "covered")
            self.assertEqual(
                coverage["_examples/hello/main.go"]["pass"], "ignored_directory"
            )
            # The broken example costs only its own file.
            self.assertEqual(
                coverage["_examples/broken/main.go"]["ignored_directory_pass"],
                "does_not_load",
            )
            self.assertEqual(coverage["testdata/data.go"]["pass"], "ignored_directory")
            # Indexed by its own module's run, not the root's ignored pass.
            case = coverage["internal/e2e/testdata/case/q.go"]
            self.assertEqual(case["status"], "covered")
            self.assertNotIn("pass", case)
            self.assertEqual(indexers.failed_files(report), {})
            index = scip_pb2.Index()
            index.ParseFromString(
                (out / "scip-go-ignored-directories.scip").read_bytes()
            )
            documents = {d.relative_path: d for d in index.documents}
            self.assertEqual(
                sorted(documents), ["_examples/hello/main.go", "testdata/data.go"]
            )
            for document in documents.values():
                self.assertTrue(
                    any(
                        o.symbol.endswith("`example.com/m/lib`/Hello().")
                        for o in document.occurrences
                    ),
                    document.relative_path,
                )


def _go_version(directory, environ):
    """The Go release the go command runs for a module, as (major, minor)."""
    import re
    import subprocess

    try:
        result = subprocess.run(
            ["go", "env", "GOVERSION"],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=120,
            env=environ,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.match(r"go(\d+)\.(\d+)", result.stdout.strip())
    return (int(match[1]), int(match[2])) if match else None


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-go"])
    and shutil.which("go")
    and importlib.util.find_spec("google.protobuf"),
    "Install scip-go, go and the scip extra",
)
class NewStandardLibraryTest(unittest.TestCase):
    def test_a_standard_package_newer_than_the_indexer_still_indexes(self):
        # Go 1.27 added the standard package uuid, which database/sql/driver
        # imports. scip-go 0.1.26 knew the standard library from a list built
        # into it, took uuid for a module's package without a module, and
        # failed the run: "Should not be possible to have nil module for
        # userland package: uuid uuid".
        with tempfile.TemporaryDirectory() as scratch:
            root, out = Path(scratch) / "work", Path(scratch) / "out"
            root.mkdir()
            (root / "go.mod").write_text("module example.com/svc\n\ngo 1.27.1\n")
            (root / "main.go").write_text(
                'package main\n\nimport "database/sql/driver"\n\n'
                "var _ driver.Valuer\n\nfunc main() {}\n"
            )
            environ = dict(os.environ)
            if (_go_version(root, environ) or (0, 0)) < (1, 27):
                self.skipTest("Needs Go 1.27 or later")
            report = indexers.index_workspace(
                root, out, languages=("go",), environ=environ
            )
            self.assertEqual(indexers.failed_files(report), {})
            self.assertEqual(report["coverage"]["main.go"]["status"], "covered")


if __name__ == "__main__":
    unittest.main()
