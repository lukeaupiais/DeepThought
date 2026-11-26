"""Go interface implementations recorded by scip-go.

scip-go type-checks each package and records, on the symbol of every named
type and method, the interfaces (and interface methods) it implements as
``is_implementation`` relationships: a type whose method set (pointer
receivers, promoted methods and embedded interfaces included, as the Go
type checker reads them) has every method of the interface. Each recorded
relationship whose two ends are declarations of the analyzed files is one
link: a concrete type IMPLEMENTS an interface, and an interface method
DISPATCHES_TO the method a call through it can run. An interface or method
declared outside the repository (``io.Reader``, ``error``) has no
declaration to link to.

The method-set rule of ``deepthought.go_interfaces`` stands where scip-go
records nothing it could: for types of files no scip-go index covers, for
interfaces declared inside a function (scip-go takes only package-level
types), and, for scip-go 0.2.7, where the interface's package has in-package
tests and the type is in another package: scip-go then reads the interface
from the package's test variant, whose named types are not the ones the
type's methods are written with, and records no implementation (see
``method_set_stands``).
"""

from dataclasses import dataclass
import posixpath
import re

from .scip_calls import ScipReferences, _declaration, _key

SCIP_GO_IMPLEMENTATION = "scip.go-implementation"

_CONCRETE = {"Struct", "Type"}


@dataclass(frozen=True)
class ScipImplementations:
    # (type id, interface id), sorted.
    implements: tuple[tuple[str, str], ...]
    # (interface method id, method id), sorted.
    dispatches: tuple[tuple[str, str], ...]
    # Parsed Go files a scip-go index covers: the method-set rule does not
    # run for their types.
    covered: frozenset[str]
    # (indexer name@version, receipt digest) per pair of either kind.
    producers: dict
    # Directories of covered Go packages with in-package test files.
    tested: frozenset[str] = frozenset()

    def method_set_stands(self, type_, interface) -> bool:
        """Whether the method-set rule's (type, interface) pair stands:
        scip-go could not record it (see the module docstring)."""
        if type_.file not in self.covered:
            return True
        if "." in interface.symbol:
            return True  # Declared inside a function.
        directory = posixpath.dirname(interface.file)
        return directory in self.tested and posixpath.dirname(type_.file) != directory


def bind_implementations(result, references: ScipReferences) -> ScipImplementations:
    """The links scip-go records between declarations of ``result``.
    ``references`` is a ``deepthought.scip_calls.ScipReferences`` over the
    run's indexes."""
    languages = {item.file: item.language for item in result.coverage}
    views = {}
    covered = set()
    for file, view in references.covering.items():
        if view.tool.startswith("scip-go@") and languages.get(file) == "go":
            covered.add(file)
            # Indexes of one indexer share their definitions.
            views[id(view.definitions)] = view
    interface_methods = _interface_methods(result.definitions)
    implements, dispatches, producers = set(), set(), {}
    for view in views.values():
        for found in view.definitions.values():
            for scip in found:
                if not scip.implements or scip.file not in covered:
                    continue
                source = _declaration(scip, view, references.lines, references.in_file)
                if source is None:
                    continue
                for native in scip.implements:
                    for target_scip in view.definitions.get(
                        _key(scip.file, native), ()
                    ):
                        target = _declaration(
                            target_scip, view, references.lines, references.in_file
                        )
                        if target is None or target.id == source.id:
                            continue
                        if source.kind in _CONCRETE and target.kind == "Interface":
                            pair = (source.id, target.id)
                            implements.add(pair)
                        elif (
                            source.kind == "Method"
                            and source.id not in interface_methods
                            and target.id in interface_methods
                        ):
                            pair = (target.id, source.id)
                            dispatches.add(pair)
                        else:
                            continue
                        producers[pair] = (view.tool, view.receipt)
    return ScipImplementations(
        tuple(sorted(implements)),
        tuple(sorted(dispatches)),
        frozenset(covered),
        producers,
        _tested(covered, references.lines),
    )


_PACKAGE = re.compile(rb"^package\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)


def _package(lines, file):
    """The package clause's name of a Go file, or None."""
    text = b"\n".join(lines.get(file, n) for n in range(1, 200))
    found = _PACKAGE.search(text)
    return found and found.group(1)


def _tested(covered, lines):
    """Directories of ``covered`` holding a _test.go file of the package
    itself (not an external ``_test`` package)."""
    packages = {}
    for file in sorted(covered):
        if not file.endswith("_test.go"):
            packages.setdefault(posixpath.dirname(file), _package(lines, file))
    return frozenset(
        posixpath.dirname(file)
        for file in covered
        if file.endswith("_test.go")
        and _package(lines, file) is not None
        and _package(lines, file) == packages.get(posixpath.dirname(file))
    )


def _interface_methods(definitions):
    """Ids of the methods declared inside a Go interface's span."""
    spans = {}
    for d in definitions:
        if d.kind == "Interface":
            spans.setdefault(d.file, []).append(d)
    return {
        d.id
        for d in definitions
        if d.kind == "Method"
        and any(
            (i.line, i.column) <= (d.line, d.column)
            and (d.end_line, d.end_column) <= (i.end_line, i.end_column)
            for i in spans.get(d.file, ())
        )
    }
