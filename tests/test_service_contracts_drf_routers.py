"""Django REST framework router routes through the scip-python index: the
routes SimpleRouter generates for each registered ViewSet's actions, read
from its class and the DRF classes among its bases, under the URLconf
pattern that includes ``router.urls``."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_python"
)

FILES = {
    "shop/__init__.py": "",
    "shop/views.py": """from rest_framework import mixins, viewsets
from rest_framework.decorators import action

class Base(viewsets.GenericViewSet, mixins.ListModelMixin):
    pass

class Tags(Base):
    pass

class Items(viewsets.ModelViewSet):
    def retrieve(self, request, pk=None):
        pass

    @action(detail=True, methods=["post"], url_path="upload-image")
    def upload(self, request, pk=None):
        pass

class Odd(Unknown):
    pass
""",
    "shop/urls.py": """from django.urls import include, path
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register("tags", views.Tags)
router.register("items", views.Items)
router.register("odd", views.Odd)

urlpatterns = [path("api/", include(router.urls))]
""",
}

DRF = "scip-python python djangorestframework 3.15.2 "
DJANGO = "scip-python python Django 5.1.0 `django.urls.conf`/"
APP = "scip-python python app 0 "
VIEWS = APP + "`shop.views`/"
ROUTER = APP + "`shop.urls`/router."

# (0-based line, name, nth occurrence on the line, symbol, is definition)
OCCURRENCES = {
    "shop/views.py": [
        (3, "Base", 0, VIEWS + "Base#", True),
        (
            3,
            "GenericViewSet",
            0,
            DRF + "`rest_framework.viewsets`/GenericViewSet#",
            False,
        ),
        (
            3,
            "ListModelMixin",
            0,
            DRF + "`rest_framework.mixins`/ListModelMixin#",
            False,
        ),
        (6, "Tags", 0, VIEWS + "Tags#", True),
        (6, "Base", 0, VIEWS + "Base#", False),
        (9, "Items", 0, VIEWS + "Items#", True),
        (9, "ModelViewSet", 0, DRF + "`rest_framework.viewsets`/ModelViewSet#", False),
        (13, "action", 0, DRF + "`rest_framework.decorators`/action().", False),
        (17, "Odd", 0, VIEWS + "Odd#", True),
    ],
    "shop/urls.py": [
        (4, "router", 0, ROUTER, True),
        (4, "DefaultRouter", 0, DRF + "`rest_framework.routers`/DefaultRouter#", False),
        (5, "router", 0, ROUTER, False),
        (
            5,
            "register",
            0,
            DRF + "`rest_framework.routers`/BaseRouter#register().",
            False,
        ),
        (5, "Tags", 0, VIEWS + "Tags#", False),
        (6, "router", 0, ROUTER, False),
        (
            6,
            "register",
            0,
            DRF + "`rest_framework.routers`/BaseRouter#register().",
            False,
        ),
        (6, "Items", 0, VIEWS + "Items#", False),
        (7, "router", 0, ROUTER, False),
        (
            7,
            "register",
            0,
            DRF + "`rest_framework.routers`/BaseRouter#register().",
            False,
        ),
        (7, "Odd", 0, VIEWS + "Odd#", False),
        (9, "path", 0, DJANGO + "path.", False),
        (9, "include", 0, DJANGO + "include().", False),
        (9, "router", 0, ROUTER, False),
        (9, "urls", 0, DRF + "`rest_framework.routers`/BaseRouter#urls().", False),
    ],
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
        result, routes, _, unread, _ = tool.last_facts
        names = {d.id: d.symbol.rpartition(".")[2] for d in result.definitions}
        rules = {
            r.properties["rule_id"] for r in output.relationships if r.type == "HANDLES"
        }
        return (
            sorted(
                (r.path, r.methods, names.get(r.handler_id))
                for r in routes
                if r.framework == "django-rest-framework"
            ),
            sorted((u.file, u.line, u.reason) for u in unread if u.kind == "route"),
            rules,
        )


@unittest.skipUnless(READY, "tree-sitter Python grammar is not installed")
class DrfRouterTest(unittest.TestCase):
    def test_registered_viewsets_have_the_routes_of_their_actions(self):
        routes, unread, rules = analyze(indexed=True)
        self.assertEqual(
            routes,
            [
                ("/api/items", ("GET", "POST"), "Items"),
                ("/api/items/{var}", ("GET",), "retrieve"),
                ("/api/items/{var}", ("PUT", "PATCH", "DELETE"), "Items"),
                ("/api/items/{var}/upload-image", ("POST",), "upload"),
                ("/api/tags", ("GET",), "Tags"),
            ],
        )
        self.assertIn(("shop/urls.py", 8, "viewset_base_not_resolved"), unread)
        self.assertEqual(rules, {"service-contracts.drf-router-route"})

    def test_without_an_index_none_are_read(self):
        routes, _, _ = analyze(indexed=False)
        self.assertEqual(routes, [])


if __name__ == "__main__":
    unittest.main()
