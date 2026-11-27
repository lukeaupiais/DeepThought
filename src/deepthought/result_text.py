"""Write a read-only Cypher result as short sentences instead of JSON.

The renderer knows the four kinds of value Neo4j returns, never the query:

- a code item (node) reads ``Name (file:line)``, with ``test`` added for test
  files; a file reads ``path (file)``, which as a caller means code outside
  any declaration, such as a test file's anonymous callbacks;
- a link (relationship) reads as a sentence built from its type's verb in
  ``RELATIONSHIP_SENTENCES``: ``A (a.go:10) calls B (b.go:4) at line 12``;
- a chain (path) reads ``A (a.go:10) → B → C`` with each link's lines;
- a plain value reads ``label value`` under the column name the query chose.

Rows are then shortened without losing facts: what every row shares is said
once at the top, repeating leading values become headings, rows that differ
only in a line number are merged ("at lines 195, 209"), and rows in test files
follow the others under their own heading. The reply opens with how many
results there are and whether the limit cut them, and closes with how
completely the engine found each relationship family the result touches.
"""

from collections import OrderedDict
import json
import re

from .indexers import LANGUAGE_SUFFIXES
from .source_roles import source_roles
from .storage.graph.schema import RELATIONSHIP_SENTENCES


def _is_node(value):
    return isinstance(value, dict) and "labels" in value and "properties" in value


def _is_link(value):
    return (
        isinstance(value, dict)
        and "type" in value
        and "source" in value
        and "target" in value
    )


def _is_path(value):
    return isinstance(value, dict) and set(value) == {"nodes", "relationships"}


def _path_of(node):
    p = node["properties"]
    return p.get("file_path") or p.get("path")


def _is_test(node):
    return "test" in source_roles(_path_of(node) or "")


def _name(node):
    p = node["properties"]
    labels = node["labels"]
    if "File" in labels:
        return p.get("path") or p.get("file_path") or p.get("name")
    if "UnresolvedCall" in labels:
        return "the unlinked call " + str(p.get("callee") or p.get("name"))
    for key in ("display_name", "name", "qualified_name"):
        if p.get(key) not in (None, ""):
            return str(p[key])
    return "/".join(sorted(labels))


_PLAIN_KINDS = {"Function", "Method"}


def _item(node, short=False):
    """One code item. ``short`` drops the location (used inside chains)."""
    p = node["properties"]
    labels = node["labels"]
    if "EdgeCoverage" in labels:
        return _coverage_phrase(p)
    name = _name(node)
    if short:
        return name
    if "ExternalPackage" in labels:
        # A dependency outside the repository: no file, a version.
        kind = " ".join(str(x) for x in (p.get("ecosystem"), "package") if x)
        version = p.get("version")
        return f"{name} {version} ({kind})" if version else f"{name} ({kind})"
    path = _path_of(node)
    if "File" in labels:
        details = ["file"]
    else:
        line = p.get("start_line", p.get("line"))
        details = [f"{path}:{line}" if path and line is not None else path]
        kind = p.get("kind")
        if isinstance(kind, str) and kind not in _PLAIN_KINDS:
            # Functions and methods ("Owner.member") need no kind word.
            details.insert(0, re.sub(r"(?<=[a-z])(?=[A-Z])", " ", kind).lower())
    if "UnresolvedCall" in labels:
        details.append(
            ", ".join(
                str(x)
                for x in (p.get("status"), p.get("reason"))
                if x not in (None, "")
            )
        )
    if _is_test(node):
        details.append("test")
    if p.get("generated_by"):
        # Generated code is changed through its source, not by hand.
        details.append("generated")
    details = [d for d in details if d]
    return f"{name} ({', '.join(details)})" if details else name


def _unlinked(records):
    """ " (unlinked sites: reason n, ...)" summed over EdgeCoverage records:
    sites no resolver decided. Calls into libraries are external, not here."""
    total = {}
    for record in records:
        reasons = record.get("unresolved_reasons")
        if isinstance(reasons, str):
            try:
                reasons = json.loads(reasons)
            except ValueError:
                reasons = None
        if isinstance(reasons, dict):
            for reason, count in reasons.items():
                if isinstance(count, int) and count:
                    total[reason] = total.get(reason, 0) + count
    if not total:
        return ""
    return (
        " (unlinked sites: "
        + ", ".join(f"{reason} {count}" for reason, count in sorted(total.items()))
        + ")"
    )


def _coverage_phrase(p):
    family = " ".join(str(x) for x in (p.get("family"), p.get("protocol")) if x)
    if p.get("status") == "linked":
        state = "complete, compiler-checked"
    else:
        state = "may be incomplete" + (f" ({p['reason']})" if p.get("reason") else "")
    state += _unlinked([p])
    return f"{family} in {p.get('language')}: {state}"


class _Num:
    """Numbers that rows may share a sentence over: "at line 5" / "at lines 5, 9"."""

    def __init__(self, one, many, numbers):
        self.one, self.many, self.numbers = one, many, list(numbers)

    def text(self):
        nums = sorted(set(self.numbers))
        word = self.one if len(nums) == 1 else self.many
        return f"{word} {', '.join(str(n) for n in nums)}".strip()


def _number(value):
    return isinstance(value, int) and not isinstance(value, bool)


class _Renderer:
    def __init__(self, items, nodes):
        self.items = items
        self.nodes = dict(nodes)
        for row in items:
            for value in row.values():
                self._collect(value)

    def _collect(self, value):
        if _is_node(value):
            self.nodes.setdefault(value["element_id"], value)
        elif _is_path(value):
            for node in value["nodes"]:
                self._collect(node)
        elif isinstance(value, list):
            for v in value:
                self._collect(v)
        elif isinstance(value, dict) and not _is_link(value):
            for v in value.values():
                self._collect(v)

    sources = {}

    def block(self, text):
        """A multi-line value; a declaration's source is numbered by line."""
        start = self.sources.get(text.rstrip("\n"))
        return _numbered(text, start) if start is not None else text.rstrip("\n")

    def endpoint(self, element_id, short=False):
        node = self.nodes.get(element_id)
        return _item(node, short) if node else "(item not returned)"

    # -- parts of one row -------------------------------------------------
    def link_parts(self, link, drop=None):
        """A sentence for one link; ``drop`` leaves out a side said above."""
        verb = RELATIONSHIP_SENTENCES.get(link["type"], link["type"].lower())
        props = link.get("properties") or {}
        source = self.endpoint(link["source"])
        target = self.endpoint(link["target"])
        if drop == "target":
            text = source
        elif drop == "source":
            text = target
        else:
            text = f"{source} {verb} {target}"
        evidence = props.get("evidence_type")
        if evidence and evidence != "resolved":
            text += f" [{evidence.replace('_', ' ')}]"
        parts = [text]
        line = props.get("line")
        if _number(line) and link["type"] != "HAS_UNRESOLVED_CALL":
            parts.append(_Num("at line", "at lines", [line]))
        return parts

    def path_text(self, path, drop=None):
        """A chain as one sentence: "A (a.go:3) calls B at line 4, which calls
        C at line 9". ``drop`` "end" or "start" names that node "it", because
        a line above says which node every chain ends or starts at."""
        nodes, links = path["nodes"], path["relationships"]
        if not nodes:
            return "(empty chain)"
        names = [_name(n) for n in nodes]
        seen = set()

        def step(i):
            if drop == "end" and i == len(nodes) - 1 and i > 0:
                return "it"
            if drop == "start" and i == 0:
                return "it"
            ident = nodes[i]["element_id"]
            if ident in seen:
                return names[i]
            seen.add(ident)
            # First mention gives the location: later line numbers in the
            # chain are in this item's file.
            return _item(nodes[i])

        parts = [step(0)]
        for hop, link in enumerate(links):
            verb = RELATIONSHIP_SENTENCES.get(link["type"], link["type"].lower())
            # A chain may come back to an item, so the direction is read
            # from this hop's own node, not from the item's first position.
            forward = link["source"] == nodes[hop]["element_id"]
            line = (link.get("properties") or {}).get("line")
            if forward:
                phrase = f"{verb} {step(hop + 1)}"
            else:
                phrase = f"is what {step(hop + 1)} {verb}"
            parts[-1] += (" " if hop == 0 else ", which ") + phrase
            if _number(line):
                # The first hop's line stays a number so rows that differ
                # only there merge ("calls it at lines 1839, 1844").
                if hop == 0:
                    parts.append(_Num("at line", "at lines", [line]))
                    parts.append("")
                else:
                    parts[-1] += f" at line {line}"
        return parts

    def value_text(self, value):
        if value is None or value == [] or value == "":
            return None
        if _is_node(value):
            return _item(value)
        if _is_link(value):
            parts = self.link_parts(value)
            return " ".join(p if isinstance(p, str) else p.text() for p in parts)
        if _is_path(value):
            return _join(self.path_text(value))
        if isinstance(value, list):
            texts = [self.value_text(v) for v in value]
            texts = [t for t in texts if t is not None]
            if all(_number(v) for v in value if v is not None):
                texts = [str(n) for n in sorted(set(v for v in value if v is not None))]
            return ", ".join(texts)
        if isinstance(value, dict):
            inner = [
                f"{k} {t}"
                for k, v in value.items()
                if (t := self.value_text(v)) is not None
            ]
            return "{" + "; ".join(inner) + "}"
        if isinstance(value, bool):
            return "yes" if value else "no"
        return str(value)

    def row_parts(self, row, columns, drops):
        parts, blocks = [], []
        # Items a link in the same row already names are not repeated.
        ends = {
            end
            for value in row.values()
            if _is_link(value)
            for end in (value["source"], value["target"])
        }
        for column in columns:
            value = row.get(column)
            if _is_node(value) and value["element_id"] in ends:
                continue
            if _is_link(value):
                parts.extend(self.link_parts(value, drops.get(column)))
            elif _number(value) or (
                isinstance(value, list) and value and all(_number(v) for v in value)
            ):
                numbers = value if isinstance(value, list) else [value]
                parts.append(_Num(column, column, numbers))
            else:
                text = self.value_text(value)
                if text is None:
                    continue
                if _is_path(value):
                    parts.extend(
                        p for p in self.path_text(value, drops.get(column)) if p != ""
                    )
                elif _is_node(value):
                    parts.append(text)
                elif isinstance(value, str) and "\n" in value:
                    # Source text and other multi-line values follow the
                    # row's other values as an indented block.
                    blocks.append(f"{column}:\n" + self.block(value))
                else:
                    parts.append(f"{column} {text}")
        return parts + blocks

    def row_is_test(self, row, columns):
        """A row is a test row when its first code item is in a test file."""
        for column in columns:
            value = row.get(column)
            if _is_node(value):
                return _is_test(value)
            if _is_link(value):
                node = self.nodes.get(value["source"])
                return bool(node) and _is_test(node)
            if _is_path(value) and value["nodes"]:
                return _is_test(value["nodes"][0])
        return False


def _merge(rows):
    """Merge rows identical except in one number part; pick the part that
    removes the most rows. Only numbers are merged, never names or files."""
    best = rows
    width = max((len(r) for r in rows), default=0)
    for index in range(width):
        if not all(len(r) == width and isinstance(r[index], _Num) for r in rows):
            continue
        merged = OrderedDict()
        for parts in rows:
            key = tuple(
                p if isinstance(p, str) else (p.one, tuple(sorted(set(p.numbers))))
                for i, p in enumerate(parts)
                if i != index
            )
            if key in merged:
                merged[key][index].numbers.extend(parts[index].numbers)
            else:
                merged[key] = [
                    _Num(p.one, p.many, p.numbers) if isinstance(p, _Num) else p
                    for p in parts
                ]
        if len(merged) < len(best):
            best = list(merged.values())
    return best


def _join(parts):
    out = ""
    for part in parts:
        text = part if isinstance(part, str) else part.text()
        if not text:
            continue
        if not out:
            out = text
        elif text.startswith(", which "):
            out += text
        elif isinstance(part, _Num) and text.startswith("at "):
            out += " " + text
        else:
            out += "; " + text
    return out


def _languages(items, nodes):
    found = set()

    def visit(value):
        if isinstance(value, str):
            for suffix, language in LANGUAGE_SUFFIXES.items():
                if value.endswith(suffix):
                    found.add(language)
        elif _is_node(value):
            language = value["properties"].get("language")
            if isinstance(language, str):
                found.add(language)
            visit(_path_of(value))
        elif isinstance(value, dict):
            for v in value.values():
                visit(v)
        elif isinstance(value, list):
            for v in value:
                visit(v)

    for row in items:
        visit(row)
    for node in nodes.values():
        visit(node)
    return found


def coverage_line(items, nodes, coverage, query):
    """How complete each relationship family the result or query touches is,
    for the languages in the result. None when nothing applies."""
    if not coverage:
        return None
    families = {c.get("family") for c in coverage}
    touched = set()

    def visit(value):
        if _is_link(value):
            touched.add(value["type"])
        elif _is_path(value):
            for link in value["relationships"]:
                touched.add(link["type"])
        elif isinstance(value, list):
            for v in value:
                visit(v)
        elif isinstance(value, dict) and not _is_node(value):
            for v in value.values():
                visit(v)

    for row in items:
        visit(row)
    upper = (query or "").upper()
    for family in families:
        if family and f":{family}" in upper.replace(" ", ""):
            touched.add(family)
        if family and f"|{family}" in upper.replace(" ", ""):
            touched.add(family)
    if (query or "") and "EDGECOVERAGE" in upper:
        return None
    languages = _languages(items, nodes)
    phrases = []
    for family in sorted(touched & families):
        # Every analyzed language where the family may be incomplete is
        # named, not only the languages of the results: a site missing from
        # the graph is missing from the results too.
        gaps = {
            c.get("language")
            for c in coverage
            if c.get("family") == family
            and c.get("status") != "linked"
            and c.get("language")
        }
        for language in sorted(languages | gaps):
            records = [
                c
                for c in coverage
                if c.get("family") == family and c.get("language") == language
            ]
            if not records:
                continue
            linked = [c for c in records if c.get("status") == "linked"]
            if len(linked) == len(records):
                phrases.append(
                    f"{family} in {language}: complete, compiler-checked"
                    + _unlinked(records)
                )
            else:
                reasons = sorted(
                    {
                        str(c.get("reason"))
                        for c in records
                        if c.get("status") != "linked" and c.get("reason")
                    }
                )
                protocols = sorted(
                    {
                        str(c.get("protocol"))
                        for c in records
                        if c.get("status") != "linked" and c.get("protocol")
                    }
                )
                what = f"{family} ({', '.join(protocols)})" if protocols else family
                where = "" if language in languages else " (none returned)"
                phrases.append(
                    f"{what} in {language}{where}: may be incomplete"
                    + (f" ({', '.join(reasons)})" if reasons else "")
                    + _unlinked(records)
                    + ", check the source"
                )
    if not phrases:
        return None
    return "Coverage: " + "; ".join(phrases) + "."


# Calls a resolver placed outside the repository: another declaration that
# happens to share the name, not a gap.
_EXTERNAL = ("scip_external", "pyright_external", "compiler_external_module")


def _some(items, shown):
    more = len(items) - shown
    return ", ".join(items[:shown]) + (f" and {more} more" if more > 0 else "")


def _plural(n, one, many):
    return f"{n} {one if n == 1 else many}"


def text_check_lines(checks):
    """Sentences for ``GraphQueryService._text_check`` records: how every
    line of the analyzed source naming the result's declaration is
    accounted for, and which lines, if any, still need reading."""
    lines = []
    for c in checks or ():
        name = c["name"]
        if c.get("too_many"):
            lines.append(
                f"Text check of '{name}': more than {c['lines'] - 1} lines name it,"
                " too common a word to account for line by line."
            )
            continue
        where = (
            f" (declared at {_some(c['declared_at'], 3)})"
            if c.get("declared_at")
            else ""
        )
        head = (
            f"Text check of '{name}'{where}: "
            + _plural(c["lines"], "line", "lines")
            + " in "
            + _plural(c["files"], "analyzed file", "analyzed files")
            + (" names" if c["lines"] == 1 else " name")
            + " it"
        )
        parts = []
        if c.get("in_result"):
            parts.append(
                _plural(
                    c["in_result"],
                    "is a link in this result",
                    "are links in this result",
                )
            )
        if c.get("declaration"):
            parts.append(
                _plural(c["declaration"], "is its declaration", "are its declaration")
            )
        left = c.get("left_out") or []
        if left:
            said = []
            for link in left:
                verb = RELATIONSHIP_SENTENCES.get(link["kind"], link["kind"].lower())
                source = link["source"]
                if link["start"] is None:
                    source = f"{link['file']} (file)"
                else:
                    source += f" ({link['file']}:{link['start']})"
                said.append(f"{source} {verb} {name} at line {link['line']}")
            what = (
                "is a link the graph holds that this result leaves out"
                if len(left) == 1
                else "are links the graph holds that this result leaves out"
            )
            if not c.get("result_links"):
                what = (
                    "is a link the graph holds to it"
                    if len(left) == 1
                    else "are links the graph holds to it"
                )
            parts.append(f"{len(left)} {what}: " + _some(said, 6))
        if c.get("import"):
            parts.append(_plural(c["import"], "is an import", "are imports"))
        unlinked = c.get("unlinked") or []
        external = [u for u in unlinked if u["reason"] in _EXTERNAL]
        gaps = [u for u in unlinked if u["reason"] not in _EXTERNAL]
        if external:
            parts.append(
                _plural(len(external), "is a call", "are calls")
                + " the compiler index resolved outside the repository"
            )
        if gaps:
            parts.append(
                _plural(len(gaps), "is a call", "are calls")
                + " no resolver linked: "
                + _some([f"{u['at']} ({u['reason']})" for u in gaps], 5)
            )
        others = c.get("other_targets") or []
        if others:
            parts.append(
                _plural(len(others), "links", "link")
                + f" another declaration named {name}"
                + " ("
                + _some(
                    [f"{o['at']} to {o['target']} at {o['target_at']}" for o in others],
                    3,
                )
                + ")"
            )
        if c.get("other_declaration"):
            parts.append(
                _plural(c["other_declaration"], "declares", "declare")
                + f" another {name}"
            )
        if c.get("comment"):
            parts.append(_plural(c["comment"], "is in a comment", "are in comments"))
        strings = c.get("strings") or []
        if strings:
            parts.append(
                _plural(len(strings), "is in a string", "are in strings")
                + ": "
                + _some(strings, 5)
            )
        missing = c.get("not_in_graph") or []
        if missing:
            parts.append(
                _plural(len(missing), "is code", "are code")
                + " the graph does not link: "
                + _some(missing, 10)
            )
        text = head + (": " + "; ".join(parts) if parts else "") + "."
        if missing or gaps or strings:
            text += " Every other line is accounted for."
        elif c["lines"]:
            text += f" A text search for '{name}' finds nothing more than this."
        if c.get("unchecked_files"):
            text += (
                " Not checked, edited since the graph was built: "
                + _some(c["unchecked_files"], 5)
                + "."
            )
        lines.append(text)
    return lines


def _numbered(text, start):
    """Source text with each line's number in its file, for editing."""
    lines = text.rstrip("\n").split("\n")
    width = len(str(start + len(lines) - 1))
    return "\n".join(f"{start + i:>{width}}| {line}" for i, line in enumerate(lines))


def _examples(record, outgoing):
    shown = []
    for other in record["first"]:
        if other["start"] is None:
            where = f"{other['file']} (file)" if other["file"] else None
        else:
            where = f"{other['file']}:{other['start']}"
        text = str(other["name"]) if other["name"] is not None else "(unnamed)"
        if where and other["name"] != other["file"]:
            text += f" ({where})"
        elif where:
            text = where
        if other["line"] is not None:
            text += f" at line {other['line']}"
        shown.append(text)
    more = record["links"] - len(shown)
    verb = RELATIONSHIP_SENTENCES.get(record["kind"], record["kind"].lower())
    noun = _plural(record["links"], f"{record['kind']} link", f"{record['kind']} links")
    head = (
        f"{noun} from it (it {verb} these)"
        if outgoing
        else f"{noun} into it (each {verb} it)"
    )
    return head + ": " + "; ".join(shown) + (f"; and {more} more" if more > 0 else "")


def subject_lines(subjects):
    """A line per declaration a result is about
    (``GraphQueryService._result_subjects``): what links into it and what
    it links to, each kind with a count and its first items and lines."""
    lines = []
    for subject in subjects or ():
        where = (
            f"{subject['file']}:{subject['start']}"
            if subject.get("start") is not None
            else subject.get("file")
        )
        parts = [_examples(r, False) for r in subject.get("into") or []]
        parts += [_examples(r, True) for r in subject.get("out") or []]
        lines.append(
            f"Links of {subject['name']} ({where}): "
            + (". ".join(parts) if parts else "none in the graph")
            + "."
        )
    return lines


def render(
    response,
    *,
    nodes=None,
    coverage=None,
    query=None,
    limit=None,
    text_check=None,
    subjects=None,
):
    """Text for a ``query_cypher`` response. ``nodes`` maps element ids to
    nodes that links in the result point at but the query did not return;
    ``coverage`` is the snapshot's EdgeCoverage records as plain dicts;
    ``text_check`` is ``GraphQueryService._text_check``'s records;
    ``subjects`` the declarations the result is about, with their links
    (their source text in the result is numbered by line)."""
    items = response.get("items") or []
    renderer = _Renderer(items, nodes or {})
    renderer.sources = {
        str(s["source"]).rstrip("\n"): s["start"]
        for s in subjects or ()
        if s.get("source") and s.get("start") is not None
    }
    count = len(items)
    noun = "result" if count == 1 else "results"
    if response.get("truncated"):
        header = (
            f"{count} {noun}, cut at the limit: more exist. Narrow the query"
            + (f" or raise max_elements above {limit}" if limit else "")
            + "."
        )
    else:
        header = (
            f"{count} {noun}: every one this query matched." if count else "No results."
        )
    lines = [header]
    fresh = response.get("freshness") or {}
    if fresh.get("status") == "changed":
        # Files named in the result that were edited or removed in the
        # checkout since the snapshot was analyzed: their lines may have moved.
        said = []
        if fresh.get("changed_files"):
            said.append("edited " + ", ".join(fresh["changed_files"]))
        if fresh.get("missing_files"):
            said.append("removed " + ", ".join(fresh["missing_files"]))
        more = " and more" if fresh.get("files_listed_truncated") else ""
        lines.append(
            "Changed since the graph was built: "
            + "; ".join(said)
            + more
            + ". Lines in these files may have moved: read them in source."
        )
    if count:
        columns = list(items[0].keys())
        # What every row shares, said once.
        drops = {}
        shared = []
        notes = []
        if count > 1:
            for column in columns:
                values = [row.get(column) for row in items]
                if all(_is_link(v) for v in values):
                    types = {v["type"] for v in values}
                    if len(types) != 1:
                        continue
                    verb = RELATIONSHIP_SENTENCES.get(
                        values[0]["type"], values[0]["type"].lower()
                    )
                    if len({v["target"] for v in values}) == 1:
                        drops[column] = "target"
                        notes.append(
                            f"Each of the following {verb} "
                            + renderer.endpoint(values[0]["target"])
                            + ":"
                        )
                    elif len({v["source"] for v in values}) == 1:
                        drops[column] = "source"
                        notes.append(
                            renderer.endpoint(values[0]["source"])
                            + f" {verb} each of the following:"
                        )
                elif all(_is_path(v) and v["nodes"] for v in values):
                    ends = {v["nodes"][-1]["element_id"] for v in values}
                    starts = {v["nodes"][0]["element_id"] for v in values}
                    if len(ends) == 1:
                        drops[column] = "end"
                        notes.append(
                            "Each chain below ends at "
                            + _item(values[0]["nodes"][-1])
                            + ' ("it"):'
                        )
                    elif len(starts) == 1:
                        drops[column] = "start"
                        notes.append(
                            "Each chain below starts at "
                            + _item(values[0]["nodes"][0])
                            + ' ("it"):'
                        )
        constant = [
            c
            for c in columns
            if count > 1
            and c not in drops
            and not any(_is_link(row.get(c)) or _is_path(row.get(c)) for row in items)
            and len({json.dumps(row.get(c), sort_keys=True) for row in items}) == 1
        ]
        # A column that is null in every result is said once; a null in
        # some results only is left out of those results.
        constant += [
            c
            for c in columns
            if count == 1 and c not in drops and items[0].get(c) is None
        ]
        for column in constant:
            if items[0].get(column) is None:
                shared.append(f"{column} none")
                continue
            text = renderer.value_text(items[0].get(column))
            if isinstance(items[0].get(column), str) and "\n" in text:
                text = renderer.block(text)
            if text is not None:
                if "\n" in text:
                    text = "\n" + text.rstrip("\n")
                    text = text.replace("\n", "\n    ")
                    shared.append(f"{column}:{text}\n")
                else:
                    shared.append(f"{column} {text}")
        if shared:
            lines.append("Same for every result: " + "; ".join(shared) + ".")
        lines.extend(notes)
        rest = [c for c in columns if c not in constant]
        # Leading plain columns that repeat become headings.
        heads = []
        for column in rest[:-1]:
            if count < 2 or any(
                _is_node(row.get(column))
                or _is_link(row.get(column))
                or _is_path(row.get(column))
                or (isinstance(row.get(column), str) and "\n" in row.get(column))
                for row in items
            ):
                break
            runs = 1 + sum(
                1
                for a, b in zip(items, items[1:])
                if any(a.get(h) != b.get(h) for h in heads + [column])
            )
            if runs * 2 <= count + 1:
                heads.append(column)
            else:
                break
        leaf = [c for c in rest if c not in heads]
        previous = None
        block = []

        def flush():
            if not block:
                return
            indent = "  " * len(heads)
            main = [r for r in block if not renderer.row_is_test(r, leaf)]
            tests = [r for r in block if renderer.row_is_test(r, leaf)]
            for group, title in ((main, None), (tests, "In tests:")):
                if not group:
                    continue
                if title and main:
                    lines.append(indent + title)
                for parts in _merge(
                    [renderer.row_parts(r, leaf, drops) for r in group]
                ):
                    text = _join(parts).replace("\n", "\n" + indent + "    ")
                    lines.append(indent + "- " + (text or "(no values)"))
            block.clear()

        for row in items:
            current = [row.get(h) for h in heads]
            if current != previous:
                flush()
                for i, head in enumerate(heads):
                    if previous is None or current[: i + 1] != previous[: i + 1]:
                        text = renderer.value_text(current[i])
                        lines.append(
                            "  " * i
                            + f"{head} {text if text is not None else '(none)'}:"
                        )
                previous = current
            block.append(row)
        flush()
    lines.extend(subject_lines(subjects))
    line = coverage_line(items, renderer.nodes, coverage, query)
    if line:
        lines.append(line)
    lines.extend(text_check_lines(text_check))
    return "\n".join(lines)
