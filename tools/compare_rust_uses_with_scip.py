"""Compare resolved Rust ``use`` declarations with rust-analyzer's SCIP index.

For each ``use`` leaf (and each ``path::*`` glob's module), rust-analyzer's
index has an occurrence of the symbol it names on the declaration's lines,
and defines that symbol in some file. A link the analysis marks
``rust-use-declared`` (the name is declared in the linked module, or is the
module) or ``rust-use-reexport`` (the declaration a chain of re-exports
reaches) is confirmed when that is the file. A ``rust-use-module`` link names
the module the path goes through when the name is not declared there (a
re-export, a constant, a variant or a macro-generated item), so it is counted
as such when rust-analyzer's definition is elsewhere.

Usage:
  rust-analyzer scip ROOT && rm -rf ROOT/target
  python tools/compare_rust_uses_with_scip.py ROOT ROOT/index.scip
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import re

import deepthought
from deepthought.imports import (
    RUST_USE_DECLARED,
    RUST_USE_MODULE,
    RUST_USE_REEXPORT,
    resolve_imports,
)

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
        defined.setdefault(d.native_id, set()).add(real(d.file))
    occurrences = {}
    for r in source.references:
        if r.native_id.startswith("local "):
            continue
        names = _NAME.findall(r.native_id.split(" ", 4)[-1])
        if names:
            key = (real(r.file), r.line, names[-1])
            occurrences.setdefault(key, set()).add(r.native_id)
    uses = {
        item.id: item
        for item in result.imports
        if item.kind in {"use", "use_glob", "extern_crate"}
    }
    outcomes, examples = Counter(), {}
    for resolution in resolve_imports(result):
        item = uses.get(resolution.import_id)
        if item is None:
            continue
        name = item.specifier.split("::")[-1]
        symbols = set()
        for line in range(item.line, item.end_line + 1):
            symbols |= occurrences.get((item.file, line, name), set())
        expected = {f for s in symbols for f in defined.get(s, ())}
        link = resolution.links[0] if resolution.links else None
        if link is None:
            if expected:
                outcome = f"missed_{resolution.reason}"
            elif symbols:
                outcome = f"{resolution.reason}_outside_repository"
            else:
                outcome = f"{resolution.reason}_not_indexed"
        else:
            kind = {
                RUST_USE_DECLARED: "declared",
                RUST_USE_REEXPORT: "reexport",
                RUST_USE_MODULE: "module",
            }.get(link.rule_id, "extern_crate")
            if not expected:
                where = "outside_repository" if symbols else "not_indexed"
                outcome = f"{kind}_{where}"
            elif link.target_file in expected:
                outcome = f"{kind}_confirmed"
            elif kind == "module":
                outcome = "module_defined_elsewhere"
            else:
                outcome = f"{kind}_contradicted"
        outcomes[outcome] += 1
        if outcome.endswith(("contradicted", "elsewhere")) or outcome.startswith(
            "missed"
        ):
            examples.setdefault(outcome, []).append(
                [
                    item.file,
                    item.line,
                    item.specifier,
                    link.target_file if link else None,
                    sorted(expected),
                ]
            )
    print(
        json.dumps(
            {
                "uses": len(uses),
                **dict(sorted(outcomes.items())),
                "examples": {k: v[:12] for k, v in sorted(examples.items())},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
