import importlib.util
import json
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
