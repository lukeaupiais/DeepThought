"""Explicit, provisioned TypeScript indexing and source-binding receipts.

No installation or network operations. Before/after hashes detect observed input
changes, without claiming filesystem snapshot isolation or indexer soundness.
"""

from hashlib import sha256
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time

from .models import is_relative_file


def _digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _hash(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("SCIP input must be a regular non-symlink file")
    return sha256(path.read_bytes()).hexdigest()


def capture_inputs(workspace, index_path):
    """Capture TS/JS/config inputs and installed dependency files, including membership."""
    root = Path(workspace).resolve(strict=True)
    excluded = {
        Path(index_path).absolute(),
        Path(str(index_path) + ".sources.json").absolute(),
    }
    files = {}
    for directory, names, filenames in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if name not in {".git", ".bin"})
        for name in names:
            if (Path(directory) / name).is_symlink():
                raise ValueError(
                    "Symlink input directories require an explicit supported profile"
                )
        for name in sorted(filenames):
            path = Path(directory) / name
            if path in excluded:
                continue
            if path.suffix not in {
                ".ts",
                ".tsx",
                ".js",
                ".jsx",
                ".mjs",
                ".cjs",
                ".json",
            } and name not in {"yarn.lock", "pnpm-lock.yaml"}:
                continue
            files[path.relative_to(root).as_posix()] = _hash(path)
    if not files:
        raise ValueError("No TypeScript/JavaScript configuration or source inputs")
    return files


def validate_index(workspace, index_path):
    """Verify index bytes and the current input set against a generation receipt."""
    path = Path(index_path).absolute()
    receipt_path = Path(str(path) + ".sources.json")
    if receipt_path.is_symlink() or receipt_path.stat().st_size > 16_000_000:
        raise ValueError("Invalid SCIP source receipt")
    receipt = json.loads(receipt_path.read_text())
    if (
        receipt.get("schema_version") != 1
        or receipt.get("profile") != "scip-typescript-v1"
    ):
        raise ValueError("Unsupported SCIP source receipt profile")
    expected = receipt.get("files")
    producer = receipt.get("producer", {})
    if producer.get("name") != "scip-typescript" or producer.get("version") != "0.6.0":
        raise ValueError(
            "The validated TypeScript profile requires scip-typescript 0.6.0"
        )
    if (
        not isinstance(expected, dict)
        or not expected
        or any(not is_relative_file(name) for name in expected)
    ):
        raise ValueError("Invalid SCIP input manifest")
    if _hash(path) != receipt.get("index_sha256"):
        raise ValueError("SCIP index differs from its source receipt")
    if capture_inputs(workspace, path) != expected:
        raise ValueError(
            "SCIP source/configuration/dependencies changed; regenerate the index"
        )
    if receipt.get("position_encoding") != "utf-16":
        raise ValueError("Unsupported TypeScript coordinate profile")
    return receipt


def index_typescript(workspace, *, node, indexer, output=None, timeout=180):
    """Run explicitly provisioned scip-typescript and publish its binding receipt."""
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("SCIP indexer timeout must be finite and positive")
    try:
        from ._scip import scip_pb2
    except ImportError as exc:
        raise ValueError("Provision the scip extra before indexing") from exc
    started = time.monotonic()
    root = Path(workspace).resolve(strict=True)
    output = Path(output).absolute() if output is not None else root / "index.scip"
    node, indexer = Path(node).resolve(strict=True), Path(indexer).resolve(strict=True)
    if output.suffix != ".scip" or not output.parent.is_dir() or output.is_symlink():
        raise ValueError("Index output requires an existing directory and regular path")
    before = capture_inputs(root, output)
    with tempfile.TemporaryDirectory(prefix="deepthought-scip-") as scratch:
        temporary = Path(scratch) / "index.scip"
        argv = [
            str(node),
            str(indexer),
            "index",
            "--cwd",
            str(root),
            "--output",
            str(temporary),
            "--no-progress-bar",
        ]
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError(
                "SCIP indexer exceeded its deadline; previous index preserved"
            ) from exc
        if (
            result.returncode
            or "unexpected error" in (result.stdout + result.stderr).lower()
        ):
            raise ValueError(
                "SCIP indexer failed or reported incomplete indexing: "
                + (result.stdout + result.stderr)[-4000:]
            )
        after = capture_inputs(root, output)
        if before != after:
            raise ValueError(
                "SCIP inputs changed during indexing; previous index preserved"
            )
        data = temporary.read_bytes()
        parsed = scip_pb2.Index()
        parsed.ParseFromString(data)
        if (
            parsed.metadata.tool_info.name != "scip-typescript"
            or parsed.metadata.tool_info.version != "0.6.0"
            or not parsed.documents
        ):
            raise ValueError("Expected nonempty scip-typescript 0.6.0 output")
        for document in parsed.documents:
            if (
                not is_relative_file(document.relative_path)
                or document.relative_path not in before
            ):
                raise ValueError(
                    "SCIP indexed a document outside the captured input manifest"
                )
        receipt = {
            "schema_version": 1,
            "profile": "scip-typescript-v1",
            "index_sha256": sha256(data).hexdigest(),
            "files": before,
            "position_encoding": "utf-16",
            "encoding_basis": "Pinned scip-typescript uses TypeScript getLineAndCharacterOfPosition; explicit fallback for unspecified document encoding",
            "producer": {
                "name": parsed.metadata.tool_info.name,
                "version": parsed.metadata.tool_info.version,
                "node_sha256": _hash(node),
                "entry_sha256": _hash(indexer),
            },
            "documents": len(parsed.documents),
            "wall_seconds": time.monotonic() - started,
        }
        # Write both files through same-directory temporary files. A crash between
        # replacements leaves an explicitly rejected pair, never a falsely fresh index.
        staged = []
        try:
            for destination, content in [
                (output, data),
                (
                    Path(str(output) + ".sources.json"),
                    json.dumps(receipt, sort_keys=True).encode(),
                ),
            ]:
                with tempfile.NamedTemporaryFile(
                    dir=output.parent, delete=False
                ) as stream:
                    staged.append(Path(stream.name))
                    stream.write(content)
                os.replace(staged[-1], destination)
        finally:
            for path in staged:
                path.unlink(missing_ok=True)
    return {
        "index": str(output),
        "source_receipt_sha256": _digest(receipt),
        "documents": receipt["documents"],
        "wall_seconds": receipt["wall_seconds"],
    }
