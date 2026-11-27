"""Bases of classes and interfaces resolved from SCIP indexes.

A base written in a class or interface declaration (``class A(B)``,
``class A extends B implements I``) is a reference the indexer records at
the base's last name. The declaration that reference names is the base, as
the compiler or type checker reads it: through imports, re-exports, aliases
and shadowing. Files no index covers keep the name-matched rules of
``deepthought.inheritance``.

A reference the index records but that names nothing the repository
declares (a base from an installed package) links nothing, even where a
repository class has the same name. Where the index records no reference at
the base, or names a repository declaration this rule does not take (in
Python, one of another name that no import of the file binds to the written
name, since scip-python follows ``from m import *`` to wrong symbols), the
parser's resolution stands.
"""

from collections import defaultdict
from dataclasses import dataclass

from .calls import CallResolution

SCIP_PYTHON_BASE = "scip.python-base"
SCIP_TYPESCRIPT_BASE = "scip.typescript-base"
EXTERNAL = "scip_external"

_RULES = {"scip-python": SCIP_PYTHON_BASE, "scip-typescript": SCIP_TYPESCRIPT_BASE}
_TYPES = {"Class", "Interface"}


@dataclass(frozen=True)
class BaseBinding:
    resolution: CallResolution
    # (indexer name@version, receipt digest) when an index decided it.
    producer: tuple[str, str] | None = None


def _last_name(text: bytes, column: int, name: str):
    """UTF-8 byte column of the last segment of ``name`` written from
    ``column`` of ``text``, before any type arguments; else None."""
    last = name.rsplit(".", 1)[-1].encode()
    written = text[column:]
    for stop in (b"<", b"[", b"("):
        cut = written.find(stop)
        if cut != -1:
            written = written[:cut]
    found = written.rfind(last)
    return None if found == -1 else column + found


def _renamed(item, target, imports):
    """Python: whether the declaration's name is not the base's written name
    and no import of the file binds that name to it (scip-python follows
    ``from m import *`` re-exports to wrong declarations)."""
    if not item.file.endswith((".py", ".pyi")):
        return False
    written = item.name.rsplit(".", 1)[-1]
    declared = target.symbol.rsplit(".", 1)[-1]
    return written != declared and (written, declared) not in imports[item.file]


def bind_supertypes(result, parsed, references) -> list[BaseBinding]:
    """One binding per resolution in ``parsed`` (``resolve_supertypes`` of
    ``result``), in the same order. ``references`` is a
    ``deepthought.scip_calls.ScipReferences`` over the run's indexes."""
    bases = {item.id: item for item in result.supertypes}
    imports = defaultdict(set)
    for item in result.imports:
        for imported, local in zip(item.names, item.local_names):
            imports[item.file].add((local, imported.rsplit(".", 1)[-1]))
    bound = []
    for resolution in parsed:
        item = bases[resolution.call_id]
        tool = references.tool(item.file)
        rule = tool and _RULES.get(tool[0].split("@", 1)[0])
        column = (
            _last_name(
                references.lines.get(item.file, item.line), item.column, item.name
            )
            if rule
            else None
        )
        found = (
            None
            if column is None
            else references.declarations(item.file, item.line, column, None)
        )
        if found is None:
            bound.append(BaseBinding(resolution))
            continue
        targets = [
            d
            for d in found
            if d.kind in _TYPES
            and d.id != item.owner_id
            and not _renamed(item, d, imports)
        ]
        if len(targets) == 1:
            bound.append(
                BaseBinding(
                    CallResolution(item.id, "linked", targets[0].id, rule), tool
                )
            )
        elif targets:
            bound.append(
                BaseBinding(
                    CallResolution(
                        item.id,
                        "ambiguous",
                        rule_id=rule,
                        reason="scip_several_declarations",
                        candidates=tuple(sorted(d.id for d in targets)),
                    ),
                    tool,
                )
            )
        elif references.definition_sites(item.file, item.line, column):
            # The index names a repository declaration this rule does not
            # take (another name no import explains, or not a class): the
            # parser's resolution stands.
            bound.append(BaseBinding(resolution))
        else:
            # The index names nothing the repository declares: a base from an
            # installed package, whatever the parser matched by name.
            bound.append(
                BaseBinding(
                    CallResolution(item.id, "unresolved", reason=EXTERNAL), tool
                )
            )
    return bound
