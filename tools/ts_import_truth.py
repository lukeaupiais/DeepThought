"""Import truth for JavaScript and TypeScript from webpack's resolver.

    NODE_PATH=DIR python tools/ts_import_truth.py WORKSPACE --language typescript > TRUTH.tsv

The targets come from enhanced-resolve, set up the way TypeScript resolves
(``tools/ts_import_oracle.cjs``), so the truth shares no code with the
TypeScript compiler or scip-typescript, which the engine uses. DIR holds
enhanced-resolve and @babel/parser (``npm install enhanced-resolve@5.18.1
@babel/parser@7.26.10``). Every file that git tracks with the language's
extensions (``.ts``/``.tsx``/``.mts``/``.cts`` for TypeScript,
``.js``/``.jsx``/``.mjs``/``.cjs`` for JavaScript) is read; each line that
names a module specifier lists the repository files it resolves to, or
``-`` (external) when it resolves to none in the repository. A ``.d.ts``
target also accepts the implementation file beside it, so an import of a
package whose ``types`` field names ``src/index.d.ts`` accepts
``src/index.js`` too. The output is the format ``derive-imports`` writes.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ground_truth import write_truth

HELPER = Path(__file__).with_name("ts_import_oracle.cjs")
EXTENSIONS = {
    "typescript": re.compile(r"\.(m|c)?tsx?$"),
    "javascript": re.compile(r"\.(m|c)?jsx?$"),
}


def sources(workspace, language):
    files = subprocess.run(
        ["git", "-C", str(workspace), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    pattern = EXTENSIONS[language]
    return sorted(f for f in files if pattern.search(f) and "node_modules/" not in f)


def resolve(workspace, files, node=None):
    """The oracle's rows: ``{file, line, spec, targets}`` or ``{file, error}``."""
    node = node or os.environ.get("NODE") or shutil.which("node")
    if node is None:
        raise RuntimeError("node is not installed")
    result = subprocess.run(
        [node, str(HELPER), str(workspace)],
        input=json.dumps(files),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def truth(rows):
    """``(expected, indexed)`` in ``read_truth``'s shape."""
    expected, errors = {}, []
    for row in rows:
        if "error" in row:
            errors.append(row["file"])
            continue
        key = (row["file"], row["line"], "import")
        targets = {(t, 0, "module") for t in row["targets"]}
        expected.setdefault(key, set()).update(targets or {("-", 0, "external")})
    for key, targets in expected.items():
        # A line naming a repository module and an external one is judged
        # by the repository target.
        if len(targets) > 1:
            targets.discard(("-", 0, "external"))
    return expected, errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--language", required=True, choices=sorted(EXTENSIONS))
    args = parser.parse_args(argv)
    files = sources(args.workspace, args.language)
    expected, errors = truth(resolve(args.workspace, files))
    if errors:
        print("not parsed: " + ", ".join(errors), file=sys.stderr)
    write_truth(expected, set(files) - set(errors), sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
