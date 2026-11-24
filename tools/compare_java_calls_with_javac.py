"""Compare Java call (or, with --bases, declared base) links with javac.

For every source root the analysis finds, tools/JavaCallOracle.java attributes
that root's files with javac (the root first on the source path, every other
root after it) and reports the declaration each method call and ``new``
expression binds to. A ``new`` expression binds the class it creates. With
``--bases``, each extends or implements entry is compared instead, by the
type javac binds it to.

Each call site is classified by its outcome here against javac's:

- ``confirmed``: linked to javac's declaration (same file, name and line);
- ``contradicted``: linked, javac bound another repository declaration;
- ``linked_javac_unbound``: linked, javac bound none in the repository;
- ``ambiguous_includes_javac`` / ``ambiguous_excludes_javac`` /
  ``ambiguous_javac_unbound``;
- ``missed``: unresolved, javac bound a repository declaration;
- ``both_unbound``: neither binds a repository declaration.

A method call is not compared (``not_compared``) when javac reports it
uncertain: the method is overloaded and an argument's type or a parameter
type of an overload is one javac could not resolve. The oracle is given no
class path, so such a choice is javac's error recovery.

Usage:
  python tools/compare_java_calls_with_javac.py ROOT [--bases] [--java JAVA]
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess

import deepthought
from deepthought.calls import resolve_calls
from deepthought.imports import _java_index
from deepthought.inheritance import resolve_supertypes
from compare_java_declarations_with_javac import _offsets

ORACLE = Path(__file__).with_name("JavaCallOracle.java")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--java", default="java")
    parser.add_argument("--bases", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    result = deepthought.analyze_repository(
        root,
        repository_id="comparison",
        languages=("java",),
        max_files=1_000_000,
        max_total_bytes=10**10,
    )
    source_roots = sorted(set(_java_index(result)[0].values()))
    javac = {}
    for source_root in source_roots:
        others = [r for r in source_roots if r != source_root]
        output = subprocess.run(
            [
                args.java,
                "--add-exports",
                "jdk.compiler/com.sun.tools.javac.code=ALL-UNNAMED",
                "--add-exports",
                "jdk.compiler/com.sun.tools.javac.api=ALL-UNNAMED",
                str(ORACLE),
                str(root),
                source_root or ".",
                *(r or "." for r in others),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        for row in map(json.loads, output.splitlines()):
            if row.get("base", False) != args.bases:
                continue
            target = (
                "uncertain"
                if row.get("uncertain")
                else (row["target_file"], row["target_symbol"], row["target_line"])
                if "target_file" in row
                else None
            )
            javac.setdefault((row["file"], row["line"], row["column"]), []).append(
                target
            )
    by_id = {d.id: d for d in result.definitions}
    facts = result.supertypes if args.bases else result.call_sites
    sites = {s.id: s for s in facts}
    resolve = resolve_supertypes if args.bases else resolve_calls
    offsets = {}
    outcomes, by_rule, examples = Counter(), Counter(), {}
    for resolution in resolve(result):
        site = sites[resolution.call_id]
        if site.file not in offsets:
            offsets[site.file] = _offsets((root / site.file).read_text())
        offset = offsets[site.file]
        column = offset(site.line, site.column) - offset(site.line, 0)
        found = javac.get((site.file, site.line, column))
        if found is None or len(set(found)) > 1 or "uncertain" in found:
            outcomes["not_compared"] += 1
            continue
        expected = found[0]

        def key(definition_id):
            d = by_id[definition_id]
            return d.file, d.symbol, d.line

        if resolution.status == "linked":
            ours = key(resolution.target_id)
            if ours == expected:
                outcome = "confirmed"
            else:
                outcome = "contradicted" if expected else "linked_javac_unbound"
            by_rule[(resolution.rule_id, outcome)] += 1
        elif resolution.status == "ambiguous":
            candidates = {key(i) for i in resolution.candidates}
            outcome = (
                "ambiguous_javac_unbound"
                if expected is None
                else "ambiguous_includes_javac"
                if expected in candidates
                else "ambiguous_excludes_javac"
            )
        else:
            outcome = "missed" if expected else "both_unbound"
            if expected:
                by_rule[(resolution.reason, outcome)] += 1
        outcomes[outcome] += 1
        if outcome in {
            "contradicted",
            "linked_javac_unbound",
            "ambiguous_excludes_javac",
        } or (args.bases and outcome == "missed"):
            examples.setdefault(outcome, []).append(
                [
                    site.file,
                    site.line,
                    site.name if args.bases else site.callee,
                    list(key(resolution.target_id)) if resolution.target_id else None,
                    list(expected) if expected else None,
                ]
            )
    print(
        json.dumps(
            {
                "facts": len(facts),
                "source_roots": len(source_roots),
                **dict(sorted(outcomes.items())),
                "by_rule": {
                    f"{rule} {outcome}": n
                    for (rule, outcome), n in sorted(by_rule.items())
                },
                "examples": {k: v[:12] for k, v in sorted(examples.items())},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
