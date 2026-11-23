"""Compare Tree-sitter Go declarations with scip-go definitions.

A Go function, method or named type is matched when scip-go defines a global
symbol of the same name on the same line of the same file. Methods are also
checked for their receiver: the scip-go symbol must belong to the receiver
type that Tree-sitter named. Local types inside functions, interface method
elements and files excluded by build constraints have no comparable scip-go
symbol and are listed separately.

Usage:
  python tools/compare_go_declarations_with_scip.py ROOT INDEX.scip
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import re

import deepthought

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("index", type=Path)
    args = parser.parse_args()
    result = deepthought.analyze_repository(
        args.root, repository_id="comparison", languages=("go",)
    )
    source = deepthought.ingest_scip(args.index, repository_id="comparison")
    scip = {}
    for d in source.definitions:
        descriptor = d.native_id.split(" ")[-1]
        # Functions and methods end in `().`; named types in `#`.
        if descriptor.endswith("().") or descriptor.endswith("#"):
            scip[(d.file, d.line, _NAME.findall(descriptor)[-1])] = descriptor
    ours = {
        (d.file, d.line, d.symbol.split(".")[-1]): d
        for d in result.definitions
        if d.kind in {"Function", "Method", "Struct", "Interface", "Type", "TypeAlias"}
    }
    matched = ours.keys() & scip.keys()
    receivers = [
        (ours[k].symbol, scip[k])
        for k in sorted(matched)
        if ours[k].kind == "Method"
        and "." in ours[k].symbol
        and not re.search(
            r"[/`]" + re.escape(ours[k].symbol.split(".")[0]) + "#", scip[k]
        )
    ]
    interfaces = {
        (d.file, d.symbol) for d in result.definitions if d.kind == "Interface"
    }
    unmatched = Counter()
    examples = []
    for key in sorted(ours.keys() - scip.keys()):
        d = ours[key]
        owner = d.symbol.rsplit(".", 1)[0]
        if d.kind == "Method" and (d.file, owner) in interfaces:
            reason = "interface_method_element"
        elif d.kind != "Method" and "." in d.symbol:
            reason = "local_declaration"
        else:
            reason = "not_indexed"
        unmatched[reason] += 1
        examples.append([d.file, d.line, d.symbol, reason])
    print(
        json.dumps(
            {
                "declarations": len(ours),
                "scip_declarations": len(scip),
                "matched": len(matched),
                "receiver_mismatches": receivers[:20],
                "tree_sitter_only": dict(sorted(unmatched.items())),
                "tree_sitter_only_examples": examples[:20],
                "scip_only": sorted(map(list, scip.keys() - ours.keys()))[:20],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
