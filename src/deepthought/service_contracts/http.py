"""HTTP route templates, request templates and the rule that matches them.

A route is what a server declares (``GET /accounts/{name}``); a request is what
a client writes (``"http://account-service/accounts/" + name``). Both become
segment lists. Matching is structural and declared below, never scored: a
request links to a route only when exactly one route is the most specific
compatible match.
"""

from dataclasses import dataclass
import re

HTTP_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")
ANY_METHOD = "*"
# A value the source does not fix (a variable, a call, a URI template
# variable); it stands for text inside one path segment.
PLACEHOLDER = "\x00"

_SCHEME = re.compile(r"(?i)^(?:https?|wss?)://")
_ROUTE_VARIABLE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}")


@dataclass(frozen=True)
class Segment:
    """One path segment.

    ``kind`` is ``literal`` (exact text), ``variable`` (a whole-segment route
    variable such as ``{id}``), ``pattern`` (text with variables or a single
    ``*`` inside, compiled to ``regex``) or ``rest`` (Spring's ``**`` or a
    ``{*name}`` capture: zero or more segments).
    """

    kind: str
    text: str
    regex: str | None = None


def _route_segment(text: str) -> Segment:
    if text == "**" or re.fullmatch(r"\{\*[^{}]*\}|\{[^{}]*\.\.\.\}", text):
        return Segment("rest", "**")
    if text == "*" or _ROUTE_VARIABLE.fullmatch(text):
        return Segment("variable", "{}")
    if "*" in text or "{" in text:
        parts, last = [], 0
        for match in _ROUTE_VARIABLE.finditer(text):
            parts.append(re.escape(text[last : match.start()]))
            parts.append(".+")
            last = match.end()
        parts.append(re.escape(text[last:]))
        regex = "".join(parts).replace(r"\*", ".*")
        display = _ROUTE_VARIABLE.sub("{}", text)
        return Segment("pattern", display, regex)
    return Segment("literal", text)


def _split(path: str) -> list[str]:
    return [part for part in path.split("/") if part]


def join_paths(*parts: str) -> str:
    """Join declared path fragments with single slashes, as Spring does."""
    segments = [s for part in parts for s in _split(part)]
    return "/" + "/".join(segments)


def route_template(path: str) -> tuple[Segment, ...]:
    """A server-declared path; trailing and repeated slashes are ignored."""
    return tuple(_route_segment(s) for s in _split(path))


def display_route(segments) -> str:
    return "/" + "/".join(s.text for s in segments)


@dataclass(frozen=True)
class RequestTarget:
    """What a client URL fixes: the literal host, if any, and its path."""

    host: str | None
    segments: tuple[str, ...]  # PLACEHOLDER marks text the source does not fix
    reason: str | None = None  # why no path could be read

    @property
    def display(self) -> str:
        if self.reason:
            return ""
        return "/" + "/".join(s.replace(PLACEHOLDER, "{?}") for s in self.segments)


def request_target(url: str) -> RequestTarget:
    """Read a client URL in which PLACEHOLDER marks unknown text.

    ``scheme://host[:port]/path`` keeps the literal host (lowercased, port
    dropped). A URL that starts with unknown text (a base URL variable) keeps
    only the path that follows it, which must start with ``/``; the unknown
    base is assumed to add no path segments. Query strings and fragments are
    dropped, as are URI template variables (``{name}``), which become unknown
    text.
    """
    url = re.sub(r"\{[^{}/]*\}", PLACEHOLDER, url)
    url = re.split(r"[?#]", url, maxsplit=1)[0]
    host = None
    match = _SCHEME.match(url)
    if match:
        rest = url[match.end() :]
        authority, slash, path = rest.partition("/")
        if not authority:
            return RequestTarget(None, (), "no_host")
        name = authority.split("@")[-1].split(":")[0]
        if PLACEHOLDER not in name:
            host = name.lower()
        # A host that is not literal ("http://" + host + "/path") leaves the
        # path literal; the host stays unknown. A literal host with no path
        # is the root path; unknown text after the host may hold a path.
        if not slash and PLACEHOLDER in authority:
            return RequestTarget(None, (), "no_literal_path")
        url = (slash + path) or "/"
    elif url.startswith(PLACEHOLDER):
        base, slash, path = url.lstrip(PLACEHOLDER).partition("/")
        if base:
            return RequestTarget(None, (), "base_not_separated")
        url = slash + path
    if not url.startswith("/"):
        return RequestTarget(None, (), "no_literal_path")
    # Adjacent unknown parts are one unknown text.
    segments = tuple(
        re.sub(f"{PLACEHOLDER}+", PLACEHOLDER, part) for part in _split(url)
    )
    # A path with no literal text ("/" + path) fixes nothing to match.
    if segments and all(s == PLACEHOLDER for s in segments):
        return RequestTarget(None, (), "no_literal_path")
    return RequestTarget(host, segments)


def _segment_rank(request: str, route: Segment) -> int | None:
    """How well one request segment fits one route segment; None if not.

    3: equal literal text. 2: unknown request text against a route variable.
    1: literal request text against a route variable or pattern, or unknown
    text that a route literal or pattern could hold.
    """
    unknown = PLACEHOLDER in request
    if route.kind == "literal":
        if not unknown:
            return 3 if request == route.text else None
        pattern = "".join(
            ".+" if part == PLACEHOLDER else re.escape(part)
            for part in re.split(f"({PLACEHOLDER})", request)
            if part
        )
        return 1 if re.fullmatch(pattern, route.text) else None
    if route.kind == "variable":
        return 2 if request == PLACEHOLDER else 1
    if route.kind == "pattern":
        if unknown:
            return 1
        return 1 if re.fullmatch(route.regex, request) else None
    raise ValueError("rest segments are matched by match_rank")


def match_rank(request: tuple[str, ...], route: tuple[Segment, ...]):
    """Per-segment ranks of a compatible match, or None.

    A ``rest`` segment matches any remaining segments and ranks 0 for each.
    """
    ranks = []
    for index, segment in enumerate(route):
        if segment.kind == "rest":
            # Only a trailing rest segment is supported; others never match.
            if index != len(route) - 1:
                return None
            ranks.extend(0 for _ in request[index:])
            return tuple(ranks)
        if index >= len(request):
            return None
        rank = _segment_rank(request[index], segment)
        if rank is None:
            return None
        ranks.append(rank)
    return tuple(ranks) if len(request) == len(route) else None


def method_compatible(request_method: str | None, route_methods) -> bool:
    if request_method is None or ANY_METHOD in route_methods:
        return True
    return request_method in route_methods


def best_routes(request_method, request_segments, routes):
    """The routes tied for the most specific compatible match.

    ``routes`` are (key, methods, segments). The ranks are compared segment by
    segment from the left, so a literal at the first differing segment beats
    a variable, as in Spring's own route ordering; an exact method beats a
    route that accepts any method. Returns the tied keys, sorted.
    """
    scored = []
    for key, methods, segments in routes:
        if not method_compatible(request_method, methods):
            continue
        rank = match_rank(request_segments, segments)
        if rank is None:
            continue
        exact_method = int(request_method is not None and request_method in methods)
        scored.append(((rank, exact_method), key))
    if not scored:
        return ()
    best = max(score for score, _ in scored)
    return tuple(sorted(key for score, key in scored if score == best))
