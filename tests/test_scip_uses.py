"""Uses other than calls, from SCIP references (deepthought.scip_uses)."""

import asyncio
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

from deepthought import indexers
from deepthought.analysis_tools.tree_sitter import TreeSitterTool

PYTHON = """\
class Item:
    def __init__(self):
        self.price = 0


def handler(x):
    return x


HANDLERS = {"a": handler}


def update(item: Item):
    item.price = 3
    item.price += 1
    handler(item)
    return item.price


class Special(Item):
    pass
"""

GO = """\
package shop

type Item struct {
	Price int
}

func handler(x int) int { return x }

var handlers = map[string]func(int) int{"a": handler}

func update(item *Item) int {
	item.Price = 3
	handler(item.Price)
	return item.Price
}

func make() Item {
	return Item{Price: 1}
}
"""


def _edges(root, languages):
    report = indexers.index_workspace(
        root, Path(root).parent / "out", languages=languages
    )
    assert not indexers.failed_files(report), report
    indexes = indexers.ingest_indexed(report, "r", root)
    output = asyncio.run(
        TreeSitterTool(root).analyze(
            "r", languages=list(languages), scip_indexes=indexes
        )
    )
    names = {
        n.properties["qualified_name"]: n.properties.get("name")
        or n.properties.get("path")
        for n in output.nodes
    }
    edges = {
        (
            r.type,
            names.get(r.source_match["qualified_name"], "?"),
            names.get(r.target_match["qualified_name"], "?"),
        ): r.properties
        for r in output.relationships
        if r.type
        in {
            "USES_TYPE",
            "REFERENCES_FUNCTION",
            "READS_FIELD",
            "WRITES_FIELD",
            "CONTAINS",
        }
    }
    return output, edges


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-python"])
    and importlib.util.find_spec("google.protobuf"),
    "Install scip-python and the scip extra",
)
class PythonUsesTest(unittest.TestCase):
    def test_types_function_values_and_fields_become_edges(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "work"
            root.mkdir()
            (root / "shop.py").write_text(PYTHON)
            output, edges = _edges(root, ("python",))
            self.assertIn(("USES_TYPE", "update", "Item"), edges)
            # The dict entry passes the function; the call is a CALLS edge.
            value = edges[("REFERENCES_FUNCTION", "shop.py", "handler")]
            self.assertEqual((value["line"], value["sites"]), (10, 1))
            self.assertEqual(value["rule_id"], "scip.use-function-value")
            self.assertEqual(value["evidence_type"], "resolved")
            self.assertTrue(value["producer"].startswith("scip-python@"))
            self.assertNotIn(("REFERENCES_FUNCTION", "update", "handler"), edges)
            self.assertEqual(
                edges[("WRITES_FIELD", "update", "Item.price")]["sites"], 2
            )
            self.assertEqual(edges[("READS_FIELD", "update", "Item.price")]["sites"], 1)
            self.assertIn(("CONTAINS", "Item", "Item.price"), edges)
            # A named base is the INHERITS_FROM edge's, not a type use.
            self.assertNotIn(("USES_TYPE", "Special", "Item"), edges)
            self.assertNotIn(("USES_TYPE", "shop.py", "Item"), edges)
            self.assertEqual(output.metadata.custom_metrics["scip_fields"], 1)


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-go"])
    and shutil.which("go")
    and importlib.util.find_spec("google.protobuf"),
    "Install scip-go, Go and the scip extra",
)
class GoUsesTest(unittest.TestCase):
    def test_keyed_literal_and_assignment_write_a_field(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "work"
            root.mkdir()
            (root / "go.mod").write_text("module example.com/shop\n\ngo 1.21\n")
            (root / "shop.go").write_text(GO)
            _, edges = _edges(root, ("go",))
            self.assertIn(("USES_TYPE", "update", "Item"), edges)
            self.assertIn(("USES_TYPE", "make", "Item"), edges)
            self.assertIn(("REFERENCES_FUNCTION", "shop.go", "handler"), edges)
            self.assertNotIn(("REFERENCES_FUNCTION", "update", "handler"), edges)
            self.assertIn(("WRITES_FIELD", "update", "Item.Price"), edges)
            self.assertEqual(edges[("READS_FIELD", "update", "Item.Price")]["sites"], 2)
            self.assertIn(("WRITES_FIELD", "make", "Item.Price"), edges)
            self.assertIn(("CONTAINS", "Item", "Item.Price"), edges)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-typescript"])
    and importlib.util.find_spec("google.protobuf"),
    "Install scip-typescript and the scip extra",
)
class SkippedFileUsesTest(unittest.TestCase):
    def test_uses_in_files_the_analysis_left_out_are_not_edges(self):
        # The index's project covers the whole tsconfig, a Next.js page in
        # route-group and dynamic-segment folders included; the analysis
        # skips the page, whose uses then have no File to start from.
        page = "src/(all)/[slug]/index.tsx"
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            for name, text in {
                "package.json": '{"name": "w", "version": "1.0.0"}',
                "tsconfig.json": (
                    '{"compilerOptions": {"strict": true, "module": "esnext",'
                    ' "moduleResolution": "bundler", "jsx": "preserve"},'
                    ' "include": ["src"]}'
                ),
                "src/a.ts": (
                    "export interface Props { slug: string }\n"
                    "export function helper(p: Props) { return p.slug; }\n"
                ),
                page: (
                    'import { Props, helper } from "../../a";\n'
                    "export default function Page(p: Props) {\n"
                    "  const f = helper;\n"
                    "  return f(p);\n"
                    "}\n"
                ),
                "src/main.ts": (
                    'import { helper, Props } from "./a";\n'
                    "const p: Props = { slug: 's' };\n"
                    "export const f = helper;\n"
                ),
            }.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            output = asyncio.run(
                TreeSitterTool(root).analyze(
                    "r", languages=["typescript"], skip_patterns=["src/(all)/*"]
                )
            )
        sources = {
            r.source_match["qualified_name"]
            for r in output.relationships
            if r.type in {"USES_TYPE", "REFERENCES_FUNCTION", "READS_FIELD"}
        }
        self.assertIn("r::src/main.ts::file", sources)
        self.assertFalse(any(page in source for source in sources), sources)
