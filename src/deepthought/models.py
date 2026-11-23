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


IMPORT_KINDS = frozenset({"import", "from_import", "export_from"})


@dataclass(frozen=True)
class Import:
    """A static import statement as written; resolution is a separate step."""

    id: str
    file: str
    specifier: str
    kind: str
    line: int
    column: int
    end_line: int
    end_column: int
    names: tuple[str, ...] = ()
    coordinate_encoding: str = "utf-8"
    # Names the statement binds in the importing module (aliases applied).
    local_names: tuple[str, ...] = ()

    def __post_init__(self):
        validate_location(
            self.file,
            self.line,
            self.column,
            self.end_line,
            self.end_column,
            self.coordinate_encoding,
        )
        if not all(isinstance(v, str) and v for v in (self.id, self.specifier)):
            raise ValueError("Import identity and specifier must be nonempty strings")
        if self.kind not in IMPORT_KINDS:
            raise ValueError("Unsupported import kind")
        for values in (self.names, self.local_names):
            if not isinstance(values, tuple) or not all(
                isinstance(name, str) and name for name in values
            ):
                raise ValueError("Imported names must be tuples of nonempty strings")


CALL_KINDS = frozenset({"call", "new"})


@dataclass(frozen=True)
class CallSite:
    """A call whose callee is a name or dotted name, as written; not a resolved call."""

    id: str
    file: str
    callee: str
    kind: str
    line: int
    column: int
    end_line: int
    end_column: int
    caller_id: str | None = None
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
        if not all(isinstance(v, str) and v for v in (self.id, self.callee)):
            raise ValueError("Call identity and callee must be nonempty strings")
        if self.kind not in CALL_KINDS:
            raise ValueError("Unsupported call kind")
        if self.caller_id is not None and (
            not isinstance(self.caller_id, str) or not self.caller_id
        ):
            raise ValueError("Caller identity must be a nonempty string or None")


SUPERTYPE_KINDS = frozenset({"extends", "implements"})
PACKAGE_KINDS = frozenset({"package", "module"})


@dataclass(frozen=True)
class Supertype:
    """A base named in a class or interface declaration, as written; not resolved.

    ``kind`` is the clause that names it: Python bases and TypeScript
    ``extends`` are ``extends``; a TypeScript class's ``implements`` entries are
    ``implements``.
    """

    id: str
    file: str
    owner_id: str
    name: str
    kind: str
    line: int
    column: int
    end_line: int
    end_column: int
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
            isinstance(v, str) and v for v in (self.id, self.owner_id, self.name)
        ):
            raise ValueError("Supertype identity, owner and name must be nonempty")
        if self.kind not in SUPERTYPE_KINDS:
            raise ValueError("Unsupported supertype kind")


@dataclass(frozen=True)
class PackageClause:
    """A package or module a file declares, as written.

    ``package`` is a Go source file's ``package name`` or a Java file's dotted
    ``package a.b;`` name; ``module`` is the module path a ``go.mod`` file's
    ``module`` directive declares.
    """

    id: str
    file: str
    name: str
    line: int
    column: int
    end_line: int
    end_column: int
    kind: str = "package"
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
        if not all(isinstance(v, str) and v for v in (self.id, self.name)):
            raise ValueError("Package clause identity and name must be nonempty")
        if self.kind not in PACKAGE_KINDS:
            raise ValueError("Unsupported package clause kind")


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
    # Schema 3 adds import, call-site and supertype facts; producers without
    # them keep emitting schema 2.
    schema_version: str = "2"
    coordinate_encoding: str = "utf-8"
    references: tuple[Reference, ...] = ()
    imports: tuple[Import, ...] = ()
    call_sites: tuple[CallSite, ...] = ()
    supertypes: tuple[Supertype, ...] = ()
    packages: tuple[PackageClause, ...] = ()

    def __post_init__(self):
        if self.status not in {"succeeded", "partial", "failed", "cancelled"}:
            raise ValueError("Invalid analysis status")
        if (
            self.schema_version not in ("2", "3")
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
        if len({i.id for i in self.imports}) != len(self.imports):
            raise ValueError("Duplicate import identities")
        if self.imports and "imports" not in self.capabilities:
            raise ValueError("Import facts require the imports capability")
        if len({c.id for c in self.call_sites}) != len(self.call_sites):
            raise ValueError("Duplicate call-site identities")
        if self.call_sites and "call_sites" not in self.capabilities:
            raise ValueError("Call-site facts require the call_sites capability")
        definitions = {d.id: d.file for d in self.definitions}
        if any(
            c.caller_id is not None and definitions.get(c.caller_id) != c.file
            for c in self.call_sites
        ):
            raise ValueError("Call-site callers must be definitions in the same file")
        if len({t.id for t in self.supertypes}) != len(self.supertypes):
            raise ValueError("Duplicate supertype identities")
        if self.supertypes and "supertypes" not in self.capabilities:
            raise ValueError("Supertype facts require the supertypes capability")
        if any(definitions.get(t.owner_id) != t.file for t in self.supertypes):
            raise ValueError("Supertype owners must be definitions in the same file")
        if len({p.id for p in self.packages}) != len(self.packages):
            raise ValueError("Duplicate package clause identities")
        if len({p.file for p in self.packages}) != len(self.packages):
            raise ValueError("A file declares at most one package")
        if self.packages and "packages" not in self.capabilities:
            raise ValueError("Package clauses require the packages capability")
        if self.schema_version == "2" and {
            "imports",
            "call_sites",
            "supertypes",
            "packages",
        } & set(self.capabilities):
            raise ValueError(
                "Schema 2 cannot contain imports, call sites, supertypes or packages"
            )
        covered = {item.file for item in self.coverage}
        if any(
            item.file not in covered
            for item in (
                *self.definitions,
                *self.references,
                *self.imports,
                *self.call_sites,
                *self.supertypes,
                *self.packages,
            )
        ):
            raise ValueError("Every fact requires source file coverage")
        if self.status == "succeeded" and (
            self.diagnostics or any(item.status != "analyzed" for item in self.coverage)
        ):
            raise ValueError(
                "Successful analysis cannot contain incomplete coverage or diagnostics"
            )

    def to_dict(self) -> dict:
        data = json.loads(json.dumps(asdict(self)))
        if not self.supertypes and "supertypes" not in self.capabilities:
            # Artifacts without supertype facts keep their serialized identity.
            del data["supertypes"]
        if not self.packages and "packages" not in self.capabilities:
            del data["packages"]
        if self.schema_version == "2":
            # Schema 2 predates these facts; keep its serialized identity unchanged.
            del data["imports"], data["call_sites"]
        return data

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, ensure_ascii=False, allow_nan=False
        )

    @classmethod
    def from_json(cls, value: str) -> "AnalysisResult":
        data = json.loads(value)
        if not isinstance(data, dict) or data.get("schema_version") not in (
            "1",
            "2",
            "3",
        ):
            raise ValueError("Unsupported artifact schema version")
        try:
            if data["schema_version"] == "1":
                if data.get("references") or "references" in data.get(
                    "capabilities", []
                ):
                    raise ValueError("Schema 1 cannot contain references")
                data["schema_version"] = "2"
                data["references"] = []
            if data["schema_version"] == "2":
                if {"imports", "call_sites", "supertypes", "packages"} & data.keys():
                    raise ValueError(
                        "Schema 2 cannot contain imports, call sites, supertypes "
                        "or packages"
                    )
                data["imports"] = []
                data["call_sites"] = []
            data["references"] = tuple(Reference(**item) for item in data["references"])
            data["imports"] = tuple(
                Import(
                    **{
                        **item,
                        "names": tuple(item["names"]),
                        "local_names": tuple(item["local_names"]),
                    }
                )
                for item in data["imports"]
            )
            # Schema 3 artifacts written before call sites existed have no key.
            data["call_sites"] = tuple(
                CallSite(**item) for item in data.get("call_sites", [])
            )
            data["supertypes"] = tuple(
                Supertype(**item) for item in data.get("supertypes", [])
            )
            data["packages"] = tuple(
                PackageClause(**item) for item in data.get("packages", [])
            )
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
