"""PHP call truth from PHP's own tokenizer."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import ground_truth  # noqa: E402

APP = r"""<?php
namespace App;

use App\Util\Helper as H;
use function App\Util\fmt;
use Vendor\Thing;

function local() { return 1; }

trait Greets {
    public function greet() { return $this->name(); }
    public function name() { return 'x'; }
}

abstract class Base {
    public function __construct() {}
    public function run() { return $this->step(); }
    abstract protected function step();
    public static function make() { return new static(); }
}

class Child extends Base {
    use Greets;
    protected function step() {
        local();
        fmt(1);
        strlen('a');
        \strlen('b');
        H::help();
        $x = new H();
        $y = new \ArrayObject([]);
        $z = new Thing();
        parent::run();
        self::make();
        $this->greet();
        $this?->run();
        $o = new class { function f() { return $this->f(); } };
        $fn = function () use ($x) { return $this->step(); };
        return $x->other();
    }
}
"""
UTIL = r"""<?php
namespace App\Util;

function fmt($a) { return $a; }

class Helper {
    public static function help() { return 1; }
}
"""


@unittest.skipUnless(shutil.which("php"), "php is required")
class PhpCallTruthTests(unittest.TestCase):
    def test_name_resolution_hierarchy_and_builtins(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "src/Util").mkdir(parents=True)
            (Path(root) / "src/a.php").write_text(APP)
            (Path(root) / "src/Util/h.php").write_text(UTIL)
            (Path(root) / "src/broken.php").write_text("<?php function (")
            out = subprocess.run(
                ["php", str(ROOT / "tools/php_call_truth.php"), root],
                capture_output=True,
                text=True,
                check=True,
            )
            path = Path(root) / "calls.tsv"
            path.write_text(out.stdout)
            expected, indexed = ground_truth.read_truth(path)
        self.assertIn("src/broken.php: unparsed", out.stderr)
        self.assertEqual(indexed, {"src/a.php", "src/Util/h.php"})
        external = {("<external>", 0, "external")}
        self.assertEqual(
            expected,
            {
                # A trait's own method through `$this`.
                ("src/a.php", 11, "name"): {("src/a.php", 12, "callable")},
                # An abstract method declared on the class itself.
                ("src/a.php", 17, "step"): {("src/a.php", 18, "callable")},
                # new static: the class and its constructor.
                ("src/a.php", 19, "static"): {
                    ("src/a.php", 15, "callable"),
                    ("src/a.php", 16, "callable"),
                },
                ("src/a.php", 25, "local"): {("src/a.php", 8, "callable")},
                # use function, and the global fallback to a built-in.
                ("src/a.php", 26, "fmt"): {("src/Util/h.php", 4, "callable")},
                ("src/a.php", 27, "strlen"): external,
                ("src/a.php", 28, "strlen"): external,
                # An aliased class import; a class with no constructor.
                ("src/a.php", 29, "help"): {("src/Util/h.php", 7, "callable")},
                ("src/a.php", 30, "H"): {("src/Util/h.php", 6, "callable")},
                ("src/a.php", 31, "ArrayObject"): external,
                # parent::, self::, a trait method, `$this?->`, and `$this` in a
                # closure.
                ("src/a.php", 33, "run"): {("src/a.php", 17, "callable")},
                ("src/a.php", 34, "make"): {("src/a.php", 19, "callable")},
                ("src/a.php", 35, "greet"): {("src/a.php", 11, "callable")},
                ("src/a.php", 36, "run"): {("src/a.php", 17, "callable")},
                ("src/a.php", 38, "step"): {("src/a.php", 24, "callable")},
            },
        )
        # Left out: new of a dependency's class (32), calls in an anonymous
        # class (37) and calls on other variables (39).


if __name__ == "__main__":
    unittest.main()
