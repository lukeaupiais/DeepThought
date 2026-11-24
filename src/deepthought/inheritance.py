"""Match declared bases to class and interface declarations by name.

The same name-binding rules as calls (see calls.py) read one AnalysisResult.
They match names, so every link is name_matched: rebinding, shadowing and
runtime base expressions are not checked. Compiler-backed subtype edges come
from SCIP instead. A base that no rule ties to exactly one declaration stays
unresolved or ambiguous, with a reason, and produces no link.
"""

from dataclasses import dataclass

from .calls import (
    RUST_IMPL_TRAIT,  # noqa: F401 (re-exported)
    RUST_IMPL_TYPE,  # noqa: F401 (re-exported)
    CallResolution,
    _JavaScope,
    _PhpCalls,
    _pick,
    _RustPaths,
    name_bindings,
)
from .models import AnalysisResult

BASE_LEXICAL_NAME = "tree-sitter.base-lexical-name"
BASE_IMPORTED_NAME = "tree-sitter.base-imported-name"
BASE_MODULE_ATTRIBUTE = "tree-sitter.base-module-attribute"
JAVA_BASE_TYPE = "tree-sitter.java-base-type"
RUST_SUPERTRAIT = "tree-sitter.rust-supertrait"
PHP_BASE_NAME = "tree-sitter.php-base-name"

_TYPES = {"Class", "Interface"}


def resolve_supertypes(result: AnalysisResult) -> tuple[CallResolution, ...]:
    """One resolution per supertype fact, in identity order.

    ``call_id`` of each resolution is the supertype's identity.
    """
    by_id = {d.id: d for d in result.definitions}
    bindings = name_bindings(result)
    languages = {item.file: item.language for item in result.coverage}
    java = _JavaScope(result, bindings.by_symbol)
    rust = php = None
    resolutions = []
    for item in sorted(result.supertypes, key=lambda value: value.id):
        # A base is evaluated in the scope that encloses its declaration, and
        # that scope may be a class body (Python nested classes).
        scope = by_id[item.owner_id].symbol.split(".")[:-1]
        if languages.get(item.file) == "rust":
            # A supertrait is a path read as an impl's trait is.
            rust = rust or _RustPaths(result, bindings.by_symbol)
            resolutions.append(
                rust.resolve(
                    item.id,
                    item,
                    item.name.split("::"),
                    {"Trait"},
                    RUST_SUPERTRAIT,
                    RUST_SUPERTRAIT,
                )
            )
            continue
        if languages.get(item.file) == "php":
            # A class extends a class, implements interfaces and uses
            # traits; an interface extends interfaces.
            php = php or _PhpCalls(result, languages)
            owner = by_id[item.owner_id]
            kind = (
                "Trait"
                if item.kind == "uses"
                else "Class"
                if item.kind == "extends" and owner.kind == "Class"
                else "Interface"
            )
            resolutions.append(
                _pick(
                    item.id,
                    [
                        d
                        for d in php.types.get(php.fold(item.qualified_name), ())
                        if d.kind == kind
                    ],
                    PHP_BASE_NAME,
                    "type_not_declared",
                )
            )
            continue
        if languages.get(item.file) == "java":
            resolutions.append(
                _pick(
                    item.id,
                    java.type(item.file, scope, item.name),
                    JAVA_BASE_TYPE,
                    "type_not_declared",
                )
            )
            continue
        parts = item.name.split(".")
        if len(parts) == 1:
            lexical = [
                d
                for d in bindings.by_symbol.get(
                    (item.file, ".".join((*scope, item.name))), ()
                )
                if d.kind in _TYPES
            ] or bindings.lexical(item.file, scope, item.name, _TYPES)
            bound = bindings.names.get((item.file, item.name), [])
            if lexical and bound:
                resolution = CallResolution(
                    item.id, "ambiguous", reason="multiple_bindings"
                )
            elif lexical:
                resolution = _pick(
                    item.id, lexical, BASE_LEXICAL_NAME, "no_declaration"
                )
            elif len(bound) > 1:
                resolution = CallResolution(
                    item.id, "ambiguous", reason="multiple_bindings"
                )
            elif bound and bound[0][0] not in (None, "default"):
                name, targets = bound[0]
                resolution = _pick(
                    item.id,
                    bindings.top_level(targets, name, _TYPES),
                    BASE_IMPORTED_NAME,
                    "imported_name_not_declared",
                )
            else:
                resolution = CallResolution(
                    item.id,
                    "unresolved",
                    reason="unbound_name" if not bound else "unsupported_binding",
                )
        else:
            head, name = ".".join(parts[:-1]), parts[-1]
            bound = bindings.modules.get((item.file, head), [])
            if len(bound) == 1:
                resolution = _pick(
                    item.id,
                    bindings.top_level(bound[0], name, _TYPES),
                    BASE_MODULE_ATTRIBUTE,
                    "module_attribute_not_declared",
                )
            elif bound:
                resolution = CallResolution(
                    item.id, "ambiguous", reason="multiple_bindings"
                )
            else:
                resolution = CallResolution(
                    item.id, "unresolved", reason="attribute_base"
                )
        resolutions.append(resolution)
    return tuple(resolutions)


@dataclass(frozen=True)
class ImplResolution:
    """An impl's type and trait, each resolved as a Rust path in item
    position is (see calls._RustPaths); ``trait`` is None for an inherent
    impl."""

    impl_id: str
    type: CallResolution
    trait: CallResolution | None


def resolve_rust_impls(result: AnalysisResult) -> tuple[ImplResolution, ...]:
    """One resolution per Rust impl fact, in identity order.

    The type resolves to a struct, enum, union or type alias and the trait
    to a trait, read from the impl's module after the items and uses of the
    function bodies around it. A type that is one of the impl's own type
    parameters (``impl<T> Trait for T``) names no declaration.
    """
    if not result.impls:
        return ()
    paths = _RustPaths(result, name_bindings(result).by_symbol)
    resolutions = []
    for item in sorted(result.impls, key=lambda value: value.id):
        kind = paths.impl_type(item)
        resolutions.append(ImplResolution(item.id, kind, paths.impl_trait(item)))
    return tuple(resolutions)
