"""Check the engine's protoc reader against an independent .proto parser.

    python tools/proto_truth.py REPO [REPO ...] [--oracle BINARY]

The engine reads .proto files only with protoc
(``deepthought.service_contracts.protoc.read_protos``). The oracle is
github.com/emicklei/proto (``tools/proto_oracle``, built with ``go build``
when ``--oracle`` is not given), a Go parser that shares no code with
protoc. For every .proto file git tracks in REPO, both list each rpc as
(file, package.Service, method, line of the rpc, streaming kind) and each
``google.api.http`` binding as (file, package.Service, method, verb, path).

*agree* counts the rpcs and bindings both list. *engine_only* is anything
the engine lists that the oracle does not: a misread. *oracle_only* is
split by whether protoc compiled the file. A file the engine left unread is
reported with protoc's reason, since protoc decides what compiles: an
import outside the repository, or a file protoc rejects. In a file the
engine read, an rpc it missed is a misread too.

Prints one JSON object per repository. Exits 1 when any repository has a
misread (``engine_only`` or ``missed_in_read_files``).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ORACLE_DIR = Path(__file__).with_name("proto_oracle")


def build_oracle(directory):
    go = os.environ.get("DEEPTHOUGHT_GO") or shutil.which("go")
    if go is None:
        raise RuntimeError("go is not installed")
    binary = Path(directory) / "proto_oracle"
    subprocess.run(
        [go, "build", "-o", str(binary), "."],
        cwd=ORACLE_DIR,
        check=True,
        capture_output=True,
    )
    return binary


def oracle_declarations(oracle, repo, protos):
    """(rpcs, bindings, parse errors) as the oracle reads them."""
    result = subprocess.run(
        [str(oracle), str(repo)],
        input=json.dumps(protos),
        capture_output=True,
        text=True,
        check=True,
    )
    rpcs, bindings, errors = set(), set(), {}
    for item in json.loads(result.stdout):
        if item.get("error"):
            errors[item["file"]] = item["error"]
            continue
        for rpc in item["rpcs"]:
            service = (
                f"{item['package']}.{rpc['service']}"
                if item["package"]
                else rpc["service"]
            )
            rpcs.add(
                (item["file"], service, rpc["method"], rpc["line"], rpc["streaming"])
            )
            for b in rpc["http"]:
                bindings.add(
                    (item["file"], service, rpc["method"], b["verb"], b["path"])
                )
    return rpcs, bindings, errors


def engine_declarations(repo, protos):
    """(rpcs, bindings, files read) as the engine's protoc reader reads them."""
    from deepthought.service_contracts.protoc import read_protos

    texts = {p: (repo / p).read_text("utf-8", "replace") for p in protos}
    read = read_protos(repo, protos, texts)
    rpcs, bindings = set(), set()
    for _package, methods, http, _types in read.values():
        for m in methods:
            rpcs.add((m.file, m.service, m.method, m.line, m.streaming))
        for b in http:
            bindings.add((b.file, b.service, b.method, b.verb, b.path))
    return rpcs, bindings, set(read)


def compare(repo, oracle, examples=10):
    repo = Path(repo)
    protos = sorted(
        subprocess.run(
            ["git", "-C", str(repo), "ls-files", "*.proto"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    )
    o_rpcs, o_http, errors = oracle_declarations(oracle, repo, protos)
    e_rpcs, e_http, read = engine_declarations(repo, protos)
    engine_only = sorted((e_rpcs - o_rpcs) | (e_http - o_http))
    missed = sorted(x for x in (o_rpcs - e_rpcs) | (o_http - e_http) if x[0] in read)
    unread_rpcs = sorted(x for x in o_rpcs - e_rpcs if x[0] not in read)
    return {
        "repo": repo.name,
        "protos": len(protos),
        "read_by_engine": len(read),
        "oracle_parse_errors": len(errors),
        "rpcs": {
            "engine": len(e_rpcs),
            "oracle": len(o_rpcs),
            "agree": len(e_rpcs & o_rpcs),
        },
        "http": {
            "engine": len(e_http),
            "oracle": len(o_http),
            "agree": len(e_http & o_http),
        },
        "engine_only": [list(x) for x in engine_only[:examples]],
        "missed_in_read_files": [list(x) for x in missed[:examples]],
        "rpcs_in_unread_files": len(unread_rpcs),
        "unread_files": sorted(set(protos) - read)[:examples],
        "misreads": len(engine_only) + len(missed),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("repos", nargs="+", type=Path)
    parser.add_argument("--oracle", type=Path)
    parser.add_argument("--examples", type=int, default=10)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        oracle = args.oracle or build_oracle(tmp)
        reports = [compare(r, oracle, args.examples) for r in args.repos]
    for report in reports:
        print(json.dumps(report))
    return 1 if any(r["misreads"] for r in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
