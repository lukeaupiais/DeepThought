"""Django URLconf routes through the scip-python index: path() and
re_path()/url() entries, includes composing prefixes, views of the
repository, and entries that are not read."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.service_contracts.django_urls import _regex_path

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_python"
)

FILES = {
    "site/__init__.py": "",
    "site/urls.py": """from django.urls import include, path
from django.contrib import admin

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("shop.urls")),
]
""",
    "shop/__init__.py": "",
    "shop/views.py": """def item(request, item_id):
    pass

class Orders:
    pass
""",
    "shop/urls.py": """from django.urls import include, path, re_path
from . import views
from .views import Orders

urlpatterns = [
    path("items/<int:item_id>/", views.item),
    re_path(r"^orders/(?P<order_id>[0-9]+)/?$", Orders.as_view()),
    re_path(r"^a|b$", views.item),
]

nested = [path("<int:pk>/", views.item)]
unmounted = [path("never/", views.item)]
urlpatterns += [path("more/", include(nested))]
""",
}

DJANGO = "scip-python python Django 5.1.0 `django.urls.conf`/"
APP = "scip-python python app 0 "
ITEM = APP + "`shop.views`/item()."
ORDERS = APP + "`shop.views`/Orders#"

# (0-based line, name, nth occurrence on the line, symbol, is definition)
OCCURRENCES = {
    "site/urls.py": [
        (4, "path", 0, DJANGO + "path.", False),
        (5, "path", 0, DJANGO + "path.", False),
        (5, "include", 0, DJANGO + "include().", False),
    ],
    "shop/views.py": [
        (0, "item", 0, ITEM, True),
        (3, "Orders", 0, ORDERS, True),
    ],
    "shop/urls.py": [
        (5, "path", 0, DJANGO + "path.", False),
        (5, "item", 2, ITEM, False),
        (6, "re_path", 0, DJANGO + "re_path.", False),
        (6, "Orders", 0, ORDERS, False),
        (7, "re_path", 0, DJANGO + "re_path.", False),
        (7, "item", 0, ITEM, False),
        (10, "path", 0, DJANGO + "path.", False),
        (10, "item", 0, ITEM, False),
        (11, "path", 0, DJANGO + "path.", False),
        (11, "item", 0, ITEM, False),
        (12, "path", 0, DJANGO + "path.", False),
        (12, "include", 0, DJANGO + "include().", False),
    ],
    "site/__init__.py": [],
    "shop/__init__.py": [],
}


def scip_index(directory):
    import deepthought
    from deepthought._scip import scip_pb2 as wire

    index = wire.Index()
    index.metadata.tool_info.name = "scip-python"
    index.metadata.tool_info.version = "0.6.8"
    for path, found in OCCURRENCES.items():
        document = index.documents.add(relative_path=path)
        lines = FILES[path].split("\n")
        for line, name, nth, symbol, definition in found:
            column = -1
            for _ in range(nth + 1):
                column = lines[line].index(name, column + 1)
            document.occurrences.add(
                symbol=symbol,
                range=[line, column, column + len(name)],
                symbol_roles=wire.Definition if definition else 0,
            )
    path = Path(directory) / "index.scip"
    path.write_bytes(index.SerializeToString())
    return deepthought.ingest_scip(path, repository_id="r"), "sha256:test"


def analyze(indexed):
    with (
        tempfile.TemporaryDirectory() as directory,
        tempfile.TemporaryDirectory() as scratch,
    ):
        for path, text in FILES.items():
            (Path(directory) / path).parent.mkdir(parents=True, exist_ok=True)
            (Path(directory) / path).write_text(text)
        tool = ServiceContractTool(Path(directory))
        indexes = [scip_index(scratch)] if indexed else []
        output = asyncio.run(
            tool.analyze("r", languages={"python"}, scip_indexes=indexes)
        )
        _, routes, _, unread, _ = tool.last_facts
        handlers = sorted(
            (
                r.target_match["qualified_name"].rsplit("::", 1)[-1],
                r.properties["rule_id"],
            )
            for r in output.relationships
            if r.type == "HANDLES"
        )
        return (
            sorted((r.file, r.methods, r.path) for r in routes),
            sorted((u.file, u.line, u.reason) for u in unread),
            handlers,
        )


class RegexPathTest(unittest.TestCase):
    def test_regexes_read_as_django_reverses_them(self):
        self.assertEqual(_regex_path(r"^api/"), "api/")
        self.assertEqual(_regex_path(r"^users/(?P<pk>\d+)/?$"), "users/{var}")
        self.assertEqual(_regex_path(r"^(?:v1/)?items/(\w+)$"), "items/{var}")
        self.assertIsNone(_regex_path(r"^a|b$"))
        self.assertIsNone(_regex_path(r"^[ab]$"))


@unittest.skipUnless(READY, "tree-sitter Python grammar is not installed")
class DjangoUrlconfTest(unittest.TestCase):
    def test_urlconf_entries_are_routes(self):
        routes, unread, handlers = analyze(indexed=True)
        self.assertEqual(
            routes,
            [
                ("shop/urls.py", ("*",), "/api/v1/items/{var}"),
                ("shop/urls.py", ("*",), "/api/v1/more/{var}"),
                ("shop/urls.py", ("*",), "/api/v1/orders/{var}"),
            ],
        )
        self.assertEqual(
            [u for u in unread if u[0].endswith("urls.py")],
            [
                ("shop/urls.py", 8, "path_not_literal"),
                ("site/urls.py", 5, "view_not_resolved"),
            ],
        )
        self.assertEqual(
            [rule for _, rule in handlers],
            ["service-contracts.django-urlconf-route"] * 3,
        )

    def test_without_an_index_none_are_read(self):
        routes, _, _ = analyze(indexed=False)
        self.assertEqual(routes, [])


if __name__ == "__main__":
    unittest.main()
