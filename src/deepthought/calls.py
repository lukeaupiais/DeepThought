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

from .imports import PYTHON_FROM_IMPORT_SUBMODULE, resolve_imports
from .models import AnalysisResult

CALL_LEXICAL_NAME = "tree-sitter.call-lexical-name"
CALL_IMPORTED_NAME = "tree-sitter.call-imported-name"
CALL_MODULE_ATTRIBUTE = "tree-sitter.call-module-attribute"
CALL_SELF_METHOD = "tree-sitter.call-self-method"
CALL_PACKAGE_NAME = "tree-sitter.call-package-name"

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


def resolve_calls(result: AnalysisResult) -> tuple[CallResolution, ...]:
    """One resolution per call site, in call identity order."""
    by_id = {d.id: d for d in result.definitions}
    bindings = name_bindings(result)
    by_symbol, names, modules = bindings.by_symbol, bindings.names, bindings.modules
    top_level = bindings.top_level
    peers = package_peers(result)

    resolutions = []
    for site in sorted(result.call_sites, key=lambda value: value.id):
        caller = by_id.get(site.caller_id)
        scope = caller.symbol.split(".") if caller is not None else []
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
