"""Compare resolved Rust calls with rust-analyzer's SCIP index.

For each call site, rust-analyzer's index has an occurrence of the symbol the
callee's last name refers to on the call's line, and defines that symbol on
the line of some declaration. A call the analysis links is confirmed when the
linked declaration is on that line of that file, with the same name. A
link that differs is classified: rust-analyzer naming a module of the same
name (a call cannot name a module), its declaration lying in a macro body the
analysis does not read, or another declaration of the same name in the same
scope (another ``#[cfg]`` configuration).
Unresolved calls are counted by reason, as missed when rust-analyzer defines
the callee in the repository, and an ambiguous call by whether its candidates
include rust-analyzer's.

Usage:
  rust-analyzer scip ROOT && rm -rf ROOT/target
  python tools/compare_rust_calls_with_scip.py ROOT ROOT/index.scip
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import re

import deepthought
from deepthought.calls import resolve_calls

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
            occurrences.setdefault(key, set()).add(r.native_id)
    by_id = {d.id: d for d in result.definitions}
    declared = {}
    for d in result.definitions:
        declared.setdefault((d.file, d.line, d.symbol.split(".")[-1]), []).append(d)
    sites = {s.id: s for s in result.call_sites}
    outcomes, examples = Counter(), {}
    for resolution in resolve_calls(result):
        site = sites[resolution.call_id]
        name = re.split(r"::|\.", site.callee)[-1]
        symbols = set()
        for line in range(site.line, site.end_line + 1):
            symbols |= occurrences.get((site.file, line, name), set())
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
            elif all(s.endswith("/") for s in symbols):
                # rust-analyzer names a module of the same name (`pub use
                # once::once`); a call cannot name a module.
                outcome = "linked_scip_names_module"
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
        elif expected:
            outcome = f"missed_{resolution.reason}"
        elif symbols:
            outcome = f"{resolution.reason}_outside_repository"
        else:
            outcome = f"{resolution.reason}_not_indexed"
        outcomes[outcome] += 1
        if outcome.startswith(("missed", "linked_scip", "linked_other")) or (
            outcome.endswith(("contradicted", "excludes_it"))
        ):
            examples.setdefault(outcome, []).append(
                [site.file, site.line, site.callee, ours, sorted(expected)]
            )
    print(
        json.dumps(
            {
                "call_sites": len(sites),
                **dict(sorted(outcomes.items())),
                "examples": {k: v[:12] for k, v in sorted(examples.items())},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
