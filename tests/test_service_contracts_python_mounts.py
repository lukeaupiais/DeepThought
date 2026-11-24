"""Python route prefixes composed across files from router mounts."""

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
    # FastAPI: app -> api_router (relative module import) -> items router.
    "shop/app/main.py": """
from fastapi import FastAPI
from app.api.router import api_router

app = FastAPI()
app.include_router(api_router, prefix="/shop")
""",
    "shop/app/__init__.py": "",
    "shop/app/api/__init__.py": "",
    "shop/app/api/router.py": """
from fastapi import APIRouter
from .routes import items
from .routes.admin import router as admin_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(items.router)
api_router.include_router(admin_router, prefix="/admin")
api_router.include_router(admin_router, prefix="/staff")
""",
    "shop/app/api/routes/__init__.py": "",
    "shop/app/api/routes/items.py": """
from fastapi import APIRouter

router = APIRouter(prefix="/items")

@router.get("/{item_id}")
def item(item_id):
    pass
""",
    "shop/app/api/routes/admin.py": """
from fastapi import APIRouter

router = APIRouter()

@router.delete("/cache")
def clear():
    pass
""",
    # Re-exported through a package __init__; never mounted router keeps
    # its own prefix.
    "shop/app/api/extra/__init__.py": "from .stats import router\n",
    "shop/app/api/extra/stats.py": """
from fastapi import APIRouter

router = APIRouter(prefix="/stats")
orphan = APIRouter(prefix="/orphan")

@router.get("")
def stats():
    pass

@orphan.get("/x")
def x():
    pass
""",
    "shop/app/extra_mount.py": """
from app.main import app
from app.api import extra

app.include_router(extra.router, prefix="/more")
""",
    # Flask: url_prefix at registration replaces the blueprint's own; nested
    # blueprints join the parent's prefix.
    "web/views.py": """
from flask import Blueprint

orders = Blueprint("orders", __name__, url_prefix="/ignored")
lines = Blueprint("lines", __name__, url_prefix="/lines")

@orders.route("/<int:order_id>")
def order(order_id):
    pass

@lines.get("/<int:line_id>")
def line(line_id):
    pass

orders.register_blueprint(lines)
""",
    "web/app.py": """
from flask import Flask
from views import orders

def create_app():
    app = Flask(__name__)
    app.register_blueprint(orders, url_prefix="/orders")
    return app
""",
}


@unittest.skipUnless(READY, "tree-sitter-python is not installed")
class PythonMountTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        for name, text in FILES.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.tool = ServiceContractTool(root)
        asyncio.run(self.tool.analyze("r"))

    def tearDown(self):
        self.directory.cleanup()

    def test_routes(self):
        _, routes, _, _, _ = self.tool.last_facts
        found = sorted(
            (r.file.rpartition("/")[2], " ".join(r.methods), r.path) for r in routes
        )
        self.assertEqual(
            found,
            [
                # Mounted twice: one route per mount.
                ("admin.py", "DELETE", "/shop/api/v1/admin/cache"),
                ("admin.py", "DELETE", "/shop/api/v1/staff/cache"),
                ("items.py", "GET", "/shop/api/v1/items/{item_id}"),
                ("stats.py", "GET", "/more/stats"),
                ("stats.py", "GET", "/orphan/x"),
                ("views.py", "GET", "/orders/lines/{var}"),
                ("views.py", "GET", "/orders/{var}"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
