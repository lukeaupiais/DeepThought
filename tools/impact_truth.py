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
_TERM = re.compile(r"[/#]`?([A-Za-z_$][\w$]*)`?\.$")
# After the name: `= function`, `= (...) =>`, `= <T>(...) =>` or `= x =>`,
# optionally typed and async.
_FUNCTION_VALUE = re.compile(
    r"\s*(?::[^=]*)?=\s*(?:async\s+)?"
    r"(?:function\b|(?:<(?:[^<>]|<[^<>]*>)*>\s*)?\(|[A-Za-z_$][\w$]*\s*=>)"
)
_CLOSE = {"(": ")", "[": "]", "{": "}", "<": ">"}


def _skip_balanced(text, i):
    """The index after the bracket group opening at ``text[i]``; an arrow's
    ``=>`` is not a closing ``>``. len(text) when it does not close."""
    stack = []
    while i < len(text):
        c = text[i]
        if c == ">" and text[i - 1 : i] == "=":
            pass
        elif c in _CLOSE:
            stack.append(_CLOSE[c])
        elif stack and c == stack[-1]:
            stack.pop()
            if not stack:
                return i + 1
        i += 1
    return len(text)


def holds_function(after):
    """Whether the text after a variable's name assigns it a function:
    ``= function``, ``= (...) =>``, ``= x =>`` or ``= <T, ...>(...) =>``,
    optionally typed and async; a type annotation or type parameters may
    nest and span lines."""
    if _FUNCTION_VALUE.match(after):
        return True
    i = len(after) - len(after.lstrip())
    if after[i : i + 1] == ":":
        # Skip the annotation to its top-level `=` (not an arrow's `=>`).
        i += 1
        while i < len(after):
            c = after[i]
            if c in _CLOSE:
                i = _skip_balanced(after, i)
                continue
            if c == "=" and after[i + 1 : i + 2] != ">":
                break
            i += 1
    rest = after[i:]
    if not rest.startswith("="):
        return False
    rest = rest[1:].lstrip()
    if rest.startswith("async"):
        rest = rest[5:].lstrip()
    if rest.startswith("<"):
        rest = rest[_skip_balanced(rest, 0) :].lstrip()
        return rest.startswith("(")
    return bool(_FUNCTION_VALUE.match(" = " + rest))


def _code_end(text, i, stop):
    """The index of the first character in ``stop`` at bracket depth 0 from
    ``text[i]``, skipping strings, template literals and comments (``<`` is
    not a bracket here: it is a comparison as often as a type argument).
    len(text) when none."""
    depth, quote = 0, None
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
        elif text.startswith("//", i):
            i = text.find("\n", i)
            if i < 0:
                return len(text)
            continue
        elif text.startswith("/*", i):
            i = text.find("*/", i)
            if i < 0:
                return len(text)
            i += 2
            continue
        elif depth == 0 and c in stop:
            return i
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth < 0:
                return i
        i += 1
    return len(text)


def _type_end(text, i, arrow=False):
    """From ``text[i]``, past a type annotation (whose ``<...>``, brackets
    and function types' ``=>`` nest), the index of the first top-level
    ``=`` (with ``arrow``, of a ``=>``), ``;``, ``,``, ``{`` or line end;
    len(text) when none. A line end inside brackets does not count."""
    while i < len(text):
        c = text[i]
        if c in "<([":
            i = _skip_balanced(text, i)
            continue
        if text.startswith("=>", i):
            if arrow:
                return i
            i += 2
            continue
        if c in "=;,{\n":
            if c == "\n" and arrow:
                i += 1  # a return type may continue on the next line
                continue
            return i
        i += 1
    return len(text)


def initializer_lines(text):
    """How many lines past the first the function value after a variable's
    or class field's name runs (``text`` starts just after the name), for
    an initializer the index gives no enclosing range: scip-typescript
    records none for a class field holding an arrow function
    (``fetch = async (...) => { ... }``). None when it is not found."""
    # The value's `=` must come before the declaration ends (a `;`, `,` or
    # line end outside brackets): an interface member or a field without an
    # initializer has none. Types nest `<...>` (no comparisons there).
    i = _type_end(text, 0)
    if i >= len(text) or text[i] != "=" or text[i + 1 : i + 2] == ">":
        return None
    i += 1
    head = re.match(r"\s*(?:async\b\s*)?", text[i:])
    i += head.end()
    if re.match(r"function\b", text[i:]):
        end = _code_end(text, _code_end(text, i, "{") + 1, "}")
        return text.count("\n", 0, min(end, len(text)))
    if text[i : i + 1] == "<":
        i = _skip_balanced(text, i)
        i += len(text[i:]) - len(text[i:].lstrip())
    if text[i : i + 1] == "(":
        i = _code_end(text, i + 1, ")") + 1
    elif match := re.match(r"[A-Za-z_$][\w$]*", text[i:]):
        i += match.end()
    else:
        return None
    # An optional return type, then the arrow itself: `(() => x)()` and
    # `f(...)` are not function values.
    j = _type_end(text, i, arrow=True)
    if text[j : j + 2] != "=>" or (
        text[i:j].strip() and not text[i:j].strip().startswith(":")
    ):
        return None
    body = j + 2 + len(text[j + 2 :]) - len(text[j + 2 :].lstrip())
    if text[body : body + 1] == "{":
        end = _code_end(text, body + 1, "}")
    else:
        end = _code_end(text, body, ";\n")
    return text.count("\n", 0, min(end, len(text)))


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
            value = None
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
                # The initializer can start on a later line (`const f =`
                # then the arrow, or type parameters split across lines).
                after = "\n".join(
                    [after.decode("utf-8", "replace"), *texts[file][line : line + 60]]
                )
                if not holds_function(after):
                    continue
                value = "\n".join(
                    [after.split("\n", 1)[0], *texts[file][line : line + 400]]
                )
            if not occurrence.symbol_roles & 1:
                overloads.append((document.relative_path, line, occurrence, symbol))
                continue
            if len(occurrence.enclosing_range):
                span = list(occurrence.enclosing_range)
                first = span[0] + 1
                last = (span[2] if len(span) == 4 else span[0]) + 1
            elif spans and (document.relative_path, line) in spans:
                first, last = spans[(document.relative_path, line)]
            elif value is not None and (more := initializer_lines(value)) is not None:
                first, last = line, line + more
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


def call_graph(nodes, calls, aliases=None, name_line_inside=False, regions=()):
    """{callee: {callers}} over oracle nodes, from call-truth positions.

    ``name_line_inside``: a call on a callable's name line belongs to it
    (Go, Rust, Java, TypeScript: a one-line body such as
    ``func (ns nodes) Sort() { sort.Sort(ns); ns.tailSort() }``). In Python
    that line holds decorators and default values, which run where the
    function is declared, so it belongs to the enclosing callable.

    ``regions``: (file, owner line, first, last) code outside any callable
    that runs as part of an owner, such as a Java field initializer, which
    runs when its class is constructed. A call there belongs to the owner,
    unless a callable inside the region (an anonymous class's method) holds
    it. Owners are callers only, never targets or seeds."""
    by_file = defaultdict(list)
    for (file, line), (_, first, last) in nodes.items():
        by_file[file].append((last - first, line, first, last, False))
    for file, owner, first, last in regions:
        by_file[file].append((last - first, owner, first, last, True))
    for spans in by_file.values():
        spans.sort()

    def caller(file, line):
        for _, name_line, first, last, region in by_file.get(file, ()):
            if region:
                if first <= line <= last:
                    return (file, name_line)
                continue
            # Decorators (above the name line, though scip-python's range
            # starts at the first one) and the definition line itself run
            # where the declaration is, not in its body.
            if name_line < line <= last or (name_line_inside and line == name_line):
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


def enclosing_regions(regions):
    """{region: {the region around it}} over (file, owner line, first, last)
    regions, keyed as call_graph keys them.

    A callback inside another (an act(() => ...) in an it(...), an it(...)
    in a describe(...)) is passed to a call the oracle does not see run, so
    whether the enclosing one runs it is not ruled on: derive adds these
    edges to the widened graph only, and a dependent reached only through
    one is unjudged."""
    edges = defaultdict(set)
    for file, owner, first, last in regions:
        around = [
            (b - a, line)
            for f, line, a, b in regions
            if f == file and a <= first and last <= b and (a, b) != (first, last)
        ]
        if around and min(around)[1] != owner:
            edges[(file, owner)].add((file, min(around)[1]))
    return dict(edges)


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


def read_initializers(path):
    """{(file, owner line, first, last): owner name} from tools/
    JavaMethodSpans.java --initializers."""
    regions = {}
    for text in Path(path).read_text("utf-8").splitlines():
        if text and not text.startswith("#"):
            file, line, first, last, name = text.split("\t")
            regions[(file, int(line), int(first), int(last))] = name
    return regions


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
    possible_calls=(),
    initializers=None,
    dispatch=None,
    nested_regions=False,
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
    # Java field initializers and initializer blocks: their calls belong to
    # the class (tools/JavaMethodSpans.java --initializers).
    owners = {(f, ln): name for (f, ln, _, _), name in (initializers or {}).items()}
    regions = list(initializers or {})
    names = {key: value[0] for key, value in nodes.items()} | owners
    incoming = call_graph(nodes, positions, aliases, language != "python", regions)
    # Calls the oracle cannot rule on (javac's error recovery), bound to every
    # target they could take: a dependent reached only through one of them
    # is neither expected nor wrong.
    possible = {site: set(targets) for site, targets in positions.items()}
    for path in possible_calls:
        for site, targets in read_truth(path)[0].items():
            possible.setdefault(site, set()).update(targets)
    # A call of an interface method can run any implementation in the corpus
    # (tools/go_interface_dispatch.go): the oracle binds it to the interface,
    # so which one runs is not ruled on either.
    for site, targets in positions.items():
        for target_file, target_line, _ in targets:
            for impl in (dispatch or {}).get((target_file, target_line), ()):
                possible.setdefault(site, set()).add((*impl, "callable"))
    widened = (
        call_graph(nodes, possible, aliases, language != "python", regions)
        if possible_calls or dispatch or nested_regions
        else incoming
    )
    if nested_regions:
        for region, parents in enclosing_regions(regions).items():
            widened.setdefault(region, set()).update(parents)
    rows, unjudged = [], []
    for seed in seeds(nodes, repo, language, revision, commits, aliases):
        reached = reach(incoming, seed, max_depth)
        for (file, line), depth in sorted(reached.items()):
            rows.append((*seed, nodes[seed][0], file, line, names[(file, line)], depth))
        for node in sorted(set(reach(widened, seed, max_depth)) - set(reached)):
            unjudged.append((*seed, *node))
    return rows, aliases, nodes, unjudged


def write(rows, stream, aliases=None, nodes=None, unjudged=()):
    print(HEADER, file=stream)
    # Dependents of a seed reached only through a call the oracle cannot
    # rule on: an engine dependent here is unjudged, not extra.
    for seed_file, seed_line, file, line in sorted(unjudged):
        print(f"# unjudged\t{seed_file}\t{seed_line}\t{file}\t{line}", file=stream)
    # Every oracle callable, so the scorer can tell a declaration the index
    # does not name (no node can judge it) from one the truth just does not
    # list.
    for (file, line), (name, _, _) in sorted((nodes or {}).items()):
        print(f"# node\t{file}\t{line}\t{name}", file=stream)
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


def read_dispatch(path):
    """{(interface file, line): {(file, line)}} from go_interface_dispatch.go."""
    dispatch = {}
    for row in Path(path).read_text("utf-8").splitlines():
        if row:
            file, line, impl_file, impl_line = row.split("\t")
            dispatch.setdefault((file, int(line)), set()).add(
                (impl_file, int(impl_line))
            )
    return dispatch


def read_unjudged(path):
    """{(seed_file, seed_line, file, line)} from ``# unjudged`` rows."""
    found = set()
    for text in Path(path).read_text("utf-8").splitlines():
        if text.startswith("# unjudged\t"):
            _, sf, sl, f, ln = text.split("\t")
            found.add((sf, int(sl), f, int(ln)))
    return found


def read_nodes(path):
    """{(file, line): name} from a truth file's ``# node`` rows, or None."""
    nodes = {}
    for text in Path(path).read_text("utf-8").splitlines():
        if text.startswith("# node\t"):
            _, file, line, name = text.split("\t")
            nodes[(file, int(line))] = name
    return nodes or None


def read(path):
    """{(seed_file, seed_line, seed_name): {(file, line, name): depth}}."""
    truth = defaultdict(dict)
    for text in Path(path).read_text("utf-8").splitlines():
        if text and not text.startswith("#"):
            sf, sl, sn, f, ln, n, d = text.split("\t")
            truth[(sf, int(sl), sn)][(f, int(ln), n)] = int(d)
    return dict(truth)


def score(
    result,
    truth,
    max_depth=3,
    examples=10,
    aliases=None,
    fold_nested=False,
    nodes=None,
    unjudged=None,
    indexed=None,
    resolutions=None,
):
    """Engine dependents of each seed against the oracle's.

    ``resolutions``: the call resolutions to follow (by default the
    parser's, ``resolve_calls``), such as calls bound from SCIP indexes.

    *confirmed*: an engine dependent the oracle also reaches; *extra*: one
    it does not (a wrong link, or a caller reached through one); *missed*:
    an oracle dependent the engine does not reach. Seeds the engine does not
    declare are counted, and their dependents are all missed.

    ``fold_nested``: the index makes a function declared inside another
    one a local symbol (scip-typescript), so the oracle has no node for it
    and counts its calls as the enclosing callable's. An engine declaration
    that matches no oracle node and lies inside another engine declaration
    is then read as the innermost one that encloses it, when that one is a
    node of the truth.

    ``nodes``: every oracle callable (the truth's ``# node`` rows). An engine
    dependent that is none of them, such as a function scip-typescript makes
    local inside an anonymous callback, cannot be judged: it is *unjudged*,
    not extra, and the search still goes through it.

    ``unjudged``: (seed file, seed line, file, line) pairs the truth's
    ``# unjudged`` rows list, dependents reached only through a call the
    oracle cannot rule on. An engine dependent among them is unjudged too.

    ``indexed``: the files the call oracle indexed. An engine dependent in
    any other file (a Go file behind a build tag the index was not built
    with) is one the oracle never saw: unjudged, not extra.
    """
    from deepthought.calls import resolve_calls

    known = {}
    known.update(nodes or {})
    for (sf, sl, sn), dependents in truth.items():
        known[(sf, sl)] = sn
        for f, ln, n in dependents:
            known[(f, ln)] = n
    named = defaultdict(list)
    for (file, line), name in known.items():
        named[(file, name)].append(line)

    from deepthought.syntax import TEST_KINDS

    def name_of(d):
        if d.kind in TEST_KINDS and (d.file, d.line) in known:
            # A test case's name is its call as written (`it("a.b")`,
            # `it.each(rows)("x")`), dots and modifier arguments included;
            # the oracle keys the callback by the call's line, as the
            # engine's declaration starts there.
            return known[(d.file, d.line)]
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
    if fold_nested:
        # Outermost first, so an enclosing declaration is already read.
        order = sorted(definitions.values(), key=lambda d: (d.line - d.end_line, d.id))
        by_file = defaultdict(list)
        for d in order:
            node = node_of[d.id]
            if node[:2] not in known:
                enclosing = [
                    e
                    for e in by_file[d.file]
                    if e.line <= d.line
                    and d.end_line <= e.end_line
                    and (e.line, e.end_line) != (d.line, d.end_line)
                ]
                if enclosing:
                    inner = min(enclosing, key=lambda e: (e.end_line - e.line, -e.line))
                    # Only into a node of the truth: a declaration the index
                    # does name (a method) is never merged into another.
                    if node_of[inner.id][:2] in known:
                        node_of[d.id] = node_of[inner.id]
            by_file[d.file].append(d)
    sites = {s.id: s for s in result.call_sites}
    incoming = defaultdict(set)
    for r in resolve_calls(result) if resolutions is None else resolutions:
        site = sites[r.call_id]
        if r.status == "linked" and site.caller_id in node_of:
            source, target = node_of[site.caller_id], node_of[r.target_id]
            if source != target:
                incoming[target].add(source)
    engine_nodes = set(node_of.values())
    # An unjudged row names the oracle's line for a callable (its name
    # line); an engine node the truth does not otherwise list keeps its
    # declaration's first line (an annotation), so match on the span.
    spans = defaultdict(list)
    for i, d in definitions.items():
        spans[node_of[i]].append((d.line, d.end_line))
    unjudged_lines = defaultdict(set)
    for sf, sl, f, ln in unjudged or ():
        unjudged_lines[(sf, sl, f)].add(ln)

    def is_unjudged(seed, node):
        lines = unjudged_lines.get((*seed[:2], node[0]), ())
        return node[1] in lines or any(
            start <= ln <= end for start, end in spans.get(node, ()) for ln in lines
        )

    totals = {"seeds": len(truth), "seeds_undeclared": 0}
    counts = {"expected": 0, "confirmed": 0, "missed": 0, "extra": 0, "unjudged": 0}
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
            if node not in expected and (
                (nodes and node[:2] not in nodes)
                or is_unjudged(seed, node)
                or (indexed is not None and node[0] not in indexed)
            ):
                counts["unjudged"] += 1
            elif node not in expected:
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
    derive_cmd.add_argument(
        "--possible-calls",
        type=Path,
        action="append",
        default=[],
        help="edges the oracle cannot rule on (derive-java --recovered): "
        "dependents reached only through them are listed as unjudged",
    )
    derive_cmd.add_argument(
        "--initializers",
        type=Path,
        help="code outside callables that runs as part of a class "
        "(JavaMethodSpans.java --initializers): its calls belong to the class; "
        "or anonymous callbacks (ts_callback_regions.cjs), callers of their own",
    )
    derive_cmd.add_argument(
        "--dispatch",
        type=Path,
        help="interface methods and their implementations "
        "(go_interface_dispatch.go): dependents reached only through a call "
        "of an interface method are listed as unjudged",
    )
    args = parser.parse_args(argv)
    # scip-typescript gives a function inside another a local symbol, so
    # the truth lists every node to tell those apart from unlisted ones.
    scripts = args.language in {"typescript", "javascript"}
    initializers = read_initializers(args.initializers) if args.initializers else None
    if scripts and initializers:
        # Anonymous callbacks (tools/ts_callback_regions.cjs) in a file the
        # call truth covers: elsewhere no call of theirs is known.
        from ground_truth import read_truth

        indexed = read_truth(args.calls)[1]
        initializers = {k: v for k, v in initializers.items() if k[0] in indexed}
    rows, aliases, nodes, unjudged = derive(
        None if args.index == "-" else Path(args.index),
        args.calls,
        args.history,
        args.language,
        args.revision,
        args.commits,
        args.max_depth,
        read_spans(args.spans) if args.spans else None,
        args.extra_calls,
        args.possible_calls,
        initializers,
        read_dispatch(args.dispatch) if args.dispatch else None,
        scripts,
    )
    if scripts and initializers:
        # The callbacks are callers the truth can name, so an engine
        # declaration there (a test case) is judged.
        nodes = {
            **{(f, ln): (name, a, b) for (f, ln, a, b), name in initializers.items()},
            **nodes,
        }
    write(rows, sys.stdout, aliases, nodes if scripts else None, unjudged)
    return 0


if __name__ == "__main__":
    sys.exit(main())
