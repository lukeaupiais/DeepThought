"""SCIP reference pilot: frozen gold, grader controls and condition interfaces."""

import asyncio
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from check_scip_eval import check  # noqa: E402
from prepare_scip_eval import SCIP_TOOLS, TREE_TOOLS, reference_tool_access  # noqa: E402
from scip_eval_grader import score, validate_expected  # noqa: E402

SCENARIOS = sorted((ROOT / "evals/scenarios/scip/references").glob("*.json"))
FIXTURE = ROOT / "tests/fixtures/scip_alias_reexport"
HAVE = lambda *modules: all(importlib.util.find_spec(m) for m in modules)  # noqa: E731


def oracle_for(scenario):
    """A source oracle that agrees with the frozen gold."""
    target = scenario["expected"][0]["target"]
    return {
        "targets": {
            target: {
                "references": [
                    {
                        **{k: v for k, v in row.items() if k != "target"},
                        "source_text": target,
                    }
                    for row in scenario["expected"]
                ],
                "definitions": [
                    {
                        "file": scenario["expected"][0]["file"],
                        "line": 1,
                        "column": 0,
                        "end_line": 1,
                        "end_column": len(target),
                    }
                ],
            }
        },
        "source_sha256": dict(scenario["source"]["files"]),
    }


class FrozenScenarioTests(unittest.TestCase):
    def test_gold_is_valid_and_every_falsification_control_is_rejected(self):
        self.assertEqual(len(SCENARIOS), 2)
        for path in SCENARIOS:
            scenario = json.loads(path.read_text())
            with self.subTest(path.name):
                validate_expected(scenario)
                self.assertEqual(scenario["grader"], "scip-reference-locations-v1")
                result = check(scenario, oracle_for(scenario))
                self.assertTrue(all(result["controls_rejected"].values()))
                self.assertEqual(result["expected"], len(scenario["expected"]))

    def test_check_rejects_an_oracle_that_disagrees_with_the_gold(self):
        scenario = json.loads(SCENARIOS[0].read_text())
        moved = oracle_for(scenario)
        target = next(iter(moved["targets"].values()))
        target["references"][0]["line"] += 1
        target["references"][0]["end_line"] += 1
        with self.assertRaisesRegex(ValueError, "differs from frozen"):
            check(scenario, moved)
        edited = oracle_for(scenario)
        name = next(iter(edited["source_sha256"]))
        edited["source_sha256"][name] = sha256(b"other").hexdigest()
        with self.assertRaisesRegex(ValueError, "hashes differ"):
            check(scenario, edited)

    def test_score_counts_partial_answers(self):
        scenario = json.loads(SCENARIOS[0].read_text())
        half = deepcopy(scenario["expected"][: len(scenario["expected"]) // 2])
        result = score(scenario, {"references": half, "limitations": ["partial"]})
        self.assertFalse(result["quality_pass"])
        self.assertEqual(result["correct_count"], len(half))
        self.assertEqual(result["precision"], 1.0)
        self.assertEqual(len(result["missing"]), len(scenario["expected"]) - len(half))
        self.assertTrue(score(scenario, {"references": []})["recall"] == 0)
        self.assertFalse(
            score(scenario, {"references": [], "notes": 1})["quality_pass"]
        )

    def test_gold_that_escapes_the_selected_source_is_rejected(self):
        scenario = json.loads(SCENARIOS[0].read_text())
        scenario["expected"][0]["file"] = "outside.ts"
        with self.assertRaisesRegex(ValueError, "escapes selected source"):
            validate_expected(scenario)
        scenario = json.loads(SCENARIOS[0].read_text())
        scenario["expected"].append(deepcopy(scenario["expected"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            validate_expected(scenario)


class ConditionInterfaceTests(unittest.TestCase):
    def test_conditions_differ_only_by_the_scip_capabilities(self):
        tree, scip = reference_tool_access("tree"), reference_tool_access("scip")
        self.assertEqual(tree["tools"], list(TREE_TOOLS))
        self.assertEqual(scip["tools"], list(SCIP_TOOLS))
        self.assertEqual(
            set(scip["tools"]) - set(tree["tools"]),
            {"findReferences", "getFileDependencies"},
        )
        for access in (tree, scip):
            # The help text names no scenario target.
            self.assertNotIn("getNamedType", access["help"])
            self.assertNotIn("typeFromAST", access["help"])
            for name in access["tools"]:
                if name != "queryGraph":
                    self.assertIn(name, access["help"])

    @unittest.skipUnless(HAVE("mcp"), "Provision the mcp extra")
    def test_every_listed_tool_exists_in_the_served_mcp(self):
        from deepthought.graph_mcp import create_server

        with patch.dict(os.environ, {"DEEPTHOUGHT_ALLOW_DATABASE_SCOPE_CYPHER": "1"}):
            server = create_server()
        served = {tool.name for tool in asyncio.run(server.list_tools())}
        self.assertLessEqual(set(SCIP_TOOLS), served)


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_NEO4J_URI")
    and HAVE("neo4j", "tree_sitter_typescript", "google.protobuf"),
    "Provision isolated Neo4j and the typescript and scip extras",
)
class CombinedPreparationTests(unittest.TestCase):
    def scenario(self):
        files = {
            name: sha256((FIXTURE / name).read_bytes()).hexdigest()
            for name in ("a.ts", "b.ts", "c.ts")
        }
        return {
            "schema_version": 1,
            "id": "scip-eval-combined-control",
            "tool": "tree-sitter",
            "grader": "scip-reference-locations-v1",
            "task": "Inventory references to alpha.",
            "languages": ["typescript"],
            "source": {"kind": "synthetic", "files": files},
            "expected": [
                {
                    "target": "alpha",
                    "file": "b.ts",
                    "line": 1,
                    "column": 9,
                    "end_line": 1,
                    "end_column": 14,
                }
            ],
        }

    def tearDown(self):
        self.clean()

    def clean(self):
        from deepthought.graph_cli import connection_settings
        from deepthought.storage.graph import Neo4jConnectionManager

        async def clean():
            manager = Neo4jConnectionManager(**connection_settings())
            async with manager:
                async with manager.driver.session(database=manager.database) as s:
                    await (
                        await s.run(
                            "MATCH (n {repo_url:$repo}) DETACH DELETE n",
                            repo="synthetic://scip-eval-combined-control",
                        )
                    ).consume()

        asyncio.run(clean())

    def test_scip_condition_stores_both_tools_in_one_verified_revision(self):
        from deepthought.evaluation.neo4j import prepare_neo4j

        prepared = {}
        for condition in ("tree", "scip"):
            # Each condition needs its own dedicated database.
            self.clean()
            prepared[condition] = prepare_neo4j(
                self.scenario(),
                FIXTURE,
                tool_access=reference_tool_access(condition),
                additional_tools=("scip",) if condition == "scip" else (),
            )
        tree, scip = prepared["tree"], prepared["scip"]
        self.assertTrue(tree["neo4j"]["verified"] and scip["neo4j"]["verified"])
        self.assertEqual(tree["contexts"]["baseline"], scip["contexts"]["baseline"])
        self.assertEqual(
            [t["name"] for t in scip["provenance"]["contributing_tools"]],
            ["tree-sitter", "scip"],
        )
        self.assertNotIn("contributing_tools", tree["provenance"])
        self.assertGreater(scip["neo4j"]["nodes"], tree["neo4j"]["nodes"])
        metrics = scip["private_tool_output"]["metadata"]["custom_metrics"]
        self.assertEqual(metrics["declaration_binding"]["bound"], 3)
        with self.assertRaisesRegex(ValueError, "once"):
            prepare_neo4j(
                self.scenario(),
                FIXTURE,
                tool_access=reference_tool_access("scip"),
                additional_tools=("tree-sitter",),
            )


if __name__ == "__main__":
    unittest.main()
