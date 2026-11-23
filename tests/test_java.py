"""Java declarations (opt-in language)."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought import analyze_repository
from deepthought.analysis_tools import TreeSitterTool
from deepthought.imports import (
    JAVA_PACKAGE_MEMBER,
    JAVA_SAME_SOURCE_ROOT,
    resolve_imports,
)

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
        self.assertEqual(
            [(i.specifier, i.names, i.local_names) for i in result.imports],
            [("java.util.List", (), ("List",))],
        )
        self.assertEqual([p.name for p in result.packages], ["app"])
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

    def test_imports_link_to_the_declaring_files_by_package_and_source_root(self):
        self.maxDiff = None
        core = "core/src/main/java/app/core/"
        util = (
            "package app.core;\n"
            "public class Util {\n"
            "  public static class Nested {}\n"
            "  public static int helper() { class Local {} return 0; }\n"
            "}\n"
        )
        files = {
            core + "Util.java": util,
            core + "Other.java": "package app.core;\nclass Other {}\n",
            core + "package-info.java": "package app.core;\n",
            # Two files of one root declaring the same type.
            core + "Twin.java": "package app.core;\nclass Twin {}\n",
            core + "TwinCopy.java": "package app.core;\nclass Twin {}\n",
            "core/src/main/java/app/Main.java": (
                "package app;\n"
                "import app.core.Util;\n"
                "import app . core . Util . Nested;\n"
                "import static app.core.Util.helper;\n"
                "import app.core.*;\n"
                "import static app.core.Util.*;\n"
                "import app.core.Util.helper.Local;\n"
                "import java.util.List;\n"
                "import app.core.Twin;\n"
                "class Main {}\n"
            ),
            # Another flavor of the same package, and a root with neither.
            "flavor/src/app/core/Util.java": util,
            "flavor/src/app/Use.java": "package app;\nimport app.core.Util;\n",
            "tests/src/app/T.java": (
                "package app;\nimport app.core.Util;\nimport app.core.Other;\n"
            ),
            # Not under its package path, so javac could not find it by name.
            "misplaced/Stray.java": "package app.stray;\nclass Stray {}\n",
            "misplaced/Uses.java": "package app;\nimport app.stray.Stray;\n",
        }
        result = analyze(files)
        imports = {i.id: i for i in result.imports}
        outcomes = {
            (
                imports[r.import_id].file,
                imports[r.import_id].specifier,
                imports[r.import_id].names,
            ): (
                r.status,
                r.reason,
                [(link.target_file, link.rule_id) for link in r.links],
                r.candidates,
            )
            for r in resolve_imports(result)
        }
        main = "core/src/main/java/app/Main.java"
        # flavor/ declares app.core.Util too, so the importer's root decides.
        own = [(core + "Util.java", JAVA_SAME_SOURCE_ROOT)]
        self.assertEqual(
            outcomes,
            {
                (main, "app.core.Util", ()): ("linked", None, own, ()),
                (main, "app.core.Util.Nested", ()): ("linked", None, own, ()),
                (main, "app.core.Util", ("helper",)): ("linked", None, own, ()),
                # A package wildcard reaches every file declaring a type.
                (main, "app.core", ("*",)): (
                    "linked",
                    None,
                    [
                        (core + "Other.java", JAVA_SAME_SOURCE_ROOT),
                        (core + "Twin.java", JAVA_SAME_SOURCE_ROOT),
                        (core + "TwinCopy.java", JAVA_SAME_SOURCE_ROOT),
                        (core + "Util.java", JAVA_SAME_SOURCE_ROOT),
                    ],
                    (),
                ),
                (main, "app.core.Util", ("*",)): ("linked", None, own, ()),
                (main, "app.core.Util.helper.Local", ()): (
                    "unresolved",
                    "not_in_repository",
                    [],
                    (),
                ),
                (main, "app.core.Twin", ()): (
                    "ambiguous",
                    "multiple_declarations",
                    [],
                    (core + "Twin.java", core + "TwinCopy.java"),
                ),
                (main, "java.util.List", ()): (
                    "unresolved",
                    "not_in_repository",
                    [],
                    (),
                ),
                ("flavor/src/app/Use.java", "app.core.Util", ()): (
                    "linked",
                    None,
                    [("flavor/src/app/core/Util.java", JAVA_SAME_SOURCE_ROOT)],
                    (),
                ),
                ("tests/src/app/T.java", "app.core.Util", ()): (
                    "ambiguous",
                    "multiple_source_roots",
                    [],
                    (core + "Util.java", "flavor/src/app/core/Util.java"),
                ),
                # Only one root declares Other.
                ("tests/src/app/T.java", "app.core.Other", ()): (
                    "linked",
                    None,
                    [(core + "Other.java", JAVA_PACKAGE_MEMBER)],
                    (),
                ),
                ("misplaced/Uses.java", "app.stray.Stray", ()): (
                    "unresolved",
                    "not_in_repository",
                    [],
                    (),
                ),
            },
        )

    @unittest.skipUnless(
        importlib.util.find_spec("tree_sitter_go"), "Provision the Go grammar"
    )
    def test_java_package_clauses_do_not_join_go_packages(self):
        result = analyze(
            {
                "go.mod": "module example.com/m\n",
                "main.go": 'package main\nimport "example.com/m/lib"\n',
                "lib/x.go": "package lib\n",
                "lib/Y.java": "package lib;\nclass Y {}\n",
            },
            languages=("go", "java"),
        )
        (resolution,) = resolve_imports(result)
        self.assertEqual([link.target_file for link in resolution.links], ["lib/x.go"])

    def test_java_is_opt_in(self):
        default = analyze({"Outer.java": SOURCE, "a.py": "x = 1\n"}, languages=None)
        self.assertEqual([c.file for c in default.coverage], ["a.py"])


if __name__ == "__main__":
    unittest.main()
