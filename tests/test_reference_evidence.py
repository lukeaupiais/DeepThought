"""Real compiler evidence and adversarial scope checks, separate from agent scores."""

import asyncio
from hashlib import sha256
from copy import deepcopy
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from uuid import uuid4

from deepthought.analysis_tools import ScipAnalysisTool
from deepthought.typescript_reference_evidence import (
    ReferenceSourceValidator,
    PROFILE,
    digest,
)


class ReferenceFreshnessTests(unittest.TestCase):
    def test_truncated_export_candidates_cannot_be_treated_as_unique(self):
        from deepthought.graph_queries import _source_references

        response = {
            "items": [{"evidence": {"native_id": "one", "exports": []}}],
            "returned": 1,
            "truncated": True,
        }
        result = _source_references(response, {}, {"export_name": "shared"}, 1, 16000)
        self.assertEqual(result["source_verification"]["status"], "ambiguous")
        self.assertIsNone(result["next_cursor"])

    def test_source_configuration_scope_runtime_and_repository_must_match(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "source"
            shutil.copytree(
                Path(__file__).parent / "fixtures/scip_alias_reexport", root
            )
            node = base / "node"
            node.write_bytes(b"runtime fingerprint only, never executed")
            compiler = base / "compiler"
            (compiler / "lib").mkdir(parents=True)
            compiler_entry = compiler / "lib/typescript.js"
            compiler_entry.write_text("compiler fingerprint")
            validator = ReferenceSourceValidator(
                root, root / "index.scip", node, compiler, "owned"
            )
            before = validator.capture("owned")
            receipt, runtime = before
            files = ["a.ts", "b.ts", "c.ts"]
            scope = {
                "profile": PROFILE,
                "compiler_version": "6.0.3",
                "compiler_sha256": sha256(compiler_entry.read_bytes()).hexdigest(),
                "runtime_sha256": runtime,
                "index_sha256": receipt["index_sha256"],
                "source_receipt_sha256": digest(
                    {k: v for k, v in receipt.items() if k != "wall_seconds"}
                ),
                "files": files,
                "files_sha256": digest(files),
                "source_sha256": digest({f: receipt["files"][f] for f in files}),
            }
            metadata = {
                "tool_name": "scip",
                "custom_metrics": {"reference_verification": scope},
            }
            validator.validate(before, metadata, "owned")
            for name in ["a.ts", "tsconfig.json"]:
                path = root / name
                content = path.read_bytes()
                path.write_bytes(content + b"\n")
                with self.subTest(input=name), self.assertRaises(ValueError):
                    validator.validate(before, metadata, "owned")
                path.write_bytes(content)
            added = root / "dependency.ts"
            added.write_text("export const changed = true;")
            with self.assertRaises(ValueError):
                validator.validate(before, metadata, "owned")
            added.unlink()
            for key in [
                "profile",
                "runtime_sha256",
                "index_sha256",
                "files_sha256",
                "source_sha256",
            ]:
                damaged = deepcopy(metadata)
                damaged["custom_metrics"]["reference_verification"][key] = "changed"
                with self.subTest(field=key), self.assertRaises(ValueError):
                    validator.validate(before, damaged, "owned")
            with self.assertRaises(ValueError):
                validator.capture("another-repository")
            compiler_entry.write_text("changed compiler")
            with self.assertRaises(ValueError):
                validator.validate(before, metadata, "owned")


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_TYPESCRIPT_NODE")
    and os.environ.get("DEEPTHOUGHT_TYPESCRIPT_ROOT"),
    "Provision the pinned TypeScript compiler and Node for source-evidence controls",
)
class ReferenceEvidenceTests(unittest.TestCase):
    def test_compiler_evidence_closes_alias_gap_without_changing_original_facts(self):
        root = Path(__file__).parent / "fixtures/scip_alias_reexport"
        tool = ScipAnalysisTool(
            root,
            reference_node=os.environ["DEEPTHOUGHT_TYPESCRIPT_NODE"],
            reference_typescript=os.environ["DEEPTHOUGHT_TYPESCRIPT_ROOT"],
        )
        output = asyncio.run(tool.analyze("owned-alias-control"))
        from deepthought.storage.graph.writer import _prepare

        repeat = asyncio.run(tool.analyze("owned-alias-control"))
        self.assertEqual(
            output.metadata.custom_metrics["snapshot_id"],
            repeat.metadata.custom_metrics["snapshot_id"],
        )
        self.assertEqual(
            _prepare(
                output,
                "owned-alias-control",
                output.metadata.custom_metrics["snapshot_id"],
            )[1],
            _prepare(
                repeat,
                "owned-alias-control",
                repeat.metadata.custom_metrics["snapshot_id"],
            )[1],
        )
        proofs = [n.properties for n in output.nodes if "ReferenceEvidence" in n.labels]
        proof = next(
            p
            for p in proofs
            if p["native_id"].endswith("/alpha().") and "`a.ts`" in p["native_id"]
        )
        self.assertEqual(proof["verification_status"], "checked")
        self.assertEqual(len(proof["references"]), 8)
        self.assertEqual(proof["indexed_count"], 7)
        self.assertEqual(proof["missing_in_scip"], 1)
        self.assertEqual(proof["extra_in_scip"], 0)
        original = [n.properties for n in output.nodes if "Reference" in n.labels]
        unresolved = next(
            p
            for p in original
            if p["file_path"] == "c.ts" and p["start_line"] == 1 and p["column"] == 9
        )
        self.assertEqual(unresolved["resolution"], "unresolved")
        self.assertIn(
            "verified_references", output.metadata.custom_metrics["capabilities"]
        )


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_NEO4J_URI")
    and os.environ.get("DEEPTHOUGHT_TYPESCRIPT_NODE")
    and os.environ.get("DEEPTHOUGHT_TYPESCRIPT_ROOT"),
    "Provision isolated Neo4j and compiler controls",
)
class ReferenceEvidenceGraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_checked_pages_and_negative_source_guards(self):
        from deepthought.storage.graph import (
            Neo4jConnectionManager,
            WriteCoordinator,
            setup_schema,
        )
        from deepthought.graph_queries import GraphQueryService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            shutil.copytree(
                Path(__file__).parent / "fixtures/scip_alias_reexport", root
            )
            repo = "source-evidence-" + uuid4().hex
            node, compiler = (
                os.environ["DEEPTHOUGHT_TYPESCRIPT_NODE"],
                os.environ["DEEPTHOUGHT_TYPESCRIPT_ROOT"],
            )
            tool = ScipAnalysisTool(
                root, reference_node=node, reference_typescript=compiler
            )
            output = await tool.analyze(repo)
            snapshot = output.metadata.custom_metrics["snapshot_id"]
            native = next(
                n.properties["native_id"]
                for n in output.nodes
                if "ReferenceEvidence" in n.labels
                and n.properties["native_id"].endswith("/alpha().")
                and "`a.ts`" in n.properties["native_id"]
            )
            manager = Neo4jConnectionManager(
                os.environ["DEEPTHOUGHT_NEO4J_URI"],
                os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
                os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
            )
            driver = await manager.connect()
            try:
                await setup_schema(driver)
                stored = await WriteCoordinator(driver).write_tool_output(
                    "scip", output, repo
                )
                self.assertTrue(stored.success, stored.errors)
                events = []
                validator = ReferenceSourceValidator(
                    root, root / "index.scip", node, compiler, repo
                )
                api = GraphQueryService(
                    driver, reference_validator=validator, observer=events.append
                )
                page = await api.get_references(
                    native, repo, snapshot_id=snapshot, compact=True, limit=100
                )
                page_event = events[-1]
                self.assertEqual(page["returned"], 8)
                self.assertEqual(page["source_verification"]["status"], "verified")
                self.assertEqual(
                    page["source_verification"]["comparison"]["missing_in_scip"], 1
                )
                self.assertEqual(
                    page["source_verification"]["freshness"],
                    "checked_before_and_after_query",
                )
                self.assertEqual(
                    page["reference_coverage"][0]["source_binding"][
                        "workspace_freshness"
                    ],
                    "checked_before_and_after_query",
                )
                exported = await api.get_references(
                    None,
                    repo,
                    snapshot_id=snapshot,
                    compact=True,
                    export_name="outward",
                    export_file="b.ts",
                    limit=100,
                )
                self.assertEqual(exported["returned"], 8)
                self.assertEqual(exported["source_verification"]["status"], "verified")
                from deepthought.analysis_tools import TreeSitterTool

                ast_output = await TreeSitterTool(root).analyze(repo, {"typescript"})
                association = await WriteCoordinator(driver).write_outputs(
                    [ast_output, output], repo, snapshot_id="ast-control"
                )
                self.assertTrue(association.success, association.errors)
                context = await api.get_code_context(
                    "a.ts", repo, snapshot_id="ast-control", native_symbol=native
                )
                self.assertEqual(context["returned"], 2)
                self.assertTrue(
                    all(row["element"]["source_available"] for row in context["items"])
                )
                self.assertEqual(context["source_verification"]["status"], "verified")
                original_body = context["items"][0]["element"]
                async with driver.session() as session:
                    result = await session.run(
                        "MATCH (n {repo_url:$repo,snapshot_id:'ast-control',qualified_name:$name}) SET n.source_code='damaged'",
                        repo=repo,
                        name=original_body["qualified_name"],
                    )
                    await result.consume()
                with self.assertRaisesRegex(
                    ValueError, "context source integrity mismatch"
                ):
                    await api.get_code_context(
                        "a.ts", repo, snapshot_id="ast-control", native_symbol=native
                    )
                async with driver.session() as session:
                    result = await session.run(
                        "MATCH (n {repo_url:$repo,snapshot_id:'ast-control',qualified_name:$name}) SET n.source_code=$body",
                        repo=repo,
                        name=original_body["qualified_name"],
                        body=original_body["source_code"],
                    )
                    await result.consume()
                absent = await api.get_references(
                    None,
                    repo,
                    snapshot_id=snapshot,
                    compact=True,
                    export_name="alpha",
                    export_file="b.ts",
                )
                self.assertEqual(absent["returned"], 0)
                self.assertEqual(absent["source_verification"]["status"], "unavailable")
                from hashlib import sha256
                import json

                self.assertEqual(
                    page_event["response_sha256"],
                    sha256(
                        json.dumps(page, sort_keys=True, ensure_ascii=False).encode()
                    ).hexdigest(),
                )
                first = await api.get_references(
                    native, repo, snapshot_id=snapshot, compact=True, limit=3
                )
                second = await api.get_references(
                    native,
                    repo,
                    snapshot_id=snapshot,
                    compact=True,
                    limit=100,
                    after_reference_id=first["next_cursor"],
                )
                self.assertEqual(first["returned"] + second["returned"], 8)
                without_source = await GraphQueryService(driver).get_references(
                    native, repo, snapshot_id=snapshot, compact=True
                )
                self.assertEqual(
                    without_source["source_verification"]["status"], "unverified"
                )
                path = root / "a.ts"
                path.write_bytes(path.read_bytes() + b"\n")
                stale = await api.get_references(
                    native, repo, snapshot_id=snapshot, compact=True
                )
                self.assertEqual(stale["source_verification"]["status"], "unverified")
                stale_context = await api.get_code_context(
                    "a.ts", repo, snapshot_id="ast-control", native_symbol=native
                )
                self.assertEqual(
                    stale_context["source_verification"]["status"], "unverified"
                )
                self.assertEqual(stale_context["source_scope"], "stored_snapshot")
                async with driver.session() as session:
                    await session.run(
                        "MATCH (p:ReferenceEvidence {repo_url:$repo,native_id:$native}) SET p.references_sha256='damaged'",
                        repo=repo,
                        native=native,
                    )
                with self.assertRaisesRegex(ValueError, "integrity mismatch"):
                    await api.get_references(
                        native, repo, snapshot_id=snapshot, compact=True
                    )
            finally:
                async with driver.session() as session:
                    await session.run(
                        "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=repo
                    )
                await manager.close()
