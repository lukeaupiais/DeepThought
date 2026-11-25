"""Bind JavaScript and TypeScript imports to the modules a SCIP index resolves.

scip-typescript records the module specifier of every ``import``, ``export
... from``, ``require()`` and ``import()`` as a reference to the module
symbol of the file the TypeScript compiler resolved it to, and defines that
symbol at the start of the file. An import in a file an index covers takes
its target from there, by the compiler's own module resolution (paths,
extensions, index files, package ``exports``), not from the parser's path
rules (``deepthought.imports``), which keep only the files no index covers.
"""

from dataclasses import dataclass
from pathlib import Path
import posixpath

from .imports import ImportLink, ImportResolution
from .scip_calls import _IndexView, _Lines, _to_utf8

SCIP_IMPORT_MODULE = "scip.import-module"
NO_OCCURRENCE = "scip_no_occurrence"
EXTERNAL = "scip_external"
NOT_ANALYZED = "scip_target_not_analyzed"
AMBIGUOUS = "multiple_module_references"
_LANGUAGES = {"javascript", "typescript"}
_QUOTES = "'\"`"


@dataclass(frozen=True)
class ImportBinding:
    """Every import's resolution, in the order given, and for each one an
    index decided, the producing indexer and its receipt digest."""

    resolutions: tuple[ImportResolution, ...]
    producers: dict


def bind_imports(result, indexes, root, resolutions) -> ImportBinding:
    """``resolutions`` (the parser's, ``imports.resolve_imports``) with each
    JavaScript or TypeScript import of a file an index covers replaced by
    the module the index's reference at its specifier names."""
    languages = {c.file: c.language for c in result.coverage}
    files = set(languages)
    imports = {i.id: i for i in result.imports}
    covering, definitions = {}, {}
    for index, receipt in indexes:
        tool = next(
            (f"{n}@{v}" for n, v in index.analyzer_versions if n.startswith("scip-")),
            "scip",
        )
        view = _IndexView(index, tool, receipt)
        for key, found in view.definitions.items():
            definitions.setdefault(key, []).extend(found)
        for coverage in index.coverage:
            covering.setdefault(coverage.file, view)
    modules = {}
    for view in {id(v): v for v in covering.values()}.values():
        for (file, _, _), found in view.references.items():
            for r in found:
                # Module symbols end in "/"; SCIP local symbols never do.
                if r.native_id.endswith("/"):
                    modules.setdefault((id(view), file), []).append(r)
    lines = _Lines(Path(root))
    bound, producers = [], {}
    for resolution in resolutions:
        item = imports.get(resolution.import_id)
        view = covering.get(item.file) if item is not None else None
        if view is None or languages.get(item.file) not in _LANGUAGES:
            bound.append(resolution)
            continue
        producers[item.id] = (view.tool, view.receipt)
        found = {
            r.native_id
            for r in modules.get((id(view), item.file), ())
            if _names_specifier(r, item, view, lines)
        }
        if not found:
            bound.append(ImportResolution(item.id, "unresolved", reason=NO_OCCURRENCE))
            continue
        # A `declare module './m'` augmentation elsewhere is referenced at
        # the specifier too; the module is the file's own symbol.
        files_of = {
            symbol: sorted(
                {
                    d.file
                    for d in definitions.get((symbol,), ())
                    if symbol.endswith(f"`{posixpath.basename(d.file)}`/")
                }
            )
            for symbol in found
        }
        if len(found) > 1 and sum(1 for f in files_of.values() if f) == 1:
            found = {s for s, f in files_of.items() if f}
        if len(found) > 1:
            bound.append(
                ImportResolution(
                    item.id,
                    "ambiguous",
                    reason=AMBIGUOUS,
                    candidates=tuple(sorted(found)),
                )
            )
            continue
        (symbol,) = found
        targets = files_of[symbol]
        if len(targets) > 1:
            # One symbol defined in several files: indexes of packages
            # that share a name and version.
            bound.append(
                ImportResolution(
                    item.id, "ambiguous", reason=AMBIGUOUS, candidates=tuple(targets)
                )
            )
        elif not targets:
            bound.append(ImportResolution(item.id, "unresolved", reason=EXTERNAL))
        elif targets[0] not in files:
            bound.append(ImportResolution(item.id, "unresolved", reason=NOT_ANALYZED))
        else:
            bound.append(
                ImportResolution(
                    item.id,
                    "linked",
                    (ImportLink(targets[0], SCIP_IMPORT_MODULE),),
                )
            )
    return ImportBinding(tuple(bound), producers)


def _names_specifier(reference, item, view, lines):
    """Whether a module reference is the import's specifier: inside the
    statement, over the specifier written in quotes."""
    if reference.line != reference.end_line:
        return False
    text = lines.get(reference.file, reference.line)
    encoding = view.encoding(reference)
    start = _to_utf8(text, reference.column, encoding)
    end = _to_utf8(text, reference.end_column, encoding)
    if (
        not (item.line, item.column)
        <= (reference.line, start)
        <= (
            item.end_line,
            item.end_column,
        )
    ):
        return False
    written = text[start:end].decode("utf-8", "replace")
    return written in {q + item.specifier + q for q in _QUOTES}
