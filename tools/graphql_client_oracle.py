"""Label GraphQL client operations with the server fields that declare them.

    python tools/graphql_client_oracle.py SPEC CORPUS CHECKOUT > LABELS.json

The labels come from graphql-core's parser, not from the engine. SPEC names
the corpus, its server scopes (each with its schema files) and its client
roots. Every ``gql`` / ``graphql`` template literal and every ``.graphql``
or ``.gql`` file under a client root is parsed. Each root field of an
operation is a call site, except ``__typename``, introspection fields and
fields marked ``@client`` (Apollo local state). The site is linked to a
server when exactly one scope the client can reach (its ``servers`` in the
spec, from the deployment config; every scope when absent) declares the
field on its Query, Mutation or Subscription type; with more than one it is listed as ambiguous, and with
none as unmatched. Sites under a ``dont_care`` root are listed with the
spec's reason instead of being labelled.

Template interpolations (``${FRAGMENT}``) are blanked to spaces so the
line numbers stay those of the file. The output follows the format of the
other files in evals/fixtures/service_contracts.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from graphql import GraphQLSyntaxError, parse
from graphql.language import (
    ObjectTypeDefinitionNode,
    ObjectTypeExtensionNode,
    OperationDefinitionNode,
    SchemaDefinitionNode,
    SchemaExtensionNode,
)

TEMPLATE = re.compile(r"\b(?:gql|graphql)\s*(?:\(\s*)?`")
SOURCES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".graphql", ".gql"}
SKIPPED_DIRS = {"node_modules", "__generated__", "dist", "build", ".git"}


def templates(text):
    """(line offset, body) of each tagged template literal in ``text``."""
    for match in TEMPLATE.finditer(text):
        i, body = match.end(), []
        while i < len(text) and text[i] != "`":
            if text.startswith("${", i):
                depth = 0
                while i < len(text):
                    c = text[i]
                    body.append("\n" if c == "\n" else " ")
                    i += 1
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            break
                continue
            if text[i] == "\\" and i + 1 < len(text):
                body.append(text[i + 1])
                i += 2
                continue
            body.append(text[i])
            i += 1
        yield text.count("\n", 0, match.end()), "".join(body)


def documents(path):
    """Parsed GraphQL documents in ``path`` with their line offsets."""
    text = path.read_text("utf-8", errors="replace")
    pieces = [(0, text)] if path.suffix in (".graphql", ".gql") else templates(text)
    for offset, body in pieces:
        try:
            yield offset, parse(body)
        except GraphQLSyntaxError as error:
            print(f"not GraphQL: {path}:{offset + 1}: {error.message}", file=sys.stderr)


def _line(offset, node):
    return offset + node.loc.source.get_location(node.loc.start).line


def root_fields(checkout, schema_files):
    """``{(operation, field): (file, line)}`` for one server scope."""
    roots = {"query": "Query", "mutation": "Mutation", "subscription": "Subscription"}
    parsed = [
        (f, offset, doc)
        for f in schema_files
        for offset, doc in documents(checkout / f)
    ]
    for _, _, doc in parsed:
        for d in doc.definitions:
            if isinstance(d, (SchemaDefinitionNode, SchemaExtensionNode)):
                for op in d.operation_types or ():
                    roots[op.operation.value] = op.type.name.value
    fields = {}
    for f, offset, doc in parsed:
        for d in doc.definitions:
            if not isinstance(d, (ObjectTypeDefinitionNode, ObjectTypeExtensionNode)):
                continue
            for operation, type_name in roots.items():
                if d.name.value == type_name:
                    for field in d.fields or ():
                        fields.setdefault(
                            (operation, field.name.value),
                            (f, _line(offset, field.name)),
                        )
    return fields


def client_sites(checkout, root, excluded):
    """Root-field call sites of the operations under ``root``."""
    for path in sorted((checkout / root).rglob("*")):
        rel = path.relative_to(checkout).as_posix()
        if (
            not path.is_file()
            or path.suffix not in SOURCES
            or rel in excluded
            or SKIPPED_DIRS & set(Path(rel).parts)
        ):
            continue
        for offset, doc in documents(path):
            for d in doc.definitions:
                if not isinstance(d, OperationDefinitionNode):
                    continue
                for selection in d.selection_set.selections:
                    name = getattr(getattr(selection, "name", None), "value", None)
                    if name is None or name.startswith("__"):
                        continue
                    if any(
                        x.name.value == "client" for x in selection.directives or ()
                    ):
                        continue
                    yield {
                        "file": rel,
                        "line": _line(offset, selection.name),
                        "operation_type": d.operation.value,
                        "operation": d.name.value if d.name else None,
                        "field": name,
                    }


def label(spec, checkout):
    servers = {s["name"]: root_fields(checkout, s["schema"]) for s in spec["servers"]}
    schemas = {f for s in spec["servers"] for f in s["schema"]}
    excluded = schemas | set(spec.get("not_clients", ()))
    links, ambiguous, unmatched, dont_care = [], [], [], []
    for client in spec["clients"]:
        for site in client_sites(checkout, client["root"], excluded):
            key = (site["operation_type"], site["field"])
            contract = f"{site['operation_type'].capitalize()}.{site['field']}"
            where = {"file": site["file"], "line": site["line"]}
            if "dont_care" in client:
                dont_care.append({**where, "reason": client["dont_care"]})
                continue
            reachable = client.get("servers", servers)
            hits = sorted(n for n in reachable if key in servers[n])
            if len(hits) == 1:
                server_file, server_line = servers[hits[0]][key]
                links.append(
                    {
                        "kind": "graphql",
                        "from_service": client["service"],
                        "to_service": hits[0],
                        "contract": contract,
                        "client": where,
                        "server": {"file": server_file, "line": server_line},
                        "evidence": f"operation {site['operation']}",
                        "in_scope_static": True,
                        "hop": client.get("hop", "direct"),
                        "label_confidence": "high",
                    }
                )
            elif hits:
                ambiguous.append({**where, "contract": contract, "servers": hits})
            else:
                unmatched.append({**where, "contract": contract})
    for i, link in enumerate(links, 1):
        link["id"] = f"{spec['corpus']}#{i}"
    pairs = sorted({(x["from_service"], x["to_service"], "graphql") for x in links})
    return {
        **{k: spec[k] for k in ("corpus", "repo", "commit", "license", "languages")},
        "sources": [
            "graphql-core parse of every gql/graphql template literal and "
            ".graphql/.gql file under the client roots, and of the server "
            "schemas; tools/graphql_client_oracle.py with the spec in "
            "evals/fixtures/service_contracts/graphql-specs.json",
            *spec.get("sources", ()),
        ],
        "services": list(
            {
                s["name"]: {"name": s["name"], "root": s["root"]}
                for s in [
                    *spec["servers"],
                    *(
                        {"name": c["service"], "root": c["root"]}
                        for c in spec["clients"]
                    ),
                ]
            }.values()
        ),
        "summary": {
            "graphql": {"in_scope": len(links), "out_of_scope": 0},
            "service_pairs_in_scope": [list(p) for p in pairs],
        },
        "links": links,
        "dont_care_call_sites": dont_care,
        "notes": spec.get("notes", []),
        "unmatched_clients": unmatched,
        "ambiguous_clients": ambiguous,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("spec", type=Path, help="graphql-specs.json")
    parser.add_argument("corpus", help="corpus name in the spec file")
    parser.add_argument("checkout", type=Path, help="clone at the pinned commit")
    args = parser.parse_args(argv)
    specs = {s["corpus"]: s for s in json.loads(args.spec.read_text("utf-8"))}
    json.dump(label(specs[args.corpus], args.checkout), sys.stdout, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
