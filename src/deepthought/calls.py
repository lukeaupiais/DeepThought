"""Match named call sites to declarations by declared name-binding rules.

These rules read only one AnalysisResult. They match names, so every link
they produce is name_matched: rebinding, shadowing, inheritance and dynamic
dispatch are not checked. Compiler-backed edges come from SCIP instead. A
call that no rule ties to exactly one declaration stays unresolved or
ambiguous, with a reason, and produces no link.
"""

from collections import defaultdict
from dataclasses import dataclass, field, replace
import posixpath

from .imports import (
    PYTHON_FROM_IMPORT_SUBMODULE,
    _java_index,
    _RustIndex,
    resolve_imports,
)
from .models import AnalysisResult, Definition

CALL_LEXICAL_NAME = "tree-sitter.call-lexical-name"
CALL_IMPORTED_NAME = "tree-sitter.call-imported-name"
CALL_MODULE_ATTRIBUTE = "tree-sitter.call-module-attribute"
CALL_SELF_METHOD = "tree-sitter.call-self-method"
CALL_PRIVATE_NAME = "tree-sitter.js-call-private-name"
CALL_PACKAGE_NAME = "tree-sitter.call-package-name"
GO_CALL_CONVERSION = "tree-sitter.go-call-conversion"
GO_CALL_TYPED_RECEIVER = "tree-sitter.go-call-typed-receiver"
CALL_PYTHON_REEXPORT = "tree-sitter.python-call-reexport"
CALL_JS_REEXPORT = "tree-sitter.js-call-reexport"
CALL_JS_STATIC_METHOD = "tree-sitter.js-call-static-method"
CALL_JS_TYPED_RECEIVER = "tree-sitter.js-call-typed-receiver"
CALL_JS_MEMBER_CHAIN = "tree-sitter.js-call-member-chain"
CALL_PYTHON_TYPED_RECEIVER = "tree-sitter.python-call-typed-receiver"
PYTHON_CALL_SUPER = "tree-sitter.python-call-super"
PYTHON_CALL_SELF_INHERITED = "tree-sitter.python-call-self-inherited"

JAVA_ENCLOSING_METHOD = "tree-sitter.java-enclosing-type-method"
JAVA_STATIC_IMPORT = "tree-sitter.java-static-import"
JAVA_TYPE_MEMBER = "tree-sitter.java-type-member"
JAVA_TYPE_NAME = "tree-sitter.java-type-name"
JAVA_INHERITED_METHOD = "tree-sitter.java-inherited-method"
JAVA_TYPED_RECEIVER = "tree-sitter.java-call-typed-receiver"
JAVA_PLATFORM_ARGUMENT = "tree-sitter.java-overload-platform-argument"
JAVA_FIELD_ARGUMENT = "tree-sitter.java-overload-static-field-argument"
RUST_CALL_PATH = "tree-sitter.rust-call-path"
RUST_CALL_LOCAL_ITEM = "tree-sitter.rust-call-local-item"
RUST_CALL_ASSOCIATED = "tree-sitter.rust-call-associated"
RUST_CALL_SELF_METHOD = "tree-sitter.rust-call-self-method"
RUST_IMPL_TYPE = "tree-sitter.rust-impl-type"
RUST_IMPL_TRAIT = "tree-sitter.rust-impl-trait"
# Rust declarations an impl can be written for.
_RUST_TYPES = {"Struct", "Enum", "Union", "TypeAlias"}
# Rust declarations a call can name: functions and tuple structs.
_RUST_CALLABLE = {"Function", "Struct"}
_CALLABLE = {"Function", "Class"}
# Go type declarations a conversion `T(x)` can name; an alias's methods are
# its target's, so a conversion's result is followed only for the others.
_GO_TYPES = {"Type", "Struct", "Interface"}
_GO_CONVERSIONS = _GO_TYPES | {"TypeAlias"}
_METHOD = {"Method", "Function"}


class _Any:
    """Every kind of declaration."""

    def __contains__(self, kind):
        return True


_ANY = _Any()


@dataclass(frozen=True)
class CallResolution:
    call_id: str
    status: str  # linked | ambiguous | unresolved
    target_id: str | None = None
    rule_id: str | None = None
    reason: str | None = None
    candidates: tuple[str, ...] = ()


def _pick(call_id, matches, rule_id, none_reason):
    # `typing.overload` signatures declare call forms; the call runs the
    # declaration they stand beside.
    runs = [d for d in matches if not d.overload]
    ids = tuple(sorted(d.id for d in (runs or matches)))
    if len(ids) == 1:
        return CallResolution(call_id, "linked", ids[0], rule_id)
    if ids:
        return CallResolution(
            call_id, "ambiguous", reason="multiple_declarations", candidates=ids
        )
    return CallResolution(call_id, "unresolved", reason=none_reason)


@dataclass(frozen=True)
class NameBindings:
    """Declarations by (file, symbol) and the names each file's imports bind."""

    by_symbol: dict  # (file, symbol) -> [Definition]
    names: dict  # (file, local) -> [(imported name or None, target files)]
    modules: dict  # (file, dotted prefix) -> [target files]
    # (file, local) -> [(imported name, target files)], for imports outside
    # any function or class body: the module's own attributes.
    module_level: dict = field(default_factory=dict)
    # JavaScript/TypeScript file -> [(exported name, name, target files)],
    # one per name an ``export ... from`` re-exports.
    reexports: dict = field(default_factory=dict)
    # (file, alias) -> [Definition], for JavaScript/TypeScript top-level
    # declarations a local ``export { name as alias }`` exports.
    aliases: dict = field(default_factory=dict)

    def top_level(self, files, name, kinds):
        """Declarations named ``name`` at the top level of ``files``, and
        JavaScript or TypeScript ones they export as ``name`` by a local
        ``export { declared as name }``."""
        return [
            d
            for f in files
            for d in (
                *self.by_symbol.get((f, name), ()),
                *self.aliases.get((f, name), ()),
            )
            if d.kind in kinds
        ]

    def lexical(self, file, scope, name, kinds):
        """Declarations of ``name`` in the innermost enclosing scope declaring it.

        Scopes run from innermost out; class bodies are not enclosing scopes.
        """
        for depth in range(len(scope), -1, -1):
            prefix = ".".join(scope[:depth])
            if prefix and any(
                d.kind == "Class" for d in self.by_symbol.get((file, prefix), ())
            ):
                continue
            symbol = f"{prefix}.{name}" if prefix else name
            found = [
                d for d in self.by_symbol.get((file, symbol), ()) if d.kind in kinds
            ]
            if found:
                return found
        return []


def name_bindings(result: AnalysisResult) -> NameBindings:
    by_symbol = defaultdict(list)
    aliases = defaultdict(list)
    for d in result.definitions:
        by_symbol[(d.file, d.symbol)].append(d)
        for alias in d.export_aliases or ():
            aliases[(d.file, alias)].append(d)
    module_files, submodules = {}, {}
    for resolution in resolve_imports(result):
        module_files[resolution.import_id] = tuple(
            link.target_file
            for link in resolution.links
            if link.rule_id != PYTHON_FROM_IMPORT_SUBMODULE
        )
        submodules[resolution.import_id] = tuple(
            link.target_file
            for link in resolution.links
            if link.rule_id == PYTHON_FROM_IMPORT_SUBMODULE
        )
    python_files = {item.file for item in result.coverage if item.language == "python"}
    declared = {p.file: p.name for p in result.packages if p.kind == "package"}
    # The bodies of each file's functions and classes.
    bodies = defaultdict(list)
    for d in result.definitions:
        if d.kind in {"Function", "Method", "Class"}:
            bodies[d.file].append(((d.line, d.column), (d.end_line, d.end_column)))
    names = defaultdict(list)
    modules = defaultdict(list)
    module_level = defaultdict(list)
    reexports = defaultdict(list)

    def bind_module(file, prefix, targets):
        # The same module bound twice (`import a.b` and `import a.c` both
        # bind `a`) is one binding.
        if targets not in modules[(file, prefix)]:
            modules[(file, prefix)].append(targets)

    pending = []
    for item in result.imports:
        targets = module_files.get(item.id, ())
        if item.exported_names is not None:
            reexports[item.file].extend(
                (exported, name, targets)
                for name, exported in zip(item.names, item.exported_names)
            )
        if item.reexported_as is not None:
            # `import { a as b } from 'm'; export { b as c }` re-exports what
            # `export { a as c } from 'm'` would.
            imported = dict(zip(item.local_names, item.names))
            reexports[item.file].extend(
                (exported, imported[local], targets)
                for local, exported in item.reexported_as
            )
        local_names = item.local_names
        if not local_names and not item.names and targets:
            # A Go import without an alias binds the package's declared name.
            local_names = tuple(
                sorted({declared[f] for f in targets if f in declared})[:1]
            )
        if item.kind == "import" and not item.names:
            for local in local_names:
                prefix = (
                    item.specifier if local == item.specifier.split(".")[0] else local
                )
                bind_module(item.file, prefix, targets)
                names[(item.file, local)].append((None, targets))
                if (
                    item.file in python_files
                    and prefix == item.specifier
                    and len(targets) == 1
                ):
                    # Python: `import a.b.c` binds `a`, and `a.b` names its
                    # attribute: each package of the path, by the
                    # `__init__.py` above the module imported.
                    parts = item.specifier.split(".")
                    package = posixpath.dirname(targets[0])
                    if targets[0].endswith("/__init__.py"):
                        package = posixpath.dirname(package)
                    for depth in range(len(parts) - 1, 0, -1):
                        init = f"{package}/__init__.py"
                        if init not in python_files:
                            break
                        bind_module(item.file, ".".join(parts[:depth]), (init,))
                        package = posixpath.dirname(package)
        for name, local in zip(item.names, item.local_names):
            if name == "*" and local != "*":
                modules[(item.file, local)].append(targets)
                names[(item.file, local)].append((None, targets))
            elif name != "*":
                names[(item.file, local)].append((name, targets))
                if not any(
                    start <= (item.line, item.column) < end
                    for start, end in bodies[item.file]
                ):
                    module_level[(item.file, local)].append((name, targets))
                pending.append((item, name, local, targets))
    # Python `from pkg import m` where m is a submodule of pkg that pkg's
    # `__init__.py` neither declares nor imports: m is that module.
    for item, name, local, targets in pending:
        found = [
            f
            for f in submodules.get(item.id, ())
            if f.endswith((f"/{name}.py", f"/{name}/__init__.py"))
            or f in (f"{name}.py", f"{name}/__init__.py")
        ]
        if (
            len(found) != 1
            or item.file not in python_files
            or any(by_symbol.get((t, name)) or names.get((t, name)) for t in targets)
        ):
            continue
        bind_module(item.file, local, (found[0],))
    return NameBindings(by_symbol, names, modules, module_level, reexports, aliases)


def package_peers(result: AnalysisResult) -> dict:
    """The files of each file's package: same directory, same package clause.

    This is Go's package scope; a file's external test package (``x_test``) is
    a different package. Build constraints are not evaluated, so files for
    different platforms are all peers.
    """
    groups = defaultdict(list)
    for clause in result.packages:
        if clause.kind != "package":
            continue
        groups[(posixpath.dirname(clause.file), clause.name)].append(clause.file)
    return {file: tuple(sorted(files)) for files in groups.values() for file in files}


_JAVA_TYPES = {"Class", "Interface", "Enum", "Record", "Annotation"}
_JAVA_CLASSES = {"Class", "Enum", "Record"}


@dataclass(frozen=True)
class _Member:
    """A method found on a type, and whether the type inherits it."""

    definition: Definition
    inherited: bool

    @property
    def id(self):
        return self.definition.id

    @property
    def parameters(self):
        return self.definition.parameters

    @property
    def variadic(self):
        return self.definition.variadic


def _shape(method):
    """What decides whether a Java method overrides another: its parameter
    count, variable arity and parameter types as written."""
    return (method.parameters, method.variadic, method.definition.parameter_types)


def _may_override(near, far):
    """Whether a parameter of type ``near`` as written may override one of
    type ``far``: the same type, where a simple name may be an imported
    qualified one (``Map.Entry`` and ``Entry``). A type parameter of a class
    (``*``) matches only another: ``write(JsonElement)`` may override
    ``write(T)`` in ``TypeAdapter<T>``, but ``getOrDefault(..., String)``
    overloads ``getOrDefault(..., T)`` when ``T`` is bound to an enum, and
    telling the two apart needs the type arguments of every base."""
    near_base, far_base = near.rstrip("[]."), far.rstrip("[].")
    if near[len(near_base) :] != far[len(far_base) :]:
        return False  # different array dimensions or variable arity
    if "*" in {near_base, far_base}:
        return near_base == far_base
    return near_base.split(".")[-1] == far_base.split(".")[-1]


def _overridden(method, shapes):
    """Whether a nearer method with one of ``shapes`` overrides ``method``:
    the same parameter count and variable arity, and parameter types that
    may be the same (``_may_override``). Otherwise the methods may be
    overloads (``putBytes(byte[])`` and ``putBytes(ByteBuffer)``), and
    both stay."""
    count, variadic, types = _shape(method)
    return any(
        (c, v) == (count, variadic)
        and (t == types if None in (t, types) else all(map(_may_override, t, types)))
        for c, v, t in shapes
    )


_JAVA_WIDENING = {
    "byte": {"short", "int", "long", "float", "double"},
    "short": {"int", "long", "float", "double"},
    "char": {"int", "long", "float", "double"},
    "int": {"long", "float", "double"},
    "long": {"float", "double"},
    "float": {"double"},
    "double": set(),
    "boolean": set(),
}
_JAVA_BOXES = {
    "boolean": "Boolean",
    "byte": "Byte",
    "char": "Character",
    "short": "Short",
    "int": "Integer",
    "long": "Long",
    "float": "Float",
    "double": "Double",
}
_JAVA_UNBOXES = {box: primitive for primitive, box in _JAVA_BOXES.items()}
_JAVA_NUMBER = {"Number", "Comparable", "Serializable", "Object", "Constable"}
# Every supertype of the java.lang types a literal or boxing gives.
_JAVA_LANG = {
    "Object": {"Object"},
    "String": {
        "String",
        "Object",
        "CharSequence",
        "Comparable",
        "Serializable",
        "Constable",
        "ConstantDesc",
    },
    "Class": {
        "Class",
        "Object",
        "Type",
        "Serializable",
        "GenericDeclaration",
        "AnnotatedElement",
        "TypeDescriptor",
        "OfField",
    },
    "Boolean": {"Boolean", "Object", "Comparable", "Serializable", "Constable"},
    "Character": {"Character", "Object", "Comparable", "Serializable", "Constable"},
    "Byte": {"Byte", *_JAVA_NUMBER},
    "Short": {"Short", *_JAVA_NUMBER},
    "Integer": {"Integer", "ConstantDesc", *_JAVA_NUMBER},
    "Long": {"Long", "ConstantDesc", *_JAVA_NUMBER},
    "Float": {"Float", "ConstantDesc", *_JAVA_NUMBER},
    "Double": {"Double", "ConstantDesc", *_JAVA_NUMBER},
}


def _java_split(written):
    """A Java type as written: its element type and array dimensions."""
    base = written.removesuffix("...").rstrip("[]")
    return base, (len(written) - len(base)) // 2


class _JavaScope:
    """Java name lookup: enclosing types, then imports, then the package."""

    def __init__(self, result, by_symbol):
        self.by_symbol = by_symbol
        self.roots, self.packages, self.types = _java_index(result)
        self.package = {
            p.file: p.name
            for p in result.packages
            # Only Java files in a source root, which declare one package.
            if p.file in self.roots
        }
        owners = {d.id: d for d in result.definitions}
        # (file, type symbol) -> base names, a class's superclass first.
        self.bases = defaultdict(list)
        for item in sorted(
            result.supertypes,
            key=lambda i: (i.owner_id, i.line, i.column),
        ):
            owner = owners.get(item.owner_id)
            if item.file in self.roots and owner is not None:
                self.bases[(owner.file, owner.symbol)].append(item.name)
        self._members = {}  # (file, type symbol, name) -> [_Member]
        self.definitions = {d.id: d for d in result.definitions}
        # Call sites by (file, span), and the return types of what they link.
        self.spans = {
            (s.file, s.line, s.column, s.end_line, s.end_column): s
            for s in result.call_sites
        }
        self._results = {}
        # Whether subtype checks may use that JDK types are no repository
        # subtypes (see ``methods``).
        self._platform = False
        # Whether an argument ``X.f`` takes the type of the static field
        # ``f`` of the type ``X`` (see ``methods``).
        self._fields = False
        self._supertypes = {}
        self.single = defaultdict(list)  # (file, simple name) -> type FQNs
        self.on_demand = defaultdict(list)  # file -> package or type names
        self.static = defaultdict(list)  # (file, member) -> type FQNs
        self.static_all = defaultdict(list)  # file -> type FQNs
        for item in result.imports:
            if item.file not in self.roots:
                continue
            if item.names == ():
                self.single[(item.file, item.local_names[0])].append(item.specifier)
            elif item.names == ("*",):
                self.on_demand[item.file].append(item.specifier)
                self.static_all[item.file].append(item.specifier)
            else:
                self.static[(item.file, item.names[0])].append(item.specifier)

    def _defs(self, file, symbol, kinds):
        return [d for d in self.by_symbol.get((file, symbol), ()) if d.kind in kinds]

    def qualified(self, name, root):
        """Type declarations of a fully qualified type name, own root first."""
        by_root = self.types.get(name)
        if not by_root:
            return []
        # Another root's copy only when the importer's root has none; copies
        # in several other roots are all candidates (only the build decides).
        files = by_root.get(root) or [f for fs in by_root.values() for f in fs]
        found = []
        for file in sorted(set(files)):
            symbol = name[len(self.package[file]) + 1 :]
            found.extend(self._defs(file, symbol, _JAVA_TYPES))
        return found

    def member(self, file, symbol, name, seen):
        """Member types ``name`` of a type: declared, else inherited."""
        found = self._defs(file, f"{symbol}.{name}", _JAVA_TYPES)
        if found or (file, symbol) in seen:
            return found
        seen = seen | {(file, symbol)}
        # A type's bases are named in the scope that encloses it.
        outer = symbol.split(".")[:-1]
        for base in self.bases.get((file, symbol), ()):
            for d in self.type(file, outer, base, seen):
                found.extend(
                    n
                    for n in self.member(d.file, d.symbol, name, seen)
                    if n not in found
                )
        return found

    def type(self, file, scope, name, seen=frozenset()):
        """Declarations a type name denotes from a file and scope."""
        first, *rest = name.split(".")
        root = self.roots.get(file)
        found = []
        for depth in range(len(scope), -1, -1):
            prefix = ".".join(scope[:depth])
            if prefix:
                # A type's members, declared or inherited; a method's local
                # classes (a method has no bases).
                found = self.member(file, prefix, first, seen)
            else:  # top level
                found = self._defs(file, first, _JAVA_TYPES)
            if found:
                break
        # A single-type import binds its name even to a type outside the
        # repository, shadowing the package and every later lookup.
        imported = not found and bool(self.single.get((file, first)))
        if imported:
            for spec in self.single[(file, first)]:
                found.extend(self.qualified(spec, root))
        elif not found and root is not None:
            # A static import can name a member type; a method it names
            # (or a type outside the repository) does not stop the lookup.
            for spec in self.static.get((file, first), ()):
                for d in self.qualified(spec, root):
                    found.extend(self.member(d.file, d.symbol, first, seen))
            package = self.package.get(file)
            if not found and package is not None:
                found = self.qualified(f"{package}.{first}", root)
            if not found:
                for spec in self.on_demand.get(file, ()):
                    found.extend(self.qualified(f"{spec}.{first}", root))
        if not found and rest and root is not None and not imported:
            # A fully qualified name: the longest package prefix that names it.
            parts = name.split(".")
            for size in range(len(parts), 1, -1):
                found = self.qualified(".".join(parts[:size]), root)
                if found:
                    rest = parts[size:]
                    break
            else:
                return []
        for part in rest:
            found = [
                n for d in found for n in self.member(d.file, d.symbol, part, seen)
            ]
        return found

    @staticmethod
    def arity(site, methods):
        """Methods whose parameters accept the call's argument count."""
        if site.arguments is None:
            return methods
        n = site.arguments
        return [
            m
            for m in methods
            if m.parameters is None
            or n == m.parameters
            or (m.variadic and n >= m.parameters - 1)
        ]

    def methods(self, site, candidates, rule_id, none_reason, scope=()):
        """Link among same-named methods by arity, then by the argument
        types written in the call (``overloads``); none fits: unresolved.

        A link to a method the type inherits rather than declares carries
        the inherited-method rule.
        """
        if not candidates:
            return CallResolution(site.id, "unresolved", reason=none_reason)
        declared = {m.id for m in candidates if not m.inherited}
        fitting = self.arity(site, candidates)
        chosen = self.overloads(site, scope, fitting)
        if len(chosen) > 1:
            # Only the rule that JDK types are no repository subtypes may
            # tell the overloads apart; a link it decides carries its rule.
            self._platform = True
            try:
                narrowed = self.overloads(site, scope, fitting)
            finally:
                self._platform = False
            if len(narrowed) == 1:
                return CallResolution(
                    site.id, "linked", narrowed[0].definition.id, JAVA_PLATFORM_ARGUMENT
                )
        if len(chosen) > 1 and site.argument_fields:
            # Only the types of static fields named as arguments may tell
            # the overloads apart; a link they decide carries their rule.
            self._fields = True
            try:
                narrowed = self.overloads(site, scope, fitting)
            finally:
                self._fields = False
            if len(narrowed) == 1:
                return CallResolution(
                    site.id, "linked", narrowed[0].definition.id, JAVA_FIELD_ARGUMENT
                )
        resolution = _pick(
            site.id,
            [m.definition for m in chosen],
            rule_id,
            "no_matching_arity",
        )
        if resolution.status == "linked" and resolution.target_id not in declared:
            return CallResolution(
                site.id, "linked", resolution.target_id, JAVA_INHERITED_METHOD
            )
        return resolution

    def supertypes(self, file, scope, written):
        """(simple names of a type and of every supertype, whether that set
        is complete) for a class type written in ``file`` at ``scope``; None
        when the name denotes more than one type."""
        key = (file, tuple(scope), written)
        if key in self._supertypes:
            return self._supertypes[key]
        simple = written.split(".")[-1]
        found = self.type(file, scope, written)
        if len(found) > 1:
            result = None
        elif not found:
            known = written in {simple, f"java.lang.{simple}"} and simple in _JAVA_LANG
            result = (
                (frozenset(_JAVA_LANG[simple]), True)
                if known
                else (frozenset({simple, "Object"}), False)
            )
        else:
            names, complete, stack, seen = {"Object"}, True, [found[0]], set()
            while stack:
                d = stack.pop()
                if d.id in seen:
                    continue
                seen.add(d.id)
                names.add(d.symbol.split(".")[-1])
                if d.kind in {"Enum", "Record"}:
                    complete = False  # Enum or Record, and what they implement
                outer = d.symbol.split(".")[:-1]
                for base in self.bases.get((d.file, d.symbol), ()):
                    bases = self.type(d.file, outer, base)
                    if len(bases) == 1:
                        stack.append(bases[0])
                        continue
                    last = base.split(".")[-1]
                    names.add(last)
                    if bases or last not in _JAVA_LANG:
                        complete = False
                    else:
                        names |= _JAVA_LANG[last]
            result = (frozenset(names), complete)
        self._supertypes[key] = result
        return result

    def subtype(self, near, near_at, far, far_at, phase=1):
        """Whether the type ``near`` (an argument's, or a parameter's) is
        assignable to the parameter type ``far`` in an overload phase: 1
        without boxing, 2 with it (JLS 15.12.2). True, False, or None when
        that is not known. ``*_at`` is the (file, scope) a type is written
        in; a type parameter (``*``, ``?``) has no known bounds."""
        if near == "null":
            return far not in _JAVA_WIDENING
        near_base, near_dims = _java_split(near)
        far_base, far_dims = _java_split(far)
        if "*" in near_base or "?" in near_base:
            return None
        if "*" in far_base or "?" in far_base:
            if far_dims or near_dims:
                return None
            if near_base in _JAVA_WIDENING:
                return None if phase > 1 else False
            return None
        if near_dims or far_dims:
            if near_dims == far_dims:
                if near_base in _JAVA_WIDENING or far_base in _JAVA_WIDENING:
                    return near_base == far_base
                return self.subtype(near_base, near_at, far_base, far_at)
            if near_dims > far_dims and far_dims == 0:
                if far_base.split(".")[-1] in {"Object", "Cloneable", "Serializable"}:
                    return True
                return None if far_base not in _JAVA_WIDENING else False
            return None if near_dims > far_dims else False
        if near_base in _JAVA_WIDENING:
            if far_base in _JAVA_WIDENING:
                return far_base == near_base or far_base in _JAVA_WIDENING[near_base]
            if phase == 1:
                return False
            return far_base.split(".")[-1] in _JAVA_LANG[_JAVA_BOXES[near_base]]
        known = self.supertypes(*near_at, near_base)
        if far_base in _JAVA_WIDENING:
            if phase == 1:
                return False
            if known is not None and known[1]:
                primitive = _JAVA_UNBOXES.get(near_base.split(".")[-1])
                if primitive is None:
                    return False
                return far_base == primitive or far_base in _JAVA_WIDENING[primitive]
            return None
        if known is None:
            return None
        names, complete = known
        if far_base.split(".")[-1] in names:
            return True
        if (
            self._platform
            and not complete
            and self.external(*near_at, near_base)
            and self.type(*far_at, far_base)
        ):
            # A JDK type cannot extend or implement a repository type.
            return False
        return False if complete else None

    def external(self, file, scope, written):
        """Whether a type name that no repository declaration in scope
        denotes is a Java platform type: a single-type import from a
        ``java.`` package, which only the JDK may define (the JVM refuses a
        class in a ``java.`` package from any other class loader)."""
        specs = self.single.get((file, written.split(".")[0]))
        return bool(specs) and all(spec.startswith("java.") for spec in specs)

    def static_field(self, file, scope, written, name):
        """(type, (file, scope) it is written in) of the static field
        ``name`` that the type name ``written`` declares, seen from a file
        and scope; None when the name denotes no single repository type or
        that type declares no such field with a recorded type."""
        found = self.type(file, scope, written)
        if len(found) != 1 or not found[0].fields:
            return None
        typed = dict(found[0].fields).get(name)
        if typed is None:
            return None
        return typed, (found[0].file, tuple(found[0].symbol.split(".")))

    def call_result(self, file, span):
        """(return type, (file, scope) it is written in) of the method that
        the Java call at ``span`` in ``file`` links to; None when that call
        is not linked or its method's return type is not recorded."""
        site = self.spans.get((file, *span))
        if site is None:
            return None
        if site.id not in self._results:
            caller = self.definitions.get(site.caller_id)
            scope = caller.symbol.split(".") if caller is not None else []
            # Resolved first by the pass without the platform rule.
            target = self.definitions.get(self.call(site, scope).target_id)
            self._results[site.id] = (
                (
                    target.result_type,
                    (target.file, tuple(target.symbol.split(".")[:-1])),
                )
                if target is not None and target.result_type is not None
                else None
            )
        return self._results[site.id]

    def overloads(self, site, scope, methods):
        """The methods among ``methods`` that the call's written argument
        types may select, as javac does (JLS 15.12.2): the first phase
        (strict, then with boxing, then variable arity) with an applicable
        method, then the most specific of them. Only methods known to be
        the choice are returned; otherwise ``methods`` is returned."""
        if len(methods) < 2 or not (
            site.argument_types
            or site.argument_calls
            or (self._fields and site.argument_fields)
        ):
            return methods
        at = (site.file, tuple(scope))
        arguments = list(site.argument_types or (None,) * site.arguments)
        ats = [at] * len(arguments)
        for i, span in enumerate(site.argument_calls or ()):
            if span is not None:
                # An argument that is a method call has the type its linked
                # method returns, written where that method is declared.
                found = self.call_result(site.file, span)
                if found is not None:
                    arguments[i], ats[i] = found
        for i, named in enumerate((self._fields and site.argument_fields) or ()):
            if named is not None:
                # ``X.f`` with no variable ``X`` in scope: the static field
                # ``f`` of the type ``X``, typed where ``X`` declares it.
                found = self.static_field(site.file, scope, *named)
                if found is not None:
                    arguments[i], ats[i] = found
        top = scope[0] if scope else None
        visible = [
            m
            for m in methods
            # A private method is accessible only in its top-level class.
            if not m.definition.private
            or (m.definition.file, m.definition.symbol.split(".")[0])
            == (site.file, top)
        ]

        def parameters(m, phase):
            types = m.definition.parameter_types
            if types is None:
                return None
            if phase < 3:
                return list(types) if len(types) == len(arguments) else []
            # A fixed-arity method that got here did not apply in phase 2.
            if len(arguments) < len(types) - 1:
                return []
            last = types[-1].removesuffix("...")
            return [*types[:-1], *[last] * (len(arguments) - len(types) + 1)]

        def applicable(m, phase):
            types = parameters(m, phase)
            if types is None:
                return None
            if not types and arguments:
                return False
            owner = (m.definition.file, tuple(m.definition.symbol.split(".")[:-1]))
            checks = [
                None if a is None else self.subtype(a, a_at, t, owner, min(phase, 2))
                for a, a_at, t in zip(arguments, ats, types)
            ]
            if False in checks:
                return False
            return True if all(checks) else None

        def more_specific(m, other):
            near_at = (m.definition.file, tuple(m.definition.symbol.split(".")[:-1]))
            far_at = (
                other.definition.file,
                tuple(other.definition.symbol.split(".")[:-1]),
            )
            return all(
                self.subtype(p, near_at, q, far_at) is True
                for p, q in zip(parameters(m, 1), parameters(other, 1))
            )

        outcomes = [[(m, applicable(m, phase)) for m in visible] for phase in (1, 2, 3)]
        for phase, outcome in enumerate(outcomes, start=1):
            definite = [m for m, ok in outcome if ok is True]
            maybe = [m for m, ok in outcome if ok is None]
            if not definite and not maybe:
                continue
            if not definite:
                # Whether this phase applies is not known: any method
                # possible here or later may be the one.
                possible = {
                    m.id: m
                    for later in outcomes[phase - 1 :]
                    for m, ok in later
                    if ok is not False
                }
                return list(possible.values()) if len(possible) == 1 else methods
            if phase == 3:
                return definite if len(definite) == 1 and not maybe else methods
            pool = definite + maybe
            best = [
                m
                for m in definite
                if all(more_specific(m, o) for o in pool if o is not m)
            ]
            return best if len(best) == 1 else methods
        return methods

    def members(self, file, symbol, name):
        """Methods ``name`` of a type: declared, then inherited through its
        declared bases unless a nearer method with the same parameter count
        overrides them (argument types are not compared)."""
        key = (file, symbol, name)
        if key not in self._members:
            self._members[key] = self._inherit(file, symbol, name, frozenset())
        return self._members[key]

    def _inherit(self, file, symbol, name, seen):
        found = [
            _Member(m, False) for m in self._defs(file, f"{symbol}.{name}", {"Method"})
        ]
        if (file, symbol) in seen:
            return found
        seen = seen | {(file, symbol)}
        shapes = {_shape(m) for m in found}
        outer = symbol.split(".")[:-1]
        inherited = {}
        # Bases in source order: a class's superclass comes first.
        for base in self.bases.get((file, symbol), ()):
            for t in self.type(file, outer, base):
                for m in self._inherit(t.file, t.symbol, name, seen):
                    # A private method is not inherited.
                    if not m.definition.private and not _overridden(m, shapes):
                        inherited.setdefault(m.id, _Member(m.definition, True))
            # A method declared in a class (the superclass chain) implements
            # a same-shaped interface method rather than competing with it;
            # interface methods do not hide each other by name.
            shapes |= {
                _shape(m)
                for m in inherited.values()
                if self._defs(
                    m.definition.file,
                    m.definition.symbol.rpartition(".")[0],
                    _JAVA_CLASSES,
                )
            }
        return found + list(inherited.values())

    def anonymous_members(self, file, anonymous, name):
        """Methods ``name`` of an anonymous class: declared in its body, then
        inherited from its base (unless a body method of the same shape
        overrides them, and except private ones). A base outside the
        repository adds none."""
        base, scope, first, last = anonymous
        found = [
            _Member(m, False)
            for m in self._defs(file, f"{scope}.{name}", {"Method"})
            if first <= m.line <= last
        ]
        shapes = {_shape(m) for m in found}
        inherited = {}
        for t in self.type(file, scope.split("."), base):
            for m in self.members(t.file, t.symbol, name):
                if not m.definition.private and not _overridden(m, shapes):
                    inherited.setdefault(m.id, _Member(m.definition, True))
        return found + list(inherited.values())

    def enclosing_members(self, site, scope, name):
        """Methods ``name`` of each class around a call, innermost first:
        the named types of its scope and the anonymous classes it is in,
        ordered by how their spans nest."""
        classes = [
            (
                d.line,
                d.end_line,
                lambda owner=owner: self.members(site.file, owner, name),
            )
            for owner in self.enclosing_types(site.file, scope)
            for d in self._defs(site.file, owner, _JAVA_TYPES)[:1]
        ] + [
            (a[2], a[3], lambda a=a: self.anonymous_members(site.file, a, name))
            for a in site.anonymous or ()
        ]
        for _, _, methods in sorted(classes, key=lambda c: (-c[0], c[1])):
            yield methods()

    def enclosing_types(self, file, scope):
        for depth in range(len(scope), 0, -1):
            prefix = ".".join(scope[:depth])
            if self._defs(file, prefix, _JAVA_TYPES):
                yield prefix

    def constructor(self, site, scope, resolution):
        """A ``new T(...)`` linked to a type that declares constructors
        links to the one the argument types select (``overloads``), which is
        what the construction runs; otherwise it keeps the type."""
        if resolution.status != "linked":
            return resolution
        owner = self.definitions[resolution.target_id]
        declared = [
            _Member(d, False)
            for d in self._defs(
                owner.file,
                f"{owner.symbol}.{owner.symbol.split('.')[-1]}",
                {"Constructor"},
            )
        ]
        chosen = self.overloads(site, scope, self.arity(site, declared))
        if len(chosen) == 1:
            return replace(resolution, target_id=chosen[0].id)
        return resolution

    def call(self, site, scope):
        parts = site.callee.split(".")
        file = site.file
        if site.kind == "new":
            resolution = _pick(
                site.id,
                self.type(file, scope, site.callee),
                JAVA_TYPE_NAME,
                "type_not_declared",
            )
            return self.constructor(site, scope, resolution)
        name = parts[-1]
        if len(parts) == 1:
            for methods in self.enclosing_members(site, scope, name):
                if methods:
                    # Java stops at the innermost type with a method of that
                    # name, declared or inherited.
                    return self.methods(
                        site,
                        methods,
                        JAVA_ENCLOSING_METHOD,
                        "no_declaration",
                        scope=scope,
                    )
            root = self.roots.get(file)
            imported = (
                [
                    m
                    for spec in self.static.get((file, name), [])
                    or self.static_all.get(file, [])
                    for t in self.qualified(spec, root)
                    for m in self.members(t.file, t.symbol, name)
                ]
                if root is not None
                else []
            )
            if imported:
                return self.methods(
                    site, imported, JAVA_STATIC_IMPORT, "no_declaration", scope=scope
                )
            return CallResolution(site.id, "unresolved", reason="unbound_name")
        head = ".".join(parts[:-1])
        if head == "this":
            innermost = next(self.enclosing_members(site, scope, name), None)
            if innermost is None:
                return CallResolution(site.id, "unresolved", reason="outside_class")
            return self.methods(
                site, innermost, CALL_SELF_METHOD, "not_declared_in_class", scope=scope
            )
        if site.receiver_type is not None:
            # A variable or field whose declaration writes its type.
            owners = self.type(file, scope, site.receiver_type)
            if not owners:
                return CallResolution(
                    site.id, "unresolved", reason="receiver_type_not_declared"
                )
            return self.methods(
                site,
                [m for t in owners for m in self.members(t.file, t.symbol, name)],
                JAVA_TYPED_RECEIVER,
                "not_declared_in_type",
                scope=scope,
            )
        owners = self.type(file, scope, head)
        if not owners:
            return CallResolution(site.id, "unresolved", reason="attribute_call")
        return self.methods(
            site,
            [m for t in owners for m in self.members(t.file, t.symbol, name)],
            JAVA_TYPE_MEMBER,
            "not_declared_in_type",
            scope=scope,
        )


class _RustPaths:
    """Rust paths in item position (a call's path, an impl's type and trait):
    read from the module they are written in as a use path is, after the
    items and uses of the function bodies around them."""

    def __init__(self, result: AnalysisResult, by_symbol):
        files = {item.file for item in result.coverage}
        languages = {item.file: item.language for item in result.coverage}
        self.index = _RustIndex(result, files, languages)
        self.by_symbol = by_symbol
        self.spans = defaultdict(list)
        for d in result.definitions:
            if languages.get(d.file) == "rust":
                self.spans[d.file].append(d)
        self.impls = defaultdict(list)
        for item in result.impls:
            self.impls[item.file].append(item)
        self._types, self._members, self._aliased = {}, None, set()
        self._definitions = {d.id: d for d in result.definitions}
        self._kinds = {d.id: d.kind for d in result.definitions}

    def impl_type(self, item):
        """The declaration an impl is written for."""
        if item.id not in self._types:
            self._types[item.id] = (
                CallResolution(item.id, "unresolved", reason="generic_parameter")
                if item.type in item.generics
                else self.resolve(
                    item.id,
                    item,
                    item.type.split("::"),
                    _RUST_TYPES,
                    RUST_IMPL_TYPE,
                    RUST_IMPL_TYPE,
                )
            )
        return self._types[item.id]

    def impl_trait(self, item):
        """The trait declaration an impl implements, or None for an inherent
        impl."""
        if item.trait is None:
            return None
        key = (item.id, "trait")
        if key not in self._types:
            self._types[key] = self.resolve(
                item.id,
                item,
                item.trait.split("::"),
                {"Trait"},
                RUST_IMPL_TRAIT,
                RUST_IMPL_TRAIT,
            )
        return self._types[key]

    def _trait_method(self, trait_id, name):
        trait = self._definitions[trait_id]
        return [
            d
            for d in self.by_symbol.get((trait.file, f"{trait.symbol}.{name}"), ())
            if d.kind == "Method"
        ]

    def _innermost_trait(self, site):
        """The trait declaration around ``site`` when no impl inside it is."""
        around = [
            d
            for d in self.spans[site.file]
            if d.kind == "Trait" and self._contains(d, site)
        ]
        if not around:
            return None
        trait = max(around, key=lambda d: (d.line, d.column))
        if any(
            self._contains(i, site) and (i.line, i.column) > (trait.line, trait.column)
            for i in self.impls[site.file]
        ):
            return None
        return trait

    def _innermost_impl(self, site):
        """The impl block around ``site``, or None outside one or inside a
        trait written in it."""
        around = [i for i in self.impls[site.file] if self._contains(i, site)]
        if not around:
            return None
        item = max(around, key=lambda i: (i.line, i.column))
        if any(
            d.kind == "Trait"
            and self._contains(d, site)
            and (d.line, d.column) > (item.line, item.column)
            for d in self.spans[site.file]
        ):
            return None
        return item

    def members(self):
        """Type declaration ID -> [(impl, functions it declares)]: the
        functions an impl body declares, by the type the impl resolves to."""
        if self._members is None:
            self._members = defaultdict(list)
            for file, items in sorted(self.impls.items()):
                declared = defaultdict(list)
                for d in self.spans[file]:
                    if d.kind != "Method":
                        continue
                    item = self._innermost_impl(d)
                    # A method qualified by the impl's type, not one of a
                    # trait declared inside it.
                    if item is not None and d.symbol.split(".")[-2:-1] == [
                        item.type.split("::")[-1]
                    ]:
                        declared[item.id].append(d)
                for item in items:
                    kind = self.impl_type(item)
                    if kind.status != "linked":
                        continue
                    if self._kinds[kind.target_id] == "TypeAlias":
                        # The aliased type is not read: an impl for an alias
                        # may declare a name another impl of that type does.
                        self._aliased |= {
                            d.symbol.split(".")[-1] for d in declared[item.id]
                        }
                    self._members[kind.target_id].append((item, declared[item.id]))
        return self._members

    def _associated(self, site, owners, name, rule):
        """A function ``name`` the impls of ``owners`` declare: an inherent
        impl's, else a trait impl's, or the trait's own (a provided method
        the impl does not override). A type alias's impls are not read as
        its aliased type's, so a name an impl for an alias declares is not
        taken from a trait."""
        if any(self._kinds[owner] == "TypeAlias" for owner in owners):
            return CallResolution(site.id, "unresolved", reason="type_alias")
        members = self.members()
        found = [
            (item.trait is not None, d)
            for owner in owners
            for item, functions in members.get(owner, ())
            for d in functions
            if d.symbol.split(".")[-1] == name
        ]
        inherent = [d for trait, d in found if not trait]
        if inherent:
            return _pick(site.id, inherent, rule, "no_declaration")
        provided = []
        for owner in owners:
            for item, functions in members.get(owner, ()):
                trait = self.impl_trait(item)
                if (
                    trait is not None
                    and trait.status == "linked"
                    and not any(d.symbol.split(".")[-1] == name for d in functions)
                ):
                    provided.extend(self._trait_method(trait.target_id, name))
        candidates = [d for _, d in found] + provided
        if candidates and name in self._aliased:
            return CallResolution(site.id, "unresolved", reason="type_alias")
        if candidates:
            return _pick(
                site.id, {d.id: d for d in candidates}.values(), rule, "no_declaration"
            )
        return CallResolution(site.id, "unresolved", reason="associated_item")

    @staticmethod
    def _contains(d, site):
        return (d.line, d.column) <= (site.line, site.column) and (
            site.end_line,
            site.end_column,
        ) <= (d.end_line, d.end_column)

    def _scope(self, site):
        """The inline modules around the call, or None inside a module
        written in a function body."""
        modules = [
            d
            for d in self.spans[site.file]
            if d.kind == "Module" and self._contains(d, site)
        ]
        if not modules:
            return ()
        innermost = max(modules, key=lambda d: (d.line, d.column))
        parts = tuple(innermost.symbol.split("."))
        for depth in range(1, len(parts)):
            if not any(
                d.kind == "Module"
                for d in self.by_symbol.get((site.file, ".".join(parts[:depth])), ())
            ):
                return None
        return parts

    def _bodies(self, site):
        """Functions and methods around the call, innermost first."""
        return sorted(
            (
                d
                for d in self.spans[site.file]
                if d.kind in {"Function", "Method"} and self._contains(d, site)
            ),
            key=lambda d: (d.line, d.column),
            reverse=True,
        )

    def _declared(self, meanings, kinds):
        """Declarations of ``kinds`` the item meanings name."""
        found = []
        for what, file, root, module, _, declared in meanings:
            if what != "item":
                continue
            for context_root, path in self.index.contexts.get(file, ()):
                if context_root != root or module[: len(path)] != path:
                    continue
                symbol = ".".join(module[len(path) :] + (declared,))
                found.extend(
                    d for d in self.by_symbol.get((file, symbol), ()) if d.kind in kinds
                )
        return found

    def _path(self, root, module, segments):
        """(meanings of the last name, whether a name before it is an item
        such as a type, failures) of a path read from a module."""
        if len(segments) == 1:
            meanings, failures = self.index._follow(root, module, segments, frozenset())
            return meanings, False, failures
        prefix, failures = self.index._follow(root, module, segments[:-1], frozenset())
        return self._last(prefix, segments[-1]) + (failures,)

    def _last(self, prefix, name):
        """(meanings of ``name`` in the modules ``prefix`` reaches, whether
        ``prefix`` reaches an item instead)."""
        meanings, through_item = set(), False
        for m in prefix:
            if m[0] == "module":
                meanings |= self.index._follow(m[2], m[3], [name], frozenset())[0]
            elif m[0] in {"item", "through"}:
                through_item = True
        return meanings, through_item

    def _body_binding(self, uses, name, root, path, failures):
        """(meanings the uses in the bodies around a call bind ``name`` to,
        whether an explicit use binds it): explicit uses, or, when none
        binds it, the names globs supply."""
        found = set()
        explicit = [
            u
            for u in uses
            if u.kind == "use" and u.local_names and u.local_names[0] == name
        ]
        for u in explicit or [u for u in uses if u.kind == "use_glob"]:
            walked = self.index._walk(u.specifier.split("::"), u.names, root, path)
            if len(walked) != 3:
                if explicit:
                    failures.append(walked)
                continue
            meanings = self.index._follow(*walked, frozenset())[0]
            if explicit:
                found |= meanings
                continue
            for m in meanings:
                if m[0] == "module":
                    supplied = self.index._follow(m[2], m[3], [name], frozenset())[0]
                    found |= {x for x in supplied if x[0] != "undeclared"}
        return found, bool(explicit)

    def call(self, site):
        segments = site.callee.split("::")
        if "." in segments[-1]:
            receiver, _, name = site.callee.rpartition(".")
            trait = self._innermost_trait(site) if receiver == "self" else None
            if trait is not None:
                # A method of the trait itself; supertraits are not read.
                return _pick(
                    site.id,
                    self._trait_method(trait.id, name),
                    RUST_CALL_SELF_METHOD,
                    "method_call",
                )
            item = self._innermost_impl(site) if receiver == "self" else None
            kind = self.impl_type(item) if item is not None else None
            if kind is None or kind.status != "linked":
                return CallResolution(site.id, "unresolved", reason="method_call")
            return self._associated(site, [kind.target_id], name, RUST_CALL_SELF_METHOD)
        if segments[0] == "Self":
            item = self._innermost_impl(site) if len(segments) == 2 else None
            kind = self.impl_type(item) if item is not None else None
            if kind is None or kind.status != "linked":
                return CallResolution(site.id, "unresolved", reason="associated_item")
            return self._associated(
                site, [kind.target_id], segments[1], RUST_CALL_ASSOCIATED
            )
        resolution = self.resolve(
            site.id,
            site,
            segments,
            _RUST_CALLABLE,
            RUST_CALL_LOCAL_ITEM,
            RUST_CALL_PATH,
        )
        if resolution.reason != "associated_item":
            return resolution
        # A path through a type: a function its impls declare.
        owner = self.resolve(
            site.id,
            site,
            segments[:-1],
            _RUST_TYPES,
            RUST_IMPL_TYPE,
            RUST_IMPL_TYPE,
        )
        if owner.status != "linked":
            return resolution
        return self._associated(
            site, [owner.target_id], segments[-1], RUST_CALL_ASSOCIATED
        )

    def resolve(self, item_id, site, segments, kinds, local_rule, path_rule):
        """The declaration of ``kinds`` a path written at ``site`` (a fact with
        a file and a span) names: an item declared in a function body around
        it (``local_rule``), else through the module trees (``path_rule``)."""
        bodies = self._bodies(site)
        name = segments[-1]
        if len(segments) == 1:
            # An item declared in a function body around the call.
            for body in bodies:
                local = [
                    d
                    for d in self.by_symbol.get(
                        (site.file, f"{body.symbol}.{name}"), ()
                    )
                    if d.kind in kinds
                ]
                if local:
                    return _pick(item_id, local, local_rule, "no_declaration")
        contexts = self.index.contexts.get(site.file)
        scope = self._scope(site)
        if not contexts:
            return CallResolution(
                item_id, "unresolved", reason="module_context_unknown"
            )
        if scope is None:
            return CallResolution(item_id, "unresolved", reason="module_missing")
        # Uses written in the bodies around the call bind before the module.
        uses = [
            u
            for u in self.index.body_uses.get(site.file, ())
            if u.block[:2] <= (site.line, site.column)
            and (site.end_line, site.end_column) <= u.block[2:]
        ]
        found, failures = [], []
        for root, path in sorted(contexts):
            module = path + scope
            meanings, through_item = None, False
            bound, explicit = self._body_binding(
                uses, segments[0], root, path, failures
            )
            if explicit and not bound:
                # A use in the body binds the name to something outside the
                # repository.
                continue
            if bound and len(segments) == 1:
                meanings = bound
            elif bound:
                meanings = set()
                for m in bound:
                    if m[0] == "module":
                        more, item, _ = self._path(m[2], m[3], segments[1:])
                        meanings |= more
                        through_item |= item
                    elif m[0] in {"item", "through"}:
                        through_item = True
            elif segments[0] not in {"crate", "self", "super", ""}:
                first = self.index._follow(root, module, segments[:1], frozenset())[0]
                if any(m[0] != "undeclared" for m in first):
                    meanings, through_item, _ = self._path(root, module, segments)
                elif len(segments) == 1:
                    failures.append(("unresolved", "unbound_name", (), ()))
                    continue
            if meanings is None:
                walked = self.index._walk(segments, scope, root, path)
                if len(walked) != 3:
                    failures.append(walked)
                    continue
                if not walked[2]:
                    # The path names a module or crate root.
                    failures.append(("unresolved", "not_declared", (), ()))
                    continue
                meanings, through_item, failed = self._path(*walked)
                failures.extend(failed)
            declared = self._declared(meanings, kinds)
            if through_item:
                failures.append(("unresolved", "associated_item", (), ()))
            elif not declared:
                failures.append(("unresolved", "not_declared", (), ()))
            found.extend(declared)
        unique = {d.id: d for d in found}
        if unique:
            return _pick(item_id, unique.values(), path_rule, "no_declaration")
        status, reason = (
            min(failures)[:2] if failures else ("unresolved", "not_declared")
        )
        return CallResolution(item_id, status, reason=reason)


def resolve_calls(result: AnalysisResult) -> tuple[CallResolution, ...]:
    """One resolution per call site, in call identity order."""
    by_id = {d.id: d for d in result.definitions}
    starts = defaultdict(list)
    for d in result.definitions:
        starts[(d.file, d.line, d.column)].append(d)
    bindings = name_bindings(result)
    by_symbol, names, modules = bindings.by_symbol, bindings.names, bindings.modules
    top_level = bindings.top_level
    peers = package_peers(result)
    go_variables = {
        clause.file: clause.variables
        for clause in result.packages
        if clause.variables is not None
    }
    languages = {item.file: item.language for item in result.coverage}
    java = _JavaScope(result, by_symbol)
    rust = (
        _RustPaths(result, by_symbol)
        if any(item.language == "rust" for item in result.coverage)
        else None
    )

    defaults = defaultdict(list)
    for d in result.definitions:
        if d.default_export and d.kind in _CALLABLE:
            defaults[d.file].append(d)

    def declared_in(files, name):
        """Callables the modules in ``files`` declare at top level as
        ``name``; for ``default``, the JavaScript or TypeScript declaration
        each exports as its default."""
        if name == "default":
            return [d for file in files for d in defaults.get(file, ())]
        return top_level(files, name, _CALLABLE)

    def js_exported(module, name, seen):
        """Declarations a JavaScript or TypeScript module exports as
        ``name`` without declaring it: through ``export { name } from``
        (aliases applied), or else every ``export * from``, which never
        gives ``default``. A name that several star exports give is
        ambiguous, as in the language. An imported name exported by a
        separate ``export { name }`` is not followed."""
        if (module, name) in seen:
            return []
        seen = seen | {(module, name)}
        declared = declared_in((module,), name)
        if declared:
            return declared
        forwards = bindings.reexports.get(module, ())
        named = [(n, t) for e, n, t in forwards if e == name]
        if named:
            if len(named) != 1 or named[0][0] in (None, "*"):
                return []
            original, targets = named[0]
            return js_exported(targets[0], original, seen) if len(targets) == 1 else []
        found = {}
        for exported, original, targets in forwards if name != "default" else ():
            if (exported, original) == ("*", "*") and len(targets) == 1:
                for d in js_exported(targets[0], name, seen):
                    found[d.id] = d
        return list(found.values())

    def exported(files, name, rule, reason):
        """The resolution of ``name`` as an attribute of the modules in
        ``files``: declared there at top level, or else (Python only) bound
        there by a ``from M import name`` that is followed to M, or
        (JavaScript and TypeScript) re-exported (``js_exported``)."""
        rule_used, seen = rule, set()
        while True:
            declared = declared_in(files, name)
            if declared or len(files) != 1:
                return _pick(site.id, declared, rule_used, reason)
            (module,) = files
            if languages.get(module) in {"javascript", "typescript"}:
                forwarded = js_exported(module, name, frozenset())
                return _pick(
                    site.id,
                    forwarded,
                    CALL_JS_REEXPORT if forwarded else rule_used,
                    reason,
                )
            bound = bindings.module_level.get((module, name), [])
            if (
                languages.get(module) != "python"
                or (module, name) in seen
                # Exactly one import binds it, outside any function or class.
                or not bound
                or len(names.get((module, name), [])) != 1
            ):
                return _pick(site.id, declared, rule_used, reason)
            seen.add((module, name))
            # A module binding (`import m as name`) has no name, and nothing
            # is declared under it.
            name, files = bound[0]
            rule_used = CALL_PYTHON_REEXPORT

    def go_named(file, typed):
        """The files that may declare the Go type ``typed`` written in
        ``file``, and its name there."""
        *package, type_name = typed.split(".")
        if package:
            bound = modules.get((file, package[0]), [])
            return (bound[0] if bound else ()), type_name
        return peers.get(file, (file,)), type_name

    def go_methods(file, typed, name):
        files, type_name = go_named(file, typed)
        return [
            d
            for f in files
            for d in by_symbol.get((f, f"{type_name}.{name}"), ())
            if d.kind == "Method"
        ]

    def go_variable(files, name):
        """(file, type) of the one package-level Go variable ``name``
        declared in ``files`` whose declaration gives its type. None when
        none, or more than one (files for other platforms), is declared."""
        found = [
            (f, typed)
            for f in files
            for variable, typed in go_variables.get(f, ())
            if variable == name
        ]
        if len(found) != 1 or found[0][1] is None:
            return None
        file, typed = found[0]
        return go_result(file, typed[:-2]) if typed.endswith("()") else (file, typed)

    def go_result(file, function):
        """(file, type) of the first result of the Go function ``F`` or
        ``pkg.F`` called in ``file``: one function of the package, or of
        the package the file imports as ``pkg``; or ``v.F``, the first
        result of the method ``F`` of a package variable ``v`` of another
        file. None otherwise."""
        *package, function_name = function.split(".")
        if package and not modules.get((file, package[0])):
            start = go_variable(peers.get(file, (file,)), package[0])
            return None if start is None else go_follow(*start, (f"{function_name}()",))
        files = (
            (modules.get((file, package[0])) or [()])[0]
            if package
            else peers.get(file, (file,))
        )
        found = [
            d
            for f in files
            for d in by_symbol.get((f, function_name), ())
            if d.kind == "Function"
        ]
        if not found:
            # `T(x)`: a conversion to the one type `T` of the package.
            types = [
                d
                for f in files
                for d in by_symbol.get((f, function_name), ())
                if d.kind in _GO_TYPES
            ]
            if len(types) == 1:
                return types[0].file, function_name
        if len(found) != 1 or found[0].result_type is None:
            return None
        return found[0].file, found[0].result_type

    def go_follow(file, typed, steps):
        """(file, type) after following ``steps`` from the Go type ``typed``
        written in ``file``: a field of one struct, the first result of one
        method, an element of a slice. The type is written in the file
        returned. None when a step names nothing declared, or more than
        one thing."""
        for step in steps:
            if step == "[]":
                if not typed.startswith("[]"):
                    # A named slice type (`type byName []*T`): its element.
                    files, type_name = go_named(file, typed)
                    named = [
                        d for f in files for d in by_symbol.get((f, type_name), ())
                    ]
                    if len(named) != 1 or named[0].underlying_type is None:
                        return None
                    file, typed = named[0].file, named[0].underlying_type
                typed = typed[2:]
                continue
            if step.endswith("()"):
                found = go_methods(file, typed, step[:-2])
                if len(found) != 1 or found[0].result_type is None:
                    return None
                file, typed = found[0].file, found[0].result_type
                continue
            files, type_name = go_named(file, typed)
            # Only a struct has fields, and a name declared twice gives none.
            structs = [d for f in files for d in by_symbol.get((f, type_name), ())]
            field = (
                dict(structs[0].fields or ()).get(step) if len(structs) == 1 else None
            )
            if field is None:
                return None
            file, typed = structs[0].file, field
        return file, typed

    def declared_through(files, name, kinds):
        """Top-level ``kinds`` named ``name`` in the modules ``files``,
        following a single module-level ``from M import name`` (Python) to
        the module that declares it."""
        seen = set()
        while True:
            declared = top_level(files, name, kinds)
            if declared or len(files) != 1:
                return declared
            (module,) = files
            bound = bindings.module_level.get((module, name), [])
            if (
                languages.get(module) != "python"
                or (module, name) in seen
                or not bound
                or len(names.get((module, name), [])) != 1
            ):
                return declared
            seen.add((module, name))
            name, files = bound[0]

    def python_class(file, scope, written):
        """The classes a Python annotation names, looked up as a base is:
        an enclosing or module-level class, an imported name, or an
        attribute of an imported module."""
        *head, name = written.split(".")
        if head:
            bound = modules.get((file, ".".join(head)), [])
            return (
                declared_through(bound[0], name, {"Class"}) if len(bound) == 1 else []
            )
        for depth in range(len(scope), 0, -1):
            # A parameter annotation is evaluated in the class body around
            # the method, which `lexical` skips, so a name declared there
            # gives no single class.
            prefix = ".".join(scope[:depth])
            if any(
                d.kind == "Class" for d in by_symbol.get((file, prefix), ())
            ) and by_symbol.get((file, f"{prefix}.{name}")):
                return []
        lexical = bindings.lexical(file, scope, name, {"Class"})
        bound = names.get((file, name), [])
        if lexical and not bound:
            return lexical
        if not lexical and len(bound) == 1 and bound[0][0] not in (None, "default"):
            imported, targets = bound[0]
            return declared_through(targets, imported, {"Class"})
        return []

    python_bases = None

    def python_method(cls, name):
        """``(methods, reason)``: the methods ``name`` a Python class has,
        declared or inherited through its declared bases, depth first and
        left to right; that order is the method resolution order when no
        class is reached twice. A base that names no repository class may
        hold the method, so the lookup stops there."""
        nonlocal python_bases
        if python_bases is None:
            from .inheritance import resolve_supertypes

            facts = {item.id: item for item in result.supertypes}
            python_bases = defaultdict(list)
            for resolution in resolve_supertypes(result):
                item = facts[resolution.call_id]
                if languages.get(item.file) == "python" and item.name != "object":
                    # `object` is every class's last base and declares no
                    # method a repository class would call by name.
                    python_bases[item.owner_id].append(
                        ((item.line, item.column), resolution)
                    )
            for entries in python_bases.values():
                entries.sort(key=lambda entry: entry[0])
        # Without a class reached twice, the method resolution order is the
        # depth-first, left-to-right order of the bases.
        seen, stack = set(), [cls]
        while stack:
            current = stack.pop()
            if current.id in seen:
                return [], "diamond"
            seen.add(current.id)
            stack.extend(
                by_id[base.target_id]
                for _, base in python_bases.get(current.id, ())
                if base.status == "linked"
            )

        def walk(current):
            own = [
                d
                for d in by_symbol.get((current.file, f"{current.symbol}.{name}"), ())
                if d.kind in _METHOD
            ]
            if own:
                return own
            for _, base in python_bases.get(current.id, ()):
                if base.status != "linked":
                    raise LookupError("base_not_resolved")
                found = walk(by_id[base.target_id])
                if found:
                    return found
            return []

        try:
            found = walk(cls)
        except LookupError as error:
            return [], str(error)
        return found, "not_declared_on_type"

    def python_super(site, scope, name):
        """``super().m()`` in a method of the Python class ``C`` around it:
        the method ``m`` the bases of ``C`` declare or inherit, in the
        method resolution order after ``C``."""
        owner = next(
            (
                d
                for depth in range(len(scope) - 1, 0, -1)
                for d in by_symbol.get((site.file, ".".join(scope[:depth])), ())
                if d.kind == "Class"
            ),
            None,
        )
        # Only directly in a method: a nested function has no implicit
        # `__class__`.
        if owner is None or owner.symbol != ".".join(scope[:-1]):
            return CallResolution(site.id, "unresolved", reason="outside_class")
        _, reason = python_method(owner, name)
        if reason == "diamond":
            return CallResolution(site.id, "unresolved", reason=reason)
        for _, base in python_bases.get(owner.id, ()):
            if base.status != "linked":
                return CallResolution(site.id, "unresolved", reason="base_not_resolved")
            methods, reason = python_method(by_id[base.target_id], name)
            if methods or reason != "not_declared_on_type":
                return _pick(site.id, methods, PYTHON_CALL_SUPER, reason)
        return CallResolution(site.id, "unresolved", reason="not_declared_on_type")

    def script_class(site, head):
        """The class that the first name of a JavaScript or TypeScript call
        ``head.m()`` denotes: declared where the call's scopes see ``head``,
        or bound by a named import of one class. None otherwise."""
        if languages.get(site.file) not in {"javascript", "typescript"}:
            return None
        return script_named(site.file, head, site.binding)

    def script_named(file, head, binding):
        """The class ``head`` names in a JavaScript or TypeScript file where
        ``binding`` (as ``CallSite.binding``) is its declaration."""
        if binding is not None:
            found = starts.get((file, *binding), [])
        else:
            bound = names.get((file, head), [])
            if len(bound) != 1 or bound[0][0] is None:
                return None
            imported, targets = bound[0]
            linked = exported(targets, imported, CALL_IMPORTED_NAME, "not_declared")
            found = [by_id[linked.target_id]] if linked.target_id else []
        return found[0] if len(found) == 1 and found[0].kind == "Class" else None

    script_bases = None

    def script_base(cls):
        """``(resolved, base)``: whether a JavaScript or TypeScript class
        extends nothing or a repository class, and that class or None."""
        nonlocal script_bases
        if script_bases is None:
            from .inheritance import resolve_supertypes

            facts = {item.id: item for item in result.supertypes}
            script_bases = defaultdict(list)
            for resolution in resolve_supertypes(result):
                item = facts[resolution.call_id]
                if item.kind == "extends":
                    # A class extends at most one class; `implements` names
                    # types whose members it does not inherit.
                    script_bases[item.owner_id].append(resolution)
        bases = script_bases.get(cls.id, [])
        if not bases:
            return True, None
        if bases[0].target_id is None:
            return False, None
        return True, by_id[bases[0].target_id]

    def script_single(cls):
        """``cls`` when no interface or namespace merges with the class,
        which would give it members its body does not declare."""
        if cls is not None and len(by_symbol.get((cls.file, cls.symbol), ())) == 1:
            return cls
        return None

    def script_field(cls, name):
        """The class the instance field or getter ``name`` of a JavaScript
        or TypeScript class holds, declared on it or inherited through
        ``extends``: written in ``Definition.fields`` and named where the
        class is declared. None when that is not one repository class."""
        seen, current = set(), cls
        while current.id not in seen:
            seen.add(current.id)
            written = [t for n, t in current.fields or () if n == name]
            if written:
                # The name is the class's own scopes' declaration, which
                # shadows an import, or else the import's.
                scope = current.symbol.split(".")[:-1]
                lexical = bindings.lexical(current.file, scope, written[0], _ANY)
                if lexical:
                    found = lexical[0]
                    return script_single(found if found.kind == "Class" else None)
                return script_single(script_named(current.file, written[0], None))
            if name in (current.properties or ()) or by_symbol.get(
                (current.file, f"{current.symbol}.{name}")
            ):
                # Untyped, or a method.
                return None
            resolved, base = script_base(current)
            if base is None:
                return None
            current = base
        return None

    def script_method(cls, name):
        """``(methods, reason)``: the instance method ``name`` a JavaScript
        or TypeScript class declares, or inherits through ``extends``. A
        field of that name hides an inherited method, and a base that names
        no repository class may declare it, so either stops the lookup."""
        seen, current = set(), cls
        while current.id not in seen:
            seen.add(current.id)
            own = [
                d
                for d in by_symbol.get((current.file, f"{current.symbol}.{name}"), ())
                if d.kind in _METHOD and not d.static
            ]
            if own:
                return own, "not_declared_on_type"
            if name in (current.properties or ()):
                return [], "field_not_method"
            resolved, base = script_base(current)
            if base is None:
                return [], "not_declared_on_type" if resolved else "base_not_resolved"
            current = base
        return [], "base_not_resolved"

    resolutions = []
    for site in sorted(result.call_sites, key=lambda value: value.id):
        caller = by_id.get(site.caller_id)
        scope = caller.symbol.split(".") if caller is not None else []
        if languages.get(site.file) == "java":
            resolutions.append(java.call(site, scope))
            continue
        if languages.get(site.file) == "rust":
            resolutions.append(rust.call(site))
            continue
        parts = site.callee.split(".")
        classes = (
            python_class(site.file, scope[:-1], site.receiver_type)
            if site.receiver_type is not None and languages.get(site.file) == "python"
            else []
        )
        if len(classes) == 1:
            # `x.m()` where `x` is a parameter annotated with a class, or a
            # local whose one assignment calls or annotates a class. The
            # name is looked up where the function is defined. A name that
            # is no single repository class (`str`, a factory function)
            # leaves the call to the rules below.
            methods, reason = python_method(classes[0], parts[-1])
            resolutions.append(
                _pick(site.id, methods, CALL_PYTHON_TYPED_RECEIVER, reason)
            )
            continue
        if site.shadowed and parts[0] not in {"self", "this"}:
            # A local name (`for f in fs: f()`) is not the module's `f`.
            resolutions.append(
                CallResolution(site.id, "unresolved", reason="local_binding")
            )
            continue
        if len(parts) == 1 and site.binding is not None:
            # JavaScript and TypeScript: the declaration the call's scopes
            # give, or none; a binding that declares no callable (a
            # parameter, `let f = make()`) is not one further out.
            if not site.binding:
                resolutions.append(
                    CallResolution(site.id, "unresolved", reason="unbound_name")
                )
                continue
            resolutions.append(
                _pick(
                    site.id,
                    [
                        d
                        for d in starts.get((site.file, *site.binding), ())
                        if d.symbol.split(".")[-1] == site.callee
                        and d.kind in _CALLABLE
                    ],
                    CALL_LEXICAL_NAME,
                    "local_binding",
                )
            )
            continue
        if len(parts) == 1:
            lexical = bindings.lexical(site.file, scope, site.callee, _CALLABLE)
            bound = names.get((site.file, site.callee), [])
            if lexical and bound:
                resolutions.append(
                    CallResolution(site.id, "ambiguous", reason="multiple_bindings")
                )
            elif lexical:
                resolutions.append(
                    _pick(site.id, lexical, CALL_LEXICAL_NAME, "no_declaration")
                )
            elif len(bound) > 1:
                resolutions.append(
                    CallResolution(site.id, "ambiguous", reason="multiple_bindings")
                )
            elif not bound and site.file in peers:
                functions = top_level(peers[site.file], site.callee, _CALLABLE)
                types = (
                    []
                    if functions
                    else top_level(peers[site.file], site.callee, _GO_CONVERSIONS)
                )
                resolutions.append(
                    _pick(site.id, types, GO_CALL_CONVERSION, "no_declaration")
                    if types
                    else _pick(site.id, functions, CALL_PACKAGE_NAME, "no_declaration")
                )
            elif bound and bound[0][0] is not None:
                name, targets = bound[0]
                resolutions.append(
                    exported(
                        targets, name, CALL_IMPORTED_NAME, "imported_name_not_declared"
                    )
                )
            else:
                resolutions.append(
                    CallResolution(
                        site.id,
                        "unresolved",
                        reason="unbound_name" if not bound else "unsupported_binding",
                    )
                )
            continue
        head, name = ".".join(parts[:-1]), parts[-1]
        script = languages.get(site.file) in {"javascript", "typescript"}
        if script and site.receiver_type is not None and not name.startswith("#"):
            if site.receiver_type == "this":
                # The innermost class around the call: `this` there is one
                # of its instances.
                owner = next(
                    (
                        found[0]
                        for depth in range(len(scope), 0, -1)
                        if (
                            found := by_symbol.get(
                                (site.file, ".".join(scope[:depth])), ()
                            )
                        )
                        and found[0].kind == "Class"
                    ),
                    None,
                )
            else:
                owner = script_named(
                    site.file, site.receiver_type, site.receiver_binding
                )
            owner = script_single(owner)
            for step in site.receiver_steps or ():
                if owner is None:
                    break
                owner = script_field(owner, step)
            if owner is not None:
                # `x.m()` where `x` is annotated with a class or bound to
                # `new` of it, or `x.a.m()` through the fields' classes.
                methods, reason = script_method(owner, name)
                resolutions.append(
                    _pick(
                        site.id,
                        methods,
                        CALL_JS_TYPED_RECEIVER
                        if site.receiver_type != "this" and not site.receiver_steps
                        else CALL_JS_MEMBER_CHAIN,
                        reason,
                    )
                )
                continue
            if site.receiver_steps or site.receiver_type == "this":
                resolutions.append(
                    CallResolution(
                        site.id, "unresolved", reason="receiver_type_unknown"
                    )
                )
                continue
        steps = site.receiver_steps or ()
        variable = None
        if not script and site.receiver_variable is not None:
            # Go: `v.M()` on a package variable of another file of the
            # package, or `pkg.V.M()` on a variable of an imported package.
            imported = modules.get((site.file, site.receiver_variable))
            if not imported:
                variable = go_variable(
                    peers.get(site.file, (site.file,)), site.receiver_variable
                )
            elif steps:
                variable = go_variable(imported[0], steps[0])
                steps = steps[1:]
        if not script and (
            site.receiver_type is not None
            or site.receiver_function is not None
            or variable is not None
        ):
            # Go: `x.M()` on a variable whose declaration gives its type,
            # or binds it to the first result of a function.
            start = (
                variable
                if variable is not None
                else (site.file, site.receiver_type)
                if site.receiver_function is None
                else go_result(site.file, site.receiver_function)
            )
            followed = None if start is None else go_follow(*start, steps)
            if followed is None or followed[1].startswith("[]"):
                resolutions.append(
                    CallResolution(
                        site.id, "unresolved", reason="receiver_type_unknown"
                    )
                )
                continue
            resolutions.append(
                _pick(
                    site.id,
                    go_methods(*followed, name),
                    GO_CALL_TYPED_RECEIVER,
                    "not_declared_on_type",
                )
            )
            continue
        if head == "super()" and languages.get(site.file) == "python":
            resolutions.append(python_super(site, scope, name))
            continue
        if head in {"self", "this"} or name.startswith("#"):
            owner = next(
                (
                    ".".join(scope[:depth])
                    for depth in range(len(scope), 0, -1)
                    if any(
                        d.kind == "Class"
                        for d in by_symbol.get((site.file, ".".join(scope[:depth])), ())
                    )
                ),
                None,
            )
            own = (
                []
                if owner is None
                else [
                    d
                    for d in by_symbol.get((site.file, f"{owner}.{name}"), ())
                    if d.kind in _METHOD
                ]
            )
            classes = (
                [d for d in by_symbol.get((site.file, owner), ()) if d.kind == "Class"]
                if owner is not None
                and not own
                and head == "self"
                and languages.get(site.file) == "python"
                else []
            )
            if owner is None:
                resolutions.append(
                    CallResolution(site.id, "unresolved", reason="outside_class")
                )
            elif len(classes) == 1:
                # Python `self.m()` where the class around it declares no
                # `m`: the one its declared bases give.
                methods, reason = python_method(classes[0], name)
                resolutions.append(
                    _pick(
                        site.id,
                        methods,
                        PYTHON_CALL_SELF_INHERITED,
                        "not_declared_in_class"
                        if reason == "not_declared_on_type"
                        else reason,
                    )
                )
            else:
                resolutions.append(
                    _pick(
                        site.id,
                        [
                            d
                            for d in by_symbol.get((site.file, f"{owner}.{name}"), ())
                            if d.kind in _METHOD
                        ],
                        # A private name (`obj.#m`) can only name a member of
                        # a class around it, whatever `obj` is; the innermost
                        # class is the one, unless it declares no `#m`.
                        CALL_SELF_METHOD
                        if head in {"self", "this"}
                        else CALL_PRIVATE_NAME,
                        "not_declared_in_class",
                    )
                )
            continue
        bound = modules.get((site.file, head), [])
        if len(bound) == 1:
            resolutions.append(
                exported(
                    bound[0],
                    name,
                    CALL_MODULE_ATTRIBUTE,
                    "module_attribute_not_declared",
                )
            )
        elif bound:
            resolutions.append(
                CallResolution(site.id, "ambiguous", reason="multiple_bindings")
            )
        elif (owner := script_class(site, head)) is not None:
            # `C.m()` on a class: a static method `C` declares.
            resolutions.append(
                _pick(
                    site.id,
                    [
                        d
                        for d in by_symbol.get(
                            (owner.file, f"{owner.symbol}.{name}"), ()
                        )
                        if d.kind in _METHOD and d.static
                    ],
                    CALL_JS_STATIC_METHOD,
                    "static_not_declared",
                )
            )
        else:
            resolutions.append(
                CallResolution(site.id, "unresolved", reason="attribute_call")
            )
    return tuple(_constructors(resolutions, result, by_id, by_symbol, languages))


def _constructors(resolutions, result, by_id, by_symbol, languages):
    """JavaScript and TypeScript: a ``new C(...)`` linked to a class that
    declares one ``constructor`` links to that constructor, which is what
    the construction runs; a class without one keeps the link."""
    sites = {s.id: s for s in result.call_sites}
    for r in resolutions:
        site = sites[r.call_id]
        target = by_id.get(r.target_id)
        if (
            r.status == "linked"
            and site.kind == "new"
            and target is not None
            and target.kind == "Class"
            and languages.get(target.file) in {"javascript", "typescript"}
        ):
            # Overload signatures declare constructors too: then the class.
            found = by_symbol.get((target.file, f"{target.symbol}.constructor"), ())
            if len(found) == 1:
                r = replace(r, target_id=found[0].id)
        yield r
