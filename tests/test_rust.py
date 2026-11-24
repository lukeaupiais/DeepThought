"""Rust declarations (opt-in language)."""

import asyncio
import dataclasses
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
    RUST_USE_EXPORTED_MACRO,
    RUST_USE_MODULE,
    RUST_USE_REEXPORT,
    resolve_imports,
)
from deepthought.analysis_tools import TreeSitterTool
from deepthought.calls import (
    RUST_CALL_ASSOCIATED,
    RUST_CALL_LOCAL_ITEM,
    RUST_CALL_PATH,
    RUST_CALL_SELF_METHOD,
    resolve_calls,
)
from deepthought.inheritance import (
    RUST_IMPL_TRAIT,
    RUST_IMPL_TYPE,
    RUST_SUPERTRAIT,
    resolve_rust_impls,
    resolve_supertypes,
)
from deepthought.models import AnalysisResult

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
        # declarations; impls are their own facts, not supertypes.
        self.assertEqual(result.supertypes, ())
        self.assertEqual(
            [(i.line, i.type, i.trait, i.generics) for i in result.impls],
            [
                (12, "Point", None, ("T",)),
                (15, "Point", "Draw", ()),
                (16, "Shape", "crate::outer::Draw", ()),
                # A tuple and a primitive type are not paths.
                (18, "U", "Draw", ()),
                (28, "outer::Shape", "outer::Draw", ()),
            ],
        )
        self.assertEqual(
            [(c.callee, c.line) for c in result.call_sites], [("helper", 26)]
        )
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
                "pub fn regex() {}\n"
                "pub mod prelude;\npub mod x;\npub mod y;\npub mod both;\n"
                "pub mod one;\n"
                "mod a1;\nmod a2;\n"
                "pub use shapes as shp;\n"
                "pub mod hub { pub use super::shapes::Kind; }\n",
                "util/src/prelude.rs": "pub use crate::shapes::*;\n"
                "pub use crate::inner::deeper;\n"
                "pub use crate::Circle as Round;\n"
                "pub use crate::shp::sub::S as S2;\n"
                "pub use crate::shapes::MAX;\n",
                "util/src/x.rs": "pub use crate::y::Loop;\n"
                "fn hidden() { use crate::Top as Hid; use crate::shapes::sub::*; }\n"
                "struct P;\nimpl P { fn m() { use crate::Top as Hid2; extern crate app as Ext; } }\n",
                "util/src/y.rs": "pub use crate::x::Loop;\n",
                "util/src/both.rs": "pub use crate::a1::*;\npub use crate::a2::*;\n",
                "util/src/one.rs": "pub use crate::a1::*;\n"
                "pub use crate::shapes::*;\n"
                "pub use crate::a2::Same;\n",
                "util/src/a1.rs": "pub struct Same;\n",
                "util/src/a2.rs": "pub struct Same;\n",
                "util/src/shapes.rs": "pub struct Circle;\n"
                "pub enum Kind { A }\n"
                "pub const MAX: u8 = 1;\n"
                "pub mod sub;\n",
                "util/src/shapes/sub.rs": "pub struct S;\n",
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
                "use cyc::again::again::Deeper;\n"
                "use my_util::prelude::Kind;\n"
                "use my_util::prelude::deeper::f as f2;\n"
                "use my_util::prelude::Round;\n"
                "use my_util::x::Loop;\n"
                "use my_util::both::Same;\n"
                "use my_util::prelude::Kind::A as A2;\n"
                "pub extern crate my_util as mu;\n"
                "use mu::Top as T4;\n"
                "use my_util::prelude::sub::S;\n"
                "use my_util::prelude::S2 as S3;\n"
                "use my_util::hub::Kind as K2;\n"
                "use my_util::one::Same as Same2;\n"
                "use my_util::one::Circle as C9;\n"
                "use self::imp as imp_alias;\n"
                "mod dual;\n"
                "fn dual() {}\n"
                "use crate::dual::Bar;\n"
                "use my_util::prelude::MAX as M2;\n"
                "use my_util::x::Hid;\n"
                "use my_util::x::S;\n"
                "use my_util::x::Hid2;\n"
                "use my_util::x::Ext;\n",
                "app/src/dual.rs": "pub struct Bar;\n",
                "app/src/a.rs": "pub struct Thing;\nuse super::b::c::Deep;\n",
                "app/src/b/c.rs": "pub struct Deep;\nuse super::super::a::Thing;\n",
                "app/src/main.rs": "mod shared;\nuse app::b::c::Deep;\n"
                "use app::mu::shapes::Circle;\n",
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
            # Re-exporting helpers, checked through the uses of them.
            and imports[r.import_id].file
            not in {
                f"util/src/{name}.rs" for name in ("prelude", "x", "y", "both", "one")
            }
        }
        declared, module = RUST_USE_DECLARED, RUST_USE_MODULE
        reexport = RUST_USE_REEXPORT
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
                # A re-export, followed to the declaration.
                ("app/src/lib.rs", 6): (
                    "linked",
                    None,
                    [("util/src/shapes.rs", reexport)],
                ),
                # A path through an enum: the enum is declared there.
                ("app/src/lib.rs", 7): (
                    "linked",
                    None,
                    [("util/src/shapes.rs", declared)],
                ),
                # Constants are not declarations here: the module the path
                # goes through.
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
                # A module declared once per #[cfg], in two files: both, as
                # every configuration is analyzed.
                ("app/src/lib.rs", 30): (
                    "linked",
                    None,
                    [("app/src/unix.rs", module), ("app/src/windows.rs", module)],
                ),
                # Modules inside function bodies are not followed.
                ("app/src/lib.rs", 31): ("unresolved", "module_missing", []),
                # A file is visited once per crate: cyc's `again` (cyc.rs
                # itself) is not a module again.
                ("app/src/lib.rs", 33): (
                    "linked",
                    None,
                    [("app/src/cyc.rs", module)],
                ),
                # Through a glob re-export.
                ("app/src/lib.rs", 34): (
                    "linked",
                    None,
                    [("util/src/shapes.rs", reexport)],
                ),
                # Through a re-exported module, then on down.
                ("app/src/lib.rs", 35): (
                    "linked",
                    None,
                    [("util/src/lib.rs", reexport)],
                ),
                # A chain of re-exports, renamed on the way.
                ("app/src/lib.rs", 36): (
                    "linked",
                    None,
                    [("util/src/shapes.rs", reexport)],
                ),
                # Re-exports in a cycle are not followed.
                ("app/src/lib.rs", 37): (
                    "linked",
                    None,
                    [("util/src/x.rs", module)],
                ),
                # Two globs that both supply a name can only both compile
                # under different #[cfg]s: both are followed.
                ("app/src/lib.rs", 38): (
                    "linked",
                    None,
                    [("util/src/a1.rs", reexport), ("util/src/a2.rs", reexport)],
                ),
                # A name inside a re-exported item is declared with it.
                ("app/src/lib.rs", 39): (
                    "linked",
                    None,
                    [("util/src/shapes.rs", reexport)],
                ),
                ("app/src/lib.rs", 40): (
                    "linked",
                    None,
                    [("util/src/lib.rs", RUST_EXTERN_CRATE)],
                ),
                # A crate renamed by `extern crate` in the crate root.
                ("app/src/lib.rs", 41): (
                    "linked",
                    None,
                    [("util/src/lib.rs", declared)],
                ),
                # A module a glob re-exports, then on down to its own file.
                ("app/src/lib.rs", 42): (
                    "linked",
                    None,
                    [("util/src/shapes/sub.rs", reexport)],
                ),
                # A re-export whose own path goes through a re-exported
                # module.
                ("app/src/lib.rs", 43): (
                    "linked",
                    None,
                    [("util/src/shapes/sub.rs", reexport)],
                ),
                # A re-export inside an inline module, read from there.
                ("app/src/lib.rs", 44): (
                    "linked",
                    None,
                    [("util/src/shapes.rs", reexport)],
                ),
                # An explicit use shadows globs that supply the name.
                ("app/src/lib.rs", 45): (
                    "linked",
                    None,
                    [("util/src/a2.rs", reexport)],
                ),
                # Of two globs, the one that supplies the name.
                ("app/src/lib.rs", 46): (
                    "linked",
                    None,
                    [("util/src/shapes.rs", reexport)],
                ),
                # A module declared once per #[cfg] names both files.
                ("app/src/lib.rs", 47): (
                    "linked",
                    None,
                    [("app/src/unix.rs", declared), ("app/src/windows.rs", declared)],
                ),
                # A re-exported constant: the module the re-export names.
                ("app/src/lib.rs", 51): (
                    "linked",
                    None,
                    [("util/src/shapes.rs", module)],
                ),
                # A use in a function body binds nothing in the module.
                ("app/src/lib.rs", 52): (
                    "linked",
                    None,
                    [("util/src/x.rs", module)],
                ),
                ("app/src/lib.rs", 53): (
                    "linked",
                    None,
                    [("util/src/x.rs", module)],
                ),
                ("app/src/lib.rs", 54): (
                    "linked",
                    None,
                    [("util/src/x.rs", module)],
                ),
                ("app/src/lib.rs", 55): (
                    "linked",
                    None,
                    [("util/src/x.rs", module)],
                ),
                # A path goes on through the module, not the function, of a
                # name that is both.
                ("app/src/lib.rs", 50): (
                    "linked",
                    None,
                    [("app/src/dual.rs", declared)],
                ),
                # A crate re-exported by `pub extern crate`, then on down.
                ("app/src/main.rs", 3): (
                    "linked",
                    None,
                    [("util/src/shapes.rs", reexport)],
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
                ("util/src/lib.rs", 13): (
                    "linked",
                    None,
                    [("util/src/shapes.rs", declared)],
                ),
                ("util/src/lib.rs", 14): (
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

    def test_item_macro_bodies_are_read_as_items(self):
        files = {
            "src/macros.rs": "macro_rules! cfg_a {\n"
            '    ($($item:item)*) => { $( #[cfg(feature = "a")] $item )* }\n'
            "}\n"
            "macro_rules! cfg_b {\n"
            "    (#![$m:meta] $($i:item)*) => {\n"
            "        $( #[cfg($m)] #[allow(unused)] $i )*\n"
            "    }\n"
            "}\n"
            "macro_rules! two { ($($i:item)*) => { $($i)* }; () => {} }\n"
            "macro_rules! wrap { ($($i:item)*) => { mod w { $($i)* } } }\n"
            "macro_rules! split { ($($i:item)*) => { $($i)* } }\n"
            "macro_rules! extra { ($($i:item)*) => { $($i)* fn more() {} } }\n",
            "src/other.rs": "macro_rules! split { ($e:expr) => {} }\n",
            "src/lib.rs": "#[macro_use]\nmod macros;\nmod other;\n"
            "cfg_a! {\n"
            "    mod inside;\n"
            "    pub fn shown() {}\n"
            "    pub use inside::Thing;\n"
            "}\n"
            "mod m {\n"
            "    cfg_b! {\n"
            "        #![unix]\n"
            "        pub fn deep() {}\n"
            "        cfg_a! { pub fn deeper() {} }\n"
            "        mod sub;\n"
            "        pub use self::sub::S;\n"
            "    }\n"
            "}\n"
            "two! { pub fn not_two() {} }\n"
            "wrap! { pub fn not_wrapped() {} }\n"
            "split! { pub fn not_split() {} }\n"
            "fn body() { cfg_a! { fn in_fn() {} } }\n"
            "crate::cfg_a! { pub fn via_path() {} }\n"
            "extra! { pub fn not_extra() {} }\n"
            "cfg_a! { pub fn kept() {} pub fn broken( }\n",
            "src/m/sub.rs": "pub struct S;\n",
            "src/inside.rs": "pub struct Thing;\n",
        }
        result = analyze(files)
        self.assertEqual(
            dict(result.analyzer_versions)["rust-item-macros"], "cfg_a,cfg_b"
        )
        self.assertEqual(
            sorted(
                (d.symbol, d.line, d.column)
                for d in result.definitions
                if d.file == "src/lib.rs" and d.kind != "Macro"
            ),
            [
                ("body", 21, 0),
                ("inside", 5, 4),
                ("m", 9, 0),
                ("m.deep", 12, 8),
                ("m.deeper", 13, 17),
                ("m.sub", 14, 8),
                ("macros", 2, 0),
                ("other", 3, 0),
                ("shown", 6, 4),
                ("via_path", 22, 16),
            ],
        )
        imports = {i.id: i for i in result.imports}
        self.assertEqual(
            sorted(
                (
                    imports[r.import_id].line,
                    imports[r.import_id].names,
                    [(link.target_file, link.rule_id) for link in r.links],
                )
                for r in resolve_imports(result)
                if imports[r.import_id].file == "src/lib.rs"
            ),
            [
                (2, (), [("src/macros.rs", RUST_MODULE_FILE)]),
                (3, (), [("src/other.rs", RUST_MODULE_FILE)]),
                (5, (), [("src/inside.rs", RUST_MODULE_FILE)]),
                (7, (), [("src/inside.rs", RUST_USE_DECLARED)]),
                (14, (), [("src/m/sub.rs", RUST_MODULE_FILE)]),
                (15, ("m",), [("src/m/sub.rs", RUST_USE_DECLARED)]),
            ],
        )
        # Declaration text is read from the expanded body too.
        with tempfile.TemporaryDirectory() as directory:
            for name, content in files.items():
                path = Path(directory) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            output = asyncio.run(
                TreeSitterTool(directory).analyze("r", languages=("rust",))
            )
            sources = {
                n.properties["name"]: n.properties.get("source_code")
                for n in output.nodes
                if "Symbol" in n.labels
            }
            self.assertEqual(sources["m.deeper"], "pub fn deeper() {}")
            # A changed macro set parses every Rust file again.
            (Path(directory) / "src/other.rs").write_text(
                "macro_rules! cfg_a { ($e:expr) => {} }\n"
            )
            again = analyze_repository(
                directory,
                repository_id="fixture",
                languages=("rust",),
                previous=result,
            )
        self.assertEqual(
            dict(again.analyzer_versions)["rust-item-macros"], "cfg_b,split"
        )
        symbols = [d.symbol for d in again.definitions]
        self.assertNotIn("shown", symbols)
        self.assertIn("not_split", symbols)

    def test_item_macros_come_from_the_selected_sources(self):
        through = "macro_rules! m { ($($i:item)*) => { $($i)* } }\n"
        other = "macro_rules! m { ($e:expr) => {} }\n"
        with tempfile.TemporaryDirectory() as directory:
            for name, content in {
                "src/lib.rs": through,
                "skip/a.rs": other,
                "src/notes.txt": other,
                "other/b.rs": other,
                "big/c.rs": other + "// " + "x" * 200 + "\n",
            }.items():
                path = Path(directory) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            result = analyze_repository(
                directory,
                repository_id="fixture",
                languages=("rust",),
                include=("src/*", "skip/*", "big/*"),
                exclude=("skip/*",),
                max_file_bytes=100,
            )
        # Files that are not Rust, excluded, not included or oversized define
        # no macros.
        self.assertEqual(dict(result.analyzer_versions)["rust-item-macros"], "m")

    def test_exported_macros_belong_to_the_crate_root(self):
        result = analyze(
            {
                "util/Cargo.toml": '[package]\nname = "my-util"\n',
                "util/src/lib.rs": "mod macros;\n"
                "mod inner { #[macro_export] macro_rules! deep { () => {} } }\n"
                "pub use crate::shout as again;\n",
                "util/src/macros.rs": "#[macro_export]\n/// Doc.\n"
                "macro_rules! shout { () => {} }\n"
                "#[allow(unused)]\nmacro_rules! quiet { () => {} }\n"
                "#[macro_export(local_inner_macros)]\n"
                "#[doc(hidden)]\n"
                "macro_rules! loud { () => {} }\n",
                "app/Cargo.toml": '[package]\nname = "app"\n',
                "app/src/lib.rs": "mod m;\n"
                "use my_util::shout;\n"
                "use my_util::quiet;\n"
                "use my_util::deep;\n"
                "use my_util::loud;\n"
                "use crate::local;\n"
                "use my_util::again;\n"
                "use crate::m::local as l2;\n"
                "use my_util::shout::nope;\n"
                "use my_util::inner::shout as s3;\n",
                "app/src/m.rs": "#[macro_export]\nmacro_rules! local { () => {} }\n",
            }
        )
        self.assertEqual(
            sorted(
                (d.file, d.symbol, d.macro_export)
                for d in result.definitions
                if d.kind == "Macro"
            ),
            [
                ("app/src/m.rs", "local", True),
                ("util/src/lib.rs", "inner.deep", True),
                ("util/src/macros.rs", "loud", True),
                ("util/src/macros.rs", "quiet", False),
                ("util/src/macros.rs", "shout", True),
            ],
        )
        # Only Rust macros record the attribute.
        self.assertEqual(
            {d.macro_export is None for d in result.definitions if d.kind != "Macro"},
            {True},
        )
        imports = {i.id: i for i in result.imports}
        exported = RUST_USE_EXPORTED_MACRO
        self.assertEqual(
            sorted(
                (
                    imports[r.import_id].line,
                    [(link.target_file, link.rule_id) for link in r.links],
                )
                for r in resolve_imports(result)
                if imports[r.import_id].file == "app/src/lib.rs"
                and imports[r.import_id].kind == "use"
            ),
            [
                (2, [("util/src/macros.rs", exported)]),
                # A macro without the attribute is not at the root.
                (3, [("util/src/lib.rs", RUST_USE_MODULE)]),
                # Exported from inside an inline module.
                (4, [("util/src/lib.rs", exported)]),
                (5, [("util/src/macros.rs", exported)]),
                (6, [("app/src/m.rs", exported)]),
                (7, [("util/src/macros.rs", RUST_USE_REEXPORT)]),
                # Where it is written, it is declared as before.
                (8, [("app/src/m.rs", RUST_USE_DECLARED)]),
                # Only the last name of a path, and only at the root.
                (9, [("util/src/lib.rs", RUST_USE_MODULE)]),
                (10, [("util/src/lib.rs", RUST_USE_MODULE)]),
            ],
        )
        # The attribute is serialized for Rust macros only.
        data = result.to_dict()["definitions"]
        self.assertEqual(
            sorted(d["symbol"] for d in data if "macro_export" in d),
            ["inner.deep", "local", "loud", "quiet", "shout"],
        )
        self.assertEqual(
            type(result).from_json(result.to_json()).definitions, result.definitions
        )
        with self.assertRaises(ValueError):
            dataclasses.replace(result.definitions[0], macro_export="yes")

    def test_a_file_in_two_crates_is_read_in_each_crate_alone(self):
        # tool.rs is the bin crate's root and the lib crate's module `tool`.
        result = analyze(
            {
                "Cargo.toml": '[package]\nname = "p"\nversion = "0.1.0"\n'
                '[lib]\npath = "src/lib.rs"\n'
                '[[bin]]\nname = "tool"\npath = "src/bin/tool.rs"\n',
                "src/lib.rs": '#[path = "bin/tool.rs"]\npub mod tool;\n'
                "pub trait Thing {}\n",
                "src/bin/tool.rs": "use crate::Thing;\npub fn f() {}\n"
                "pub mod tool { pub fn f() {} }\n"
                "pub fn run() { f(); Thing(); }\n",
            }
        )
        symbols = {d.id: d.symbol for d in result.definitions}
        sites = {c.id: c for c in result.call_sites}
        self.assertEqual(
            sorted(
                (sites[r.call_id].callee, symbols.get(r.target_id), r.reason)
                for r in resolve_calls(result)
            ),
            [
                # In the lib crate the trait is no function; in the bin
                # crate the use names nothing. The declared item's reason
                # is the one reported.
                ("Thing", None, "not_declared"),
                # Not `tool.f`, which is `f` read in the bin crate's context
                # with the lib crate's module path.
                ("f", "f", None),
            ],
        )

    def test_calls_resolve_through_module_trees(self):
        result = analyze(
            {
                "util/Cargo.toml": '[package]\nname = "my-util"\n',
                "util/src/lib.rs": "pub mod shapes;\npub fn top() {}\n"
                "pub use shapes::area as size;\n"
                "pub mod both;\npub use both::both;\n",
                "util/src/both.rs": "pub fn both() {}\npub fn other() {}\n",
                "util/src/shapes.rs": "pub fn area() {}\npub struct P(u8);\n"
                "pub enum K { A(u8) }\npub struct Unit;\n"
                "pub trait Speak {}\npub fn Speak() {}\n"
                "impl Unit { pub fn new() -> Self { Unit } }\n",
                "app/Cargo.toml": '[package]\nname = "app"\n',
                "app/src/lib.rs": "mod a;\n"
                "use my_util::shapes::{self, P};\n"
                "use my_util::shapes::*;\n"
                "fn local() {}\n"
                "fn caller(x: Unit) {\n"
                "    fn inner() {}\n"
                "    inner();\n"
                "    local();\n"
                "    a::helper();\n"
                "    crate::a::helper();\n"
                "    self::a::helper();\n"
                "    my_util::top();\n"
                "    ::my_util::size();\n"
                "    shapes::area();\n"
                "    P(1);\n"
                "    K::A(1);\n"
                "    Unit::new();\n"
                "    x.clone();\n"
                "    Some(1);\n"
                "    std::mem::drop(1);\n"
                "    a::missing();\n"
                "    Self::new();\n"
                "    { use my_util::top as t; t(); }\n"
                "    { use crate::a::*; helper(); }\n"
                "    { use std::mem::drop as local; local(); }\n"
                "    <u8 as Default>::default();\n"
                "    area::<u8>();\n"
                "}\n"
                "mod m { fn g() { super::local(); local(); } }\n"
                "#[cfg(unix)]\nfn twice() {}\n#[cfg(windows)]\nfn twice() {}\n"
                "fn more() { twice(); }\n"
                "fn last() { my_util::both::other(); }\n"
                "use my_util::both::other as o2;\nfn after() { o2(); }\n"
                "mod b;\n",
                "app/src/b.rs": "fn caller() {\n"
                "    fn helper() {}\n"
                "    enum top {}\n"
                "    super::a::helper();\n"
                "    super::local();\n"
                "    { use my_util::top as t; use super::a::helper; t(); }\n"
                "    { use super::a::*; top(); }\n"
                "    { use super::a as aa; aa::helper(); }\n"
                "    { use my_util::shapes::K as KK; KK::A(1); }\n"
                "    self::nothing();\n"
                "    my_util::shapes::Speak();\n"
                "    my_util::shapes::Unit::<u8>::new();\n"
                "    ::my_util();\n"
                "}\n"
                "fn top() {}\n"
                "mod m { pub mod n { fn h() { super::super::top(); } } }\n"
                "fn holder() { mod inner { use crate::a::helper; fn k() { j(); } } }\n",
                "app/src/a.rs": "pub fn helper() {}\n",
                "loose.rs": "fn f() { g(); }\nfn g() {}\n",
            }
        )
        names = {d.id: (d.file, d.symbol) for d in result.definitions}
        sites = {c.id: c for c in result.call_sites}
        path, local = RUST_CALL_PATH, RUST_CALL_LOCAL_ITEM
        self.maxDiff = None
        self.assertEqual(
            sorted(
                (
                    sites[r.call_id].file,
                    sites[r.call_id].line,
                    sites[r.call_id].callee,
                    r.status,
                    names.get(r.target_id),
                    r.rule_id or r.reason,
                )
                for r in resolve_calls(result)
                if sites[r.call_id].file != "util/src/shapes.rs"
            ),
            [
                # A path, not the local item of the same name.
                (
                    "app/src/b.rs",
                    4,
                    "super::a::helper",
                    "linked",
                    ("app/src/a.rs", "helper"),
                    path,
                ),
                (
                    "app/src/b.rs",
                    5,
                    "super::local",
                    "linked",
                    ("app/src/lib.rs", "local"),
                    path,
                ),
                # Each use binds its own name.
                ("app/src/b.rs", 6, "t", "linked", ("util/src/lib.rs", "top"), path),
                # A local enum is not callable, and a glob that does not
                # supply the name leaves the module's.
                ("app/src/b.rs", 7, "top", "linked", ("app/src/b.rs", "top"), path),
                (
                    "app/src/b.rs",
                    8,
                    "aa::helper",
                    "linked",
                    ("app/src/a.rs", "helper"),
                    path,
                ),
                ("app/src/b.rs", 9, "KK::A", "unresolved", None, "associated_item"),
                (
                    "app/src/b.rs",
                    10,
                    "self::nothing",
                    "unresolved",
                    None,
                    "not_declared",
                ),
                # A function sharing its name with a trait.
                (
                    "app/src/b.rs",
                    11,
                    "my_util::shapes::Speak",
                    "linked",
                    ("util/src/shapes.rs", "Speak"),
                    path,
                ),
                (
                    "app/src/b.rs",
                    12,
                    "my_util::shapes::Unit::new",
                    "linked",
                    ("util/src/shapes.rs", "Unit.new"),
                    RUST_CALL_ASSOCIATED,
                ),
                ("app/src/b.rs", 13, "::my_util", "unresolved", None, "not_declared"),
                # From nested inline modules.
                (
                    "app/src/b.rs",
                    16,
                    "super::super::top",
                    "linked",
                    ("app/src/b.rs", "top"),
                    path,
                ),
                # A module inside a function body is not reached.
                ("app/src/b.rs", 17, "j", "unresolved", None, "module_missing"),
                (
                    "app/src/lib.rs",
                    7,
                    "inner",
                    "linked",
                    ("app/src/lib.rs", "caller.inner"),
                    local,
                ),
                (
                    "app/src/lib.rs",
                    8,
                    "local",
                    "linked",
                    ("app/src/lib.rs", "local"),
                    path,
                ),
                (
                    "app/src/lib.rs",
                    9,
                    "a::helper",
                    "linked",
                    ("app/src/a.rs", "helper"),
                    path,
                ),
                (
                    "app/src/lib.rs",
                    10,
                    "crate::a::helper",
                    "linked",
                    ("app/src/a.rs", "helper"),
                    path,
                ),
                (
                    "app/src/lib.rs",
                    11,
                    "self::a::helper",
                    "linked",
                    ("app/src/a.rs", "helper"),
                    path,
                ),
                (
                    "app/src/lib.rs",
                    12,
                    "my_util::top",
                    "linked",
                    ("util/src/lib.rs", "top"),
                    path,
                ),
                # Through a re-export.
                (
                    "app/src/lib.rs",
                    13,
                    "::my_util::size",
                    "linked",
                    ("util/src/shapes.rs", "area"),
                    path,
                ),
                (
                    "app/src/lib.rs",
                    14,
                    "shapes::area",
                    "linked",
                    ("util/src/shapes.rs", "area"),
                    path,
                ),
                # A tuple struct.
                (
                    "app/src/lib.rs",
                    15,
                    "P",
                    "linked",
                    ("util/src/shapes.rs", "P"),
                    path,
                ),
                # A variant and an associated function: names inside a type.
                ("app/src/lib.rs", 16, "K::A", "unresolved", None, "associated_item"),
                (
                    "app/src/lib.rs",
                    17,
                    "Unit::new",
                    "linked",
                    ("util/src/shapes.rs", "Unit.new"),
                    RUST_CALL_ASSOCIATED,
                ),
                ("app/src/lib.rs", 18, "x.clone", "unresolved", None, "method_call"),
                ("app/src/lib.rs", 19, "Some", "unresolved", None, "unbound_name"),
                (
                    "app/src/lib.rs",
                    20,
                    "std::mem::drop",
                    "unresolved",
                    None,
                    "external_crate",
                ),
                (
                    "app/src/lib.rs",
                    21,
                    "a::missing",
                    "unresolved",
                    None,
                    "not_declared",
                ),
                (
                    "app/src/lib.rs",
                    22,
                    "Self::new",
                    "unresolved",
                    None,
                    "associated_item",
                ),
                # Uses in a body bind there, before the module's names.
                ("app/src/lib.rs", 23, "t", "linked", ("util/src/lib.rs", "top"), path),
                (
                    "app/src/lib.rs",
                    24,
                    "helper",
                    "linked",
                    ("app/src/a.rs", "helper"),
                    path,
                ),
                ("app/src/lib.rs", 25, "local", "unresolved", None, "external_crate"),
                # A generic function's path; a qualified path is not recorded.
                (
                    "app/src/lib.rs",
                    27,
                    "area",
                    "linked",
                    ("util/src/shapes.rs", "area"),
                    path,
                ),
                ("app/src/lib.rs", 29, "local", "unresolved", None, "unbound_name"),
                (
                    "app/src/lib.rs",
                    29,
                    "super::local",
                    "linked",
                    ("app/src/lib.rs", "local"),
                    path,
                ),
                # A function declared once per #[cfg].
                (
                    "app/src/lib.rs",
                    34,
                    "twice",
                    "ambiguous",
                    None,
                    "multiple_declarations",
                ),
                # `both` is a module and a function; the path goes on through
                # the module.
                (
                    "app/src/lib.rs",
                    35,
                    "my_util::both::other",
                    "linked",
                    ("util/src/both.rs", "other"),
                    path,
                ),
                (
                    "app/src/lib.rs",
                    37,
                    "o2",
                    "linked",
                    ("util/src/both.rs", "other"),
                    path,
                ),
                ("loose.rs", 1, "g", "unresolved", None, "module_context_unknown"),
            ],
        )
        # A use in a block records the block it binds in.
        self.assertEqual(
            sorted(
                (i.line, i.specifier, i.block)
                for i in result.imports
                if i.file == "app/src/lib.rs" and i.block is not None
            ),
            [
                (23, "my_util::top", (23, 4, 23, 35)),
                (24, "crate::a", (24, 4, 24, 34)),
                (25, "std::mem::drop", (25, 4, 25, 45)),
            ],
        )
        # A module in a function body holds the uses written in it.
        self.assertEqual(
            [i.block for i in result.imports if i.file == "app/src/b.rs"][-1], None
        )
        data = result.to_dict()["imports"]
        self.assertEqual(sum("block" in i for i in data), 8)
        self.assertEqual(type(result).from_json(result.to_json()), result)
        with self.assertRaises(ValueError):
            dataclasses.replace(result.imports[0], block=(1, 2, 3))

    def test_impls_resolve_their_type_and_trait(self):
        files = {
            "util/Cargo.toml": '[package]\nname = "my-util"\n',
            "util/src/lib.rs": "pub mod shapes;\npub trait Top {}\n",
            "util/src/shapes.rs": "pub trait Speak {}\npub fn Speak() {}\n"
            "pub struct Unit;\npub struct Pt {}\npub fn Pt() {}\n",
            "app/Cargo.toml": '[package]\nname = "app"\n',
            "app/src/lib.rs": "mod a;\n"
            "use my_util::shapes::Speak;\n"
            "pub struct S;\n"
            "pub type Alias = S;\n"
            "impl Speak for S {}\n"
            "impl my_util::Top for Alias {}\n"
            "impl<T> Speak for T {}\n"
            "impl S { fn new() {} }\n"
            "impl !Send for S {}\n"
            "impl Speak for (S, S) {}\n"
            "impl std::fmt::Debug for S {}\n"
            "impl Speak for a::Missing {}\n"
            "impl<'a> Speak for &'a mut a::B<u8> {}\n"
            "fn f() {\n"
            "    struct Local;\n"
            "    use my_util::Top as T2;\n"
            "    impl T2 for Local {}\n"
            "    impl Speak for my_util::shapes::Unit {}\n"
            "}\n"
            "macro_rules! items { ($($i:item)*) => { $($i)* } }\n"
            "items! { impl Speak for crate::a::B<u8> {} }\n"
            "impl<T> <T as Speak>::Y for S {}\n"
            "impl ::my_util::Top for r#S {}\n"
            "impl<'a, U, const N: usize> Speak for my_util::shapes::Pt {}\n"
            "impl Speak for ::S {}\n",
            "app/src/a.rs": "pub struct B<T>(T);\n",
        }
        result = analyze(files)
        names = {d.id: (d.file, d.symbol) for d in result.definitions}
        impls = {i.id: i for i in result.impls}

        def outcome(resolution):
            if resolution is None:
                return None
            return (
                resolution.status,
                names.get(resolution.target_id),
                resolution.rule_id or resolution.reason,
            )

        self.maxDiff = None
        lib, a, shapes = "app/src/lib.rs", "app/src/a.rs", "util/src/shapes.rs"
        kind, trait = RUST_IMPL_TYPE, RUST_IMPL_TRAIT
        speak = ("linked", (shapes, "Speak"), trait)
        self.assertEqual(
            sorted(
                (
                    impls[r.impl_id].line,
                    impls[r.impl_id].type,
                    outcome(r.type),
                    outcome(r.trait),
                )
                for r in resolve_rust_impls(result)
            ),
            [
                # The trait, not the function of the same name.
                (5, "S", ("linked", (lib, "S"), kind), speak),
                (
                    6,
                    "Alias",
                    ("linked", (lib, "Alias"), kind),
                    ("linked", ("util/src/lib.rs", "Top"), trait),
                ),
                (7, "T", ("unresolved", None, "generic_parameter"), speak),
                # An inherent impl names no trait; a negative impl and a
                # tuple are not facts.
                (8, "S", ("linked", (lib, "S"), kind), None),
                (
                    11,
                    "S",
                    ("linked", (lib, "S"), kind),
                    ("unresolved", None, "external_crate"),
                ),
                (12, "a::Missing", ("unresolved", None, "not_declared"), speak),
                (13, "a::B", ("linked", (a, "B"), kind), speak),
                # Items and uses of the function body around the impl.
                (
                    17,
                    "Local",
                    ("linked", (lib, "f.Local"), kind),
                    ("linked", ("util/src/lib.rs", "Top"), trait),
                ),
                (
                    18,
                    "my_util::shapes::Unit",
                    ("linked", (shapes, "Unit"), kind),
                    speak,
                ),
                # An impl a pass-through macro holds.
                (21, "crate::a::B", ("linked", (a, "B"), kind), speak),
                # A trait written as a qualified path is not a fact.
                (
                    23,
                    "S",
                    ("linked", (lib, "S"), kind),
                    ("linked", ("util/src/lib.rs", "Top"), trait),
                ),
                # The struct, not the function of the same name.
                (24, "my_util::shapes::Pt", ("linked", (shapes, "Pt"), kind), speak),
                # A leading `::` names a crate.
                (25, "::S", ("unresolved", None, "external_crate"), speak),
            ],
        )
        self.assertEqual(
            [i.generics for i in result.impls if i.line in {7, 24}],
            [("T",), ("U",)],
        )
        self.assertEqual(AnalysisResult.from_json(result.to_json()).impls, result.impls)
        self.assertIn("impls", result.capabilities)
        item = result.impls[0]
        for change in (
            {"type": ""},
            {"trait": ""},
            {"generics": ["T"]},
            {"generics": ("",)},
        ):
            with self.assertRaises(ValueError):
                dataclasses.replace(item, **change)
        with self.assertRaises(ValueError):
            dataclasses.replace(
                result,
                capabilities=tuple(c for c in result.capabilities if c != "impls"),
            )
        with tempfile.TemporaryDirectory() as directory:
            for name, content in files.items():
                path = Path(directory) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            output = asyncio.run(
                TreeSitterTool(directory).analyze("r", languages=("rust",))
            )
            reused = {}
            again = analyze_repository(
                directory,
                repository_id="fixture",
                languages=("rust",),
                previous=result,
                reuse=reused,
            )
        self.assertEqual(reused["parsed"], 0)
        self.assertEqual(again.impls, result.impls)
        nodes = {n.properties["qualified_name"]: n.properties for n in output.nodes}
        self.assertEqual(
            sorted(
                (
                    nodes[e.source_match["qualified_name"]]["name"],
                    nodes[e.target_match["qualified_name"]]["name"],
                    e.properties["line"],
                    e.properties["base"],
                    e.properties["heritage"],
                    e.properties["evidence_type"],
                    e.properties["rule_id"],
                )
                for e in output.relationships
                if e.type == "INHERITS_FROM"
            ),
            [
                ("Alias", "Top", 6, "my_util::Top", "impl", "resolved", trait),
                ("B", "Speak", 13, "Speak", "impl", "resolved", trait),
                ("B", "Speak", 21, "Speak", "impl", "resolved", trait),
                ("Pt", "Speak", 24, "Speak", "impl", "resolved", trait),
                ("S", "Speak", 5, "Speak", "impl", "resolved", trait),
                ("S", "Top", 23, "::my_util::Top", "impl", "resolved", trait),
                ("Unit", "Speak", 18, "Speak", "impl", "resolved", trait),
                ("f.Local", "Top", 17, "T2", "impl", "resolved", trait),
            ],
        )
        counts = output.metadata.custom_metrics["supertype_resolution"]
        self.assertEqual(
            counts,
            {
                "linked": 8,
                "type_unresolved:external_crate": 1,
                "type_unresolved:generic_parameter": 1,
                "type_unresolved:not_declared": 1,
                "unresolved:external_crate": 1,
            },
        )

    def test_methods_and_associated_functions_resolve_through_impls(self):
        result = analyze(
            {
                "Cargo.toml": '[package]\nname = "x"\n',
                "src/lib.rs": (
                    "mod shapes {\n"
                    "    pub struct P;\n"
                    "    pub enum K { A(u8) }\n"
                    "    pub trait Speak { fn talk(&self) {} fn hush(&self); fn both(); type Kind; }\n"
                    "    pub trait Loud { fn both(); }\n"
                    "    impl P { pub fn new() -> Self { Self::make() } fn make() -> Self { P } fn talk(&self) {} }\n"
                    "    impl Speak for P { fn hush(&self) { self.talk(); self.loud(); } fn both() {} }\n"
                    "    impl Loud for P { fn both() {} }\n"
                    "    impl K { fn run(&self) { self.go(); Self::stop(); } }\n"
                    "    impl Speak for K { fn hush(&self) { self.talk(); Self::both(); } fn both() {} }\n"
                    "}\n"
                    "use shapes::{K, P};\n"
                    "pub type Alias = P;\n"
                    "impl Alias { fn alias_only() {} }\n"
                    "struct Q;\n"
                    "trait T2 { fn alias_only(); fn deflt(&self) { self.deflt(); self.other(); Self::deflt(); } }\n"
                    "impl T2 for Q { fn alias_only() {} }\n"
                    "impl<T> T2 for T { fn alias_only() {} }\n"
                    "fn f(x: P) {\n"
                    "    P::new();\n"
                    "    P::talk(&x);\n"
                    "    P::hush(&x);\n"
                    "    P::both();\n"
                    "    K::A(1);\n"
                    "    K::hush(&K::A(1));\n"
                    "    K::talk(&K::A(1));\n"
                    "    K::missing();\n"
                    "    Alias::new();\n"
                    "    Q::alias_only();\n"
                    "    Q::deflt(&Q);\n"
                    "    x.talk();\n"
                    "    self.a.b();\n"
                    "    shapes::P::make();\n"
                    "}\n"
                    "impl P {\n"
                    "    fn nested(&self, x: P) {\n"
                    "        x.talk();\n"
                    "        Self::make::x();\n"
                    "        trait In { fn i(&self) { Self::make(); } }\n"
                    "    }\n"
                    "}\n"
                    "trait T3 { fn d(&self) { struct Z; impl Z { fn z(&self) { self.zz(); } fn zz(&self) {} } } }\n"
                    "#[cfg(a)]\nstruct D;\n#[cfg(b)]\nstruct D;\n"
                    "fn g() { D::d(); P::Kind(); }\n"
                ),
            }
        )
        names = {d.id: (d.symbol, d.line) for d in result.definitions}
        sites = {c.id: c for c in result.call_sites}
        assoc, method = RUST_CALL_ASSOCIATED, RUST_CALL_SELF_METHOD
        self.maxDiff = None
        self.assertEqual(
            sorted(
                (
                    sites[r.call_id].line,
                    sites[r.call_id].column,
                    sites[r.call_id].callee,
                    names.get(r.target_id),
                    r.rule_id or r.reason,
                    [names[c] for c in r.candidates],
                )
                for r in resolve_calls(result)
            ),
            [
                # Self is the type of the impl around the call.
                (6, 36, "Self::make", ("shapes.P.make", 6), assoc, []),
                # An inherent method before the trait's provided one.
                (7, 40, "self.talk", ("shapes.P.talk", 6), method, []),
                (7, 53, "self.loud", None, "associated_item", []),
                (9, 29, "self.go", None, "associated_item", []),
                (9, 40, "Self::stop", None, "associated_item", []),
                # A provided method the impl does not override.
                (10, 40, "self.talk", ("shapes.Speak.talk", 4), method, []),
                (10, 53, "Self::both", ("shapes.K.both", 10), assoc, []),
                # In a trait body: the trait's own methods.
                (16, 46, "self.deflt", ("T2.deflt", 16), method, []),
                (16, 60, "self.other", None, "method_call", []),
                (16, 74, "Self::deflt", None, "associated_item", []),
                (20, 4, "P::new", ("shapes.P.new", 6), assoc, []),
                (21, 4, "P::talk", ("shapes.P.talk", 6), assoc, []),
                # A trait impl's method.
                (22, 4, "P::hush", ("shapes.P.hush", 7), assoc, []),
                # Two traits' impls declare it.
                (
                    23,
                    4,
                    "P::both",
                    None,
                    "multiple_declarations",
                    [("shapes.P.both", 7), ("shapes.P.both", 8)],
                ),
                # An enum variant is not a function.
                (24, 4, "K::A", None, "associated_item", []),
                (25, 4, "K::hush", ("shapes.K.hush", 10), assoc, []),
                (25, 13, "K::A", None, "associated_item", []),
                (26, 4, "K::talk", ("shapes.Speak.talk", 4), assoc, []),
                (26, 13, "K::A", None, "associated_item", []),
                (27, 4, "K::missing", None, "associated_item", []),
                # The aliased type is not read; a name an impl for an alias
                # declares is not taken from a trait.
                (28, 4, "Alias::new", None, "type_alias", []),
                (29, 4, "Q::alias_only", None, "type_alias", []),
                (30, 4, "Q::deflt", ("T2.deflt", 16), assoc, []),
                # Receiver types are not inferred.
                (31, 4, "x.talk", None, "method_call", []),
                (32, 4, "self.a.b", None, "method_call", []),
                (33, 4, "shapes::P::make", ("shapes.P.make", 6), assoc, []),
                # Only `self` has the impl's type.
                (37, 8, "x.talk", None, "method_call", []),
                (38, 8, "Self::make::x", None, "associated_item", []),
                # Self inside a trait written in an impl is not the impl's.
                (39, 33, "Self::make", None, "associated_item", []),
                # An impl inside a trait's method body.
                (42, 58, "self.zz", ("T3.d.Z.zz", 42), method, []),
                # A type declared once per #[cfg] is not taken.
                (47, 9, "D::d", None, "associated_item", []),
                # A trait's associated type is not a function.
                (47, 17, "P::Kind", None, "associated_item", []),
            ],
        )

    def test_supertraits_resolve_as_paths(self):
        files = {
            "Cargo.toml": '[package]\nname = "x"\n',
            "src/lib.rs": (
                "mod c { pub trait D<T> {} pub fn D() {} }\n"
                "trait B {}\n"
                "trait E<'x> {}\n"
                "trait F {}\n"
                "trait A<T>: B + c::D<T> + ?Sized + 'static + for<'x> E<'x> + Send\n"
                "where\n"
                "    Self: F,\n"
                "    T: B,\n"
                "{\n"
                "}\n"
                "trait G: Missing + c::Nope + std::fmt::Debug {}\n"
                "macro_rules! items { ($($i:item)*) => { $($i)* } }\n"
                "items! { trait H: crate::B {} }\n"
                "fn f() { trait L {} trait M: L {} }\n"
            ),
        }
        result = analyze(files)
        names = {d.id: d.symbol for d in result.definitions}
        bases = {s.id: s for s in result.supertypes}
        rule = RUST_SUPERTRAIT
        self.assertEqual(
            sorted(
                (
                    bases[r.call_id].line,
                    bases[r.call_id].column,
                    names[bases[r.call_id].owner_id],
                    bases[r.call_id].name,
                    bases[r.call_id].kind,
                    names.get(r.target_id),
                    r.rule_id or r.reason,
                )
                for r in resolve_supertypes(result)
            ),
            [
                # Bounds and `where Self:` bounds, not lifetimes, `?Sized` or
                # other where predicates; the trait, not a function.
                (5, 12, "A", "B", "extends", "B", rule),
                (5, 16, "A", "c::D", "extends", "c.D", rule),
                (5, 53, "A", "E", "extends", "E", rule),
                (5, 61, "A", "Send", "extends", None, "unbound_name"),
                (7, 10, "A", "F", "extends", "F", rule),
                (11, 9, "G", "Missing", "extends", None, "unbound_name"),
                (11, 19, "G", "c::Nope", "extends", None, "not_declared"),
                (11, 29, "G", "std::fmt::Debug", "extends", None, "external_crate"),
                # In an item macro and in a function body.
                (13, 18, "H", "crate::B", "extends", "B", rule),
                (14, 29, "f.M", "L", "extends", "f.L", rule),
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            for name, content in files.items():
                path = Path(directory) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            output = asyncio.run(
                TreeSitterTool(directory).analyze("r", languages=("rust",))
            )
        nodes = {n.properties["qualified_name"]: n.properties for n in output.nodes}
        edges = [
            (
                nodes[e.source_match["qualified_name"]]["name"],
                nodes[e.target_match["qualified_name"]]["name"],
                e.properties["heritage"],
                e.properties["evidence_type"],
                e.properties["rule_id"],
            )
            for e in output.relationships
            if e.type == "INHERITS_FROM"
        ]
        self.assertEqual(len(edges), 6)
        self.assertIn(("A", "c.D", "extends", "resolved", rule), edges)


if __name__ == "__main__":
    unittest.main()
