"""Build the agent-ab-1 task scenarios from ground truth.

Each task in evals/milestones/agent-ab-1.json becomes one scenario JSON for
deepthought.evaluation, graded by a grader from tools/ab_graders.py. Truth
comes only from the verification ground truth (impact and call truth TSVs,
contract labels) and from facts hand-checked against source at the pin, never
from engine output. Seeded patches for the check tasks are built here from call
truth with the fixed seeds the milestone names.

    python tools/ab_tasks.py MILESTONE_JSON --cache DIR --out DIR

reads pinned checkouts from DIR/<cache name> (as tools/ground_truth.py check
leaves them), reads blobs at the pinned commit only, and never runs repository
code. Every hand-checked fact and every patch edit asserts the exact source text
it relies on, so a wrong pin or a moved line fails the build.
"""

import argparse
import csv
import difflib
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
GT = ROOT / "evals" / "ground_truth"
CONTRACTS = ROOT / "evals" / "fixtures" / "service_contracts"

# corpus -> (checkout in the cache, scenario languages)
CORPORA = {
    "cobra": ("cobra", ["go"]),
    "chi": ("chi", ["go"]),
    "hono": ("hono", ["typescript"]),
    "ky": ("ky", ["typescript"]),
    "contracts-online-boutique": (
        "contracts-online-boutique",
        ["go", "javascript", "python", "java"],
    ),
    "contracts-robot-shop": (
        "contracts-robot-shop",
        ["javascript", "python", "php", "java", "go"],
    ),
}


def git(checkout, *args):
    return subprocess.run(
        ["git", "-C", str(checkout), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


class Pin:
    """Read-only view of one corpus at its pinned commit."""

    def __init__(self, corpus, cache):
        name, self.languages = CORPORA[corpus]
        manifest = json.loads((GT / "manifest.json").read_text())
        entry = next(
            c
            for section in ("corpora", "contracts")
            for c in manifest[section]
            if c["name"] == corpus
        )
        self.corpus, self.url, self.commit = corpus, entry["url"], entry["commit"]
        self.checkout = Path(cache) / name
        head = git(self.checkout, "rev-parse", "HEAD").strip()
        if head != self.commit:
            raise SystemExit(f"{corpus}: checkout at {head}, pin is {self.commit}")
        self._blobs = {}

    def text(self, path):
        if path not in self._blobs:
            self._blobs[path] = git(self.checkout, "show", f"{self.commit}:{path}")
        return self._blobs[path]

    def lines(self, path):
        return self.text(path).splitlines()

    def expect(self, path, line, fragment):
        actual = self.lines(path)[line - 1]
        if fragment not in actual:
            raise SystemExit(
                f"{self.corpus} {path}:{line} lacks {fragment!r}: {actual!r}"
            )

    def whole(self, path, role="unjudged"):
        return {
            "role": role,
            "file": path,
            "line": 1,
            "span": [1, len(self.lines(path))],
        }

    def source(self):
        files = {}
        rows = git(self.checkout, "ls-tree", "-r", "-l", "--full-tree", self.commit)
        for row in rows.splitlines():
            meta, path = row.split("\t", 1)
            mode, kind, sha, size = meta.split()
            if kind != "blob" or mode == "120000" or int(size) > 4_000_000:
                continue
            data = subprocess.run(
                ["git", "-C", str(self.checkout), "cat-file", "blob", sha],
                check=True,
                capture_output=True,
            ).stdout
            files[path] = hashlib.sha256(data).hexdigest()
        return {
            "kind": "public_repository",
            "repository": self.url,
            "revision": self.commit,
            "files": dict(sorted(files.items())),
        }


def loc(role, file, line, span=None):
    row = {"role": role, "file": file, "line": line}
    if span:
        row["span"] = span
    return row


# ------------------------------------------------------------------ truth


def impact_rows(corpus, seed_file, seed_line):
    with (GT / "impact" / f"{corpus}.tsv").open() as stream:
        rows = [
            r
            for r in csv.reader(stream, delimiter="\t")
            if r and not r[0].startswith("#")
        ]
    found = [
        (r[3], int(r[4]), r[5], int(r[6]))
        for r in rows
        if r[0] == seed_file and int(r[1]) == seed_line
    ]
    if not found:
        raise SystemExit(f"no impact truth for {corpus} {seed_file}:{seed_line}")
    return found


def call_sites(corpus, target_file, target_line):
    with (GT / "calls" / f"{corpus}.tsv").open() as stream:
        rows = [
            r
            for r in csv.reader(stream, delimiter="\t")
            if r and not r[0].startswith("#")
        ]
    return sorted(
        (r[0], int(r[1]))
        for r in rows
        if r[3] == target_file and int(r[4]) == target_line
    )


def contract_links(name, contract):
    labels = json.loads((CONTRACTS / f"{name}.json").read_text())
    links = [link for link in labels["links"] if link["contract"] == contract]
    if not links:
        raise SystemExit(f"no {name} links for {contract}")
    return labels, links


def dont_cares(pin, labels):
    rows = []
    for site in labels.get("dont_care_call_sites", []):
        if site["line"] is None:
            rows.append(pin.whole(site["file"]))
        else:
            rows.append(loc("unjudged", site["file"], site["line"]))
    return rows


def affected_impact(pin, seed_file, seed_line, seed_name):
    rows = impact_rows(pin.corpus, seed_file, seed_line)
    seed = [r for r in rows if r[3] == 0]
    if [r[2] for r in seed] != [seed_name]:
        raise SystemExit(
            f"{pin.corpus}: seed {seed_file}:{seed_line} is not {seed_name}"
        )
    # The seed itself is the change, not something it affects.
    return [loc("unjudged", seed_file, seed_line)] + [
        loc("affected", f, n) for f, n, _, d in rows if d > 0
    ]


def plan_impact(pin, seed_file, seed_line, seed_name):
    rows = impact_rows(pin.corpus, seed_file, seed_line)
    if [r[2] for r in rows if r[3] == 0] != [seed_name]:
        raise SystemExit(
            f"{pin.corpus}: seed {seed_file}:{seed_line} is not {seed_name}"
        )
    # The changed function and its direct callers must be edited; callers two
    # or three calls away keep compiling but their behaviour can change.
    return [loc("must_edit" if d <= 1 else "retest", f, n) for f, n, _, d in rows]


def u1(pin):
    labels, links = contract_links("online-boutique", "hipstershop.CartService/GetCart")
    server = {(link["server"]["file"], link["server"]["line"]) for link in links}
    rows = [
        loc("affected", link["client"]["file"], link["client"]["line"])
        for link in links
    ]
    rows += [loc("affected", f, n) for f, n in sorted(server)]
    pin.expect("src/frontend/rpc.go", 58, ".GetCart(ctx")
    pin.expect("src/checkoutservice/main.go", 325, ".GetCart(ctx")
    pin.expect("src/cartservice/src/services/CartService.cs", 40, "GetCart(")
    # Definitions and stores of the message, and the protoc output, are
    # reasonable things to review but are not call or handler sites.
    rows += [pin.whole("protos/demo.proto")]
    rows += [
        pin.whole(f"src/{svc}/genproto/{name}")
        for svc in ("frontend", "checkoutservice")
        for name in ("demo.pb.go", "demo_grpc.pb.go")
    ]
    for path in sorted(
        git(
            pin.checkout,
            "ls-tree",
            "-r",
            "--name-only",
            pin.commit,
            "src/cartservice/src/cartstore",
        ).split()
    ):
        rows.append(pin.whole(path))
    for path in ("src/frontend/handlers.go", "src/checkoutservice/main.go"):
        for n, text in enumerate(pin.lines(path), 1):
            if ("getCart(" in text or "getUserCart(" in text) and "func " not in text:
                rows.append(loc("unjudged", path, n))
    return rows + dont_cares(pin, labels)


def u2(pin):
    labels, links = contract_links("robot-shop", "GET /product/{sku}")
    rows = []
    for link in links:
        client = link["client"]
        if client["file"] == "ratings/html/src/Service/CatalogueService.php":
            # curl_init(:32) issues the request for the URL built at :27.
            pin.expect(client["file"], 27, "/product/%s")
            pin.expect(client["file"], 32, "curl_init")
            rows.append(loc("affected", client["file"], 32, [27, 32]))
        else:
            rows.append(loc("affected", client["file"], client["line"]))
    server = links[0]["server"]
    pin.expect(server["file"], server["line"], "app.get('/product/:sku'")
    rows.append(loc("unjudged", server["file"], server["line"]))
    rows.append(pin.whole("web/default.conf.template"))
    return rows + dont_cares(pin, labels)


def p3(pin):
    labels, links = contract_links(
        "online-boutique", "hipstershop.CurrencyService/Convert"
    )
    rows = [
        loc("must_edit", link["client"]["file"], link["client"]["line"])
        for link in links
    ]
    server = {(link["server"]["file"], link["server"]["line"]) for link in links}
    rows += [loc("must_edit", f, n) for f, n in sorted(server)]
    pin.expect("src/currencyservice/server.js", 138, "function convert (call")
    # Repointing also touches the address wiring and the deployment manifests;
    # those are right to mention but are not call sites or the handler.
    rows += [pin.whole("protos/demo.proto")]
    for path in ("src/frontend/main.go", "src/checkoutservice/main.go"):
        for n, text in enumerate(pin.lines(path), 1):
            if "currencySvc" in text and n not in {
                link["client"]["line"] for link in links
            }:
                rows.append(loc("unjudged", path, n))
    for path in git(
        pin.checkout, "grep", "-l", "CURRENCY_SERVICE_ADDR", pin.commit
    ).split():
        path = path.split(":", 1)[1]
        if not path.endswith((".go", ".js")):
            rows.append(pin.whole(path))
    return rows + dont_cares(pin, labels)


# ---------------------------------------------------------- seeded patches


def unified(pin, edits):
    """edits: {path: [(line, old, new)]}; returns a git-style unified diff."""
    out = []
    for path in sorted(edits):
        before = pin.text(path).splitlines(keepends=True)
        after = list(before)
        for line, old, new in edits[path]:
            if old not in after[line - 1]:
                raise SystemExit(f"{pin.corpus} {path}:{line} lacks {old!r}")
            after[line - 1] = after[line - 1].replace(old, new)
        out += difflib.unified_diff(before, after, f"a/{path}", f"b/{path}", n=3)
    return "".join(out)


def check_patch(pin, target, old, new, sites, omitted, extra=()):
    edits = {}
    for file, line in [target, *[s for s in sites if s not in omitted], *extra]:
        edits.setdefault(file, []).append((line, old, new))
    covered = [loc("covered", f, n) for f, n in sites if (f, n) not in omitted]
    return unified(pin, edits), [loc("omission", f, n) for f, n in omitted] + covered


def c1(pin):
    target = ("command.go", 1219)
    sites = call_sites("cobra", *target)
    omitted = sorted(random.Random(1).sample(sites, 2))
    pin.expect("command.go", 1216, "// InitDefaultHelpFlag adds")
    pin.expect("command.go", 929, "flag in InitDefaultHelpFlag()")
    patch, rows = check_patch(
        pin,
        target,
        "InitDefaultHelpFlag",
        "InitHelpFlag",
        sites,
        omitted,
        extra=[("command.go", 1216), ("command.go", 929)],
    )
    return patch, rows, omitted


def c2(pin):
    target = ("source/utils/is.ts", 13)
    sites = call_sites("ky", *target)
    omitted = sorted(random.Random(2).sample(sites, 1))
    edits = {}
    pin.expect(*target, "export const isError = (value: unknown): value is Error =>")
    edits[target[0]] = [
        (13, "(value: unknown)", "(value: unknown, crossRealm: boolean)")
    ]
    pin.expect(target[0], 14, "objectToString.call(value) === '[object Error]'")
    edits[target[0]].append(
        (14, "|| objectToString", "|| (crossRealm && objectToString")
    )
    edits[target[0]].append((14, "'[object Error]';", "'[object Error]');"))
    for file, line in sites:
        if (file, line) in omitted:
            continue
        text = pin.lines(file)[line - 1]
        start = text.index("isError(") + len("isError(")
        depth, end = 1, start
        while depth:
            depth += {"(": 1, ")": -1}.get(text[end], 0)
            end += 1
        argument = text[start : end - 1]
        edits.setdefault(file, []).append(
            (line, f"isError({argument})", f"isError({argument}, true)")
        )
    rows = [loc("omission", f, n) for f, n in omitted]
    rows += [loc("covered", f, n) for f, n in sites if (f, n) not in omitted]
    return unified(pin, edits), rows, omitted


def c3(pin):
    labels, links = contract_links("robot-shop", "GET /product/{sku}")
    edits = {
        "catalogue/server.js": [(82, "'/product/:sku'", "'/products/item/:sku'")],
        "cart/server.js": [(362, ":8080/product/'", ":8080/products/item/'")],
        "web/static/js/controller.js": [
            (244, "'/api/catalogue/product/'", "'/api/catalogue/products/item/'")
        ],
    }
    omitted = [
        ("ratings/html/src/Service/CatalogueService.php", 32),
        ("load-gen/robot-shop.py", 68),
    ]
    pin.expect("ratings/html/src/Service/CatalogueService.php", 27, "'%s/product/%s'")
    pin.expect("load-gen/robot-shop.py", 68, "'/api/catalogue/product/{}'")
    clients = {(link["client"]["file"], link["client"]["line"]) for link in links}
    expected_clients = {
        ("cart/server.js", 362),
        ("web/static/js/controller.js", 243),
        ("ratings/html/src/Service/CatalogueService.php", 27),
        ("ratings/html/src/Service/CatalogueService.php", 32),
        ("load-gen/robot-shop.py", 68),
    }
    if not clients <= expected_clients or len(clients) != 4:
        raise SystemExit(
            f"robot-shop GET /product/{{sku}} clients changed: {sorted(clients)}"
        )
    rows = [
        loc("omission", omitted[0][0], 32, [27, 32]),
        loc("omission", *omitted[1]),
        loc("covered", "cart/server.js", 362),
        loc("covered", "web/static/js/controller.js", 244, [243, 244]),
        loc("unjudged", "catalogue/server.js", 82),
        pin.whole("web/default.conf.template"),
    ]
    return unified(pin, edits), rows + dont_cares(pin, labels), omitted


# ------------------------------------------------------------------ facts


def k1(pin):
    pin.expect(
        "command.go", 1084, "func (c *Command) ExecuteC() (cmd *Command, err error) {"
    )
    return [
        {"role": "fact", "key": "file", "value": "command.go"},
        {"role": "fact", "key": "line", "value": 1084},
        {"role": "fact", "key": "returns", "value": ["*Command", "error"]},
    ]


# name -> (line, required): checked with the TypeScript AST at the pin. Every
# exported const initialised with an arrow function is required. getQueryParam
# casts the module-private _getQueryParam and decodeURIComponent_ aliases the
# global, so naming either is neither right nor wrong.
HONO_URL_EXPORTS = {
    "splitPath": (8, True),
    "splitRoutingPath": (16, True),
    "getPattern": (51, True),
    "tryDecode": (81, True),
    "tryDecodeURI": (104, True),
    "getPath": (109, True),
    "getQueryStrings": (156, True),
    "getPathNoStrict": (170, True),
    "mergePath": (187, True),
    "checkOptionalParameter": (200, True),
    "tryDecodeURIComponent": (237, True),
    "getQueryParam": (345, False),
    "getQueryParams": (353, True),
    "decodeURIComponent_": (362, False),
}


def k2(pin):
    exported = [
        (n, text)
        for n, text in enumerate(pin.lines("src/utils/url.ts"), 1)
        if text.startswith("export ")
    ]
    names = {}
    for n, text in exported:
        if text.startswith("export type "):
            continue
        name = text.split()[2].rstrip(":")
        names[name] = n
    if names != {k: v[0] for k, v in HONO_URL_EXPORTS.items()}:
        raise SystemExit(f"hono src/utils/url.ts exports differ: {names}")
    value = [{"name": k, "line": v[0]} for k, v in HONO_URL_EXPORTS.items() if v[1]]
    optional = [
        {"name": k, "line": v[0]} for k, v in HONO_URL_EXPORTS.items() if not v[1]
    ]
    return [{"role": "fact", "key": "functions", "value": value, "optional": optional}]


def k3(pin):
    pin.expect("source/utils/normalize.ts", 25, "const defaultRetryOptions")
    pin.expect("source/utils/normalize.ts", 26, "limit: 2,")
    return [
        {"role": "fact", "key": "limit", "value": 2},
        {"role": "fact", "key": "file", "value": "source/utils/normalize.ts"},
        {"role": "fact", "key": "line", "value": 26},
    ]


FACT_KEYS = {
    "K1": 'Put them in facts as {"file": path, "line": number, "returns": [result types, '
    "without names]}.",
    "K2": 'Put them in facts as {"functions": [{"name": name, "line": number}]}. An '
    "exported function is an exported function declaration or an exported const "
    "initialised with a function; line is the line of the export statement.",
    "K3": 'Put them in facts as {"limit": number, "file": path, "line": number}.',
}


def build(milestone, cache):
    spec = json.loads(Path(milestone).read_text())
    pins = {}
    out = {}
    for task in spec["tasks"]:
        tid, corpus = task["id"], task["corpus"]
        pin = pins.setdefault(corpus, Pin(corpus, cache))
        prompt, metadata, patch = task["prompt"], {"milestone": spec["milestone"]}, None
        if tid == "U1":
            expected = u1(pin)
        elif tid == "U2":
            expected = u2(pin)
        elif tid == "U3":
            expected = affected_impact(pin, "command.go", 1744, "InheritedFlags")
        elif tid == "P1":
            expected = plan_impact(pin, "mux.go", 295, "Mount")
        elif tid == "P2":
            expected = plan_impact(pin, "src/utils/html.ts", 53, "stringBufferToString")
        elif tid == "P3":
            expected = p3(pin)
        elif tid in ("C1", "C2", "C3"):
            patch, expected, omitted = {"C1": c1, "C2": c2, "C3": c3}[tid](pin)
            metadata["patch"] = f"patches/{tid}.diff"
            metadata["patch_sha256"] = hashlib.sha256(patch.encode()).hexdigest()
            metadata["omitted"] = [f"{f}:{n}" for f, n in omitted]
            prompt += "\n\nThe patch, against the checkout you are given:\n\n" + patch
        else:
            expected = {"K1": k1, "K2": k2, "K3": k3}[tid](pin)
            prompt += " " + FACT_KEYS[tid]
        if task["grader"] in ("affected-set-v1", "change-plan-v1", "change-check-v1"):
            metadata["tolerance"] = 3
        out[tid] = (
            {
                "schema_version": 1,
                "id": f"agent-ab-1-{tid}",
                "tool": "tree-sitter",
                "task": prompt,
                "languages": pin.languages,
                "source": pin.source(),
                "expected": expected,
                "grader": task["grader"],
                "metadata": {
                    **metadata,
                    "objective": task["objective"],
                    "corpus": corpus,
                },
            },
            patch,
        )
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("milestone")
    parser.add_argument("--cache", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT / "tools"))
    import ab_graders  # noqa: F401  (registers the graders)
    from deepthought.evaluation import validate_scenario

    out = Path(args.out)
    (out / "tasks").mkdir(parents=True, exist_ok=True)
    (out / "patches").mkdir(exist_ok=True)
    for tid, (scenario, patch) in build(args.milestone, args.cache).items():
        validate_scenario(scenario)
        (out / "tasks" / f"{tid}.scenario.json").write_text(
            json.dumps(scenario, indent=1, ensure_ascii=False) + "\n"
        )
        if patch is not None:
            (out / "patches" / f"{tid}.diff").write_text(patch)
        roles = {}
        for row in scenario["expected"]:
            roles[row["role"]] = roles.get(row["role"], 0) + 1
        print(tid, scenario["grader"], len(scenario["source"]["files"]), "files", roles)


if __name__ == "__main__":
    main()
