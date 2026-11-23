"""Contract facts as written in source; matching them is a separate step."""

from dataclasses import dataclass


@dataclass(frozen=True)
class HttpRoute:
    """A route a server declares, such as a Spring ``@GetMapping``.

    ``path`` is the declared path joined with its type-level prefix;
    ``methods`` are HTTP methods or ``("*",)`` for any. ``handler_id`` is the
    declaration that handles it. ``service`` is the logical service name the
    file's build module declares (Spring's ``spring.application.name``), if
    exactly one.
    """

    id: str
    file: str
    line: int
    column: int
    handler_id: str
    methods: tuple[str, ...]
    path: str
    framework: str
    service: str | None = None


@dataclass(frozen=True)
class HttpRequest:
    """A client request whose method and URL the source fixes, as written.

    ``url`` keeps unknown text as ``PLACEHOLDER``. ``target_service`` is a
    service name the client names explicitly (a Feign client's name); the
    URL's literal host is read when matching. ``method`` is None when the
    source does not fix it. ``caller_id`` is the innermost enclosing
    declaration, None at file level. A URL chosen in ``if``/``else``
    branches gives one request per branch, each counting ``alternatives``.
    """

    id: str
    file: str
    line: int
    column: int
    caller_id: str | None
    method: str | None
    url: str
    client: str
    target_service: str | None = None
    # How many URLs the call site may request; each is one request.
    alternatives: int = 1


@dataclass(frozen=True)
class Unread:
    """A route or request the extractor saw but could not read, and why."""

    file: str
    line: int
    kind: str  # route | request
    reason: str
