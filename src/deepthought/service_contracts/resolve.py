"""Link client requests to the routes servers declare, by declared rules.

Every link is ``pattern_matched``: it rests on the request's method and path
fitting a route template, and on service names, never on a traced value. A
request that no rule ties to exactly one route stays unresolved or ambiguous,
with a reason, and produces no link.
"""

from dataclasses import dataclass

from .http import best_routes, request_target, route_template

HTTP_SERVICE_PATH = "service-contracts.http-service-path"
HTTP_PATH = "service-contracts.http-path"


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


def resolve_requests(routes, requests):
    """One resolution per request, in request order.

    The target service is the request's explicit service name (a Feign
    client's), else its URL's literal host. If it equals the name of a
    service that declares routes, only that service's routes are candidates
    (rule ``http-service-path``). Otherwise every route is a candidate (rule
    ``http-path``). The most specific compatible route wins (see
    ``http.best_routes``); a tie is ambiguous.
    """
    templates = [(r.id, r.methods, route_template(r.path), r.service) for r in routes]
    services = {r.service for r in routes if r.service}
    resolutions = []
    for request in requests:
        target = request_target(request.url)
        if target.reason:
            resolutions.append(
                RequestResolution(request.id, "unresolved", reason=target.reason)
            )
            continue
        service = request.target_service or target.host
        if service in services:
            candidates = [t[:3] for t in templates if t[3] == service]
            rule, none_reason = HTTP_SERVICE_PATH, "no_route_in_service"
        else:
            candidates = [t[:3] for t in templates]
            rule, none_reason = HTTP_PATH, "no_route"
            service = None
        best = best_routes(request.method, target.segments, candidates)
        common = {"path": target.display, "target_service": service}
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
