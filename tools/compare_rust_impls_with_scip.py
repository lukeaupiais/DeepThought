"""Compare resolved Rust impl blocks with rust-analyzer's SCIP index.

For each impl, rust-analyzer's index has an occurrence of the symbol the last
name of its type, and of its trait, refers to on the impl's first line, and
defines that symbol on the line of some declaration. A type or trait the
analysis links is confirmed when the linked declaration is on that line of
that file, with the same name; a link that differs is classified as
compare_rust_calls_with_scip.py classifies a call's. A type or trait left
unresolved is counted by reason, as missed when rust-analyzer defines it in
the repository at a declaration the analysis reads. Occurrences are looked
for on the impl's first line only; when the type and the trait share their
last name elsewhere on that line, the trait's is the last occurrence before
``for`` and the type's the first after it.

Usage:
  rust-analyzer scip ROOT && rm -rf ROOT/target
  python tools/compare_rust_impls_with_scip.py ROOT ROOT/index.scip
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import re

import deepthought
from deepthought.inheritance import resolve_rust_impls

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("index", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    result = deepthought.analyze_repository(
        root, repository_id="comparison", languages=("rust",)
    )
    source = deepthought.ingest_scip(args.index, repository_id="comparison")

    def real(file):
        # A path through a symbolic link names the file the analysis reads.
        return (root / file).resolve().relative_to(root).as_posix()

    defined = {}
    for d in source.definitions:
        names = _NAME.findall(d.native_id.split(" ", 4)[-1])
        if names:
            defined.setdefault(d.native_id, set()).add(
                (real(d.file), d.line, names[-1])
            )
    occurrences = {}
    for r in source.references:
        if r.native_id.startswith("local "):
            continue
        names = _NAME.findall(r.native_id.split(" ", 4)[-1])
        if names:
            key = (real(r.file), r.line, names[-1])
            occurrences.setdefault(key, {}).setdefault(r.column, set()).add(r.native_id)
    by_id = {d.id: d for d in result.definitions}
    declared = {}
    for d in result.definitions:
        declared.setdefault((d.file, d.line, d.symbol.split(".")[-1]), []).append(d)
    impls = {i.id: i for i in result.impls}
    separators = {}
    for item in result.impls:
        if item.trait is None:
            continue
        text = (root / item.file).read_bytes().splitlines()[item.line - 1]
        # The `for` between trait and type; `for<'a>` binds lifetimes.
        found = [
            m.start() for m in re.finditer(rb"\bfor\b(?!\s*<)", text[item.column :])
        ]
        if found:
            separators[(item.file, item.line)] = item.column + found[0]
    outcomes, examples = Counter(), {}

    def classify(item, part, path, resolution):
        name = path.split("::")[-1]
        columns = occurrences.get((item.file, item.line, name), {})
        split = separators.get((item.file, item.line))
        if item.trait is not None and split is not None and len(columns) > 1:
            # The name also occurs in type arguments or bounds
            # (`impl<T: io::Read> Read for X<T>`, `impl From<A> for A`): the
            # trait is the last occurrence before `for`, the type the first
            # after it.
            if part == "trait":
                chosen = sorted(c for c in columns if c < split)[-1:]
            else:
                chosen = sorted(c for c in columns if c > split)[:1]
            columns = {c: columns[c] for c in chosen}
        symbols = set().union(*columns.values())
        expected = {
            location
            for s in symbols
            for location in defined.get(s, ())
            if location[2] == name
        }
        ids = [i for i in (resolution.target_id, *resolution.candidates) if i]
        ours = [
            (by_id[i].file, by_id[i].line, by_id[i].symbol.split(".")[-1]) for i in ids
        ]
        if resolution.status == "linked":
            if not expected:
                outcome = (
                    "linked_outside_repository" if symbols else "linked_not_indexed"
                )
            elif ours[0] in expected:
                outcome = "linked_confirmed"
            elif not any(location in declared for location in expected):
                # rust-analyzer's declaration is in a macro body not read.
                outcome = "linked_scip_declaration_not_parsed"
            elif any(
                d.symbol == by_id[ids[0]].symbol
                for location in expected
                for d in declared.get(location, ())
                if d.file == by_id[ids[0]].file
            ):
                # Another declaration of the same name in the same scope:
                # rust-analyzer indexes another #[cfg] configuration.
                outcome = "linked_other_cfg_declaration"
            else:
                outcome = "linked_contradicted"
        elif resolution.status == "ambiguous":
            if not expected:
                outcome = "ambiguous_not_indexed"
            elif expected & set(ours):
                outcome = "ambiguous_includes_it"
            else:
                outcome = "ambiguous_excludes_it"
        elif expected and not all(location in declared for location in expected):
            # rust-analyzer's declaration is in a macro body not read.
            outcome = f"{resolution.reason}_scip_declaration_not_parsed"
        elif expected:
            outcome = f"missed_{resolution.reason}"
        elif symbols:
            outcome = f"{resolution.reason}_outside_repository"
        else:
            outcome = f"{resolution.reason}_not_indexed"
        outcome = f"{part}_{outcome}"
        outcomes[outcome] += 1
        if (
            "missed" in outcome
            or "linked_scip" in outcome
            or "linked_other" in outcome
            or (outcome.endswith(("contradicted", "excludes_it")))
        ):
            examples.setdefault(outcome, []).append(
                [item.file, item.line, path, ours, sorted(expected)]
            )

    resolutions = resolve_rust_impls(result)
    edges = 0
    for resolution in resolutions:
        item = impls[resolution.impl_id]
        classify(item, "type", item.type, resolution.type)
        if resolution.trait is not None:
            classify(item, "trait", item.trait, resolution.trait)
            edges += (
                resolution.type.status == "linked"
                and resolution.trait.status == "linked"
            )
    print(
        json.dumps(
            {
                "impls": len(impls),
                "trait_edges": edges,
                **dict(sorted(outcomes.items())),
                "examples": {k: v[:12] for k, v in sorted(examples.items())},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
