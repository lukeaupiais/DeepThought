"""Go interface implementations recorded by scip-go, end to end through
TreeSitterTool."""

import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path

from deepthought import indexers
from deepthought.go_interfaces import GO_INTERFACE_METHOD_SET
from deepthought.scip_implements import SCIP_GO_IMPLEMENTATION

FILES = {
    "go.mod": "module example.com/app\n\ngo 1.22\n",
    "store/store.go": (
        "package store\n\n"
        "type Item struct{ Key string }\n\n"
        "type Getter interface{ Get(k string) Item }\n\n"
        "type Store interface {\n\tGetter\n\tPut(i Item)\n}\n\n"
        "type Sizer interface{ Size() int }\n\n"
        "type Mem struct{}\n\n"
        "func (*Mem) Get(k string) Item { return Item{k} }\n\n"
        "func (*Mem) Put(i Item) {}\n"
    ),
    # An in-package test: scip-go 0.2.7 reads store's interfaces from its
    # test variant.
    "store/store_test.go": (
        'package store\n\nimport "testing"\n\nfunc TestMem(t *testing.T) {}\n'
    ),
    "disk/disk.go": (
        "package disk\n\n"
        'import "example.com/app/store"\n\n'
        "type Disk struct{}\n\n"
        "func (*Disk) Get(k string) store.Item { return store.Item{} }\n\n"
        "func (*Disk) Put(i store.Item) {}\n\n"
        "func (Disk) Size() int { return 0 }\n\n"
        # Promoted methods: the method-set rule does not count them.
        "type Cached struct{ *Disk }\n\n"
        "func Pick(v any) bool {\n"
        "\ttype closer interface{ Size() int }\n"
        "\t_, ok := v.(closer)\n"
        "\treturn ok\n"
        "}\n"
    ),
    "log/log.go": (
        "package log\n\n"
        "type Writer struct{}\n\n"
        # Implements io.Writer, declared outside the repository.
        "func (Writer) Write(p []byte) (int, error) { return len(p), nil }\n"
    ),
}


@unittest.skipUnless(
    indexers.locate(indexers.INDEXERS["scip-go"])
    and importlib.util.find_spec("tree_sitter_go"),
    "Install scip-go and the languages extra",
)
class ScipGoImplementationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from deepthought.analysis_tools.tree_sitter import TreeSitterTool

        with tempfile.TemporaryDirectory() as scratch:
            for path, text in FILES.items():
                target = Path(scratch) / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
            cls.output = asyncio.run(
                TreeSitterTool(scratch).analyze("repo", languages={"go"})
            )
        cls.names = {
            n.properties["qualified_name"]: n.properties.get("name")
            for n in cls.output.nodes
        }

    def edges(self, kind):
        return sorted(
            (
                self.names[r.source_match["qualified_name"]],
                self.names[r.target_match["qualified_name"]],
                r.properties["rule_id"],
            )
            for r in self.output.relationships
            if r.type == kind
        )

    def test_types_implement_what_the_type_checker_says(self):
        scip, methods = SCIP_GO_IMPLEMENTATION, GO_INTERFACE_METHOD_SET
        self.assertEqual(
            self.edges("IMPLEMENTS"),
            [
                # Promoted from the embedded *Disk. (Cached also implements
                # Getter and Store, which neither rule records: scip-go for
                # the test variant, the method-set rule for promotion.)
                ("Cached", "Sizer", scip),
                # Read by scip-go from store's test variant, where store.Item
                # is another type: the method-set rule stands.
                ("Disk", "Getter", methods),
                # Declared inside a function, which scip-go does not index.
                ("Disk", "Pick.closer", methods),
                ("Disk", "Sizer", scip),
                ("Disk", "Store", methods),
                ("Mem", "Getter", scip),
                ("Mem", "Store", scip),
            ],
        )

    def test_interface_methods_dispatch_to_the_methods_calls_run(self):
        self.assertIn(
            ("Getter.Get", "Mem.Get", SCIP_GO_IMPLEMENTATION),
            self.edges("DISPATCHES_TO"),
        )
        self.assertIn(
            ("Sizer.Size", "Disk.Size", SCIP_GO_IMPLEMENTATION),
            self.edges("DISPATCHES_TO"),
        )
        self.assertIn(
            ("Store.Put", "Disk.Put", GO_INTERFACE_METHOD_SET),
            self.edges("DISPATCHES_TO"),
        )

    def test_links_name_the_indexer(self):
        edge = next(
            r
            for r in self.output.relationships
            if r.properties.get("rule_id") == SCIP_GO_IMPLEMENTATION
        )
        self.assertTrue(edge.properties["producer"].startswith("scip-go@"))
        self.assertEqual(edge.properties["evidence_type"], "resolved")
        self.assertEqual(
            self.output.metadata.custom_metrics["go_implementations"],
            {GO_INTERFACE_METHOD_SET: 3, SCIP_GO_IMPLEMENTATION: 4},
        )


if __name__ == "__main__":
    unittest.main()
