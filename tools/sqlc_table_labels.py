"""Table labels for the Go code sqlc generates, from sqlc's own analysis.

sqlc parses each query with the database engine's parser (PostgreSQL's
libpg_query for ``engine: postgresql``) and resolves every column and
parameter against the schema. Its ``json`` generator writes that analysis
out. This tool runs it on a scratch copy and maps each query to the line of
the generated Go method that runs it, so the labels name what sqlc resolved,
not what a pattern over the SQL text finds.

A statement whose first keyword is INSERT, UPDATE or DELETE WRITES its
target table: the ``insert_into_table`` sqlc reports, or the table its
parameters and returned columns resolve to. Any other statement READS the
tables its columns and parameters resolve to. A query sqlc resolves to no
table, or to more than one target for a write, is listed under ``unlabelled``.

Usage:
  python tools/sqlc_table_labels.py WORKSPACE --sqlc PATH [--config sqlc.yaml]
      > labels.json
"""

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_CONST = re.compile(r"^const (\w+) = `-- name: (\w+) :\w+")
_WRITE = re.compile(r"^\s*(?:WITH\b.*?\)\s*)?(INSERT|UPDATE|DELETE)\b", re.I | re.S)


def _tables(query):
    names = set()
    for c in query.get("columns", ()):
        if c.get("table") and c["table"].get("name"):
            names.add(c["table"]["name"])
    for p in query.get("params", ()):
        t = (p.get("column") or {}).get("table")
        if t and t.get("name"):
            names.add(t["name"])
    return names


def _strip_comments(text):
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("--")
    )


def analyze(workspace, sqlc, config):
    """[(gen dir, query json)] for each sql block of the sqlc config."""
    import yaml

    workspace = Path(workspace).resolve()
    scratch = Path(tempfile.mkdtemp())
    try:
        root = scratch / "src"
        shutil.copytree(workspace, root, ignore=shutil.ignore_patterns(".git"))
        settings = yaml.safe_load((root / config).read_text("utf-8"))
        out = []
        for i, block in enumerate(settings["sql"]):
            gen = block.get("gen", {}).get("go", {}).get("out")
            if gen is None:
                continue
            oracle = {
                "version": "2",
                "sql": [
                    {
                        "schema": block["schema"],
                        "queries": block["queries"],
                        "engine": block["engine"],
                        "gen": {
                            "json": {"out": f"oracle-{i}", "filename": "codegen.json"}
                        },
                    }
                ],
            }
            (root / "sqlc.oracle.yaml").write_text(json.dumps(oracle), "utf-8")
            subprocess.run(
                [sqlc, "generate", "-f", "sqlc.oracle.yaml"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            data = json.loads(
                (root / f"oracle-{i}" / "codegen.json").read_text("utf-8")
            )
            out += [(gen, q) for q in data["queries"]]
        return out
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def call_sites(workspace, gen):
    """{query name: (file, line)} of the call that runs each generated query."""
    sites = {}
    for path in sorted((Path(workspace) / gen).glob("*.go")):
        lines = path.read_text("utf-8").split("\n")
        consts = {}
        for line in lines:
            m = _CONST.match(line)
            if m:
                consts[m.group(1)] = m.group(2)
        for n, line in enumerate(lines, 1):
            m = re.search(
                r"\.(?:Query|QueryRow|Exec|QueryContext|QueryRowContext|"
                r"ExecContext)\(ctx, (\w+)\b",
                line,
            )
            if m and m.group(1) in consts:
                rel = path.relative_to(workspace).as_posix()
                sites[consts[m.group(1)]] = (rel, n)
    return sites


def labels(workspace, sqlc, config):
    accesses, unlabelled = [], []
    for gen, query in analyze(workspace, sqlc, config):
        site = call_sites(workspace, gen).get(query["name"])
        if site is None:
            unlabelled.append({"query": query["name"], "reason": "no call site"})
            continue
        text = _strip_comments(query["text"])
        verb = _WRITE.match(text)
        if verb:
            target = query.get("insert_into_table") or {}
            names = {target["name"]} if target.get("name") else _tables(query)
            mode = "WRITES"
        else:
            names, mode = _tables(query), "READS"
        if not names or (mode == "WRITES" and len(names) != 1):
            unlabelled.append({"query": query["name"], "reason": "tables not resolved"})
            continue
        accesses.append(
            {
                "file": site[0],
                "line": site[1],
                "query": query["name"],
                "tables": sorted([t, mode] for t in names),
            }
        )
    accesses.sort(key=lambda a: (a["file"], a["line"]))
    return {"accesses": accesses, "unlabelled": unlabelled}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workspace")
    parser.add_argument("--sqlc", required=True)
    parser.add_argument("--config", default="sqlc.yaml")
    args = parser.parse_args(argv)
    print(json.dumps(labels(args.workspace, args.sqlc, args.config), indent=1))


if __name__ == "__main__":
    main()
