"""Match named call sites to declarations by declared name-binding rules.

These rules read only one AnalysisResult. They match names, so every link
they produce is name_matched: rebinding, shadowing, inheritance and dynamic
dispatch are not checked. Compiler-backed edges come from SCIP instead. A
call that no rule ties to exactly one declaration stays unresolved or
ambiguous, with a reason, and produces no link.
"""

from collections import defaultdict
from dataclasses import dataclass
import posixpath

from .imports import PYTHON_FROM_IMPORT_SUBMODULE, _java_index, resolve_imports
from .models import AnalysisResult

CALL_LEXICAL_NAME = "tree-sitter.call-lexical-name"
CALL_IMPORTED_NAME = "tree-sitter.call-imported-name"
CALL_MODULE_ATTRIBUTE = "tree-sitter.call-module-attribute"
CALL_SELF_METHOD = "tree-sitter.call-self-method"
CALL_PACKAGE_NAME = "tree-sitter.call-package-name"

JAVA_ENCLOSING_METHOD = "tree-sitter.java-enclosing-type-method"
JAVA_STATIC_IMPORT = "tree-sitter.java-static-import"
JAVA_TYPE_MEMBER = "tree-sitter.java-type-member"
JAVA_TYPE_NAME = "tree-sitter.java-type-name"
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
    ids = tuple(sorted(d.id for d in matches))
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
    names = defaultdict(list)
    modules = defaultdict(list)
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
    return NameBindings(by_symbol, names, modules)


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


class _JavaScope:
    """Java name lookup: enclosing types, then imports, then the package."""

    def __init__(self, result, by_symbol):
        self.by_symbol = by_symbol
        self.roots, self.packages, self.types = _java_index(result)
        self.package = {
            p.file: p.name
            for p in result.packages
            if p.kind == "package" and p.file in self.roots
        }
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
        files = by_root.get(root) or (
            next(iter(by_root.values())) if len(by_root) == 1 else []
        )
        found = []
        for file in sorted(set(files)):
            symbol = name[len(self.package[file]) + 1 :]
            found.extend(self._defs(file, symbol, _JAVA_TYPES))
        return found

    def type(self, file, scope, name):
        """Declarations a type name denotes from a file and scope."""
        first, *rest = name.split(".")
        root = self.roots.get(file)
        found = []
        for depth in range(len(scope), -1, -1):
            prefix = ".".join(scope[:depth])
            found = self._defs(
                file, f"{prefix}.{first}" if prefix else first, _JAVA_TYPES
            )
            if found:
                break
        if not found and root is not None:
            for spec in self.single.get((file, first), ()):
                found.extend(self.qualified(spec, root))
            package = self.package.get(file)
            if not found and package is not None:
                found = self.qualified(f"{package}.{first}", root)
            if not found:
                for spec in self.on_demand.get(file, ()):
                    found.extend(self.qualified(f"{spec}.{first}", root))
        if not found and rest and root is not None:
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
                n
                for d in found
                for n in self._defs(d.file, f"{d.symbol}.{part}", _JAVA_TYPES)
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

    def methods(self, site, declared, rule_id, none_reason):
        """Link among same-named methods by arity; none fits: unresolved."""
        if not declared:
            return CallResolution(site.id, "unresolved", reason=none_reason)
        return _pick(site.id, self.arity(site, declared), rule_id, "no_matching_arity")

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
            for owner in self.enclosing_types(file, scope):
                methods = self._defs(file, f"{owner}.{name}", {"Method"})
                if methods:
                    # Java stops at the innermost type declaring the name; a
                    # fitting overload missing there is inherited.
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
                    for m in self._defs(t.file, f"{t.symbol}.{name}", {"Method"})
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
            owner = next(self.enclosing_types(file, scope), None)
            if owner is None:
                return CallResolution(site.id, "unresolved", reason="outside_class")
            return self.methods(
                site,
                self._defs(file, f"{owner}.{name}", {"Method"}),
                CALL_SELF_METHOD,
                "not_declared_in_class",
            )
        owners = self.type(file, scope, head)
        if not owners:
            return CallResolution(site.id, "unresolved", reason="attribute_call")
        return self.methods(
            site,
            [
                m
                for t in owners
                for m in self._defs(t.file, f"{t.symbol}.{name}", {"Method"})
            ],
            JAVA_TYPE_MEMBER,
            "not_declared_in_type",
        )


def resolve_calls(result: AnalysisResult) -> tuple[CallResolution, ...]:
    """One resolution per call site, in call identity order."""
    by_id = {d.id: d for d in result.definitions}
    bindings = name_bindings(result)
    by_symbol, names, modules = bindings.by_symbol, bindings.names, bindings.modules
    top_level = bindings.top_level
    peers = package_peers(result)
    languages = {item.file: item.language for item in result.coverage}
    java = _JavaScope(result, by_symbol)

    resolutions = []
    for site in sorted(result.call_sites, key=lambda value: value.id):
        caller = by_id.get(site.caller_id)
        scope = caller.symbol.split(".") if caller is not None else []
        if languages.get(site.file) == "java":
            resolutions.append(java.call(site, scope))
            continue
        parts = site.callee.split(".")
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
                    _pick(
                        site.id,
                        top_level(targets, name, _CALLABLE),
                        CALL_IMPORTED_NAME,
                        "imported_name_not_declared",
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
        if head in {"self", "this"}:
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
                        CALL_SELF_METHOD,
                        "not_declared_in_class",
                    )
                )
            continue
        bound = modules.get((site.file, head), [])
        if len(bound) == 1:
            resolutions.append(
                _pick(
                    site.id,
                    top_level(bound[0], name, _CALLABLE),
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
