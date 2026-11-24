"""Shared helper for the hand-labelled contract corpora built after build.py.

Line numbers are located by exact substring match at the pinned checkout,
asserted unique (or nth). CORPORA names the directory holding the clones.
"""

import json
import os
import subprocess

C = os.environ.get("CORPORA", "corpora")


def head(repo):
    return subprocess.check_output(
        ["git", "-C", f"{C}/{repo}", "rev-parse", "HEAD"], text=True
    ).strip()


class Corpus:
    def __init__(self, repo_dir, **meta):
        self.dir = repo_dir
        self.meta = meta
        self.links = []
        self.dont_care = []

    def loc(self, f, pat, n=None):
        lines = open(f"{C}/{self.dir}/{f}", encoding="utf-8").read().split("\n")
        hits = [i + 1 for i, line in enumerate(lines) if pat in line]
        if n is None:
            assert len(hits) == 1, (self.dir, f, pat, hits)
            return {"file": f, "line": hits[0]}
        assert len(hits) >= n, (self.dir, f, pat, hits)
        return {"file": f, "line": hits[n - 1]}

    def link(self, kind, frm, to, contract, client, server, evidence, reason=None, **extra):
        item = {
            "kind": kind,
            "from_service": frm,
            "to_service": to,
            "contract": contract,
            "client": client,
            "server": server,
            "evidence": evidence,
            "in_scope_static": reason is None,
        }
        if reason is not None:
            item["out_of_scope_reason"] = reason
        item.update({"hop": "direct", "label_confidence": "high", **extra})
        self.links.append(item)

    def dc(self, where, why):
        self.dont_care.append({**where, "reason": why})

    def write(self, out):
        m = dict(self.meta)
        m["commit"] = head(self.dir)
        for i, link in enumerate(self.links):
            link["id"] = f"{m['corpus']}#{i + 1}"
        summary = {
            k: {
                "in_scope": sum(1 for x in self.links if x["kind"] == k and x["in_scope_static"]),
                "out_of_scope": sum(
                    1 for x in self.links if x["kind"] == k and not x["in_scope_static"]
                ),
            }
            for k in ("http", "grpc", "messaging")
        }
        pairs = sorted(
            {(x["from_service"], x["to_service"], x["kind"]) for x in self.links if x["in_scope_static"]}
        )
        summary["service_pairs_in_scope"] = [list(p) for p in pairs]
        m["summary"] = summary
        m["links"] = self.links
        m["dont_care_call_sites"] = self.dont_care
        order = ["corpus", "repo", "commit", "license", "languages", "sources",
                 "services", "summary", "links", "dont_care_call_sites", "notes"]
        m = {k: m[k] for k in order if k in m} | {k: v for k, v in m.items() if k not in order}
        with open(f"{out}/{m['corpus']}.json", "w") as fh:
            json.dump(m, fh, indent=2)
            fh.write("\n")
        print(m["corpus"], m["commit"], len(self.links), "links")
