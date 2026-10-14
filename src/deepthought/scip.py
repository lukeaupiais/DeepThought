"""Ingest supplied SCIP facts, preserving native symbols and document paths.

SCIP references are not interpreted as calls or construction. Executable
indexing, cross-tool resolution and graph composition are separate operations.
"""

from hashlib import sha256
from importlib.metadata import version
import json
import os
from pathlib import Path
import stat

from .models import (
    AnalysisResult,
    Definition,
    Diagnostic,
    FileCoverage,
    is_relative_file,
)
from .syntax import identifier


def ingest_scip(
    path: str | Path,
    *,
    repository_id: str,
    max_index_bytes: int = 50_000_000,
    path_prefix: str = "",
) -> AnalysisResult:
    """Read a local artifact; never invoke an indexer or read indexed source files."""
    if not repository_id.strip() or max_index_bytes < 1:
        raise ValueError("Nonempty repository_id and positive max_index_bytes required")
    if not isinstance(path_prefix, str) or (
        path_prefix and not is_relative_file(path_prefix)
    ):
        raise ValueError("path_prefix must be canonical and relative")
    definitions, coverage, diagnostics, versions = [], [], [], {}
    try:
        from google.protobuf.message import DecodeError
        from ._scip import scip_pb2 as wire
    except ImportError:
        diagnostics.append(
            Diagnostic(
                "protocol_unavailable", "Provision the scip extra before ingestion"
            )
        )
        wire = None
    if wire is not None:
        try:
            fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("Index must be a regular file")
                content = source.read(max_index_bytes + 1)
            if len(content) > max_index_bytes:
                raise ValueError("Index exceeds max_index_bytes")
            index = wire.Index()
            index.ParseFromString(content)
            if not index.metadata.tool_info.name:
                raise ValueError("Index is missing producing-tool metadata")
            versions["protobuf"] = version("protobuf")
            versions[index.metadata.tool_info.name] = index.metadata.tool_info.version
            seen_files, seen_definitions = set(), set()
            for doc in sorted(index.documents, key=lambda item: item.relative_path):
                file = doc.relative_path
                if not is_relative_file(file):
                    diagnostics.append(
                        Diagnostic(
                            "invalid_document_path",
                            "SCIP document path must be canonical and relative",
                        )
                    )
                    continue
                if path_prefix:
                    file = f"{path_prefix}/{file}"
                if file in seen_files:
                    diagnostics.append(
                        Diagnostic(
                            "duplicate_document", "SCIP repeats a document path", file
                        )
                    )
                    continue
                seen_files.add(file)
                infos = {item.symbol: item for item in doc.symbols}
                encoding = {1: "utf-8", 2: "utf-16", 3: "utf-32"}.get(
                    doc.position_encoding, "unspecified"
                )
                before = len(diagnostics)
                if encoding == "unspecified":
                    diagnostics.append(
                        Diagnostic(
                            "position_encoding_unspecified",
                            "Indexer did not declare source coordinate encoding",
                            file,
                        )
                    )
                ordinals = {}
                for occurrence in sorted(
                    doc.occurrences, key=lambda item: (tuple(item.range), item.symbol)
                ):
                    if not occurrence.symbol_roles & wire.Definition:
                        continue
                    location = list(occurrence.range)
                    if len(location) == 3:
                        location.insert(2, location[0])
                    if (
                        len(location) != 4
                        or min(location) < 0
                        or tuple(location[:2]) > tuple(location[2:])
                        or not occurrence.symbol
                    ):
                        diagnostics.append(
                            Diagnostic(
                                "invalid_definition_range",
                                "Invalid SCIP definition occurrence",
                                file,
                            )
                        )
                        continue
                    native = occurrence.symbol
                    info = infos.get(native)
                    kind = (
                        wire.SymbolInformation.Kind.Name(info.kind)
                        if info is not None
                        else "UnspecifiedKind"
                    )
                    if kind == "UnspecifiedKind":
                        kind = "Symbol"
                    symbol = (
                        info.display_name
                        if info is not None and info.display_name
                        else native
                    )
                    occurrence_key = (file, native, kind, tuple(location))
                    if occurrence_key in seen_definitions:
                        continue
                    seen_definitions.add(occurrence_key)
                    ordinal = ordinals.get((native, kind), 0)
                    ordinals[(native, kind)] = ordinal + 1
                    identity = identifier(repository_id, file, native, kind, ordinal)
                    definitions.append(
                        Definition(
                            identity,
                            file,
                            symbol,
                            kind,
                            location[0] + 1,
                            location[1],
                            location[2] + 1,
                            location[3],
                            native,
                            encoding,
                        )
                    )
                digest = sha256(doc.SerializeToString(deterministic=True)).hexdigest()
                coverage.append(
                    FileCoverage(
                        file,
                        doc.language or "unspecified",
                        "partial" if len(diagnostics) != before else "analyzed",
                        digest,
                    )
                )
        except (OSError, ValueError, DecodeError) as exc:
            diagnostics.append(Diagnostic("invalid_index", str(exc)))
    status = (
        "succeeded" if not diagnostics else ("partial" if definitions else "failed")
    )
    definitions.sort(key=lambda item: (item.file, item.line, item.column, item.id))
    manifest = {
        "repository_id": repository_id,
        "status": status,
        "diagnostics": [vars(item) for item in diagnostics],
        "definitions": [vars(d) for d in definitions],
        "coverage": [vars(c) for c in coverage],
        "versions": sorted(versions.items()),
        "analyzer_version": "0.1.0a3",
    }
    snapshot = sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return AnalysisResult(
        repository_id,
        snapshot,
        status,
        tuple(definitions),
        tuple(coverage),
        tuple(diagnostics),
        "scip",
        tuple(sorted(versions.items())),
        coordinate_encoding="per-definition",
    )
