"""Score service-contract links against a labelled corpus.

The corpus file lists services (name, root directory) and labelled links
between them, each with its client location (see
docs/graph-port.md, "Service contracts"). The tool's REQUESTS edges are
lifted to service pairs by the longest service root that contains each end.

Two levels are scored:

- service pairs: (from service, to service, kind) sets;
- call sites: a linked request matches a labelled link of the same kind when
  the client file is the same and the client and server lines are at most
  --line-tolerance apart (see _score_sites).

Labelled links marked ``in_scope_static: false`` are reported but left out of
recall. Usage:

    python tools/check_contracts_ground_truth.py CORPUS.json WORKSPACE [--json OUT]
"""

import argparse
import asyncio
import json
from pathlib import Path
import sys


def _service_of(path, roots):
    best = None
    for name, root in roots:
        prefix = root.rstrip("/") + "/" if root else ""
        if path.startswith(prefix) and (best is None or len(prefix) > len(best[1])):
            best = (name, prefix)
    return None if best is None else best[0]


def _near(location, file, line, tolerance):
    return (
        location is not None
        and location.get("file") == file
        and location.get("line") is not None
        and abs(location["line"] - line) <= tolerance
    )


def _dont_care(item, entries, tolerance):
    for entry in entries:
        path = entry.get("file") or ""
        if entry.get("line") is None:
            if item["file"] == path or item["file"].startswith(path.rstrip("/") + "/"):
                return True
        elif _near(entry, item["file"], item["line"], tolerance):
            return True
    return False


def _ratio(a, b):
    return None if b == 0 else round(a / b, 4)


def _score_sites(predicted, labelled, corpus, tolerance):
    """Greedy one-to-one matching of predicted links to labelled ones.

    A prediction matches a labelled link when the client file is the same,
    both client and server lines are within ``tolerance`` (any of the link's
    ``server_alternatives`` counts), in a fixed order. Predictions matching
    out-of-scope links, or made at don't-care call sites, count for neither
    side; predictions from ``unmatched_clients`` are false positives.
    """
    used = set()
    true_positive, false_positive, neutral = [], [], []
    dont_care = corpus.get("dont_care_call_sites", [])
    for item in sorted(predicted, key=lambda i: (i["file"], i["line"], i["route"])):
        best = None
        for index, link in enumerate(labelled):
            if index in used or not _near(
                link.get("client"), item["file"], item["line"], tolerance
            ):
                continue
            servers = [link.get("server")] + link.get("server_alternatives", [])
            if not any(
                _near(server, item["server_file"], item["server_line"], tolerance)
                for server in servers
            ):
                continue
            distance = abs(link["client"]["line"] - item["line"])
            if best is None or distance < best[0]:
                best = (distance, index)
        if best is not None:
            used.add(best[1])
            link = labelled[best[1]]
            if link.get("in_scope_static", True):
                true_positive.append((item, link))
            else:
                neutral.append(item)
        elif _dont_care(item, dont_care, tolerance) and not any(
            _near(u.get("client"), item["file"], item["line"], tolerance)
            for u in corpus.get("unmatched_clients", [])
        ):
            neutral.append(item)
        else:
            false_positive.append(item)
    in_scope = [
        (index, link)
        for index, link in enumerate(labelled)
        if link.get("in_scope_static", True)
    ]
    false_negative = [link for index, link in in_scope if index not in used]
    return {
        "_tp_items": [item for item, _ in true_positive],
        "predicted": len(true_positive) + len(false_positive),
        "neutral": len(neutral),
        "labelled_in_scope": len(in_scope),
        "true_positive": len(true_positive),
        "precision": _ratio(
            len(true_positive), len(true_positive) + len(false_positive)
        ),
        "recall": _ratio(len(true_positive), len(in_scope)),
        "false_positive": false_positive,
        "false_negative": [
            {
                "id": link.get("id"),
                "contract": link.get("contract"),
                "hop": link.get("hop"),
                "client": link.get("client"),
            }
            for link in false_negative
        ],
    }


def _score_pairs(sites, labelled):
    """Service pairs of true and false positives against in-scope pairs."""

    def pair(link):
        return (link["from_service"], link["to_service"])

    expected = {pair(link) for link in labelled if link.get("in_scope_static", True)}
    found = {
        (i["from_service"], i["to_service"])
        for i in sites["_tp_items"] + sites["false_positive"]
        if i["from_service"] != i["to_service"]
    }
    correct = found & {pair(link) for link in labelled}
    return {
        "predicted": len(found),
        "labelled_in_scope": len(expected),
        "true_positive": len(correct),
        "precision": _ratio(len(correct), len(found)),
        "recall": _ratio(len(expected & found), len(expected)),
        "false_positive": sorted(found - correct),
        "false_negative": sorted(expected - found),
    }


def score(corpus, workspace, line_tolerance=3, kind="http"):
    from deepthought.analysis_tools import ServiceContractTool

    tool = ServiceContractTool(Path(workspace))
    output = asyncio.run(tool.analyze("corpus"))
    _, routes, requests, unread, resolutions = tool.last_facts
    roots = [(s["name"], s["root"]) for s in corpus["services"]]
    route_by_id = {r.id: r for r in routes}
    request_by_id = {r.id: r for r in requests}
    predicted = []
    for resolution in resolutions:
        if resolution.status != "linked":
            continue
        request = request_by_id[resolution.request_id]
        route = route_by_id[resolution.route_id]
        predicted.append(
            {
                "kind": kind,
                "from_service": _service_of(request.file, roots),
                "to_service": _service_of(route.file, roots),
                "file": request.file,
                "line": request.line,
                "server_file": route.file,
                "server_line": route.line,
                "route": f"{' '.join(route.methods)} {route.path}",
                "rule_id": resolution.rule_id,
            }
        )
    labelled = [link for link in corpus["links"] if link["kind"] == kind]
    report = {
        "corpus": corpus.get("corpus"),
        "commit": corpus.get("commit"),
        "resolution": output.metadata.custom_metrics["http_request_resolution"],
        "routes": len(routes),
        "requests": len(requests),
        "unread": len(unread),
    }
    for name, subset in (
        (
            "direct",
            [link for link in labelled if link.get("hop", "direct") == "direct"],
        ),
        ("all", labelled),
    ):
        sites = _score_sites(predicted, subset, corpus, line_tolerance)
        if name == "direct":
            # Predictions matching labelled indirect links are not errors here.
            indirect = [
                link for link in labelled if link.get("hop", "direct") != "direct"
            ]
            extra = _score_sites(
                sites["false_positive"], indirect, corpus, line_tolerance
            )
            sites["neutral"] += extra["true_positive"]
            kept = {id(i) for i in extra["false_positive"]}
            sites["false_positive"] = [
                i for i in sites["false_positive"] if id(i) in kept
            ]
            sites["predicted"] = sites["true_positive"] + len(sites["false_positive"])
            sites["precision"] = _ratio(sites["true_positive"], sites["predicted"])
        pairs = _score_pairs(sites, subset)
        del sites["_tp_items"]
        report[name] = {"call_sites": sites, "service_pairs": pairs}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("corpus", type=Path)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--line-tolerance", type=int, default=3)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    report = score(corpus, args.workspace, args.line_tolerance)
    text = json.dumps(report, indent=2, sort_keys=True, default=list)
    if args.json:
        args.json.write_text(text + "\n", encoding="utf-8")
    summary = {
        name: {
            level: {
                k: report[name][level][k]
                for k in (
                    "precision",
                    "recall",
                    "true_positive",
                    "predicted",
                    "labelled_in_scope",
                )
            }
            for level in ("call_sites", "service_pairs")
        }
        for name in ("direct", "all")
    }
    print(json.dumps({"corpus": report["corpus"], **summary}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
