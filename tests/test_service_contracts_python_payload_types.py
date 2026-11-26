"""Types a FastAPI path operation reads its request body as and writes its
response from, resolved through a scip-python index: the route CARRIES
their declarations."""

import asyncio
import importlib.util
from pathlib import Path
import re
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_python"
)

SCHEMAS = """from enum import Enum
from dataclasses import dataclass
from pydantic import BaseModel

class Status(str, Enum):
    open = "open"

class OrderIn(BaseModel):
    item: str

class Order(OrderIn):
    id: int

@dataclass
class Note:
    text: str

class Plain:
    pass
"""

MAIN = """from typing import Annotated, Optional
from fastapi import FastAPI, Depends, Body, Query
from . import schemas
from .schemas import OrderIn, Status, Note, Plain

app = FastAPI()

def db():
    return None

@app.post("/orders/{order_id}", response_model=schemas.Order)
def create(order_id: int, order: OrderIn, status: Status = Query(None), s: Plain = Depends(db), note: Annotated[Note, Body(alias="memo")] = None):
    return None

@app.get("/orders")
def listed(status: Optional[Status] = None, plain: Plain = None) -> list[schemas.Order]:
    return []

@app.put("/orders/{order_id}", response_model=None)
def replace(order_id: int, order: Annotated[OrderIn, Body(embed=True)]) -> schemas.Order:
    return None
"""

FILES = {"app/__init__.py": "", "app/schemas.py": SCHEMAS, "app/main.py": MAIN}

STDLIB = "scip-python python python-stdlib 3.11 "
PACKAGE = "scip-python python app 0 `app.schemas`/"
FASTAPI = "scip-python python fastapi 0.115.0 `fastapi.param_functions`/"
SYMBOLS = {
    "Enum": STDLIB + "enum/Enum#",
    "dataclass": STDLIB + "dataclasses/dataclass().",
    "str": STDLIB + "builtins/str#",
    "int": STDLIB + "builtins/int#",
    "list": STDLIB + "builtins/list#",
    "Annotated": STDLIB + "typing/Annotated.",
    "Optional": STDLIB + "typing/Optional.",
    "BaseModel": "scip-python python pydantic 2.9.0 `pydantic.main`/BaseModel#",
    "Depends": FASTAPI + "Depends().",
    "Body": FASTAPI + "Body().",
    "Query": FASTAPI + "Query().",
    "Status": PACKAGE + "Status#",
    "OrderIn": PACKAGE + "OrderIn#",
    "Order": PACKAGE + "Order#",
    "Note": PACKAGE + "Note#",
    "Plain": PACKAGE + "Plain#",
}


def index(directory, symbols):
    """A scip-python index of FILES recording every written name of
    ``symbols``; a class statement's name is its definition."""
    import deepthought
    from deepthought._scip import scip_pb2 as wire

    built = wire.Index()
    built.metadata.tool_info.name = "scip-python"
    built.metadata.tool_info.version = "0.6.8"
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, symbols)) + r")\b")
    for path, source in FILES.items():
        document = built.documents.add(relative_path=path)
        for line, written in enumerate(source.split("\n")):
            for match in pattern.finditer(written):
                defines = path == "app/schemas.py" and written.startswith(
                    "class " + match.group(1)
                )
                document.occurrences.add(
                    symbol=symbols[match.group(1)],
                    range=[line, match.start(), match.end()],
                    symbol_roles=wire.Definition if defines else 0,
                )
    path = Path(directory) / ".index.scip"
    path.write_bytes(built.SerializeToString())
    return deepthought.ingest_scip(path, repository_id="r"), "sha256:test"


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class FastApiPayloadTypeTests(unittest.TestCase):
    def carried(self, symbols):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, source in FILES.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source, encoding="utf-8")
            indexes = () if symbols is None else (index(directory, symbols),)
            output = asyncio.run(
                ServiceContractTool(root).analyze(
                    "r", languages=["python"], scip_indexes=indexes
                )
            )
        handlers = {
            r.target_match["qualified_name"]: r.source_match["qualified_name"]
            for r in output.relationships
            if r.type == "HANDLES"
        }
        found = sorted(
            (
                handlers[r.source_match["qualified_name"]].split("::")[-2],
                r.target_match["qualified_name"].split("::")[2],
                r.properties["direction"],
                r.properties.get("body_key"),
                r.properties["rule_id"],
            )
            for r in output.relationships
            if r.type == "CARRIES"
        )
        return found, output.metadata.custom_metrics["payload_types_unresolved"]

    def test_bodies_and_responses_follow_fastapi(self):
        # create: two bodies (order, note), so each is embedded under its
        # name or alias; status is a query enum, s a dependency. listed: an
        # enum and a plain class are query parameters. replace: embed=True
        # keys the one body, and response_model=None leaves the return.
        found, unresolved = self.carried(SYMBOLS)
        request = "service-contracts.fastapi-body-type"
        response = "service-contracts.fastapi-response-type"
        self.assertEqual(
            found,
            [
                ("create", "Note", "request", "memo", request),
                ("create", "Order", "response", None, response),
                ("create", "OrderIn", "request", "order", request),
                ("listed", "Order", "response", None, response),
                ("replace", "OrderIn", "request", "order", request),
            ],
        )
        self.assertEqual(unresolved, 0)

    def test_without_fastapi_and_pydantic_resolved_nothing_is_guessed(self):
        # The packages were not installed when indexing: their names are
        # local symbols, so markers and bases cannot be told.
        symbols = {
            **SYMBOLS,
            "BaseModel": "local 0",
            "Depends": "local 1",
            "Body": "local 2",
            "Query": "local 3",
        }
        found, unresolved = self.carried(symbols)
        self.assertEqual(
            [f[:3] for f in found],
            [
                ("create", "Order", "response"),
                ("listed", "Order", "response"),
            ],
        )
        self.assertGreater(unresolved, 0)

    def test_an_import_the_index_did_not_resolve_is_unresolved(self):
        # schemas.Order resolved nowhere: a local symbol, not a library's.
        found, unresolved = self.carried({**SYMBOLS, "Order": "local 4"})
        self.assertNotIn("Order", [f[1] for f in found])
        self.assertEqual(unresolved, 2)  # create and listed

    def test_without_an_index_nothing_is_carried(self):
        found, unresolved = self.carried(None)
        self.assertEqual(found, [])
        self.assertGreater(unresolved, 0)


if __name__ == "__main__":
    unittest.main()
