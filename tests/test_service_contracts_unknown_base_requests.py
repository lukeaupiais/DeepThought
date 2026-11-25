"""Requests to routes declared under a base the source does not fix: the
request's leading literal segments may stand for that base."""

import unittest

from deepthought.service_contracts.http import PLACEHOLDER
from deepthought.service_contracts.model import HttpRequest, HttpRoute
from deepthought.service_contracts.resolve import (
    HTTP_PATH,
    HTTP_UNKNOWN_BASE,
    resolve_requests,
)


def _route(identifier, path, base_unknown=True, methods=("GET",), written=True):
    return HttpRoute(
        identifier,
        "app.py",
        1,
        0,
        "h",
        methods,
        path,
        "fastapi",
        base_unknown=base_unknown,
        base_written=base_unknown and written,
    )


def _request(identifier, url, method="GET"):
    return HttpRequest(identifier, "c.ts", 1, 0, None, method, url, "fetch")


class UnknownBaseRequestTests(unittest.TestCase):
    def test_resolution(self):
        routes = [
            _route("items", "/items/{id}"),
            _route("users", "/users/"),
            _route("me", "/users/me"),
            _route("any", "/{id}"),
            _route("exact", "/api/v1/users/", base_unknown=False),
            _route("a", "/x/y"),
            _route("b", "/y"),
            _route("dup1", "/dup"),
            _route("dup2", "/dup"),
            # An ASP.NET builder parameter: the base may be empty.
            _route("builder", "/orders", written=False),
        ]
        requests = [
            _request("prefixed", "/api/v1/items/7"),
            _request("variable-tail", PLACEHOLDER + "/api/v1/items/" + PLACEHOLDER),
            # A whole-path match always wins.
            _request("exact", "/api/v1/users/"),
            _request("specific", "/api/v1/users/me"),
            # The skipped prefix must be literal.
            _request("unknown-prefix", "/" + PLACEHOLDER + "/items/7"),
            # /x/y after skipping /a wins over /y after skipping /a/x.
            _request("shortest", "/a/x/y"),
            # Two routes fit after the same skip.
            _request("two", "/api/dup"),
            # Only routes with a literal segment take part: /{id} would fit.
            _request("nothing", "/api/v1/unknown"),
            _request("builder", "/api/orders"),
            # The method still has to fit.
            _request("method", "/api/v1/items/7", "DELETE"),
        ]
        found = {r.request_id: r for r in resolve_requests(routes, requests)}

        def shown(key):
            r = found[key]
            return (r.status, r.route_id or r.candidates, r.rule_id)

        self.assertEqual(shown("prefixed"), ("linked", "items", HTTP_UNKNOWN_BASE))
        self.assertEqual(shown("variable-tail"), ("linked", "items", HTTP_UNKNOWN_BASE))
        self.assertEqual(shown("exact"), ("linked", "exact", HTTP_PATH))
        self.assertEqual(shown("specific"), ("linked", "me", HTTP_UNKNOWN_BASE))
        self.assertEqual(found["unknown-prefix"].status, "unresolved")
        self.assertEqual(shown("shortest"), ("linked", "a", HTTP_UNKNOWN_BASE))
        self.assertEqual(shown("two"), ("ambiguous", ("dup1", "dup2"), None))
        self.assertEqual(found["nothing"].status, "unresolved")
        self.assertEqual(found["method"].status, "unresolved")
        self.assertEqual(found["builder"].status, "unresolved")


if __name__ == "__main__":
    unittest.main()
