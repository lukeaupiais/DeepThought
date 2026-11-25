"""Label GraphQL client operations with the server fields that declare them.

    NODE_PATH=DIR python tools/graphql_client_oracle.py SPEC CORPUS CHECKOUT > LABELS.json

The labels come from graphql-js, the reference GraphQL implementation, not
from the engine (which reads GraphQL with graphql-core). DIR holds graphql
and typescript (``npm install graphql@16.9.0 typescript@5.6.3``);
``tools/graphql_documents.cjs`` reads the files with them. SPEC names the
corpus, its server scopes (each with its schema files) and its client roots.
Every template literal tagged ``gql`` or ``graphql`` (found by the
TypeScript compiler) and every ``.graphql`` or ``.gql`` file under a client
root is parsed. Each root field of an operation is a call site, except
``__typename``, introspection fields and fields marked ``@client`` (Apollo
local state). The site is linked to a server when exactly one scope the
client can reach (its ``servers`` in the spec, from the deployment config;
every scope when absent) declares the field on its Query, Mutation or
Subscription type; with more than one it is listed as ambiguous, and with
none as unmatched. Sites under a ``dont_care`` root are listed with the
spec's reason instead of being labelled.

Template interpolations (``${FRAGMENT}``) are blanked to spaces so the
line numbers stay those of the file. The output follows the format of the
other files in evals/fixtures/service_contracts.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SOURCES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".graphql", ".gql"}
SKIPPED_DIRS = {"node_modules", "__generated__", "dist", "build", ".git"}
HELPER = Path(__file__).with_name("graphql_documents.cjs")


def read_documents(paths, node=None):
    """``{path: [(line offset, document summary)]}`` from graphql-js."""
    node = node or os.environ.get("NODE") or shutil.which("node")
    if not node:
        raise RuntimeError("node is required to run graphql-js")
    paths = [str(p) for p in paths]
    out = subprocess.run(
        [node, str(HELPER)],
        input=json.dumps(paths),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found = {}
    for entry in json.loads(out):
        docs = []
        for doc in entry["documents"]:
            if "error" in doc:
                print(
                    f"not GraphQL: {entry['file']}:{doc['line'] + 1}: {doc['error']}",
                    file=sys.stderr,
                )
            else:
                docs.append((doc["line"], doc))
        found[entry["file"]] = docs
    return found


def root_fields(checkout, schema_files, docs):
    """``{(operation, field): (file, line)}`` for one server scope."""
    roots = {"query": "Query", "mutation": "Mutation", "subscription": "Subscription"}
    parsed = [
        (f, offset, doc)
        for f in schema_files
        for offset, doc in docs[str(checkout / f)]
    ]
    for _, _, doc in parsed:
        roots.update(doc["schema_roots"])
    fields = {}
    for f, offset, doc in parsed:
        for d in doc["object_types"]:
            for operation, type_name in roots.items():
                if d["name"] == type_name:
                    for field in d["fields"]:
                        fields.setdefault(
                            (operation, field["name"]), (f, offset + field["line"])
                        )
    return fields


def client_files(checkout, root, excluded):
    """Source files under ``root`` that may hold client operations."""
    for path in sorted((checkout / root).rglob("*")):
        rel = path.relative_to(checkout).as_posix()
        if (
            path.is_file()
            and path.suffix in SOURCES
            and rel not in excluded
            and not SKIPPED_DIRS & set(Path(rel).parts)
        ):
            yield path


def client_sites(checkout, root, excluded, docs):
    """Root-field call sites of the operations under ``root``."""
    for path in client_files(checkout, root, excluded):
        rel = path.relative_to(checkout).as_posix()
        for offset, doc in docs[str(path)]:
            for operation in doc["operations"]:
                for field in operation["fields"]:
                    if field["name"].startswith("__") or field["client"]:
                        continue
                    yield {
                        "file": rel,
                        "line": offset + field["line"],
                        "operation_type": operation["type"],
                        "operation": operation["name"],
                        "field": field["name"],
                    }


def label(spec, checkout):
    schemas = {f for s in spec["servers"] for f in s["schema"]}
    excluded = schemas | set(spec.get("not_clients", ()))
    files = {checkout / f for f in schemas}
    for client in spec["clients"]:
        files.update(client_files(checkout, client["root"], excluded))
    docs = read_documents(sorted(files))
    servers = {
        s["name"]: root_fields(checkout, s["schema"], docs) for s in spec["servers"]
    }
    links, ambiguous, unmatched, dont_care = [], [], [], []
    for client in spec["clients"]:
        for site in client_sites(checkout, client["root"], excluded, docs):
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
            "graphql-js parse of every gql/graphql template literal (found "
            "by the TypeScript compiler) and .graphql/.gql file under the "
            "client roots, and of the server schemas; "
            "tools/graphql_client_oracle.py with the spec in "
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
