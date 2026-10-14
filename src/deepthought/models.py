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


def validate_location(file, line, column, end_line, end_column, encoding):
    if not is_relative_file(file):
        raise ValueError("Source file must be canonical and relative")
    if (
        any(type(v) is not int for v in (line, column, end_line, end_column))
        or line < 1
        or min(column, end_column) < 0
        or (end_line, end_column) < (line, column)
    ):
        raise ValueError("Invalid source range")
    if encoding not in {"utf-8", "utf-16", "utf-32", "unspecified"}:
        raise ValueError("Unsupported source coordinate encoding")


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
        validate_location(
            self.file,
            self.line,
            self.column,
            self.end_line,
            self.end_column,
            self.coordinate_encoding,
        )
        if not all(
            isinstance(value, str) and value
            for value in (self.id, self.symbol, self.kind)
        ):
            raise ValueError(
                "Definition identity, symbol and kind must be nonempty strings"
            )
        if self.native_id is not None and (
            not isinstance(self.native_id, str) or not self.native_id.strip()
        ):
            raise ValueError("Invalid native symbol")


@dataclass(frozen=True)
class Reference:
    id: str
    file: str
    native_id: str
    line: int
    column: int
    end_line: int
    end_column: int
    symbol_roles: int = 0
    coordinate_encoding: str = "utf-8"

    def __post_init__(self):
        validate_location(
            self.file,
            self.line,
            self.column,
            self.end_line,
            self.end_column,
            self.coordinate_encoding,
        )
        if not all(isinstance(v, str) and v.strip() for v in (self.id, self.native_id)):
            raise ValueError("Reference identity and native symbol required")
        if (
            type(self.symbol_roles) is not int
            or self.symbol_roles < 0
            or self.symbol_roles & 1
        ):
            raise ValueError(
                "Reference roles must be nonnegative and exclude Definition"
            )


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
    schema_version: str = "2"
    coordinate_encoding: str = "utf-8"
    references: tuple[Reference, ...] = ()

    def __post_init__(self):
        if self.status not in {"succeeded", "partial", "failed", "cancelled"}:
            raise ValueError("Invalid analysis status")
        if (
            self.schema_version != "2"
            or not isinstance(self.repository_id, str)
            or not self.repository_id.strip()
        ):
            raise ValueError("Invalid artifact schema or repository identity")
        if len({d.id for d in self.definitions}) != len(self.definitions):
            raise ValueError("Duplicate definition identities")
        if len({c.file for c in self.coverage}) != len(self.coverage):
            raise ValueError("Duplicate coverage files")
        if len({r.id for r in self.references}) != len(self.references):
            raise ValueError("Duplicate reference identities")
        if self.references and "references" not in self.capabilities:
            raise ValueError("Reference facts require the references capability")
        covered = {item.file for item in self.coverage}
        if any(
            item.file not in covered for item in (*self.definitions, *self.references)
        ):
            raise ValueError("Every fact requires source file coverage")
        if self.status == "succeeded" and (
            self.diagnostics or any(item.status != "analyzed" for item in self.coverage)
        ):
            raise ValueError(
                "Successful analysis cannot contain incomplete coverage or diagnostics"
            )

    def to_dict(self) -> dict:
        return json.loads(json.dumps(asdict(self)))

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, ensure_ascii=False, allow_nan=False
        )

    @classmethod
    def from_json(cls, value: str) -> "AnalysisResult":
        data = json.loads(value)
        if not isinstance(data, dict) or data.get("schema_version") not in ("1", "2"):
            raise ValueError("Unsupported artifact schema version")
        try:
            if data["schema_version"] == "1":
                if data.get("references") or "references" in data.get(
                    "capabilities", []
                ):
                    raise ValueError("Schema 1 cannot contain references")
                data["schema_version"] = "2"
                data["references"] = []
            data["references"] = tuple(Reference(**item) for item in data["references"])
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
