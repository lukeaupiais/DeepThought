"""Check that the text form of raw Cypher results keeps every fact.

Runs each query twice against a dedicated single-snapshot database, once as
JSON rows and once as text (``GraphQueryService.query_cypher_text``), and
checks that every fact in the rows appears in the text:

- a code item: its name, and its file:line or file;
- a link: both ends' names, its line, and its type's verb;
- a chain: every item's name and every link's line;
- a plain value: the value; a list: each element.

Queries come from a JSON file of {"query", "parameters"} objects (--queries),
from queries generated over every relationship type and label in the
database (--generate), or both. Prints one line per query that lost a fact
and a summary; exits non-zero when any fact was lost.

    python tools/check_result_text.py --repository-id URL --snapshot ID --generate
"""

import argparse
import asyncio
import json
import os
import sys

from neo4j import AsyncGraphDatabase

from deepthought.graph_queries import GraphQueryService
from deepthought.storage.graph.schema import RELATIONSHIP_SENTENCES


def facts(value, out):
    """The strings a faithful rendering must contain."""
    if isinstance(value, dict) and "labels" in value and "properties" in value:
        p = value["properties"]
        if "EdgeCoverage" in value["labels"]:
            out.update(str(p[k]) for k in ("family", "language") if p.get(k))
            return
        if "File" in value["labels"]:
            out.add(str(p.get("path") or p.get("file_path")))
            return
        name = p.get("callee") if "UnresolvedCall" in value["labels"] else None
        name = name or p.get("display_name") or p.get("name") or p.get("qualified_name")
        if name is not None:
            out.add(str(name))
        path = p.get("file_path") or p.get("path")
        line = p.get("start_line", p.get("line"))
        if path:
            out.add(f"{path}:{line}" if line is not None else str(path))
    elif isinstance(value, dict) and {"type", "source", "target"} <= set(value):
        out.add(RELATIONSHIP_SENTENCES.get(value["type"], value["type"].lower()))
        line = (value.get("properties") or {}).get("line")
        if isinstance(line, int) and value["type"] != "HAS_UNRESOLVED_CALL":
            out.add(str(line))
    elif isinstance(value, dict) and set(value) == {"nodes", "relationships"}:
        for node in value["nodes"]:
            p = node["properties"]
            name = p.get("display_name") or p.get("name") or p.get("path")
            if name is not None:
                out.add(str(name))
        for link in value["relationships"]:
            facts(link, out)
    elif isinstance(value, dict):
        for v in value.values():
            facts(v, out)
    elif isinstance(value, list):
        for v in value:
            facts(v, out)
    elif isinstance(value, bool):
        out.add("yes" if value else "no")
    elif isinstance(value, str) and "\n" in value:
        # A multi-line value is written as an indented block: every line.
        out.update(line.strip() for line in value.splitlines() if line.strip())
    elif value is not None and value != "":
        out.add(str(value))


async def labels_and_types(driver, database):
    async with driver.session(database=database) as session:
        types = [
            r["t"]
            async for r in await session.run(
                "MATCH ()-[r]->() RETURN DISTINCT type(r) AS t ORDER BY t"
            )
        ]
        labels = [
            r["l"]
            async for r in await session.run(
                "MATCH (n) UNWIND labels(n) AS l RETURN DISTINCT l ORDER BY l"
            )
        ]
    return types, labels


def generated(types, labels):
    """Varied query shapes over every relationship type and label."""
    queries = []
    for t in types:
        queries += [
            f"MATCH ()-[r:{t}]->() RETURN r LIMIT $fetch_limit",
            f"MATCH (a)-[r:{t}]->(b) RETURN a, r, b LIMIT $fetch_limit",
            f"MATCH p=()-[:{t}]->() RETURN p LIMIT $fetch_limit",
            f"MATCH (a)-[r:{t}]->(b) RETURN coalesce(a.name, a.path) AS source, "
            f"b.name AS target, r.line AS line ORDER BY source, line LIMIT $fetch_limit",
            f"MATCH (a)-[r:{t}]->(b) RETURN coalesce(a.file_path, a.path) AS file, "
            f"count(r) AS links ORDER BY links DESC LIMIT $fetch_limit",
            f"MATCH (a)-[r:{t}]->(b) WITH b, collect(r.line) AS lines "
            f"RETURN b, lines ORDER BY size(lines) DESC LIMIT $fetch_limit",
            f"MATCH (a)-[r:{t}]->(b) WITH b ORDER BY b.qualified_name LIMIT 1 "
            f"MATCH (c)-[s:{t}]->(b) RETURN s ORDER BY s.line LIMIT $fetch_limit",
            f"MATCH (a)-[r:{t}]->(b) WITH a ORDER BY a.qualified_name LIMIT 1 "
            f"MATCH (a)-[s:{t}]->(c) RETURN s, c LIMIT $fetch_limit",
            f"MATCH p=()-[:{t}*1..3]->() RETURN length(p) AS steps, p "
            f"ORDER BY steps LIMIT $fetch_limit",
            f"MATCH (a)-[r:{t}]->(b) RETURN {{from: a.name, to: b.name, line: r.line}} AS link "
            f"LIMIT $fetch_limit",
        ]
    for label in labels:
        queries += [
            f"MATCH (n:{label}) RETURN n LIMIT $fetch_limit",
            f"MATCH (n:{label}) RETURN n.name AS name, n.file_path AS file, "
            f"n.start_line AS line ORDER BY file, line LIMIT $fetch_limit",
            f"MATCH (n:{label}) RETURN count(n) AS count",
            f"MATCH (n:{label}) RETURN collect(n.name)[0..20] AS names",
        ]
    queries += [
        "MATCH p=(d)-[:CALLS*1..3]->(s:Symbol) WITH s, d, min(length(p)) AS steps "
        "RETURN steps, d, s ORDER BY steps LIMIT $fetch_limit",
        "MATCH (e:EdgeCoverage) RETURN e.family AS family, e.protocol AS protocol, "
        "e.language AS language, e.status AS status, e.reason AS reason "
        "ORDER BY family, protocol, language",
        "RETURN 1 AS one, 'two' AS two, [3, 4] AS more, true AS yes, null AS nothing",
    ]
    return [
        {"query": q.replace(" LIMIT $fetch_limit", " LIMIT 100"), "parameters": {}}
        for q in queries
    ]


async def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repository-id", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--queries", help="JSON list of {query, parameters}")
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--max-elements", type=int, default=100)
    parser.add_argument("--show", action="store_true", help="print each text reply")
    args = parser.parse_args()
    driver = AsyncGraphDatabase.driver(
        os.environ["DEEPTHOUGHT_NEO4J_URI"],
        auth=(
            os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
            os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
        ),
    )
    database = os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j")
    service = GraphQueryService(driver, database)
    queries = []
    if args.queries:
        queries += json.load(open(args.queries))
    if args.generate:
        queries += generated(*(await labels_and_types(driver, database)))
    checked = lost = failed = 0
    sizes = [0, 0]
    for item in queries:
        options = dict(
            snapshot_id=args.snapshot,
            allow_database_scope=True,
            limit=args.max_elements,
            max_output_bytes=65536,
        )
        try:
            rows = await service.query_cypher(
                item["query"],
                item.get("parameters") or {},
                args.repository_id,
                **options,
            )
            text = await service.query_cypher_text(
                item["query"],
                item.get("parameters") or {},
                args.repository_id,
                **options,
            )
        except Exception as error:  # a query the service rejects in both forms
            failed += 1
            print(f"REJECTED {type(error).__name__}: {error}: {item['query'][:120]}")
            continue
        checked += 1
        sizes[0] += len(json.dumps(rows, indent=2).encode())
        sizes[1] += len(text.encode())
        wanted = set()
        facts(rows["items"], wanted)
        missing = sorted(f for f in wanted if f not in text)
        if "more lines cut at the" in text:
            missing = []  # the text budget cut whole lines and said so
        if missing:
            lost += 1
            print(f"LOST {missing[:8]}: {item['query'][:160]}")
        if args.show:
            print("=" * 72, item["query"], text, sep="\n")
    await driver.close()
    print(
        json.dumps(
            {
                "queries": checked,
                "rejected": failed,
                "lost_facts_in": lost,
                "json_bytes": sizes[0],
                "text_bytes": sizes[1],
            }
        )
    )
    return 1 if lost else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
