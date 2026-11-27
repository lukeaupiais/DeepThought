import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from deepthought import indexers
from deepthought.go_dependencies import download


def write(root, files):
    for name, content in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


DEP_MOD = "module example.com/dep\n\ngo 1.21\n"
DEP_GO = 'package dep\n\nfunc Hello() string { return "hello" }\n'


def module_proxy(directory):
    """A GOPROXY directory serving example.com/dep v1.0.0."""
    versions = Path(directory) / "example.com" / "dep" / "@v"
    versions.mkdir(parents=True)
    (versions / "list").write_text("v1.0.0\n")
    (versions / "v1.0.0.info").write_text(
        '{"Version": "v1.0.0", "Time": "2024-01-01T00:00:00Z"}'
    )
    (versions / "v1.0.0.mod").write_text(DEP_MOD)
    # example.com/nozip has its go.mod served but not its files.
    nozip = Path(directory) / "example.com" / "nozip" / "@v"
    nozip.mkdir(parents=True)
    (nozip / "list").write_text("v1.0.0\n")
    (nozip / "v1.0.0.info").write_text((versions / "v1.0.0.info").read_text())
    (nozip / "v1.0.0.mod").write_text("module example.com/nozip\n\ngo 1.21\n")
    with zipfile.ZipFile(versions / "v1.0.0.zip", "w") as archive:
        archive.writestr("example.com/dep@v1.0.0/go.mod", DEP_MOD)
        archive.writestr("example.com/dep@v1.0.0/dep.go", DEP_GO)
    return Path(directory).as_uri()


def app(requirement):
    return {
        "go.mod": (
            f"module example.com/app\n\ngo 1.21\n\nrequire {requirement} v1.0.0\n"
        ),
        "main.go": (
            f'package main\n\nimport "{requirement}"\n\n'
            "func main() { println(dep.Hello()) }\n"
        ),
    }


class _ProxyTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.base = Path(scratch.name)
        self.cache = self.base / "user-modcache"
        self.cache.mkdir()
        # Offline: a proxy on disk, no checksum database, and a module cache
        # of the machine that must stay untouched.
        self.environ = {
            **os.environ,
            "GOPROXY": module_proxy(self.base / "proxy"),
            "GOSUMDB": "off",
            "GOMODCACHE": str(self.cache),
            "GOFLAGS": "-mod=mod",
        }


@unittest.skipUnless(shutil.which("go"), "Install go")
class DownloadTest(_ProxyTest):
    def test_each_module_downloads_into_scratch_and_the_workspace_stays(self):
        root = self.base / "work"
        write(root, app("example.com/dep"))
        write(root / "sub", app("example.com/missing"))
        write(root / "vend", {**app("example.com/dep"), "vendor/modules.txt": ""})
        write(root / "bare", {"go.mod": "module example.com/bare\n\ngo 1.21\n"})
        # Each module is downloaded as scip-go indexes it, not as a go.work
        # that leaves it out would have it.
        write(root, {"go.work": "go 1.21\n\nuse ./vend\n"})
        found = download(
            root,
            [".", "bare", "nothing", "sub", "vend"],
            self.base / "scratch",
            environ=self.environ,
        )
        modules = found["modules"]
        self.assertEqual(
            {
                m: (e["status"], e["downloaded"], e["failed"])
                for m, e in modules.items()
            },
            {
                ".": ("downloaded", 1, []),
                "bare": ("none", 0, []),
                "nothing": ("none", 0, []),
                "sub": ("failed", 0, ["example.com/missing@v1.0.0"]),
                "vend": ("present", 0, []),
            },
        )
        self.assertIn("example.com/missing", modules["sub"]["detail"])
        self.assertIsNone(modules["."]["detail"])
        self.assertIsNone(modules["."]["go_sum_sha256"])
        cache = Path(found["modcache"])
        self.assertTrue(cache.is_relative_to(self.base / "scratch"))
        source = cache / "example.com" / "dep@v1.0.0" / "dep.go"
        self.assertTrue(source.is_file())
        self.assertTrue(source.parent.stat().st_mode & 0o200)
        # Checksums go to the copy; the workspace and the machine's cache
        # are left as they were, and the scratch cache can be removed.
        self.assertFalse((root / "go.sum").exists())
        self.assertEqual(list(self.cache.iterdir()), [])
        shutil.rmtree(cache)

    def test_a_partial_download_names_what_failed(self):
        root = self.base / "work"
        write(root, app("example.com/dep"))
        with (root / "go.mod").open("a") as go_mod:
            go_mod.write("require example.com/nozip v1.0.0\n")
        (root / "go.sum").write_text("")
        entry = download(root, ["."], self.base / "scratch", environ=self.environ)[
            "modules"
        ]["."]
        self.assertEqual(entry["status"], "partial")
        self.assertEqual(entry["downloaded"], 1)
        self.assertEqual(entry["failed"], ["example.com/nozip@v1.0.0"])
        self.assertEqual(
            entry["go_sum_sha256"],
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        self.assertEqual((root / "go.sum").read_text(), "")

    def test_a_module_that_does_not_match_its_go_sum_is_not_downloaded(self):
        root = self.base / "work"
        write(root, app("example.com/dep"))
        (root / "go.sum").write_text(
            "example.com/dep v1.0.0 h1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n"
        )
        entry = download(root, ["."], self.base / "scratch", environ=self.environ)[
            "modules"
        ]["."]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("checksum mismatch", entry["detail"])

    def test_without_go_nothing_downloads(self):
        root = self.base / "work"
        write(root, app("example.com/dep"))
        entry = download(
            root, ["."], self.base / "scratch", environ={"PATH": str(self.base)}
        )["modules"]["."]
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["detail"], "go is not installed")


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-go"])
    and shutil.which("go")
    and importlib.util.find_spec("google.protobuf"),
    "Install scip-go, go and the scip extra",
)
class IndexedModulesTest(_ProxyTest):
    def module(self, directory, requirement):
        write(directory, app(requirement))
        if requirement == "example.com/dep":
            # The checksums a real module would commit.
            probe = self.base / "probe"
            write(probe, app(requirement))
            subprocess.run(
                ["go", "mod", "tidy"],
                cwd=probe,
                env={**self.environ, "GOMODCACHE": str(self.base / "probe-cache")},
                check=True,
                capture_output=True,
            )
            shutil.copyfile(probe / "go.sum", directory / "go.sum")
            shutil.rmtree(self.base / "probe-cache", ignore_errors=True)

    def index_all(self, root):
        environ = {**self.environ, "GOFLAGS": ""}
        return indexers.index_workspace(
            root, self.base / "out", languages=("go",), environ=environ
        )

    def index(self, requirement):
        root = self.base / "work"
        self.module(root, requirement)
        report = self.index_all(root)
        (run,) = report["runs"]
        if run["index"] is None:
            return root, report, run, set()
        from deepthought._scip import scip_pb2

        index = scip_pb2.Index()
        index.ParseFromString(Path(run["index"]).read_bytes())
        symbols = {o.symbol for d in index.documents for o in d.occurrences}
        return root, report, run, symbols

    def test_modules_download_once_for_every_run(self):
        root = self.base / "work"
        self.module(root, "example.com/dep")
        self.module(root / "sub", "example.com/dep")
        # Built only on Windows: a platform pass of its own.
        write(root, {"win_windows.go": "package main\n\nfunc win() {}\n"})
        from deepthought import go_dependencies

        with patch.object(
            go_dependencies, "download", wraps=go_dependencies.download
        ) as spy:
            report = self.index_all(root)
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(spy.call_args.args[1], [".", "sub"])
        self.assertEqual(
            [
                (r["module"], r["platform"], r["status"], r["dependencies"]["status"])
                for r in report["runs"]
            ],
            [
                (".", "linux/amd64", "indexed", "downloaded"),
                ("sub", "linux/amd64", "indexed", "downloaded"),
                (".", "windows/386", "indexed", "downloaded"),
            ],
        )
        self.assertEqual(report["coverage"]["win_windows.go"]["status"], "covered")
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_the_indexer_reads_only_the_downloaded_modules(self):
        environment = indexers._environment(
            indexers.INDEXERS["scip-go"],
            self.base,
            self.environ,
            None,
            {"modcache": "/cache"},
        )
        self.assertEqual(
            (environment["GOMODCACHE"], environment["GOPROXY"]), ("/cache", "off")
        )

    def test_required_modules_type_calls_into_them(self):
        root, report, run, symbols = self.index("example.com/dep")
        self.assertEqual(run["status"], "indexed", run["detail"])
        self.assertEqual(run["dependencies"]["status"], "downloaded")
        self.assertEqual(report["coverage"]["main.go"]["status"], "covered")
        self.assertNotIn("warning", report["coverage"]["main.go"])
        self.assertTrue(
            any("example.com/dep" in s and s.endswith("Hello().") for s in symbols),
            symbols,
        )
        self.assertEqual(list(self.cache.iterdir()), [])
        receipt = json.loads(Path(run["index"] + ".sources.json").read_text())
        dependencies = receipt["producer"]["dependencies"]
        self.assertEqual(dependencies["status"], "downloaded")
        self.assertNotIn("detail", dependencies)
        self.assertNotIn("modcache", json.dumps(receipt))

    def test_a_failed_download_is_named_on_each_file(self):
        _, report, run, _ = self.index("example.com/missing")
        self.assertEqual(run["dependencies"]["status"], "failed")
        # scip-go 0.2.7 still indexes a package whose module is missing (its
        # symbols are then misattributed), so the file is covered and says
        # its dependencies were unavailable.
        self.assertEqual((run["status"], run["reason"]), ("indexed", None))
        self.assertEqual(
            report["coverage"]["main.go"],
            {
                "language": "go",
                "status": "covered",
                "indexer": "scip-go",
                "warning": "dependencies_unavailable",
            },
        )
        self.assertEqual(indexers.failed_files(report), {})


if __name__ == "__main__":
    unittest.main()
