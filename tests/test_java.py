"""Java declarations (opt-in language)."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought import analyze_repository
from deepthought.analysis_tools import TreeSitterTool

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_java"
)
SOURCE = """package app;

import java.util.List;

/** Not part of the declaration's text. */
@Deprecated
public class Outer<T> extends Base implements Runnable {
  int field;
  public Outer() { this(0); }
  Outer(int value) { }
  public void run() {
    class Local { void inside() {} }
    Runnable anonymous = new Runnable() { public void run() {} };
    Runnable lambda = () -> helper();
  }
  void run(int times) { }
  static class Inner {}
  interface Shape { double area(); default String label() { return "é"; } }
  enum Color { RED { String tint() { return "r"; } }, BLUE; String code() { return ""; } }
  record Point(int x, int y) { Point { } static Point origin() { return null; } }
  @interface Tag { String value() default ""; }
}
"""


def analyze(files, languages=("java",)):
    with tempfile.TemporaryDirectory() as directory:
        for name, content in files.items():
            path = Path(directory) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        options = {} if languages is None else {"languages": languages}
        return analyze_repository(directory, repository_id="fixture", **options)


@unittest.skipUnless(READY, "Provision the Java grammar")
class JavaTests(unittest.TestCase):
    def test_declarations_kinds_and_lexical_names(self):
        result = analyze({"app/Outer.java": SOURCE})
        self.assertEqual(
            [(d.symbol, d.kind) for d in result.definitions],
            [
                ("Outer", "Class"),
                ("Outer.Outer", "Constructor"),
                ("Outer.Outer", "Constructor"),
                ("Outer.run", "Method"),
                ("Outer.run.Local", "Class"),
                ("Outer.run.Local.inside", "Method"),
                # A method of an anonymous class belongs to the enclosing scope.
                ("Outer.run.run", "Method"),
                ("Outer.run", "Method"),
                ("Outer.Inner", "Class"),
                ("Outer.Shape", "Interface"),
                ("Outer.Shape.area", "Method"),
                ("Outer.Shape.label", "Method"),
                ("Outer.Color", "Enum"),
                ("Outer.Color.tint", "Method"),
                ("Outer.Color.code", "Method"),
                ("Outer.Point", "Record"),
                ("Outer.Point.Point", "Constructor"),
                ("Outer.Point.origin", "Method"),
                ("Outer.Tag", "Annotation"),
                ("Outer.Tag.value", "Method"),
            ],
        )
        # Overloads keep distinct identities.
        self.assertEqual(len({d.id for d in result.definitions}), 20)
        self.assertEqual([c.status for c in result.coverage], ["analyzed"])
        self.assertEqual(result.imports, ())
        self.assertEqual(result.call_sites, ())
        self.assertEqual(result.supertypes, ())

    def test_annotations_belong_to_the_text_and_javadoc_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "Outer.java").write_text(SOURCE)
            output = asyncio.run(
                TreeSitterTool(directory).analyze("r", languages=("java",))
            )
        symbols = {
            (n.properties["name"], n.properties["start_line"]): n.properties
            for n in output.nodes
            if "Symbol" in n.labels
        }
        outer = symbols[("Outer", 6)]
        self.assertTrue(outer["source_code"].startswith("@Deprecated\npublic class"))
        self.assertEqual(outer["name_range_utf8"], [7, 13, 7, 18])
        label = symbols[("Outer.Shape.label", 18)]
        self.assertEqual(label["source_code"], 'default String label() { return "é"; }')
        names = {n.properties["qualified_name"]: n.properties for n in output.nodes}
        nested = {
            (
                names[r.source_match["qualified_name"]]["name"],
                names[r.target_match["qualified_name"]]["name"],
            )
            for r in output.relationships
            if r.type == "CONTAINS" and "Symbol" in r.source_labels
        }
        self.assertIn(("Outer.Point", "Outer.Point.origin"), nested)
        self.assertIn(("Outer.run", "Outer.run.run"), nested)

    def test_java_is_opt_in(self):
        default = analyze({"Outer.java": SOURCE, "a.py": "x = 1\n"}, languages=None)
        self.assertEqual([c.file for c in default.coverage], ["a.py"])


if __name__ == "__main__":
    unittest.main()
