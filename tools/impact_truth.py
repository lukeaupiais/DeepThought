"""Impact ground truth: the callers an edit reaches, by the oracle's call graph.

Historical replay (tools/replay_history.py) asks whether impact analysis
anticipates the files a commit co-edits, which mixes the engine's accuracy
with whether developers had to touch a dependent at all. This asks only the
first question: for declarations real commits edited, which declarations call
them, directly or transitively, according to the oracle?

``derive`` takes the corpus's call truth (tools/ground_truth.py ``calls``
rows, from the SCIP index) and the index's definition spans. A call is an
edge from the innermost oracle callable whose body holds the call line to
each callable the oracle says it targets. Seeds come from the last
``--commits`` qualifying commits up to the pinned revision (the replay's
selection): the innermost callables whose span holds a line that the
commit wrote and that is unchanged at the pin (``git blame`` at the pin
names that commit), so the line still means what the commit made it. Each seed's dependents are found by a
reverse breadth-first search up to ``--max-depth``.

``score`` runs the same search over the engine's linked CALLS edges
(resolve_calls) and judges each engine dependent against the seed's oracle
dependents. An engine declaration is matched to the oracle callable of the
same name whose definition line its span holds.

Rows (tab-separated): seed_file, seed_line, seed_name, file, line, name,
depth. A depth 0 row lists the seed itself, so seeds with no callers are
kept.

Usage:
  python tools/impact_truth.py derive INDEX CALLS_TSV HISTORY \\
      --language L --revision PIN [--commits 30] [--max-depth 3] \\
      [--spans SPANS_TSV] > impact.tsv

For Go, scip-go records no definition spans; pass the output of
``go run tools/go_function_spans.go WORKSPACE`` as ``--spans``. For Java,
whose call truth comes from javac, there is no index: pass ``-`` as INDEX and
the output of ``java tools/JavaMethodSpans.java WORKSPACE`` as ``--spans``.
"""

import argparse
from collections import defaultdict, deque
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

HEADER = "# seed_file\tseed_line\tseed_name\tfile\tline\tname\tdepth"
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)
# The last descriptor of a callable symbol: `name().` or `` `name`(). ``.
_NAME = re.compile(r"`?([^`./#()\[\]:]+)`?\([^)]*\)\.$")
_TERM = re.compile(r"/`?([A-Za-z_$][\w$]*)`?\.$")
# After the name: `= function`, `= (...) =>`, `= <T>(...) =>` or `= x =>`,
# optionally typed and async.
_FUNCTION_VALUE = re.compile(
    r"\s*(?::[^=]*)?=\s*(?:async\s+)?"
    r"(?:function\b|(?:<[^>]*>\s*)?\(|[A-Za-z_$][\w$]*\s*=>)"
)
_SCRIPTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
# Names the engine writes differently: TypeScript constructors.
_RENAMED = {"<constructor>": "constructor"}


def _python_block(lines, line):
    """The last line of the block a ``def`` on ``line`` (1-based) opens: past
    the signature (to its closing brackets), then the lines indented deeper
    than the ``def``."""
    indent = len(lines[line - 1]) - len(lines[line - 1].lstrip())
    depth, header = 0, line
    for number in range(line, len(lines) + 1):
        code = lines[number - 1].split("#", 1)[0]
        depth += sum(code.count(c) for c in "([{") - sum(code.count(c) for c in ")]}")
        header = number
        if depth <= 0 and code.rstrip().endswith(":"):
            break
    last = header
    for number in range(header + 1, len(lines) + 1):
        text = lines[number - 1]
        if not text.strip() or text.lstrip().startswith("#"):
            continue
        if len(text) - len(text.lstrip()) <= indent:
            break
        last = number
    return last


_PY_DEF = re.compile(r"^\s*(?:async\s+)?def\s+$")


def callables(index, source=None, spans=None):
    """``({(file, line): (name, first_line, last_line)}, {alias: node})``
    for every callable the index defines with an enclosing range (1-based).

    scip-python gives only the first of a function's ``@overload``
    declarations a definition with a range; the others, implementation
    included, are references on a ``def`` line. With ``source`` (a function
    from a file to its text), each such ``def`` becomes an alias of the
    first: one node whose spans are all of them, the block found by
    indentation. ``spans`` (``{(file, name_line): (first, last)}``, from
    the language's own parser) supplies spans the index does not record.
    """
    from deepthought._scip import scip_pb2

    data = scip_pb2.Index()
    data.ParseFromString(Path(index).read_bytes())
    found, by_symbol, overloads, texts = {}, {}, [], {}
    for document in data.documents:
        if document.relative_path.startswith("../"):
            continue  # outside the repository (scip-go's build cache)
        for occurrence in document.occurrences:
            symbol = occurrence.symbol
            if symbol.startswith("local "):
                continue
            # Scheme, manager, package and version come before the descriptors.
            descriptors = symbol.split(" ", 4)[-1]
            line = occurrence.range[0] + 1
            match = _NAME.search(descriptors)
            if not match:
                # A variable holding a function (`const f = () => ...`).
                match = _TERM.search(descriptors)
                if not (
                    match
                    and occurrence.symbol_roles & 1
                    and source
                    and document.relative_path.endswith(_SCRIPTS)
                ):
                    continue
                file = document.relative_path
                if file not in texts:
                    texts[file] = source(file).splitlines()
                after = texts[file][line - 1].encode("utf-8")[occurrence.range[2] :]
                if not _FUNCTION_VALUE.match(after.decode("utf-8", "replace")):
                    continue
            if not occurrence.symbol_roles & 1:
                overloads.append((document.relative_path, line, occurrence, symbol))
                continue
            if len(occurrence.enclosing_range):
                span = list(occurrence.enclosing_range)
                first = span[0] + 1
                last = (span[2] if len(span) == 4 else span[0]) + 1
            elif spans and (document.relative_path, line) in spans:
                first, last = spans[(document.relative_path, line)]
            else:
                continue
            name = _RENAMED.get(match.group(1), match.group(1))
            found[(document.relative_path, line)] = (name, first, last)
            by_symbol.setdefault(symbol, (document.relative_path, line))
    aliases = {}
    for file, line, occurrence, symbol in overloads:
        node = by_symbol.get(symbol)
        if source is None or node is None or node[0] != file:
            continue
        if file not in texts:
            texts[file] = source(file).splitlines()
        text = texts[file][line - 1]
        if not _PY_DEF.match(text[: occurrence.range[1]]):
            continue
        aliases[(file, line)] = node
        found.setdefault(
            (file, line), (found[node][0], line, _python_block(texts[file], line))
        )
    return found, aliases


def call_graph(nodes, calls, aliases=None):
    """{callee: {callers}} over oracle nodes, from call-truth positions."""
    by_file = defaultdict(list)
    for (file, line), (_, first, last) in nodes.items():
        by_file[file].append((last - first, line, first, last))
    for spans in by_file.values():
        spans.sort()

    def caller(file, line):
        for _, name_line, first, last in by_file.get(file, ()):
            # Decorators (above the name line, though scip-python's range
            # starts at the first one) and the definition line itself run
            # where the declaration is, not in its body.
            if name_line < line <= last:
                return (aliases or {}).get((file, name_line), (file, name_line))
        return None

    incoming = defaultdict(set)
    for (file, line, _), targets in calls.items():
        source = caller(file, line)
        if source is None:
            continue
        for target_file, target_line, kind in targets:
            target = (target_file, target_line)
            # A call of a variable holding a function targets a value.
            if kind in {"callable", "value"} and target in nodes and target != source:
                incoming[target].add(source)
    return incoming


def reach(incoming, seed, max_depth):
    """{dependent: depth} by reverse breadth-first search from ``seed``."""
    depth = {seed: 0}
    queue = deque([seed])
    while queue:
        node = queue.popleft()
        if depth[node] == max_depth:
            continue
        for source in sorted(incoming.get(node, ())):
            if source not in depth:
                depth[source] = depth[node] + 1
                queue.append(source)
    return depth


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def blamed_lines(repo, revision, file):
    """{commit: {lines of ``file`` at ``revision`` that commit last changed}}."""
    lines = defaultdict(set)
    porcelain = git(repo, "blame", "--porcelain", revision, "--", file)
    for header in re.finditer(r"^([0-9a-f]{40}) \d+ (\d+)", porcelain, re.M):
        lines[header.group(1)].add(int(header.group(2)))
    return lines


def seeds(nodes, repo, language, revision, commits, aliases=None):
    """Innermost oracle callables holding a line that one of the replay's
    commits wrote and that survives unchanged at ``revision`` (by blame)."""
    from replay_history import candidates

    by_file = defaultdict(list)
    for (file, line), (_, first, last) in nodes.items():
        by_file[file].append((first, last, line))
    blame = {}
    chosen = set()
    for commit, edited in candidates(repo, language, commits, 12, revision):
        for file in edited:
            if file not in by_file:
                continue
            if file not in blame:
                blame[file] = blamed_lines(repo, revision, file)
            lines = blame[file].get(commit, ())
            touched = [
                s for s in by_file[file] if any(s[0] <= n <= s[1] for n in lines)
            ]
            for first, last, line in touched:
                if not any(
                    o != (first, last, line) and first <= o[0] and o[1] <= last
                    for o in touched
                ):
                    chosen.add((aliases or {}).get((file, line), (file, line)))
    return sorted(chosen)


def read_spans(path):
    """Function spans from a language's own parser, as ``callables`` takes
    them: ``{(file, name_line): (first, last)}``, or with a fifth (name)
    column ``{(file, name_line): (name, first, last)}`` (tools/
    go_function_spans.go writes four columns, tools/JavaMethodSpans.java
    five)."""
    spans = {}
    for text in Path(path).read_text("utf-8").splitlines():
        if text and not text.startswith("#"):
            file, line, first, last, *name = text.split("\t")
            value = (int(first), int(last))
            spans[(file, int(line))] = (name[0], *value) if name else value
    return spans


def derive(
    index,
    calls,
    repo,
    language,
    revision,
    commits=30,
    max_depth=3,
    spans=None,
    extra_calls=(),
):
    from ground_truth import read_truth

    if index is None:
        # No index (javac truth): the callables are the parser's spans.
        nodes, aliases = dict(spans), {}
    else:
        nodes, aliases = callables(
            index, lambda file: git(repo, "show", f"{revision}:{file}"), spans
        )
    positions, _ = read_truth(calls)
    # Edges that are not call sites, such as Java's this(...) and super(...).
    for path in extra_calls:
        for site, targets in read_truth(path)[0].items():
            positions.setdefault(site, set()).update(targets)
    incoming = call_graph(nodes, positions, aliases)
    rows = []
    for seed in seeds(nodes, repo, language, revision, commits, aliases):
        for (file, line), depth in sorted(reach(incoming, seed, max_depth).items()):
            rows.append(
                (*seed, nodes[seed][0], file, line, nodes[(file, line)][0], depth)
            )
    return rows, aliases


def write(rows, stream, aliases=None):
    print(HEADER, file=stream)
    # A def the index gives another's symbol: an overload, or a redefinition
    # of the same name in one scope. The scorer counts the engine's
    # declaration there as the node it is folded into.
    for (file, line), (_, canonical) in sorted((aliases or {}).items()):
        print(f"# alias\t{file}\t{line}\t{canonical}", file=stream)
    for seed_file, seed_line, seed_name, file, line, name, depth in rows:
        print(
            f"{seed_file}\t{seed_line}\t{seed_name}\t{file}\t{line}\t{name}\t{depth}",
            file=stream,
        )


def read_aliases(path):
    """{(file, line): canonical line} from a truth file's ``# alias`` rows."""
    aliases = {}
    for text in Path(path).read_text("utf-8").splitlines():
        if text.startswith("# alias\t"):
            _, file, line, canonical = text.split("\t")
            aliases[(file, int(line))] = int(canonical)
    return aliases


def read(path):
    """{(seed_file, seed_line, seed_name): {(file, line, name): depth}}."""
    truth = defaultdict(dict)
    for text in Path(path).read_text("utf-8").splitlines():
        if text and not text.startswith("#"):
            sf, sl, sn, f, ln, n, d = text.split("\t")
            truth[(sf, int(sl), sn)][(f, int(ln), n)] = int(d)
    return dict(truth)


def score(result, truth, max_depth=3, examples=10, aliases=None):
    """Engine dependents of each seed against the oracle's.

    *confirmed*: an engine dependent the oracle also reaches; *extra*: one
    it does not (a wrong link, or a caller reached through one); *missed*:
    an oracle dependent the engine does not reach. Seeds the engine does not
    declare are counted, and their dependents are all missed.
    """
    from deepthought.calls import resolve_calls

    known = {}
    for (sf, sl, sn), dependents in truth.items():
        known[(sf, sl)] = sn
        for f, ln, n in dependents:
            known[(f, ln)] = n
    named = defaultdict(list)
    for (file, line), name in known.items():
        named[(file, name)].append(line)

    def name_of(d):
        # TypeScript private methods: the index names `#m` as `m`.
        return re.split(r"::|\.", d.symbol)[-1].lstrip("#")

    # Oracle nodes some engine declaration spans. The truth file lists only
    # seeds and their dependents, so a same-name node may be another
    # declaration entirely (a method and a function both named `M`); a node
    # already spanned by its own declaration is never a fold target.
    spanned = {
        (d.file, n)
        for d in result.definitions
        for n in named.get((d.file, name_of(d)), ())
        if d.line <= n <= d.end_line
    }

    def oracle_node(d):
        name = name_of(d)
        candidates = named.get((d.file, name), ())
        lines = [n for n in candidates if d.line <= n <= d.end_line]
        folded = [
            canonical
            for (file, line), canonical in (aliases or {}).items()
            if file == d.file
            and d.line <= line <= d.end_line
            and canonical in candidates
        ]
        if not lines and folded:
            lines = folded
        if (
            not lines
            and len(candidates) == 1
            and (d.file, candidates[0]) not in spanned
        ):
            # The one oracle node of that name in the file: an overload
            # the oracle folds into its first declaration.
            lines = candidates
        return (d.file, min(lines), name) if lines else (d.file, d.line, name)

    definitions = {d.id: d for d in result.definitions}
    node_of = {i: oracle_node(d) for i, d in definitions.items()}
    sites = {s.id: s for s in result.call_sites}
    incoming = defaultdict(set)
    for r in resolve_calls(result):
        site = sites[r.call_id]
        if r.status == "linked" and site.caller_id in node_of:
            source, target = node_of[site.caller_id], node_of[r.target_id]
            if source != target:
                incoming[target].add(source)
    engine_nodes = set(node_of.values())
    totals = {"seeds": len(truth), "seeds_undeclared": 0}
    counts = {"expected": 0, "confirmed": 0, "missed": 0, "extra": 0}
    by_depth = defaultdict(lambda: {"expected": 0, "confirmed": 0})
    samples = {"missed": [], "extra": [], "seeds_undeclared": []}
    for seed in sorted(truth):
        expected = {k: v for k, v in truth[seed].items() if v > 0}
        if seed not in engine_nodes:
            totals["seeds_undeclared"] += 1
            samples["seeds_undeclared"].append(list(seed))
            reached = {}
        else:
            reached = {k: v for k, v in reach(incoming, seed, max_depth).items() if v}
        counts["expected"] += len(expected)
        for node, depth in expected.items():
            by_depth[depth]["expected"] += 1
            if node in reached:
                counts["confirmed"] += 1
                by_depth[depth]["confirmed"] += 1
            else:
                counts["missed"] += 1
                samples["missed"].append([*seed[:2], *node[:2], node[2], depth])
        for node, depth in sorted(reached.items()):
            if node not in expected:
                counts["extra"] += 1
                samples["extra"].append([*seed[:2], *node[:2], node[2], depth])
    reported = counts["confirmed"] + counts["extra"]
    report = {
        **totals,
        **counts,
        "recall": round(counts["confirmed"] / counts["expected"], 4)
        if counts["expected"]
        else None,
        "precision": round(counts["confirmed"] / reported, 4) if reported else None,
        "by_depth": {str(k): v for k, v in sorted(by_depth.items())},
    }
    return report, {k: v[:examples] for k, v in samples.items()}


def main(argv=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    derive_cmd = commands.add_parser("derive")
    derive_cmd.add_argument("index", help="SCIP index, or - with named --spans")
    derive_cmd.add_argument("calls", type=Path)
    derive_cmd.add_argument("history", type=Path)
    derive_cmd.add_argument("--language", required=True)
    derive_cmd.add_argument("--revision", required=True)
    derive_cmd.add_argument("--commits", type=int, default=30)
    derive_cmd.add_argument("--max-depth", type=int, default=3)
    derive_cmd.add_argument(
        "--spans", type=Path, help="function spans for an index without them"
    )
    derive_cmd.add_argument(
        "--extra-calls",
        type=Path,
        action="append",
        default=[],
        help="more edges in the call-truth format (Java constructor chains)",
    )
    args = parser.parse_args(argv)
    rows, aliases = derive(
        None if args.index == "-" else Path(args.index),
        args.calls,
        args.history,
        args.language,
        args.revision,
        args.commits,
        args.max_depth,
        read_spans(args.spans) if args.spans else None,
        args.extra_calls,
    )
    write(rows, sys.stdout, aliases)
    return 0


if __name__ == "__main__":
    sys.exit(main())
