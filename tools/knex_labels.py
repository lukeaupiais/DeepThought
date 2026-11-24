"""Label the tables Knex query-builder calls name, by Knex's own semantics.

Every JavaScript and TypeScript file of WORKSPACE (tests, node_modules and
type declarations left out) is parsed with tree-sitter, and every call that
names a table with a string literal through a Knex handle is a site:

- ``H('t')``: a Knex instance or transaction called with a table
  (``knex('posts')``, ``trx('posts')``, ``db.knex('posts')``);
- ``<builder>.from|into|table('t')``: a builder started from a handle
  (``knex.select('id').from('posts')``, ``trx.into('posts')``);
- ``<builder>.join|leftJoin|...('t', ...)``: a joined table, always READS;
- ``<handle>.batchInsert('t', rows)``: WRITES;
- ``<handle>.schema.createTable|createTableIfNotExists('t', ...)``: DECLARES.

A name the file gives a common table expression (``.with('t', ...)``) is
not a table, and its sites are not labelled.

``H`` is an identifier or property named in ``--handle`` (default knex,
trx, transaction, connection, db, transacting). Each handle name is checked
by hand per file: ``--not-handle FILE:NAME`` drops a name a file uses for
something else. Other schema calls on a literal table (alterTable, table,
dropTable, hasTable, renameTable, ...) are listed under ``not_counted``.

A table literal ``'posts as p'`` names ``posts``, and ``'app.posts'`` names
``posts``: tables join across readers by unqualified name. A database's own
catalog (schemas information_schema, pg_catalog, mysql, sys,
performance_schema; names starting pg_ or sqlite_) is no application table
and its sites are listed under ``not_counted``. The access of a builder
table follows Knex's execution model: each execution of a builder runs one
query, which writes the table when its chain calls insert, update, del,
delete, increment, decrement, truncate, upsert or merge, and reads it
otherwise. A builder stored in a variable (not awaited) runs once per later
chain on that variable in the same function, and the table takes the union
of their accesses; awaited, the variable holds rows. A chain
that only builds SQL text (toString, toSQL, toQuery) or whose access cannot
be read this way is unresolved and is given by hand with
``--override FILE:LINE:TABLE=ACCESS`` or left out with ``--skip FILE:LINE``.
The script refuses to write labels while any site is unresolved.

Labels are per line: the set of (table, access) the line's sites name.

Usage:
  python tools/knex_labels.py WORKSPACE OUT.json --repo URL [--show]
      [--handle NAME ...] [--not-handle FILE:NAME ...]
      [--override FILE:LINE:TABLE=READS|WRITES|READS,WRITES ...]
      [--skip FILE:LINE ...] [--skip-file PATH_PREFIX ...]
"""

import argparse
from collections import defaultdict
import json
from pathlib import Path
import subprocess

import tree_sitter
import tree_sitter_javascript
import tree_sitter_typescript

HANDLES = ("knex", "trx", "transaction", "connection", "db", "transacting")
WRITES = {
    "insert",
    "update",
    "del",
    "delete",
    "increment",
    "decrement",
    "truncate",
    "upsert",
    "merge",
}
TEXT_ONLY = {"toString", "toSQL", "toQuery"}
TABLE_METHODS = {"from", "into", "table"}
JOINS = {
    "join",
    "innerJoin",
    "leftJoin",
    "leftOuterJoin",
    "rightJoin",
    "rightOuterJoin",
    "fullOuterJoin",
    "crossJoin",
}
DECLARES = {"createTable", "createTableIfNotExists"}
SCHEMA_OTHER = {
    "alterTable",
    "table",
    "dropTable",
    "dropTableIfExists",
    "hasTable",
    "hasColumn",
    "renameTable",
    "dropColumn",
    "raw",
}
SKIPPED_DIRS = {"node_modules", "test", "tests", "__tests__", ".git", "e2e", ".yarn"}
LANGUAGES = {
    ".js": tree_sitter.Language(tree_sitter_javascript.language()),
    ".cjs": tree_sitter.Language(tree_sitter_javascript.language()),
    ".mjs": tree_sitter.Language(tree_sitter_javascript.language()),
    ".ts": tree_sitter.Language(tree_sitter_typescript.language_typescript()),
}


def text(node):
    return node.text.decode("utf-8", "replace")


def literal(node):
    """The value of a plain string or substitution-free template literal."""
    if node is None:
        return None
    if node.type == "string":
        return text(node)[1:-1]
    if node.type == "template_string" and not any(
        c.type == "template_substitution" for c in node.children
    ):
        return text(node)[1:-1]
    return None


def table_name(value):
    name = value.split(" as ")[0].split(" AS ")[0].strip()
    if not name or not all(c.isalnum() or c in "_." for c in name):
        return None
    return name


SYSTEM_SCHEMAS = {
    "information_schema",
    "pg_catalog",
    "mysql",
    "sys",
    "performance_schema",
}


def system_catalog(table):
    """A database's own catalog, not an application table:
    ``information_schema.tables``, ``pg_database``, ``sqlite_master``."""
    parts = table.lower().split(".")
    return (
        len(parts) > 1
        and parts[-2] in SYSTEM_SCHEMAS
        or parts[-1].startswith(("pg_", "sqlite_"))
    )


def file_constants(tree):
    """Top-level ``const NAME = 'text'`` of a file, exported or not."""
    found = {}
    for node in tree.root_node.named_children:
        if node.type == "export_statement":
            node = node.child_by_field_name("declaration")
        if node is None or node.type != "lexical_declaration":
            continue
        if not text(node).startswith("const"):
            continue
        for decl in node.named_children:
            if decl.type != "variable_declarator":
                continue
            name, value = (
                decl.child_by_field_name("name"),
                decl.child_by_field_name("value"),
            )
            if name is not None and name.type == "identifier":
                value = literal(unwrap(value))
                if value is not None:
                    found[text(name)] = value
    return found


def module_file(path, source):
    """The workspace file a relative import names, or None."""
    if not source.startswith("."):
        return None
    base = (path.parent / source).resolve()
    for stem in (base.with_suffix(""), base):
        for suffix in LANGUAGES:
            for candidate in (Path(f"{stem}{suffix}"), stem / f"index{suffix}"):
                if candidate.is_file():
                    return candidate
    return None


_CONSTANTS = {}


def constants_of(path):
    path = path.resolve()
    if path not in _CONSTANTS:
        tree = tree_sitter.Parser(LANGUAGES[path.suffix]).parse(path.read_bytes())
        _CONSTANTS[path] = file_constants(tree)
    return _CONSTANTS[path]


def table_constants(path, tree):
    """String constants a file can name a table by: its own top-level
    consts and the ones it imports by name from a relative module."""
    found = dict(constants_of(path))
    for node in tree.root_node.named_children:
        if node.type != "import_statement":
            continue
        source = literal(node.child_by_field_name("source"))
        target = module_file(path, source) if source else None
        if target is None:
            continue
        exported = constants_of(target)
        stack = [node]
        while stack:
            spec = stack.pop()
            stack.extend(spec.named_children)
            if spec.type != "import_specifier":
                continue
            name = spec.child_by_field_name("name")
            alias = spec.child_by_field_name("alias") or name
            if name is not None and text(name) in exported:
                found[text(alias)] = exported[text(name)]
    return found


def first_argument(call):
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    named = [c for c in args.named_children if c.type != "comment"]
    return named[0] if named else None


def callee_name(call):
    """The name a call's callee ends with: ``knex`` for ``this.knex(...)``."""
    # tree-sitter reads ``await trx<T>('t')`` as a call of ``await trx``.
    fn = unwrap(call.child_by_field_name("function"))
    if fn is None:
        return None, None
    if fn.type == "identifier":
        return plain(text(fn)), None
    if fn.type == "member_expression":
        prop = fn.child_by_field_name("property")
        return (
            plain(text(prop)) if prop is not None else None
        ), fn.child_by_field_name("object")
    return None, None


def plain(name):
    """``#knex`` and ``_knex`` name the same handle as ``knex``."""
    return name.lstrip("#").lstrip("_") if name else name


def is_handle(node, handles):
    """An expression that is a Knex handle: ``knex``, ``this.knex``."""
    if node is None:
        return False
    node = unwrap(node)
    if node.type == "identifier":
        return plain(text(node)) in handles
    if node.type == "member_expression":
        prop = node.child_by_field_name("property")
        return prop is not None and plain(text(prop)) in handles
    return False


def unwrap(node):
    while node is not None and node.type in {
        "await_expression",
        "parenthesized_expression",
        "non_null_expression",
        "as_expression",
    }:
        named = node.named_children
        node = named[0] if named else None
    return node


_CURRENT = set()


def handles_of(member):
    prop = member.child_by_field_name("property")
    return prop is not None and plain(text(prop)) in _CURRENT


def chain_inner(node):
    """Method names and root of the chain below ``node``: for
    ``knex.select('a').where(x)`` the names [select, where] and ``knex``."""
    names = []
    node = unwrap(node)
    while node is not None:
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "member_expression" and handles_of(fn):
                return names, fn, node
            name, obj = callee_name(node)
            if obj is None:
                return names, node.child_by_field_name("function"), node
            if plain(name) in _CURRENT:
                # ``await this.#knex<T>('t')`` parses as a call of
                # ``await this.#knex``.
                return names, unwrap(fn), node
            names.append(name)
            node = unwrap(obj)
            continue
        if node.type == "member_expression":
            prop = node.child_by_field_name("property")
            if prop is not None and text(prop) in {"schema", "queryBuilder"}:
                names.append(text(prop))
                node = unwrap(node.child_by_field_name("object"))
                continue
            return names, node, None
        return names, node, None
    return names, None, None


def chain_outer(call):
    """Method names called on ``call``'s result, and the node the whole
    chain ends at."""
    names = []
    node = call
    while True:
        parent = node.parent
        while parent is not None and parent.type in {
            "parenthesized_expression",
            "non_null_expression",
            "as_expression",
        }:
            node, parent = parent, parent.parent
        if (
            parent is not None
            and parent.type == "member_expression"
            and parent.child_by_field_name("object") == node
            and parent.parent is not None
            and parent.parent.type == "call_expression"
            and parent.parent.child_by_field_name("function") == parent
        ):
            prop = parent.child_by_field_name("property")
            names.append(text(prop))
            node = parent.parent
            continue
        return names, node


def held_name(end):
    """The variable a chain's value is stored in, when it is."""
    parent = end.parent
    while parent is not None and parent.type in {"await_expression"}:
        parent = parent.parent
    if parent is not None and parent.type == "variable_declarator":
        name = parent.child_by_field_name("name")
        if name is not None and name.type == "identifier":
            return text(name)
    if parent is not None and parent.type == "assignment_expression":
        left = parent.child_by_field_name("left")
        if left is not None and left.type == "identifier":
            return text(left)
    return None


FUNCTIONS = {
    "function_declaration",
    "function_expression",
    "function",
    "arrow_function",
    "method_definition",
    "generator_function_declaration",
}
READ_TERMINALS = {
    "select",
    "first",
    "pluck",
    "count",
    "countDistinct",
    "min",
    "max",
    "sum",
    "sumDistinct",
    "avg",
    "avgDistinct",
    "then",
}


def scope_of(node):
    """The function around ``node``, or the file."""
    while node.parent is not None and node.type not in FUNCTIONS:
        node = node.parent
    return node


def chains_on(scope, name, after):
    """The method chains later called on identifier ``name`` in ``scope``:
    ``q.where(x).update(y)`` is one chain, {where, update}."""
    found = []
    stack = [scope]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.start_byte < after or node.type != "member_expression":
            continue
        obj = node.child_by_field_name("object")
        if obj is None or obj.type != "identifier" or text(obj) != name:
            continue
        call = node.parent
        if call is None or call.type != "call_expression":
            continue
        prop = node.child_by_field_name("property")
        found.append({text(prop), *chain_outer(call)[0]})
    return found


def cte_names(tree):
    """Names a file gives common table expressions: ``.with('x', ...)``."""
    names = set()
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type == "call_expression" and callee_name(node)[0] in {
            "with",
            "withRecursive",
            "withMaterialized",
            "withNotMaterialized",
        }:
            value = literal(first_argument(node))
            if value is not None:
                names.add(value.strip())
    return names


KNEX_TYPES = {"Knex", "Knex.Transaction"}


def typed_handles(tree):
    """Names the file declares with the type Knex or Knex.Transaction (or a
    union of them, null and undefined aside): parameters, fields and
    variables, ``executor: Knex``, ``private readonly db: Knex``."""
    names = set()
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type != "type_annotation":
            continue
        parts = {
            p.strip()
            for p in text(node).lstrip(":").split("|")
            if p.strip() not in {"null", "undefined", ""}
        }
        if not parts or not parts <= KNEX_TYPES:
            continue
        owner = node.parent
        name = owner.child_by_field_name("name") or owner.child_by_field_name("pattern")
        if name is not None and name.type in {
            "identifier",
            "property_identifier",
            "private_property_identifier",
        }:
            names.add(plain(text(name)))
    return names


def transaction_parameters(tree, local):
    """Names a file gives the transaction Knex passes to a callback:
    ``knex.transaction(async (tx) => ...)`` makes ``tx`` a handle. Repeated
    until no name is added, for transactions nested in transactions."""
    names = set()
    while True:
        known = local | names
        found = set()
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            stack.extend(node.children)
            if node.type != "call_expression":
                continue
            callee = node.child_by_field_name("function")
            if (
                callee is None
                or callee.type != "member_expression"
                or text(callee.child_by_field_name("property")) != "transaction"
                or not is_handle(callee.child_by_field_name("object"), known)
            ):
                continue
            args = node.child_by_field_name("arguments")
            callback = args.named_children[0] if args and args.named_children else None
            if callback is None or callback.type not in {
                "arrow_function",
                "function_expression",
                "function",
            }:
                continue
            first = first_parameter(callback)
            if first is not None:
                found.add(plain(first))
        if found <= names:
            return names
        names |= found


NOT_SUBQUERY = {"transaction", "then", "catch", "finally", "asCallback", "map", "on"}
CALLBACKS = {"arrow_function", "function_expression", "function"}


def first_parameter(callback):
    single = callback.child_by_field_name("parameter")
    params = callback.child_by_field_name("parameters")
    first = single or (
        params.named_children[0] if params and params.named_children else None
    )
    if first is not None and first.type in {"required_parameter", "optional_parameter"}:
        first = first.child_by_field_name("pattern")
    if first is not None and first.type == "identifier":
        return text(first)
    return None


def typed_builders(tree):
    """Names the file declares with the type Knex.QueryBuilder: a builder
    handed in by the caller, ``(q: Knex.QueryBuilder) => q.from('t')``."""
    names = set()
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type != "type_annotation":
            continue
        kind = text(node).lstrip(":").strip().split("<")[0].strip()
        if kind != "Knex.QueryBuilder":
            continue
        name = node.parent.child_by_field_name(
            "pattern"
        ) or node.parent.child_by_field_name("name")
        if name is not None and name.type == "identifier":
            names.add(text(name))
    return names


def subquery_parameters(tree, local, builders):
    """Names a file gives the builder Knex passes to a callback argument of
    a builder method: ``.whereIn('id', (q) => q.from('t'))``,
    ``.union(function (q) { ... })``. Repeated until no name is added, for
    subqueries nested in subqueries."""
    names = set()
    while True:
        known = builders | names
        found = set()
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            stack.extend(node.children)
            if node.type != "call_expression":
                continue
            callee = node.child_by_field_name("function")
            if callee is None or callee.type != "member_expression":
                continue
            if text(callee.child_by_field_name("property")) in NOT_SUBQUERY:
                continue
            _, root, _ = chain_inner(callee.child_by_field_name("object"))
            if not (is_handle(root, local) or is_handle(root, known)):
                continue
            args = node.child_by_field_name("arguments")
            for arg in args.named_children if args else []:
                if arg.type in CALLBACKS:
                    name = first_parameter(arg)
                    if name is not None:
                        found.add(name)
        if found <= names:
            return names
        names |= found


def held_builders(tree, local):
    """Variables a file stores a handle's builder in: ``const q = knex('t')``."""
    names = set()
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type != "variable_declarator":
            continue
        name, value = (
            node.child_by_field_name("name"),
            node.child_by_field_name("value"),
        )
        if name is None or value is None or name.type != "identifier":
            continue
        _, root, _ = chain_inner(value)
        if is_handle(root, local) and unwrap(value).type == "call_expression":
            names.add(text(name))
    return names


def awaited(end):
    parent = end.parent
    return parent is not None and parent.type in {
        "await_expression",
        "return_statement",
        "arrow_function",
    }


def access_of(base, later, executed):
    """Knex runs one query per execution of a builder: an execution with a
    write method writes the table, any other reads it. A builder held in a
    variable runs each time a later chain on it executes."""
    if (base | set().union(*later)) & TEXT_ONLY:
        return None
    kinds = set()
    for chain in [base, *later]:
        if chain & WRITES:
            kinds.add("WRITES")
        elif chain & READ_TERMINALS or (chain is base and (executed or not later)):
            kinds.add("READS")
    return sorted(kinds) or ["READS"]


def sites(path, rel, handles, not_handles):
    language = LANGUAGES[path.suffix]
    tree = tree_sitter.Parser(language).parse(path.read_bytes())
    local = {
        h for h in handles | typed_handles(tree) if f"{rel}:{h}" not in not_handles
    }
    local |= {
        h
        for h in transaction_parameters(tree, local)
        if f"{rel}:{h}" not in not_handles
    }
    _CURRENT.clear()
    _CURRENT.update(local)
    builders = held_builders(tree, local) | typed_builders(tree)
    builders |= subquery_parameters(tree, local, builders)
    ctes = cte_names(tree)
    consts = table_constants(path, tree)
    found = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type != "call_expression":
            continue
        arg = first_argument(node)
        value = literal(arg)
        if value is None and arg is not None and arg.type == "identifier":
            value = consts.get(text(arg))
        if value is None:
            continue
        name, obj = callee_name(node)
        line = arg.start_point[0] + 1
        table = table_name(value)
        if table is None or table in ctes:
            # A common table expression the file names with .with('x', ...)
            # is no table.
            continue
        catalog = system_catalog(table)
        # Tables join across readers by name, so a schema qualifier is
        # dropped: ``app.users`` names ``users``.
        table = table.split(".")[-1]
        inner = []
        if name in local:
            # H('t') or x.H('t')
            kind = "entry"
        elif name == "batchInsert" and obj is not None and is_handle(obj, local):
            kind = "batch"
        elif (
            name in TABLE_METHODS | JOINS | DECLARES | SCHEMA_OTHER and obj is not None
        ):
            inner, root, _ = chain_inner(obj)
            if not (is_handle(root, local) or is_handle(root, builders)):
                continue
            if "schema" in inner:
                kind = "declare" if name in DECLARES else "schema"
            elif name in JOINS:
                kind = "join"
            elif name in TABLE_METHODS:
                kind = "entry"
            else:
                continue
        else:
            continue
        outer, end = chain_outer(node)
        held = held_name(end)
        base = set(inner) | set(outer)
        # Awaited, the variable holds rows, not a builder.
        later = (
            chains_on(scope_of(end), held, end.end_byte)
            if held and not awaited(end)
            else []
        )
        methods = base.union(*later)
        if catalog:
            access = "not_counted"
        elif kind == "batch":
            access = ["WRITES"]
        elif kind == "declare":
            access = ["DECLARES"]
        elif kind == "schema":
            access = "not_counted"
        elif "columnInfo" in methods:
            # Column metadata from the information schema, not the rows.
            access = "not_counted"
        elif kind == "join":
            access = ["READS"]
        else:
            access = access_of(base, later, awaited(end))
        how = (
            f"{kind} {name}: "
            + ", ".join(sorted(methods))
            + (f" (held {held})" if held else "")
        )
        found.append(
            {"file": rel, "line": line, "table": table, "access": access, "how": how}
        )
    return found


def label(workspace, handles, not_handles, overrides, skips, skip_files):
    labels, unresolved, ignored = [], [], []
    for path in sorted(Path(workspace).rglob("*")):
        rel = path.relative_to(workspace).as_posix()
        if (
            path.suffix not in LANGUAGES
            or path.name.endswith(".d.ts")
            or ".test." in path.name
            or SKIPPED_DIRS & set(Path(rel).parts)
            or any(rel.startswith(p) for p in skip_files)
            or not path.is_file()
        ):
            continue
        for item in sites(path, rel, handles, not_handles):
            key = f"{rel}:{item['line']}"
            if key in skips:
                continue
            override = overrides.get(f"{key}:{item['table']}")
            if override is not None:
                item["access"], item["how"] = override, "override"
            if item["access"] == "not_counted":
                ignored.append(item)
            elif item["access"]:
                labels.append(item)
            else:
                unresolved.append(item)
    return labels, unresolved, ignored


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workspace", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--handle", action="append")
    parser.add_argument("--not-handle", action="append", default=[])
    parser.add_argument("--override", action="append", default=[])
    parser.add_argument("--skip", action="append", default=[])
    parser.add_argument("--skip-file", action="append", default=[])
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args(argv)
    overrides = {}
    for item in args.override:
        key, _, kinds = item.rpartition("=")
        overrides[key] = sorted(kinds.split(","))
    labels, unresolved, ignored = label(
        args.workspace,
        set(args.handle or HANDLES),
        set(args.not_handle),
        overrides,
        set(args.skip),
        tuple(args.skip_file),
    )
    if args.show:
        for item in labels + ignored:
            print(
                item["file"], item["line"], item["table"], item["access"], item["how"]
            )
    if unresolved:
        for item in unresolved:
            print("unresolved:", item["file"], item["line"], item["table"], item["how"])
        return 1
    per_line = defaultdict(set)
    for item in labels:
        for kind in item["access"]:
            per_line[(item["file"], item["line"])].add((item["table"], kind))
    commit = subprocess.check_output(
        ["git", "-C", str(args.workspace), "rev-parse", "HEAD"], text=True
    ).strip()
    args.out.write_text(
        json.dumps(
            {
                "repo": args.repo,
                "commit": commit,
                "labelled_by": "tools/knex_labels.py, every label read by hand",
                "accesses": [
                    {"file": f, "line": ln, "tables": sorted(map(list, v))}
                    for (f, ln), v in sorted(per_line.items())
                ],
                "not_counted": sorted(
                    {(i["file"], i["line"]) for i in ignored} - set(per_line)
                ),
            },
            indent=2,
        )
        + "\n",
        "utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
