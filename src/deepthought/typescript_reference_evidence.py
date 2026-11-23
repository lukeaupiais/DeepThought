"""Compiler-backed source evidence owned by the SCIP AnalysisTool.

No model calls, installation, network access or execution of project modules.
The query adapter validates current inputs against the stored evidence scope.
"""

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import os
import stat

from .models import is_relative_file, validate_location
from .scip_indexing import capture_inputs, validate_index

PROFILE = "typescript-symbol-references-v1"
SCRIPT = Path(__file__).with_suffix(".cjs")
LOCATION = ("file_path", "start_line", "column", "end_line", "end_column")


def digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def runtime_binding(node, typescript):
    node, typescript = (
        Path(node).resolve(strict=True),
        Path(typescript).resolve(strict=True),
    )
    return digest(
        {
            "node": sha256(node.read_bytes()).hexdigest(),
            "typescript": capture_inputs(typescript, typescript / "unused.scip"),
            "checker": sha256(SCRIPT.read_bytes()).hexdigest(),
        }
    )


def symbol_key(native, file):
    return (file + "::" + native) if native.startswith("local ") else native


def build_reference_evidence(result, receipt, workspace, node, typescript):
    root = Path(workspace).resolve(strict=True)
    files = sorted(c.file for c in result.coverage)
    runtime = runtime_binding(node, typescript)
    scope = {
        "profile": PROFILE,
        "compiler_version": "6.0.3",
        "runtime_sha256": runtime,
        "files": files,
        "files_sha256": digest(files),
        "source_sha256": digest({file: receipt["files"][file] for file in files}),
        "index_sha256": receipt["index_sha256"],
        "source_receipt_sha256": digest(
            {k: v for k, v in receipt.items() if k != "wall_seconds"}
        ),
        "compiler_sha256": sha256(
            (Path(typescript) / "lib/typescript.js").read_bytes()
        ).hexdigest(),
        "scope_definition": "Compiler-symbol identity and alias closure over selected files, excluding target declaration names",
    }
    targets = {}
    original = {}
    identities = {}
    for definition in result.definitions:
        key = symbol_key(definition.native_id, definition.file)
        identities[key] = (
            definition.native_id,
            definition.file if definition.native_id.startswith("local ") else "",
        )
        targets.setdefault(key, {"key": key, "definitions": []})["definitions"].append(
            {
                "file": definition.file,
                "line": definition.line,
                "column": definition.column,
                "end_line": definition.end_line,
                "end_column": definition.end_column,
                "kind": definition.kind,
            }
        )
    for reference in result.references:
        key = symbol_key(reference.native_id, reference.file)
        identities[key] = (
            reference.native_id,
            reference.file if reference.native_id.startswith("local ") else "",
        )
        original.setdefault(key, []).append(
            {
                "file_path": reference.file,
                "start_line": reference.line,
                "column": reference.column,
                "end_line": reference.end_line,
                "end_column": reference.end_column,
            }
        )
    request = {
        "workspace": str(root),
        "typescript": str(Path(typescript).resolve()),
        "files": files,
        "targets": list(targets.values()),
        "configuration_files": [
            file
            for file in receipt["files"]
            if file in {"package.json", "package-lock.json", "tsconfig.json"}
        ],
    }
    process = subprocess.run(
        [str(Path(node).resolve()), str(SCRIPT)],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if process.returncode:
        raise ValueError(
            "TypeScript reference evidence failed: " + process.stderr[-3000:]
        )
    output = json.loads(process.stdout)
    if output.get("compiler_version") != "6.0.3" or output.get("diagnostics") != 0:
        raise ValueError("Unqualified TypeScript reference evidence")
    if runtime_binding(node, typescript) != runtime:
        raise ValueError("Compiler inputs changed during source analysis")
    checked = {r["key"]: r for r in output["records"]}
    evidence = []
    for key in sorted(set(targets) | set(original)):
        record = checked.get(key, {"status": "unavailable", "references": []})
        references = (
            record["references"]
            if record["status"] == "checked"
            else original.get(key, [])
        )
        rows = []
        for reference in references:
            rows.append(
                {**reference, "reference_id": digest([PROFILE, key, reference])}
            )
        rows.sort(key=lambda r: r["reference_id"])
        actual = {tuple(r[k] for k in LOCATION) for r in references}
        indexed = {tuple(r[k] for k in LOCATION) for r in original.get(key, [])}
        evidence.append(
            {
                "symbol_key": key,
                "native_id": identities[key][0],
                "file_scope": identities[key][1],
                "verification_status": record["status"],
                "compiler_symbol_id": record.get("compiler_symbol_id"),
                "exports": record.get("exports", []),
                "export_names": sorted({e["name"] for e in record.get("exports", [])}),
                "export_keys": sorted(
                    {
                        json.dumps([e["file"], e["name"]], ensure_ascii=False)
                        for e in record.get("exports", [])
                    }
                ),
                "references": rows,
                "references_sha256": digest(rows),
                "indexed_count": len(indexed),
                "missing_in_scip": len(actual - indexed)
                if record["status"] == "checked"
                else None,
                "extra_in_scip": len(indexed - actual)
                if record["status"] == "checked"
                else None,
            }
        )
    scope["checked_symbols"] = sum(
        item["verification_status"] == "checked" for item in evidence
    )
    scope["unavailable_symbols"] = len(evidence) - scope["checked_symbols"]
    return scope, evidence


class ReferenceSourceValidator:
    """Explicit trusted workspace binding, never selected from graph-supplied paths."""

    def __init__(self, workspace, index, node, typescript, repository_id):
        self.workspace, self.index = Path(workspace), Path(index)
        self.node, self.typescript = Path(node), Path(typescript)
        self.repository_id = repository_id

    def capture(self, repository_id):
        if repository_id != self.repository_id:
            raise ValueError("Reference evidence repository binding differs")
        receipt = validate_index(self.workspace, self.index)
        return receipt, runtime_binding(self.node, self.typescript)

    def validate_context(self, before, items):
        """Compare stored body hashes and returned pages with bound source bytes."""
        from .consistency import _open_source

        files = {}
        root_fd = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for item in items:
                element = item["element"]
                if not element["source_available"]:
                    continue
                file = element["file_path"]
                if not is_relative_file(file) or file not in before[0]["files"]:
                    raise ValueError("Context source is outside the bound input set")
                if file not in files:
                    with os.fdopen(_open_source(root_fd, file), "rb") as source:
                        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                            raise ValueError("Context source must be regular")
                        content = source.read(2_000_001)
                    if (
                        len(content) > 2_000_000
                        or sha256(content).hexdigest() != before[0]["files"][file]
                    ):
                        raise ValueError(
                            "Context source changed during body verification"
                        )
                    files[file] = content.split(b"\n")
                lines = files[file]
                validate_location(
                    file,
                    element["source_start_line"],
                    element["source_start_column"],
                    element["source_end_line"],
                    element["source_end_column"],
                    "utf-8",
                )
                start, end = (
                    element["source_start_line"] - 1,
                    element["source_end_line"] - 1,
                )
                first, last = (
                    element["source_start_column"],
                    element["source_end_column"],
                )
                if (
                    end >= len(lines)
                    or first > len(lines[start])
                    or last > len(lines[end])
                ):
                    raise ValueError("Stored context source range exceeds its file")
                body = (
                    lines[start][first:last]
                    if start == end
                    else b"\n".join(
                        [
                            lines[start][first:],
                            *lines[start + 1 : end],
                            lines[end][:last],
                        ]
                    )
                ).decode("utf-8")
                code, offset = element["source_code"], element["source_offset"]
                truncated = offset + len(code) < len(body)
                if (
                    element["file_sha256"] != before[0]["files"][file]
                    or sha256(body.encode()).hexdigest() != element["source_sha256"]
                    or len(body) != element["source_total_characters"]
                    or body[offset : offset + len(code)] != code
                    or element["source_truncated"] != truncated
                    or element["source_next_offset"]
                    != (offset + len(code) if truncated else None)
                ):
                    raise ValueError("Stored context source integrity mismatch")
        finally:
            os.close(root_fd)

    def validate(self, before, metadata, repository_id):
        after = self.capture(repository_id)
        if before != after:
            raise ValueError("Source or compiler inputs changed during query")
        sources = metadata.get("custom_metrics", {}).get("tool_outputs", [metadata])
        source = next(item for item in sources if item["tool_name"] == "scip")
        scope = source["custom_metrics"]["reference_verification"]
        receipt, runtime = after
        files = scope["files"]
        if not all(
            is_relative_file(file) and file in receipt["files"] for file in files
        ):
            raise ValueError("Invalid reference evidence source scope")
        if (
            scope["profile"] != PROFILE
            or scope.get("compiler_version") != "6.0.3"
            or scope.get("compiler_sha256")
            != sha256((self.typescript / "lib/typescript.js").read_bytes()).hexdigest()
            or scope["runtime_sha256"] != runtime
            or scope["index_sha256"] != receipt["index_sha256"]
            or scope["source_receipt_sha256"]
            != digest({k: v for k, v in receipt.items() if k != "wall_seconds"})
            or scope["files_sha256"] != digest(files)
            or scope["source_sha256"]
            != digest({file: receipt["files"][file] for file in files})
        ):
            raise ValueError("Source evidence differs from selected inputs")
        return {
            k: v
            for k, v in scope.items()
            if k not in {"files", "preparation_wall_seconds"}
        }
