import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from deepthought import indexers
from deepthought.js_dependencies import install_directories


def write(root, files):
    for name, content in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


class InstallDirectoriesTest(unittest.TestCase):
    def test_each_lockfile_directory_unless_a_workspace_root_covers_it(self):
        with tempfile.TemporaryDirectory() as scratch:
            write(
                scratch,
                {
                    "pnpm-lock.yaml": "",
                    "packages/a/package-lock.json": "{}",
                    "node_modules/x/package-lock.json": "{}",
                },
            )
            self.assertEqual(install_directories(scratch), ["."])
            os.remove(Path(scratch) / "pnpm-lock.yaml")
            write(scratch, {"server/package-lock.json": "{}"})
            self.assertEqual(install_directories(scratch), ["packages/a", "server"])
            for name in ("packages", "server", "node_modules"):
                shutil.rmtree(Path(scratch) / name)
            self.assertEqual(install_directories(scratch), ["."])


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-typescript"])
    and shutil.which("npm")
    and importlib.util.find_spec("google.protobuf"),
    "Install scip-typescript, npm and the scip extra",
)
class InstalledPackagesTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        base = Path(self.scratch.name)
        self.root, self.out = base / "work", base / "out"
        write(
            self.root / "vendor" / "tinylib",
            {
                "package.json": json.dumps(
                    {"name": "tinylib", "version": "1.0.0", "types": "index.d.ts"}
                ),
                "index.d.ts": "export declare class Thing { go(): number; }\n",
            },
        )
        write(
            self.root,
            {
                "tsconfig.json": json.dumps(
                    {"compilerOptions": {"strict": True}, "include": ["*.ts"]}
                ),
                "app.ts": (
                    'import { Thing } from "tinylib";\n'
                    "export function run() {\n  return new Thing().go();\n}\n"
                ),
            },
        )
        # Offline: a local package, and a registry nothing listens on.
        self.environ = {
            **os.environ,
            "npm_config_registry": "http://127.0.0.1:9/",
            "npm_config_fetch_retries": "0",
            "npm_config_fetch_timeout": "2000",
        }

    def index(self, dependencies):
        (self.root / "package.json").write_text(
            json.dumps({"name": "app", "private": True, "dependencies": dependencies})
        )
        report = indexers.index_workspace(
            self.root, self.out, languages=("typescript",), environ=self.environ
        )
        run = report["runs"][0]
        from deepthought._scip import scip_pb2

        index = scip_pb2.Index()
        index.ParseFromString(Path(run["index"]).read_bytes())
        symbols = {o.symbol for d in index.documents for o in d.occurrences}
        return report, run, symbols

    def test_declared_packages_type_calls_and_leave_the_workspace_alone(self):
        report, run, symbols = self.index({"tinylib": "file:./vendor/tinylib"})
        self.assertEqual(run["status"], "indexed", run["detail"])
        self.assertEqual(run["dependencies"]["status"], "installed")
        self.assertEqual(run["dependencies"]["installs"][0]["manager"], "npm")
        self.assertEqual(report["coverage"]["app.ts"]["status"], "covered")
        self.assertNotIn("warning", report["coverage"]["app.ts"])
        self.assertTrue(any(s.endswith("Thing#go().") for s in symbols), symbols)
        self.assertFalse((self.root / "node_modules").exists())
        receipt = json.loads(Path(run["index"] + ".sources.json").read_text())
        self.assertEqual(receipt["producer"]["dependencies"]["status"], "installed")
        self.assertEqual(receipt["producer"]["arguments"][:3], ["index", "--cwd", "."])

    def test_a_failed_install_is_named_on_each_file_and_still_indexes(self):
        report, run, _ = self.index({"no-such-package-anywhere": "1.0.0"})
        self.assertEqual(run["status"], "indexed", run["detail"])
        self.assertEqual(run["dependencies"]["status"], "failed")
        self.assertEqual(
            report["coverage"]["app.ts"]["warning"], "dependencies_unavailable"
        )
        self.assertEqual(indexers.failed_files(report), {})


if __name__ == "__main__":
    unittest.main()
