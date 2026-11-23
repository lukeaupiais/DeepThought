"""Rust declarations (opt-in language)."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought import analyze_repository
from deepthought.analysis_tools import TreeSitterTool

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_rust"
)
SOURCE = """mod outer {
    pub struct Point<T> { x: T }
    pub enum Shape { A, B(u8) }
    pub union U { a: u32 }
    pub trait Draw {
        fn draw(&self);
        fn size() -> u32 { 1 }
        type Out;
        const N: u8;
    }
    type Alias = Point<u8>;
    impl<T> Point<T> {
        pub fn new(x: T) -> Self { fn inner() {} Point { x } }
    }
    impl Draw for Point<u8> { fn draw(&self) {} type Out = u8; const N: u8 = 1; }
    impl<'a> crate::outer::Draw for &'a Shape { fn draw(&self) {} }
    impl Draw for (u8, u8) { fn draw(&self) {} }
    impl Draw for *const U { fn draw(&self) {} }
    macro_rules! mac { () => {} }
    const C: u8 = 1;
    static S: u8 = 2;
    extern "C" { fn ext(); }
    mod file_module;
    mac! { fn hidden() {} }
}
fn r#match() { let c = || 1; helper(); }
impl i32 { fn is_odd(&self) {} }
impl outer::Draw for outer::Shape { fn draw(&self) {} }
"""


def analyze(files, languages=("rust",)):
    with tempfile.TemporaryDirectory() as directory:
        for name, content in files.items():
            path = Path(directory) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        options = {} if languages is None else {"languages": languages}
        return analyze_repository(directory, repository_id="fixture", **options)


@unittest.skipUnless(READY, "Rust grammar is not installed")
class RustTests(unittest.TestCase):
    def test_declarations_methods_by_implementing_type_and_kinds(self):
        result = analyze({"src/lib.rs": SOURCE})
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(
            [(d.line, d.symbol, d.kind) for d in result.definitions],
            [
                (1, "outer", "Module"),
                (2, "outer.Point", "Struct"),
                (3, "outer.Shape", "Enum"),
                (4, "outer.U", "Union"),
                (5, "outer.Draw", "Trait"),
                (6, "outer.Draw.draw", "Method"),
                (7, "outer.Draw.size", "Method"),
                (8, "outer.Draw.Out", "TypeAlias"),
                (11, "outer.Alias", "TypeAlias"),
                (13, "outer.Point.new", "Method"),
                # A function inside a method is a function, named lexically.
                (13, "outer.Point.new.inner", "Function"),
                (15, "outer.Point.draw", "Method"),
                (15, "outer.Point.Out", "TypeAlias"),
                # References, paths and type arguments are dropped.
                (16, "outer.Shape.draw", "Method"),
                # A tuple names no type: the method stays in the module.
                (17, "outer.draw", "Method"),
                (18, "outer.U.draw", "Method"),
                (19, "outer.mac", "Macro"),
                # A foreign function is a function of the enclosing module.
                (22, "outer.ext", "Function"),
                # `mod name;` declares a module whose items are in its file.
                (23, "outer.file_module", "Module"),
                # A raw identifier names the bare word; the macro body is not
                # parsed, so `hidden` is not a declaration.
                (26, "match", "Function"),
                (27, "i32.is_odd", "Method"),
                # A path names its last segment.
                (28, "Shape.draw", "Method"),
            ],
        )
        # Constants, statics, fields, variants and closures are not
        # declarations; calls and uses are not facts for Rust yet.
        self.assertEqual(
            (result.call_sites, result.imports, result.supertypes), ((), (), ())
        )

    def test_rust_is_opt_in_and_tool_projects_nesting(self):
        default = analyze({"src/lib.rs": SOURCE, "a.py": "x = 1\n"}, languages=None)
        self.assertEqual([c.file for c in default.coverage], ["a.py"])
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "lib.rs").write_text(SOURCE)
            output = asyncio.run(
                TreeSitterTool(directory).analyze("r", languages=("rust",))
            )
        names = {n.properties["qualified_name"]: n.properties for n in output.nodes}
        nested = {
            (
                names[r.source_match["qualified_name"]]["name"],
                names[r.target_match["qualified_name"]]["name"],
            )
            for r in output.relationships
            if r.type == "CONTAINS" and "Symbol" in r.source_labels
        }
        self.assertIn(("outer", "outer.Point"), nested)
        self.assertIn(("outer.Draw", "outer.Draw.draw"), nested)
        self.assertIn(("outer.Point.new", "outer.Point.new.inner"), nested)

    def test_partial_parse_keeps_declarations(self):
        result = analyze({"src/lib.rs": "fn ok() {}\nfn broken( {\n"})
        self.assertEqual(result.coverage[0].status, "partial")
        self.assertIn("ok", [d.symbol for d in result.definitions])


if __name__ == "__main__":
    unittest.main()
