import base64
import hashlib
import http.server
import importlib.util
import io
import json
import os
import shutil
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path

from deepthought import indexers
from deepthought import js_dependencies
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
                    "package.json": "{}",
                    "pnpm-lock.yaml": "",
                    "packages/a/package.json": "{}",
                    "packages/a/package-lock.json": "{}",
                    "node_modules/x/package.json": "{}",
                    "node_modules/x/package-lock.json": "{}",
                },
            )
            self.assertEqual(install_directories(scratch), ["."])
            os.remove(Path(scratch) / "pnpm-lock.yaml")
            write(
                scratch,
                {"server/package.json": "{}", "server/package-lock.json": "{}"},
            )
            self.assertEqual(install_directories(scratch), ["packages/a", "server"])
            # A lockfile with no package.json beside it installs nothing and
            # covers nothing.
            write(scratch, {"package-lock.json": "{}"})
            os.remove(Path(scratch) / "package.json")
            self.assertEqual(install_directories(scratch), ["packages/a", "server"])
            for name in ("packages", "server", "node_modules"):
                shutil.rmtree(Path(scratch) / name)
            self.assertEqual(install_directories(scratch), [])

    def test_packages_that_are_not_workspaces_install_where_they_are(self):
        declares = json.dumps({"dependencies": {"x": "1"}})
        with tempfile.TemporaryDirectory() as scratch:
            write(
                scratch,
                {
                    "package.json": json.dumps({"devDependencies": {"gulp": "4"}}),
                    "packages/a/package.json": declares,
                    "packages/b/package.json": declares,
                    "packages/b/test/package.json": declares,
                    "packages/c/package.json": "{}",
                },
            )
            self.assertEqual(
                install_directories(scratch),
                [".", "packages/a", "packages/b", "packages/b/test"],
            )
            # A workspace root installs its packages.
            write(
                scratch,
                {"package.json": json.dumps({"workspaces": ["packages/*"]})},
            )
            self.assertEqual(install_directories(scratch), ["."])


class ManagerTest(unittest.TestCase):
    def manager(self, files):
        with tempfile.TemporaryDirectory() as scratch:
            write(scratch, files)
            manager, arguments, lockfile = js_dependencies._managers(scratch)[0]
            command = (
                js_dependencies._yarn_berry_command(scratch, dict(os.environ))
                if arguments is js_dependencies.YARN_BERRY
                else None
            )
            if command and len(command) == 2 and command[1].startswith(scratch):
                command = [Path(command[0]).name, Path(command[1]).name]
            return manager, arguments[:2], lockfile, command

    def test_the_lockfile_chooses_the_manager(self):
        self.assertEqual(
            self.manager({"bun.lock": "{}", "yarn.lock": ""})[:3],
            ("bun", ["install", "--frozen-lockfile"], "bun.lock"),
        )
        self.assertEqual(
            self.manager({"npm-shrinkwrap.json": "{}", "package-lock.json": "{}"})[:3],
            ("npm", ["ci", "--ignore-scripts"], "npm-shrinkwrap.json"),
        )
        self.assertEqual(
            self.manager({"yarn.lock": "# yarn lockfile v1\n"})[:3],
            ("yarn", ["install", "--frozen-lockfile"], "yarn.lock"),
        )

    def test_every_lockfile_of_a_directory_is_tried_in_order(self):
        with tempfile.TemporaryDirectory() as scratch:
            write(scratch, {"yarn.lock": "", "package-lock.json": "{}"})
            self.assertEqual(
                [m[2] for m in js_dependencies._managers(scratch)],
                ["yarn.lock", "package-lock.json", "package-lock.json"],
            )

    def test_npm_tries_the_configured_registry_then_the_lockfiles_hosts(self):
        with tempfile.TemporaryDirectory() as scratch:
            write(scratch, {"package-lock.json": "{}"})
            self.assertEqual(
                [m[1][-1] for m in js_dependencies._managers(scratch)],
                ["--replace-registry-host=always", "--no-fund"],
            )

    @unittest.skipUnless(shutil.which("node"), "Install node")
    def test_yarn_2_and_later_run_the_projects_own_release(self):
        berry = "# comment\n\n__metadata:\n  version: 8\n"
        manager, arguments, _, command = self.manager(
            {
                "yarn.lock": berry,
                ".yarnrc.yml": "yarnPath: .yarn/releases/yarn-4.5.0.cjs\n",
                ".yarn/releases/yarn-4.5.0.cjs": "",
            }
        )
        self.assertEqual((manager, arguments), ("yarn", ["install", "--immutable"]))
        self.assertEqual(command, ["node", "yarn-4.5.0.cjs"])
        if shutil.which("corepack"):
            *_, command = self.manager({"yarn.lock": berry})
            self.assertEqual(command[1:], ["yarn"])


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

    def test_a_file_dependencys_prepare_script_does_not_run(self):
        marker = Path(self.scratch.name) / "prepare-ran"
        package = self.root / "vendor" / "tinylib" / "package.json"
        manifest = json.loads(package.read_text())
        manifest["scripts"] = {"prepare": f"touch {marker}"}
        package.write_text(json.dumps(manifest))
        _, run, symbols = self.index({"tinylib": "file:./vendor/tinylib"})
        self.assertEqual(run["dependencies"]["status"], "installed")
        self.assertFalse(marker.exists())
        self.assertTrue(any(s.endswith("Thing#go().") for s in symbols), symbols)

    def test_a_javascript_workspace_without_a_tsconfig_is_indexed(self):
        (self.root / "tsconfig.json").unlink()
        (self.root / "app.ts").unlink()
        (self.root / "app.js").write_text(
            'import { Thing } from "tinylib";\n'
            "export function run() {\n  return new Thing().go();\n}\n"
        )
        (self.root / "package.json").write_text(
            json.dumps(
                {
                    "name": "app",
                    "private": True,
                    "dependencies": {"tinylib": "file:./vendor/tinylib"},
                }
            )
        )
        report = indexers.index_workspace(
            self.root, self.out, languages=("javascript",), environ=self.environ
        )
        (run,) = report["runs"]
        self.assertEqual(run["status"], "indexed", run["detail"])
        self.assertEqual(run["dependencies"]["status"], "installed")
        self.assertEqual(report["coverage"]["app.js"]["status"], "covered")
        self.assertFalse((self.root / "tsconfig.json").exists())
        from deepthought._scip import scip_pb2

        index = scip_pb2.Index()
        index.ParseFromString(Path(run["index"]).read_bytes())
        symbols = {o.symbol for d in index.documents for o in d.occurrences}
        self.assertTrue(any(s.endswith("Thing#go().") for s in symbols), symbols)

    @unittest.skipUnless(shutil.which("bun"), "Install bun")
    def test_a_bun_lockfile_installs_with_bun(self):
        (self.root / "package.json").write_text(
            json.dumps(
                {
                    "name": "app",
                    "private": True,
                    "dependencies": {"tinylib": "file:./vendor/tinylib"},
                }
            )
        )
        import subprocess

        subprocess.run(
            ["bun", "install", "--ignore-scripts"],
            cwd=self.root,
            check=True,
            capture_output=True,
            env=self.environ,
        )
        shutil.rmtree(self.root / "node_modules")
        self.assertTrue((self.root / "bun.lock").is_file())
        report, run, symbols = self.index({"tinylib": "file:./vendor/tinylib"})
        self.assertEqual(run["dependencies"]["status"], "installed")
        self.assertEqual(run["dependencies"]["installs"][0]["manager"], "bun")
        self.assertTrue(any(s.endswith("Thing#go().") for s in symbols), symbols)

    def test_a_failed_install_is_named_on_each_file_and_still_indexes(self):
        report, run, _ = self.index({"no-such-package-anywhere": "1.0.0"})
        self.assertEqual(run["status"], "indexed", run["detail"])
        self.assertEqual(run["dependencies"]["status"], "failed")
        self.assertEqual(
            report["coverage"]["app.ts"]["warning"], "dependencies_unavailable"
        )
        self.assertEqual(indexers.failed_files(report), {})


def tarball(files):
    """An npm package tarball, every file under ``package/``."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(f"package/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@unittest.skipUnless(shutil.which("npm"), "Install npm")
class UnreachableLockfileHostTest(unittest.TestCase):
    """A lockfile pinning tarballs to a host that is gone still installs
    from the configured registry, checked against its integrity hash."""

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        base = Path(self.scratch.name)
        self.root, self.out = base / "work", base / "out"
        data = tarball(
            {
                "package.json": json.dumps(
                    {"name": "tinylib", "version": "1.0.0", "types": "index.d.ts"}
                ),
                "index.d.ts": "export declare class Thing { go(): number; }\n",
            }
        )
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()

        class Registry(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/tinylib/-/tinylib-1.0.0.tgz":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *_):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Registry)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        registry = f"http://127.0.0.1:{server.server_port}/"
        gone = "http://127.0.0.1:9/tinylib/-/tinylib-1.0.0.tgz"
        write(
            self.root,
            {
                "package.json": json.dumps(
                    {
                        "name": "app",
                        "private": True,
                        "dependencies": {"tinylib": "1.0.0"},
                    }
                ),
                "package-lock.json": json.dumps(
                    {
                        "name": "app",
                        "lockfileVersion": 3,
                        "requires": True,
                        "packages": {
                            "": {"name": "app", "dependencies": {"tinylib": "1.0.0"}},
                            "node_modules/tinylib": {
                                "version": "1.0.0",
                                "resolved": gone,
                                "integrity": integrity,
                            },
                        },
                    }
                ),
                "tsconfig.json": json.dumps(
                    {"compilerOptions": {"strict": True}, "include": ["*.ts"]}
                ),
                "app.ts": (
                    'import { Thing } from "tinylib";\n'
                    "export function run() {\n  return new Thing().go();\n}\n"
                ),
            },
        )
        self.environ = {
            **os.environ,
            "npm_config_registry": registry,
            "npm_config_cache": str(base / "cache"),
            "npm_config_fetch_retries": "0",
            "npm_config_fetch_timeout": "2000",
            "NO_PROXY": "127.0.0.1",
            "no_proxy": "127.0.0.1",
        }

    def test_the_configured_registry_serves_the_pinned_tarball(self):
        report = indexers.index_workspace(
            self.root, self.out, languages=("typescript",), environ=self.environ
        )
        (run,) = report["runs"]
        self.assertEqual(run["status"], "indexed", run["detail"])
        install = run["dependencies"]["installs"][0]
        self.assertEqual(install["status"], "installed", install)
        self.assertIn("--replace-registry-host=always", install["command"])
        self.assertNotIn("warning", report["coverage"]["app.ts"])
        from deepthought._scip import scip_pb2

        index = scip_pb2.Index()
        index.ParseFromString(Path(run["index"]).read_bytes())
        symbols = {o.symbol for d in index.documents for o in d.occurrences}
        self.assertTrue(any(s.endswith("Thing#go().") for s in symbols), symbols)


if __name__ == "__main__":
    unittest.main()
