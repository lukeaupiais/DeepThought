"""Compare resolved Rust ``mod name;`` files with rust-analyzer's SCIP index.

For each ``mod name;`` the analysis records, rust-analyzer's index has an
occurrence of the module's symbol on the declaration, and defines that module
in the file it includes. A link is confirmed when it reaches that file.
Declarations rust-analyzer did not index (an inactive ``#[cfg]``, a file no
crate builds) have no occurrence and are counted separately. A module declared
more than once in one file (one per ``#[cfg]``) has one symbol for all its
declarations, so those are not compared. A declaration the analysis misses is
classified when its file is chosen by ``#[cfg_attr(..., path = ...)]``, which is
not read, or lies through a symbolic link, which the analysis does not follow.

Usage:
  rust-analyzer scip ROOT && rm -rf ROOT/target
  python tools/compare_rust_modules_with_scip.py ROOT ROOT/index.scip
"""

import argparse
from collections import Counter
import json
from pathlib import Path

import deepthought
from deepthought.imports import resolve_imports


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
        if d.native_id.endswith("/"):
            defined.setdefault(d.native_id, set()).add(real(d.file))
    occurrences = {}
    for r in source.references:
        if r.native_id.endswith("/"):
            descriptor = r.native_id.split(" ", 4)[-1]
            name = descriptor.rstrip("/").rsplit("/", 1)[-1].strip("`")
            occurrences.setdefault((real(r.file), r.line, name), set()).add(r.native_id)
    imports = {item.id: item for item in result.imports}

    def declaration(item):
        # The file, the inline modules around the declaration, and its name.
        if item.kind == "module_path":
            return item.file, item.names, item.local_names[0]
        return item.file, tuple(item.specifier.split("/")[:-1]), item.local_names[0]

    declared = Counter(declaration(item) for item in result.imports)

    def missed(item):
        lines = (root / item.file).read_text().split("\n")[: item.line - 1]
        attributes = []
        while lines and lines[-1].strip().startswith("#["):
            attributes.append(lines.pop())
        if any("cfg_attr" in a and "path" in a for a in attributes):
            return "cfg_attr_path"
        directory = (root / item.file).parent
        for part in Path(item.specifier).parts:
            directory = directory / part
            if directory.is_symlink():
                return "through_symlink"
        return None

    outcomes, examples = Counter(), []
    for resolution in resolve_imports(result):
        item = imports[resolution.import_id]
        symbols = set()
        for line in range(item.line, item.end_line + 1):
            symbols |= occurrences.get((item.file, line, item.local_names[0]), set())
        expected = {f for s in symbols for f in defined.get(s, ())}
        ours = {link.target_file for link in resolution.links}
        if not expected:
            outcome = (
                "linked_not_indexed" if ours else f"{resolution.reason}_not_indexed"
            )
        elif len(expected) > 1:
            # rust-analyzer gives a package's library and its test crates the
            # same symbols, so one symbol can define modules in both.
            outcome = (
                "shared_symbol_includes_ours"
                if ours and ours <= expected
                else "shared_symbol_not_compared"
            )
        elif declared[declaration(item)] > 1:
            outcome = "cfg_variant_not_compared"
        elif ours == expected:
            outcome = "confirmed"
        elif ours:
            outcome = "contradicted"
        else:
            outcome = f"missed_{missed(item) or resolution.reason}"
        outcomes[outcome] += 1
        if outcome in {"contradicted", f"missed_{resolution.reason}"}:
            examples.append(
                [item.file, item.line, item.specifier, sorted(ours), sorted(expected)]
            )
    print(
        json.dumps(
            {
                "module_declarations": len(result.imports),
                **dict(sorted(outcomes.items())),
                "examples": examples[:20],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
