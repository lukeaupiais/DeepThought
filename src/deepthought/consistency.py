"""Bounded comparisons of recorded source bytes, never workspace-wide freshness."""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from threading import Event

from .models import AnalysisResult


@dataclass(frozen=True)
class FileConsistency:
    file: str
    status: str
    reason: str


@dataclass(frozen=True)
class SourceConsistency:
    repository_id: str
    snapshot_id: str
    status: str
    checks: tuple[FileConsistency, ...]
    recorded_files: int
    bytes_read: int
    cancelled: bool

    def to_dict(self) -> dict:
        return json.loads(self.to_json())

    def to_json(self) -> str:
        return json.dumps(
            {
                "artifact_type": "source_consistency",
                "schema_version": "1",
                "scope": "recorded_files",
                **asdict(self),
            },
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )


def _open_source(root_fd, file):
    """Open every path component relative to a pinned root, without symlinks."""
    directory = os.dup(root_fd)
    try:
        parts = file.split("/")
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        return os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    finally:
        os.close(directory)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _compare(root_fd, coverage, max_file_bytes, remaining, cancel_event):
    consumed = 0
    try:
        fd = _open_source(root_fd, coverage.file)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                return "unverified", "not_regular", consumed
            if before.st_size > max_file_bytes:
                return "unverified", "file_byte_limit", consumed
            if before.st_size > remaining:
                return "unverified", "total_byte_limit", consumed
            digest = sha256()
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return "unverified", "cancelled", consumed
                block = os.read(
                    fd,
                    min(
                        65_536, max_file_bytes - consumed + 1, remaining - consumed + 1
                    ),
                )
                consumed += len(block)
                if consumed > max_file_bytes or consumed > remaining:
                    return "unverified", "byte_limit", consumed
                if not block:
                    break
                digest.update(block)
            after = os.fstat(fd)
            # Reopen from the root to detect replacement of a file or ancestor.
            try:
                current = _open_source(root_fd, coverage.file)
            except OSError:
                return "unverified", "changed_during_check", consumed
            try:
                stable = (
                    _identity(before)
                    == _identity(after)
                    == _identity(os.fstat(current))
                )
            finally:
                os.close(current)
            if not stable:
                return "unverified", "changed_during_check", consumed
            return (
                ("unchanged", "digest_match", consumed)
                if digest.hexdigest() == coverage.digest
                else ("changed", "digest_mismatch", consumed)
            )
        finally:
            os.close(fd)
    except FileNotFoundError:
        return "changed", "missing", consumed
    except OSError:
        return "unverified", "source_unreadable", consumed


def check_source_consistency(
    path: str | os.PathLike,
    source: AnalysisResult,
    *,
    max_files: int = 10_000,
    max_file_bytes: int = 2_000_000,
    max_total_bytes: int = 100_000_000,
    cancel_event: Event | None = None,
) -> SourceConsistency:
    """Compare recorded Tree-sitter source hashes against a host-selected root.

    Linux descriptor-relative, no-symlink reads. Only recorded files are checked;
    additions, capture completeness, index freshness and navigation authorization
    are outside this contract. Comparisons are sequential, not an atomic view.
    Artifacts and repository-to-root assignment must be trusted by the caller.
    Cancellation is cooperative between reads. Hosts own isolation and deadlines.
    """
    if not isinstance(source, AnalysisResult):
        raise ValueError("An analysis artifact is required")
    if any(
        type(value) is not int or value < 1
        for value in (max_files, max_file_bytes, max_total_bytes)
    ):
        raise ValueError("File and byte limits must be positive integers")
    checks = []
    total = 0
    root_fd = None
    try:
        for coverage in source.coverage[:max_files]:
            if cancel_event is not None and cancel_event.is_set():
                break
            if (
                source.analyzer != "tree-sitter"
                or coverage.status not in {"analyzed", "partial"}
                or not isinstance(coverage.digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", coverage.digest) is None
            ):
                status, reason = "unverified", "no_source_digest"
            else:
                if root_fd is None:
                    # The host selects this root. Do not resolve symlink aliases.
                    root_fd = os.open(
                        Path(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                    )
                status, reason, consumed = _compare(
                    root_fd,
                    coverage,
                    max_file_bytes,
                    max(0, max_total_bytes - total),
                    cancel_event,
                )
                total += consumed
            checks.append(FileConsistency(coverage.file, status, reason))
            if total > max_total_bytes:
                break
    finally:
        if root_fd is not None:
            os.close(root_fd)
    cancelled = cancel_event is not None and cancel_event.is_set()
    status = (
        "changed"
        if any(check.status == "changed" for check in checks)
        else "unchanged"
        if (
            checks
            and not cancelled
            and len(checks) == len(source.coverage)
            and all(check.status == "unchanged" for check in checks)
        )
        else "unverified"
    )
    return SourceConsistency(
        source.repository_id,
        source.snapshot_id,
        status,
        tuple(checks),
        len(source.coverage),
        total,
        cancelled,
    )
