"""Link client requests to the routes servers declare, by declared rules.

Every link is ``pattern_matched``: it rests on the request's method and path
fitting a route template, and on service names, never on a traced value. A
request that no rule ties to exactly one route stays unresolved or ambiguous,
with a reason, and produces no link.
"""

from dataclasses import dataclass

from .gateways import forward
from .http import PLACEHOLDER, best_routes, request_target, route_template
from .listeners import LOOPBACK, under

HTTP_SERVICE_PATH = "service-contracts.http-service-path"
HTTP_PATH = "service-contracts.http-path"
HTTP_GATEWAY = "service-contracts.http-gateway"
HTTP_LOCAL_PORT = "service-contracts.http-local-port"
HTTP_UNKNOWN_BASE = "service-contracts.http-path-unknown-base"


@dataclass(frozen=True)
class RequestResolution:
    request_id: str
    status: str  # linked | ambiguous | unresolved
    route_id: str | None = None
    rule_id: str | None = None
    reason: str | None = None
    candidates: tuple[str, ...] = ()
    path: str = ""  # the request path as read, unknown text shown as {?}
    target_service: str | None = None
    gateway: str | None = None  # "file:line" of the gateway route followed


def resolve_requests(
    routes, requests, gateways=(), hosts=None, project=None, ports=None, served=None
):
    """One resolution per request, in request order.

    The target service is the request's explicit service name (a Feign
    client's), else its URL's literal host, or the service that ``hosts``
    ({host name: service}) gives for it. If it equals the name of a
    service that declares routes, only that service's routes are candidates
    (rule ``http-service-path``). Otherwise every route is a candidate (rule
    ``http-path``). The most specific compatible route wins (see
    ``http.best_routes``); a tie is ambiguous.

    A request with no literal host that names no known service, whose path
    starts with the literal prefix of a gateway route, is forwarded: the
    longest prefix wins, its replacement takes its place, and the route's
    target narrows the candidates as a host would (rule ``http-gateway``).
    Prefixes tied in length with different forwards are ambiguous.

    A request whose URL starts with unknown text (a base URL variable) is
    sent to a host configured elsewhere, so under ``http-path`` the routes
    of its own project (``project``: file -> project directory) are not
    candidates. A file whose project is not known (``project`` gives
    None) excludes nothing, nor does a test's request, since tests
    usually call their own project's service.

    A route declared under an unknown base written as text
    (``base_written``, such as a FastAPI prefix read from settings, or Go's
    ``baseUrl + "/cart"``) stores only the path after that base. A route
    builder the reader could not follow (an ASP.NET ``app`` parameter) may
    add nothing, so its routes do not take part. When no candidate matches the request's whole path, such a route
    matches the request's path after one or more leading literal segments,
    which stand for the unknown base (rule ``http-path-unknown-base``). Only
    a route with a literal segment of its own takes part (``/{id}`` would fit
    any path). The fewest skipped segments that fit any route win, and the
    request links only when exactly one route fits with that many.
    """
    templates = [
        (
            r.id,
            r.methods,
            route_template(r.path.lower() if r.case_insensitive else r.path),
            r.service,
            r.case_insensitive,
        )
        for r in routes
    ]
    unknown_base = {r.id for r in routes if r.base_unknown and r.base_written}
    services = {r.service for r in routes if r.service}
    route_files = {r.id: r.file for r in routes}
    resolutions = []
    for request in requests:
        target = request_target(request.url)
        if target.reason:
            resolutions.append(
                RequestResolution(request.id, "unresolved", reason=target.reason)
            )
            continue
        service = request.target_service or (hosts or {}).get(target.host, target.host)
        segments, gateway, forwarded = target.segments, None, False
        if service not in services and target.host is None:
            matches = forward(gateways, target.segments, request.method)
            if len({(m.replacement, m.target) for m in matches}) > 1:
                resolutions.append(
                    RequestResolution(
                        request.id,
                        "ambiguous",
                        reason="several_gateways",
                        path=target.display,
                    )
                )
                continue
            if matches:
                route = min(matches, key=lambda m: (m.file, m.line))
                segments = route.replacement + target.segments[len(route.prefix) :]
                gateway, forwarded = f"{route.file}:{route.line}", True
                service = route.target
        if service in services:
            candidates = [t[:3] + t[4:] for t in templates if t[3] == service]
            rule, none_reason = HTTP_SERVICE_PATH, "no_route_in_service"
        else:
            candidates = [t[:3] + t[4:] for t in templates]
            if (
                project is not None
                and not forwarded
                and not getattr(request, "test_source", False)
                and request.url.startswith(PLACEHOLDER)
            ):
                own = project(request.file)
                if own is not None:
                    candidates = [
                        c for c in candidates if project(route_files[c[0]]) != own
                    ]
            rule, none_reason = HTTP_PATH, "no_route"
            programs = (ports or {}).get(target.port)
            if target.host in LOOPBACK and programs and not forwarded:
                candidates = [
                    c
                    for c in candidates
                    if any(
                        under(file, program)
                        for file in (served or {}).get(c[0], (route_files[c[0]],))
                        for program in programs
                    )
                ]
                rule, none_reason = HTTP_LOCAL_PORT, "no_route_on_port"
            service = None
        if forwarded:
            rule = HTTP_GATEWAY
        best = best_routes(request.method, segments, candidates)
        if not best and not forwarded:
            best = _under_unknown_base(
                request.method,
                segments,
                [
                    c
                    for c in candidates
                    if c[0] in unknown_base and any(s.kind == "literal" for s in c[2])
                ],
            )
            if best:
                rule = HTTP_UNKNOWN_BASE
        shown = "/" + "/".join(s.replace(PLACEHOLDER, "{?}") for s in segments)
        common = {
            "path": shown,
            "target_service": service,
            "gateway": gateway,
        }
        if len(best) == 1:
            resolutions.append(
                RequestResolution(request.id, "linked", best[0], rule, **common)
            )
        elif best:
            resolutions.append(
                RequestResolution(
                    request.id,
                    "ambiguous",
                    reason="several_routes",
                    candidates=best,
                    **common,
                )
            )
        else:
            resolutions.append(
                RequestResolution(
                    request.id, "unresolved", reason=none_reason, **common
                )
            )
    return resolutions


def _under_unknown_base(method, segments, candidates):
    """The routes declared under an unknown base that match the request's
    path once its leading literal segments are skipped, the skipped text
    being taken as that base. The shortest skip that fits any route wins
    (``/a/x/y`` goes to ``/x/y`` before ``/y``), and the rest of the path
    must fix some text of its own; the keys that tie are returned sorted."""
    for skip in range(1, len(segments)):
        if PLACEHOLDER in segments[skip - 1]:
            return ()
        rest = segments[skip:]
        if all(PLACEHOLDER in s for s in rest):
            return ()
        found = best_routes(method, rest, candidates)
        if found:
            return found
    return ()
