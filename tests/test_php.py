"""PHP declarations (opt-in language)."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought import analyze_repository
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
                # A closure, an anonymous class and a method body declare
                # what is in them only when they run.
                ("a.php", 11, "App.Http.Controller.make", "Method"),
                # A keyword can name a method.
                ("a.php", 16, "App.Http.Controller.list", "Method"),
                ("a.php", 18, "App.Http.Contract", "Interface"),
                ("a.php", 18, "App.Http.Contract.handle", "Method"),
                ("a.php", 19, "App.Http.Helpers", "Trait"),
                ("a.php", 19, "App.Http.Helpers.help", "Method"),
                ("a.php", 20, "App.Http.Suit", "Enum"),
                ("a.php", 22, "App.Http.Suit.label", "Method"),
                # Both branches of a conditional declaration.
                ("a.php", 25, "App.Http.helper", "Function"),
                ("a.php", 27, "App.Http.helper", "Function"),
                ("a.php", 29, "App.Http.top", "Function"),
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
            (result.imports, result.call_sites, result.supertypes), ((), (), ())
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
