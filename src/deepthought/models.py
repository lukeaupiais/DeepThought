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
    # Java methods declared in an enum constant's body (``A { void m() {} }``),
    # named as the enum's members are: True. They are no members of the enum
    # type; a call through it binds the enum's own or inherited method.
    constant_body: bool | None = None
    # JavaScript and TypeScript methods declared ``static``: True. C
    # functions with internal linkage (declared ``static``): True. None
    # elsewhere.
    static: bool | None = None
    # JavaScript and TypeScript declarations their module exports as
    # ``default``: True. None elsewhere.
    default_export: bool | None = None
    # SCIP local variables an index from scip-python documents with one
    # inferred type: that type's qualified name (``app.models.User``), as
    # the indexer writes it. None elsewhere.
    value_type: str | None = None
    # Rust ``macro_rules!``: whether ``#[macro_export]`` places the macro at
    # its crate's root. None elsewhere.
    macro_export: bool | None = None
    # Python: True for a ``typing.overload`` signature. TypeScript: True for
    # a signature without a body that overloads a function or method
    # implemented beside it. None elsewhere.
    overload: bool | None = None
    # C functions declared without a body (a prototype): True. None
    # elsewhere.
    prototype: bool | None = None
    # Go functions and methods (interface methods too): the type of the
    # first result, as ``T``, ``pkg.T`` or ``[]`` before an element type,
    # through pointers and type arguments. Java methods: the return type as
    # written without type arguments (``Foo``, ``Map.Entry``, ``int``). None
    # elsewhere, or when that type is of another kind (``void``, an array, a
    # type parameter). Python functions and methods: the class the return
    # annotation names, as a parameter annotation's (``T``, ``m.T``,
    # ``Optional[T]``). Rust functions and methods with a body: the type path
    # the return type writes, joined by ``::``, through references and
    # without type arguments (``T``, ``m::T``, ``Self``), unless it names a
    # type parameter; None for any other return type.
    result_type: str | None = None
    # Go struct types: (name, type) for each named field whose type is
    # written as for ``result_type``. Java types: the same for each static
    # field and enum constant. JavaScript and TypeScript classes: (name,
    # class) for each instance field annotated with a class or initialized
    # with ``new`` of one, constructor parameter property annotated with one,
    # and getter whose return type is one. None elsewhere, or when there is
    # none.
    fields: tuple[tuple[str, str], ...] | None = None
    # JavaScript and TypeScript classes: the names of the instance fields the
    # body declares (``x = 1``, ``x: T``, ``declare x``) and of the
    # constructor's parameter properties (``private x: T``), which hide a
    # method of that name the class inherits. Java types: the names of every
    # field the body declares (static, private or not), enum constants and
    # record components. None elsewhere, or when there is none.
    properties: tuple[str, ...] | None = None
    # Go named types declared as a slice or array (``type byName []*T``):
    # that type, written as for ``result_type`` (``[]T``). None elsewhere.
    underlying_type: str | None = None
    # JavaScript and TypeScript top-level declarations: the other names a
    # local ``export { name as alias }`` (no ``from``) exports them under,
    # ``default`` aside (see ``default_export``). None elsewhere, or when
    # there is none.
    export_aliases: tuple[str, ...] | None = None
    # Go functions, methods and interface methods: the parameter and result
    # types, without names, as ``(T1,T2)(R1,R2)`` (``...T`` for a variadic one); a named type is written
    # as in the source (``T``, ``pkg.T``). None when a type is a type
    # parameter, an instantiated generic, or a struct or non-empty interface
    # literal. None elsewhere.
    signature: str | None = None
    # Go interface types: the types each embedded element writes (``Reader``,
    # ``io.Reader``; a union or ``~T`` constraint as written, without spaces).
    # None elsewhere, or when there is none.
    embeds: tuple[str, ...] | None = None

    def __post_init__(self):
        if self.underlying_type is not None and (
            not isinstance(self.underlying_type, str)
            or not self.underlying_type.startswith("[]")
        ):
            raise ValueError("underlying_type must be a slice type or None")
        if self.result_type is not None and (
            not isinstance(self.result_type, str) or not self.result_type
        ):
            raise ValueError("result_type must be a nonempty string or None")
        if self.fields is not None and (
            type(self.fields) is not tuple
            or not self.fields
            or not all(
                type(field) is tuple
                and len(field) == 2
                and all(isinstance(part, str) and part for part in field)
                for field in self.fields
            )
        ):
            raise ValueError("fields must be a tuple of (name, type) pairs")
        if self.properties is not None and (
            type(self.properties) is not tuple
            or not self.properties
            or not all(isinstance(name, str) and name for name in self.properties)
        ):
            raise ValueError("properties must be a tuple of nonempty strings")
        if self.export_aliases is not None and (
            type(self.export_aliases) is not tuple
            or not self.export_aliases
            or not all(isinstance(name, str) and name for name in self.export_aliases)
        ):
            raise ValueError("export_aliases must be a tuple of nonempty strings")
        if self.signature is not None and (
            not isinstance(self.signature, str) or not self.signature.startswith("(")
        ):
            raise ValueError("signature must be a parenthesized string or None")
        if self.embeds is not None and (
            type(self.embeds) is not tuple
            or not self.embeds
            or not all(isinstance(name, str) and name for name in self.embeds)
        ):
            raise ValueError("embeds must be a tuple of nonempty strings")
        if self.parameter_types is not None and (
            type(self.parameter_types) is not tuple
            or not all(isinstance(t, str) and t for t in self.parameter_types)
        ):
            raise ValueError("parameter_types must be a tuple of nonempty strings")
        if self.private is not None and self.private is not True:
            raise ValueError("private must be True or None")
        if self.constant_body not in (None, True):
            raise ValueError("constant_body must be True or None")
        if self.static is not None and self.static is not True:
            raise ValueError("static must be True or None")
        if self.default_export is not None and self.default_export is not True:
            raise ValueError("default_export must be True or None")
        if self.overload is not None and self.overload is not True:
            raise ValueError("overload must be True or None")
        if self.prototype is not None and self.prototype is not True:
            raise ValueError("prototype must be True or None")
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
# or an extern crate. "script_config", "path_alias" and "package_entry": a
# tsconfig.json or jsconfig.json and its ``paths`` entries, and the entry
# files a package.json names; module resolution reads them. "include": a C
# `#include "path"`. "require" and "dynamic_import": a JavaScript
# `require("x")` or a JavaScript or TypeScript `import("x")` call.
IMPORT_KINDS = frozenset(
    {
        "import",
        "require",
        "dynamic_import",
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
        "script_config",
        "path_alias",
        "package_entry",
        "include",
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
    # JavaScript and TypeScript re-exports (``export { a as b } from``): the
    # name each of ``names`` is exported under (aliases applied; ``*`` for
    # ``export *``, the namespace for ``export * as ns``). None elsewhere.
    exported_names: tuple[str, ...] | None = None
    # JavaScript and TypeScript imports: (local, exported) for each name the
    # statement binds that a local ``export { local as exported }`` (no
    # ``from``) of the same module exports again, which re-exports what the
    # import names. None elsewhere, or when there is none.
    reexported_as: tuple[tuple[str, str], ...] | None = None

    def __post_init__(self):
        if self.reexported_as is not None and (
            type(self.reexported_as) is not tuple
            or not self.reexported_as
            or not all(
                type(pair) is tuple
                and len(pair) == 2
                and all(isinstance(n, str) and n for n in pair)
                and pair[0] in self.local_names
                for pair in self.reexported_as
            )
        ):
            raise ValueError("reexported_as must pair local names with exported names")
        if self.exported_names is not None and (
            type(self.exported_names) is not tuple
            or len(self.exported_names) != len(self.names)
            or not all(isinstance(n, str) and n for n in self.exported_names)
        ):
            raise ValueError("exported_names must name each re-exported name")
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
    # Java calls with arguments: the static type of each argument where it
    # is written in it (``String`` for a string literal, ``int``, ``Class``
    # for a class literal and a concatenation with a String, ``Foo`` for
    # ``new Foo()``, ``(Foo) e`` or a variable declared ``Foo``, ``int`` for
    # one declared ``int``, ``null``), None for any other argument.
    # None when no argument's type is written.
    argument_types: tuple[str | None, ...] | None = None
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
    # written (``Foo``, ``Map.Entry``). Go calls ``x.M()`` and ``x.f.g.M()``
    # where ``x`` is declared in the function around the call: the type of
    # ``x`` its declaration gives, written as for ``Definition.result_type``.
    # Python calls ``x.m()``: the class name (``T``, ``m.T``) that the
    # binding of ``x`` writes. JavaScript and TypeScript calls ``x.m()``
    # where the declaration of ``x`` the call sees writes a type annotation
    # ``T`` or ``T<...>``, or initializes ``x`` with ``new T(...)``: ``T``;
    # or ``"this"`` for ``this`` in an instance method or field of a class
    # declaration (``this.a.m()``, ``const y = this; y.m()``). Rust calls
    # ``x.m()``: the type path, joined by ``::``, that the innermost
    # declaration of the local ``x`` writes (a parameter or ``let`` that
    # binds ``x`` alone), or ``Self``. None elsewhere.
    receiver_type: str | None = None
    # JavaScript and TypeScript, with a ``receiver_type`` other than
    # ``"this"``: the start (line, column) of the declaration of ``T`` seen
    # where ``x`` is declared, as for ``binding``. None elsewhere.
    receiver_binding: tuple[int, ...] | None = None
    # Go calls on a variable bound to a function's result (``x := F()``,
    # ``x := pkg.F()``): that function, ``F`` or ``pkg.F``, in place of
    # ``receiver_type``; the receiver's type is its first result. None
    # elsewhere.
    receiver_function: str | None = None
    # Go calls ``x.M()`` where nothing in the function or the file declares
    # ``x``: that name, in place of ``receiver_type``, for the resolver to
    # find as a package-level variable of another file of the package, or
    # (with ``receiver_steps``, ``pkg.V.M()``) as the package the file
    # imports as ``x``. None elsewhere.
    receiver_variable: str | None = None
    # Go: how the receiver's type follows from ``receiver_type`` (or
    # ``receiver_function``), one step each: a field name ``f``, a method
    # call ``M()`` (its first result), or ``[]`` (an element of a slice).
    # JavaScript and TypeScript: the property names from ``receiver_type``
    # to the receiver, through the member chain ``x.a.b.m()`` and through
    # declarations ``const y = x.a`` or ``const { a } = x`` that ``y.m()``
    # follows. None when the receiver is ``x`` itself.
    receiver_steps: tuple[str, ...] | None = None
    # JavaScript and TypeScript calls of a plain name, and of ``x.m``: the
    # start (line, column) of the declaration of that name (of ``x``) the
    # call sees in its file,
    # following block and function scopes; ``()`` when the file declares
    # none the call sees. None when an import binds the name, or the scopes
    # around the call cannot be read (``with``, a syntax error), and
    # elsewhere.
    binding: tuple[int, ...] | None = None
    # Java calls with an argument that is itself a method call: that call's
    # span (line, column, end line, end column) for each such argument, None
    # for any other argument. None when no argument is a method call.
    argument_calls: tuple[tuple[int, int, int, int] | None, ...] | None = None
    # Java calls with an argument ``X.f`` whose ``X`` no variable or field
    # declared around the call names: (``X``, ``f``) for each such argument,
    # None for any other argument. None when there is no such argument.
    argument_fields: tuple[tuple[str, str] | None, ...] | None = None
    # Java calls ``e.m()`` whose receiver ``e`` is itself a method call or a
    # ``new`` (``a.b().m()``, ``new T().m()``): the span (line, column, end
    # line, end column) of ``e``. Python calls ``x.m()`` on a local whose one
    # binding is ``x = f(...)``: the span of that call. Rust calls ``x.m()`` whose receiver is the
    # result of a call: the span of that call, either the receiver itself
    # (``T::new().m()``, ``x.a().m()``) or the value of the ``let`` that is
    # the innermost declaration of the local ``x`` and binds it alone without
    # a type (``let x = T::new();``). None elsewhere.
    receiver_call: tuple[int, int, int, int] | None = None
    # Java calls ``X.f.m()`` (three or more names): True when no variable or
    # field named ``X`` can be in scope, so ``X`` names a type or package.
    # None elsewhere.
    type_qualified: bool | None = None
    # Java calls ``x.m()`` whose ``receiver_type`` comes from a declaration
    # outside a type around the call that may inherit a field (an anonymous
    # class, an enum constant body, a class with bases): True, and the call
    # binds that declaration only if none of those types inherits a field
    # ``x``. None elsewhere.
    receiver_inherits: bool | None = None
    # Python, JavaScript, TypeScript and Go calls: (line, column) where the
    # last name of the callee starts (``f`` in ``a.b.f()``), where an
    # indexer's occurrence of the called symbol is. None when the callee
    # does not end in a name, and elsewhere.
    callee_at: tuple[int, int] | None = None

    def __post_init__(self):
        if self.callee_at is not None and (
            type(self.callee_at) is not tuple
            or len(self.callee_at) != 2
            or not all(type(v) is int and v >= 0 for v in self.callee_at)
            or self.callee_at[0] < 1
        ):
            raise ValueError("callee_at must be (line, column) or None")
        if self.binding is not None and (
            type(self.binding) is not tuple
            or len(self.binding) not in (0, 2)
            or not all(type(v) is int and v >= 0 for v in self.binding)
            or (self.binding and self.binding[0] < 1)
        ):
            raise ValueError("binding must be (line, column), () or None")
        if self.receiver_binding is not None and (
            self.receiver_type is None
            or type(self.receiver_binding) is not tuple
            or len(self.receiver_binding) not in (0, 2)
            or not all(type(v) is int and v >= 0 for v in self.receiver_binding)
            or (self.receiver_binding and self.receiver_binding[0] < 1)
        ):
            raise ValueError(
                "receiver_binding must be (line, column) or () with a receiver_type"
            )
        if self.receiver_call is not None and (
            type(self.receiver_call) is not tuple
            or len(self.receiver_call) != 4
            or not all(type(v) is int and v >= 0 for v in self.receiver_call)
        ):
            raise ValueError("receiver_call must be a span of four integers")
        if self.argument_calls is not None and (
            type(self.argument_calls) is not tuple
            or len(self.argument_calls) != self.arguments
            or all(span is None for span in self.argument_calls)
            or not all(
                span is None
                or (
                    type(span) is tuple
                    and len(span) == 4
                    and all(type(v) is int and v >= 0 for v in span)
                )
                for span in self.argument_calls
            )
        ):
            raise ValueError("argument_calls must give a span for some argument")
        if self.argument_fields is not None and (
            type(self.argument_fields) is not tuple
            or len(self.argument_fields) != self.arguments
            or all(field is None for field in self.argument_fields)
            or not all(
                field is None
                or (
                    type(field) is tuple
                    and len(field) == 2
                    and all(isinstance(v, str) and v for v in field)
                )
                for field in self.argument_fields
            )
        ):
            raise ValueError("argument_fields must name a field for some argument")
        if self.receiver_type is not None and (
            not isinstance(self.receiver_type, str) or not self.receiver_type
        ):
            raise ValueError("receiver_type must be a nonempty string or None")
        if self.receiver_function is not None and (
            not isinstance(self.receiver_function, str)
            or not self.receiver_function
            or self.receiver_type is not None
        ):
            raise ValueError(
                "receiver_function must be a nonempty string, without a type"
            )
        if self.receiver_variable is not None and (
            not isinstance(self.receiver_variable, str)
            or not self.receiver_variable
            or self.receiver_type is not None
            or self.receiver_function is not None
        ):
            raise ValueError(
                "receiver_variable must be a nonempty string, without a type"
            )
        if self.receiver_steps is not None and (
            (
                self.receiver_type is None
                and self.receiver_function is None
                and self.receiver_variable is None
            )
            or type(self.receiver_steps) is not tuple
            or not self.receiver_steps
            or not all(isinstance(step, str) and step for step in self.receiver_steps)
        ):
            raise ValueError("receiver_steps must be nonempty strings after a type")
        if self.shadowed not in (None, True):
            raise ValueError("shadowed must be True or None")
        if self.type_qualified not in (None, True):
            raise ValueError("type_qualified must be True or None")
        if self.receiver_inherits not in (None, True) or (
            self.receiver_inherits and self.receiver_type is None
        ):
            raise ValueError("receiver_inherits must be True with a receiver_type")
        if self.argument_types is not None and (
            type(self.argument_types) is not tuple
            or len(self.argument_types) != self.arguments
            or all(t is None for t in self.argument_types)
            or not all(
                t is None or (isinstance(t, str) and t) for t in self.argument_types
            )
        ):
            raise ValueError("argument_types must give a type for some argument")
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
    # Go ``package`` clauses: (name, type) for each package-level ``var``
    # the file declares, the type as for ``Definition.result_type`` (or
    # ``F()``/``pkg.F()``: the first result of that function) when the
    # declaration writes it or a value ``T{...}``, ``&T{...}``, ``new(T)``
    # or a call gives it, else None. None elsewhere, or when there is none.
    variables: tuple[tuple[str, str | None], ...] | None = None

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
        if self.variables is not None and (
            type(self.variables) is not tuple
            or not self.variables
            or not all(
                type(item) is tuple
                and len(item) == 2
                and isinstance(item[0], str)
                and item[0]
                and (item[1] is None or (isinstance(item[1], str) and item[1]))
                for item in self.variables
            )
        ):
            raise ValueError("variables must be (name, type or None) pairs")


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
            if item["constant_body"] is None:
                del item["constant_body"]
            if item["static"] is None:
                del item["static"]
            if item["default_export"] is None:
                del item["default_export"]
            if item["macro_export"] is None:
                del item["macro_export"]
            if item["overload"] is None:
                del item["overload"]
            if item["prototype"] is None:
                del item["prototype"]
            if item["result_type"] is None:
                del item["result_type"]
            if item["fields"] is None:
                del item["fields"]
            if item["underlying_type"] is None:
                del item["underlying_type"]
            if item["properties"] is None:
                del item["properties"]
            if item["export_aliases"] is None:
                del item["export_aliases"]
            if item["signature"] is None:
                del item["signature"]
            if item["embeds"] is None:
                del item["embeds"]
        for item in data["call_sites"]:
            if item["arguments"] is None:
                del item["arguments"]
            if item["argument_types"] is None:
                del item["argument_types"]
            if item["anonymous"] is None:
                del item["anonymous"]
            if item["shadowed"] is None:
                del item["shadowed"]
            if item["receiver_type"] is None:
                del item["receiver_type"]
            if item["receiver_function"] is None:
                del item["receiver_function"]
            if item["receiver_variable"] is None:
                del item["receiver_variable"]
            if item["receiver_steps"] is None:
                del item["receiver_steps"]
            if item["binding"] is None:
                del item["binding"]
            if item["receiver_binding"] is None:
                del item["receiver_binding"]
            if item["argument_calls"] is None:
                del item["argument_calls"]
            if item["receiver_call"] is None:
                del item["receiver_call"]
            if item["callee_at"] is None:
                del item["callee_at"]
            if item["type_qualified"] is None:
                del item["type_qualified"]
            if item["receiver_inherits"] is None:
                del item["receiver_inherits"]
            if item["argument_fields"] is None:
                del item["argument_fields"]
        for item in data["imports"]:
            if item["block"] is None:
                del item["block"]
            if item["exported_names"] is None:
                del item["exported_names"]
            if item["reexported_as"] is None:
                del item["reexported_as"]
        if not self.supertypes and "supertypes" not in self.capabilities:
            # Artifacts without supertype facts keep their serialized identity.
            del data["supertypes"]
        for item in data.get("packages", ()):
            if item["variables"] is None:
                del item["variables"]
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
                        **(
                            {"exported_names": tuple(item["exported_names"])}
                            if isinstance(item.get("exported_names"), list)
                            else {}
                        ),
                        **(
                            {
                                "reexported_as": tuple(
                                    tuple(pair) for pair in item["reexported_as"]
                                )
                            }
                            if isinstance(item.get("reexported_as"), list)
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
                            {"argument_types": tuple(item["argument_types"])}
                            if isinstance(item.get("argument_types"), list)
                            else {}
                        ),
                        **(
                            {"anonymous": tuple(map(tuple, item["anonymous"]))}
                            if isinstance(item.get("anonymous"), list)
                            else {}
                        ),
                        **(
                            {"receiver_steps": tuple(item["receiver_steps"])}
                            if isinstance(item.get("receiver_steps"), list)
                            else {}
                        ),
                        **(
                            {"binding": tuple(item["binding"])}
                            if isinstance(item.get("binding"), list)
                            else {}
                        ),
                        **(
                            {"receiver_call": tuple(item["receiver_call"])}
                            if isinstance(item.get("receiver_call"), list)
                            else {}
                        ),
                        **(
                            {"callee_at": tuple(item["callee_at"])}
                            if isinstance(item.get("callee_at"), list)
                            else {}
                        ),
                        **(
                            {"receiver_binding": tuple(item["receiver_binding"])}
                            if isinstance(item.get("receiver_binding"), list)
                            else {}
                        ),
                        **(
                            {
                                "argument_calls": tuple(
                                    None if span is None else tuple(span)
                                    for span in item["argument_calls"]
                                )
                            }
                            if isinstance(item.get("argument_calls"), list)
                            else {}
                        ),
                        **(
                            {
                                "argument_fields": tuple(
                                    None if field is None else tuple(field)
                                    for field in item["argument_fields"]
                                )
                            }
                            if isinstance(item.get("argument_fields"), list)
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
                PackageClause(
                    **{
                        **item,
                        **(
                            {
                                "variables": tuple(
                                    tuple(v) if isinstance(v, list) else v
                                    for v in item["variables"]
                                )
                            }
                            if isinstance(item.get("variables"), list)
                            else {}
                        ),
                    }
                )
                for item in data.get("packages", [])
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
                        "fields": tuple(tuple(field) for field in item["fields"])
                        if isinstance(item.get("fields"), list)
                        else item.get("fields"),
                        "properties": tuple(item["properties"])
                        if isinstance(item.get("properties"), list)
                        else item.get("properties"),
                        "export_aliases": tuple(item["export_aliases"])
                        if isinstance(item.get("export_aliases"), list)
                        else item.get("export_aliases"),
                        "parameter_types": tuple(item["parameter_types"])
                        if isinstance(item.get("parameter_types"), list)
                        else item.get("parameter_types"),
                        "embeds": tuple(item["embeds"])
                        if isinstance(item.get("embeds"), list)
                        else item.get("embeds"),
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
