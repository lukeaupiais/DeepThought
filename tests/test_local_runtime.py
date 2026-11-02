"""Local SDK behavior with Python egress/process guards and unchanged inputs."""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import unittest

FIXTURES = Path(__file__).parent / "fixtures"


class LocalRuntimeTests(unittest.TestCase):
    def test_sdk_neither_connects_spawns_nor_activates_model_configuration(self):
        script = r"""
import sys
def deny(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'subprocess.Popen', 'os.system', 'os.posix_spawn'}:
        raise RuntimeError('Forbidden runtime operation: '+event)
sys.addaudithook(deny)
from deepthought import GraphSnapshot, QueryRequest, analyze_repository, compose_graph, ingest_scip, query_graph
r=analyze_repository(sys.argv[1], repository_id='fixture', languages=('python', 'typescript'))
assert r.status == 'succeeded', r.diagnostics
assert r.definitions
from pathlib import Path
sources=[ingest_scip(Path(sys.argv[1])/'scip'/f'{language}.scip', repository_id=language) for language in ('python', 'typescript')]
graph=compose_graph(sources)
assert graph.references
assert GraphSnapshot.from_json(graph.to_json()) == graph
query=query_graph(graph, QueryRequest('definitions', 'python', limit=1))
assert query.matches and len(query.to_json().encode('utf-8')) + 1 <= 32768
for node in graph.lookup_definitions('helper'):
    graph.references_to(node.id)
    graph.dependencies(node.repository_id, node.definition.file)
for name in ['core', 'storage', 'deepthought_analysis', 'torch', 'sentence_transformers', 'bertopic', 'neo4j', 'deepthought_ai']:
    assert name not in sys.modules, name
"""
        environment = dict(
            os.environ,
            DEEPTHOUGHT_LLM_NAMING="true",
            OPENAI_API_KEY="unusable-test-marker",
            PYTHONDONTWRITEBYTECODE="1",
        )
        before = {
            p: hashlib.sha256(p.read_bytes()).digest()
            for p in FIXTURES.rglob("*")
            if p.is_file()
        }
        run = subprocess.run(
            [sys.executable, "-c", script, str(FIXTURES)],
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        after = {
            p: hashlib.sha256(p.read_bytes()).digest()
            for p in FIXTURES.rglob("*")
            if p.is_file()
        }
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
