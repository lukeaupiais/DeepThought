"""Ruby declarations (opt-in language)."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought import analyze_repository
from deepthought.syntax import declaration_sources, make_parser

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_ruby"
)
SOURCE = """module A
  class B::C < D
    def m; end
    def self.s(x) = x
    class << self
      def t; end
    end
    def ==(other); end
    def x=(value); end
    def ok?; end
    attr_reader :y
    included do
      def from_block; end
    end
    def outer
      def runtime; end
      class Hidden; end
    end
  end
  class ::Top; end
end
def top; end
X = Class.new do
  def anonymous; end
end
class self::Dynamic
  def unnamed_scope; end
end
"""


def analyze(files, languages=("ruby",)):
    with tempfile.TemporaryDirectory() as directory:
        for name, content in files.items():
            path = Path(directory) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        options = {} if languages is None else {"languages": languages}
        return analyze_repository(directory, repository_id="fixture", **options)


@unittest.skipUnless(READY, "Ruby grammar is not installed")
class RubyTests(unittest.TestCase):
    def test_modules_classes_and_methods(self):
        result = analyze({"lib/a.rb": SOURCE})
        self.assertEqual(result.status, "succeeded")
        self.maxDiff = None
        self.assertEqual(
            [(d.line, d.symbol, d.kind) for d in result.definitions],
            [
                (1, "A", "Module"),
                # The written path joins the enclosing module.
                (2, "A.B.C", "Class"),
                (3, "A.B.C.m", "Method"),
                # Singleton methods, also in `class << self`, are methods.
                (4, "A.B.C.s", "Method"),
                (6, "A.B.C.t", "Method"),
                (8, "A.B.C.==", "Method"),
                (9, "A.B.C.x=", "Method"),
                (10, "A.B.C.ok?", "Method"),
                # A block in a class body (`included do`) defines methods of
                # the class.
                (13, "A.B.C.from_block", "Method"),
                # A def or class in a method body is defined when it runs.
                (15, "A.B.C.outer", "Method"),
                # `::Top` starts at the top.
                (20, "Top", "Class"),
                (22, "top", "Function"),
                (24, "anonymous", "Function"),
                # A class whose path starts with an expression is not named,
                # and neither is anything in it.
            ],
        )
        self.assertEqual((result.imports, result.call_sites), ((), ()))

    def test_ruby_is_opt_in(self):
        default = analyze({"a.rb": SOURCE, "a.py": "x = 1\n"}, languages=None)
        self.assertEqual([c.file for c in default.coverage], ["a.py"])

    def test_declaration_sources_name_the_declared_name(self):
        content = b"class A::B\n  def self.go; end\nend\n"
        sources = declaration_sources(
            make_parser("ruby", ".rb").parse(content),
            content,
            "fixture",
            "a.rb",
            language="ruby",
        )
        self.assertEqual(
            sorted((s["source_code"], s["name_range_utf8"]) for s in sources.values()),
            [
                ("class A::B\n  def self.go; end\nend", [1, 9, 1, 10]),
                ("def self.go; end", [2, 11, 2, 13]),
            ],
        )


if __name__ == "__main__":
    unittest.main()
