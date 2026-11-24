"""PHP declarations (opt-in language)."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought import analyze_repository
from deepthought.imports import (
    PHP_USE_DECLARED,
    PHP_USE_NAMESPACE,
    resolve_imports,
)
from deepthought.syntax import declaration_sources, make_parser

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_php"
)
SOURCE = """<?php
namespace App\\Http;

use Other\\Thing;

#[Attribute]
abstract class Controller extends Base implements Contract {
    const X = 1;
    public $field;
    abstract protected function handle(): void;
    public static function &make() {
        $f = function () { function in_closure() {} };
        $g = fn() => 1;
        return new class { function anonymous() {} };
    }
    function list() {}
}
interface Contract { function handle(); }
trait Helpers { function help() {} }
enum Suit: string {
    case Hearts = 'H';
    function label() { function nested() {} class InBody {} }
}
if (!function_exists('helper')) {
    function helper() {}
} else {
    function helper() {}
}
function top() {}
$f = function () { function in_closure() {} };
$g = fn() => new class { function in_arrow() {} };
$o = new class { function in_anonymous_class() {} };
"""
LATER = """<?php
namespace First;
function a() {}
namespace Second;
function b() {}
?>
<p><?= b() ?></p>
"""
BRACED = """<?php
namespace One { class C {} }
namespace { function global_function() {} }
"""


def analyze(files, languages=("php",)):
    with tempfile.TemporaryDirectory() as directory:
        for name, content in files.items():
            path = Path(directory) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        options = {} if languages is None else {"languages": languages}
        return analyze_repository(directory, repository_id="fixture", **options)


@unittest.skipUnless(READY, "PHP grammar is not installed")
class PhpTests(unittest.TestCase):
    def test_classes_functions_and_methods(self):
        result = analyze({"a.php": SOURCE, "b.php": LATER, "c.php": BRACED})
        self.assertEqual(result.status, "succeeded")
        self.maxDiff = None
        self.assertEqual(
            [(d.file, d.line, d.symbol, d.kind) for d in result.definitions],
            [
                # Symbols join the namespace; the attribute's line starts
                # the class.
                ("a.php", 6, "App.Http.Controller", "Class"),
                ("a.php", 10, "App.Http.Controller.handle", "Method"),
                ("a.php", 11, "App.Http.Controller.make", "Method"),
                # What a body or closure declares goes to the namespace when
                # it runs.
                ("a.php", 12, "App.Http.in_closure", "Function"),
                # An anonymous class's methods are named by where it is.
                ("a.php", 14, "App.Http.Controller.make.anonymous", "Method"),
                # A keyword can name a method.
                ("a.php", 16, "App.Http.Controller.list", "Method"),
                ("a.php", 18, "App.Http.Contract", "Interface"),
                ("a.php", 18, "App.Http.Contract.handle", "Method"),
                ("a.php", 19, "App.Http.Helpers", "Trait"),
                ("a.php", 19, "App.Http.Helpers.help", "Method"),
                ("a.php", 20, "App.Http.Suit", "Enum"),
                ("a.php", 22, "App.Http.Suit.label", "Method"),
                ("a.php", 22, "App.Http.nested", "Function"),
                ("a.php", 22, "App.Http.InBody", "Class"),
                # Both branches of a conditional declaration.
                ("a.php", 25, "App.Http.helper", "Function"),
                ("a.php", 27, "App.Http.helper", "Function"),
                ("a.php", 29, "App.Http.top", "Function"),
                ("a.php", 30, "App.Http.in_closure", "Function"),
                ("a.php", 31, "App.Http.in_arrow", "Method"),
                ("a.php", 32, "App.Http.in_anonymous_class", "Method"),
                # A namespace statement scopes what follows, up to the next.
                ("b.php", 3, "First.a", "Function"),
                ("b.php", 5, "Second.b", "Function"),
                ("c.php", 2, "One.C", "Class"),
                # A braced namespace without a name is the global one.
                ("c.php", 3, "global_function", "Function"),
            ],
        )
        helpers = {d.id for d in result.definitions if d.symbol.endswith("helper")}
        self.assertEqual(len(helpers), 2)
        self.assertEqual(
            [(i.file, i.line, i.specifier) for i in result.imports],
            [("a.php", 4, "Other\\Thing")],
        )
        self.assertEqual((result.call_sites, result.supertypes), ((), ()))

    def test_use_imports_link_to_declaring_files(self):
        files = {
            "src/User.php": "<?php\nnamespace App\\Models;\nclass User {}\n"
            "function helper() {}\n",
            "src/Post.php": "<?php\nnamespace App\\Models;\ninterface Post {}\n"
            "enum Kind { case A; function go() {} }\n",
            # One file declaring a type in each branch of an `if`.
            "src/Compat.php": "<?php\nnamespace App;\n"
            "if (PHP_VERSION_ID > 80000) { class Compat {} }\n"
            "else { class Compat {} }\n",
            # Two files declaring one type.
            "a/Twice.php": "<?php\nnamespace App;\ntrait Twice {}\n",
            "b/Twice.php": "<?php\nnamespace App;\ntrait Twice {}\n",
            # A class in a function body.
            "src/Late.php": "<?php\nnamespace App;\n"
            "function make() { class Late {} }\n",
            "src/main.php": "<?php\n"
            "namespace App\\Http;\n"
            "use App\\Models\\User;\n"
            "use \\app\\MODELS\\Post as P, App\\Compat;\n"
            "use function App\\Models\\helper, App\\Models\\missing as gone;\n"
            "use const App\\Models\\LIMIT;\n"
            "use App\\{function make, Twice, Models, Late};\n"
            "use function App\\{make as build};\n"
            "use Vendor\\Package\\Thing;\n"
            "use App\\Models\\Kind, App\\Models\\helper, App;\n"
            "use function App\\Models, App\\Models\\Kind\\go;\n"
            "class Controller { use Twice; }\n"
            "function f() { $c = function () use ($x) {}; }\n",
        }
        result = analyze(files)
        main = [i for i in result.imports if i.file == "src/main.php"]
        self.assertEqual(
            [(i.line, i.kind, i.specifier, i.local_names) for i in main],
            [
                (3, "use", "App\\Models\\User", ("User",)),
                (4, "use", "app\\MODELS\\Post", ("P",)),
                (4, "use", "App\\Compat", ("Compat",)),
                # The keyword applies to every clause.
                (5, "use_function", "App\\Models\\helper", ("helper",)),
                (5, "use_function", "App\\Models\\missing", ("gone",)),
                (6, "use_const", "App\\Models\\LIMIT", ("LIMIT",)),
                # A group joins its prefix; a clause may name its own kind.
                (7, "use_function", "App\\make", ("make",)),
                (7, "use", "App\\Twice", ("Twice",)),
                (7, "use", "App\\Models", ("Models",)),
                (7, "use", "App\\Late", ("Late",)),
                (8, "use_function", "App\\make", ("build",)),
                (9, "use", "Vendor\\Package\\Thing", ("Thing",)),
                (10, "use", "App\\Models\\Kind", ("Kind",)),
                (10, "use", "App\\Models\\helper", ("helper",)),
                (10, "use", "App", ("App",)),
                (11, "use_function", "App\\Models", ("Models",)),
                (11, "use_function", "App\\Models\\Kind\\go", ("go",)),
                # A trait use in a class body and a closure's `use` are not
                # imports.
            ],
        )
        ids = {i.id: i for i in main}
        self.assertEqual(
            [
                (
                    ids[r.import_id].specifier,
                    r.status,
                    [(link.target_file, link.rule_id) for link in r.links],
                    r.reason,
                    r.candidates,
                )
                for r in sorted(
                    (r for r in resolve_imports(result) if r.import_id in ids),
                    key=lambda r: main.index(ids[r.import_id]),
                )
            ],
            [
                (
                    "App\\Models\\User",
                    "linked",
                    [("src/User.php", PHP_USE_DECLARED)],
                    None,
                    (),
                ),
                # Names compare ignoring ASCII case.
                (
                    "app\\MODELS\\Post",
                    "linked",
                    [("src/Post.php", PHP_USE_DECLARED)],
                    None,
                    (),
                ),
                (
                    "App\\Compat",
                    "linked",
                    [("src/Compat.php", PHP_USE_DECLARED)],
                    None,
                    (),
                ),
                (
                    "App\\Models\\helper",
                    "linked",
                    [("src/User.php", PHP_USE_DECLARED)],
                    None,
                    (),
                ),
                ("App\\Models\\missing", "unresolved", [], "not_in_repository", ()),
                ("App\\Models\\LIMIT", "unresolved", [], "constant", ()),
                (
                    "App\\make",
                    "linked",
                    [("src/Late.php", PHP_USE_DECLARED)],
                    None,
                    (),
                ),
                (
                    "App\\Twice",
                    "ambiguous",
                    [],
                    "multiple_declarations",
                    ("a/Twice.php", "b/Twice.php"),
                ),
                # A namespace: every file declaring something in it.
                (
                    "App\\Models",
                    "linked",
                    [
                        ("src/Post.php", PHP_USE_NAMESPACE),
                        ("src/User.php", PHP_USE_NAMESPACE),
                    ],
                    None,
                    (),
                ),
                # A class declared in a function body is declared there.
                (
                    "App\\Late",
                    "linked",
                    [("src/Late.php", PHP_USE_DECLARED)],
                    None,
                    (),
                ),
                (
                    "App\\make",
                    "linked",
                    [("src/Late.php", PHP_USE_DECLARED)],
                    None,
                    (),
                ),
                ("Vendor\\Package\\Thing", "unresolved", [], "not_in_repository", ()),
                (
                    "App\\Models\\Kind",
                    "linked",
                    [("src/Post.php", PHP_USE_DECLARED)],
                    None,
                    (),
                ),
                # A function is not a class, nor a namespace.
                ("App\\Models\\helper", "unresolved", [], "not_in_repository", ()),
                (
                    "App",
                    "linked",
                    [
                        (f, PHP_USE_NAMESPACE)
                        for f in (
                            "a/Twice.php",
                            "b/Twice.php",
                            "src/Compat.php",
                            "src/Late.php",
                            "src/Post.php",
                            "src/User.php",
                            "src/main.php",
                        )
                    ],
                    None,
                    (),
                ),
                # A function import names no namespace, and a method is not
                # a function.
                ("App\\Models", "unresolved", [], "not_in_repository", ()),
                ("App\\Models\\Kind\\go", "unresolved", [], "not_in_repository", ()),
            ],
        )

    def test_use_imports_link_only_to_php_declarations(self):
        result = analyze(
            {"thing.py": "class Thing:\n    pass\n", "a.php": "<?php\nuse Thing;\n"},
            languages=("php", "python"),
        )
        self.assertEqual(
            [(r.status, r.reason) for r in resolve_imports(result)],
            [("unresolved", "not_in_repository")],
        )

    def test_php_is_opt_in(self):
        default = analyze({"a.php": SOURCE, "a.py": "x = 1\n"}, languages=None)
        self.assertEqual([c.file for c in default.coverage], ["a.py"])

    def test_declaration_sources_name_the_declared_name(self):
        content = b"<?php\nnamespace N;\nfinal class C {\n  function go() {}\n}\n"
        sources = declaration_sources(
            make_parser("php", ".php").parse(content),
            content,
            "fixture",
            "a.php",
            language="php",
        )
        self.assertEqual(
            sorted((s["source_code"], s["name_range_utf8"]) for s in sources.values()),
            [
                ("final class C {\n  function go() {}\n}", [3, 12, 3, 13]),
                ("function go() {}", [4, 11, 4, 13]),
            ],
        )


if __name__ == "__main__":
    unittest.main()
