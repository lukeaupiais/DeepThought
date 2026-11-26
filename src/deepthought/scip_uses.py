"""What code uses besides calls, from the SCIP references the indexer records.

A call is one use of a declaration; code also names a type (an annotation, a
conversion, ``isinstance``), passes a function as a value (a handler, a
callback, an entry in a lookup table) and reads or writes a field. These
couple code as calls do and change with it, so each reference an index
records in a covered file that is not at a call site's name becomes a use:

| Edge | Target |
|---|---|
| ``USES_TYPE`` | a parsed class, struct, interface, type alias or enum |
| ``REFERENCES_FUNCTION`` | a parsed function or method, not called there |
| ``READS_FIELD``, ``WRITES_FIELD`` | a field: a term the index defines on a type (``T#name.``) |

The source is the innermost parsed declaration around the reference, or its
file. Targets are read back from the index's definitions as calls are
(``deepthought.scip_calls``). Fields are not parsed declarations: each field
the index defines inside a parsed type becomes a ``Field`` of that type.
Whether a field use writes is read from the syntax around it (the indexers
mark every reference ``ReadAccess`` or nothing): the target of an assignment,
augmented assignment, ``++``/``--``, ``del``, or a Go keyed composite literal
element. Uses of one kind from one source to one target are one edge, with
the first site and the number of sites. References in import statements are
left to the import edges, and local variables have no declaration to link.
"""

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath

from .models import AnalysisResult
from .scip_calls import _declaration, _IndexView, _key, _Lines, _to_utf8
from .syntax import LANGUAGES, make_parser, parse_source

USES_TYPE = "USES_TYPE"
REFERENCES_FUNCTION = "REFERENCES_FUNCTION"
READS_FIELD = "READS_FIELD"
WRITES_FIELD = "WRITES_FIELD"

RULES = {
    USES_TYPE: "scip.use-type",
    REFERENCES_FUNCTION: "scip.use-function-value",
    READS_FIELD: "scip.use-field-read",
    WRITES_FIELD: "scip.use-field-write",
}
FIELD_RULE = "scip.field-definition"

TYPE_KINDS = frozenset(
    {"Class", "Struct", "Interface", "Type", "TypeAlias", "Enum", "Trait"}
)
FUNCTION_KINDS = frozenset({"Function", "Method"})

# A term defined on a type: ``...Type#name.``, not a method (``name().``).
_FIELD = re.compile(r"#(`[^`]+`|[^#./()`\s]+)\.$")


@dataclass(frozen=True)
class Field:
    """A field the index defines inside a parsed type."""

    id: str
    file: str
    owner_id: str
    name: str
    line: int
    column: int
    native_id: str
    producer: tuple


@dataclass(frozen=True)
class Use:
    kind: str
    file: str
    source_id: str | None  # None: the file itself
    target_id: str  # a parsed declaration, or a Field id
    line: int
    column: int
    sites: int
    producer: tuple

    @property
    def rule_id(self):
        return RULES[self.kind]


def _language(file):
    suffix = PurePosixPath(file).suffix
    for language, (suffixes, _, _) in LANGUAGES.items():
        if suffix in suffixes and language in {
            "python",
            "typescript",
            "javascript",
            "go",
        }:
            return language
    return None


class _Writes:
    """Whether a name written at a position is assigned to, from the file's
    syntax tree."""

    _ASSIGNMENTS = {
        "python": {"assignment": "left", "augmented_assignment": "left"},
        "typescript": {
            "assignment_expression": "left",
            "augmented_assignment_expression": "left",
        },
        "go": {"assignment_statement": "left"},
    }
    _ASSIGNMENTS["javascript"] = _ASSIGNMENTS["typescript"]
    # Wrappers of an assignment target: `a.b`, `a.b[i]`, `(a.b)`, `a, b = ...`.
    _CHAINS = {
        "attribute",
        "member_expression",
        "selector_expression",
        "subscript",
        "subscript_expression",
        "index_expression",
        "parenthesized_expression",
        "pattern_list",
        "tuple_pattern",
        "expression_list",
        "non_null_expression",
    }

    _MEMBERS = {
        "attribute": "attribute",
        "member_expression": "property",
        "selector_expression": "field",
    }

    def __init__(self, root):
        self.root, self.trees, self.parsers = root, {}, {}

    def _tree(self, file):
        if file not in self.trees:
            language = _language(file)
            tree = None
            if language is not None:
                suffix = PurePosixPath(file).suffix
                try:
                    content = (self.root / file).read_bytes()
                    key = (language, suffix == ".tsx")
                    if key not in self.parsers:
                        self.parsers[key] = make_parser(language, suffix)
                    tree = parse_source(self.parsers[key], language, suffix, content)
                except (OSError, ImportError, ValueError):
                    tree = None
            self.trees[file] = (language, tree)
        return self.trees[file]

    def __call__(self, file, line, column):
        language, tree = self._tree(file)
        if tree is None:
            return False
        point = (line - 1, column)
        node = tree.root_node.named_descendant_for_point_range(point, point)
        if node is None:
            return False
        assignments = self._ASSIGNMENTS.get(language, {})
        child = node
        while child.parent is not None:
            parent = child.parent
            if parent.type in assignments:
                return parent.child_by_field_name(assignments[parent.type]) == child
            if parent.type in {"update_expression", "inc_statement", "dec_statement"}:
                return True
            if parent.type == "delete_statement":
                return True
            if language == "go" and parent.type in {"keyed_element", "literal_element"}:
                # `T{Field: v}`: the key names the field written.
                element = parent if parent.type == "literal_element" else child
                keyed = element.parent
                return (
                    keyed is not None
                    and keyed.type == "keyed_element"
                    and keyed.named_children[:1] == [element]
                )
            if parent.type not in self._CHAINS:
                return False
            # In `a.b = 1` the write is to b, the last name, not to a.
            member = self._MEMBERS.get(parent.type)
            if member is not None and parent.child_by_field_name(member) != child:
                return False
            # Only the object side of `a.b` and the indexed side of `a[i]`
            # carry the write to the name: `x[a.b] = 1` writes x, not b.
            if parent.type in {"subscript", "subscript_expression", "index_expression"}:
                value = parent.child_by_field_name(
                    "value" if language == "python" else "object"
                ) or (parent.named_children[0] if parent.named_children else None)
                if value != child:
                    return False
            child = parent
        return False


def bind_uses(result: AnalysisResult, indexes, root):
    """The uses (``Use``) and fields (``Field``) that ``indexes`` (pairs of
    an ingested SCIP index and its receipt digest, as ``bind_calls`` takes
    them) give the declarations of ``result``; ``root`` holds the sources."""
    from pathlib import Path

    from .syntax import identifier

    root = Path(root)
    lines = _Lines(root)
    in_file = defaultdict(list)
    for d in result.definitions:
        in_file[d.file].append(d)
    covering, shared_by_indexer, views = {}, {}, []
    for index, receipt in indexes:
        tool = next(
            (f"{n}@{v}" for n, v in index.analyzer_versions if n.startswith("scip-")),
            "scip",
        )
        view = _IndexView(index, tool, receipt)
        shared = shared_by_indexer.setdefault(tool.split("@", 1)[0], defaultdict(list))
        for key, found in view.definitions.items():
            shared[key].extend(found)
        view.definitions = shared
        views.append((index, view))
        for coverage in index.coverage:
            covering.setdefault(coverage.file, view)
    # A call's name is the call's use.
    called = {(s.file, *s.callee_at) for s in result.call_sites if s.callee_at}
    named = set()
    for d in result.definitions:
        name = d.symbol.rsplit(".", 1)[-1].encode()
        at = lines.get(d.file, d.line).find(name, d.column)
        if at >= 0:
            named.add((d.file, d.line, at))
    imports = defaultdict(list)
    for item in result.imports:
        imports[item.file].append((item.line, item.end_line))

    def innermost(file, line, column, kinds=None):
        point = (line, column)
        holding = [
            d
            for d in in_file.get(file, ())
            if (d.line, d.column) <= point <= (d.end_line, d.end_column)
            and (kinds is None or d.kind in kinds)
        ]
        return min(
            holding,
            key=lambda d: (d.end_line - d.line, d.end_column - d.column, d.id),
            default=None,
        )

    fields = {}

    def field_of(scip, view):
        match = _FIELD.search(scip.native_id or "")
        if match is None or scip.native_id.startswith("local "):
            return None
        text = lines.get(scip.file, scip.line)
        column = _to_utf8(text, scip.column, view.encoding(scip))
        owner = innermost(scip.file, scip.line, column, TYPE_KINDS)
        if owner is None:
            return None
        key = (owner.id, scip.native_id)
        if key not in fields:
            name = match.group(1).strip("`")
            fields[key] = Field(
                identifier(owner.id, scip.file, scip.native_id, "Field", 0),
                scip.file,
                owner.id,
                name,
                scip.line,
                column,
                scip.native_id,
                (view.tool, view.receipt),
            )
        return fields[key]

    writes = _Writes(root)
    grouped = {}
    for index, view in views:
        for reference in index.references:
            if covering.get(reference.file) is not view:
                continue
            text = lines.get(reference.file, reference.line)
            column = _to_utf8(text, reference.column, view.encoding(reference))
            if (reference.file, reference.line, column) in called:
                continue
            if (reference.file, reference.line, column) in named:
                # A declaration's own name the index records as a reference
                # (scip-python: a redefinition; scip-typescript: a member).
                continue
            if any(
                start <= reference.line <= end
                for start, end in imports.get(reference.file, ())
            ):
                continue
            declared = view.definitions.get(
                _key(reference.file, reference.native_id), ()
            )
            target_kind = None
            for scip in declared:
                parsed = _declaration(scip, view, lines, in_file)
                if parsed is not None and parsed.kind in TYPE_KINDS:
                    target_kind, target = USES_TYPE, parsed.id
                elif parsed is not None and parsed.kind in FUNCTION_KINDS:
                    target_kind, target = REFERENCES_FUNCTION, parsed.id
                elif parsed is None:
                    found = field_of(scip, view)
                    if found is None:
                        continue
                    target_kind = (
                        WRITES_FIELD
                        if writes(reference.file, reference.line, column)
                        else READS_FIELD
                    )
                    target = found.id
                else:
                    continue
                break
            if target_kind is None:
                continue
            source = innermost(reference.file, reference.line, column)
            if source is not None and source.id == target:
                # A declaration naming itself (a recursive reference, a
                # class in its own annotations) adds no coupling.
                continue
            key = (target_kind, source.id if source else reference.file, target)
            site = (reference.line, column)
            earlier = grouped.get(key)
            if earlier is None:
                grouped[key] = [
                    reference.file,
                    source.id if source else None,
                    site,
                    1,
                    (view.tool, view.receipt),
                ]
            else:
                earlier[2] = min(earlier[2], site)
                earlier[3] += 1
    uses = [
        Use(kind, file, source, target, site[0], site[1], count, producer)
        for (kind, _, target), (file, source, site, count, producer) in grouped.items()
    ]
    uses.sort(key=lambda u: (u.file, u.line, u.column, u.kind, u.target_id))
    return uses, sorted(fields.values(), key=lambda f: (f.file, f.line, f.column))
