"""The types the fields of a carried type hold are carried too: a route
or message destination CARRIES them with ``via`` naming the fields
followed, as the Go type checker, the TypeScript compiler or a scip-python
index resolves each field's type."""

import asyncio
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from typescript_ready import needs_typescript
from go_stubs import needs_go


def _ready(*grammars):
    return importlib.util.find_spec("tree_sitter") and all(
        importlib.util.find_spec(g) for g in grammars
    )


def carried(root, files, **options):
    for name, text in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    output = asyncio.run(ServiceContractTool(Path(root)).analyze("r", **options))
    found = sorted(
        (
            r.target_match["qualified_name"].split("::")[2].rsplit(".", 1)[-1],
            r.properties["direction"],
            tuple(r.properties.get("via", ())),
            r.properties.get("via_field"),
            r.properties["rule_id"],
            r.properties["line"],
        )
        for r in output.relationships
        if r.type == "CARRIES"
    )
    metrics = output.metadata.custom_metrics
    return (
        found,
        metrics["payload_field_types"],
        metrics["payload_field_types_unresolved"],
    )


GO_TYPES = """package api

import "time"

type Owner struct {
\tName string `json:"name"`
\tTeam *Team  `json:"team"`
}

type Team struct {
\tLead  *Owner   `json:"lead"`
\tAt    time.Time `json:"at"`
}

type Audit struct {
\tBy string
}

type Item struct {
\tAudit
\tTitle  string           `json:"title"`
\tOwner  Owner            `json:"owner"`
\tTags   map[string][]Tag `json:"tags"`
\tHidden *Secret          `json:"-"`
\tsecret Secret
\tExtra  any              `json:"extra"`
}

type Tag struct {
\tName string
}

type Secret struct {
\tValue string
}

type Page struct {
\tData  []Item `json:"data"`
\tCount int    `json:"count"`
}
"""

GO_MAIN = """package main

import (
\t"encoding/json"
\t"net/http"

\t"example.com/shop/api"
)

func items(w http.ResponseWriter, r *http.Request) {
\tjson.NewEncoder(w).Encode(api.Page{})
}

func main() {
\thttp.HandleFunc("/items", items)
\thttp.ListenAndServe(":8080", nil)
}
"""


@unittest.skipUnless(_ready("tree_sitter_go"), "tree-sitter grammars are not installed")
@needs_go
class GoFieldTypeTests(unittest.TestCase):
    def test_fields_are_followed_through_the_type_checker(self):
        # Embedded Audit is a field; map values and slices are read
        # through; json:"-" and unexported fields are not written; the
        # library's time.Time is not followed; Team.Lead leads back to
        # Owner, already carried, which ends the cycle; any is unresolved.
        with tempfile.TemporaryDirectory() as directory:
            found, added, unresolved = carried(
                directory,
                {
                    "svc/go.mod": "module example.com/shop\n\ngo 1.22\n",
                    "svc/api/types.go": GO_TYPES,
                    "svc/main.go": GO_MAIN,
                },
            )
        rule = "service-contracts.go-field-type"
        self.assertEqual(
            [f[:5] for f in found],
            [
                ("Audit", "response", ("Page.Data", "Item.Audit"), "Item.Audit", rule),
                ("Item", "response", ("Page.Data",), "Page.Data", rule),
                ("Owner", "response", ("Page.Data", "Item.Owner"), "Item.Owner", rule),
                (
                    "Page",
                    "response",
                    (),
                    None,
                    "service-contracts.go-response-body-type",
                ),
                ("Tag", "response", ("Page.Data", "Item.Tags"), "Item.Tags", rule),
                (
                    "Team",
                    "response",
                    ("Page.Data", "Item.Owner", "Owner.Team"),
                    "Owner.Team",
                    rule,
                ),
            ],
        )
        self.assertEqual(found[1][5], 38)  # the line of Page.Data
        self.assertEqual((added, unresolved), (5, 1))


TS_FILES = {
    "package.json": json.dumps({"dependencies": {"@nestjs/common": "10"}}),
    "tsconfig.json": json.dumps(
        {"compilerOptions": {"experimentalDecorators": True, "baseUrl": "."}}
    ),
    "src/dto.ts": (
        "export interface Named { name: string; }\n"
        "export class Owner implements Named { name: string; teams: Team[]; }\n"
        "export type Team = { lead?: Owner; size: number };\n"
        "export class ItemView extends Base {\n"
        "  static empty: Ignored;\n"
        "  constructor(public readonly owner: Owner | null) { super(); }\n"
        "}\n"
        "export class Base { id: string; }\n"
        "export class Ignored { x: number; }\n"
        "export interface Page<T> { data: T[]; total: number; }\n"
        "export interface ItemsPage extends Page<ItemView> { extra: any; }\n"
    ),
    "src/items.controller.ts": (
        "import { Controller, Get } from '@nestjs/common';\n"
        "import { ItemsPage } from './dto';\n"
        "@Controller('items')\n"
        "export class ItemsController {\n"
        "  @Get()\n"
        "  list(): Promise<ItemsPage> {\n"
        "    return null as any;\n"
        "  }\n"
        "}\n"
    ),
}


@unittest.skipUnless(
    _ready("tree_sitter_typescript"), "tree-sitter grammars are not installed"
)
@needs_typescript
class TypeScriptFieldTypeTests(unittest.TestCase):
    def test_properties_and_heritage_through_the_compiler(self):
        # ItemsPage extends Page<ItemView>: the base and its type argument
        # are carried (name in parentheses). A parameter property is a
        # field; a static one is not; Owner implements Named.
        with tempfile.TemporaryDirectory() as directory:
            found, added, unresolved = carried(directory, TS_FILES)
        rule = "service-contracts.ts-field-type"
        self.assertEqual(
            [f[:4] for f in found if f[4] == rule],
            [
                (
                    "Base",
                    "response",
                    ("ItemsPage(ItemView)", "ItemView(Base)"),
                    "ItemView(Base)",
                ),
                (
                    "ItemView",
                    "response",
                    ("ItemsPage(ItemView)",),
                    "ItemsPage(ItemView)",
                ),
                (
                    "Named",
                    "response",
                    ("ItemsPage(ItemView)", "ItemView.owner", "Owner(Named)"),
                    "Owner(Named)",
                ),
                (
                    "Owner",
                    "response",
                    ("ItemsPage(ItemView)", "ItemView.owner"),
                    "ItemView.owner",
                ),
                ("Page", "response", ("ItemsPage(Page)",), "ItemsPage(Page)"),
                (
                    "Team",
                    "response",
                    ("ItemsPage(ItemView)", "ItemView.owner", "Owner.teams"),
                    "Owner.teams",
                ),
            ],
        )
        self.assertEqual((added, unresolved), (6, 1))


SCHEMAS = """from typing import ClassVar
from pydantic import BaseModel

class Owner(BaseModel):
    name: str

class ItemBase(BaseModel):
    title: str
    owner: Owner | None = None

class ItemPublic(ItemBase):
    id: int
    registry: ClassVar[Registry]

class Registry:
    pass

class ItemsPublic(BaseModel):
    data: list[ItemPublic]
    count: int
    later: Missing
"""

MAIN = """from fastapi import FastAPI
from .schemas import ItemsPublic

app = FastAPI()

@app.get("/items", response_model=ItemsPublic)
def items():
    return None
"""

PY_FILES = {"app/__init__.py": "", "app/schemas.py": SCHEMAS, "app/main.py": MAIN}

STDLIB = "scip-python python python-stdlib 3.11 "
PACKAGE = "scip-python python app 0 `app.schemas`/"
PY_SYMBOLS = {
    "str": STDLIB + "builtins/str#",
    "int": STDLIB + "builtins/int#",
    "list": STDLIB + "builtins/list#",
    "ClassVar": STDLIB + "typing/ClassVar.",
    "BaseModel": "scip-python python pydantic 2.9.0 `pydantic.main`/BaseModel#",
    "Missing": "local 0",
    **{
        name: PACKAGE + name + "#"
        for name in ("Owner", "ItemBase", "ItemPublic", "Registry", "ItemsPublic")
    },
}


def index(directory, files, symbols):
    """A scip-python index recording every written name of ``symbols``; a
    class statement's name is its definition."""
    import deepthought
    from deepthought._scip import scip_pb2 as wire

    built = wire.Index()
    built.metadata.tool_info.name = "scip-python"
    built.metadata.tool_info.version = "0.6.8"
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, symbols)) + r")\b")
    for path, source in files.items():
        document = built.documents.add(relative_path=path)
        for line, written in enumerate(source.split("\n")):
            for match in pattern.finditer(written):
                defines = written.startswith("class " + match.group(1))
                document.occurrences.add(
                    symbol=symbols[match.group(1)],
                    range=[line, match.start(), match.end()],
                    symbol_roles=wire.Definition if defines else 0,
                )
    path = Path(directory) / ".index.scip"
    path.write_bytes(built.SerializeToString())
    return deepthought.ingest_scip(path, repository_id="r"), "sha256:test"


@unittest.skipUnless(
    _ready("tree_sitter_python"), "tree-sitter grammars are not installed"
)
class PythonFieldTypeTests(unittest.TestCase):
    def test_annotated_attributes_and_bases_through_the_index(self):
        # A ClassVar is not a field; Missing resolves nowhere.
        with tempfile.TemporaryDirectory() as directory:
            found, added, unresolved = carried(
                directory,
                PY_FILES,
                languages=["python"],
                scip_indexes=(index(directory, PY_FILES, PY_SYMBOLS),),
            )
        rule = "service-contracts.python-field-type"
        self.assertEqual(
            [f[:5] for f in found],
            [
                (
                    "ItemBase",
                    "response",
                    ("ItemsPublic.data", "ItemPublic(ItemBase)"),
                    "ItemPublic(ItemBase)",
                    rule,
                ),
                (
                    "ItemPublic",
                    "response",
                    ("ItemsPublic.data",),
                    "ItemsPublic.data",
                    rule,
                ),
                (
                    "ItemsPublic",
                    "response",
                    (),
                    None,
                    "service-contracts.fastapi-response-type",
                ),
                (
                    "Owner",
                    "response",
                    ("ItemsPublic.data", "ItemPublic(ItemBase)", "ItemBase.owner"),
                    "ItemBase.owner",
                    rule,
                ),
            ],
        )
        self.assertEqual((added, unresolved), (3, 1))


if __name__ == "__main__":
    unittest.main()
