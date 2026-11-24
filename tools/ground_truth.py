"""Ground-truth corpus: compiler-derived expected call edges and scoring.

Expected edges come from a compiler or language server's SCIP index, never
from the engine's own extraction, so a regression in a rule cannot move its
own yardstick.

A *call* in the ground truth is a reference occurrence that the source text
shows is applied to an argument list: the occurrence is followed, on the same
line, by ``(``, optionally after a ``::<...>`` or ``<...>`` generic argument
list. The key ``(file, line, name)`` is a source position with the name as
written, so any extractor's call site can be judged against it. Its targets
are where the oracle says the name leads, with a kind:

  callable   an in-repository function, method or type (descriptor ending
             ``().`` or ``#``): each line that defines it
  value      another in-repository symbol (``const f = () => ...``, a
             field): each line that defines it
  external   a symbol defined outside the repository (library, builtin)
  local      a local binding (parameter, loop variable, local function):
             the line that binds it

Only calls with a callable target are *expected* (the recall denominator);
the others let a link at that position be judged right or wrong.

Subcommands:
  derive WORKSPACE INDEX                   write expected calls (TSV) to stdout
  derive-imports WORKSPACE INDEX --language L [--directory]
                                           expected import targets (TSV)
  derive-java WORKSPACE                    the same from javac's attribution
                                           (tools/JavaCallTruth.java)
  score WORKSPACE TRUTH --language L       score the engine against a TSV
  check [--corpus NAME ...] [--cache DIR]  fetch each pinned corpus, score it
                                           and compare with the baseline; the
                                           manifest's ``contracts`` corpora
                                           (labelled cross-service links) and
                                           ``declarations`` corpora (C, C++,
                                           Ruby and PHP declarations from
                                           each language's own parser) and
                                           ``routes`` corpora (HTTP routes
                                           and GraphQL fields labelled from
                                           a spec) too

Scores (calls):
  confirmed     linked, and the target is one the oracle names
  contradicted  linked, the oracle judges the position, names other targets
  unjudged      linked at a position the oracle records no call at
  missed        an expected call with no confirmed engine link
  precision = confirmed / (confirmed + contradicted)
  recall    = expected calls with a confirmed link / expected calls
"""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "evals" / "ground_truth"
HEADER = "# file\tline\tname\ttarget_file\ttarget_line\tkind"
_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_DECLARES = re.compile(
    r"(?:\b(?:def|fn|func|function|class|struct|trait)|\bfunc\s*\([^)]*\))\s+$"
)
_GENERIC = re.compile(r"(?:::)?<[^<>()]*(?:<[^<>()]*>[^<>()]*)*>")


def _descriptor_name(native_id):
    names = _NAME.findall(native_id.split(" ", 4)[-1])
    return names[-1] if names else None


def _callable(native_id):
    return native_id.endswith(("().", "#")) and not native_id.startswith("local ")


def _slice(text, start, end, encoding):
    """Characters ``start:end`` of a line in the index's coordinate units."""
    if encoding == "utf-8":
        return text.encode("utf-8")[start:end].decode("utf-8", "replace")
    if encoding == "utf-16":
        stop = None if end is None else 2 * end
        return text.encode("utf-16-le")[2 * start : stop].decode("utf-16-le", "replace")
    return text[start:end]


def _applied(rest):
    """Whether the text after a name occurrence opens an argument list."""
    generic = _GENERIC.match(rest)
    if generic:
        rest = rest[generic.end() :]
    return rest.lstrip(" \t").startswith("(")


def derive(workspace, index):
    """Expected calls and the files the oracle indexed.

    Returns ``({(file, line, name): {(target_file, target_line)}}, files)``.
    The name is the one written at the occurrence. A constructor's targets
    include its class, since a call that names a class is a construction.
    """
    import deepthought

    source = deepthought.ingest_scip(index, repository_id="ground-truth")
    workspace = Path(workspace)
    indexed = {c.file for c in source.coverage}
    defined, bound = {}, {}
    for d in source.definitions:
        if d.native_id.startswith("local "):
            # Local symbols are scoped to their document.
            bound.setdefault((d.file, d.native_id), set()).add(
                (d.file, d.line, "local")
            )
        elif d.native_id:
            kind = "callable" if _callable(d.native_id) else "value"
            defined.setdefault(d.native_id, set()).add((d.file, d.line, kind))
    lines = {}

    def text_of(file):
        if file not in lines:
            try:
                lines[file] = (workspace / file).read_text("utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                lines[file] = []
        return lines[file]

    # scip-python gives the other declarations of an @overload group (the
    # implementation too), and a redefinition in the same scope, the first
    # one's symbol as a reference on a `def` line. Each is a declaration of
    # that symbol, so a call to it may link to any of them.
    for r in source.references:
        locations = defined.get(r.native_id)
        if not locations or r.line != r.end_line or not _callable(r.native_id):
            continue
        if not r.native_id.startswith("scip-python "):
            continue
        text = text_of(r.file)
        if r.line <= len(text) and _DECLARES.search(
            _slice(text[r.line - 1], 0, r.column, r.coordinate_encoding)
        ):
            locations.add((r.file, r.line, "callable"))
    for native, locations in list(defined.items()):
        if native.endswith("`<constructor>`()."):
            owner = native[: -len("`<constructor>`().")]
            locations |= {
                (file, line, "callable") for file, line, _ in defined.get(owner, ())
            }
    expected = {}
    for r in source.references:
        if r.line != r.end_line:
            continue
        text = text_of(r.file)
        if r.line > len(text):
            continue
        line = text[r.line - 1]
        encoding = r.coordinate_encoding
        written = _slice(line, r.column, r.end_column, encoding)
        name = written if _NAME.fullmatch(written) else _descriptor_name(r.native_id)
        if not name or not _applied(_slice(line, r.end_column, None, encoding)):
            continue
        if _DECLARES.search(_slice(line, 0, r.column, encoding)):
            # A declaration the index records as a reference (scip-python
            # marks each @overload this way), not a call.
            continue
        if r.native_id in defined:
            targets = defined[r.native_id]
        elif r.native_id.startswith("local "):
            targets = bound.get((r.file, r.native_id), {("-", 0, "local")})
        else:
            targets = {("-", 0, "external")}
        expected.setdefault((r.file, r.line, name), set()).update(targets)
    return expected, indexed


_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)


def java_source_roots(workspace):
    """Directories a file's package declaration places it under."""
    roots = set()
    for path in sorted(Path(workspace).rglob("*.java")):
        if ".git" in path.parts or not path.is_file():
            continue
        match = _PACKAGE.search(path.read_text("utf-8", "replace"))
        depth = len(match.group(1).split(".")) if match else 0
        parent = path.parent.relative_to(workspace)
        if depth <= len(parent.parts):
            roots.add(Path(*parent.parts[: len(parent.parts) - depth]).as_posix())
    return sorted(roots)


def derive_java(workspace, java="java", bases=False, chains=False):
    """Expected calls (or, with ``bases``, declared supertypes; with
    ``chains``, explicit constructor invocations) from javac, attributing one
    source root at a time."""
    workspace = Path(workspace).resolve()
    roots = java_source_roots(workspace)
    oracle = Path(__file__).with_name("JavaCallTruth.java")
    expected, indexed = {}, set()
    for root in roots:
        others = [r for r in roots if r != root]
        output = subprocess.run(
            [
                java,
                "--add-exports",
                "jdk.compiler/com.sun.tools.javac.tree=ALL-UNNAMED",
                "--add-exports",
                "jdk.compiler/com.sun.tools.javac.code=ALL-UNNAMED",
                "--add-exports",
                "jdk.compiler/com.sun.tools.javac.util=ALL-UNNAMED",
                str(oracle),
                *(["--bases"] if bases else []),
                *(["--chains"] if chains else []),
                str(workspace),
                root,
                *others,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        for row in output.splitlines():
            file, line, name, target_file, target_line, kind = row.split("\t")
            expected.setdefault((file, int(line), name), set()).add(
                (target_file, int(target_line), kind)
            )
    for root in roots:
        for path in (workspace / root).rglob("*.java"):
            indexed.add(path.relative_to(workspace).as_posix())
    return expected, indexed


def _package(symbol):
    """A SCIP symbol's scheme, manager, package name and version."""
    return tuple(symbol.split(" ", 4)[:4])


def derive_inherits(workspace, index):
    """Expected direct supertypes from an index's implementation relationships.

    Returns truth in the calls format, keyed by each subtype's defining
    position and name: ``{(file, line, name): {(file, line, kind)}}`` with
    kind ``type`` (an in-repository supertype, at each defining line),
    ``external``, or ``unknown`` (a symbol of the repository's own package
    the index never defines, such as a class local to a function: the
    oracle names no line, so a link to it is not judged). Indexers differ in whether they list every ancestor or only
    direct ones, so the relation is reduced to its direct edges first: a
    supertype also reachable through another listed supertype is dropped.
    """
    from deepthought._scip import scip_pb2

    index_data = scip_pb2.Index()
    index_data.ParseFromString(Path(index).read_bytes())
    defined, supers = {}, {}
    for document in index_data.documents:
        for occurrence in document.occurrences:
            if occurrence.symbol_roles & 1 and occurrence.symbol.endswith("#"):
                defined.setdefault(occurrence.symbol, set()).add(
                    (document.relative_path, occurrence.range[0] + 1)
                )
        for info in document.symbols:
            if not info.symbol.endswith("#"):
                continue
            for relation in info.relationships:
                if relation.is_implementation and relation.symbol.endswith("#"):
                    supers.setdefault(info.symbol, set()).add(relation.symbol)
    expected = {}
    for sub, targets in supers.items():
        indirect = {
            far for near in targets for far in supers.get(near, ()) if far != near
        }
        name = _descriptor_name(sub)
        for file, line in defined.get(sub, ()):
            for target in sorted(targets - indirect):
                expected.setdefault((file, line, name), set()).update(
                    {(f, n, "type") for f, n in defined[target]}
                    if target in defined
                    else {("-", 0, "unknown")}
                    if _package(target) == _package(sub)
                    else {("-", 0, "external")}
                )
    indexed = {document.relative_path for document in index_data.documents}
    return expected, indexed


def write_truth(expected, indexed, stream):
    stream.write(HEADER + "\n")
    for file in sorted(indexed):
        stream.write(f"# indexed\t{file}\n")
    for (file, line, name), targets in sorted(expected.items()):
        for target in sorted(targets):
            stream.write("\t".join(map(str, (file, line, name, *target))) + "\n")


def read_truth(path):
    expected, indexed = {}, set()
    for row in Path(path).read_text("utf-8").splitlines():
        if row.startswith("# indexed\t"):
            indexed.add(row.split("\t", 1)[1])
        if not row or row.startswith("#"):
            continue
        file, line, name, target_file, target_line, kind = row.split("\t")
        expected.setdefault((file, int(line), name), set()).add(
            (target_file, int(target_line), kind)
        )
    return expected, indexed


def _at(definition):
    return (definition.file, definition.line) if definition else None


def score(result, truth, examples=10):
    """Judge every engine call resolution against expected calls."""
    from deepthought.calls import resolve_calls

    positions, indexed = truth
    expected = {
        k
        for k, targets in positions.items()
        if any(kind == "callable" for _, _, kind in targets)
    }
    by_id = {d.id: d for d in result.definitions}
    in_file = {}
    for d in result.definitions:
        in_file.setdefault(d.file, []).append(d)
    cache = {}

    def innermost(file, line):
        """The narrowest declaration whose span holds ``line`` of ``file``."""
        if (file, line) not in cache:
            spans = [d for d in in_file.get(file, ()) if d.line <= line <= d.end_line]
            cache[(file, line)] = min(
                spans, key=lambda d: (d.end_line - d.line, -d.line, d.id), default=None
            )
        return cache[(file, line)]

    sites = {s.id: s for s in result.call_sites}
    outcomes = Counter()
    by_rule = {}
    found = set()
    samples = {}
    # Why each expected call has no confirmed link, by engine outcome.
    near = {}
    for resolution in resolve_calls(result):
        site = sites[resolution.call_id]
        name = re.split(r"::|\.", site.callee)[-1]
        keys = [
            (site.file, line, name)
            for line in range(site.line, site.end_line + 1)
            if (site.file, line, name) in positions
        ]
        ids = [i for i in (resolution.target_id, *resolution.candidates) if i]
        ours = [(by_id[i].file, by_id[i].line) for i in ids]
        targets = {t[:2] for k in keys for t in positions[k]}
        # A declaration's line can be that of its annotations or modifiers,
        # an oracle's that of its name: one inside the other is a match.
        targets |= {(d.file, d.line) for t in targets if (d := innermost(*t))}
        if resolution.status == "linked":
            if not keys:
                outcome = "unjudged" if site.file in indexed else "not_indexed"
            elif ours[0] in targets:
                outcome = "confirmed"
                found.update(
                    k
                    for k in keys
                    if any(
                        ours[0] == t[:2] or ours[0] == _at(innermost(*t[:2]))
                        for t in positions[k]
                    )
                )
            else:
                outcome = "contradicted"
        elif resolution.status == "ambiguous":
            outcome = (
                "ambiguous_unjudged"
                if not keys or not set(keys) & expected
                else "ambiguous_no_candidates"
                if not ours
                else "ambiguous_includes"
                if targets & set(ours)
                else "ambiguous_excludes"
            )
        else:
            outcome = (
                "unresolved_expected" if set(keys) & expected else "unresolved_unjudged"
            )
        outcomes[outcome] += 1
        if resolution.status == "linked":
            by_rule.setdefault(resolution.rule_id, Counter())[outcome] += 1
        for key in set(keys) & expected:
            near.setdefault(key, f"{resolution.status}:{resolution.reason or 'linked'}")
        if outcome in {"contradicted", "ambiguous_excludes", "unjudged"}:
            samples.setdefault(outcome, []).append(
                [site.file, site.line, site.callee, ours[:3], sorted(targets)[:3]]
            )
    found &= expected
    missed = sorted(expected - found)
    why = Counter(near.get(k, "no_call_site") for k in missed)
    judged = outcomes["confirmed"] + outcomes["contradicted"]
    report = {
        "expected": len(expected),
        **{k: outcomes[k] for k in sorted({"confirmed", "contradicted", *outcomes})},
        "missed": len(missed),
        "missed_by_engine_outcome": dict(sorted(why.items())),
        "precision": round(outcomes["confirmed"] / judged, 4) if judged else None,
        "recall": round(len(found) / len(expected), 4) if expected else None,
        "by_rule": {r: dict(sorted(c.items())) for r, c in sorted(by_rule.items())},
    }
    samples["missed"] = [[*k, near.get(k, "no_call_site")] for k in missed]
    return report, {k: v[:examples] for k, v in sorted(samples.items())}


def analyze(workspace, languages, exclude=()):
    import deepthought

    return deepthought.analyze_repository(
        Path(workspace).resolve(),
        repository_id="ground-truth",
        languages=tuple(languages),
        exclude=tuple(exclude),
        max_files=100_000,
        max_total_bytes=1_000_000_000,
    )


def fetch(corpus, cache):
    """A clean checkout of the corpus's pinned commit under ``cache``."""
    target = Path(cache) / corpus["name"]
    if not (target / ".git").is_dir():
        target.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        subprocess.run(
            ["git", "-C", str(target), "remote", "add", "origin", corpus["url"]],
            check=True,
        )
    head = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "-q", "--verify", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != corpus["commit"]:
        subprocess.run(
            ["git", "-C", str(target), "fetch", "-q", "--depth", "1", "origin"]
            + [corpus["commit"]],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(target), "checkout", "-q", "--detach", "FETCH_HEAD"],
            check=True,
        )
    subprocess.run(
        ["git", "-C", str(target), "clean", "-qfdx"], check=True
    )  # Nothing but the pinned tree is analyzed.
    return target


def regressions(report, baseline):
    """Ways ``report`` is worse than ``baseline``; empty when no worse."""
    problems = []
    if report["contradicted"] > baseline["contradicted"]:
        problems.append(
            f"contradicted {baseline['contradicted']} -> {report['contradicted']}"
        )
    if report["confirmed"] < baseline["confirmed"]:
        problems.append(f"confirmed {baseline['confirmed']} -> {report['confirmed']}")
    for rule, counts in sorted(baseline.get("by_rule", {}).items()):
        now = report.get("by_rule", {}).get(rule, {})
        for outcome, worse in (("contradicted", int.__gt__), ("confirmed", int.__lt__)):
            before, after = counts.get(outcome, 0), now.get(outcome, 0)
            if worse(after, before):
                problems.append(f"{rule} {outcome} {before} -> {after}")
    if report["expected"] != baseline["expected"]:
        problems.append(
            f"expected {baseline['expected']} -> {report['expected']} "
            "(the truth file changed; update the baseline in the same commit)"
        )
    return problems


def impact_regressions(report, baseline):
    """Fewer oracle dependents reached, more wrong ones, or a changed truth."""
    problems = []
    if report["confirmed"] < baseline["confirmed"]:
        problems.append(f"confirmed {baseline['confirmed']} -> {report['confirmed']}")
    if report["extra"] > baseline["extra"]:
        problems.append(f"extra {baseline['extra']} -> {report['extra']}")
    if (report["seeds"], report["expected"]) != (
        baseline["seeds"],
        baseline["expected"],
    ):
        problems.append(
            f"seeds/expected {baseline['seeds']}/{baseline['expected']} -> "
            f"{report['seeds']}/{report['expected']} "
            "(the truth file changed; update the baseline in the same commit)"
        )
    return problems


CONTRACT_COUNTS = ("true_positive", "predicted", "labelled_in_scope")


def score_contracts(corpus, workspace):
    """Counts per link kind and level for one labelled contract corpus.

    Uses the ``all`` scope of tools/check_contracts_ground_truth.py (direct
    and gateway hops), with its false positives and negatives as examples.
    """
    import check_contracts_ground_truth

    labels = json.loads((CORPUS / corpus["labels"]).read_text("utf-8"))
    report, samples = {}, {}
    for kind in corpus["kinds"]:
        scored = check_contracts_ground_truth.score(labels, workspace, kind=kind)
        report[kind] = {
            level: {k: scored["all"][level][k] for k in CONTRACT_COUNTS}
            for level in ("call_sites", "service_pairs")
        }
        samples[kind] = {
            level: {
                "false_positive": scored["all"][level]["false_positive"],
                "false_negative": scored["all"][level]["false_negative"],
            }
            for level in ("call_sites", "service_pairs")
        }
    return report, samples


def score_routes(corpus, workspace, examples=10):
    """The routes and GraphQL root fields the engine reads, against labels
    taken from a source the code does not produce (an API spec, a committed
    generated schema).

    HTTP routes match on method and path (``{var}`` for any parameter); a
    GraphQL field is found only when a resolver ``HANDLES`` it. *extra* is
    what the engine reads that the labels do not list, apart from
    ``neutral_http`` entries (real routes beyond the spec).
    """
    import asyncio

    from deepthought.analysis_tools import ServiceContractTool

    labels = json.loads((CORPUS / corpus["labels"]).read_text("utf-8"))
    tool = ServiceContractTool(Path(workspace))
    output = asyncio.run(tool.analyze("routes"))
    _, routes, _, _, _ = tool.last_facts
    read_http = {(m, r.path) for r in routes for m in r.methods}
    fields = {
        n.properties["qualified_name"]: n.properties["name"]
        for n in output.nodes
        if "GraphqlField" in n.labels
    }
    handled = {
        fields[r.target_match["qualified_name"]]
        for r in output.relationships
        if r.type == "HANDLES" and r.target_match.get("qualified_name") in fields
    }
    report, samples = {}, {}
    for kind, expected, read, neutral in (
        (
            "http",
            {tuple(x) for x in labels.get("http", ())},
            read_http,
            {tuple(x) for x in labels.get("neutral_http", ())},
        ),
        ("graphql", set(labels.get("graphql", ())), handled, set()),
    ):
        if not expected:
            continue
        extra = read - expected - neutral
        report[kind] = {
            "expected": len(expected),
            "found": len(expected & read),
            "extra": len(extra),
        }
        samples[kind] = {
            "missed": sorted(expected - read)[:examples],
            "extra": sorted(extra)[:examples],
        }
    return report, samples


def route_regressions(report, baseline):
    """Fewer labelled routes or fields read, more unlabelled ones, or
    changed labels, per kind."""
    problems = []
    for kind, before in sorted(baseline.items()):
        after = report.get(kind)
        if after is None:
            problems.append(f"{kind}: no longer scored")
            continue
        if after["found"] < before["found"]:
            problems.append(f"{kind} found {before['found']} -> {after['found']}")
        if after["extra"] > before["extra"]:
            problems.append(f"{kind} extra {before['extra']} -> {after['extra']}")
        if after["expected"] != before["expected"]:
            problems.append(
                f"{kind} expected {before['expected']} -> {after['expected']} "
                "(the labels changed; update the baseline in the same commit)"
            )
    return problems


def score_tables(corpus, workspace, examples=10):
    """The table each ActiveRecord model maps to, against labels that
    ActiveRecord itself computed (tools/rails_table_oracle.rb).

    A model is *found* when the engine maps it to the labelled table and to
    no other, *wrong* when it maps it to any other table, and *missed* when
    it maps it to none. *extra* is a model the engine maps that the labels
    neither list nor set aside under ``not_counted``. The engine runs with its
    defaults, file limit included, on Ruby files only.
    """
    import asyncio

    from deepthought.analysis_tools.data_contracts import DataContractTool

    labels = json.loads((CORPUS / corpus["labels"]).read_text("utf-8"))
    output = asyncio.run(
        DataContractTool(Path(workspace)).analyze("tables", languages=["ruby"])
    )
    nodes = {n.properties.get("qualified_name"): n for n in output.nodes}
    mapped = {}
    for r in output.relationships:
        source = nodes.get(r.source_match.get("qualified_name"))
        target = nodes.get(r.target_match.get("qualified_name"))
        if (
            r.type != "MAPS_TO"
            or r.properties.get("member")
            or source is None
            or target is None
            or "Class" not in source.labels
            or not str(source.properties.get("file_path", "")).endswith(".rb")
        ):
            continue
        name = source.properties["name"].replace(".", "::")
        mapped.setdefault(name, set()).add(target.properties.get("name"))
    expected = {m["class"]: m["table"] for m in labels["models"]}
    ignored = {m["class"] for m in labels.get("not_counted", ())}
    found = sorted(c for c, t in expected.items() if mapped.get(c) == {t})
    wrong = sorted(c for c, t in expected.items() if mapped.get(c, {t}) - {t})
    missed = sorted(c for c in expected if c not in mapped)
    extra = sorted(c for c in mapped if c not in expected and c not in ignored)
    report = {
        "expected": len(expected),
        "found": len(found),
        "wrong": len(wrong),
        "missed": len(missed),
        "extra": len(extra),
    }
    samples = {
        "wrong": [[c, expected[c], sorted(mapped[c])] for c in wrong][:examples],
        "missed": [[c, expected[c]] for c in missed][:examples],
        "extra": [[c, sorted(mapped[c])] for c in extra][:examples],
    }
    return report, samples


def table_regressions(report, before):
    """Fewer models found, more wrong or extra ones, or changed labels."""
    problems = [
        f"{metric} {before[metric]} -> {report[metric]}"
        for metric, higher in (("found", True), ("wrong", False), ("extra", False))
        if (
            report[metric] < before[metric]
            if higher
            else report[metric] > before[metric]
        )
    ]
    if report["expected"] != before["expected"]:
        problems.append(
            f"expected {before['expected']} -> {report['expected']} (the labels "
            "changed; update the baseline in the same commit)"
        )
    return problems


def contract_regressions(report, baseline):
    """Fewer correct links, more wrong ones, or changed labels, per kind."""
    problems = []
    for kind, levels in sorted(baseline.items()):
        for level, before in sorted(levels.items()):
            after = report.get(kind, {}).get(level)
            if after is None:
                problems.append(f"{kind} {level}: no longer scored")
                continue
            wrong = (
                before["predicted"] - before["true_positive"],
                after["predicted"] - after["true_positive"],
            )
            if after["true_positive"] < before["true_positive"]:
                problems.append(
                    f"{kind} {level} correct "
                    f"{before['true_positive']} -> {after['true_positive']}"
                )
            if wrong[1] > wrong[0]:
                problems.append(f"{kind} {level} wrong {wrong[0]} -> {wrong[1]}")
            if after["labelled_in_scope"] != before["labelled_in_scope"]:
                problems.append(
                    f"{kind} {level} labelled {before['labelled_in_scope']} -> "
                    f"{after['labelled_in_scope']} (the labels changed; update "
                    "the baseline in the same commit)"
                )
    return problems


# What the contract scorer needs beyond the grammars: gateway routes in YAML.
CONTRACT_MODULES = ("yaml",)
CONTRACT_LANGUAGES = ("python", "typescript", "javascript", "go", "java")


def missing_requirements(manifest, names=()):
    """Modules the selected corpora need that are not installed.

    A missing grammar or YAML reader does not fail analysis: files are
    reported unavailable and gateway routes unread, so scores drop as if the
    engine had regressed. ``check`` refuses to score instead.
    """
    import importlib.util

    from deepthought.syntax import LANGUAGES

    def selected(corpus):
        return not names or corpus["name"] in names

    modules = set()
    for corpus in manifest["corpora"] + manifest.get("declarations", []):
        if selected(corpus):
            modules.update(LANGUAGES[lang][1] for lang in corpus["languages"])
    if any(
        selected(c)
        for c in [*manifest.get("contracts", ()), *manifest.get("routes", ())]
    ):
        modules.update(CONTRACT_MODULES)
        modules.update(LANGUAGES[lang][1] for lang in CONTRACT_LANGUAGES)
    if any(selected(c) for c in manifest.get("tables", ())):
        modules.add(LANGUAGES["ruby"][1])
    return sorted(m for m in modules if importlib.util.find_spec(m) is None)


def declaration_regressions(report, baseline):
    """Fewer declarations found, more misread ones, or a changed truth."""
    problems = []
    for key, worse in (
        ("found", int.__lt__),
        ("kind_mismatch", int.__gt__),
        ("extra", int.__gt__),
    ):
        if worse(report[key], baseline[key]):
            problems.append(f"{key} {baseline[key]} -> {report[key]}")
    if report["expected"] != baseline["expected"]:
        problems.append(
            f"expected {baseline['expected']} -> {report['expected']} "
            "(the truth file changed; update the baseline in the same commit)"
        )
    return problems


def check(names, cache, update=False, examples=10):
    manifest = json.loads((CORPUS / "manifest.json").read_text("utf-8"))
    missing = missing_requirements(manifest, names)
    if missing:
        raise RuntimeError(
            "not installed: "
            + ", ".join(missing)
            + " (install the package with its `languages` extra); scores "
            "without them would read as regressions"
        )
    baseline_path = CORPUS / "baseline.json"
    baseline = json.loads(baseline_path.read_text("utf-8"))
    reports, failed = {}, False
    for corpus in manifest["corpora"]:
        if names and corpus["name"] not in names:
            continue
        workspace = fetch(corpus, cache)
        expected = read_truth(CORPUS / corpus["truth"])
        result = analyze(workspace, corpus["languages"], corpus.get("exclude", ()))
        report, samples = score(result, expected, examples)
        problems = (
            regressions(report, baseline[corpus["name"]])
            if (corpus["name"] in baseline and not update)
            else []
        )
        if "inherits" in corpus:
            inherits, inherit_samples = score_inherits(
                result, read_truth(CORPUS / corpus["inherits"]), examples
            )
            report["inherits"] = inherits
            samples["inherits"] = inherit_samples
            if not update and "inherits" in baseline.get(corpus["name"], {}):
                problems += [
                    "inherits: " + p
                    for p in regressions(inherits, baseline[corpus["name"]]["inherits"])
                ]
        if "imports" in corpus:
            imported, import_samples = score_imports(
                result,
                read_truth(CORPUS / corpus["imports"]),
                corpus.get("imports_granularity") == "directory",
                examples,
            )
            report["imports"] = imported
            samples["imports"] = import_samples
            if not update and "imports" in baseline.get(corpus["name"], {}):
                problems += [
                    "imports: " + p
                    for p in regressions(imported, baseline[corpus["name"]]["imports"])
                ]
        if "impact" in corpus:
            import impact_truth

            path = CORPUS / corpus["impact"]
            impact, impact_samples = impact_truth.score(
                result,
                impact_truth.read(path),
                examples=examples,
                aliases=impact_truth.read_aliases(path),
            )
            report["impact"] = impact
            samples["impact"] = impact_samples
            if not update and "impact" in baseline.get(corpus["name"], {}):
                problems += [
                    "impact: " + p
                    for p in impact_regressions(
                        impact, baseline[corpus["name"]]["impact"]
                    )
                ]
        failed |= bool(problems)
        reports[corpus["name"]] = {
            "commit": corpus["commit"],
            **report,
            "regressions": problems,
            "examples": samples,
        }
        if update:
            baseline[corpus["name"]] = report
    declared = baseline.setdefault("declarations", {})
    for corpus in manifest.get("declarations", ()):
        if names and corpus["name"] not in names:
            continue
        import c_declaration_truth

        workspace = fetch(corpus, cache)
        result = analyze(workspace, corpus["languages"])
        report, samples = c_declaration_truth.score(
            result, c_declaration_truth.read(CORPUS / corpus["truth"]), examples
        )
        problems = (
            declaration_regressions(report, declared[corpus["name"]])
            if (corpus["name"] in declared and not update)
            else []
        )
        failed |= bool(problems)
        reports[corpus["name"]] = {
            "commit": corpus["commit"],
            "declarations": report,
            "regressions": problems,
            "examples": samples,
        }
        if update:
            declared[corpus["name"]] = report
    contracts = baseline.setdefault("contracts", {})
    for corpus in manifest.get("contracts", ()):
        if names and corpus["name"] not in names:
            continue
        report, samples = score_contracts(corpus, fetch(corpus, cache))
        problems = (
            contract_regressions(report, contracts[corpus["name"]])
            if (corpus["name"] in contracts and not update)
            else []
        )
        failed |= bool(problems)
        reports[corpus["name"]] = {
            "commit": corpus["commit"],
            "contracts": report,
            "regressions": problems,
            "examples": samples,
        }
        if update:
            contracts[corpus["name"]] = report
    routed = baseline.setdefault("routes", {})
    for corpus in manifest.get("routes", ()):
        if names and corpus["name"] not in names:
            continue
        report, samples = score_routes(corpus, fetch(corpus, cache), examples)
        problems = (
            route_regressions(report, routed[corpus["name"]])
            if (corpus["name"] in routed and not update)
            else []
        )
        failed |= bool(problems)
        reports[corpus["name"]] = {
            "commit": corpus["commit"],
            "routes": report,
            "regressions": problems,
            "examples": samples,
        }
        if update:
            routed[corpus["name"]] = report
    tabled = baseline.setdefault("tables", {})
    for corpus in manifest.get("tables", ()):
        if names and corpus["name"] not in names:
            continue
        report, samples = score_tables(corpus, fetch(corpus, cache), examples)
        problems = (
            table_regressions(report, tabled[corpus["name"]])
            if (corpus["name"] in tabled and not update)
            else []
        )
        failed |= bool(problems)
        reports[corpus["name"]] = {
            "commit": corpus["commit"],
            "tables": report,
            "regressions": problems,
            "examples": samples,
        }
        if update:
            tabled[corpus["name"]] = report
    if update:
        baseline_path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
    return reports, failed


# Lines that can hold an import's module: a module named elsewhere (a
# Python `click.echo(...)`) is a use, not an import.
_IMPORT_LINE = {
    "python": re.compile(r"^\s*(?:from|import)\s"),
    "typescript": re.compile(r"\b(?:from|import|require)\b"),
    "javascript": re.compile(r"\b(?:from|import|require)\b"),
    "go": re.compile(r"^\s*(?:import\s+)?(?:[\w.]+\s+)?\"[^\"]*\"\s*(?://.*)?$"),
}
_RELATIVE_IMPORT = re.compile(r"^\s*from\s+(\.+)([\w.]*)\s+import\b")


def _without_version(symbol):
    """A SCIP symbol without backticks or its package version: an indexer can
    name one package with two versions (scip-python: the installed version
    for some references, the commit for definitions)."""
    parts = symbol.replace("`", "").split(" ", 4)
    if len(parts) == 5:
        del parts[3]
    return " ".join(parts)


def derive_imports(workspace, index, language, directory=False):
    """Expected import targets from an index's module references.

    On each line holding a reference to a module or package symbol, the
    rightmost such reference (the innermost module of a dotted path) names
    what the import statement imports: the files that define that symbol
    (their directories when ``directory``, as for Go packages), or
    ``external`` when the repository defines none. scip-python names a
    relative import's module without its package, so for Python the module
    is rebuilt from the leading dots and the importing module's own name.

    Returns calls-format truth keyed by ``(file, line, "import")``.
    """
    from deepthought._scip import scip_pb2

    workspace = Path(workspace)
    index_data = scip_pb2.Index()
    index_data.ParseFromString(Path(index).read_bytes())
    defined, own = {}, {}
    for document in index_data.documents:
        for occurrence in document.occurrences:
            if occurrence.symbol_roles & 1:
                symbol = _without_version(occurrence.symbol)
                defined.setdefault(symbol, set()).add(document.relative_path)
                if symbol.endswith("/__init__:"):
                    own.setdefault(document.relative_path, symbol)

    def module(symbol):
        return not symbol.startswith("local ") and symbol.endswith(("/", ":"))

    def absolute(symbol, file, text):
        match = _RELATIVE_IMPORT.match(text)
        if not match or file not in own:
            return symbol
        head, tail = own[file].rsplit(" ", 1)
        package = tail[: -len("/__init__:")].split(".")
        if not file.endswith("__init__.py"):
            package = package[:-1]
        package = package[: len(package) - (len(match.group(1)) - 1)]
        name = ".".join(package + ([match.group(2)] if match.group(2) else []))
        return f"{head} {name}/__init__:"

    statement = _IMPORT_LINE[language]
    expected = {}
    for document in index_data.documents:
        file = document.relative_path
        if file.startswith("../"):
            continue  # Generated outside the repository (scip-go's build cache).
        rightmost = {}
        for occurrence in document.occurrences:
            if occurrence.symbol_roles & 1 or not module(occurrence.symbol):
                continue
            line = occurrence.range[0] + 1
            column = occurrence.range[1]
            if line not in rightmost or column > rightmost[line][0]:
                rightmost[line] = (column, occurrence.symbol)
        if not rightmost:
            continue
        try:
            text = (workspace / file).read_text("utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            text = []
        for line, (_, symbol) in rightmost.items():
            if line > len(text) or not statement.search(text[line - 1]):
                continue  # A module named in code, not in an import.
            symbol = _without_version(symbol)
            if language == "python" and line <= len(text):
                symbol = absolute(symbol, file, text[line - 1])
            targets = defined.get(symbol, set())
            if directory:
                targets = {Path(t).parent.as_posix() for t in targets}
            expected[(file, line, "import")] = (
                {(t, 0, "module") for t in targets}
                if targets
                else {("-", 0, "external")}
            )
    indexed = {
        document.relative_path
        for document in index_data.documents
        if not document.relative_path.startswith("../")
    }
    return expected, indexed


def score_imports(result, truth, directory=False, examples=10):
    """Import resolutions judged against module-reference truth.

    An import statement is judged by the truth lines it spans. It is
    *confirmed* when it links to an expected target, *contradicted* when it
    links only elsewhere while the truth names a repository target,
    *linked_external* when it links but the oracle says the module is
    outside the repository. Recall is over truth lines with a repository
    target: found when an import spanning the line links to one of them.
    """
    from deepthought.imports import resolve_imports

    expected, indexed = truth
    imports = {i.id: i for i in result.imports}
    counts = Counter()
    found = set()
    samples = {"contradicted": [], "linked_external": [], "missed": []}
    for resolution in resolve_imports(result):
        item = imports[resolution.import_id]
        if item.file not in indexed:
            counts["not_indexed"] += 1
            continue
        keys = [
            (item.file, n, "import")
            for n in range(item.line, item.end_line + 1)
            if (item.file, n, "import") in expected
        ]
        if not keys:
            counts[f"{resolution.status}_unjudged"] += 1
            continue
        want = {t[0] for key in keys for t in expected[key] if t[2] == "module"}
        got = {link.target_file for link in resolution.links}
        if directory:
            got = {Path(t).parent.as_posix() for t in got}
        if resolution.status != "linked":
            counts[f"{resolution.status}_{'expected' if want else 'external'}"] += 1
        elif not want:
            counts["linked_external"] += 1
            samples["linked_external"].append([item.file, item.line, item.specifier])
        elif got & want:
            counts["confirmed"] += 1
            found.update(k for k in keys if {t[0] for t in expected[k]} & got)
        else:
            counts["contradicted"] += 1
            samples["contradicted"].append(
                [item.file, item.line, item.specifier, sorted(got), sorted(want)]
            )
    repository = {
        key
        for key, targets in expected.items()
        if any(t[2] == "module" for t in targets)
    }
    samples["missed"] = [list(k[:2]) for k in sorted(repository - found)]
    for name in samples:
        samples[name] = samples[name][:examples]
    judged = counts["confirmed"] + counts["contradicted"] + counts["linked_external"]
    report = {
        "expected": len(repository),
        "confirmed": counts["confirmed"],
        "contradicted": counts["contradicted"] + counts["linked_external"],
        **{
            k: v
            for k, v in sorted(counts.items())
            if k not in {"confirmed", "contradicted"}
        },
        "precision": round(counts["confirmed"] / judged, 4) if judged else None,
        "recall": round(len(found) / len(repository), 4) if repository else None,
    }
    return report, samples


def score_inherits(result, truth, examples=10):
    """Judge every INHERITS_FROM link (declared bases; not Rust impls).

    A link is judged when its subtype is a subtype in the truth: confirmed
    when its target is one of the truth's supertypes, contradicted
    otherwise. Recall is the share of in-repository truth edges the links
    reach.
    """
    from deepthought.inheritance import resolve_supertypes

    positions, _ = truth
    by_id = {d.id: d for d in result.definitions}
    in_file = {}
    for d in result.definitions:
        in_file.setdefault(d.file, []).append(d)

    def innermost(file, line, name=None):
        """The narrowest declaration spanning the line, preferring ``name``."""
        spans = [d for d in in_file.get(file, ()) if d.line <= line <= d.end_line]
        named = [d for d in spans if d.symbol.split(".")[-1] == name]
        return min(
            named or spans,
            key=lambda d: (d.end_line - d.line, -d.line, d.id),
            default=None,
        )

    def matches(definition, file, line):
        return (definition.file, definition.line) == (file, line) or (
            innermost(file, line) is definition
        )

    subtype_key = {}
    for key in positions:
        owner = innermost(*key)
        if owner is not None:
            subtype_key[owner.id] = key
    expected = {
        (key, target[:2])
        for key, targets in positions.items()
        for target in targets
        if target[2] == "type"
    }
    bases = {b.id: b for b in result.supertypes}
    outcomes, found, samples = Counter(), set(), {}
    for resolution in resolve_supertypes(result):
        if resolution.status != "linked":
            outcomes[f"{resolution.status}"] += 1
            continue
        item = bases[resolution.call_id]
        key = subtype_key.get(item.owner_id)
        target = by_id[resolution.target_id]
        hits = [
            t
            for t in positions.get(key, ())
            if t[2] == "type" and matches(target, *t[:2])
        ]
        if key is None or (not hits and any(t[2] == "unknown" for t in positions[key])):
            outcome = "unjudged"
        elif hits:
            outcome = "confirmed"
            found.update((key, t[:2]) for t in hits)
        else:
            outcome = "contradicted"
            samples.setdefault(outcome, []).append(
                [item.file, item.line, item.name, [target.file, target.line]]
            )
        outcomes[outcome] += 1
    # An indexer may omit an intermediate edge (scip-typescript records no
    # interface `extends`), leaving an ancestor that looks direct: it counts
    # as found when the engine's own links reach it.
    links = {}
    for resolution in resolve_supertypes(result):
        if resolution.status == "linked":
            owner = bases[resolution.call_id].owner_id
            links.setdefault(owner, set()).add(resolution.target_id)
    owners = {key: owner for owner, key in subtype_key.items()}
    for key, target in expected - found:
        reached, frontier = set(), [owners.get(key)]
        while frontier:
            for nxt in links.get(frontier.pop(), ()):
                if nxt not in reached:
                    reached.add(nxt)
                    frontier.append(nxt)
        if any(matches(by_id[d], *target) for d in reached):
            found.add((key, target))
    judged = outcomes["confirmed"] + outcomes["contradicted"]
    missed = sorted(expected - found)
    samples["missed"] = [[*k, list(t)] for k, t in missed]
    report = {
        "expected": len(expected),
        **{k: outcomes[k] for k in sorted({"confirmed", "contradicted", *outcomes})},
        "missed": len(missed),
        "precision": round(outcomes["confirmed"] / judged, 4) if judged else None,
        "recall": round(len(found) / len(expected), 4) if expected else None,
    }
    return report, {k: v[:examples] for k, v in sorted(samples.items())}


def call_rules():
    """The evidence rules call resolution can name."""
    from deepthought import calls
    from deepthought.analysis_tools.evidence import registered_rules

    rules = registered_rules()
    return sorted(
        {
            value
            for name, value in vars(calls).items()
            if name.isupper() and isinstance(value, str) and value in rules
        }
        # Imported for use, or rules of impls, not of calls.
        - {
            calls.PYTHON_FROM_IMPORT_SUBMODULE,
            calls.RUST_IMPL_TYPE,
            calls.RUST_IMPL_TRAIT,
        }
    )


def rule_coverage(reports):
    """Confirmed and contradicted links per call rule across all corpora.

    A rule with no confirmed link is not verified by the corpus: breaking it
    would go unnoticed here.
    """
    totals = {rule: Counter() for rule in call_rules()}
    for report in reports.values():
        for rule, counts in report.get("by_rule", {}).items():
            totals.setdefault(rule, Counter()).update(counts)
    return {
        "rules": {
            rule: {k: c.get(k, 0) for k in ("confirmed", "contradicted")}
            for rule, c in sorted(totals.items())
        },
        "unverified": sorted(r for r, c in totals.items() if not c.get("confirmed")),
    }


# Per layer, the counts a PR summary shows: (metric, higher is better).
SUMMARY_METRICS = {
    "calls": (("confirmed", True), ("contradicted", False), ("recall", True)),
    "inherits": (("confirmed", True), ("contradicted", False)),
    "imports": (("confirmed", True), ("contradicted", False)),
    "impact": (
        ("confirmed", True),
        ("extra", False),
        ("recall", True),
        ("precision", True),
    ),
    "declarations": (("found", True), ("kind_mismatch", False), ("extra", False)),
    "contracts": (("true_positive", True), ("false_positive", False)),
    "routes": (("found", True), ("extra", False), ("expected", True)),
    "tables": (("found", True), ("wrong", False), ("extra", False)),
}


def _contract_totals(report):
    """Call-site links summed over link kinds: (true positives, false ones)."""
    true = predicted = 0
    for levels in report.values():
        counts = levels.get("call_sites", {})
        true += counts.get("true_positive", 0)
        predicted += counts.get("predicted", 0)
    return {"true_positive": true, "false_positive": predicted - true}


def summary(reports, baseline):
    """A Markdown table of every layer's scores against the baseline.

    ``check`` writes it to ``$GITHUB_STEP_SUMMARY`` when that is set, so each
    PR's run shows how call, impact and other scores moved, not only whether
    the gate passed. A changed value is shown with its difference in bold.
    """

    def layers(name, report, base):
        if "declarations" in report:
            yield (
                "declarations",
                report["declarations"],
                base.get("declarations", {}).get(name),
            )
        elif "tables" in report:
            yield "tables", report["tables"], base.get("tables", {}).get(name)
        elif "routes" in report:
            before = base.get("routes", {}).get(name, {})
            for kind, counts in sorted(report["routes"].items()):
                yield f"routes ({kind})", counts, before.get(kind)
        elif "contracts" in report:
            before = base.get("contracts", {}).get(name)
            yield (
                "contracts",
                _contract_totals(report["contracts"]),
                _contract_totals(before) if before else None,
            )
        else:
            yield "calls", report, base.get(name)
            for layer in ("inherits", "imports", "impact"):
                if layer in report:
                    yield layer, report[layer], base.get(name, {}).get(layer)

    def cell(now, before):
        if now is None:
            return "–"
        if before is None or now == before:
            return f"{now}"
        delta = round(now - before, 4)
        return f"{now} (**{delta:+}**)"

    lines = [
        "### Ground truth against the baseline",
        "",
        "| Corpus | Layer | Scores |",
        "|---|---|---|",
    ]
    for name, report in reports.items():
        for layer, now, before in layers(name, report, baseline):
            values = ", ".join(
                f"{metric} {cell(now.get(metric), (before or {}).get(metric))}"
                for metric, _ in SUMMARY_METRICS[layer.split(" ")[0]]
            )
            lines.append(f"| {name} | {layer} | {values} |")
        if report.get("regressions"):
            lines.append(
                f"| {name} | **regressions** | "
                + "; ".join(report["regressions"])
                + " |"
            )
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("derive")
    d.add_argument("workspace", type=Path)
    d.add_argument("index", type=Path)
    i = sub.add_parser("derive-inherits")
    i.add_argument("workspace", type=Path)
    i.add_argument("index", type=Path)
    m = sub.add_parser("derive-imports")
    m.add_argument("workspace", type=Path)
    m.add_argument("index", type=Path)
    m.add_argument("--language", required=True)
    m.add_argument("--directory", action="store_true", help="targets are packages")
    j = sub.add_parser("derive-java")
    j.add_argument("workspace", type=Path)
    j.add_argument("--java", default="java")
    j.add_argument("--bases", action="store_true")
    j.add_argument(
        "--chains", action="store_true", help="this(...) and super(...) only"
    )
    s = sub.add_parser("score")
    s.add_argument("workspace", type=Path)
    s.add_argument("truth", type=Path)
    s.add_argument("--language", action="append", required=True)
    s.add_argument("--exclude", action="append", default=[])
    c = sub.add_parser("check")
    c.add_argument("--corpus", action="append", default=[])
    c.add_argument("--cache", type=Path, default=Path(".ground-truth-cache"))
    c.add_argument("--update-baseline", action="store_true")
    for p in (s, c):
        p.add_argument("--examples", type=int, default=10)
    args = parser.parse_args(argv)
    if args.command == "derive":
        write_truth(*derive(args.workspace, args.index), sys.stdout)
        return 0
    if args.command == "derive-inherits":
        write_truth(*derive_inherits(args.workspace, args.index), sys.stdout)
        return 0
    if args.command == "derive-imports":
        write_truth(
            *derive_imports(args.workspace, args.index, args.language, args.directory),
            sys.stdout,
        )
        return 0
    if args.command == "derive-java":
        write_truth(
            *derive_java(args.workspace, args.java, args.bases, args.chains),
            sys.stdout,
        )
        return 0
    if args.command == "score":
        report, samples = score(
            analyze(args.workspace, args.language, args.exclude),
            read_truth(args.truth),
            args.examples,
        )
        print(json.dumps({**report, "examples": samples}, indent=2))
        return 0
    baseline = json.loads((CORPUS / "baseline.json").read_text("utf-8"))
    try:
        reports, failed = check(
            set(args.corpus), args.cache, args.update_baseline, args.examples
        )
    except RuntimeError as error:
        print(f"ground_truth check: {error}", file=sys.stderr)
        return 2
    coverage = rule_coverage(reports)
    print(json.dumps({"corpora": reports, "rule_coverage": coverage}, indent=2))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as out:
            out.write(summary(reports, baseline))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
