"""Rust declarations (opt-in language)."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought import analyze_repository
from deepthought.imports import (
    RUST_CRATE_ROOT,
    RUST_EXTERN_CRATE,
    RUST_MODULE_FILE,
    RUST_USE_DECLARED,
    RUST_USE_MODULE,
    resolve_imports,
)
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
        # declarations; calls, uses and impls are not facts for Rust yet.
        self.assertEqual((result.call_sites, result.supertypes), ((), ()))
        self.assertEqual(
            [(i.line, i.specifier, i.kind) for i in result.imports],
            [(23, "outer/file_module", "module")],
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

    def test_module_declarations_name_their_files(self):
        result = analyze(
            {
                "src/lib.rs": '#[doc = "elsewhere.rs"]\nmod a;\nmod b;\nmod gone;\nmod both;\n'
                "mod outer {\n    mod inner;\n"
                '    #[path = "x.rs"]\n    mod in_path;\n}\n'
                '#[cfg(unix)]\n#[path = "sys/unix.rs"]\nmod sys;\n'
                "mod r#type;\nmod body {}\n",
                "src/a.rs": 'mod child;\n#[path = "../up.rs"]\nmod up;\n',
                "up.rs": "",
                "src/a/child.rs": "",
                "src/b/mod.rs": "mod grand;\n",
                "src/b/grand.rs": "",
                "src/both.rs": "",
                "src/both/mod.rs": "",
                "src/outer/inner.rs": "",
                "src/outer/x.rs": "",
                "src/sys/unix.rs": "",
                "src/type.rs": "",
                "src/bin/tool.rs": "mod helper;\n",
                "src/bin/helper.rs": "",
                "build.rs": '#[path = "src/a.rs"]\nmod shared;\nmod generated;\n',
                "generated.rs": "",
                "tests/it.rs": "mod common;\n",
                "tests/common.rs": "mod data;\n",
                "tests/common/data.rs": "",
                "src/runtime/mod.rs": "mod tests;\n",
                "src/runtime/tests/mod.rs": "mod loom;\n",
                "src/runtime/tests/loom.rs": "mod queue;\n",
                "src/runtime/tests/loom/queue.rs": "",
                "src/runtime/tests/queue.rs": "",
                "src/deep.rs": 'mod p {\n    mod q {\n        #[path = "first.rs"]\n        #[path = "second.rs"]\n        mod r;\n    }\n}\n',
                "src/deep/p/q/first.rs": "",
                "src/deep/p/q/second.rs": "",
            }
        )
        facts = {
            (i.file, i.line): (i.specifier, i.kind, i.names, i.local_names)
            for i in result.imports
        }
        self.assertEqual(
            facts[("src/lib.rs", 7)], ("outer/inner", "module", (), ("inner",))
        )
        self.assertEqual(
            facts[("src/lib.rs", 9)], ("x.rs", "module_path", ("outer",), ("in_path",))
        )
        self.assertEqual(
            facts[("src/lib.rs", 13)], ("sys/unix.rs", "module_path", (), ("sys",))
        )
        self.assertEqual(facts[("src/lib.rs", 14)], ("type", "module", (), ("type",)))
        # A module with a body includes no file.
        self.assertNotIn(("src/lib.rs", 15), facts)
        imports = {i.id: i for i in result.imports}
        outcomes = {
            (imports[r.import_id].file, imports[r.import_id].local_names[0]): (
                r.status,
                r.reason,
                [link.target_file for link in r.links] or list(r.candidates),
                {link.rule_id for link in r.links},
            )
            for r in resolve_imports(result)
        }
        linked = {
            key: value[2][0]
            for key, value in outcomes.items()
            if value[0] == "linked" and value[3] == {RUST_MODULE_FILE}
        }
        self.assertEqual(
            linked,
            {
                ("src/lib.rs", "a"): "src/a.rs",
                ("src/lib.rs", "b"): "src/b/mod.rs",
                # Another file's modules are in the directory named after it;
                # mod.rs is the module's directory itself.
                ("src/a.rs", "child"): "src/a/child.rs",
                ("src/b/mod.rs", "grand"): "src/b/grand.rs",
                # Inline modules are directories; #[path] inside them is read
                # from there, outside them from the declaring file's directory.
                ("src/lib.rs", "inner"): "src/outer/inner.rs",
                ("src/lib.rs", "in_path"): "src/outer/x.rs",
                ("src/lib.rs", "sys"): "src/sys/unix.rs",
                ("src/lib.rs", "type"): "src/type.rs",
                ("build.rs", "shared"): "src/a.rs",
                ("build.rs", "generated"): "generated.rs",
                # A path may leave the directory; the first #[path] counts.
                ("src/a.rs", "up"): "up.rs",
                ("src/deep.rs", "r"): "src/deep/p/q/first.rs",
                # Files in src/bin and in tests outside src are crate roots;
                # a test file another declares is that module instead.
                ("src/bin/tool.rs", "helper"): "src/bin/helper.rs",
                ("tests/it.rs", "common"): "tests/common.rs",
                ("tests/common.rs", "data"): "tests/common/data.rs",
                # A tests directory inside src is an ordinary module.
                ("src/runtime/mod.rs", "tests"): "src/runtime/tests/mod.rs",
                ("src/runtime/tests/mod.rs", "loom"): "src/runtime/tests/loom.rs",
                ("src/runtime/tests/loom.rs", "queue"): (
                    "src/runtime/tests/loom/queue.rs"
                ),
            },
        )
        self.assertEqual(
            outcomes[("src/lib.rs", "both")][:3],
            ("ambiguous", "module_file_ambiguous", ["src/both.rs", "src/both/mod.rs"]),
        )
        self.assertEqual(
            outcomes[("src/lib.rs", "gone")][:3],
            ("unresolved", "module_file_missing", []),
        )
        self.assertEqual(len(outcomes), len(linked) + 2)

    def test_undeclared_test_file_is_a_crate_root(self):
        # Without the declaring file, tests/common.rs is its own crate root.
        result = analyze(
            {
                "tests/common.rs": "mod data;\n",
                "tests/data.rs": "",
                "tests/common/data.rs": "",
                "examples/demo.rs": "mod part;\n",
                "examples/part.rs": "",
                "examples/demo/part.rs": "",
                # A tests directory inside src holds modules, not crates.
                "src/x/tests/orphan.rs": "mod q;\n",
                "src/x/tests/q.rs": "",
                "src/x/tests/orphan/q.rs": "",
                "src/bin/tool.rs": "mod helper;\n",
                "src/bin/helper.rs": "",
                "src/bin/tool/helper.rs": "",
            }
        )
        self.assertEqual(
            sorted(
                link.target_file for r in resolve_imports(result) for link in r.links
            ),
            [
                "examples/part.rs",
                "src/bin/helper.rs",
                "src/x/tests/orphan/q.rs",
                "tests/data.rs",
            ],
        )

    def test_cargo_manifests_name_crate_roots(self):
        result = analyze(
            {
                "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n',
                "crates/core/Cargo.toml": "[package]\n"
                'name = "my-core"\n\n'
                "[[bin]]\n"
                'name = "by-layout"\n\n'
                "[[bin]]\n"
                'name = "my-tool"\npath = "cli/main.rs"\n\n'
                "[[test]]\n"
                'name = "all"\npath = "../../tests/all.rs"\n',
                "crates/core/src/lib.rs": "",
                "crates/core/cli/main.rs": "mod args;\n",
                "crates/core/cli/args.rs": "",
                "tests/all.rs": "",
                "crates/util/Cargo.toml": "# Utilities.\n"
                '[package]\nname = "util-x"\n'
                '[lib]\nname = "ux"\npath = "lib/util.rs"\n',
                # A file a target names is a crate root: its modules are
                # beside it, even though it is not named lib.rs.
                "crates/util/lib/util.rs": "mod inner;\n",
                "crates/util/lib/inner.rs": "",
                "crates/util/lib/util/inner.rs": "",
                "binonly/Cargo.toml": '[package]\nname = "binonly"\n',
                "binonly/src/main.rs": "",
                "bad/Cargo.toml": "[package\n",
            }
        )
        coverage = {c.file: c.status for c in result.coverage}
        self.assertEqual(
            [coverage[f] for f in ("Cargo.toml", "bad/Cargo.toml")],
            ["analyzed", "partial"],
        )
        self.assertIn(
            ("syntax_error", "bad/Cargo.toml"),
            [(d.code, d.file) for d in result.diagnostics],
        )
        crates = [
            (i.file, i.line, i.specifier, i.kind, i.names, i.local_names)
            for i in result.imports
            if i.kind.startswith("crate_")
        ]
        self.assertEqual(
            crates,
            [
                (
                    "binonly/Cargo.toml",
                    1,
                    "src/lib.rs",
                    "crate_library",
                    ("binonly", "lib"),
                    ("binonly",),
                ),
                # Without [lib], the library is src/lib.rs, named by the
                # package with - as _, located at [package].
                (
                    "crates/core/Cargo.toml",
                    1,
                    "src/lib.rs",
                    "crate_library",
                    ("my-core", "lib"),
                    ("my_core",),
                ),
                # The second [[bin]]: one without a path is found by layout,
                # not written.
                (
                    "crates/core/Cargo.toml",
                    7,
                    "cli/main.rs",
                    "crate_target",
                    ("my-core", "bin"),
                    ("my_tool",),
                ),
                (
                    "crates/core/Cargo.toml",
                    11,
                    "../../tests/all.rs",
                    "crate_target",
                    ("my-core", "test"),
                    ("all",),
                ),
                (
                    "crates/util/Cargo.toml",
                    4,
                    "lib/util.rs",
                    "crate_library",
                    ("util-x", "lib"),
                    ("ux",),
                ),
            ],
        )
        imports = {i.id: i for i in result.imports}
        outcomes = {
            (imports[r.import_id].file, imports[r.import_id].specifier): (
                r.status,
                r.reason,
                [(link.target_file, link.rule_id) for link in r.links],
            )
            for r in resolve_imports(result)
        }
        self.assertEqual(
            outcomes,
            {
                ("binonly/Cargo.toml", "src/lib.rs"): (
                    "unresolved",
                    "crate_root_missing",
                    [],
                ),
                ("crates/core/Cargo.toml", "src/lib.rs"): (
                    "linked",
                    None,
                    [("crates/core/src/lib.rs", RUST_CRATE_ROOT)],
                ),
                ("crates/core/Cargo.toml", "cli/main.rs"): (
                    "linked",
                    None,
                    [("crates/core/cli/main.rs", RUST_CRATE_ROOT)],
                ),
                ("crates/core/Cargo.toml", "../../tests/all.rs"): (
                    "linked",
                    None,
                    [("tests/all.rs", RUST_CRATE_ROOT)],
                ),
                ("crates/util/Cargo.toml", "lib/util.rs"): (
                    "linked",
                    None,
                    [("crates/util/lib/util.rs", RUST_CRATE_ROOT)],
                ),
                ("crates/core/cli/main.rs", "args"): (
                    "linked",
                    None,
                    [("crates/core/cli/args.rs", RUST_MODULE_FILE)],
                ),
                ("crates/util/lib/util.rs", "inner"): (
                    "linked",
                    None,
                    [("crates/util/lib/inner.rs", RUST_MODULE_FILE)],
                ),
            },
        )
        # Manifests are read only when Rust is selected.
        other = analyze({"Cargo.toml": '[package]\nname = "x"\n'}, ("python",))
        self.assertEqual((other.coverage, other.imports), ((), ()))

    def test_use_declarations_resolve_through_module_trees(self):
        result = analyze(
            {
                "util/Cargo.toml": '[package]\nname = "my-util"\n',
                "util/src/lib.rs": "pub mod shapes;\n"
                "pub struct Top;\n"
                "pub use shapes::Circle;\n"
                "mod inner { pub mod deeper { pub fn f() {} } }\n"
                "pub fn regex() {}\n",
                "util/src/shapes.rs": "pub struct Circle;\n"
                "pub enum Kind { A }\n"
                "pub const MAX: u8 = 1;\n",
                "app/Cargo.toml": '[package]\nname = "app"\n'
                '[[bin]]\nname = "tool"\npath = "src/bin/tool.rs"\n',
                "app/src/bin/tool.rs": "pub struct Thing;\n",
                "app/tests/it.rs": "use app::Mode;\n",
                "dup1/Cargo.toml": '[package]\nname = "dup"\n',
                "dup1/src/lib.rs": "",
                "dup2/Cargo.toml": '[package]\nname = "dup"\n',
                "dup2/src/lib.rs": "",
                # A file that includes itself: module paths stop there.
                "app/src/cyc.rs": '#[path = "cyc.rs"]\nmod again;\n',
                "app/src/unix.rs": "",
                "app/src/windows.rs": "",
                "app/src/lib.rs": "mod a;\n"
                "mod b { use super::a::Thing; pub mod c; }\n"
                "mod gone;\n"
                "mod shared;\n"
                "use my_util::shapes::Circle;\n"
                "use my_util::Circle as C2;\n"
                "use my_util::shapes::Kind::A;\n"
                "use my_util::shapes::MAX;\n"
                "use ::my_util::Top;\n"
                "use crate::a::{self, Thing};\n"
                "use self::a::*;\n"
                "use my_util::inner::deeper::f;\n"
                "use std::fmt;\n"
                "use a::Thing as T2;\n"
                "use crate::gone::X;\n"
                "extern crate my_util as u;\n"
                "use super::Above;\n"
                "fn regex() {}\n"
                "use regex::Regex;\n"
                "pub enum Mode { Fast }\n"
                "use Mode::Fast;\n"
                "use tool::Thing as T3;\n"
                "use dup::X;\n"
                '#[cfg(unix)]\n#[path = "unix.rs"]\nmod imp;\n'
                '#[cfg(windows)]\n#[path = "windows.rs"]\nmod imp;\n'
                "use imp::Sys;\n"
                "fn body() { mod m { use self::Z; } }\n"
                "mod cyc;\n"
                "use cyc::again::again::Deeper;\n",
                "app/src/a.rs": "pub struct Thing;\nuse super::b::c::Deep;\n",
                "app/src/b/c.rs": "pub struct Deep;\nuse super::super::a::Thing;\n",
                "app/src/main.rs": "mod shared;\nuse app::b::c::Deep;\n",
                "app/src/shared.rs": "use crate::a::Thing;\n",
                "app/src/orphan.rs": "use crate::a::Thing;\n",
            }
        )
        imports = {i.id: i for i in result.imports}
        self.assertEqual(
            [
                (i.line, i.specifier, i.kind, i.names, i.local_names)
                for i in result.imports
                if i.file == "app/src/lib.rs" and i.line < 3 and i.kind == "use"
            ],
            [(2, "super::a::Thing", "use", ("b",), ("Thing",))],
        )
        outcomes = {
            (imports[r.import_id].file, imports[r.import_id].line): (
                r.status,
                r.reason,
                [(link.target_file, link.rule_id) for link in r.links]
                or list(r.candidates),
            )
            for r in resolve_imports(result)
            if imports[r.import_id].kind in {"use", "use_glob", "extern_crate"}
        }
        declared, module = RUST_USE_DECLARED, RUST_USE_MODULE
        self.maxDiff = None
        self.assertEqual(
            outcomes,
            {
                # super from an inline module, relative to it.
                ("app/src/lib.rs", 2): (
                    "linked",
                    None,
                    [("app/src/a.rs", declared)],
                ),
                # A workspace crate by its library name.
                ("app/src/lib.rs", 5): (
                    "linked",
                    None,
                    [("util/src/shapes.rs", declared)],
                ),
                # Not declared where the path names it (a re-export): the
                # module the path goes through.
                ("app/src/lib.rs", 6): (
                    "linked",
                    None,
                    [("util/src/lib.rs", module)],
                ),
                # A path through an enum: the enum is declared there.
                ("app/src/lib.rs", 7): (
                    "linked",
                    None,
                    [("util/src/shapes.rs", declared)],
                ),
                # Constants are not declarations here.
                ("app/src/lib.rs", 8): (
                    "linked",
                    None,
                    [("util/src/shapes.rs", module)],
                ),
                ("app/src/lib.rs", 9): (
                    "linked",
                    None,
                    [("util/src/lib.rs", declared)],
                ),
                # `crate::a::{self, Thing}` is two uses on one line; both
                # name a.rs. The glob names the module.
                ("app/src/lib.rs", 10): (
                    "linked",
                    None,
                    [("app/src/a.rs", declared)],
                ),
                ("app/src/lib.rs", 11): (
                    "linked",
                    None,
                    [("app/src/a.rs", declared)],
                ),
                # Inline modules are modules of their file.
                ("app/src/lib.rs", 12): (
                    "linked",
                    None,
                    [("util/src/lib.rs", declared)],
                ),
                ("app/src/lib.rs", 13): ("unresolved", "external_crate", []),
                # A first segment naming a local module.
                ("app/src/lib.rs", 14): (
                    "linked",
                    None,
                    [("app/src/a.rs", declared)],
                ),
                # A module whose file is missing.
                ("app/src/lib.rs", 15): ("unresolved", "module_missing", []),
                ("app/src/lib.rs", 16): (
                    "linked",
                    None,
                    [("util/src/lib.rs", RUST_EXTERN_CRATE)],
                ),
                ("app/src/lib.rs", 17): ("unresolved", "super_outside_crate", []),
                # A local function does not start a path: the crate does.
                ("app/src/lib.rs", 19): ("unresolved", "external_crate", []),
                # A local type starts a path.
                ("app/src/lib.rs", 21): (
                    "linked",
                    None,
                    [("app/src/lib.rs", declared)],
                ),
                # Binaries are not crates a path can name.
                ("app/src/lib.rs", 22): ("unresolved", "external_crate", []),
                # Two workspace libraries with one name.
                ("app/src/lib.rs", 23): (
                    "ambiguous",
                    "crate_ambiguous",
                    ["dup1/src/lib.rs", "dup2/src/lib.rs"],
                ),
                # A module declared once per #[cfg], in two files.
                ("app/src/lib.rs", 30): (
                    "ambiguous",
                    "module_file_ambiguous",
                    ["app/src/unix.rs", "app/src/windows.rs"],
                ),
                # Modules inside function bodies are not followed.
                ("app/src/lib.rs", 31): ("unresolved", "module_missing", []),
                # A file is visited once per crate: cyc's `again` (cyc.rs
                # itself) is not followed, and is a name declared in cyc.rs.
                ("app/src/lib.rs", 33): (
                    "linked",
                    None,
                    [("app/src/cyc.rs", declared)],
                ),
                # A test file outside src is a crate root of its own.
                ("app/tests/it.rs", 1): (
                    "linked",
                    None,
                    [("app/src/lib.rs", declared)],
                ),
                ("app/src/a.rs", 2): (
                    "linked",
                    None,
                    [("app/src/b/c.rs", declared)],
                ),
                ("app/src/b/c.rs", 2): (
                    "linked",
                    None,
                    [("app/src/a.rs", declared)],
                ),
                # A binary uses its package's library by name.
                ("app/src/main.rs", 2): (
                    "linked",
                    None,
                    [("app/src/b/c.rs", declared)],
                ),
                # A file in two crates whose use resolves differently in each.
                ("app/src/shared.rs", 1): (
                    "ambiguous",
                    "use_context_ambiguous",
                    ["app/src/a.rs", "app/src/main.rs"],
                ),
                ("util/src/lib.rs", 3): (
                    "linked",
                    None,
                    [("util/src/shapes.rs", declared)],
                ),
                # A file no crate includes.
                ("app/src/orphan.rs", 1): (
                    "unresolved",
                    "module_context_unknown",
                    [],
                ),
            },
        )


if __name__ == "__main__":
    unittest.main()
