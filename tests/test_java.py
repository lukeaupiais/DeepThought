"""Java declarations (opt-in language)."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought import AnalysisResult, analyze_repository
from deepthought.models import CallSite, Definition
from deepthought.analysis_tools import TreeSitterTool
from deepthought.calls import (
    CALL_SELF_METHOD,
    JAVA_ENCLOSING_METHOD,
    JAVA_STATIC_IMPORT,
    JAVA_TYPE_MEMBER,
    JAVA_TYPE_NAME,
    resolve_calls,
)
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

    def test_calls_link_by_java_name_lookup(self):
        self.maxDiff = None
        root = "src/app/"
        files = {
            root + "util/Strings.java": (
                "package app.util;\n"
                "public class Strings {\n"
                "  public static String trim(String s) { return s; }\n"
                "  public static String pad(String s) { return s; }\n"
                "  public static String pad(String s, int n) { return s; }\n"
                "  public static String join(String s, Object... rest) { return s; }\n"
                "  public static String join(int n) { return null; }\n"
                "  public static class Builder { public static Builder of() { return null; } }\n"
                "}\n"
            ),
            root + "util/Tools.java": (
                "package app.util;\npublic class Tools { public static void tool() {} }\n"
            ),
            root + "Peer.java": (
                "package app;\nclass Peer { static void ping() {} }\n"
            ),
            # The single-type import shadows this same-package Strings.
            root + "Strings.java": (
                "package app;\nclass Strings { static String trim(String s) { return s; } }\n"
            ),
            root + "Main.java": (
                "package app;\n"
                "import app.util.Strings;\n"
                "import static app.util.Strings.trim;\n"
                "import app.util.*;\n"
                "import static app.util.Tools.*;\n"
                "class Main {\n"
                "  void run(String s) {\n"
                "    helper();\n"
                "    this.helper();\n"
                "    trim(s);\n"
                "    Strings.trim(s);\n"
                "    Strings.pad(s);\n"
                "    Strings.pad(s, 1, 2);\n"
                "    Strings.join(s);\n"
                "    Strings.join(s, 1, 2);\n"
                "    Strings.join(/* one */ 1);\n"
                "    Strings.join();\n"
                "    recv(1);\n"
                "    Strings.Builder.of();\n"
                "    app.util.Strings.trim(s);\n"
                "    Peer.ping();\n"
                "    new Peer();\n"
                "    new Strings.Builder();\n"
                "    new Inner();\n"
                "    s.trim();\n"
                "    java.util.List.of();\n"
                "    missing();\n"
                "    new Tools();\n"
                "    tool();\n"
                "  }\n"
                "  void helper() {}\n"
                "  void recv(Main this, int a) {}\n"
                "  class Inner { void go() { helper(); } }\n"
                "}\n"
            ),
        }
        # Another source root with its own copy of app.util.
        files["flavor/app/util/Strings.java"] = files[root + "util/Strings.java"]
        result = analyze(files)
        symbols = {
            d.id: ("util:" if d.file == root + "util/Strings.java" else "") + d.symbol
            for d in result.definitions
            if d.file.startswith(root)
        }
        sites = {s.id: s for s in result.call_sites}
        outcomes = {
            (sites[r.call_id].callee, sites[r.call_id].line): (
                symbols.get(r.target_id),
                r.rule_id or r.reason,
            )
            for r in resolve_calls(result)
            if sites[r.call_id].file == root + "Main.java"
        }
        self.assertEqual(
            outcomes,
            {
                ("helper", 8): ("Main.helper", JAVA_ENCLOSING_METHOD),
                ("this.helper", 9): ("Main.helper", CALL_SELF_METHOD),
                ("trim", 10): ("util:Strings.trim", JAVA_STATIC_IMPORT),
                ("Strings.trim", 11): ("util:Strings.trim", JAVA_TYPE_MEMBER),
                # Overloads are told apart by argument count only.
                ("Strings.pad", 12): ("util:Strings.pad", JAVA_TYPE_MEMBER),
                ("Strings.pad", 13): (None, "no_matching_arity"),
                ("Strings.join", 14): (None, "multiple_declarations"),
                ("Strings.join", 15): ("util:Strings.join", JAVA_TYPE_MEMBER),
                # A comment is not an argument; types are not compared.
                ("Strings.join", 16): (None, "multiple_declarations"),
                ("Strings.join", 17): (None, "no_matching_arity"),
                # A receiver parameter is not counted.
                ("recv", 18): ("Main.recv", JAVA_ENCLOSING_METHOD),
                ("Strings.Builder.of", 19): (
                    "util:Strings.Builder.of",
                    JAVA_TYPE_MEMBER,
                ),
                ("app.util.Strings.trim", 20): ("util:Strings.trim", JAVA_TYPE_MEMBER),
                ("Peer.ping", 21): ("Peer.ping", JAVA_TYPE_MEMBER),
                ("Peer", 22): ("Peer", JAVA_TYPE_NAME),
                ("Strings.Builder", 23): ("util:Strings.Builder", JAVA_TYPE_NAME),
                ("Inner", 24): ("Main.Inner", JAVA_TYPE_NAME),
                ("s.trim", 25): (None, "attribute_call"),
                ("java.util.List.of", 26): (None, "attribute_call"),
                ("missing", 27): (None, "unbound_name"),
                ("Tools", 28): ("Tools", JAVA_TYPE_NAME),
                ("tool", 29): ("Tools.tool", JAVA_STATIC_IMPORT),
                # From an inner class, the outer class's method.
                ("helper", 33): ("Main.helper", JAVA_ENCLOSING_METHOD),
            },
        )

    def test_arity_is_serialized_for_java_only(self):
        java = analyze({"A.java": "class A { void f(int... xs) { f(1, 2); } }\n"})
        method = next(d for d in java.definitions if d.symbol == "A.f")
        self.assertEqual((method.parameters, method.variadic), (1, True))
        self.assertEqual([c.arguments for c in java.call_sites], [2])
        self.assertEqual(AnalysisResult.from_json(java.to_json()), java)
        data = analyze({"a.py": "def f():\n    f()\n"}, ("python",)).to_dict()
        self.assertNotIn("parameters", data["definitions"][0])
        self.assertNotIn("variadic", data["definitions"][0])
        self.assertNotIn("arguments", data["call_sites"][0])
        location = {
            "file": "A.java",
            "line": 1,
            "column": 0,
            "end_line": 1,
            "end_column": 1,
        }
        for bad in (
            {"parameters": -1},
            {"parameters": 0, "variadic": True},
            {"variadic": 1},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Definition(id="d", symbol="f", kind="Method", **location, **bad)
        with self.assertRaises(ValueError):
            CallSite(id="c", callee="f", kind="call", **location, arguments=-1)

    def test_java_is_opt_in(self):
        default = analyze({"Outer.java": SOURCE, "a.py": "x = 1\n"}, languages=None)
        self.assertEqual([c.file for c in default.coverage], ["a.py"])


if __name__ == "__main__":
    unittest.main()
