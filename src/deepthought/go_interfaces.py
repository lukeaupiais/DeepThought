"""Go interfaces implemented by the types of one AnalysisResult.

A Go type implements an interface when its method set has every method of
the interface with an identical signature; nothing declares it. This module
compares method sets: a concrete type (a struct or other named type, or a
pointer to it) declared in the analyzed files implements an interface
declared there when, for each of the interface's methods (its own and those
of the interfaces it embeds), the type declares exactly one method of that
name whose signature is identical once each named type in it is tied to its
package. An unexported method matches only within its package.

An embedded ``error``, or an exported interface of the standard library
(``io.Reader``; ``go_std_interfaces.json``, see tools/go_std_interfaces.go),
adds its methods to the set; calls through those methods have no
declaration in the analyzed files to pair. It never guesses: an interface
is left out when an embedded element is no such interface and not one
interface of the analyzed files (a type constraint, another module's
interface), or when a method's signature cannot be written
(``Definition.signature`` is None: generics, struct or non-empty interface
literals); a type is left out when its name is declared more than once in its
package, and a method when a named type in its signature cannot be tied to a
package. A type and an interface (or an interface and one it embeds) in
different Go modules of the analyzed files are not matched: the module sees
the other through a versioned dependency, not these declarations. Methods
promoted from embedded fields are not counted, so such a
type is missed, never wrongly matched.
"""

from collections import defaultdict
from dataclasses import dataclass
from functools import cache
from importlib import resources
import json
import posixpath
import re

from .calls import name_bindings, package_peers
from .imports import _go_modules, _within
from .models import AnalysisResult

GO_INTERFACE_METHOD_SET = "tree-sitter.go-interface-method-set"

_CONCRETE = {"Struct", "Type"}
_PREDECLARED = frozenset(
    "bool byte complex64 complex128 error float32 float64 int int8 int16 int32 "
    "int64 rune string uint uint8 uint16 uint32 uint64 uintptr".split()
)
_KEYWORDS = frozenset({"func", "map", "chan", "interface"})
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?")
_VERSION = re.compile(r"v[0-9]+")
# Identical types written differently (byte is uint8, rune is int32).
_SPELLED = {"byte": "uint8", "rune": "int32", "any": "interface{}"}
_ERROR = {"Error": "()(string)"}


@cache
def _std_interfaces():
    """{import path: {interface: {method: signature}}} of the standard
    library, as tools/go_std_interfaces.go writes it."""
    text = resources.files(__package__).joinpath("go_std_interfaces.json")
    return json.loads(text.read_text("utf-8"))["interfaces"]


@dataclass(frozen=True)
class GoImplementation:
    """A concrete type implementing an interface, with each interface
    method paired to the type's method a call through it can run."""

    type_id: str
    interface_id: str
    methods: tuple[tuple[str, str], ...]  # (interface method id, method id)


def _import_name(path):
    """The name a Go package is conventionally declared with: the last
    element of its path, without a major version element or ``.vN``
    suffix and without a ``go-`` prefix."""
    parts = path.split("/")
    name = parts[-1]
    if _VERSION.fullmatch(name) and len(parts) > 1:
        name = parts[-2]
    name = re.sub(r"\.v[0-9]+$", "", name)
    return name[3:] if name.startswith("go-") else name


def resolve_go_implementations(
    result: AnalysisResult,
) -> tuple[GoImplementation, ...]:
    """Every (type, interface) pair, in identity order."""
    languages = {item.file: item.language for item in result.coverage}
    go = [d for d in result.definitions if languages.get(d.file) == "go"]
    if not go:
        return ()
    bindings = name_bindings(result)
    by_symbol, modules = bindings.by_symbol, bindings.modules
    peers = package_peers(result)
    roots = _go_modules(result)

    def module(file):
        """The directory of the deepest go.mod above ``file``, or None."""
        return next((d for d, _ in roots if _within(d, file)), None)

    imports = defaultdict(list)
    for item in result.imports:
        if languages.get(item.file) == "go":
            imports[item.file].append(item)

    def package_of(file, written):
        """The package a type written as ``written`` in ``file`` belongs to,
        and its name there: (``./`` and the directory of a package of the
        analyzed files, or the import path of another, name). None when
        the qualifier names no one import of the file."""
        *qualifier, name = written.split(".")
        if not qualifier:
            return "./" + posixpath.dirname(file), name
        bound = modules.get((file, qualifier[0]))
        if bound and bound[0]:
            return "./" + posixpath.dirname(bound[0][0]), name
        found = {
            item.specifier
            for item in imports[file]
            if item.local_names == (qualifier[0],)
            or (not item.local_names and _import_name(item.specifier) == qualifier[0])
        }
        return (found.pop(), name) if len(found) == 1 else None

    def canonical(file, signature):
        """``signature`` with each named type tied to its package."""
        if signature is None:
            return None
        dotted = any(item.local_names == ("*",) for item in imports[file])
        parts, end = [], 0
        for match in _NAME.finditer(signature):
            word = match.group()
            parts.append(signature[end : match.start()])
            end = match.end()
            if word in _KEYWORDS:
                parts.append(word)
                continue
            if word in _SPELLED:
                parts.append(_SPELLED[word])
                continue
            if word in _PREDECLARED:
                parts.append(word)
                continue
            if dotted and "." not in word:
                return None  # A dot import may declare it.
            package = package_of(file, word)
            if package is None:
                return None
            parts.append(f'"{package[0]}".{package[1]}')
        parts.append(signature[end:])
        return "".join(parts)

    # Interface methods are the methods declared inside an interface's span.
    interfaces = [d for d in go if d.kind == "Interface"]
    spans = defaultdict(list)
    for d in interfaces:
        spans[d.file].append(d)
    own = defaultdict(list)
    methods = defaultdict(lambda: defaultdict(list))  # (dir, type) -> name -> defs
    for d in go:
        if d.kind != "Method" or "." not in d.symbol:
            continue
        owner = next(
            (
                i
                for i in spans.get(d.file, ())
                if (i.line, i.column) <= (d.line, d.column)
                and (d.end_line, d.end_column) <= (i.end_line, i.end_column)
            ),
            None,
        )
        if owner is not None:
            own[owner.id].append(d)
            continue
        type_name, name = d.symbol.rsplit(".", 1)
        methods[(posixpath.dirname(d.file), type_name)][name].append(d)

    def declared(file, written, kinds):
        """The one type ``written`` in ``file`` names, if of ``kinds``."""
        package = package_of(file, written)
        if package is None:
            return None
        files = (
            peers.get(file, (file,))
            if "." not in written
            else (modules.get((file, written.split(".")[0])) or [()])[0]
        )
        found = [
            d
            for f in files
            for d in by_symbol.get((f, package[1]), ())
            if d.kind in {"Struct", "Type", "Interface"}
        ]
        return found[0] if len(found) == 1 and found[0].kind in kinds else None

    def library(file, written):
        """The method set of ``error`` or of a standard library interface
        ``written`` in ``file``, without declarations; None otherwise."""
        if written == "error" and not by_symbol_declares(file, "error"):
            return {name: (sig, "", None) for name, sig in _ERROR.items()}
        package = package_of(file, written) if "." in written else None
        if package is None or package[0].startswith("./"):
            return None
        methods = _std_interfaces().get(package[0], {}).get(package[1])
        if methods is None:
            return None
        return {name: (sig, package[0], None) for name, sig in methods.items()}

    def by_symbol_declares(file, name):
        return any(by_symbol.get((f, name)) for f in peers.get(file, (file,)))

    method_sets = {}

    def method_set(interface, seen=frozenset()):
        """{name: (signature, package, interface method)}, or None."""
        if interface.id in method_sets:
            return method_sets[interface.id]
        if interface.id in seen:
            return None
        found, package = {}, posixpath.dirname(interface.file)
        for d in own[interface.id]:
            name = d.symbol.rsplit(".", 1)[1]
            written = canonical(d.file, d.signature)
            if written is None or name in found:
                found = None
                break
            found[name] = (written, "./" + package, d)
        for embedded in interface.embeds or () if found is not None else ():
            target = declared(interface.file, embedded, {"Interface"})
            if target is not None:
                inner = (
                    method_set(target, seen | {interface.id})
                    if module(target.file) == module(interface.file)
                    else None
                )
            else:
                inner = library(interface.file, embedded)
            if inner is None:
                found = None
                break
            for name, value in inner.items():
                if name in found and found[name][0] != value[0]:
                    found = None
                    break
                found.setdefault(name, value)
            if found is None:
                break
        method_sets[interface.id] = found
        return found

    # Concrete types, each with the one method of every name it declares.
    candidates = defaultdict(set)  # method name -> type ids
    concrete = {}
    for d in go:
        if d.kind not in _CONCRETE or "." in d.symbol:
            continue
        package = posixpath.dirname(d.file)
        named = [
            other
            for f in peers.get(d.file, (d.file,))
            for other in by_symbol.get((f, d.symbol), ())
            if other.kind in {"Struct", "Type", "Interface"}
        ]
        if len(named) != 1:
            continue
        own_methods = {}
        for name, defs in methods.get((package, d.symbol), {}).items():
            if len(defs) == 1:
                own_methods[name] = (
                    canonical(defs[0].file, defs[0].signature),
                    defs[0],
                )
        concrete[d.id] = (d, package, own_methods)
        for name in own_methods:
            candidates[name].add(d.id)

    found = []
    for interface in sorted(interfaces, key=lambda item: item.id):
        wanted = method_set(interface)
        if not wanted:
            continue
        names = sorted(wanted, key=lambda name: (len(candidates[name]), name))
        types = set(candidates[names[0]])
        for name in names[1:]:
            types &= candidates[name]
        for type_id in sorted(types):
            d, package, own_methods = concrete[type_id]
            if module(d.file) != module(interface.file):
                continue
            pairs = []
            for name in sorted(wanted):
                signature, declaring, interface_method = wanted[name]
                written, method = own_methods[name]
                if (
                    written is None
                    or written != signature
                    or (not name[0].isupper() and declaring != "./" + package)
                ):
                    break
                if interface_method is not None:
                    pairs.append((interface_method.id, method.id))
            else:
                found.append(GoImplementation(type_id, interface.id, tuple(pairs)))
    return tuple(found)
