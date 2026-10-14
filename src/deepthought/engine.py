"""Local full-snapshot extraction, independent of storage and host platforms."""

from hashlib import sha256
from fnmatch import fnmatchcase
from importlib.metadata import version
import json
import os
from pathlib import Path
import stat
from threading import Event

from .models import AnalysisResult, Diagnostic, FileCoverage, is_relative_file
from .syntax import LANGUAGES, extract, make_parser


def analyze_repository(
    path: str | Path,
    *,
    repository_id: str,
    languages: tuple[str, ...] = ("python", "typescript"),
    max_file_bytes: int = 2_000_000,
    cancel_event: Event | None = None,
    max_files: int = 10_000,
    max_total_bytes: int = 100_000_000,
    include: tuple[str, ...] = ("*",),
    exclude: tuple[str, ...] = (),
) -> AnalysisResult:
    """Extract named declarations; cancellation is cooperative between files."""
    root = Path(path).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Workspace must be a directory")
    if not repository_id.strip():
        raise ValueError("repository_id must be nonempty")
    if not languages or set(languages) - LANGUAGES.keys():
        raise ValueError("Supported languages: python, typescript")
    if max_file_bytes < 1:
        raise ValueError("max_file_bytes must be positive")
    if max_files < 1 or max_total_bytes < 1:
        raise ValueError("File and total-byte limits must be positive")
    languages = tuple(sorted(set(languages)))
    definitions, coverage, diagnostics = [], [], []
    parsers, versions = {}, {}
    cancelled = False
    stop = False
    processed = total_bytes = 0

    def directory_error(exc):
        diagnostics.append(
            Diagnostic(
                "directory_unreadable", "Could not enumerate a selected directory"
            )
        )

    for directory, dirs, names, directory_fd in os.fwalk(
        root, follow_symlinks=False, onerror=directory_error
    ):
        if stop:
            break
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        dirs[:] = sorted(
            d for d in dirs if d not in {".git", ".venv", "node_modules", "__pycache__"}
        )
        for name in sorted(names):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            relative = (Path(directory) / name).relative_to(root).as_posix()
            if not any(fnmatchcase(relative, pattern) for pattern in include) or any(
                fnmatchcase(relative, pattern) for pattern in exclude
            ):
                continue
            suffix = Path(name).suffix
            language = next(
                (lang for lang in languages if suffix in LANGUAGES[lang][0]), None
            )
            if language is None:
                continue
            if not is_relative_file(relative):
                diagnostics.append(
                    Diagnostic(
                        "invalid_source_path",
                        "Selected source path cannot be represented in a portable artifact",
                    )
                )
                continue
            if processed >= max_files:
                diagnostics.append(
                    Diagnostic(
                        "file_count_limit", "Analysis reached max_files", relative
                    )
                )
                stop = True
                break
            processed += 1
            digest = None
            try:
                fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
                with os.fdopen(fd, "rb") as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise ValueError("Source must be a regular file")
                    content = source.read(
                        min(max_file_bytes + 1, max_total_bytes - total_bytes + 1)
                    )
                digest = sha256(content).hexdigest()
                total_bytes += len(content)
                if total_bytes > max_total_bytes:
                    diagnostics.append(
                        Diagnostic(
                            "total_byte_limit",
                            "Analysis reached max_total_bytes",
                            relative,
                        )
                    )
                    coverage.append(FileCoverage(relative, language, "skipped"))
                    stop = True
                    break
                if len(content) > max_file_bytes:
                    coverage.append(FileCoverage(relative, language, "skipped"))
                    diagnostics.append(
                        Diagnostic(
                            "file_limit", "Source exceeds max_file_bytes", relative
                        )
                    )
                    continue
                content.decode("utf-8")
                key = (language, suffix)
                if key not in parsers:
                    parsers[key] = make_parser(language, suffix)
                    for package in (
                        "tree-sitter",
                        LANGUAGES[language][1].replace("_", "-"),
                    ):
                        versions[package] = version(package)
                tree = parsers[key].parse(content)
                if tree.root_node.has_error:
                    coverage.append(
                        FileCoverage(
                            relative, language, "partial", sha256(content).hexdigest()
                        )
                    )
                    diagnostics.append(
                        Diagnostic(
                            "syntax_error", "Source contains syntax errors", relative
                        )
                    )
                else:
                    coverage.append(
                        FileCoverage(
                            relative, language, "analyzed", sha256(content).hexdigest()
                        )
                    )
                definitions.extend(extract(tree, content, repository_id, relative))
            except ImportError:
                coverage.append(FileCoverage(relative, language, "unavailable", digest))
                diagnostics.append(
                    Diagnostic(
                        "grammar_unavailable",
                        f"Provision the {language} grammar before analysis",
                        relative,
                    )
                )
            except (OSError, UnicodeError, ValueError) as exc:
                coverage.append(FileCoverage(relative, language, "failed", digest))
                diagnostics.append(Diagnostic("source_unreadable", str(exc), relative))
    cancelled = cancelled or (cancel_event is not None and cancel_event.is_set())
    status = (
        "cancelled"
        if cancelled
        else (
            "succeeded" if not diagnostics else ("partial" if definitions else "failed")
        )
    )
    manifest = {
        "repository_id": repository_id,
        "status": status,
        "diagnostics": [vars(item) for item in diagnostics],
        "files": [vars(item) for item in coverage],
        "languages": languages,
        "versions": sorted(versions.items()),
        "analyzer_version": "0.1.0a3",
        "max_file_bytes": max_file_bytes,
        "max_files": max_files,
        "max_total_bytes": max_total_bytes,
        "include": include,
        "exclude": exclude,
    }
    snapshot = sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return AnalysisResult(
        repository_id,
        snapshot,
        status,
        tuple(definitions),
        tuple(coverage),
        tuple(diagnostics),
        "tree-sitter",
        tuple(sorted(versions.items())),
    )
