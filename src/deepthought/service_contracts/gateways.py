"""Gateway routes that forward a path prefix to a backend service.

Read: Spring Cloud Gateway routes in a module's ``application``/``bootstrap``
YAML (``spring.cloud.gateway.routes``, or ``spring.cloud.gateway.server.
webflux.routes``) with ``Path=`` predicates ending in ``/**``, ``uri`` of
``lb://service`` or ``http(s)://host``, and ``StripPrefix``/``PrefixPath``
filters or a ``RewritePath`` that replaces a literal path prefix; and nginx
``location /prefix/ { proxy_pass ...; }`` blocks in ``*.conf`` and
``*.conf.template`` files. A route with any other filter that changes the
path (another ``RewritePath``, ``SetPath``, a regex location) is recorded as
unread rather than guessed.
"""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class GatewayRoute:
    """``prefix`` (literal segments) is replaced by ``replacement`` segments
    when a request is forwarded to ``target`` (a service name or host, None
    when not literal)."""

    file: str
    line: int
    kind: str  # spring-cloud-gateway | nginx
    prefix: tuple[str, ...]
    replacement: tuple[str, ...]
    target: str | None


@dataclass(frozen=True)
class GatewayUnread:
    file: str
    line: int
    reason: str


def _segments(path):
    return tuple(part for part in path.split("/") if part)


def _line_of(text, needle, default=1):
    index = text.find(needle)
    return default if index < 0 else text.count("\n", 0, index) + 1


def _target(uri):
    match = re.match(r"(?i)^(lb|https?)://([^/:${}]+)(?::\d+)?/?$", uri or "")
    return match.group(2).lower() if match else None


def _shortcut(item):
    """(name, [args]) of a predicate or filter, shortcut or full form."""
    if isinstance(item, str):
        name, _, args = item.partition("=")
        return name.strip(), [a.strip() for a in args.split(",") if a.strip()]
    if isinstance(item, dict):
        args = item.get("args") or {}
        values = list(args.values()) if isinstance(args, dict) else [args]
        flat = []
        for value in values:
            flat.extend(value if isinstance(value, list) else [value])
        return str(item.get("name", "")), [str(v) for v in flat]
    return "", []


# RewritePath regexes that replace a literal prefix and keep the rest:
# /old/?(?<rest>.*) or /old(?<rest>/?.*), optionally anchored.
_REWRITE = re.compile(
    r"\^?(?P<old>(?:/[\w.~-]+)*)(?P<separator>/\??)?"
    r"\((?:\?<(?P<name>\w+)>)?(?P<body>/\??)?\.\*\)\$?"
)


def _rewrite(args):
    """(old prefix segments, new prefix segments) of a ``RewritePath`` that
    swaps a literal path prefix and keeps the rest of the path, else None."""
    if len(args) != 2:
        return None
    match = _REWRITE.fullmatch(args[0])
    if match is None or bool(match["separator"]) == bool(match["body"]):
        return None  # exactly one of the two must carry the slash
    references = ["$1", "${1}", "$\\{1}"]
    if match["name"]:
        references += ["${" + match["name"] + "}", "$\\{" + match["name"] + "}"]
    reference = next((r for r in references if args[1].endswith(r)), None)
    if reference is None:
        return None
    new = args[1][: -len(reference)]
    if "$" in new or "\\" in new or not (new == "" or new.startswith("/")):
        return None
    # /old/?(.*) -> /new/$1 joins with a slash in the replacement; /old(/?.*)
    # -> /new$1 joins with the captured one.
    if bool(match["separator"]) != (new.endswith("/") or new == "/"):
        return None
    if match["body"] and new.endswith("/"):
        return None
    return _segments(match["old"]), _segments(new)


def spring_gateway_routes(path, text, file):
    """Routes and unread routes of one Spring configuration YAML file."""
    try:
        import yaml
    except ImportError:  # pyyaml comes with the languages extra
        return [], [GatewayUnread(file, 1, "yaml_not_installed")]
    try:
        documents = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except yaml.YAMLError:
        return [], [GatewayUnread(file, 1, "yaml_not_parsed")]
    routes, unread = [], []
    for document in documents:
        spring = document.get("spring") or {}
        config = spring.get("config") if isinstance(spring, dict) else None
        if isinstance(config, dict) and (config.get("activate") or {}).get(
            "on-profile"
        ):
            continue  # profile documents are not read, as for service names
        gateway = (spring.get("cloud") or {}).get("gateway") or {}
        found = gateway.get("routes")
        if found is None:
            found = ((gateway.get("server") or {}).get("webflux") or {}).get("routes")
        for route in found or []:
            if not isinstance(route, dict):
                continue
            line = _line_of(text, f"id: {route.get('id')}")
            paths, strip, prefix_path, other, rewrite = [], 0, "", [], None
            path_filters = 0
            for name, args in map(_shortcut, route.get("predicates") or []):
                if name == "Path":
                    paths.extend(args)
                else:
                    other.append(name)
            for name, args in map(_shortcut, route.get("filters") or []):
                if name == "StripPrefix" and args and args[0].isdigit():
                    strip, path_filters = int(args[0]), path_filters + 1
                elif name == "PrefixPath" and args:
                    prefix_path, path_filters = args[0], path_filters + 1
                elif name == "RewritePath" and rewrite is None:
                    rewrite, path_filters = _rewrite(args), path_filters + 1
                    if rewrite is None:
                        other.append(name)
                elif name in (
                    "RewritePath",
                    "SetPath",
                    "RewriteLocationResponseHeader",
                ):
                    other.append(name)
            # A RewritePath is read alone: filters apply in order, which the
            # other path filters would make matter.
            if (other and any(n in ("RewritePath", "SetPath") for n in other)) or (
                rewrite is not None and path_filters > 1
            ):
                unread.append(GatewayUnread(file, line, "path_rewritten"))
                continue
            target = _target(str(route.get("uri", "")))
            for pattern in paths:
                if not pattern.endswith("/**") or re.search(r"[*{}?]", pattern[:-3]):
                    unread.append(GatewayUnread(file, line, "path_pattern"))
                    continue
                prefix = _segments(pattern[:-3])
                if rewrite is not None:
                    old, replacement = rewrite
                    if prefix[: len(old)] != old:
                        # The regex does not match every path the route takes.
                        unread.append(GatewayUnread(file, line, "path_rewritten"))
                        continue
                    routes.append(
                        GatewayRoute(
                            file,
                            line,
                            "spring-cloud-gateway",
                            prefix,
                            replacement + prefix[len(old) :],
                            target,
                        )
                    )
                    continue
                if strip > len(prefix):
                    unread.append(GatewayUnread(file, line, "strip_beyond_prefix"))
                    continue
                routes.append(
                    GatewayRoute(
                        file,
                        line,
                        "spring-cloud-gateway",
                        prefix,
                        _segments(prefix_path) + prefix[strip:],
                        target,
                    )
                )
    return routes, unread


_LOCATION = re.compile(
    r"location\s+(\^~\s+|=\s+|~\*?\s+)?(\S+)\s*\{((?:[^{}]|\$\{[^{}]*\})*)\}",
    re.MULTILINE,
)


def nginx_routes(text, file):
    """``location`` prefixes forwarded by ``proxy_pass`` in an nginx file."""
    routes, unread = [], []
    stripped = re.sub(r"#[^\n]*", lambda m: " " * len(m.group(0)), text)
    for match in _LOCATION.finditer(stripped):
        modifier, location, body = (
            (match.group(1) or "").strip(),
            match.group(2),
            match.group(3),
        )
        proxy = re.search(r"proxy_pass\s+([^;\s]+)\s*;", body)
        if proxy is None:
            continue
        line = stripped.count("\n", 0, match.start()) + 1
        if modifier not in ("", "^~") or not location.startswith("/"):
            unread.append(GatewayUnread(file, line, "location_not_prefix"))
            continue
        url = proxy.group(1)
        parsed = re.match(r"(?i)^https?://([^/]+)(/.*)?$", url)
        if parsed is None or "$" in (parsed.group(2) or ""):
            unread.append(GatewayUnread(file, line, "proxy_pass_not_literal"))
            continue
        host = parsed.group(1).split(":")[0]
        target = None if "$" in host else host.lower()
        prefix = _segments(location)
        # A proxy_pass with a URI replaces the matched prefix with it; without
        # one the request path is passed on unchanged.
        replacement = _segments(parsed.group(2)) if parsed.group(2) else prefix
        if not location.endswith("/") and parsed.group(2):
            # "location /api" also matches "/apix"; only whole segments are read.
            unread.append(GatewayUnread(file, line, "location_not_segment"))
            continue
        routes.append(GatewayRoute(file, line, "nginx", prefix, replacement, target))
    return routes, unread


def forward(gateways, segments):
    """The gateway routes with the longest prefix of ``segments``."""
    best, length = [], -1
    for route in gateways:
        size = len(route.prefix)
        if size == 0 or segments[:size] != route.prefix:
            continue
        if size > length:
            best, length = [route], size
        elif size == length:
            best.append(route)
    return best
