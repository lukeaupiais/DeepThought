"""Match named call sites to declarations by declared name-binding rules.

These rules read only one AnalysisResult. They match names, so every link
they produce is name_matched: rebinding, shadowing, inheritance and dynamic
dispatch are not checked. Compiler-backed edges come from SCIP instead. A
call that no rule ties to exactly one declaration stays unresolved or
ambiguous, with a reason, and produces no link.
"""

from collections import defaultdict
from dataclasses import dataclass, field
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
GO_CALL_TYPED_RECEIVER = "tree-sitter.go-call-typed-receiver"
CALL_PYTHON_REEXPORT = "tree-sitter.python-call-reexport"

JAVA_ENCLOSING_METHOD = "tree-sitter.java-enclosing-type-method"
JAVA_STATIC_IMPORT = "tree-sitter.java-static-import"
JAVA_TYPE_MEMBER = "tree-sitter.java-type-member"
JAVA_TYPE_NAME = "tree-sitter.java-type-name"
JAVA_INHERITED_METHOD = "tree-sitter.java-inherited-method"
JAVA_TYPED_RECEIVER = "tree-sitter.java-call-typed-receiver"
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
_METHOD = {"Method", "Function"}


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

    def top_level(self, files, name, kinds):
        return [
            d
            for f in files
            for d in self.by_symbol.get((f, name), ())
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
    for d in result.definitions:
        by_symbol[(d.file, d.symbol)].append(d)
    module_files = {}
    for resolution in resolve_imports(result):
        module_files[resolution.import_id] = tuple(
            link.target_file
            for link in resolution.links
            if link.rule_id != PYTHON_FROM_IMPORT_SUBMODULE
        )
    declared = {p.file: p.name for p in result.packages if p.kind == "package"}
    # The bodies of each file's functions and classes.
    bodies = defaultdict(list)
    for d in result.definitions:
        if d.kind in {"Function", "Method", "Class"}:
            bodies[d.file].append(((d.line, d.column), (d.end_line, d.end_column)))
    names = defaultdict(list)
    modules = defaultdict(list)
    module_level = defaultdict(list)
    for item in result.imports:
        targets = module_files.get(item.id, ())
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
                modules[(item.file, prefix)].append(targets)
                names[(item.file, local)].append((None, targets))
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
    return NameBindings(by_symbol, names, modules, module_level)


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

    def methods(self, site, candidates, rule_id, none_reason):
        """Link among same-named methods by arity; none fits: unresolved.

        A link to a method the type inherits rather than declares carries
        the inherited-method rule.
        """
        if not candidates:
            return CallResolution(site.id, "unresolved", reason=none_reason)
        declared = {m.id for m in candidates if not m.inherited}
        resolution = _pick(
            site.id,
            [m.definition for m in self.arity(site, candidates)],
            rule_id,
            "no_matching_arity",
        )
        if resolution.status == "linked" and resolution.target_id not in declared:
            return CallResolution(
                site.id, "linked", resolution.target_id, JAVA_INHERITED_METHOD
            )
        return resolution

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

    def call(self, site, scope):
        parts = site.callee.split(".")
        file = site.file
        if site.kind == "new":
            return _pick(
                site.id,
                self.type(file, scope, site.callee),
                JAVA_TYPE_NAME,
                "type_not_declared",
            )
        name = parts[-1]
        if len(parts) == 1:
            for methods in self.enclosing_members(site, scope, name):
                if methods:
                    # Java stops at the innermost type with a method of that
                    # name, declared or inherited.
                    return self.methods(
                        site, methods, JAVA_ENCLOSING_METHOD, "no_declaration"
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
                    site, imported, JAVA_STATIC_IMPORT, "no_declaration"
                )
            return CallResolution(site.id, "unresolved", reason="unbound_name")
        head = ".".join(parts[:-1])
        if head == "this":
            innermost = next(self.enclosing_members(site, scope, name), None)
            if innermost is None:
                return CallResolution(site.id, "unresolved", reason="outside_class")
            return self.methods(
                site, innermost, CALL_SELF_METHOD, "not_declared_in_class"
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
            )
        owners = self.type(file, scope, head)
        if not owners:
            return CallResolution(site.id, "unresolved", reason="attribute_call")
        return self.methods(
            site,
            [m for t in owners for m in self.members(t.file, t.symbol, name)],
            JAVA_TYPE_MEMBER,
            "not_declared_in_type",
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
    bindings = name_bindings(result)
    by_symbol, names, modules = bindings.by_symbol, bindings.names, bindings.modules
    top_level = bindings.top_level
    peers = package_peers(result)
    languages = {item.file: item.language for item in result.coverage}
    java = _JavaScope(result, by_symbol)
    rust = (
        _RustPaths(result, by_symbol)
        if any(item.language == "rust" for item in result.coverage)
        else None
    )

    def exported(files, name, rule, reason):
        """The resolution of ``name`` as an attribute of the modules in
        ``files``: declared there at top level, or else (Python only) bound
        there by a ``from M import name`` that is followed to M."""
        rule_used, seen = rule, set()
        while True:
            declared = top_level(files, name, _CALLABLE)
            if declared or len(files) != 1:
                return _pick(site.id, declared, rule_used, reason)
            (module,) = files
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
        if site.shadowed and parts[0] not in {"self", "this"}:
            # A local name (`for f in fs: f()`) is not the module's `f`.
            resolutions.append(
                CallResolution(site.id, "unresolved", reason="local_binding")
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
                resolutions.append(
                    _pick(
                        site.id,
                        top_level(peers[site.file], site.callee, _CALLABLE),
                        CALL_PACKAGE_NAME,
                        "no_declaration",
                    )
                )
            elif bound and bound[0][0] not in (None, "default"):
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
        if site.receiver_type is not None:
            # Go: `x.M()` on a variable whose declaration writes its type.
            *package, type_name = site.receiver_type.split(".")
            if package:
                bound = modules.get((site.file, package[0]), [])
                files = bound[0] if bound else ()
            else:
                files = peers.get(site.file, (site.file,))
            resolutions.append(
                _pick(
                    site.id,
                    [
                        d
                        for f in files
                        for d in by_symbol.get((f, f"{type_name}.{name}"), ())
                        if d.kind == "Method"
                    ],
                    GO_CALL_TYPED_RECEIVER,
                    "not_declared_on_type",
                )
            )
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
            if owner is None:
                resolutions.append(
                    CallResolution(site.id, "unresolved", reason="outside_class")
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
        else:
            resolutions.append(
                CallResolution(site.id, "unresolved", reason="attribute_call")
            )
    return tuple(resolutions)
