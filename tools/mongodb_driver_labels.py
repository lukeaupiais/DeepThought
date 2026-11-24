"""Label MongoDB driver collection accesses, `<handle>.collection('name')`.

Every `.collection('<literal>')` line in the JavaScript and TypeScript files
of WORKSPACE (tests, node_modules and comments left out) is a label: the collection is
the literal, exactly as the driver uses it, and the access comes from what
the code does with it, by the driver's semantics:

- READS: find, findOne, countDocuments, estimatedDocumentCount, distinct,
  aggregate, watch, listIndexes, indexes, and the legacy count and stats.
- WRITES: insertOne/Many, updateOne/Many, replaceOne, deleteOne/Many,
  bulkWrite, initialize(Un)OrderedBulkOp, createIndex(es), dropIndex(es),
  drop, rename, and the legacy insert, update, remove, save, ensureIndex.
- both: findOneAndUpdate, findOneAndReplace, findOneAndDelete, findAndModify.

The method is the one chained on the call, on the same line or a following
line that starts with `.`. A collection held in a variable takes the union
of the methods called on that variable in the rest of the file, which is
then checked by hand (``--show`` prints every label with its source). A line
whose method is none of these, or whose receiver is not a database handle by
reading the code, is given by hand with ``--override FILE:LINE=ACCESS`` or
left out with ``--skip FILE:LINE``. The script refuses to write labels while
any line is unresolved.

Usage:
  python tools/mongodb_driver_labels.py WORKSPACE OUT.json --repo URL \\
      [--override FILE:LINE=READS|WRITES|READS,WRITES ...] [--skip FILE:LINE ...]
"""

import argparse
import json
from pathlib import Path
import re
import subprocess

READS = {
    "find",
    "findOne",
    "countDocuments",
    "estimatedDocumentCount",
    "distinct",
    "aggregate",
    "watch",
    "listIndexes",
    "indexes",
    "count",
    "stats",
}
WRITES = {
    "insertOne",
    "insertMany",
    "updateOne",
    "updateMany",
    "replaceOne",
    "deleteOne",
    "deleteMany",
    "bulkWrite",
    "initializeUnorderedBulkOp",
    "initializeOrderedBulkOp",
    "createIndex",
    "createIndexes",
    "dropIndex",
    "dropIndexes",
    "drop",
    "rename",
    "insert",
    "update",
    "remove",
    "save",
    "ensureIndex",
}
BOTH = {"findOneAndUpdate", "findOneAndReplace", "findOneAndDelete", "findAndModify"}
SITE = re.compile(r"""\.collection\(\s*(['"])([^'"]+)\1\s*\)""")
HELD = re.compile(
    r"""(?:const|let|var)\s+(\w+)\s*=\s*(?:await\s+)?[\w.]+\.collection\("""
)
SKIPPED_DIRS = {"node_modules", "test", "tests", "__tests__", ".git"}


def access(method):
    if method in BOTH:
        return ["READS", "WRITES"]
    if method in READS:
        return ["READS"]
    if method in WRITES:
        return ["WRITES"]
    return None


def methods_on(name, lines, start):
    found = set()
    pattern = re.compile(rf"\b{re.escape(name)}\s*\.\s*(\w+)\s*\(")
    for text in lines[start:]:
        found.update(pattern.findall(text))
    return found


def label(workspace, overrides, skips):
    labels, unresolved = [], []
    for path in sorted(Path(workspace).rglob("*")):
        rel = path.relative_to(workspace)
        if (
            path.suffix not in {".js", ".mjs", ".cjs", ".ts"}
            or path.name.endswith(".d.ts")
            or SKIPPED_DIRS & set(rel.parts)
            or not path.is_file()
        ):
            continue
        lines = path.read_text("utf-8", "replace").split("\n")
        in_block = False
        for index, text in enumerate(lines):
            # Commented-out code is not an access.
            stripped = text.strip()
            if in_block:
                in_block = "*/" not in text
                continue
            if stripped.startswith("/*"):
                in_block = "*/" not in text
                continue
            if stripped.startswith("*"):
                continue
            for match in SITE.finditer(text):
                if "//" in text[: match.start()]:
                    continue
                key = f"{rel.as_posix()}:{index + 1}"
                if key in skips:
                    continue
                rest = text[match.end() :]
                if not rest.strip() and index + 1 < len(lines):
                    rest = lines[index + 1].strip()
                chained = re.match(r"\s*\.\s*(\w+)\s*\(", rest)
                held = HELD.search(text)
                if key in overrides:
                    kinds, how = overrides[key], "override"
                elif chained and access(chained.group(1)):
                    kinds, how = access(chained.group(1)), chained.group(1)
                elif held:
                    used = methods_on(held.group(1), lines, index + 1)
                    known = [access(m) for m in sorted(used) if access(m)]
                    kinds = sorted({k for kind in known for k in kind}) or None
                    how = f"{held.group(1)}: {', '.join(sorted(used))}"
                else:
                    kinds, how = None, rest.strip()[:60]
                item = {
                    "file": rel.as_posix(),
                    "line": index + 1,
                    "collection": match.group(2),
                    "access": kinds,
                    "how": how,
                }
                (labels if kinds else unresolved).append(item)
    return labels, unresolved


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workspace", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--override", action="append", default=[])
    parser.add_argument("--skip", action="append", default=[])
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args(argv)
    overrides = {}
    for item in args.override:
        key, _, kinds = item.rpartition("=")
        overrides[key] = sorted(kinds.split(","))
    labels, unresolved = label(args.workspace, overrides, set(args.skip))
    if args.show:
        for item in labels:
            print(
                item["file"],
                item["line"],
                item["collection"],
                item["access"],
                item["how"],
            )
    if unresolved:
        for item in unresolved:
            print("unresolved:", item["file"], item["line"], item["how"])
        return 1
    commit = subprocess.check_output(
        ["git", "-C", str(args.workspace), "rev-parse", "HEAD"], text=True
    ).strip()
    args.out.write_text(
        json.dumps(
            {
                "repo": args.repo,
                "commit": commit,
                "labelled_by": "tools/mongodb_driver_labels.py, every label read by hand",
                "accesses": [
                    {k: v for k, v in item.items() if k != "how"} for item in labels
                ],
            },
            indent=2,
        )
        + "\n",
        "utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
