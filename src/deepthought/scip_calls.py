"""Bind compiler-backed SCIP references to the call sites the parser records.

A call site in a file an index covers takes its target from that index: the
SCIP reference at the start of the callee's last name (``CallSite.callee_at``)
names a symbol, and the symbol's SCIP definition is read back to the parsed
declaration whose name starts there. Call sites in files no index covers keep
the parser's name-binding rules (``deepthought.calls``), which also run in
shadow over covered files so that the two can be compared.
"""

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .calls import CallResolution, resolve_calls
from .inheritance import resolve_supertypes
from .models import AnalysisResult

SCIP_CALL_OCCURRENCE = "scip.call-occurrence"
SCIP_CALL_CONSTRUCTOR = "scip.call-constructor"
SCIP_CALL_OVERLOAD_IMPLEMENTATION = "scip.call-overload-implementation"
SCIP_CALL_LOCAL_SYMBOL = "scip.call-local-symbol"

# Why a call site in a covered file has no target.
NO_OCCURRENCE = "scip_no_occurrence"
EXTERNAL = "scip_external"
NOT_DECLARATION = "scip_target_not_declaration"
SUSPECT = "scip_suspect_target"

# The columns an indexer writes when its documents leave the encoding
# unspecified: scip-python counts UTF-16 code units, as the language server
# protocol does, and scip-go counts bytes (both checked on lines with
# non-ASCII text before a name).
_UNSPECIFIED = {"scip-python": "utf-16", "scip-typescript": "utf-16"}

# Constructors by language: what `new C()` or `C()` runs.
_CONSTRUCTORS = {"constructor", "__init__"}


@dataclass(frozen=True)
class Disagreement:
    """A covered call site whose SCIP target and parser target differ."""

    file: str
    line: int
    callee: str
    scip_target: tuple[str, int] | None
    parser_target: tuple[str, int] | None
    reason: str
    rule: str

    def row(self) -> str:
        """The site as a tab-separated line: file, line, callee, SCIP target
        file and line, parser target file and line ("-" and 0 when there is
        none), reason and rule."""
        scip = self.scip_target or ("-", 0)
        parser = self.parser_target or ("-", 0)
        return "\t".join(
            str(v)
            for v in (
                self.file,
                self.line,
                self.callee,
                *scip,
                *parser,
                self.reason,
                self.rule,
            )
        )


@dataclass(frozen=True)
class CallBinding:
    """Every call site's resolution, in identity order, and for each one an
    index decided, the producing indexer (``name@version``) and the digest of
    the receipt it came with."""

    resolutions: tuple[CallResolution, ...]
    producers: dict
    disagreements: tuple[Disagreement, ...]
    # Calls in covered files whose index target was suspect, so the
    # parser's link was kept.
    parser_kept: frozenset = frozenset()

    def resolver(self, call_id) -> str:
        """Who decided a call: ``scip``, ``scip_suspect_parser`` (a covered
        call whose parser link was kept) or ``tree-sitter`` (a file no index
        covers)."""
        if call_id in self.producers:
            return "scip"
        if call_id in self.parser_kept:
            return "scip_suspect_parser"
        return "tree-sitter"


def bind_calls(
    result: AnalysisResult,
    indexes,
    root: str | Path,
) -> CallBinding:
    """Resolve ``result``'s call sites from ``indexes``: pairs of an ingested
    SCIP index (``deepthought.ingest_scip``, with the same file paths) and its
    receipt digest. ``root`` holds the sources both were made from, which the
    binder reads to convert columns and to read declared names."""
    parsed = resolve_calls(result)
    if not indexes:
        return CallBinding(tuple(parsed), {}, ())
    root = Path(root)
    by_call = {r.call_id: r for r in parsed}
    sites = {s.id: s for s in result.call_sites}
    definitions = {d.id: d for d in result.definitions}
    in_file = defaultdict(list)
    for d in result.definitions:
        in_file[d.file].append(d)
    by_symbol = defaultdict(list)
    for d in result.definitions:
        by_symbol[(d.file, d.symbol)].append(d)
    lines = _Lines(root)
    covering, definitions_by_indexer = {}, {}
    for index, receipt in indexes:
        tool = next(
            (f"{n}@{v}" for n, v in index.analyzer_versions if n.startswith("scip-")),
            "scip",
        )
        view = _IndexView(index, tool, receipt)
        # Indexes of one indexer (a second pass, one per Go module) name
        # their global symbols by package, so a symbol one defines is the
        # one another references; local symbols stay keyed by document.
        shared = definitions_by_indexer.setdefault(
            tool.split("@", 1)[0], defaultdict(list)
        )
        for key, found in view.definitions.items():
            shared[key].extend(found)
        view.definitions = shared
        for coverage in index.coverage:
            covering.setdefault(coverage.file, view)
    bases = _Bases(result, definitions)
    resolutions, producers, disagreements, kept = [], {}, [], set()
    for resolution in parsed:
        site = sites[resolution.call_id]
        view = covering.get(site.file)
        if view is None:
            resolutions.append(resolution)
            continue
        bound = _bind(site, view, lines, in_file, by_symbol)
        if (
            bound.status != "linked"
            and bound.reason == EXTERNAL
            and (_self_declares(site, bases, by_symbol, definitions))
        ):
            bound = CallResolution(site.id, "unresolved", reason=SUSPECT)
        elif bound.status == "linked" and _renamed(
            site, definitions[bound.target_id], result
        ):
            bound = CallResolution(site.id, "unresolved", reason=SUSPECT)
        chosen = bound
        if bound.reason == SUSPECT and by_call[site.id].status == "linked":
            # A target the index may have wrong: the parser's stands.
            chosen = by_call[site.id]
            kept.add(site.id)
        else:
            producers[site.id] = (view.tool, view.receipt)
        resolutions.append(chosen)
        disagreement = _compare(site, bound, by_call[site.id], definitions)
        if disagreement is not None:
            disagreements.append(disagreement)
    return CallBinding(
        tuple(resolutions), producers, tuple(disagreements), frozenset(kept)
    )


class ScipReferences:
    """The parsed declarations that SCIP references at written names resolve
    to, for rules other than call binding (``deepthought.data_contracts``).

    ``indexes`` are pairs of an ingested SCIP index and its receipt digest,
    as ``bind_calls`` takes them, and ``root`` holds the sources."""

    def __init__(self, result: AnalysisResult, indexes, root: str | Path):
        self.lines = _Lines(Path(root))
        self.in_file = defaultdict(list)
        for d in result.definitions:
            self.in_file[d.file].append(d)
        self.covering, shared_by_indexer = {}, {}
        for index, receipt in indexes:
            tool = next(
                (
                    f"{n}@{v}"
                    for n, v in index.analyzer_versions
                    if n.startswith("scip-")
                ),
                "scip",
            )
            view = _IndexView(index, tool, receipt)
            shared = shared_by_indexer.setdefault(
                tool.split("@", 1)[0], defaultdict(list)
            )
            for key, found in view.definitions.items():
                shared[key].extend(found)
            view.definitions = shared
            for coverage in index.coverage:
                self.covering.setdefault(coverage.file, view)

    def covers(self, file) -> bool:
        return file in self.covering

    def tool(self, file):
        """(indexer ``name@version``, receipt digest) of the index covering
        ``file``, or None."""
        view = self.covering.get(file)
        return None if view is None else (view.tool, view.receipt)

    def symbols(self, file, line, column):
        """The SCIP symbols referenced at ``line`` and UTF-8 byte ``column``
        of ``file``: None when no index covers the file, else a tuple (empty
        when the index records no reference there)."""
        view = self.covering.get(file)
        if view is None:
            return None
        text = self.lines.get(file, line)
        return tuple(
            sorted(
                {
                    r.native_id
                    for encoding in sorted(view.encodings.get(file, ()))
                    for r in view.references.get(
                        (file, line, _to_index(text, column, encoding)), ()
                    )
                    if view.encoding(r) == encoding
                }
            )
        )

    def value_classes(self, file, line, column):
        """The parsed classes of the inferred types (``value_type``) of the
        local variables referenced at ``line`` and UTF-8 byte ``column`` of
        ``file``: None when no index covers the file or it records no such
        local there, else a tuple (empty for a type declared outside the
        repository)."""
        view = self.covering.get(file)
        if view is None:
            return None
        types = {
            d.value_type
            for native in self.symbols(file, line, column)
            for d in view.definitions.get(_key(file, native), ())
            if native.startswith("local ") and d.value_type
        }
        if not types:
            return None
        targets = {}
        for qualified in types:
            for scip in view.python_classes.get(qualified, ()):
                parsed = _declaration(scip, view, self.lines, self.in_file)
                if parsed is not None:
                    targets[parsed.id] = parsed
        return tuple(sorted(targets.values(), key=lambda d: d.id))

    def definition_sites(self, file, line, column):
        """(file, line, UTF-8 byte column) of where the index defines the
        symbols referenced at ``line`` and ``column`` of ``file``: None when
        no index covers the file, else a tuple."""
        view = self.covering.get(file)
        if view is None:
            return None
        sites = set()
        for native in self.symbols(file, line, column):
            for d in view.definitions.get(_key(file, native), ()):
                text = self.lines.get(d.file, d.line)
                sites.add((d.file, d.line, _to_utf8(text, d.column, view.encoding(d))))
        return tuple(sorted(sites))

    def declarations(self, file, line, column, name):
        """The parsed declarations named ``name`` that the reference written
        at ``line`` and UTF-8 byte ``column`` of ``file`` resolves to: None
        when no index covers the file or it records no reference there, else
        a tuple (empty for a symbol declared outside the repository). A
        declaration of another name is dropped, since an index may follow a
        star re-export to a wrong one; a ``name`` of None keeps every one."""
        view = self.covering.get(file)
        if view is None:
            return None
        text = self.lines.get(file, line)
        found = [
            r
            for encoding in sorted(view.encodings.get(file, ()))
            for r in view.references.get(
                (file, line, _to_index(text, column, encoding)), ()
            )
            if view.encoding(r) == encoding
        ]
        if not found:
            return None
        targets = {}
        for reference in found:
            for scip in view.definitions.get(_key(file, reference.native_id), ()):
                parsed = _declaration(scip, view, self.lines, self.in_file)
                if parsed is not None and (
                    name is None or parsed.symbol.rsplit(".", 1)[-1] == name
                ):
                    targets[parsed.id] = parsed
        return tuple(sorted(targets.values(), key=lambda d: d.id))


class _Lines:
    """Source lines of the analyzed files, read when first needed."""

    def __init__(self, root):
        self.root, self.cache = root, {}

    def get(self, file, line):
        if file not in self.cache:
            try:
                self.cache[file] = (self.root / file).read_bytes().split(b"\n")
            except OSError:
                self.cache[file] = []
        text = self.cache[file]
        return text[line - 1] if 0 < line <= len(text) else b""


def _to_index(text: bytes, column: int, encoding: str) -> int:
    """A UTF-8 byte column of ``text`` in the index's ``encoding``."""
    if encoding == "utf-8":
        return column
    prefix = text[:column].decode("utf-8", "replace")
    if encoding == "utf-16":
        return len(prefix.encode("utf-16-le")) // 2
    return len(prefix)


def _to_utf8(text: bytes, column: int, encoding: str) -> int:
    """A column of ``text`` in ``encoding`` as a UTF-8 byte column."""
    if encoding == "utf-8":
        return column
    decoded = text.decode("utf-8", "replace")
    if encoding == "utf-16":
        units = decoded.encode("utf-16-le")[: column * 2]
        return len(units.decode("utf-16-le", "replace").encode("utf-8"))
    return len(decoded[:column].encode("utf-8"))


class _IndexView:
    """One ingested index: its references by position and definitions by
    symbol (local symbols by file too)."""

    def __init__(self, index, tool, receipt):
        self.tool, self.receipt = tool, receipt
        self.unspecified = _UNSPECIFIED.get(tool.split("@", 1)[0], "utf-8")
        self.references, self.encodings = defaultdict(list), defaultdict(set)
        for r in index.references:
            self.references[(r.file, r.line, r.column)].append(r)
            self.encodings[r.file].add(self.encoding(r))
        self.definitions = defaultdict(list)
        for d in index.definitions:
            if d.native_id is not None:
                self.definitions[_key(d.file, d.native_id)].append(d)
        # scip-python classes by qualified name (``a.b.C``), from their
        # symbols' descriptors (`` `a.b`/C# ``).
        self.python_classes = defaultdict(list)
        if tool.startswith("scip-python@"):
            for d in index.definitions:
                qualified = _python_class_name(d.native_id or "")
                if qualified is not None:
                    self.python_classes[qualified].append(d)

    def encoding(self, fact):
        """The encoding of a fact's columns."""
        found = fact.coordinate_encoding
        return self.unspecified if found == "unspecified" else found


def _python_class_name(native):
    """``a.b.C`` for a scip-python class symbol whose descriptors are a
    module (`` `a.b`/ ``, or ``a/`` without a dot) and type descriptors
    (``C#``), else None."""
    parts = native.split(" ", 4)
    if len(parts) != 5 or parts[0] != "scip-python":
        return None
    descriptor = parts[4]
    if descriptor.startswith("`"):
        module, separator, rest = descriptor[1:].partition("`/")
    else:
        module, separator, rest = descriptor.partition("/")
        if not module.isidentifier():
            return None
    if not separator:
        return None
    names = rest.split("#")
    if len(names) < 2 or names[-1] or not all(n.isidentifier() for n in names[:-1]):
        return None
    return ".".join((module, *names[:-1]))


def _key(file, native):
    # SCIP local symbols are scoped to their document.
    return (file, native) if native.startswith("local ") else (native,)


def _bind(site, view, lines, in_file, by_symbol) -> CallResolution:
    if site.callee_at is None:
        return CallResolution(site.id, "unresolved", reason=NO_OCCURRENCE)
    line, column = site.callee_at
    text = lines.get(site.file, line)
    found = [
        r
        for encoding in sorted(view.encodings.get(site.file, ()))
        for r in view.references.get(
            (site.file, line, _to_index(text, column, encoding)), ()
        )
        if view.encoding(r) == encoding
    ]
    if not found:
        return CallResolution(site.id, "unresolved", reason=NO_OCCURRENCE)
    targets, local, external = {}, False, False
    for reference in found:
        declared = view.definitions.get(_key(site.file, reference.native_id))
        if not declared:
            external = True
            continue
        for scip in declared:
            parsed = _declaration(scip, view, lines, in_file)
            if parsed is not None:
                targets[parsed.id] = parsed
                local = local or reference.native_id.startswith("local ")
    if not targets:
        return CallResolution(
            site.id, "unresolved", reason=EXTERNAL if external else NOT_DECLARATION
        )
    return _choose(site, list(targets.values()), local, by_symbol)


def _declaration(scip, view, lines, in_file):
    """The innermost parsed declaration whose span holds the SCIP
    definition and whose own name is the one written there."""
    text = lines.get(scip.file, scip.line)
    encoding = view.encoding(scip)
    start = _to_utf8(text, scip.column, encoding)
    end = (
        _to_utf8(text, scip.end_column, encoding)
        if scip.end_line == scip.line
        else len(text)
    )
    name = text[start:end].decode("utf-8", "replace")
    point = (scip.line, start)
    holding = [
        d
        for d in in_file.get(scip.file, ())
        if (d.line, d.column) <= point <= (d.end_line, d.end_column)
        and d.symbol.rsplit(".", 1)[-1] == name
    ]
    return min(
        holding,
        key=lambda d: (d.end_line - d.line, d.end_column - d.column, d.id),
        default=None,
    )


def _choose(site, targets, local, by_symbol) -> CallResolution:
    rule = SCIP_CALL_LOCAL_SYMBOL if local else SCIP_CALL_OCCURRENCE
    if len(targets) == 1:
        (target,) = targets
        if target.kind == "Class" and site.kind == "new":
            # `new C()` runs the constructor C declares.
            # Of overloaded constructors, the one implementation.
            found = [
                d
                for d in by_symbol.get(
                    (target.file, f"{target.symbol}.constructor"), ()
                )
                if not d.overload
            ]
            if len(found) == 1:
                return CallResolution(
                    site.id, "linked", found[0].id, SCIP_CALL_CONSTRUCTOR
                )
        if target.overload:
            # scip-python names an @overload group by its first signature:
            # the call runs the one implementation beside them.
            runs = [
                d
                for d in by_symbol.get((target.file, target.symbol), ())
                if not d.overload
            ]
            if len(runs) == 1:
                return CallResolution(
                    site.id, "linked", runs[0].id, SCIP_CALL_OVERLOAD_IMPLEMENTATION
                )
        return CallResolution(site.id, "linked", target.id, rule)
    classes = [d for d in targets if d.kind == "Class"]
    if len(classes) == 1:
        constructors = [
            d
            for d in targets
            if d is not classes[0]
            and d.symbol.rsplit(".", 1)[0] == classes[0].symbol
            and d.symbol.rsplit(".", 1)[-1] in _CONSTRUCTORS
        ]
        if len(constructors) == 1 and len(targets) == 2:
            return CallResolution(
                site.id, "linked", constructors[0].id, SCIP_CALL_CONSTRUCTOR
            )
    runs = [d for d in targets if not d.overload]
    if len(runs) == 1:
        return CallResolution(
            site.id, "linked", runs[0].id, SCIP_CALL_OVERLOAD_IMPLEMENTATION
        )
    return CallResolution(
        site.id,
        "ambiguous",
        reason="multiple_declarations",
        candidates=tuple(sorted(d.id for d in targets)),
    )


class _Bases:
    """The repository classes each class's declared bases link to."""

    def __init__(self, result, definitions):
        facts = {item.id: item for item in result.supertypes}
        self.of = defaultdict(list)
        for resolution in resolve_supertypes(result):
            if resolution.status == "linked":
                item = facts[resolution.call_id]
                self.of[item.owner_id].append(definitions[resolution.target_id])


def _self_declares(site, bases, by_symbol, definitions):
    """Python ``self.x()``: whether the class around the call, or a
    repository class it inherits from, declares ``x``."""
    parts = site.callee.split(".")
    caller = definitions.get(site.caller_id)
    if len(parts) != 2 or parts[0] != "self" or caller is None:
        return False
    scope = caller.symbol.split(".")[:-1]
    owner = next(
        (
            d
            for depth in range(len(scope), 0, -1)
            for d in by_symbol.get((site.file, ".".join(scope[:depth])), ())
            if d.kind == "Class"
        ),
        None,
    )
    seen, stack = set(), [owner] if owner is not None else []
    while stack:
        cls = stack.pop()
        if cls.id in seen:
            continue
        seen.add(cls.id)
        if by_symbol.get((cls.file, f"{cls.symbol}.{parts[1]}")):
            return True
        stack.extend(bases.of.get(cls.id, ()))
    return False


def _renamed(site, target, result):
    """Python: whether the target's name is not the name the call writes and
    no import in the file binds that name to it (an index may follow a star
    re-export to a wrong declaration)."""
    if not site.file.endswith((".py", ".pyi")):
        return False
    written = site.callee.rsplit(".", 1)[-1]
    name = target.symbol.rsplit(".", 1)
    declared = {name[-1]}
    if name[-1] in _CONSTRUCTORS and len(name) == 2:
        # `C()` runs `C.__init__`.
        declared.add(name[0].rsplit(".", 1)[-1])
    if written in declared:
        return False
    return not any(
        local == written and imported.rsplit(".", 1)[-1] in declared
        for item in result.imports
        if item.file == site.file
        for imported, local in zip(item.names, item.local_names)
    )


def _compare(site, bound, parser, definitions):
    def target(resolution):
        if resolution.status != "linked":
            return None
        d = definitions[resolution.target_id]
        return (d.file, d.line)

    scip, ours = target(bound), target(parser)
    if scip == ours:
        return None
    if scip is not None and ours is not None:
        reason = "different_target"
    elif scip is not None:
        reason = "parser_" + (parser.reason or parser.status)
    else:
        reason = bound.reason or bound.status
    return Disagreement(
        site.file,
        site.line,
        site.callee,
        scip,
        ours,
        reason,
        parser.rule_id if ours is not None else bound.rule_id,
    )
