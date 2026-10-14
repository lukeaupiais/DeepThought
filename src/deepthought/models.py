"""Portable artifacts; lines are one-based, columns retain declared encoding."""

import json
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath, PureWindowsPath


def is_relative_file(file: str) -> bool:
    return (
        isinstance(file, str)
        and bool(file)
        and "\\" not in file
        and "\x00" not in file
        and not PurePosixPath(file).is_absolute()
        and not PureWindowsPath(file).drive
        and all(part not in {"", ".", ".."} for part in file.split("/"))
    )


@dataclass(frozen=True)
class Definition:
    id: str
    file: str
    symbol: str
    kind: str
    line: int
    column: int
    end_line: int
    end_column: int
    native_id: str | None = None
    coordinate_encoding: str = "utf-8"

    def __post_init__(self):
        if not is_relative_file(self.file):
            raise ValueError("Definition file must be canonical and relative")
        if not all(
            isinstance(value, str) and value
            for value in (self.id, self.symbol, self.kind)
        ):
            raise ValueError(
                "Definition identity, symbol and kind must be nonempty strings"
            )
        if (
            any(
                type(value) is not int
                for value in (self.line, self.column, self.end_line, self.end_column)
            )
            or self.line < 1
            or min(self.column, self.end_column) < 0
            or (self.end_line, self.end_column) < (self.line, self.column)
        ):
            raise ValueError("Invalid definition source range")
        if self.coordinate_encoding not in {"utf-8", "utf-16", "utf-32", "unspecified"}:
            raise ValueError("Unsupported source coordinate encoding")


@dataclass(frozen=True)
class FileCoverage:
    file: str
    language: str
    status: str
    digest: str | None = None

    def __post_init__(self):
        if not is_relative_file(self.file) or self.status not in {
            "analyzed",
            "partial",
            "failed",
            "skipped",
            "unavailable",
        }:
            raise ValueError("Invalid file coverage")


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    file: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class AnalysisResult:
    repository_id: str
    snapshot_id: str
    status: str
    definitions: tuple[Definition, ...]
    coverage: tuple[FileCoverage, ...]
    diagnostics: tuple[Diagnostic, ...]
    analyzer: str
    analyzer_versions: tuple[tuple[str, str], ...]
    capabilities: tuple[str, ...] = ("definitions",)
    schema_version: str = "1"
    coordinate_encoding: str = "utf-8"

    def __post_init__(self):
        if self.status not in {"succeeded", "partial", "failed", "cancelled"}:
            raise ValueError("Invalid analysis status")
        if (
            self.schema_version != "1"
            or not isinstance(self.repository_id, str)
            or not self.repository_id.strip()
        ):
            raise ValueError("Invalid artifact schema or repository identity")
        if len({d.id for d in self.definitions}) != len(self.definitions):
            raise ValueError("Duplicate definition identities")
        if len({c.file for c in self.coverage}) != len(self.coverage):
            raise ValueError("Duplicate coverage files")

    def to_dict(self) -> dict:
        return json.loads(json.dumps(asdict(self)))

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, ensure_ascii=False, allow_nan=False
        )

    @classmethod
    def from_json(cls, value: str) -> "AnalysisResult":
        data = json.loads(value)
        if not isinstance(data, dict) or data.get("schema_version") != "1":
            raise ValueError("Unsupported artifact schema version")
        try:
            data["definitions"] = tuple(
                Definition(**item) for item in data["definitions"]
            )
            data["coverage"] = tuple(FileCoverage(**item) for item in data["coverage"])
            data["diagnostics"] = tuple(
                Diagnostic(**item) for item in data["diagnostics"]
            )
            data["analyzer_versions"] = tuple(
                tuple(item) for item in data["analyzer_versions"]
            )
            data["capabilities"] = tuple(data["capabilities"])
            return cls(**data)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError("Invalid extraction artifact") from exc
