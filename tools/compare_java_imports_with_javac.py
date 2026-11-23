"""Compare Java import links with javac's own import resolution.

For every source root the analysis finds (the directory above a package's
path), tools/JavaImportOracle.java parses and enters that root's files with javac, the
root first on the source path and every other root after it, and reports the
source files javac bound each import to. Nothing is written or executed.

Each import is classified by its outcome here against javac's:

- ``confirmed``: linked to exactly javac's files;
- ``contradicted``: linked, javac bound other files;
- ``linked_javac_unbound``: linked, javac bound no source file;
- ``missed``: not linked, javac bound a source file;
- ``ambiguous_includes_javac`` / ``ambiguous_excludes_javac``;
- ``both_outside``: neither binds a repository file (JDK, libraries).

javac takes the importer's own root first, as the same-source-root rule does,
so it confirms that rule's choice only for this source path order.

Usage:
  python tools/compare_java_imports_with_javac.py ROOT [--java JAVA]
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess

import deepthought
from deepthought.imports import _java_index, resolve_imports

ORACLE = Path(__file__).with_name("JavaImportOracle.java")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--java", default="java")
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
            javac.setdefault((row["file"], row["line"]), []).append(row["targets"])
    imports = {i.id: i for i in result.imports}
    per_line = Counter((i.file, i.line) for i in result.imports)
    outcomes, examples = Counter(), {}
    for resolution in resolve_imports(result):
        item = imports[resolution.import_id]
        key = (item.file, item.line)
        found = javac.get(key)
        if found is None or per_line[key] > 1 or len(found) > 1:
            outcomes["not_compared"] += 1
            continue
        expected = set(found[0])
        ours = {link.target_file for link in resolution.links}
        if resolution.status == "linked":
            if ours == expected:
                outcome = "confirmed"
            else:
                outcome = "contradicted" if expected else "linked_javac_unbound"
        elif resolution.status == "ambiguous":
            outcome = (
                "ambiguous_includes_javac"
                if expected and expected <= set(resolution.candidates)
                else "ambiguous_excludes_javac"
            )
        else:
            outcome = "missed" if expected else "both_outside"
        outcomes[outcome] += 1
        if outcome in {"contradicted", "linked_javac_unbound", "missed"}:
            examples.setdefault(outcome, []).append(
                [item.file, item.line, item.specifier, sorted(ours), sorted(expected)]
            )
    rules = Counter(
        link.rule_id for r in resolve_imports(result) for link in r.links[:1]
    )
    print(
        json.dumps(
            {
                "imports": len(result.imports),
                "source_roots": len(source_roots),
                "linked_by_rule": dict(sorted(rules.items())),
                **dict(sorted(outcomes.items())),
                "examples": {k: v[:10] for k, v in sorted(examples.items())},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
