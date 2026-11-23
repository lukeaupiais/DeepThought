"""Metamorphic checks: a source edit changes only the edges it should.

Each transformation edits a copy of a workspace in a way whose effect on the
graph is known in advance, analyzes both, and compares every call, import,
supertype and impl resolution after mapping the original through the
expected effect:

  shift FILE N     N blank lines prepended to FILE: positions in FILE, and
                   targets declared in FILE, move down N lines; nothing else
                   changes.
  rename OLD NEW   every whole-word OLD in every source file becomes NEW (same
                   length, so columns hold): the graph is the original with
                   OLD replaced by NEW in names, callees and symbols.
  add FILE         a new file whose declarations use names found nowhere
                   else: every original resolution is unchanged, except that
                   an import of a Go package also links the package's new
                   file.

Usage:
  python tools/check_metamorphic.py WORKSPACE --language L [--trials N]

Exit status 1 when any transformation's prediction fails; the report names
the first differing edges.
"""

import argparse
import json
from pathlib import Path
import random
import re
import shutil
import sys
import tempfile

EXTENSIONS = {
    "python": (".py",),
    "typescript": (".ts", ".tsx"),
    "javascript": (".js", ".jsx", ".mjs", ".cjs"),
    "go": (".go",),
    "java": (".java",),
    "rust": (".rs",),
}
# A declaration with no effect on any other name, one per language.
UNRELATED = {
    "python": (
        "zq_unrelated.py",
        "def zq_unrelated_fn():\n    return zq_unrelated_fn\n",
    ),
    "typescript": (
        "zq_unrelated.ts",
        "export function zqUnrelatedFn(): void { zqUnrelatedFn(); }\n",
    ),
    "javascript": (
        "zq_unrelated.js",
        "export function zqUnrelatedFn() { zqUnrelatedFn(); }\n",
    ),
    "go": ("zq_unrelated_zq.go", None),  # package clause copied from a peer
    "java": ("ZqUnrelated.java", "class ZqUnrelated { void zqRun() { zqRun(); } }\n"),
    "rust": (None, None),  # a new module would need a `mod` line elsewhere
}
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_RENAMABLE = re.compile(r"[a-z][A-Za-z0-9_]{5,}")
_EXCLUDED = {"constructor", "toString", "valueOf"}


def edges(result):
    """Every resolution keyed by the position and text of its fact.

    Locations are ``("@", file, line)`` so a transformation can map them.
    """
    from deepthought.calls import resolve_calls
    from deepthought.imports import resolve_imports
    from deepthought.inheritance import resolve_rust_impls, resolve_supertypes

    defs = {d.id: ("@", d.file, d.line, d.symbol, d.kind) for d in result.definitions}

    def target(resolution):
        if resolution is None:
            return None
        return (
            resolution.status,
            defs.get(resolution.target_id),
            resolution.rule_id,
            resolution.reason,
            tuple(sorted(defs[c] for c in resolution.candidates if c in defs)),
        )

    graph = {}
    sites = {s.id: s for s in result.call_sites}
    for r in resolve_calls(result):
        s = sites[r.call_id]
        graph[("call", ("@", s.file, s.line), s.column, s.callee)] = target(r)
    imports = {i.id: i for i in result.imports}
    for r in resolve_imports(result):
        i = imports[r.import_id]
        graph[("import", ("@", i.file, i.line), i.column, i.specifier)] = (
            r.status,
            tuple(sorted((link.target_file, link.rule_id) for link in r.links)),
            r.reason,
            tuple(sorted(r.candidates)),
        )
    bases = {b.id: b for b in result.supertypes}
    for r in resolve_supertypes(result):
        b = bases[r.call_id]
        graph[("supertype", ("@", b.file, b.line), b.column, b.name)] = target(r)
    impls = {i.id: i for i in result.impls}
    for r in resolve_rust_impls(result):
        i = impls[r.impl_id]
        key = ("impl", ("@", i.file, i.line), i.column, i.type, i.trait)
        graph[key] = (target(r.type), target(r.trait))
    return graph


def _map(value, location=None, text=None):
    """Apply a location function and a text function throughout ``value``."""
    if isinstance(value, tuple):
        if value and value[0] == "@" and location:
            value = location(value)
        return tuple(_map(v, location, text) for v in value)
    if isinstance(value, str) and text:
        return text(value)
    return value


def predict(graph, location=None, text=None):
    return {_map(k, location, text): _map(v, location, text) for k, v in graph.items()}


def source_files(root, languages):
    suffixes = tuple(s for lang in languages for s in EXTENSIONS[lang])
    return sorted(
        p
        for p in Path(root).rglob("*")
        if p.is_file() and p.suffix in suffixes and ".git" not in p.parts
    )


def analyze(root, languages):
    import deepthought

    return deepthought.analyze_repository(
        root,
        repository_id="metamorphic",
        languages=tuple(languages),
        max_files=100_000,
        max_total_bytes=1_000_000_000,
    )


def shift(root, rng, languages, original):
    files = source_files(root, languages)
    path = rng.choice(files)
    relative = path.relative_to(root).as_posix()
    lines = rng.randint(1, 7)
    path.write_bytes(b"\n" * lines + path.read_bytes())

    def location(at):
        return (at[0], at[1], at[2] + lines, *at[3:]) if at[1] == relative else at

    return f"shift {relative} {lines}", predict(original, location=location), None


def rename(root, rng, languages, original, result):
    files = source_files(root, languages)
    texts = {p: p.read_text("utf-8", "replace") for p in files}
    words = set()
    for text in texts.values():
        words.update(_WORD.findall(text))
    stems = {part for p in files for part in p.relative_to(root).with_suffix("").parts}
    # Prefer names that calls use, so the rename moves edges.
    called = {re.split(r"::|\.", s.callee)[-1] for s in result.call_sites}
    candidates = sorted(
        {
            d.symbol.split(".")[-1]
            for d in result.definitions
            if d.kind in {"Function", "Method"}
        }
        & called - stems - _EXCLUDED
    )
    candidates = [
        c for c in candidates if _RENAMABLE.fullmatch(c) and not c.startswith("__")
    ]
    if not candidates:
        return None
    old = rng.choice(candidates)
    for replacement in "qxzjkvw":
        new = old[:-1] + (replacement if old[-1] != replacement else "y")
        if new not in words:
            break
    else:
        return None
    pattern = re.compile(rf"\b{re.escape(old)}\b")
    for path, text in texts.items():
        if pattern.search(text):
            path.write_text(pattern.sub(new, text), "utf-8")
    return (
        f"rename {old} {new}",
        predict(original, text=lambda s: pattern.sub(new, s)),
        None,
    )


def add(root, rng, languages, original):
    options = [lang for lang in languages if UNRELATED[lang][0]]
    if not options:
        return None
    language = rng.choice(options)
    name, content = UNRELATED[language]
    if language == "go":
        peer = rng.choice(source_files(root, ["go"]))
        package = re.search(r"^package\s+(\w+)", peer.read_text("utf-8"), re.M)
        if not package:
            return None
        directory = peer.parent
        content = f"package {package.group(1)}\n\nfunc zqUnrelatedFn() {{ zqUnrelatedFn() }}\n"
    else:
        directory = Path(root)
    target = directory / name
    target.write_text(content, "utf-8")
    added = target.relative_to(root).as_posix()
    return f"add {added}", original, added


def _without_links_to(value, file):
    if value and value[0] in {"linked", "ambiguous"} and isinstance(value[1], tuple):
        links = value[1]
        if links and isinstance(links[0], tuple) and len(links[0]) == 2:
            return (
                value[0],
                tuple(link for link in links if link[0] != file),
                *value[2:],
            )
    return value


def run(workspace, languages, trials=6, seed=0):
    """Apply ``trials`` transformations; return one report per trial."""
    rng = random.Random(seed)
    base = analyze(workspace, languages)
    original = edges(base)
    reports = []
    kinds = ["shift", "rename", "add"]
    for trial in range(trials):
        kind = kinds[trial % len(kinds)]
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch, "w")
            shutil.copytree(
                workspace, root, ignore=shutil.ignore_patterns(".git"), symlinks=True
            )
            if kind == "shift":
                outcome = shift(root, rng, languages, original)
            elif kind == "rename":
                outcome = rename(root, rng, languages, original, base)
            else:
                outcome = add(root, rng, languages, original)
            if outcome is None:
                continue
            name, expected, added = outcome
            actual = edges(analyze(root, languages))
        if added:
            actual = {
                k: _without_links_to(v, added)
                for k, v in actual.items()
                if k[1][1] != added
            }
        missing = sorted(set(expected) - set(actual), key=repr)
        extra = sorted(set(actual) - set(expected), key=repr)
        changed = sorted(
            (k for k in set(expected) & set(actual) if expected[k] != actual[k]),
            key=repr,
        )
        reports.append(
            {
                "transformation": name,
                "edges": len(expected),
                "missing": len(missing),
                "extra": len(extra),
                "changed": len(changed),
                "examples": {
                    "missing": missing[:3],
                    "extra": extra[:3],
                    "changed": [[k, expected[k], actual[k]] for k in changed[:3]],
                },
            }
        )
    return reports


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--language", action="append", required=True)
    parser.add_argument("--trials", type=int, default=6)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    reports = run(args.workspace.resolve(), args.language, args.trials, args.seed)
    print(json.dumps(reports, indent=2, default=str))
    failed = any(r["missing"] or r["extra"] or r["changed"] for r in reports)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
