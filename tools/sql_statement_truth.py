"""Table truth for SQL statements from the database engines' own parsers.

Reads sqlc's end-to-end test cases (internal/endtoend/testdata), which pair
a schema with named queries for one engine, and labels each query with the
tables it reads and writes:

- PostgreSQL: libpg_query, PostgreSQL's own parser (through pglast). Every
  RangeVar is a table; the relation of INSERT, UPDATE, DELETE and MERGE is
  written. A name that a WITH clause in the statement defines is not a
  table.
- SQLite: SQLite itself. The schema is created in memory and each query is
  prepared under an authorizer, which SQLite calls for every table it reads
  (SQLITE_READ) or writes (SQLITE_INSERT, SQLITE_UPDATE, SQLITE_DELETE).
  A query that names a view is skipped, since SQLite reports the view's base
  tables.

Neither is the reader the engine uses, so the labels check it. Names are
compared unquoted and in lower case without their schema. System catalogs
(pg_*, information_schema, sqlite_*) are left out, and a table that is
written is not also listed as read. Cases that sqlc expects to fail
(stderr.txt) are skipped, as are queries the engine's parser rejects.

Usage:
  python tools/sql_statement_truth.py SQLC_TESTDATA > sql-statements.json
"""

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

_NAME = re.compile(r"^--\s*name:\s*(\w+)\s*:\w+", re.M)
_MACRO = re.compile(r"sqlc\.(?:arg|narg|slice)\s*\(\s*['\"]?\w+['\"]?\s*\)", re.I)
_EMBED = re.compile(r"sqlc\.embed\s*\(\s*(\w+)\s*\)", re.I)
_AT = re.compile(r"(?<![\w@:])@(\w+)")


def configs(testdata):
    """(case dir, engine, schema paths, query paths) of each sqlc package."""
    import yaml

    for path in sorted(Path(testdata).rglob("sqlc.*")):
        if path.suffix not in (".json", ".yaml", ".yml"):
            continue
        case = path.parent
        if any(case.glob("stderr*")) or (case / "stderr").exists():
            continue
        try:
            data = yaml.safe_load(path.read_text("utf-8"))
        except Exception:
            continue
        blocks = data.get("packages") or data.get("sql") or []
        for block in blocks:
            engine = block.get("engine")
            if engine not in ("postgresql", "sqlite"):
                continue

            def paths(value):
                value = value if isinstance(value, list) else [value]
                out = []
                for v in value:
                    p = case / v
                    out += sorted(p.glob("*.sql")) if p.is_dir() else [p]
                return [p for p in out if p.is_file()]

            yield (
                case,
                engine,
                paths(block.get("schema", [])),
                paths(block.get("queries", [])),
            )


def named_queries(text):
    """[(name, sql)] split at sqlc's ``-- name:`` comments."""
    marks = list(_NAME.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end() : end].strip()
        if body:
            out.append((m.group(1), body))
    return out


def _skip(name):
    name = name.lower()
    return name.startswith(("pg_", "sqlite_")) or name == "information_schema"


def postgres_tables(sql):
    """(reads, writes) by libpg_query, or None when it rejects the text."""
    from pglast import ast, parse_sql
    from pglast.visitors import Visitor

    text = _EMBED.sub(r"\1.*", _AT.sub("$1", _MACRO.sub("$1", sql)))
    try:
        tree = parse_sql(text)
    except Exception:
        return None
    dml = (
        ast.SelectStmt,
        ast.InsertStmt,
        ast.UpdateStmt,
        ast.DeleteStmt,
        ast.MergeStmt,
        ast.TruncateStmt,
    )
    if not all(isinstance(s.stmt, dml) for s in tree):
        return None  # DDL and utility statements (CREATE, REFRESH) are not labelled.
    ctes, targets, names = set(), [], []

    class Walk(Visitor):
        def visit_CommonTableExpr(self, ancestors, node):
            ctes.add(node.ctename.lower())

        def visit_RangeVar(self, ancestors, node):
            # FOR UPDATE OF names a FROM item (often an alias), not a table.
            if any(isinstance(a, ast.LockingClause) for a in _nodes(ancestors)):
                return
            if node.schemaname and _skip(node.schemaname):
                return
            names.append(node)

    Walk()(tree)
    for stmt in tree:
        for node in _statements(stmt.stmt, ast):
            if isinstance(node, ast.TruncateStmt):
                targets.extend(node.relations)
            elif getattr(node, "relation", None) is not None:
                targets.append(node.relation)
    writes = {
        t.relname.lower()
        for t in targets
        if not t.schemaname or not _skip(t.schemaname)
    }
    reads = set()
    for n in names:
        name = n.relname.lower()
        if any(n is t for t in targets) or _skip(name):
            continue
        if not n.schemaname and name in ctes:
            continue
        reads.add(name)
    writes = {w for w in writes if w not in ctes and not _skip(w)}
    return sorted(reads - writes), sorted(writes)


def _nodes(ancestors):
    """The nodes above a visited node."""
    out, a = [], ancestors
    while a is not None and getattr(a, "node", None) is not None:
        out.append(a.node)
        a = a.parent
    return out


def _statements(node, ast):
    """The INSERT/UPDATE/DELETE/MERGE statements in node, including those
    a WITH clause runs."""
    from pglast.visitors import Visitor

    found = []

    class Walk(Visitor):
        def visit(self, ancestors, n):
            if isinstance(
                n,
                (
                    ast.InsertStmt,
                    ast.UpdateStmt,
                    ast.DeleteStmt,
                    ast.MergeStmt,
                    ast.TruncateStmt,
                ),
            ):
                found.append(n)

    if isinstance(
        node,
        (
            ast.InsertStmt,
            ast.UpdateStmt,
            ast.DeleteStmt,
            ast.MergeStmt,
            ast.TruncateStmt,
        ),
    ):
        found.append(node)
    Walk()(node)
    return found


_READ, _INSERT, _UPDATE, _DELETE = (
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
    sqlite3.SQLITE_DELETE,
)


def sqlite_schema(paths):
    """An in-memory database with the schema, and the views it defines;
    None when SQLite rejects the schema."""
    db = sqlite3.connect(":memory:")
    try:
        for p in paths:
            db.executescript(p.read_text("utf-8"))
    except sqlite3.Error:
        return None, set()
    views = {
        r[0].lower()
        for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'view'")
    }
    return db, views


def sqlite_tables(db, views, sql):
    """(reads, writes) that SQLite reports while preparing sql, or None."""
    text = _EMBED.sub(r"\1.*", _MACRO.sub("?", sql))
    words = {w.lower() for w in re.findall(r"[A-Za-z_]\w*", text)}
    if words & views:
        return None
    reads, writes = set(), set()

    def authorize(action, arg1, arg2, dbname, source):
        if arg1 and dbname in (None, "main", "temp") and not _skip(arg1):
            if action == _READ:
                reads.add(arg1.lower())
            elif action in (_INSERT, _UPDATE, _DELETE):
                writes.add(arg1.lower())
        return sqlite3.SQLITE_OK

    db.set_authorizer(authorize)
    try:
        # EXPLAIN prepares the statement (so the authorizer runs) without
        # running it. No parameters are bound: SQLite has prepared the
        # statement by the time it rejects the missing bindings.
        db.execute("EXPLAIN " + text.rstrip().rstrip(";"))
    except sqlite3.ProgrammingError:
        pass
    except sqlite3.Error:
        return None
    finally:
        db.set_authorizer(None)
    return sorted(reads - writes), sorted(writes)


def derive(testdata):
    testdata = Path(testdata).resolve()
    seen, rows, skipped = set(), [], 0
    for case, engine, schema, queries in configs(testdata):
        db = views = None
        if engine == "sqlite":
            db, views = sqlite_schema(schema)
            if db is None:
                continue
        for qpath in queries:
            for name, sql in named_queries(qpath.read_text("utf-8")):
                key = (engine, sql)
                if key in seen:
                    continue
                seen.add(key)
                found = (
                    postgres_tables(sql)
                    if engine == "postgresql"
                    else sqlite_tables(db, views, sql)
                )
                if found is None:
                    skipped += 1
                    continue
                reads, writes = found
                if not reads and not writes:
                    continue
                rows.append(
                    {
                        "case": qpath.relative_to(testdata).as_posix(),
                        "engine": engine,
                        "name": name,
                        "sql": sql,
                        "reads": reads,
                        "writes": writes,
                    }
                )
    print(f"{len(rows)} statements, {skipped} skipped", file=sys.stderr)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("testdata")
    args = parser.parse_args(argv)
    json.dump({"statements": derive(args.testdata)}, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()
