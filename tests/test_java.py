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
    JAVA_INHERITED_METHOD,
    JAVA_STATIC_IMPORT,
    JAVA_TYPE_MEMBER,
    JAVA_TYPED_RECEIVER,
    JAVA_TYPE_NAME,
    resolve_calls,
)
from deepthought.inheritance import JAVA_BASE_TYPE, resolve_supertypes
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
        self.assertEqual(
            [(t.name, t.kind) for t in result.supertypes],
            [("Base", "extends"), ("Runnable", "implements")],
        )

    def test_comments_among_type_parameters_are_skipped(self):
        java = analyze(
            {
                "src/p/J.java": "package p;\nclass J<\n  /* note */ T> { void m(int x) {} }\n"
            }
        )
        (method,) = [d for d in java.definitions if d.symbol == "J.m"]
        self.assertEqual((method.parameters, method.parameter_types), (1, ("int",)))
        typescript = analyze(
            {"c.ts": "export class C<\n  // note\n  T = any,\n> { m(x: T) {} }\n"},
            languages=("typescript",),
        )
        (method,) = [d for d in typescript.definitions if d.symbol == "C.m"]
        # Parameter counts and types are recorded for Java only.
        self.assertEqual((method.parameters, method.parameter_types), (None, None))

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
                # `String s`: String is not declared in the repository.
                ("s.trim", 25): (None, "receiver_type_not_declared"),
                ("java.util.List.of", 26): (None, "attribute_call"),
                ("missing", 27): (None, "unbound_name"),
                ("Tools", 28): ("Tools", JAVA_TYPE_NAME),
                ("tool", 29): ("Tools.tool", JAVA_STATIC_IMPORT),
                # From an inner class, the outer class's method.
                ("helper", 33): ("Main.helper", JAVA_ENCLOSING_METHOD),
            },
        )

    def test_bases_link_by_java_type_lookup(self):
        files = {
            "src/app/api/Shape.java": (
                "package app.api;\npublic interface Shape { interface Part {} }\n"
            ),
            "src/app/Base.java": "package app;\nclass Base<T> {}\n",
            "src/app/Main.java": (
                "package app;\n"
                "import app.api.Shape;\n"
                "class Main extends Base<String> implements Shape, Shape.Part,\n"
                "    java.io.Serializable, app.api.Shape {\n"
                "  interface Local extends Shape {}\n"
                "  enum Kind implements Local { A }\n"
                "  record Point() implements Local {}\n"
                "  class Inner extends Main {}\n"
                "}\n"
            ),
        }
        result = analyze(files)
        symbols = {d.id: d.symbol for d in result.definitions}
        facts = {t.id: t for t in result.supertypes}
        outcomes = sorted(
            (
                symbols[facts[r.call_id].owner_id],
                facts[r.call_id].name,
                facts[r.call_id].kind,
                symbols.get(r.target_id),
                r.rule_id or r.reason,
            )
            for r in resolve_supertypes(result)
        )
        self.assertEqual(
            outcomes,
            [
                ("Main", "Base", "extends", "Base", JAVA_BASE_TYPE),
                ("Main", "Shape", "implements", "Shape", JAVA_BASE_TYPE),
                ("Main", "Shape.Part", "implements", "Shape.Part", JAVA_BASE_TYPE),
                ("Main", "app.api.Shape", "implements", "Shape", JAVA_BASE_TYPE),
                (
                    "Main",
                    "java.io.Serializable",
                    "implements",
                    None,
                    "type_not_declared",
                ),
                ("Main.Inner", "Main", "extends", "Main", JAVA_BASE_TYPE),
                ("Main.Kind", "Local", "implements", "Main.Local", JAVA_BASE_TYPE),
                ("Main.Local", "Shape", "extends", "Shape", JAVA_BASE_TYPE),
                ("Main.Point", "Local", "implements", "Main.Local", JAVA_BASE_TYPE),
            ],
        )

    def test_type_lookup_inherits_member_types_and_honours_imports(self):
        files = {
            "src/app/LongAdder.java": "package app;\npublic class LongAdder {}\n",
            "src/ext/Z.java": "package ext;\npublic class Z {}\n",
            "lib1/app/Dup.java": "package app;\npublic class Dup {}\n",
            "lib2/app/Dup.java": "package app;\npublic class Dup {}\n",
            "src/app/Statics.java": (
                "package app;\n"
                "import static app.Outer.Builder;\n"
                "import static app.Outer.Cycle.LongAdder;\n"
                "class Statics extends Builder {}\n"
                "class Plain extends LongAdder {}\n"
                "class Holder { static class Builder {} class Use extends Builder {} }\n"
            ),
            # Outside a source root: javac could not find it by name, and
            # its bases are not followed for member types.
            "misplaced/Weird.java": (
                "package app;\n"
                "class WA { class In {} }\n"
                "class WB extends WA { class Use extends In {} }\n"
            ),
            "src/app/Diamond.java": (
                "package app;\n"
                "interface I1 { class M {} }\n"
                "interface I2 extends I1 {}\n"
                "interface I3 extends I1 {}\n"
                "class D implements I2, I3 { class X extends M {} }\n"
            ),
            "src/app/Outer.java": (
                "package app;\n"
                "class Outer {\n"
                "  static class Builder {}\n"
                "  interface Cycle extends Cycle2 {}\n"
                "  interface Cycle2 extends Cycle {}\n"
                "}\n"
            ),
            "src/app/Main.java": (
                "package app;\n"
                "import java.util.concurrent.atomic.LongAdder;\n"
                "import other.ext;\n"
                "class Main extends Outer {\n"
                "  class Named extends ext.Z {}\n"
                "  class Twin extends Dup {}\n"
                "  class Mine extends Builder {}\n"
                "  class Deep extends Main.Builder {}\n"
                "  class Adder extends LongAdder {}\n"
                "  class Loop implements Outer.Cycle.Missing {}\n"
                "  void run() { class Local {} new Local(); new Builder(); }\n"
                "}\n"
                "class P extends Outer { interface Outer {} }\n"
                "class R extends P.Builder {}\n"
                "class Own extends Outer {\n"
                "  class Builder {}\n"
                "  class Mine extends Builder {}\n"
                "}\n"
            ),
        }
        result = analyze(files)
        symbols = {d.id: d.symbol for d in result.definitions}
        facts = {t.id: t for t in result.supertypes}
        self.assertEqual(
            sorted(
                (
                    symbols[facts[r.call_id].owner_id],
                    symbols.get(r.target_id),
                    r.rule_id or r.reason,
                )
                for r in resolve_supertypes(result)
                if facts[r.call_id].file != "src/app/Outer.java"
            ),
            [
                ("D", "I2", JAVA_BASE_TYPE),
                ("D", "I3", JAVA_BASE_TYPE),
                # Inherited along two paths, one member type: not ambiguous.
                ("D.X", "I1.M", JAVA_BASE_TYPE),
                # A member type found by lexical lookup is not joined by a
                # static import of the same name.
                ("Holder.Use", "Holder.Builder", JAVA_BASE_TYPE),
                ("I2", "I1", JAVA_BASE_TYPE),
                ("I3", "I1", JAVA_BASE_TYPE),
                ("Main", "Outer", JAVA_BASE_TYPE),
                # An imported type outside the repository shadows app.LongAdder.
                ("Main.Adder", None, "type_not_declared"),
                ("Main.Deep", "Outer.Builder", JAVA_BASE_TYPE),
                # Cyclic bases end the inherited lookup.
                ("Main.Loop", None, "type_not_declared"),
                # A member type Main inherits from Outer.
                ("Main.Mine", "Outer.Builder", JAVA_BASE_TYPE),
                # An imported name is not read as a package.
                ("Main.Named", None, "type_not_declared"),
                # Two other source roots declare it; the build decides which.
                ("Main.Twin", None, "multiple_declarations"),
                ("Own", "Outer", JAVA_BASE_TYPE),
                # A declared member type hides the inherited one.
                ("Own.Mine", "Own.Builder", JAVA_BASE_TYPE),
                # P's base is named outside P, so it is app.Outer, not P.Outer.
                ("P", "Outer", JAVA_BASE_TYPE),
                # The static import names no type Cycle declares; the package
                # type stands.
                ("Plain", "LongAdder", JAVA_BASE_TYPE),
                ("R", "Outer.Builder", JAVA_BASE_TYPE),
                # A member type imported with import static.
                ("Statics", "Outer.Builder", JAVA_BASE_TYPE),
                ("WB", "WA", JAVA_BASE_TYPE),
                ("WB.Use", None, "type_not_declared"),
            ],
        )
        sites = {c.id: c for c in result.call_sites}
        self.assertEqual(
            sorted(
                (sites[r.call_id].callee, symbols.get(r.target_id), r.rule_id)
                for r in resolve_calls(result)
            ),
            [
                ("Builder", "Outer.Builder", JAVA_TYPE_NAME),
                ("Local", "Main.run.Local", JAVA_TYPE_NAME),
            ],
        )

    def test_calls_find_inherited_methods(self):
        files = {
            "src/app/Base.java": (
                "package app;\n"
                "class Base implements Named {\n"
                "  void run() {}\n"
                "  void put(int a) {}\n"
                "  void put(int a, int b) {}\n"
                "  static void util() {}\n"
                "}\n"
            ),
            "src/app/Named.java": (
                "package app;\n"
                "interface Named { String name(); default void hello() {} }\n"
            ),
            "src/app/Other.java": (
                "package app;\ninterface Other { void hello(); void put(int a); }\n"
            ),
            "src/app/Loop.java": (
                "package app;\n"
                "interface Loop extends Loop2 {}\ninterface Loop2 extends Loop {}\n"
            ),
            "src/app/Main.java": (
                "package app;\n"
                "class Main extends Base {\n"
                "  void put(int a) {}\n"
                "  void go() {\n"
                "    run();\n"
                "    this.run();\n"
                "    put(1);\n"
                "    put(1, 2);\n"
                "    name();\n"
                "    Main.util();\n"
                "    hello();\n"
                "  }\n"
                "  class Inner { void go() { run(); } }\n"
                "}\n"
                "abstract class Both extends Base implements Other {\n"
                "  void go() { hello(); put(1); }\n"
                "}\n"
                "abstract class Cyclic implements Loop { void go() { none(); } }\n"
                "abstract class Two implements Named, Other {\n"
                "  void go() { hello(); }\n"
                "}\n"
                "interface Three extends Named, Other {\n"
                "  default void go() { hello(); }\n"
                "}\n"
                "interface Sub extends Named { String name(); }\n"
                "abstract class Partial implements Named {}\n"
                "abstract class Mixed extends Partial implements Sub {\n"
                "  void go() { name(); }\n"
                "}\n"
            ),
        }
        result = analyze(files)
        symbols = {d.id: d.symbol for d in result.definitions}
        sites = {s.id: s for s in result.call_sites}
        self.assertEqual(
            sorted(
                (
                    sites[r.call_id].line,
                    sites[r.call_id].callee,
                    symbols.get(r.target_id),
                    r.rule_id or r.reason,
                )
                for r in resolve_calls(result)
                if sites[r.call_id].file == "src/app/Main.java"
            ),
            [
                (5, "run", "Base.run", JAVA_INHERITED_METHOD),
                (6, "this.run", "Base.run", JAVA_INHERITED_METHOD),
                # Main's own put(int) overrides Base's; put(int, int) is
                # inherited.
                (7, "put", "Main.put", JAVA_ENCLOSING_METHOD),
                (8, "put", "Base.put", JAVA_INHERITED_METHOD),
                # Through Base's interface.
                (9, "name", "Named.name", JAVA_INHERITED_METHOD),
                (10, "Main.util", "Base.util", JAVA_INHERITED_METHOD),
                (11, "hello", "Named.hello", JAVA_INHERITED_METHOD),
                # From an inner class: the outer type's inherited method.
                (13, "run", "Base.run", JAVA_INHERITED_METHOD),
                # Base's own put implements Other's; Base only inherits
                # hello from Named, which does not hide Other's.
                (16, "hello", None, "multiple_declarations"),
                (16, "put", "Base.put", JAVA_INHERITED_METHOD),
                (18, "none", None, "unbound_name"),
                # Two interfaces with a same-arity method: types decide.
                (20, "hello", None, "multiple_declarations"),
                # An interface's first base is not a superclass.
                (23, "hello", None, "multiple_declarations"),
                # Partial only inherits Named.name, so it does not hide
                # Sub's; which interface method wins is not decided by name.
                (28, "name", None, "multiple_declarations"),
            ],
        )

    def test_private_methods_are_not_inherited(self):
        files = {
            "src/p/Util.java": (
                "package p;\nclass Util { static int get(Object o) { return 0; } }\n"
            ),
            "src/p/Base.java": (
                "package p;\n"
                "class Base {\n"
                "  private static int get(Object o) { return 1; }\n"
                "  private void own() { get(null); }\n"  # 4: its own
                "  void open() {}\n"
                "}\n"
            ),
            "src/p/Sub.java": (
                "package p;\n"
                "import static p.Util.get;\n"
                "class Sub extends Base {\n"
                "  void m(Sub s) {\n"
                "    get(null);\n"  # 5: the import, not Base's private get
                "    s.own();\n"  # 6: not inherited
                "    s.open();\n"  # 7
                "    new Base() {\n"
                "      void f() { own(); }\n"  # 9: not inherited either
                "    };\n"
                "  }\n"
                "}\n"
            ),
        }
        result = analyze(files)
        methods = {d.symbol: d for d in result.definitions if d.kind == "Method"}
        self.assertTrue(methods["Base.own"].private)
        self.assertIsNone(methods["Base.open"].private)
        self.assertEqual(AnalysisResult.from_json(result.to_json()), result)
        symbols = {d.id: d.symbol for d in result.definitions}
        sites = {s.id: s for s in result.call_sites}
        found = {
            (sites[r.call_id].file, sites[r.call_id].line): (
                symbols.get(r.target_id),
                r.rule_id or r.reason,
            )
            for r in resolve_calls(result)
        }
        self.assertEqual(
            found[("src/p/Base.java", 4)],
            ("Base.get", JAVA_ENCLOSING_METHOD),
        )
        self.assertEqual(found[("src/p/Sub.java", 5)], ("Util.get", JAVA_STATIC_IMPORT))
        self.assertEqual(found[("src/p/Sub.java", 6)], (None, "not_declared_in_type"))
        self.assertEqual(
            found[("src/p/Sub.java", 7)], ("Base.open", JAVA_INHERITED_METHOD)
        )
        self.assertEqual(found[("src/p/Sub.java", 9)], (None, "unbound_name"))

    def test_calls_on_variables_whose_declaration_writes_the_type(self):
        files = {
            "src/p/Foo.java": (
                "package p;\n"
                "class Foo extends Root {\n"
                "  void go() {}\n"
                "  static class Inner { void in() {} }\n"
                "}\n"
                "class Root { void up() {} }\n"
                "class Box<T> { void open() {} }\n"
            ),
            "src/p/Main.java": (
                "package p;\n"
                "class Main<T> {\n"
                "  Foo field;\n"
                "  T held;\n"
                "  <U> void m(Foo p, U u, Foo[] arr, Foo... more) {\n"  # 5
                "    p.go();\n"  # 6: parameter
                "    Foo x = new Foo();\n"
                "    x.go();\n"  # 8: local
                "    var y = new Foo();\n"
                "    y.up();\n"  # 10: var, inherited method
                "    field.go();\n"  # 11: field
                "    this.field.go();\n"  # 12
                "    for (Foo z : arr) { z.go(); }\n"  # 13: enhanced for
                "    try (Foo w = make()) { w.go(); }\n"  # 14: resource
                "    catch (Foo e) { e.go(); }\n"  # 15: catch
                "    Runnable r = (Foo a) -> a.go();\n"  # 16: typed lambda
                "    Box<Foo> b = null;\n"
                "    b.open();\n"  # 18: generic type
                "    Foo.Inner i = null;\n"
                "    i.in();\n"  # 20: qualified type
                "    arr.clone();\n"  # 21: array
                "    more.clone();\n"  # 22: variable arity
                "    u.go();\n"  # 23: type parameter
                "    held.go();\n"  # 24: class type parameter
                "    later.go();\n"  # 25: declared after
                "    Foo later = null;\n"
                "    { Foo hidden = null; }\n"
                "    hidden.go();\n"  # 28: in a block that ended
                "    if (p instanceof Foo g) { g.go(); }\n"  # 29: pattern
                "    Runnable s = q -> q.go();\n"  # 30: inferred lambda
                "    x.go(1);\n"  # 31: no such arity
                "    new Runnable() { public void run() { x.go(); } };\n"  # 32
                "  }\n"
                "  class Sub extends Root { void s() { field.go(); } }\n"  # 34
                "  Foo make() { return null; }\n"
                "  void n(Object o) {\n"
                "    if (o instanceof Box field) { field.open(); }\n"  # 37
                "    switch (o) { case Box field -> field.open(); default -> {} }\n"
                "    Foo c[] = null;\n"
                "    c.clone();\n"  # 40: an array declarator
                "    try { } catch (Foo | Root e2) { e2.go(); }\n"  # 41: multi-catch
                "    var v = make();\n"
                "    v.go();\n"  # 43: var without new
                "    Runnable t = (var d) -> d.go();\n"  # 44
                "  }\n"
                "  class In { void t() { this.field.go(); } }\n"  # 46
                "  enum E { field; void f() { field.go(); } }\n"  # 47
                "  class Impl implements Runnable { void f() { field.go(); } }\n"
                "  interface J extends Runnable { default void f() { field.go(); } }\n"
                "  enum K { A { void f() { field.go(); } } }\n"  # 50
                "}\n"
            ),
        }
        result = analyze(files)
        symbols = {d.id: d.symbol for d in result.definitions}
        sites = {s.id: s for s in result.call_sites}
        found = {
            (sites[r.call_id].line, sites[r.call_id].callee): (
                symbols.get(r.target_id),
                r.rule_id or r.reason,
            )
            for r in resolve_calls(result)
            if sites[r.call_id].file == "src/p/Main.java"
        }
        typed = ("Foo.go", JAVA_TYPED_RECEIVER)
        self.assertEqual(found[(6, "p.go")], typed)
        self.assertEqual(found[(8, "x.go")], typed)
        self.assertEqual(found[(10, "y.up")], ("Root.up", JAVA_INHERITED_METHOD))
        self.assertEqual(found[(11, "field.go")], typed)
        self.assertEqual(found[(12, "this.field.go")], typed)
        self.assertEqual(found[(13, "z.go")], typed)
        self.assertEqual(found[(14, "w.go")], typed)
        self.assertEqual(found[(15, "e.go")], typed)
        self.assertEqual(found[(16, "a.go")], typed)
        self.assertEqual(found[(18, "b.open")], ("Box.open", JAVA_TYPED_RECEIVER))
        self.assertEqual(found[(20, "i.in")], ("Foo.Inner.in", JAVA_TYPED_RECEIVER))
        self.assertEqual(found[(31, "x.go")], (None, "no_matching_arity"))
        for line, callee in (
            (21, "arr.clone"),
            (22, "more.clone"),
            (23, "u.go"),
            (24, "held.go"),
            (25, "later.go"),
            (28, "hidden.go"),
            (29, "g.go"),
            (30, "q.go"),
            # A class with a superclass may inherit a field of that name.
            (34, "field.go"),
            # Pattern variables: their scope follows control flow.
            (37, "field.open"),
            (38, "field.open"),
            (40, "c.clone"),
            (41, "e2.go"),
            (43, "v.go"),
            (44, "d.go"),
            # `this.field` is a field of the innermost class only.
            (46, "this.field.go"),
            # An enum constant hides the outer field.
            (47, "field.go"),
            # An anonymous class, a type with superinterfaces and an enum
            # constant's body may each inherit a field of that name.
            (32, "x.go"),
            (48, "field.go"),
            (49, "field.go"),
            (50, "field.go"),
        ):
            with self.subTest(line=line, callee=callee):
                self.assertEqual(found[(line, callee)], (None, "attribute_call"))

    def test_an_override_needs_parameter_types_that_may_be_the_same(self):
        files = {
            "src/p/Range.java": (
                "package p;\n"
                "import java.util.Map;\n"
                "class Range<T> {\n"
                "  T fit(T x) { return x; }\n"
                "  void up(java.util.List<String> xs) {}\n"
                "  void put(byte[] b) {}\n"
                "  void all(T[] xs) {}\n"
                "  void entry(Map.Entry e) {}\n"
                "  <U> void any(U u) {}\n"
                "  void arr(String[] a) {}\n"
                "}\n"
                "class LongRange extends Range<Long> {\n"
                "  long fit(long x) { return x; }\n"
                "  void up(java.util.List<Long> xs) {}\n"
                "  void put(java.nio.ByteBuffer b) {}\n"
                "  void all(Long xs) {}\n"
                "  void entry(Entry e) {}\n"
                "  void any(String u) {}\n"
                "  void arr(String a) {}\n"
                "}\n"
                "class Box extends Range<String> {\n"
                "  String fit(String x) { return x; }\n"
                "}\n"
                "class Main {\n"
                "  void m(LongRange r, Box b) {\n"
                "    r.fit(1L);\n"
                "    r.up(null);\n"
                "    r.put(null);\n"
                "    r.all(null);\n"
                "    r.entry(null);\n"
                "    r.any(null);\n"
                "    b.fit(null);\n"
                "    r.arr(null);\n"
                "  }\n"
                "}\n"
            ),
        }
        result = analyze(files)
        methods = {d.symbol: d for d in result.definitions if d.kind == "Method"}
        self.assertEqual(methods["Range.fit"].parameter_types, ("*",))
        self.assertEqual(methods["Range.all"].parameter_types, ("*[]",))
        self.assertEqual(methods["Range.any"].parameter_types, ("?",))
        self.assertEqual(methods["LongRange.up"].parameter_types, ("java.util.List",))
        self.assertEqual(methods["Range.entry"].parameter_types, ("Map.Entry",))
        self.assertEqual(AnalysisResult.from_json(result.to_json()), result)
        symbols = {d.id: d.symbol for d in result.definitions}
        sites = {s.id: s for s in result.call_sites}
        found = {
            sites[r.call_id].callee: (
                symbols.get(r.target_id),
                sorted(symbols[c] for c in r.candidates),
                r.rule_id or r.reason,
            )
            for r in resolve_calls(result)
        }

        def both(name):
            return (
                None,
                [f"LongRange.{name}", f"Range.{name}"],
                "multiple_declarations",
            )

        def near(symbol):
            return (symbol, [], JAVA_TYPED_RECEIVER)

        # A type parameter is not the same type as written: both stay.
        self.assertEqual(found["r.fit"], both("fit"))
        # Type arguments are not compared, as for erasure.
        self.assertEqual(found["r.up"], near("LongRange.up"))
        # Two different types as written: overloads.
        self.assertEqual(found["r.put"], both("put"))
        # A type parameter's array is not a single type.
        self.assertEqual(found["r.all"], both("all"))
        # A simple name may be the imported qualified one.
        self.assertEqual(found["r.entry"], near("LongRange.entry"))
        # A different array dimension: an overload.
        self.assertEqual(found["r.arr"], both("arr"))
        # A method's own type parameter erases to its bound: an overload.
        self.assertEqual(found["r.any"], both("any"))
        # Whether a reference type stands for the class's type parameter
        # depends on the base's type arguments, which are not followed.
        self.assertEqual(
            found["b.fit"], (None, ["Box.fit", "Range.fit"], "multiple_declarations")
        )

    def test_calls_in_anonymous_classes_look_there_first(self):
        files = {
            "src/p/Base.java": (
                "package p;\n"
                "abstract class Base {\n"
                "  abstract void go();\n"
                "  boolean isRunning() { return true; }\n"
                "  Base() {}\n"
                "  Base(Object o) {}\n"
                "}\n"
            ),
            "src/p/A.java": (
                "package p;\n"
                "class A {\n"
                "  boolean isRunning() { return false; }\n"
                "  void rm(int x) {}\n"
                "  void helper() {}\n"
                "  Object make() {\n"
                "    return new Base() {\n"
                "      void rm(int x) {}\n"
                "      void go() {\n"
                "        rm(1); isRunning(); helper(); this.isRunning();\n"
                "        new Runnable() { public void run() { rm(2); go(); this.go(); } };\n"
                "        class L { void h() { rm(4); } }\n"
                "      }\n"
                "    };\n"
                "  }\n"
                "  Runnable field = new Runnable() {\n"
                "    public void run() { helper(); }\n"
                "  };\n"
                "  void after() { rm(3); }\n"
                "}\n"
                "enum E {\n"
                "  ONE { void f() { g(); } };\n"
                "  void f() {}\n"
                "  void g() {}\n"
                "  void local() {\n"
                "    enum L { TWO { void f() { g(); } };\n"
                "      void f() {}\n"
                "      void g() {} }\n"
                "  }\n"
                "}\n"
                "class Z extends A {\n"
                "  Object two = new Base(make()) { void go() {} void x() { this.go(); } };\n"
                "}\n"
            ),
        }
        result = analyze(files)
        symbols = {d.id: (d.symbol, d.line) for d in result.definitions}
        sites = {s.id: s for s in result.call_sites}
        a = "src/p/A.java"
        base = ("Base", "A.make", 7, 14)
        self.assertEqual(
            {
                (s.line, s.callee): s.anonymous
                for s in result.call_sites
                if s.file == a and s.kind == "call"
            },
            {
                (10, "rm"): (base,),
                (10, "isRunning"): (base,),
                (10, "helper"): (base,),
                (10, "this.isRunning"): (base,),
                (11, "rm"): (("Runnable", "A.make.go", 11, 11), base),
                (11, "go"): (("Runnable", "A.make.go", 11, 11), base),
                (11, "this.go"): (("Runnable", "A.make.go", 11, 11), base),
                (12, "rm"): (base,),
                (17, "helper"): (("Runnable", "A", 16, 18),),
                (19, "rm"): None,
                (22, "g"): (("E", "E", 22, 22),),
                (26, "g"): (("L", "E.local.L", 26, 26),),
                # An argument of the new is outside the class body.
                (32, "make"): None,
                (32, "this.go"): (("Base", "Z", 32, 32),),
            },
        )
        self.assertEqual(
            sorted(
                (
                    sites[r.call_id].line,
                    sites[r.call_id].callee,
                    symbols.get(r.target_id),
                    r.rule_id or r.reason,
                )
                for r in resolve_calls(result)
                if sites[r.call_id].file == a and sites[r.call_id].kind == "call"
            ),
            [
                (10, "helper", ("A.helper", 5), JAVA_ENCLOSING_METHOD),
                (10, "isRunning", ("Base.isRunning", 4), JAVA_INHERITED_METHOD),
                # The anonymous class's own rm, not A's.
                (10, "rm", ("A.make.rm", 8), JAVA_ENCLOSING_METHOD),
                (10, "this.isRunning", ("Base.isRunning", 4), JAVA_INHERITED_METHOD),
                # Runnable is outside the repository: the next class out.
                (11, "go", ("A.make.go", 9), JAVA_ENCLOSING_METHOD),
                (11, "rm", ("A.make.rm", 8), JAVA_ENCLOSING_METHOD),
                # this is the innermost anonymous class, which has no go here.
                (11, "this.go", None, "not_declared_in_class"),
                # A local class inside the anonymous class: then the
                # anonymous class, before A.
                (12, "rm", ("A.make.rm", 8), JAVA_ENCLOSING_METHOD),
                (17, "helper", ("A.helper", 5), JAVA_ENCLOSING_METHOD),
                (19, "rm", ("A.rm", 4), JAVA_ENCLOSING_METHOD),
                # An enum constant's body inherits from its enum.
                (22, "g", ("E.g", 24), JAVA_INHERITED_METHOD),
                (26, "g", ("E.local.L.g", 28), JAVA_INHERITED_METHOD),
                (32, "make", ("A.make", 6), JAVA_INHERITED_METHOD),
                # A body on one line: its go, not Base's.
                (32, "this.go", ("Z.go", 32), CALL_SELF_METHOD),
            ],
        )
        data = result.to_dict()
        self.assertNotIn(
            "anonymous", next(c for c in data["call_sites"] if c["line"] == 19)
        )
        self.assertEqual(AnalysisResult.from_json(result.to_json()), result)
        location = {
            "file": "A.java",
            "line": 1,
            "column": 0,
            "end_line": 1,
            "end_column": 1,
        }
        for bad in (
            (),
            (("Base", "A"),),
            (("", "A", 1, 2),),
            (("Base", "A", 2, 1),),
            (("Base", "A", 0, 1),),
            [("Base", "A", 1, 2)],
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                CallSite(id="c", callee="f", kind="call", **location, anonymous=bad)

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
