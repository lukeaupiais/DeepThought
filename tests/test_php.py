"""PHP declarations (opt-in language)."""

import dataclasses
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought import analyze_repository
from deepthought.calls import (
    PHP_CALL_FUNCTION,
    PHP_CALL_INHERITED_METHOD,
    PHP_CALL_NEW,
    PHP_CALL_STATIC_METHOD,
    PHP_CALL_THIS_METHOD,
    resolve_calls,
)
from deepthought.imports import (
    PHP_USE_DECLARED,
    PHP_USE_NAMESPACE,
    resolve_imports,
)
from deepthought.inheritance import PHP_BASE_NAME, resolve_supertypes
from deepthought.models import AnalysisResult
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
        self.assertEqual(
            [c.callee for c in result.call_sites], ["function_exists", "b"]
        )
        self.assertEqual(
            [(t.name, t.kind, t.qualified_name) for t in result.supertypes],
            [
                ("Base", "extends", "App.Http.Base"),
                ("Contract", "implements", "App.Http.Contract"),
            ],
        )

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

    def test_calls_follow_php_name_resolution(self):
        result = analyze(
            {
                "a.php": r"""<?php
namespace App;
use Lib\Models\User;
use Lib\Models as M;
use function Lib\util as u;
function local() {}
class Base { function up() {} public function __construct() {} }
class C extends Base {
    function m() {
        local(); global_only(); U(); M\User::find(); \Lib\util();
        namespace\local(); strlen(''); LOCAL(); twice();
        new User(); new M\Plain(); new self(); new parent(); new Missing();
        self::m(); static::M(); parent::up(); parent::gone(); User::find(); SELF::up();
        $this->m(); $this?->m(); $this->up(); $o->m(); $f(); $this->$n();
        $g = function () { return $this->m(); }; $h = fn() => self::m();
        function inner() { self::m(); } new I();
    }
    #[Tag(new Plain())] public $p;
}
interface I { function i(); }
function top() { $this->m(); self::m(); }
""",
                "b.php": r"""<?php
namespace Lib\Models;
class User { function __construct() {} static function find() {} }
class Plain {}
namespace Lib;
function util() {}
function u() {}
namespace Other;
function twice() {}
""",
                "g.php": r"""<?php
if (!function_exists('twice')) { function twice() {} } else { function twice() {} }
function global_only() {}
""",
                "c.php": r"""<?php
namespace One { use Lib\Models\User; function f() { User::find(); } }
namespace Two { function g() { User::find(); } }
""",
            }
        )
        names = {d.id: (d.file, d.symbol) for d in result.definitions}
        sites = {c.id: c for c in result.call_sites}
        found = {
            (sites[r.call_id].file, sites[r.call_id].line, sites[r.call_id].column): (
                sites[r.call_id].callee,
                names.get(r.target_id),
                r.rule_id or r.reason,
            )
            for r in resolve_calls(result)
        }
        self.maxDiff = None
        self.assertEqual(
            found,
            {
                ("a.php", 10, 8): ("local", ("a.php", "App.local"), PHP_CALL_FUNCTION),
                ("a.php", 10, 17): (
                    "global_only",
                    ("g.php", "global_only"),
                    PHP_CALL_FUNCTION,
                ),
                ("a.php", 10, 32): ("U", ("b.php", "Lib.util"), PHP_CALL_FUNCTION),
                ("a.php", 10, 37): (
                    "M\\User::find",
                    ("b.php", "Lib.Models.User.find"),
                    PHP_CALL_STATIC_METHOD,
                ),
                ("a.php", 10, 53): (
                    "\\Lib\\util",
                    ("b.php", "Lib.util"),
                    PHP_CALL_FUNCTION,
                ),
                ("a.php", 11, 8): (
                    "namespace\\local",
                    ("a.php", "App.local"),
                    PHP_CALL_FUNCTION,
                ),
                ("a.php", 11, 27): ("strlen", None, "not_defined"),
                ("a.php", 11, 39): ("LOCAL", ("a.php", "App.local"), PHP_CALL_FUNCTION),
                ("a.php", 11, 48): ("twice", None, "multiple_declarations"),
                ("a.php", 12, 8): (
                    "User",
                    ("b.php", "Lib.Models.User.__construct"),
                    PHP_CALL_NEW,
                ),
                ("a.php", 12, 20): (
                    "M\\Plain",
                    ("b.php", "Lib.Models.Plain"),
                    PHP_CALL_NEW,
                ),
                ("a.php", 12, 35): (
                    "self",
                    ("a.php", "App.Base.__construct"),
                    PHP_CALL_NEW,
                ),
                ("a.php", 12, 47): (
                    "parent",
                    ("a.php", "App.Base.__construct"),
                    PHP_CALL_NEW,
                ),
                ("a.php", 12, 61): ("Missing", None, "not_defined"),
                ("a.php", 13, 8): (
                    "self::m",
                    ("a.php", "App.C.m"),
                    PHP_CALL_STATIC_METHOD,
                ),
                ("a.php", 13, 19): (
                    "static::M",
                    ("a.php", "App.C.m"),
                    PHP_CALL_THIS_METHOD,
                ),
                ("a.php", 13, 32): (
                    "parent::up",
                    ("a.php", "App.Base.up"),
                    PHP_CALL_STATIC_METHOD,
                ),
                ("a.php", 13, 46): ("parent::gone", None, "not_declared_on_type"),
                ("a.php", 13, 62): (
                    "User::find",
                    ("b.php", "Lib.Models.User.find"),
                    PHP_CALL_STATIC_METHOD,
                ),
                ("a.php", 13, 76): (
                    "SELF::up",
                    ("a.php", "App.Base.up"),
                    PHP_CALL_INHERITED_METHOD,
                ),
                ("a.php", 14, 8): (
                    "$this->m",
                    ("a.php", "App.C.m"),
                    PHP_CALL_THIS_METHOD,
                ),
                ("a.php", 14, 20): (
                    "$this->m",
                    ("a.php", "App.C.m"),
                    PHP_CALL_THIS_METHOD,
                ),
                ("a.php", 14, 33): (
                    "$this->up",
                    ("a.php", "App.Base.up"),
                    PHP_CALL_INHERITED_METHOD,
                ),
                ("a.php", 15, 34): ("$this->m", None, "receiver_type_unknown"),
                ("a.php", 15, 62): ("self::m", None, "receiver_type_unknown"),
                ("a.php", 16, 27): ("self::m", None, "receiver_type_unknown"),
                ("a.php", 16, 40): ("I", None, "not_defined"),
                ("a.php", 18, 10): ("Plain", None, "not_defined"),
                ("a.php", 21, 17): ("$this->m", None, "receiver_type_unknown"),
                ("a.php", 21, 29): ("self::m", None, "receiver_type_unknown"),
                ("c.php", 2, 52): (
                    "User::find",
                    ("b.php", "Lib.Models.User.find"),
                    PHP_CALL_STATIC_METHOD,
                ),
                ("c.php", 3, 31): ("User::find", None, "not_defined"),
                ("g.php", 2, 5): ("function_exists", None, "not_defined"),
            },
        )
        callers = {
            (c.file, c.line, c.column): names[c.caller_id]
            for c in result.call_sites
            if c.caller_id is not None
        }
        self.assertEqual(callers[("a.php", 10, 8)], ("a.php", "App.C.m"))
        self.assertEqual(callers[("a.php", 15, 34)], ("a.php", "App.C.m"))
        self.assertEqual(callers[("c.php", 2, 52)], ("c.php", "One.f"))
        self.assertNotIn(("g.php", 2, 5), callers)
        self.assertNotIn(("a.php", 18, 10), callers)
        self.assertEqual(
            sites[
                next(i for i, c in sites.items() if c.callee == "global_only")
            ].qualified_names,
            ("App.global_only", "global_only"),
        )
        self.assertEqual(AnalysisResult.from_json(result.to_json()), result)
        self.assertNotIn('"qualified_names": null', result.to_json())
        with self.assertRaises(ValueError):
            dataclasses.replace(result.call_sites[0], qualified_names=("",))

    def test_methods_are_found_through_traits_and_parents(self):
        result = analyze(
            {
                "h.php": r"""<?php
namespace App;
use Vendor\Base as VendorBase;
use App\Traits\Greets;
interface Shape extends Named, \Countable { function area(); }
interface Named {}
trait Loud { function shout() {} function hello() {} }
class Root { function __construct() {} function hello() {} function root() {} }
class Mid extends Root implements Shape { use Greets, Loud; function area() {} }
class Leaf extends Mid {
    function go() {
        $this->hello(); $this->shout(); $this->root(); $this->wave(); $this->area();
        parent::hello(); self::root(); static::shout(); $this->nothing(); new Leaf();
    }
}
class Outside extends VendorBase { function go() { $this->helper(); new Outside(); } }
class Unknown { use Missing; function go() { $this->x(); } }
class Both { use Loud, Echoes; function go() { $this->shout(); $this->hello(); } }
class A extends B { function go() { $this->loop(); } }
class B extends A {}
enum Suit: string implements Named { use Loud; case H = 'h'; function go() { $this->shout(); } }
""",
                "t.php": r"""<?php
namespace App\Traits;
trait Greets { use \App\Loud; function wave() {} }
namespace App;
trait Echoes { function shout() {} }
""",
            }
        )
        names = {d.id: (d.file, d.symbol) for d in result.definitions}
        facts = {s.id: s for s in result.supertypes}
        self.maxDiff = None
        self.assertEqual(
            {
                (
                    facts[r.call_id].file,
                    facts[r.call_id].line,
                    facts[r.call_id].column,
                ): (
                    names[facts[r.call_id].owner_id][1],
                    facts[r.call_id].kind,
                    facts[r.call_id].qualified_name,
                    names.get(r.target_id),
                    r.rule_id or r.reason,
                )
                for r in resolve_supertypes(result)
            },
            {
                ("h.php", 5, 24): (
                    "App.Shape",
                    "extends",
                    "App.Named",
                    ("h.php", "App.Named"),
                    PHP_BASE_NAME,
                ),
                ("h.php", 5, 31): (
                    "App.Shape",
                    "extends",
                    "Countable",
                    None,
                    "type_not_declared",
                ),
                ("h.php", 9, 18): (
                    "App.Mid",
                    "extends",
                    "App.Root",
                    ("h.php", "App.Root"),
                    PHP_BASE_NAME,
                ),
                ("h.php", 9, 34): (
                    "App.Mid",
                    "implements",
                    "App.Shape",
                    ("h.php", "App.Shape"),
                    PHP_BASE_NAME,
                ),
                ("h.php", 9, 46): (
                    "App.Mid",
                    "uses",
                    "App.Traits.Greets",
                    ("t.php", "App.Traits.Greets"),
                    PHP_BASE_NAME,
                ),
                ("h.php", 9, 54): (
                    "App.Mid",
                    "uses",
                    "App.Loud",
                    ("h.php", "App.Loud"),
                    PHP_BASE_NAME,
                ),
                ("h.php", 10, 19): (
                    "App.Leaf",
                    "extends",
                    "App.Mid",
                    ("h.php", "App.Mid"),
                    PHP_BASE_NAME,
                ),
                ("h.php", 16, 22): (
                    "App.Outside",
                    "extends",
                    "Vendor.Base",
                    None,
                    "type_not_declared",
                ),
                ("h.php", 17, 20): (
                    "App.Unknown",
                    "uses",
                    "App.Missing",
                    None,
                    "type_not_declared",
                ),
                ("h.php", 18, 17): (
                    "App.Both",
                    "uses",
                    "App.Loud",
                    ("h.php", "App.Loud"),
                    PHP_BASE_NAME,
                ),
                ("h.php", 18, 23): (
                    "App.Both",
                    "uses",
                    "App.Echoes",
                    ("t.php", "App.Echoes"),
                    PHP_BASE_NAME,
                ),
                ("h.php", 19, 16): (
                    "App.A",
                    "extends",
                    "App.B",
                    ("h.php", "App.B"),
                    PHP_BASE_NAME,
                ),
                ("h.php", 20, 16): (
                    "App.B",
                    "extends",
                    "App.A",
                    ("h.php", "App.A"),
                    PHP_BASE_NAME,
                ),
                ("h.php", 21, 29): (
                    "App.Suit",
                    "implements",
                    "App.Named",
                    ("h.php", "App.Named"),
                    PHP_BASE_NAME,
                ),
                ("h.php", 21, 41): (
                    "App.Suit",
                    "uses",
                    "App.Loud",
                    ("h.php", "App.Loud"),
                    PHP_BASE_NAME,
                ),
                ("t.php", 3, 19): (
                    "App.Traits.Greets",
                    "uses",
                    "App.Loud",
                    ("h.php", "App.Loud"),
                    PHP_BASE_NAME,
                ),
            },
        )
        sites = {c.id: c for c in result.call_sites}
        self.assertEqual(
            {
                (
                    sites[r.call_id].file,
                    sites[r.call_id].line,
                    sites[r.call_id].column,
                ): (
                    sites[r.call_id].callee,
                    names.get(r.target_id),
                    r.rule_id or r.reason,
                )
                for r in resolve_calls(result)
            },
            {
                ("h.php", 12, 8): (
                    "$this->hello",
                    ("h.php", "App.Loud.hello"),
                    PHP_CALL_INHERITED_METHOD,
                ),
                ("h.php", 12, 24): (
                    "$this->shout",
                    ("h.php", "App.Loud.shout"),
                    PHP_CALL_INHERITED_METHOD,
                ),
                ("h.php", 12, 40): (
                    "$this->root",
                    ("h.php", "App.Root.root"),
                    PHP_CALL_INHERITED_METHOD,
                ),
                ("h.php", 12, 55): (
                    "$this->wave",
                    ("t.php", "App.Traits.Greets.wave"),
                    PHP_CALL_INHERITED_METHOD,
                ),
                ("h.php", 12, 70): (
                    "$this->area",
                    ("h.php", "App.Mid.area"),
                    PHP_CALL_INHERITED_METHOD,
                ),
                ("h.php", 13, 8): (
                    "parent::hello",
                    ("h.php", "App.Loud.hello"),
                    PHP_CALL_INHERITED_METHOD,
                ),
                ("h.php", 13, 25): (
                    "self::root",
                    ("h.php", "App.Root.root"),
                    PHP_CALL_INHERITED_METHOD,
                ),
                ("h.php", 13, 39): (
                    "static::shout",
                    ("h.php", "App.Loud.shout"),
                    PHP_CALL_INHERITED_METHOD,
                ),
                ("h.php", 13, 56): ("$this->nothing", None, "not_declared_on_type"),
                ("h.php", 13, 74): (
                    "Leaf",
                    ("h.php", "App.Root.__construct"),
                    PHP_CALL_NEW,
                ),
                ("h.php", 16, 51): ("$this->helper", None, "base_not_resolved"),
                ("h.php", 16, 68): ("Outside", ("h.php", "App.Outside"), PHP_CALL_NEW),
                ("h.php", 17, 45): ("$this->x", None, "base_not_resolved"),
                ("h.php", 18, 47): ("$this->shout", None, "multiple_declarations"),
                ("h.php", 18, 63): (
                    "$this->hello",
                    ("h.php", "App.Loud.hello"),
                    PHP_CALL_INHERITED_METHOD,
                ),
                ("h.php", 19, 36): ("$this->loop", None, "base_not_resolved"),
                ("h.php", 21, 77): (
                    "$this->shout",
                    ("h.php", "App.Loud.shout"),
                    PHP_CALL_INHERITED_METHOD,
                ),
            },
        )
        self.assertEqual(AnalysisResult.from_json(result.to_json()), result)
        self.assertNotIn(
            '"qualified_name": null',
            analyze({"a.py": "class A(B): pass\n"}, ("python",)).to_json(),
        )
        with self.assertRaises(ValueError):
            dataclasses.replace(result.supertypes[0], qualified_name="")

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
