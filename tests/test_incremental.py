"""Incremental analysis: reusing unchanged files' facts must equal a full run."""

from dataclasses import replace
import importlib.util
from pathlib import Path
import random
import shutil
import tempfile
import unittest

from deepthought._version import __version__
from deepthought.engine import analyze_repository, reusable_files

READY = all(
    importlib.util.find_spec(name)
    for name in ("tree_sitter", "tree_sitter_python", "tree_sitter_typescript")
)
FIXTURES = Path(__file__).parent / "fixtures"

PYTHON_SNIPPETS = (
    "def added_{n}():\n    return helper({n})\n",
    "class Extra{n}:\n    def run(self):\n        return self.stop()\n\n    def stop(self):\n        pass\n",
    "from .models import Widget as W{n}\n",
    "def broken_{n}(:\n    pass\n",
    "import os.path as p{n}\n\n\ndef use_{n}():\n    return p{n}.join('a')\n",
)
TYPESCRIPT_SNIPPETS = (
    "export function added{n}(): number {{ return {n}; }}\n",
    "import {{ helper as h{n} }} from './helpers';\nexport const v{n} = () => h{n}();\n",
    "export class Box{n} {{ open() {{ this.close(); }} close() {{}} }}\n",
    "export function broken{n}( {{\n",
    "export * from './models';\n",
)


def analyze(root, **options):
    return analyze_repository(root, repository_id="repo", **options)


def mutate(root, rng, step):
    """Apply one random edit: change, add, delete, rename or corrupt a file."""
    files = sorted(p for p in root.rglob("*") if p.is_file())
    action = rng.choice(("append", "append", "add", "delete", "rename", "corrupt"))
    if action in {"append", "delete", "rename", "corrupt"} and not files:
        action = "add"
    if action == "add":
        suffix = rng.choice((".py", ".ts", ".tsx"))
        path = root / rng.choice(("", "pkg/", "src/")) / f"new_{step}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        pool = PYTHON_SNIPPETS if suffix == ".py" else TYPESCRIPT_SNIPPETS
        path.write_text(rng.choice(pool).format(n=step))
    elif action == "append":
        path = rng.choice(files)
        pool = PYTHON_SNIPPETS if path.suffix == ".py" else TYPESCRIPT_SNIPPETS
        with path.open("a") as handle:
            handle.write("\n" + rng.choice(pool).format(n=step))
    elif action == "delete":
        rng.choice(files).unlink()
    elif action == "rename":
        path = rng.choice(files)
        path.rename(path.with_name(f"moved_{step}{path.suffix}"))
    else:
        rng.choice(files).write_bytes(b"\xff\xfe invalid utf-8\n")
    return action


@unittest.skipUnless(READY, "Provision the python and typescript extras")
class IncrementalEqualsFullTests(unittest.TestCase):
    def workspace(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for name in ("python", "typescript"):
            shutil.copytree(FIXTURES / name, root / name)
        return root

    def test_unchanged_workspace_reuses_every_parsed_file(self):
        root = self.workspace()
        full = analyze(root)
        stats = {}
        again = analyze(root, previous=full, reuse=stats)
        self.assertEqual(again.to_json(), full.to_json())
        parsed = sum(c.status in {"analyzed", "partial"} for c in full.coverage)
        self.assertEqual(
            stats, {"reused": parsed, "parsed": 0, "previous_rejected": None}
        )
        self.assertGreater(parsed, 5)

    def test_random_edit_sequences_equal_full_analysis(self):
        # Each incremental result feeds the next, so errors cannot hide by
        # being corrected later; limits make some files skipped or failed.
        for seed in range(25):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                root = self.workspace()
                options = {"max_file_bytes": rng.choice((2_000_000, 400))}
                previous = analyze(root, **options)
                for step in range(6):
                    action = mutate(root, rng, step)
                    stats = {}
                    incremental = analyze(
                        root, previous=previous, reuse=stats, **options
                    )
                    full = analyze(root, **options)
                    self.assertEqual(
                        incremental.to_json(), full.to_json(), (step, action)
                    )
                    self.assertEqual(
                        stats["reused"] + stats["parsed"],
                        sum(c.status in {"analyzed", "partial"} for c in full.coverage),
                    )
                    previous = incremental

    def test_only_changed_files_are_parsed(self):
        root = self.workspace()
        previous = analyze(root)
        target = root / "python" / "models.py"
        target.write_text(target.read_text() + "\n\ndef extra():\n    pass\n")
        stats = {}
        analyze(root, previous=previous, reuse=stats)
        self.assertEqual(stats["parsed"], 1)

    def test_previous_analysis_is_rejected_when_identities_could_differ(self):
        root = self.workspace()
        full = analyze(root)
        versions = dict(full.analyzer_versions)
        cases = (
            (replace(full, repository_id="other"), "different_repository_id"),
            (
                replace(
                    full,
                    analyzer_versions=tuple(
                        sorted(
                            {
                                **versions,
                                "deepthought-analysis-engine": __version__ + "x",
                            }.items()
                        )
                    ),
                ),
                "different_engine_version",
            ),
            (
                replace(
                    full,
                    schema_version="2",
                    imports=(),
                    call_sites=(),
                    capabilities=("definitions",),
                ),
                "not_a_tree_sitter_schema_3_analysis",
            ),
        )
        for previous, reason in cases:
            with self.subTest(reason=reason):
                stats = {}
                result = analyze(root, previous=previous, reuse=stats)
                self.assertEqual(result.to_json(), full.to_json())
                self.assertEqual(stats["previous_rejected"], reason)
                self.assertEqual(stats["reused"], 0)
        with self.assertRaises(ValueError):
            reusable_files({"not": "a result"}, "repo")

    def test_a_changed_grammar_version_reparses_only_that_language(self):
        root = self.workspace()
        full = analyze(root)
        versions = dict(full.analyzer_versions)
        versions["tree-sitter-python"] = "0.0.0"
        previous = replace(full, analyzer_versions=tuple(sorted(versions.items())))
        stats = {}
        result = analyze(root, previous=previous, reuse=stats)
        self.assertEqual(result.to_json(), full.to_json())
        python = sum(
            c.language == "python" and c.status in {"analyzed", "partial"}
            for c in full.coverage
        )
        self.assertEqual(stats["parsed"], python)
        self.assertGreater(stats["reused"], 0)

    def test_the_engine_version_is_recorded(self):
        result = analyze(self.workspace())
        self.assertEqual(
            dict(result.analyzer_versions)["deepthought-analysis-engine"],
            __version__,
        )


def run_module(module, *arguments, env=None):
    import os
    import subprocess
    import sys

    import deepthought

    environment = dict(os.environ if env is None else env)
    package_parent = Path(deepthought.__file__).resolve().parent.parent
    if package_parent.name == "src":
        environment["PYTHONPATH"] = str(package_parent)
    else:
        environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-m", module, *arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


@unittest.skipUnless(READY, "Provision the python and typescript extras")
class IncrementalCommandTests(unittest.TestCase):
    def test_analyze_with_previous_prints_the_full_artifact(self):
        import json

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "work"
            shutil.copytree(FIXTURES / "python", root)
            scope = [str(root), "--repository-id", "repo", "--language", "python"]
            first = run_module("deepthought", "analyze", *scope)
            self.assertEqual(first.returncode, 0, first.stderr)
            earlier = Path(directory) / "earlier.json"
            earlier.write_text(first.stdout)
            (root / "main.py").write_text(
                (root / "main.py").read_text() + "\n\ndef extra():\n    pass\n"
            )
            full = run_module("deepthought", "analyze", *scope)
            again = run_module(
                "deepthought", "analyze", *scope, "--previous", str(earlier)
            )
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(again.stdout, full.stdout)
            reuse = json.loads(again.stderr)["reuse"]
            self.assertEqual(reuse["parsed"], 1)
            self.assertGreater(reuse["reused"], 0)


@unittest.skipUnless(
    READY
    and importlib.util.find_spec("neo4j")
    and __import__("os").environ.get("DEEPTHOUGHT_NEO4J_URI"),
    "Explicit isolated Neo4j settings and neo4j, python, typescript extras required",
)
class IncrementalIndexTests(unittest.TestCase):
    def test_incremental_index_publishes_the_same_snapshot_as_a_full_index(self):
        import asyncio
        import json
        from uuid import uuid4

        repo = "https://github.com/deepthought-incremental/" + uuid4().hex
        self.addCleanup(lambda: asyncio.run(self.cleanup(repo)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "work"
            shutil.copytree(FIXTURES / "python", root)
            artifact = Path(directory) / "analysis.json"
            scope = [str(root), "--repository-id", repo, "--language", "python"]

            def index(*extra):
                result = run_module("deepthought.graph_cli", "index", *scope, *extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            first = index("--write-artifact", str(artifact))
            self.assertNotIn("reuse", first)
            (root / "models.py").write_text("class Widget:\n    size = 1\n")
            full = index()
            incremental = index("--previous-artifact", str(artifact))
            self.assertEqual(incremental["snapshot_id"], full["snapshot_id"])
            self.assertTrue(incremental["stats"]["reused"])
            self.assertEqual(incremental["reuse"]["parsed"], 1)
            self.assertIsNone(incremental["reuse"]["previous_rejected"])
            self.assertEqual(
                json.loads(artifact.read_text())["snapshot_id"], first["snapshot_id"]
            )

    async def cleanup(self, repo):
        import os

        from neo4j import AsyncGraphDatabase

        driver = AsyncGraphDatabase.driver(
            os.environ["DEEPTHOUGHT_NEO4J_URI"],
            auth=(
                os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
                os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
            ),
        )
        try:
            async with driver.session(
                database=os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j")
            ) as session:
                result = await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=repo
                )
                await result.consume()
        finally:
            await driver.close()


if __name__ == "__main__":
    unittest.main()
