"""Ruby (Ripper) and PHP (token_get_all) declaration truth."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

RUBY = """module M
  class C
    def m; end
    def self.s; end
    class << self
      def t; end
    end
  end
end

def top; end
obj = Object.new
def obj.single; end
"""
PHP = """<?php
namespace N;
use function strtoupper;

interface I {}

#[Attr]
final class C implements I
{
    public function m(): void
    {
        $h = new class {
            public function inner() {}
        };
        $f = function () {};
        $c = C::class;
    }

    public function &byRef() {}
    public function list() {}
}

function top() {}
"""


def oracle(command, name, text):
    root = Path(tempfile.mkdtemp())
    try:
        (root / name).write_text(text, "utf-8")
        out = subprocess.run(
            [*command, str(root)], check=True, capture_output=True, text=True
        ).stdout
    finally:
        shutil.rmtree(root)
    rows = [line.split("\t") for line in out.splitlines() if not line.startswith("#")]
    return {(int(r[1]), r[2], r[3], int(r[4])) for r in rows}, out


class DeclarationOracleTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ruby"), "ruby is required")
    def test_ruby(self):
        rows, out = oracle(
            ["ruby", str(ROOT / "tools/ruby_declaration_truth.rb")], "a.rb", RUBY
        )
        self.assertIn("# indexed\ta.rb", out)
        self.assertEqual(
            rows,
            {
                (1, "M", "module", 1),
                (2, "C", "type", 2),
                (3, "m", "method", 3),
                (4, "s", "method", 4),
                (6, "t", "method", 6),
                (11, "top", "function", 11),
                (13, "single", "method", 13),
            },
        )

    @unittest.skipUnless(shutil.which("php"), "php is required")
    def test_php(self):
        rows, out = oracle(
            ["php", str(ROOT / "tools/php_declaration_truth.php")], "a.php", PHP
        )
        self.assertIn("# indexed\ta.php", out)
        # No row for the `use function` import, the closure, the anonymous
        # class or C::class; the attribute and modifiers start C's span.
        self.assertEqual(
            rows,
            {
                (5, "I", "type", 5),
                (8, "C", "type", 7),
                (10, "m", "method", 10),
                (13, "inner", "method", 13),
                (19, "byRef", "method", 19),
                (20, "list", "method", 20),
                (23, "top", "function", 23),
            },
        )

    @unittest.skipUnless(shutil.which("php"), "php is required")
    def test_php_file_with_syntax_error_is_not_indexed(self):
        _, out = oracle(
            ["php", str(ROOT / "tools/php_declaration_truth.php")],
            "bad.php",
            "<?php function (",
        )
        self.assertNotIn("# indexed", out)


if __name__ == "__main__":
    unittest.main()
