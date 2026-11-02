"""Real DB conformance and falsification controls, not agent-performance results."""

import asyncio
from copy import deepcopy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deepthought.evaluation import _digest, compare, finalize_record, prepare
from deepthought.evaluation.__main__ import main
from deepthought.evaluation.neo4j import QueryRecorder, prepare_neo4j

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "evals/scenarios/tree_sitter/declarations.json"
SOURCE = ROOT / "evals/fixtures/tree_sitter_declarations"
DB = bool(os.environ.get("DEEPTHOUGHT_NEO4J_URI")) and all(
    importlib.util.find_spec(module) is not None
    for module in ("neo4j", "tree_sitter_python", "tree_sitter_typescript")
)


class InterfaceDefaultTests(unittest.TestCase):
    def test_default_cli_never_silently_falls_back_to_raw_context(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
        ):
            with self.assertRaises(SystemExit) as error:
                main(
                    ["prepare", str(PACK), str(SOURCE), str(Path(directory) / "output")]
                )
            self.assertEqual(error.exception.code, 2)
            self.assertFalse((Path(directory) / "output").exists())
        for descriptor in ({}, []):
            with self.assertRaisesRegex(ValueError, "Tool access requires"):
                prepare_neo4j(PACK, SOURCE, settings={}, tool_access=descriptor)

    def test_recorder_rejects_reusing_a_previous_session_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queries.jsonl"
            QueryRecorder("preparation", "run", path)
            with self.assertRaises(FileExistsError):
                QueryRecorder("preparation", "another-run", path)


@unittest.skipUnless(DB, "Explicit isolated Neo4j plus grammars required")
class Neo4jEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.prepared = prepare_neo4j(
            PACK,
            SOURCE,
            tool_access={"command": "graph", "help": "Query the actual Neo4j graph"},
        )
        self.graph = self.prepared["neo4j"]
        self.answer = {
            "claims": self.prepared["scenario"]["expected"],
            "relationships": [],
        }
        self.recorder = QueryRecorder(self.prepared["input_id"], "treatment-run")
        self.response = asyncio.run(self.query())

    async def query(self):
        from deepthought.graph_cli import connection_settings
        from deepthought.graph_queries import GraphQueryService
        from deepthought.storage.graph import Neo4jConnectionManager

        manager = Neo4jConnectionManager(**connection_settings())
        async with manager:
            service = GraphQueryService(
                manager.driver, database=manager.database, observer=self.recorder
            )
            return await service.search_elements(
                "",
                self.graph["repo_url"],
                snapshot_id=self.graph["snapshot_id"],
                limit=100,
            )

    def tearDown(self):
        asyncio.run(self.clean())

    async def clean(self):
        from deepthought.graph_cli import connection_settings
        from deepthought.storage.graph import Neo4jConnectionManager

        manager = Neo4jConnectionManager(**connection_settings())
        async with manager:
            async with manager.driver.session(database=manager.database) as session:
                result = await session.run(
                    "MATCH (n) WHERE n.repo_url=$repo DETACH DELETE n",
                    repo=self.graph["repo_url"],
                )
                await result.consume()

    def record(self, arm, events=None, run_id=None):
        return finalize_record(
            self.prepared,
            arm,
            self.answer,
            kind="agent",
            usage={
                "input_tokens": 100 if arm == "treatment" else 200,
                "output_tokens": 10,
            },
            execution={"status": "completed", "wall_seconds": 1},
            agent={"model": "control-only", "settings": {}},
            run_id=run_id or arm + "-run",
            graph_queries=events,
        )

    def test_real_import_readback_and_observed_query_allow_integration(self):
        self.assertEqual(self.graph["nodes"], 18)
        self.assertEqual(self.graph["relationships"], 17)
        self.assertNotIn("tool_output", self.prepared["contexts"]["treatment"])
        self.assertEqual(
            self.prepared["contexts"]["treatment"]["tool_access"]["backend"], "neo4j"
        )
        self.assertEqual(self.response["returned"], 15)
        event = self.recorder.events[0]
        self.assertEqual(event["content_hash"], self.graph["content_hash"])
        self.assertEqual(event["response_sha256"], _digest(self.response))
        result = compare(
            self.record("baseline"), self.record("treatment", self.recorder.events)
        )
        self.assertTrue(result["analysis_tool_evaluation_accepted"])
        self.assertEqual(result["graph_interface"]["status"], "verified_neo4j_access")
        self.assertFalse(result["benefit_demonstrated"])  # These are harness controls.

    def test_correct_answers_without_graph_calls_fail_closed_but_keep_costs(self):
        result = compare(self.record("baseline"), self.record("treatment", []))
        self.assertEqual(result["status"], "graph_not_used_or_unverified")
        self.assertFalse(result["analysis_tool_evaluation_accepted"])
        self.assertIsNone(result["token_reduction"])
        self.assertEqual(result["known_reported_tokens"], 320)

    def test_wrong_graph_and_replayed_run_receipts_are_ineligible(self):
        for field, value in [
            ("database", "another-db"),
            ("snapshot_id", "wrong"),
            ("backend", "json"),
            ("run_id", "another-run"),
            ("input_id", "another-preparation"),
        ]:
            with self.subTest(field=field):
                event = deepcopy(self.recorder.events[0])
                event[field] = value
                event.pop("event_id")
                event["event_id"] = _digest(event)
                result = compare(
                    self.record("baseline"), self.record("treatment", [event])
                )
                self.assertFalse(result["analysis_tool_evaluation_accepted"])
                self.assertIsNone(result["token_reduction"])
        event = self.recorder.events[0]
        result = compare(
            self.record("baseline"), self.record("treatment", [event, event])
        )
        self.assertEqual(result["status"], "replayed_graph_evidence")

    def test_baseline_without_fresh_session_identity_is_ineligible(self):
        baseline = self.record("baseline")
        baseline["run_id"] = None
        baseline.pop("record_id")
        baseline["record_id"] = _digest(baseline)
        result = compare(baseline, self.record("treatment", self.recorder.events))
        self.assertEqual(result["status"], "unverified_baseline_run")
        self.assertFalse(result["analysis_tool_evaluation_accepted"])
        self.assertIsNone(result["token_reduction"])

    def test_corrupted_receipt_is_rejected(self):
        events = deepcopy(self.recorder.events)
        events[0]["returned"] = 999
        with self.assertRaisesRegex(ValueError, "receipt identity"):
            self.record("treatment", events)

    def test_only_recording_a_descriptor_cannot_certify_neo4j(self):
        prepared = prepare(
            PACK,
            SOURCE,
            tool_access={
                "command": "anything",
                "help": "Claims to be Neo4j",
                "backend": "neo4j",
            },
        )

        def record(arm):
            return finalize_record(
                prepared,
                arm,
                self.answer,
                kind="agent",
                usage={"input_tokens": 100, "output_tokens": 1},
                execution={"status": "completed", "wall_seconds": 1},
                agent={"model": "control-only", "settings": {}},
            )

        result = compare(record("baseline"), record("treatment"))
        self.assertEqual(result["status"], "unverified_graph_interface")
        self.assertFalse(result["analysis_tool_evaluation_accepted"])

    def test_default_cli_prepares_real_neo4j_and_records_its_audit_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared_dir = root / "prepared"
            main(["prepare", str(PACK), str(SOURCE), str(prepared_dir)])
            prepared = json.loads((prepared_dir / "prepared.private.json").read_text())
            self.assertEqual(prepared["provenance"]["evaluation_interface"], "neo4j")
            actor = json.loads((prepared_dir / "treatment.actor.json").read_text())
            self.assertNotIn("tool_output", actor)
            answer = root / "answer.json"
            answer.write_text(json.dumps(self.answer))
            recorder = QueryRecorder(
                prepared["input_id"], "cli-run", root / "audit.jsonl"
            )
            previous = self.recorder
            self.recorder = recorder
            try:
                asyncio.run(self.query())
            finally:
                self.recorder = previous
            main(
                [
                    "record",
                    str(prepared_dir / "prepared.private.json"),
                    "treatment",
                    str(answer),
                    str(root / "record.json"),
                    "--run-id",
                    "cli-run",
                    "--graph-queries",
                    str(root / "audit.jsonl"),
                ]
            )
            record = json.loads((root / "record.json").read_text())
            self.assertEqual(record["graph_queries"][0]["run_id"], "cli-run")

    def test_real_cli_and_mcp_emit_preparation_bound_query_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "queries.jsonl"
            log.touch()
            env = dict(os.environ)
            env.update(
                DEEPTHOUGHT_EVAL_INPUT_ID=self.prepared["input_id"],
                DEEPTHOUGHT_EVAL_RUN_ID="transport-run",
                DEEPTHOUGHT_EVAL_QUERY_LOG=str(log),
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "deepthought.graph_cli",
                    "search",
                    "--repository-id",
                    self.graph["repo_url"],
                    "--snapshot",
                    self.graph["snapshot_id"],
                    "--query",
                    "",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["returned"], 15)
            if importlib.util.find_spec("mcp") is not None:

                async def mcp_read():
                    from mcp import ClientSession, StdioServerParameters
                    from mcp.client.stdio import stdio_client

                    parameters = StdioServerParameters(
                        command=sys.executable,
                        args=["-m", "deepthought.graph_mcp"],
                        env=env,
                    )
                    async with stdio_client(parameters) as streams:
                        async with ClientSession(*streams) as session:
                            await session.initialize()
                            response = await session.call_tool(
                                "searchCodeElements",
                                {
                                    "query": "",
                                    "repository_id": self.graph["repo_url"],
                                    "snapshot_id": self.graph["snapshot_id"],
                                    "max_elements": 20,
                                },
                            )
                            self.assertFalse(response.isError)

                asyncio.run(asyncio.wait_for(mcp_read(), timeout=30))
            events = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertGreaterEqual(len(events), 1)
            for event in events:
                self.assertEqual(event["input_id"], self.prepared["input_id"])
                self.assertEqual(event["run_id"], "transport-run")
                self.assertEqual(event["content_hash"], self.graph["content_hash"])
                self.assertEqual(
                    event["interface"], "deepthought.graph_queries.GraphQueryService"
                )

    def test_contaminated_or_reused_baseline_is_not_accepted(self):
        event = deepcopy(self.recorder.events[0])
        event["run_id"] = "baseline-run"
        event.pop("event_id")
        event["event_id"] = _digest(event)
        result = compare(
            self.record("baseline", [event]),
            self.record("treatment", self.recorder.events),
        )
        self.assertEqual(result["status"], "invalid_baseline_graph_access")
        self.assertIsNone(result["token_reduction"])
        result = compare(
            self.record("baseline", run_id="treatment-run"),
            self.record("treatment", self.recorder.events),
        )
        self.assertFalse(result["analysis_tool_evaluation_accepted"])

    def test_foreign_graph_is_rejected_before_import(self):
        async def foreign(create):
            from deepthought.graph_cli import connection_settings
            from deepthought.storage.graph import Neo4jConnectionManager

            async with Neo4jConnectionManager(**connection_settings()) as manager:
                async with manager.driver.session(database=manager.database) as session:
                    query = (
                        "CREATE (:ForeignEvaluationControl {nonce:'neo4j-profile-test'})"
                        if create
                        else "MATCH (n:ForeignEvaluationControl {nonce:'neo4j-profile-test'}) DETACH DELETE n"
                    )
                    result = await session.run(query)
                    await result.consume()

        asyncio.run(foreign(True))
        try:
            with self.assertRaisesRegex(ValueError, "dedicated repository/snapshot"):
                prepare_neo4j(PACK, SOURCE)
        finally:
            asyncio.run(foreign(False))

    def test_readback_rejects_actual_database_corruption(self):
        async def corrupt():
            from deepthought.graph_cli import connection_settings
            from deepthought.storage.graph import Neo4jConnectionManager

            async with Neo4jConnectionManager(**connection_settings()) as manager:
                async with manager.driver.session(database=manager.database) as session:
                    result = await session.run(
                        'MATCH (n:Symbol {repo_url:$repo}) SET n.name="corrupted"',
                        repo=self.graph["repo_url"],
                    )
                    await result.consume()

        asyncio.run(corrupt())
        with self.assertRaisesRegex(ValueError, "node readback differs"):
            prepare_neo4j(PACK, SOURCE)


if __name__ == "__main__":
    unittest.main()
