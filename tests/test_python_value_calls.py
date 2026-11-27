"""Python calls through a parameter link to the functions passed to it
(deepthought.python_value_calls)."""

import asyncio
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from deepthought import indexers
from deepthought.analysis_tools.tree_sitter import TreeSitterTool
from deepthought.python_parameter_calls import parameter_facts

SOURCE = """\
import functools


def double(x):
    return x * 2


def inc(x):
    return x + 1


def never(x):
    return x


def apply(f, x):
    return f(x)


def forward(g, x):
    return apply(g, x)


def by_keyword(x, *, then):
    return then(x)


def nested(f):
    def inner():
        return f(1)

    def shadow(f):
        return f(2)

    return inner() + shadow(never)


def rebound(f):
    f = f or never
    return f(1)


def deco(fn):
    @functools.wraps(fn)
    def wrapper(*args):
        return fn(*args)

    return wrapper


@deco
def decorated():
    return 1


class Runner:
    def __init__(self, step):
        self.result = step(1)

    def run(self, step):
        return step(2)

    @staticmethod
    def once(step):
        return step(3)

    @classmethod
    def build(cls, step):
        return step(4)


def main():
    apply(double, 1)
    forward(inc, 1)
    by_keyword(1, then=double)
    nested(inc)
    rebound(double)
    Runner(inc).run(double)
    Runner.once(inc)
    Runner.build(double)
    runner = Runner(inc)
    Runner.run(runner, inc)
    return decorated()
"""


def _calls(root):
    output = asyncio.run(TreeSitterTool(root).analyze("r", languages=["python"]))
    names = {
        n.properties["qualified_name"]: n.properties.get("name")
        or n.properties["qualified_name"]
        for n in output.nodes
    }
    found = sorted(
        (
            names[r.source_match["qualified_name"]],
            names[r.target_match["qualified_name"]],
            r.properties["line"],
        )
        for r in output.relationships
        if r.type == "CALLS"
        and r.properties["rule_id"] == "python.call-parameter-value"
    )
    for r in output.relationships:
        if r.properties.get("rule_id") == "python.call-parameter-value":
            assert r.properties["dispatch"] == "parameter"
            assert r.properties["evidence_type"] == "resolved"
    return found, output.metadata.custom_metrics["python_parameter_calls"]


class ParameterFactsTests(unittest.TestCase):
    def test_parameters_their_calls_and_arguments(self):
        import ast

        facts = parameter_facts(ast.parse(SOURCE))
        functions = {f["name"]: f for f in facts["functions"]}
        # Calls are known by where they start and end.
        self.assertEqual(
            functions["apply"]["parameters"]["f"],
            {"called": [[17, 11, 17, 15]], "passed": []},
        )
        self.assertEqual(
            functions["forward"]["parameters"]["g"],
            {"called": [], "passed": [[21, 11, 21, 22]]},
        )
        # A nested function's call of the name counts; one that binds the
        # name again does not.
        self.assertEqual(
            functions["nested"]["parameters"]["f"]["called"], [[30, 15, 30, 19]]
        )
        self.assertNotIn("f", functions["rebound"]["parameters"])
        self.assertTrue(functions["once"]["staticmethod"])
        self.assertTrue(functions["build"]["classmethod"])
        self.assertIn(
            {"decorator": [51, 1, "deco"], "line": 52, "name": "decorated"},
            facts["decorators"],
        )
        (call,) = [c for c in facts["calls"] if c["keywords"]]
        self.assertEqual(call["keywords"], {"then": [75, 23, "double"]})
        # A chain's calls start together and end apart.
        self.assertEqual(
            [c["span"] for c in facts["calls"] if c["span"][0] == 78],
            [[78, 4, 78, 15], [78, 4, 78, 27]],
        )


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-python"])
    and importlib.util.find_spec("google.protobuf"),
    "Install scip-python and the scip extra",
)
class ParameterCallTests(unittest.TestCase):
    def test_calls_through_parameters_link_to_the_functions_passed(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "pyproject.toml").write_text('[project]\nname = "w"\n')
            (root / "app.py").write_text(SOURCE)
            found, metric = _calls(root)
        self.assertEqual(
            found,
            [
                # Runner(inc), Runner(inc).run(double), Runner.run(runner, inc)
                ("Runner.__init__", "inc", 58),
                ("Runner.build", "double", 69),
                ("Runner.once", "inc", 65),
                ("Runner.run", "double", 61),
                ("Runner.run", "inc", 61),
                ("apply", "double", 17),
                # forward(inc) passes its g on to apply.
                ("apply", "inc", 17),
                ("by_keyword", "double", 25),
                ("deco.wrapper", "decorated", 46),
                ("nested.inner", "inc", 30),
                ("nested.shadow", "never", 33),
            ],
        )
        # rebound(double) links nothing: its body rebinds f.
        self.assertEqual(metric, {"sites": 9, "edges": 11})


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_NEO4J_URI")
    and os.environ.get("DEEPTHOUGHT_NEO4J_PASSWORD")
    and indexers.locate(indexers.INDEXERS["scip-python"])
    and importlib.util.find_spec("google.protobuf"),
    "Set DEEPTHOUGHT_NEO4J_URI and install scip-python",
)
class CallerQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_callers_include_calls_through_parameters_and_values(self):
        from deepthought.graph_queries import GraphQueryService
        from deepthought.storage.graph import (
            Neo4jConnectionManager,
            WriteCoordinator,
            setup_schema,
        )

        repo = "https://github.com/deepthought-integration/" + uuid4().hex
        manager = Neo4jConnectionManager(
            os.environ["DEEPTHOUGHT_NEO4J_URI"],
            os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
            os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
            database=os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j"),
        )
        driver = await manager.connect()
        try:
            await setup_schema(driver, database=manager.database)
            with tempfile.TemporaryDirectory() as scratch:
                root = Path(scratch)
                (root / "pyproject.toml").write_text('[project]\nname = "w"\n')
                (root / "app.py").write_text(SOURCE)
                output = await TreeSitterTool(root).analyze(repo, languages=["python"])
            written = await WriteCoordinator(
                driver, database=manager.database
            ).write_tool_output("tree-sitter", output, repo)
            self.assertTrue(written.success, written.errors)
            double = next(
                n.properties["qualified_name"]
                for n in output.nodes
                if n.properties.get("name") == "double"
            )
            callers = await GraphQueryService(
                driver, manager.database
            ).get_function_callers(double, repo, snapshot_id=written.snapshot_id)
            found = {
                (
                    item["element"]["name"],
                    item["relationship"]["type"],
                    item["relationship"]["properties"]["rule_id"],
                )
                for item in callers["items"]
            }
            # apply runs double through its parameter; main passes it.
            self.assertIn(("apply", "CALLS", "python.call-parameter-value"), found)
            self.assertIn(
                ("main", "REFERENCES_FUNCTION", "scip.use-function-value"), found
            )
            self.assertIn("REFERENCES_FUNCTION", callers["semantics"])
        finally:
            async with driver.session(database=manager.database) as session:
                result = await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=repo
                )
                await result.consume()
            await manager.close()


if __name__ == "__main__":
    unittest.main()
