"""Compare Cargo.toml crate roots with rust-analyzer's SCIP index.

rust-analyzer defines a ``crate/`` symbol at line 1 of every crate root it
indexes, named by the package (``rust-analyzer cargo PACKAGE VERSION
crate/``). A crate root the analysis links from a manifest is confirmed when
rust-analyzer defines a crate of the same package in that file. Crate roots
rust-analyzer indexes and no manifest names are classified as Cargo's target
discovery by layout (``src/main.rs``, ``src/bin``, ``tests``, ``examples``,
``benches``, ``build.rs``) when they sit where it looks, so that every
unexplained difference is listed.

Usage:
  rust-analyzer scip ROOT && rm -rf ROOT/target
  python tools/compare_rust_crates_with_scip.py ROOT ROOT/index.scip
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import posixpath

import deepthought
from deepthought.imports import resolve_imports


def _discovered(file, packages):
    """Whether Cargo discovers ``file`` as a target root of a package."""
    for package in packages:
        prefix = package + "/" if package else ""
        if not file.startswith(prefix):
            continue
        parts = file[len(prefix) :].split("/")
        if parts in (["src", "main.rs"], ["build.rs"]):
            return True
        if parts[:2] == ["src", "bin"] and (
            len(parts) == 3 or (len(parts) == 4 and parts[3] == "main.rs")
        ):
            return True
        if parts[0] in {"tests", "examples", "benches"} and (
            len(parts) == 2 or (len(parts) == 3 and parts[2] == "main.rs")
        ):
            return True
    return False


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

    scip = {}
    for d in source.definitions:
        if d.native_id.endswith(" crate/"):
            scip.setdefault(real(d.file), set()).add(d.native_id.split(" ")[2])
    imports = {item.id: item for item in result.imports}
    crates = [
        (imports[r.import_id], r)
        for r in resolve_imports(result)
        if imports[r.import_id].kind in {"crate_library", "crate_target"}
    ]
    packages = {posixpath.dirname(item.file) for item, _ in crates}
    outcomes, examples, ours = Counter(), [], set()
    for item, resolution in crates:
        kind = item.names[1]
        if not resolution.links:
            outcomes[f"{kind}_{resolution.reason}"] += 1
            continue
        target = resolution.links[0].target_file
        ours.add(target)
        if target not in scip:
            outcome = f"{kind}_not_indexed"
        elif item.names[0] in scip[target]:
            outcome = f"{kind}_confirmed"
        else:
            outcome = f"{kind}_package_differs"
            examples.append([item.file, target, item.names[0], sorted(scip[target])])
        outcomes[outcome] += 1
    scip_only, scip_examples = Counter(), []
    for file in sorted(scip.keys() - ours):
        if _discovered(file, packages):
            scip_only["discovered_by_layout"] += 1
        else:
            scip_only["unexplained"] += 1
            scip_examples.append([file, sorted(scip[file])])
    print(
        json.dumps(
            {
                "manifests": len(
                    [c for c in result.coverage if c.file.endswith("Cargo.toml")]
                ),
                **dict(sorted(outcomes.items())),
                "examples": examples[:20],
                "scip_only": dict(sorted(scip_only.items())),
                "scip_only_unexplained": scip_examples[:20],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
