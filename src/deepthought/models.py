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
    # Java methods and constructors: declared parameter count, and whether
    # the last one is variable-arity (``T...``). None elsewhere.
    parameters: int | None = None
    variadic: bool = False
    # Java methods and constructors: each parameter's type as written,
    # without type arguments (``List``, ``int[]``, ``T``). None elsewhere.
    parameter_types: tuple[str, ...] | None = None
    # Java methods and constructors declared ``private``, which subtypes do
    # not inherit: True. None elsewhere.
    private: bool | None = None
    # Rust ``macro_rules!``: whether ``#[macro_export]`` places the macro at
    # its crate's root. None elsewhere.
    macro_export: bool | None = None
    # Python: True for a ``typing.overload`` signature. None elsewhere.
    overload: bool | None = None

    def __post_init__(self):
        if self.parameter_types is not None and (
            type(self.parameter_types) is not tuple
            or not all(isinstance(t, str) and t for t in self.parameter_types)
        ):
            raise ValueError("parameter_types must be a tuple of nonempty strings")
        if self.private is not None and self.private is not True:
            raise ValueError("private must be True or None")
        if self.overload is not None and self.overload is not True:
            raise ValueError("overload must be True or None")
        if self.macro_export is not None and type(self.macro_export) is not bool:
            raise ValueError("macro_export must be a boolean or None")
        if self.parameters is not None and (
            type(self.parameters) is not int or self.parameters < 0
        ):
            raise ValueError("Parameter count must be a nonnegative integer")
        if type(self.variadic) is not bool or (self.variadic and not self.parameters):
            raise ValueError("Only a declaration with parameters can be variadic")
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


# "module" and "module_path": a Rust `mod name;`, which includes a file as a
# module, by name or by a #[path] attribute. "crate_library" and
# "crate_target": a Cargo.toml target, which names a crate root file.
# "use", "use_glob" and "extern_crate": a Rust use declaration's leaf, glob
# or an extern crate.
IMPORT_KINDS = frozenset(
    {
        "import",
        "from_import",
        "export_from",
        "module",
        "module_path",
        "crate_library",
        "crate_target",
        "use",
        "use_glob",
        "extern_crate",
        "use_function",
        "use_const",
    }
)


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
    # Rust declarations written in a block (a function body, or a block in
    # one), which bind names in that block only: the block's (line, column,
    # end line, end column). None elsewhere.
    block: tuple[int, int, int, int] | None = None

    def __post_init__(self):
        if self.block is not None and (
            type(self.block) is not tuple
            or len(self.block) != 4
            or not all(type(v) is int and v >= 0 for v in self.block)
        ):
            raise ValueError("block must be a span of four nonnegative integers")
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
    # Java calls: the number of arguments written. None elsewhere.
    arguments: int | None = None
    # Java calls inside anonymous class bodies (an enum constant's body too):
    # (base type name as written, symbol of the scope the class body's
    # methods are named in, first line, last line) for each such class around
    # the call, innermost first. None elsewhere.
    anonymous: tuple[tuple[str, str, int, int], ...] | None = None
    # Python calls whose callee's first name is bound in a function, lambda
    # or comprehension around the call (a parameter, an assignment, a loop
    # variable): True. None elsewhere.
    shadowed: bool | None = None
    # Java calls ``x.m()`` and ``this.x.m()`` where the declaration of the
    # variable or field ``x`` in scope writes a class type: that type as
    # written (``Foo``, ``Map.Entry``). Go calls ``x.M()`` where ``x`` is
    # declared in the function around the call: the type that declaration
    # writes (``T`` or ``pkg.T``). None elsewhere.
    receiver_type: str | None = None

    def __post_init__(self):
        if self.receiver_type is not None and (
            not isinstance(self.receiver_type, str) or not self.receiver_type
        ):
            raise ValueError("receiver_type must be a nonempty string or None")
        if self.shadowed not in (None, True):
            raise ValueError("shadowed must be True or None")
        if self.arguments is not None and (
            type(self.arguments) is not int or self.arguments < 0
        ):
            raise ValueError("Argument count must be a nonnegative integer")
        if self.anonymous is not None and (
            type(self.anonymous) is not tuple
            or not self.anonymous
            or not all(
                type(entry) is tuple
                and len(entry) == 4
                and all(isinstance(v, str) and v for v in entry[:2])
                and all(type(v) is int and v >= 1 for v in entry[2:])
                and entry[2] <= entry[3]
                for entry in self.anonymous
            )
        ):
            raise ValueError(
                "Anonymous classes must be a nonempty tuple of "
                "(base, scope, first line, last line)"
            )
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
class ImplBlock:
    """A Rust impl block, as written; not resolved.

    ``type`` is the path of the type it implements for and ``trait`` the path
    of the trait it implements (None for an inherent impl), each joined by
    ``::`` without type arguments, references or pointers; a leading ``::``
    is kept. ``generics`` are the impl's type parameter names. The location
    spans the whole block.
    """

    id: str
    file: str
    type: str
    trait: str | None
    generics: tuple[str, ...]
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
        if not all(isinstance(v, str) and v for v in (self.id, self.type)):
            raise ValueError("Impl identity and type must be nonempty strings")
        if self.trait is not None and (
            not isinstance(self.trait, str) or not self.trait
        ):
            raise ValueError("Impl trait must be a nonempty string or None")
        if type(self.generics) is not tuple or not all(
            isinstance(v, str) and v for v in self.generics
        ):
            raise ValueError("Impl generics must be a tuple of nonempty strings")


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
    impls: tuple[ImplBlock, ...] = ()

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
        if len({i.id for i in self.impls}) != len(self.impls):
            raise ValueError("Duplicate impl identities")
        if self.impls and "impls" not in self.capabilities:
            raise ValueError("Impl facts require the impls capability")
        if self.schema_version == "2" and {
            "imports",
            "call_sites",
            "supertypes",
            "packages",
            "impls",
        } & set(self.capabilities):
            raise ValueError(
                "Schema 2 cannot contain imports, call sites, supertypes, "
                "packages or impls"
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
                *self.impls,
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
        # Arity is recorded for Java only; other facts keep their serialized
        # identity.
        for item in data["definitions"]:
            if item["parameters"] is None:
                del item["parameters"], item["variadic"]
            if item["parameter_types"] is None:
                del item["parameter_types"]
            if item["private"] is None:
                del item["private"]
            if item["macro_export"] is None:
                del item["macro_export"]
            if item["overload"] is None:
                del item["overload"]
        for item in data["call_sites"]:
            if item["arguments"] is None:
                del item["arguments"]
            if item["anonymous"] is None:
                del item["anonymous"]
            if item["shadowed"] is None:
                del item["shadowed"]
            if item["receiver_type"] is None:
                del item["receiver_type"]
        for item in data["imports"]:
            if item["block"] is None:
                del item["block"]
        if not self.supertypes and "supertypes" not in self.capabilities:
            # Artifacts without supertype facts keep their serialized identity.
            del data["supertypes"]
        if not self.packages and "packages" not in self.capabilities:
            del data["packages"]
        if not self.impls and "impls" not in self.capabilities:
            del data["impls"]
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
                if {
                    "imports",
                    "call_sites",
                    "supertypes",
                    "packages",
                    "impls",
                } & data.keys():
                    raise ValueError(
                        "Schema 2 cannot contain imports, call sites, supertypes, "
                        "packages or impls"
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
                        **(
                            {"block": tuple(item["block"])}
                            if isinstance(item.get("block"), list)
                            else {}
                        ),
                    }
                )
                for item in data["imports"]
            )
            # Schema 3 artifacts written before call sites existed have no key.
            data["call_sites"] = tuple(
                CallSite(
                    **{
                        **item,
                        **(
                            {"anonymous": tuple(map(tuple, item["anonymous"]))}
                            if isinstance(item.get("anonymous"), list)
                            else {}
                        ),
                    }
                )
                for item in data.get("call_sites", [])
            )
            data["supertypes"] = tuple(
                Supertype(**item) for item in data.get("supertypes", [])
            )
            data["packages"] = tuple(
                PackageClause(**item) for item in data.get("packages", [])
            )
            data["impls"] = tuple(
                ImplBlock(
                    **{
                        **item,
                        "generics": tuple(item["generics"])
                        if isinstance(item.get("generics"), list)
                        else item.get("generics"),
                    }
                )
                for item in data.get("impls", [])
            )
            data["definitions"] = tuple(
                Definition(
                    **{
                        **item,
                        "parameter_types": tuple(item["parameter_types"])
                        if isinstance(item.get("parameter_types"), list)
                        else item.get("parameter_types"),
                    }
                )
                for item in data["definitions"]
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
