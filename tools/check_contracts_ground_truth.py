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

For kind ``grpc`` a call site matches on the client location and the
contract (``package.Service/Method``) instead of the server line, since a
method may have several servers or one in a language not read; servers are
scored apart, as HANDLES edges found at labelled server locations.

Labelled links marked ``in_scope_static: false`` are reported but left out of
recall. Usage:

    python tools/check_contracts_ground_truth.py CORPUS.json WORKSPACE \
        [--kind http|grpc] [--json OUT]
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
            if "contract" in item:
                if item["contract"] != link.get("contract"):
                    continue
            elif not any(
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
        if i["from_service"] != i["to_service"] and i["to_service"] is not None
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


def _grpc_predictions(tool, roots):
    """One item per linked stub call; ``to_service`` is the service of the
    method's servers when they all lie in one service, else None."""
    facts = tool.last_grpc
    calls = {c.id: c for c in facts.calls}
    servers = {}
    for handler in facts.handlers:
        servers.setdefault((handler.service, handler.method), []).append(handler)
    predicted = []
    for resolution in facts.resolutions:
        if resolution.status != "linked":
            continue
        call = calls[resolution.call_id]
        handlers = servers.get((resolution.service, resolution.method), [])
        targets = {_service_of(h.file, roots) for h in handlers}
        contract = f"{resolution.service}/{resolution.method}"
        predicted.append(
            {
                "kind": "grpc",
                "from_service": _service_of(call.file, roots),
                "to_service": next(iter(targets)) if len(targets) == 1 else None,
                "file": call.file,
                "line": call.line,
                "contract": contract,
                "route": contract,
                "rule_id": "service-contracts.grpc-stub-call",
            }
        )
    return predicted


def _grpc_servers(tool, labelled, tolerance):
    """Labelled links whose server location carries a HANDLES edge."""
    handlers = tool.last_grpc.handlers
    found, missed = 0, []
    for link in labelled:
        if not link.get("in_scope_static", True):
            continue
        servers = [link.get("server")] + link.get("server_alternatives", [])
        contract = link.get("contract", "")
        if any(
            f"{h.service}/{h.method}" == contract
            and _near(server, h.file, h.line, tolerance)
            for h in handlers
            for server in servers
        ):
            found += 1
        else:
            missed.append({"id": link.get("id"), "server": link.get("server")})
    return {
        "labelled_in_scope": found + len(missed),
        "true_positive": found,
        "recall": _ratio(found, found + len(missed)),
        "false_negative": missed,
    }


def _message_pairs(endpoints, roots):
    """Each publish site paired with each consume site of its destination."""
    consumers = {}
    for endpoint in endpoints:
        if endpoint.role == "consume":
            consumers.setdefault((endpoint.broker, endpoint.destination), []).append(
                endpoint
            )
    pairs = []
    for endpoint in endpoints:
        if endpoint.role != "publish":
            continue
        for consumer in consumers.get((endpoint.broker, endpoint.destination), []):
            pairs.append(
                {
                    "kind": "messaging",
                    "from_service": _service_of(endpoint.file, roots),
                    "to_service": _service_of(consumer.file, roots),
                    "file": endpoint.file,
                    "line": endpoint.line,
                    "server_file": consumer.file,
                    "server_line": consumer.line,
                    "route": f"{endpoint.broker} {endpoint.destination}",
                    "rule_id": endpoint.client,
                }
            )
    return pairs


def score(corpus, workspace, line_tolerance=3, kind="http"):
    from deepthought.analysis_tools import ServiceContractTool

    tool = ServiceContractTool(Path(workspace))
    output = asyncio.run(tool.analyze("corpus"))
    missing = sorted(
        key
        for key in output.metadata.custom_metrics.get("unread", {})
        if key.endswith("yaml_not_installed")
    )
    if missing:
        # Gateway routes are read with pyyaml; without it the scores drop
        # for reasons of the environment, not the extractor.
        raise RuntimeError(
            "pyyaml is not installed (install the 'languages' extra); "
            "unread: " + ", ".join(missing)
        )
    _, routes, requests, unread, resolutions = tool.last_facts
    roots = [(s["name"], s["root"]) for s in corpus["services"]]
    if kind == "grpc":
        return _score_grpc(tool, output, corpus, roots, line_tolerance)
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
    if kind == "messaging":
        predicted = _message_pairs(tool.last_messages[0], roots)
    labelled = [link for link in corpus["links"] if link["kind"] == kind]
    report = {
        "corpus": corpus.get("corpus"),
        "commit": corpus.get("commit"),
        "resolution": output.metadata.custom_metrics["http_request_resolution"],
        "routes": len(routes),
        "requests": len(requests),
        "unread": len(unread),
    }
    _score_levels(report, predicted, labelled, corpus, line_tolerance)
    return report


def _score_grpc(tool, output, corpus, roots, line_tolerance):
    metrics = output.metadata.custom_metrics
    labelled = [link for link in corpus["links"] if link["kind"] == "grpc"]
    report = {
        "corpus": corpus.get("corpus"),
        "commit": corpus.get("commit"),
        "resolution": metrics["grpc_call_resolution"],
        "methods": metrics["grpc_methods"],
        "calls": metrics["grpc_calls"],
        "handlers": metrics["grpc_handlers"],
        "servers": _grpc_servers(tool, labelled, line_tolerance),
    }
    predicted = _grpc_predictions(tool, roots)
    _score_levels(report, predicted, labelled, corpus, line_tolerance)
    return report


def _score_levels(report, predicted, labelled, corpus, line_tolerance):
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("corpus", type=Path)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--line-tolerance", type=int, default=3)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--kind", default="http", choices=("http", "grpc", "messaging"))
    args = parser.parse_args(argv)
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    report = score(corpus, args.workspace, args.line_tolerance, args.kind)
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
