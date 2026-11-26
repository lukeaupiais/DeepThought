import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from deepthought import indexers


def write(root, files):
    for name, content in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


class ProjectsTest(unittest.TestCase):
    def test_nested_tsconfigs_unless_the_root_indexes_workspaces(self):
        with tempfile.TemporaryDirectory() as scratch:
            write(
                scratch,
                {
                    "tsconfig.json": "{}",
                    "server/tsconfig.json": "{}",
                    "server/node_modules/x/tsconfig.json": "{}",
                    "web/app/tsconfig.json": "{}",
                    ".hidden/tsconfig.json": "{}",
                },
            )
            self.assertEqual(
                indexers.typescript_projects(scratch), ["server", "web/app"]
            )
            # Yarn lists the workspaces of a private root only; an npm
            # workspace root that is not private keeps its projects.
            write(scratch, {"package.json": json.dumps({"workspaces": ["web/*"]})})
            self.assertEqual(
                indexers.typescript_projects(scratch), ["server", "web/app"]
            )
            write(
                scratch,
                {"package.json": json.dumps({"private": True, "workspaces": ["*"]})},
            )
            self.assertEqual(indexers.typescript_projects(scratch), [])
            (Path(scratch) / "package.json").unlink()
            write(scratch, {"pnpm-workspace.yaml": "packages: ['*']\n"})
            self.assertEqual(indexers.typescript_projects(scratch), [])


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-typescript"])
    and importlib.util.find_spec("google.protobuf"),
    "Install scip-typescript and the scip extra",
)
class NestedProjectIndexTest(unittest.TestCase):
    def test_a_nested_project_resolves_through_its_own_base_url(self):
        from deepthought._scip import scip_pb2

        with tempfile.TemporaryDirectory() as scratch:
            root, out = Path(scratch) / "work", Path(scratch) / "out"
            write(
                root,
                {
                    # The server's imports resolve only through its baseUrl.
                    "server/tsconfig.json": json.dumps(
                        {
                            "compilerOptions": {"baseUrl": ".", "strict": True},
                            "include": ["src"],
                        }
                    ),
                    "server/src/b.ts": "export function helper() { return 1; }\n",
                    "server/src/a.ts": (
                        'import { helper } from "src/b";\n'
                        "export const run = () => helper();\n"
                    ),
                    "scripts/tool.ts": "export const tool = 1;\n",
                },
            )
            report = indexers.index_workspace(root, out, languages=("typescript",))
            runs = {(r["module"] if "module" in r else None): r for r in report["runs"]}
            self.assertEqual(runs["server"]["status"], "indexed", runs["server"])
            coverage = report["coverage"]
            self.assertEqual(coverage["server/src/a.ts"]["status"], "covered")
            self.assertEqual(coverage["scripts/tool.ts"]["status"], "covered")
            index = scip_pb2.Index()
            index.ParseFromString(Path(runs["server"]["index"]).read_bytes())
            documents = {d.relative_path: d for d in index.documents}
            self.assertEqual(sorted(documents), ["server/src/a.ts", "server/src/b.ts"])
            defined = {
                o.symbol
                for o in documents["server/src/b.ts"].occurrences
                if o.symbol_roles & 1 and "helper" in o.symbol
            }
            used = {o.symbol for o in documents["server/src/a.ts"].occurrences}
            self.assertTrue(defined and defined <= used, (defined, used))
            # The root run keeps none of the server's documents.
            root_index = scip_pb2.Index()
            root_index.ParseFromString(Path(runs[None]["index"]).read_bytes())
            self.assertFalse(
                any(d.relative_path.startswith("server/") for d in root_index.documents)
            )


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-typescript"])
    and shutil.which("pnpm")
    and importlib.util.find_spec("google.protobuf"),
    "Install scip-typescript, pnpm and the scip extra",
)
class PnpmVersionTest(unittest.TestCase):
    def test_a_pnpm_workspace_pinning_another_pnpm_still_indexes(self):
        # hono pins packageManager pnpm@12.6.0: pnpm 10 switched to that
        # release, the download failed, and scip-typescript's `pnpm ls`
        # with it. The pnpm on PATH lists the workspaces instead.
        from deepthought._scip import scip_pb2

        with tempfile.TemporaryDirectory() as scratch:
            root, out = Path(scratch) / "work", Path(scratch) / "out"
            write(
                root,
                {
                    "package.json": json.dumps(
                        {
                            "name": "root",
                            "private": True,
                            "packageManager": "pnpm@99.0.0",
                        }
                    ),
                    "pnpm-workspace.yaml": "packages: ['packages/*']\n",
                    "packages/a/package.json": json.dumps({"name": "a"}),
                    "packages/a/tsconfig.json": json.dumps({"include": ["*.ts"]}),
                    "packages/a/index.ts": "export const one = 1;\n",
                },
            )
            environ = {
                **os.environ,
                "npm_config_registry": "http://127.0.0.1:9/",
                "npm_config_fetch_retries": "0",
            }
            report = indexers.index_workspace(
                root, out, languages=("typescript",), environ=environ
            )
            self.assertEqual(indexers.failed_files(report), {})
            self.assertEqual(
                report["coverage"]["packages/a/index.ts"]["status"], "covered"
            )
            (run,) = [r for r in report["runs"] if "uncovered" not in r["index"]]
            index = scip_pb2.Index()
            index.ParseFromString(Path(run["index"]).read_bytes())
            self.assertIn(
                "packages/a/index.ts", {d.relative_path for d in index.documents}
            )


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-typescript"])
    and importlib.util.find_spec("google.protobuf")
    and shutil.which("npm"),
    "Install scip-typescript, npm and the scip extra",
)
class GeneratedConfigTest(unittest.TestCase):
    def test_a_sveltekit_project_indexes_without_its_generated_config(self):
        # web/tsconfig.json extends .svelte-kit/tsconfig.json, which only
        # `svelte-kit sync` writes, by running the repository's own
        # svelte.config.js; the compiler could not read it and no web file
        # indexed (immich).
        from deepthought._scip import scip_pb2

        with tempfile.TemporaryDirectory() as scratch:
            root, out = Path(scratch) / "work", Path(scratch) / "out"
            write(
                root,
                {
                    "web/package.json": json.dumps(
                        {"name": "web", "dependencies": {"local": "file:./local"}}
                    ),
                    "web/local/package.json": json.dumps({"name": "local"}),
                    "web/tsconfig.json": json.dumps(
                        {
                            "extends": "./.svelte-kit/tsconfig.json",
                            "compilerOptions": {"strict": True},
                        }
                    ),
                    "web/src/lib/a.ts": "export const one = 1;\n",
                    "web/src/b.ts": 'import { one } from "./lib/a";\n'
                    "export const two = one + 1;\n",
                },
            )
            report = indexers.index_workspace(root, out, languages=("typescript",))
            self.assertEqual(indexers.failed_files(report), {})
            (run,) = [r for r in report["runs"] if r.get("module") == "web"]
            self.assertEqual(run["status"], "indexed", run)
            self.assertEqual(
                run["dependencies"]["placeholder_configs"],
                ["web/.svelte-kit/tsconfig.json"],
            )
            self.assertEqual(report["coverage"]["web/src/b.ts"]["status"], "covered")
            index = scip_pb2.Index()
            index.ParseFromString(Path(run["index"]).read_bytes())
            self.assertIn("web/src/b.ts", {d.relative_path for d in index.documents})
            # The workspace itself is left as it was.
            self.assertFalse((root / "web" / ".svelte-kit").exists())
