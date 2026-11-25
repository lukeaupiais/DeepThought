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
}


class GoModulesTest(unittest.TestCase):
    def test_a_go_mod_below_an_underscore_directory_is_a_module(self):
        with tempfile.TemporaryDirectory() as scratch:
            for name, text in FILES.items():
                (Path(scratch) / name).parent.mkdir(parents=True, exist_ok=True)
                (Path(scratch) / name).write_text(text)
            self.assertEqual(indexers.go_modules(scratch), [".", "_tools"])


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


if __name__ == "__main__":
    unittest.main()
