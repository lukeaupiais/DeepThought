"""Historical query facts never assert current workspace freshness."""

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from deepthought import (
    QueryRequest,
    analyze_repository,
    compose_graph,
    ingest_scip,
    query_graph,
)
from deepthought._scip import scip_pb2 as wire


class FreshnessTests(unittest.TestCase):
    def query(self, source):
        return query_graph(
            compose_graph([source]), QueryRequest("definitions", "workspace")
        )

    def test_reanalysis_tracks_edits_renames_and_deletions_without_rewriting_history(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "sample.py"
            path.write_text("def sample():\n    return 1\n")
            first = analyze_repository(
                root, repository_id="workspace", languages=("python",)
            )
            historical = self.query(first)
            self.assertEqual(historical.workspace_freshness, "unverified")
            path.write_text("def sample():\n    return 2\n")
            edited = analyze_repository(
                root, repository_id="workspace", languages=("python",)
            )
            self.assertNotEqual(first.snapshot_id, edited.snapshot_id)
            self.assertEqual(first.definitions[0].id, edited.definitions[0].id)
            path.rename(root / "renamed.py")
            renamed = analyze_repository(
                root, repository_id="workspace", languages=("python",)
            )
            self.assertNotEqual(edited.snapshot_id, renamed.snapshot_id)
            self.assertNotEqual(edited.definitions[0].id, renamed.definitions[0].id)
            self.assertEqual(renamed.definitions[0].file, "renamed.py")
            (root / "renamed.py").unlink()
            deleted = analyze_repository(
                root, repository_id="workspace", languages=("python",)
            )
            self.assertNotEqual(renamed.snapshot_id, deleted.snapshot_id)
            self.assertFalse(deleted.definitions)
            self.assertEqual(self.query(first), historical)
            for source in [first, edited, renamed, deleted]:
                result = self.query(source)
                self.assertEqual(result.to_dict()["workspace_freshness"], "unverified")
                self.assertEqual(result.snapshot_id, source.snapshot_id)
                self.assertEqual(result.status, "succeeded")

    def test_supplied_scip_remains_unverified_even_with_embedded_text(self):
        for embedded in [False, True]:
            with (
                self.subTest(embedded=embedded),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                path = root / "sample.py"
                text = "def sample(): pass\n"
                path.write_text(text)
                index = wire.Index()
                index.metadata.tool_info.name = "owned-fixture"
                doc = index.documents.add(
                    relative_path="sample.py",
                    position_encoding=wire.UTF8CodeUnitOffsetFromLineStart,
                )
                if embedded:
                    doc.text = text
                doc.symbols.add(
                    symbol="local 1",
                    display_name="sample",
                    kind=wire.SymbolInformation.Function,
                )
                doc.occurrences.add(
                    symbol="local 1", symbol_roles=wire.Definition, range=[0, 4, 10]
                )
                artifact = root / "index.scip"
                artifact.write_bytes(index.SerializeToString())
                first = ingest_scip(artifact, repository_id="workspace")
                historical = self.query(first)
                self.assertEqual(first.status, "succeeded")
                self.assertNotEqual(
                    first.coverage[0].digest, sha256(path.read_bytes()).hexdigest()
                )
                self.assertEqual(historical.workspace_freshness, "unverified")
                path.write_text("def changed(): pass\n")
                for step in ["edited", "renamed", "deleted"]:
                    if step == "renamed":
                        path.rename(root / "renamed.py")
                    elif step == "deleted":
                        (root / "renamed.py").unlink()
                    current = ingest_scip(artifact, repository_id="workspace")
                    self.assertEqual(current, first)
                    self.assertEqual(self.query(current), historical)
                    self.assertEqual(
                        self.query(current).to_dict()["workspace_freshness"],
                        "unverified",
                    )

    def test_pure_queries_do_not_read_a_workspace_to_claim_freshness(self):
        script = r"""
import sys
from deepthought import AnalysisResult, QueryRequest, compose_graph, query_graph
from dataclasses import replace
source=AnalysisResult('workspace','s'*64,'succeeded',(),(),(),'fixture',())
# Resolve imports before auditing just the public query interface.
graph=compose_graph([source])
query_graph(graph,QueryRequest('definitions','workspace')).to_json()
def deny(event,args):
    if event in {'open','os.listdir','os.scandir','subprocess.Popen','socket.connect'}:
        raise AssertionError('Query attempted I/O: '+event)
sys.addaudithook(deny)
for status in ['succeeded','partial','failed','cancelled']:
    graph=compose_graph([replace(source,status=status)])
    for request in [QueryRequest('definitions','workspace'),QueryRequest('references','workspace',symbol='sample'),QueryRequest('dependencies','workspace',file='sample.py')]:
        result=query_graph(graph,request)
        assert result.workspace_freshness=='unverified'
        assert result.to_dict()['workspace_freshness']=='unverified'
        assert result.status==(status if request.operation=='definitions' else 'unavailable')
"""
        run = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_installed_cli_reports_historical_facts_and_unverified_freshness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.py"
            source.write_text("def sample(): pass\n")
            captured = analyze_repository(
                root, repository_id="workspace", languages=("python",)
            )
            graph = compose_graph([captured])
            artifact = root / "graph.json"
            artifact.write_text(graph.to_json())
            source.unlink()
            run = subprocess.run(
                [
                    str(Path(sys.executable).with_name("deepthought")),
                    "query",
                    str(artifact),
                    "--repository-id",
                    "workspace",
                    "--operation",
                    "definitions",
                    "--max-output-bytes",
                    "2048",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(run.stdout)
            self.assertEqual(result["workspace_freshness"], "unverified")
            self.assertEqual(result["snapshot_id"], captured.snapshot_id)
            self.assertEqual(result["matches"][0]["definition"]["symbol"], "sample")
            self.assertEqual(result, self.query(captured).to_dict())
            self.assertLessEqual(len(run.stdout.encode("utf-8")), 2048)
