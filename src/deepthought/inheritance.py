"""Match declared bases to class and interface declarations by name.

The same name-binding rules as calls (see calls.py) read one AnalysisResult.
They match names, so every link is name_matched: rebinding, shadowing and
runtime base expressions are not checked. Compiler-backed subtype edges come
from SCIP instead. A base that no rule ties to exactly one declaration stays
unresolved or ambiguous, with a reason, and produces no link.
"""

from .calls import CallResolution, _pick, name_bindings
from .models import AnalysisResult

BASE_LEXICAL_NAME = "tree-sitter.base-lexical-name"
BASE_IMPORTED_NAME = "tree-sitter.base-imported-name"
BASE_MODULE_ATTRIBUTE = "tree-sitter.base-module-attribute"

_TYPES = {"Class", "Interface"}


def resolve_supertypes(result: AnalysisResult) -> tuple[CallResolution, ...]:
    """One resolution per supertype fact, in identity order.

    ``call_id`` of each resolution is the supertype's identity.
    """
    by_id = {d.id: d for d in result.definitions}
    bindings = name_bindings(result)
    resolutions = []
    for item in sorted(result.supertypes, key=lambda value: value.id):
        # A base is evaluated in the scope that encloses its declaration, and
        # that scope may be a class body (Python nested classes).
        scope = by_id[item.owner_id].symbol.split(".")[:-1]
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
