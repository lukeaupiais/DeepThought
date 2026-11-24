"""Java declarations (opt-in language)."""

import asyncio
from dataclasses import replace
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from deepthought import AnalysisResult, analyze_repository
from deepthought.models import CallSite, Definition
from deepthought.analysis_tools import TreeSitterTool
from deepthought.calls import (
    CALL_SELF_METHOD,
    JAVA_ENCLOSING_METHOD,
    JAVA_FIELD_ARGUMENT,
    JAVA_INHERITED_METHOD,
    JAVA_JDK_SUPERTYPES,
    JAVA_STATIC_IMPORT,
    JAVA_PLATFORM_ARGUMENT,
    JAVA_TYPE_MEMBER,
    JAVA_CHAINED_RECEIVER,
    JAVA_STATIC_FIELD_RECEIVER,
    JAVA_TYPED_RECEIVER,
    JAVA_TYPE_NAME,
    _JavaScope,
    _overridden,
    name_bindings,
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

    def test_parameter_types_are_recorded_for_java_only(self):
        typescript = analyze(
            {"c.ts": "export class C<T> { m(x: T) {} }\n"},
            languages=("typescript",),
        )
        (method,) = [d for d in typescript.definitions if d.symbol == "C.m"]
        # TypeScript parameter lists are formal_parameters too.
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
                # Overloads are told apart by argument count, then by the
                # argument types written (see the overload test).
                ("Strings.pad", 12): ("util:Strings.pad", JAVA_TYPE_MEMBER),
                ("Strings.pad", 13): (None, "no_matching_arity"),
                # `s` is a String: only join(String, Object...) applies.
                ("Strings.join", 14): ("util:Strings.join", JAVA_TYPE_MEMBER),
                ("Strings.join", 15): ("util:Strings.join", JAVA_TYPE_MEMBER),
                # A comment is not an argument; `1` selects join(int).
                ("Strings.join", 16): ("util:Strings.join", JAVA_TYPE_MEMBER),
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

    def test_calls_on_the_result_of_a_call(self):
        files = {
            "src/p/Builder.java": (
                "package p;\n"
                "class Builder {\n"
                "  Builder add(int x) { return this; }\n"
                "  Built build() { return null; }\n"
                "  static Builder start() { return new Builder(); }\n"
                "  <T> T any() { return null; }\n"
                "  int count() { return 0; }\n"
                "}\n"
                "class Built { void use() {} }\n"
            ),
            "src/p/Main.java": (
                "package p;\n"
                "class Main {\n"
                "  Builder mine() { return null; }\n"
                "  void m(Builder b) {\n"
                "    new Builder().add(1).build().use();\n"  # 5
                "    Builder.start()\n"
                "        .add(2)\n"
                "        .build();\n"  # 6-8: across lines
                "    mine().build();\n"  # 9: a bare call
                "    (b.add(3)).build();\n"  # 10: parenthesized
                "    b.any().use();\n"  # 11: a type parameter
                "    b.count().use();\n"  # 12: a primitive
                "    b.build().missing();\n"  # 13: not declared
                "  }\n"
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
        chained = JAVA_CHAINED_RECEIVER
        self.assertEqual(found[(5, "new Builder().add")], ("Builder.add", chained))
        self.assertEqual(
            found[(5, "new Builder().add().build")], ("Builder.build", chained)
        )
        self.assertEqual(
            found[(5, "new Builder().add().build().use")], ("Built.use", chained)
        )
        self.assertEqual(found[(6, "Builder.start().add")], ("Builder.add", chained))
        self.assertEqual(
            found[(6, "Builder.start().add().build")], ("Builder.build", chained)
        )
        self.assertEqual(found[(9, "mine().build")], ("Builder.build", chained))
        self.assertEqual(found[(10, "b.add().build")], ("Builder.build", chained))
        self.assertEqual(found[(11, "b.any().use")], (None, "receiver_type_unknown"))
        self.assertEqual(found[(12, "b.count().use")], (None, "receiver_type_unknown"))
        self.assertEqual(
            found[(13, "b.build().missing")], (None, "not_declared_in_type")
        )

    def test_calls_on_static_fields_of_a_type(self):
        files = {
            "src/p/Kinds.java": (
                "package p;\n"
                "class Kinds {\n"
                "  static final Kinds DEFAULT = new Kinds();\n"
                "  static Box BOX;\n"
                "  Box held;\n"
                "  void go() {}\n"
                "  enum Mode { FAST; void run() {} }\n"
                "}\n"
                "class Box { void open() {} }\n"
            ),
            "src/p/Main.java": (
                "package p;\n"
                "class Main {\n"
                "  void m() {\n"
                "    Kinds.DEFAULT.go();\n"  # 4
                "    Kinds.BOX.open();\n"  # 5
                "    Kinds.Mode.FAST.run();\n"  # 6: an enum constant
                "    Kinds.held.open();\n"  # 7: not static
                "  }\n"
                "  void n(Main Kinds) { Kinds.BOX.open(); }\n"  # 9: a variable
                "  class Sub extends Base { void s() { Kinds.BOX.open(); } }\n"
                "  void o() { new Object() { void t() { Kinds.BOX.open(); } }; }\n"
                "}\n"
                "class Base {}\n"
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
        rule = JAVA_STATIC_FIELD_RECEIVER
        self.assertEqual(found[(4, "Kinds.DEFAULT.go")], ("Kinds.go", rule))
        self.assertEqual(found[(5, "Kinds.BOX.open")], ("Box.open", rule))
        self.assertEqual(found[(6, "Kinds.Mode.FAST.run")], ("Kinds.Mode.run", rule))
        unresolved = (None, "attribute_call")
        self.assertEqual(found[(7, "Kinds.held.open")], unresolved)
        # A parameter named like the type obscures it.
        self.assertEqual(found[(9, "Kinds.BOX.open")], unresolved)
        # A class with a superclass, or an anonymous class, may inherit a
        # field of that name.
        self.assertEqual(found[(10, "Kinds.BOX.open")], unresolved)
        self.assertEqual(found[(11, "Kinds.BOX.open")], unresolved)

    def test_calls_on_variables_captured_by_classes_that_inherit(self):
        files = {
            "src/p/Box.java": (
                "package p;\n"
                "class Box { void open() {} }\n"
                "abstract class Task { abstract void run(); }\n"
                "abstract class Holder { Box box; abstract void run(); }\n"
                "abstract class Deep extends Holder {}\n"
                "interface Named { Box box = null; void run(); }\n"
                "enum Mode { A { void f(Box box) {} }; }\n"
            ),
            "src/p/Main.java": (
                "package p;\n"
                "import java.util.ArrayList;\n"
                "class Main {\n"
                "  void m(Box box, Object o) {\n"
                "    new Task() { void run() { box.open(); } };\n"  # 5
                "    new Holder() { void run() { box.open(); } };\n"  # 6
                "    new Deep() { void run() { box.open(); } };\n"  # 7
                "    new Named() { public void run() { box.open(); } };\n"  # 8
                "    new ArrayList<Box>() { void run() { box.open(); } };\n"  # 9
                "    class Local extends Task { void run() { box.open(); } }\n"
                "    if (o instanceof Box box2) {\n"
                "      new Task() { void run() { box2.open(); } };\n"  # 12
                "    }\n"
                "  }\n"
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
        typed = ("Box.open", JAVA_TYPED_RECEIVER)
        hidden = (None, "receiver_may_be_inherited_field")
        self.assertEqual(found[(5, "box.open")], typed)
        self.assertEqual(found[(10, "box.open")], typed)
        # A base declares a field `box`, or its interface a constant.
        self.assertEqual(found[(6, "box.open")], hidden)
        self.assertEqual(found[(7, "box.open")], hidden)
        self.assertEqual(found[(8, "box.open")], hidden)
        # A base outside the repository may declare one.
        self.assertEqual(found[(9, "box.open")], hidden)
        # A pattern variable: its scope follows control flow.
        self.assertEqual(found[(12, "box2.open")], (None, "attribute_call"))
        properties = {d.symbol: d.properties for d in result.definitions}
        self.assertEqual(properties["Holder"], ("box",))
        self.assertEqual(properties["Named"], ("box",))
        self.assertEqual(properties["Mode"], ("A",))
        self.assertIsNone(properties["Task"])

    def test_methods_of_an_enum_constant_body_are_no_members_of_the_enum(self):
        files = {
            "src/p/Policy.java": (
                "package p;\n"
                "interface Strategy { int read(); }\n"
                "enum Policy implements Strategy {\n"
                "  ONE { public int read() { return 1; } },\n"  # 4
                "  TWO { public int read() { return twice(); } int twice() { return 2; } };\n"
                "  int own() { return read(); }\n"  # 6
                "}\n"
                "class Main {\n"
                "  int m(Policy p) { return p.read() + Policy.ONE.read(); }\n"  # 9
                "}\n"
            ),
        }
        result = analyze(files)
        definitions = {d.id: d for d in result.definitions}
        sites = {s.id: s for s in result.call_sites}
        found = {
            (sites[r.call_id].line, sites[r.call_id].callee): (
                (definitions[r.target_id].symbol, definitions[r.target_id].line)
                if r.target_id
                else r.reason
            )
            for r in resolve_calls(result)
        }
        flagged = sorted(d.line for d in result.definitions if d.constant_body)
        self.assertEqual(flagged, [4, 5, 5])
        # javac binds the method the enum type has: here the interface's.
        self.assertEqual(found[(9, "p.read")], ("Strategy.read", 2))
        self.assertEqual(found[(9, "Policy.ONE.read")], ("Strategy.read", 2))
        self.assertEqual(found[(6, "read")], ("Strategy.read", 2))
        # Inside a constant's body, its own methods come first.
        self.assertEqual(found[(5, "twice")], ("Policy.twice", 5))

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
        ):
            with self.subTest(line=line, callee=callee):
                self.assertEqual(found[(line, callee)], (None, "attribute_call"))
        # A class whose repository bases declare no field of that name, and
        # an enum constant's body whose enum declares none.
        self.assertEqual(found[(34, "field.go")], typed)
        self.assertEqual(found[(50, "field.go")], typed)
        # An anonymous class and types with superinterfaces outside the
        # repository may each inherit a field of that name.
        for line, callee in ((32, "x.go"), (48, "field.go"), (49, "field.go")):
            with self.subTest(line=line, callee=callee):
                self.assertEqual(
                    found[(line, callee)], (None, "receiver_may_be_inherited_field")
                )

    def test_a_comment_between_type_parameters_is_not_a_type_variable(self):
        # TypeScript methods share Java's formal_parameters node type, and
        # both grammars keep comments as named children of type_parameters.
        files = {
            "src/main/java/demo/Box.java": (
                "package demo;\n"
                "class Box<\n  // the element\n  T> {\n"
                "  <\n  /* a key */\n  K> void put(K k, T t) {}\n"
                "}\n"
            ),
            "web/model.ts": (
                "export class Model<\n"
                "  // oxlint-disable-next-line no-explicit-any\n"
                "  T = any,\n"
                "> {\n"
                "  find(id: string): T | undefined { return undefined; }\n"
                "}\n"
            ),
        }
        result = analyze(files, languages=("java", "typescript"))
        methods = {d.symbol: d for d in result.definitions if d.kind == "Method"}
        self.assertEqual(methods["Box.put"].parameter_types, ("?", "*"))
        self.assertIn("Model.find", methods)

    def test_overloads_are_chosen_by_the_argument_types_written(self):
        lines = [
            "package p;",
            "import java.io.Reader;",
            "import java.lang.reflect.Type;",
            "import java.util.AbstractList;",
            "class Base {}",
            "class Foo extends Base {}",
            "class Other {}",
            "abstract class Items extends AbstractList<String> {}",
            "enum Color { RED }",
            "class Api {",
            "  void s(String x) {}",  # 11
            "  void s(Object x) {}",  # 12
            "  void c(Class<?> x) {}",  # 13
            "  void c(Type x) {}",  # 14
            "  void t(Type x) {}",  # 15
            "  void t(Reader x) {}",  # 16
            "  void n(int x) {}",  # 17
            "  void n(long x) {}",  # 18
            "  void w(long x) {}",  # 19
            "  void w(Integer x) {}",  # 20
            "  void b(Integer x) {}",  # 21
            "  void b(Object x) {}",  # 22
            "  void l(int x) {}",  # 23
            "  void l(Object x) {}",  # 24
            "  void z(String x) {}",  # 25
            "  void z(Integer x) {}",  # 26
            "  void k(Base x) {}",  # 27
            "  void k(Other x) {}",  # 28
            "  void j(java.util.Collection<?> x) {}",  # 29
            "  void j(String x) {}",  # 30
            "  void v(String a, String b) {}",  # 31
            "  void v(String... a) {}",  # 32
            "  <T> void g(T x) {}",  # 33
            "  void g(String x) {}",  # 34
            "  private void h(String x) {}",  # 35
            "  void h(Object x) {}",  # 36
            "  void e(Color x) {}",  # 37
            "  void e(String x) {}",  # 38
            "  void d(double x) {}",  # 39
            "  void d(Object x) {}",  # 40
            '  void inside() { h("x"); }',  # 41
            "  void p(int x) {}",  # 42
            "  void p(String x) {}",  # 43
            "  void k2(Plain x) {}",  # 44
            "  void k2(String x) {}",  # 45
            "  void q(String x) {}",  # 46
            "  void q(String x, Object... rest) {}",  # 47
            "  void s3(String x, String y) {}",  # 48
            "  void s3(Object x, Object y) {}",  # 49
            "  void xg(String... x) {}",  # 50
            "  <T> void xg(T... x) {}",  # 51
            "  void g2(int x, String y) {}",  # 52
            "  void g2(Integer x, Object y) {}",  # 53
            "  void fl(float x) {}",  # 54
            "  void fl(double x) {}",  # 55
            "  void arr(Object x) {}",  # 56
            "  void arr(String[] x) {}",  # 57
            "  void dup(Dup x) {}",  # 58
            "  void dup(String x) {}",  # 59
            "}",
            "class Plain extends Object {}",
            "class T extends Base {}",
            "class Main {",
            "  void m(Api a, Foo foo, Items items, Object o, Color red, String str) {",
            '    a.s("x");',  # 65: String over Object
            "    a.c(Foo.class);",  # 66: Class over Type
            "    a.t(Foo.class);",  # 67: a Class is a Type, not a Reader
            "    a.n(1);",  # 68: int, without widening
            "    a.w(1);",  # 69: widening before boxing
            "    a.b(1);",  # 70: boxing, then the most specific
            "    a.l(1L);",  # 71: long is no int: boxed to Long, an Object
            "    a.s(null);",  # 72: null: the most specific
            "    a.z(null);",  # 73: null: neither is more specific
            "    a.k(new Foo());",  # 74: a repository subtype
            "    a.k((Base) o);",  # 75: a cast
            "    a.k(foo);",  # 76: a declared variable
            "    a.j(items);",  # 77: supertypes outside the repository
            '    a.v("x", "y");',  # 78: fixed arity first
            '    a.v("x", "y", "z");',  # 79: variable arity
            '    a.g("x");',  # 80: a type parameter's bound is not known
            '    a.h("x");',  # 81: a private method is not accessible
            "    a.s(o.toString());",  # 82: an argument type not written
            "    a.e(red);",  # 83: an enum
            "    a.d(1);",  # 84: int widens to double
            "    a.s(str);",  # 85
            "    a.p(null);",  # 86: null is no int
            "    a.k2(new Plain());",  # 87: `extends Object` is all it has
            '    a.q("x");',  # 88: fixed arity, one parameter
            '    a.s3("x", o.hashCode());',  # 89: the second type is not written
            '    a.xg("x", "y");',  # 90: variable arity, a type parameter
            "    a.g2(1, o.toString());",  # 91: phase 1 or phase 2
            "    a.fl(1.5f);",  # 92
            "    a.n((long) 1);",  # 93: a primitive cast
            '    a.s(("x"));',  # 94: parentheses
            "    a.s((x.y.Integer) o);",  # 95: not java.lang.Integer
            "    a.arr(null);",  # 96: an array is an Object
            "    a.dup(new Dup());",  # 97: two classes named Dup
            "  }",
            "  <T> void gen(Api a, Object o) {",
            "    a.k((T) o);",  # 100: the type parameter, not class T
            "  }",
            "}",
        ]
        result = analyze(
            {
                "src/p/Api.java": "\n".join(lines) + "\n",
                "src/p/Dup.java": "package p;\nclass Dup {}\n",
                "src/p/Dup2.java": "package p;\nclass Dup {}\n",
            }
        )
        definitions = {d.id: d for d in result.definitions}
        sites = {s.id: s for s in result.call_sites}
        found = {
            (sites[r.call_id].line, sites[r.call_id].callee): (
                definitions[r.target_id].line
                if r.target_id
                else sorted(definitions[c].line for c in r.candidates)
            )
            for r in resolve_calls(result)
        }
        for line, callee, target in (
            (65, "a.s", 11),
            (66, "a.c", 13),
            (67, "a.t", 15),
            (68, "a.n", 17),
            (69, "a.w", 19),
            (70, "a.b", 21),
            (71, "a.l", 24),
            (72, "a.s", 11),
            (73, "a.z", [25, 26]),
            (74, "a.k", 27),
            (75, "a.k", 27),
            (76, "a.k", 27),
            (77, "a.j", 29),  # AbstractList is a Collection (the JDK)
            (78, "a.v", 31),
            (79, "a.v", 32),
            (80, "a.g", [33, 34]),
            (81, "a.h", 36),
            (41, "h", 35),
            (82, "a.s", [11, 12]),
            (83, "a.e", 37),  # an enum is no String
            (84, "a.d", 39),
            (85, "a.s", 11),
            (86, "a.p", 43),
            (87, "a.k2", 44),
            (88, "a.q", 46),
            (89, "a.s3", [48, 49]),
            (90, "a.xg", [50, 51]),
            (91, "a.g2", [52, 53]),
            (92, "a.fl", 54),
            (93, "a.n", 18),
            (94, "a.s", 11),
            (95, "a.s", [11, 12]),
            (100, "a.k", [27, 28]),
            (96, "a.arr", 57),
            (97, "a.dup", [58, 59]),
        ):
            with self.subTest(line=line, callee=callee):
                self.assertEqual(found[(line, callee)], target)
        types = {(s.line, s.callee): s.argument_types for s in sites.values()}
        self.assertEqual(types[(75, "a.k")], ("Base",))
        self.assertEqual(types[(71, "a.l")], ("long",))
        self.assertIsNone(types[(82, "a.s")])
        self.assertEqual(AnalysisResult.from_json(result.to_json()), result)
        site = sites[next(iter(sites))]
        for bad in ((None,), ("",), ["int"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                replace(site, arguments=len(bad), argument_types=bad)
        with self.assertRaises(ValueError):
            replace(site, arguments=2, argument_types=("int",))

    def test_primitive_variables_and_concatenations_select_overloads(self):
        lines = [
            "package p;",
            "class Api {",
            "  void add(Boolean x) {}",  # 3
            "  void add(Character x) {}",  # 4
            "  void add(Number x) {}",  # 5
            "  void add(String x) {}",  # 6
            "  void n(int x) {}",  # 7
            "  void n(long x) {}",  # 8
            "}",
            "class Main {",
            "  int count;",  # 11
            "  void m(Api a, boolean flag, Object o) {",
            "    int i = 1;",
            "    char c = 'a';",
            "    long l = 2L;",
            "    int[] xs = {1};",
            "    var v = 3;",
            '    String s = "s";',
            "    a.add(flag);",  # 19: a parameter
            "    a.add(i);",  # 20: a local, boxed to Integer, a Number
            "    a.add(c);",  # 21
            "    a.n(l);",  # 22: long, not int
            "    a.n(count);",  # 23: a field
            '    a.add("n=" + i);',  # 24: concatenation is a String
            "    a.add(s + o);",  # 25: so with a String variable
            "    a.add(i + 1);",  # 26: arithmetic on ints is an int
            "    a.add(xs);",  # 27: an array has no type here
            "    a.add(v);",  # 28: var without new
            "    for (int k = 0; k < 2; k++) { a.n(k); }",  # 29
            "    for (long e : new long[] {1}) { a.n(e); }",  # 30
            "    a.add(s == null);",  # 31: a comparison is a boolean
            "    i.hashCode();",  # 32: a primitive is no receiver type
            "  }",
            "}",
        ]
        result = analyze({"src/p/Api.java": "\n".join(lines) + "\n"})
        definitions = {d.id: d for d in result.definitions}
        sites = {s.id: s for s in result.call_sites}
        found = {
            (sites[r.call_id].line, sites[r.call_id].callee): (
                definitions[r.target_id].line
                if r.target_id
                else sorted(definitions[c].line for c in r.candidates)
            )
            for r in resolve_calls(result)
        }
        for line, callee, target in (
            (19, "a.add", 3),
            (20, "a.add", 5),
            (21, "a.add", 4),
            (22, "a.n", 8),
            (23, "a.n", 7),
            (24, "a.add", 6),
            (25, "a.add", 6),
            (26, "a.add", 5),
            (27, "a.add", [3, 4, 5, 6]),
            (28, "a.add", [3, 4, 5, 6]),
            (29, "a.n", 7),
            (30, "a.n", 8),
            (31, "a.add", 3),
        ):
            with self.subTest(line=line, callee=callee):
                self.assertEqual(found[(line, callee)], target)
        types = {(s.line, s.callee): s.argument_types for s in sites.values()}
        self.assertEqual(types[(20, "a.add")], ("int",))
        self.assertEqual(types[(24, "a.add")], ("String",))
        self.assertEqual(types[(26, "a.add")], ("int",))
        receiver = next(s for s in sites.values() if s.line == 32)
        self.assertIsNone(receiver.receiver_type)

    def test_jdk_types_are_no_repository_subtypes(self):
        lines = [
            "package p;",
            "import java.util.Map;",
            "import java.lang.reflect.Type;",
            "import p.sub.Missing;",
            "import q.Elsewhere;",
            "import p.sub.Holder.Inner;",
            "abstract class Elem {}",  # 7
            "interface Shape {}",  # 8
            "class Api {",
            "  void j(Object x) {}",  # 10
            "  void j(Elem x) {}",  # 11
            "  void s(Object x) {}",  # 12
            "  void s(Shape x) {}",  # 13
            "  void t(Type x) {}",  # 14
            "  void t(Map<?, ?> x) {}",  # 15
            "}",
            "class Main {",
            "  void m(Api a, Map<String, String> map, Missing missing,",
            "      Elsewhere other, Unknown unknown, Inner inner) {",
            "    a.j(map);",  # 20: Map is no Elem
            "    a.s(map);",  # 21: nor a Shape
            "    a.j(missing);",  # 22: p.sub may be in the repository
            "    a.j(other);",  # 23: q may be built with it
            "    a.j(unknown);",  # 24: not imported
            "    a.t(map);",  # 25: two types outside: not known
            "    a.j(inner);",  # 26: a member type in p.sub
            "  }",
            "}",
        ]
        result = analyze(
            {
                "src/p/Api.java": "\n".join(lines) + "\n",
                "src/p/sub/Other.java": "package p.sub;\nclass Other {}\n",
            }
        )
        definitions = {d.id: d for d in result.definitions}
        sites = {s.id: s for s in result.call_sites}
        found = {
            (sites[r.call_id].line, sites[r.call_id].callee): (
                definitions[r.target_id].line
                if r.target_id
                else sorted(definitions[c].line for c in r.candidates)
            )
            for r in resolve_calls(result)
        }
        for line, callee, target in (
            (20, "a.j", 10),
            (21, "a.s", 12),
            (22, "a.j", [10, 11]),
            (23, "a.j", [10, 11]),
            (24, "a.j", [10, 11]),
            (25, "a.t", 15),  # a Map is no Type (the JDK)
            (26, "a.j", [10, 11]),
        ):
            with self.subTest(line=line, callee=callee):
                self.assertEqual(found[(line, callee)], target)
        rules = {
            sites[r.call_id].line: r.rule_id
            for r in resolve_calls(result)
            if r.status == "linked" and sites[r.call_id].line in {20, 21}
        }
        self.assertEqual(rules, dict.fromkeys((20, 21), JAVA_PLATFORM_ARGUMENT))

    def test_jdk_supertypes_tell_overloads_apart(self):
        lines = [
            "package p;",  # 1
            "import java.io.Closeable;",
            "import java.io.StringWriter;",
            "import java.util.*;",
            "class Out implements Closeable {",  # 5
            "  public void close() {}",
            "}",
            "class Api {",
            "  void w(Out x) {}",  # 9
            "  void w(Appendable x) {}",  # 10
            "  void c(Collection<?> x) {}",  # 11
            "  void c(Map<?, ?> x) {}",  # 12
            "}",
            "class Main {",
            "  void m(Api a, Out out, StringWriter sw, ArrayList<String> list) {",
            "    a.w(out);",  # 16: a Closeable is no Appendable
            "    a.w(sw);",  # 17: a JDK type is no Out
            "    a.c(list);",  # 18: java.util.* gives ArrayList
            "  }",
            "}",
        ]
        other = [
            "package q;",  # 1
            "import java.util.*;",
            "import r.*;",
            "class Api {",
            "  void c(Collection<?> x) {}",  # 5
            "  void c(Map<?, ?> x) {}",  # 6
            "}",
            "class Main {",
            "  void m(Api a, ArrayList<String> list) {",
            "    a.c(list);",  # 10: r may have an ArrayList
            "  }",
            "}",
        ]
        result = analyze(
            {
                "src/p/Api.java": "\n".join(lines) + "\n",
                "src/q/Api.java": "\n".join(other) + "\n",
            }
        )
        definitions = {d.id: d for d in result.definitions}
        sites = {s.id: s for s in result.call_sites}
        found = {
            (sites[r.call_id].file, sites[r.call_id].line): (
                (definitions[r.target_id].line, r.rule_id)
                if r.target_id
                else sorted(definitions[c].line for c in r.candidates)
            )
            for r in resolve_calls(result)
            if sites[r.call_id].callee.startswith("a.")
        }
        self.assertEqual(
            found,
            {
                ("src/p/Api.java", 16): (9, JAVA_JDK_SUPERTYPES),
                ("src/p/Api.java", 17): (10, JAVA_PLATFORM_ARGUMENT),
                ("src/p/Api.java", 18): (11, JAVA_JDK_SUPERTYPES),
                ("src/q/Api.java", 10): [5, 6],
            },
        )

    def test_method_call_arguments_have_their_methods_return_types(self):
        lines = [
            "package p;",
            "class Elem {}",
            "class Prim extends Elem {}",
            "class Api {",
            "  void j(String x) {}",  # 5
            "  void j(Elem x) {}",  # 6
            "  void n(int x) {}",  # 7
            "  void n(Object x) {}",  # 8
            "}",
            "class Box<T> {",
            "  Prim prim() { return null; }",  # 11
            "  int size() { return 0; }",  # 12
            "  T item() { return null; }",  # 13
            "  void none() {}",  # 14
            "  Prim[] all() { return null; }",  # 15
            "  static String name() { return null; }",  # 16
            "  Inner inner() { return null; }",  # 17
            "  static class Inner extends Elem {}",
            "}",
            "class Main {",
            "  void m(Api a, Box<String> box, Object o) {",
            "    a.j(box.prim());",  # 22: Prim is an Elem
            "    a.n(box.size());",  # 23: int
            "    a.j(box.item());",  # 24: a type parameter
            "    a.j(Box.name());",  # 25: a static method
            "    a.j((box.prim()));",  # 26: parentheses
            "    a.j(o.toString());",  # 27: not linked
            "    a.n(box.prim());",  # 28: boxing not needed
            "    a.j(box.inner());",  # 29: a name written in Box
            "  }",
            "}",
        ]
        result = analyze({"src/p/Api.java": "\n".join(lines) + "\n"})
        definitions = {d.id: d for d in result.definitions}
        sites = {s.id: s for s in result.call_sites}
        found = {
            (sites[r.call_id].line, sites[r.call_id].callee): (
                definitions[r.target_id].line
                if r.target_id
                else sorted(definitions[c].line for c in r.candidates)
            )
            for r in resolve_calls(result)
        }
        for line, callee, target in (
            (22, "a.j", 6),
            (23, "a.n", 7),
            (24, "a.j", [5, 6]),
            (25, "a.j", 5),
            (26, "a.j", 6),
            (27, "a.j", [5, 6]),
            (28, "a.n", 8),
            (29, "a.j", 6),
        ):
            with self.subTest(line=line, callee=callee):
                self.assertEqual(found[(line, callee)], target)
        results = {
            d.symbol: d.result_type
            for d in result.definitions
            if d.symbol.startswith("Box.")
        }
        self.assertEqual(
            results,
            {
                "Box.prim": "Prim",
                "Box.size": "int",
                "Box.item": None,
                "Box.none": None,
                "Box.all": None,
                "Box.name": "String",
                "Box.inner": "Inner",
                "Box.Inner": None,
            },
        )
        calls = {(s.line, s.callee): s.argument_calls for s in sites.values()}
        self.assertEqual(calls[(22, "a.j")], ((22, 8, 22, 18),))
        self.assertEqual(calls[(26, "a.j")], ((26, 9, 26, 19),))
        self.assertIsNone(calls[(22, "box.prim")])
        self.assertEqual(AnalysisResult.from_json(result.to_json()), result)
        site = sites[next(iter(sites))]
        for bad in ((None,), ((1, 2, 3),), [(1, 2, 3, 4)], ((1, 2, 3, -1),)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                replace(site, arguments=len(bad), argument_calls=bad)
        with self.assertRaises(ValueError):
            replace(site, arguments=2, argument_calls=((1, 0, 1, 2),))

    def test_new_links_to_the_constructor_the_arguments_select(self):
        lines = [
            "package p;",
            "class Box {",
            "  Box() {}",  # 3
            "  Box(int x) {}",  # 4
            "  Box(String s) {}",  # 5
            "  Box(Object o, int n) {}",  # 6
            "  Box(String o, int n) {}",  # 7
            "}",
            "class Plain {}",  # 9
            "class Sub extends Box {}",  # 10
            "class One { One(int x) {} }",  # 11
            "class Main {",
            "  static class In { In(int x) {} }",  # 13
            "  void m(Object o, String s) {",
            "    new Box();",  # 15
            "    new Box(1);",  # 16
            '    new Box("s");',  # 16
            "    new Box(o, 1);",  # 18: Object, so not the String overload
            "    new Box(s, 1);",  # 19: the most specific
            "    new Box(null);",  # 20: String only takes null
            "    new Box(o);",  # 21: no constructor applies, kept on Box
            "    new Plain();",  # 22
            "    new Sub();",  # 23
            "    new One(1, 2);",  # 24: no arity fits
            "    new Main.In(1);",  # 24: a member type's constructor
            "  }",
            "}",
        ]
        result = analyze({"src/p/Box.java": "\n".join(lines) + "\n"})
        definitions = {d.id: d for d in result.definitions}
        sites = {s.id: s for s in result.call_sites}
        found = {
            sites[r.call_id].line: (
                definitions[r.target_id].line,
                definitions[r.target_id].kind,
                r.rule_id,
            )
            for r in resolve_calls(result)
            if r.target_id
        }
        for line, target, kind in (
            (15, 3, "Constructor"),
            (16, 4, "Constructor"),
            (17, 5, "Constructor"),
            (18, 6, "Constructor"),
            (19, 7, "Constructor"),
            (20, 5, "Constructor"),
            (21, 2, "Class"),
            (22, 9, "Class"),
            (23, 10, "Class"),
            (24, 11, "Class"),
            (25, 13, "Constructor"),
        ):
            with self.subTest(line=line):
                self.assertEqual(found[line], (target, kind, JAVA_TYPE_NAME))

    def test_operator_expressions_have_their_result_types(self):
        lines = [
            "package p;",
            "class Api {",
            "  void n(int x) {}",  # 3
            "  void n(long x) {}",  # 4
            "  void n(double x) {}",  # 5
            "  void n(boolean x) {}",  # 6
            "  void n(String x) {}",  # 7
            "  void n(float x) {}",  # 8
            "  void n(short x) {}",  # 9
            "}",
            "class Main {",
            "  void m(Api a, byte b, long l, float f, char c, boolean z, String s, Object o) {",
            "    a.n(-1);",  # 13: int
            "    a.n(-b);",  # 14: byte promoted to int
            "    a.n(b + c);",  # 15: int
            "    a.n(b * l);",  # 16: long
            "    a.n(f / 2);",  # 17: float
            "    a.n(f * 2.0);",  # 18: double
            "    a.n(l << 1);",  # 19: the left operand's type
            "    a.n(b >> l);",  # 20: int
            "    a.n(b < l);",  # 21: boolean
            "    a.n(z && z);",  # 22: boolean
            "    a.n(!z);",  # 23
            "    a.n(z & z);",  # 24: boolean
            "    a.n(b & 1);",  # 25: int
            "    a.n(o instanceof String);",  # 26
            "    a.n(z ? s : s);",  # 27: String
            "    a.n(z ? 1 : 2);",  # 28: int
            "    a.n(z ? s : null);",  # 29: not typed
            "    a.n(z ? null : null);",  # 30: not typed
            "    a.n(z ? 1 : l);",  # 31: not typed
            "    a.n(~l);",  # 32: long
            "    a.n(~f);",  # 33: not integral
            "    a.n(f & 1);",  # 34: not integral
            "    a.n(o + 1);",  # 35: not typed
            "    a.n(-o);",  # 36: not typed
            "    a.n(z | o);",  # 37: not typed
            "  }",
            "}",
        ]
        result = analyze({"src/p/Api.java": "\n".join(lines) + "\n"})
        definitions = {d.id: d for d in result.definitions}
        sites = {s.id: s for s in result.call_sites}
        found = {
            sites[r.call_id].line: (
                definitions[r.target_id].line
                if r.target_id
                else sorted(definitions[c].line for c in r.candidates)
            )
            for r in resolve_calls(result)
        }
        every = list(range(3, 10))
        self.assertEqual(
            found,
            {
                13: 3,
                14: 3,
                15: 3,
                16: 4,
                17: 8,
                18: 5,
                19: 4,
                20: 3,
                21: 6,
                22: 6,
                23: 6,
                24: 6,
                25: 3,
                26: 6,
                27: 7,
                28: 3,
                29: every,
                30: every,
                31: every,
                32: 4,
                33: every,
                34: every,
                35: every,
                36: every,
                37: every,
            },
        )

    def test_static_field_arguments_have_their_declared_types(self):
        lines = [
            "package p; import a.*; import b.*;",
            "class Elem {}",
            "class Prim extends Elem {",
            "  static final Prim ONE = new Prim();",
            "  static final int COUNT = 1;",
            "  final Prim self = null;",  # not static
            "  static Prim[] ALL = null;",
            "  static Prim LIST[] = null;",
            "  static Inner IN = null;",
            "  static class Inner extends Elem {}",
            "}",
            "interface Consts { Elem NONE = null; }",  # implicitly static
            "enum Kind { A, B; static final int LABEL = 1; }",
            "class Api {",
            "  void j(String x) {}",  # 15
            "  void j(Elem x) {}",  # 16
            "  void n(int x) {}",  # 17
            "  void n(Object x) {}",  # 18
            "  void k(Kind x) {}",  # 19
            "  void k(int x) {}",  # 20
            "  void p(String x, int y) {}",  # 21
            "  void p(Elem x, int y) {}",  # 22
            "}",
            "class Main {",
            "  void m(Api a, Object o) {",
            "    a.j(Prim.ONE);",  # 26
            "    a.n(Prim.COUNT);",  # 27: int
            "    a.j(Prim.self);",  # 28: no static field
            "    a.j(Prim.ALL);",  # 29: an array
            "    a.j(Prim.LIST);",  # 30: an array
            "    a.j(Consts.NONE);",  # 31: an interface constant
            "    a.k(Kind.A);",  # 32: an enum constant
            "    a.k(Kind.LABEL);",  # 33: a static field of an enum
            "    a.j((Prim.ONE));",  # 34: parentheses
            "    a.j(Gone.ONE);",  # 35: no repository type
            "    a.j(Prim.IN);",  # 36: a name written in Prim
            "    a.j(Dup.ONE);",  # 37: two types named Dup
            "    a.p(Prim.ONE, 1);",  # 38: with a typed argument
            "  }",
            "  void v(Api a, Kind Prim) {",
            "    a.j(Prim.ONE);",  # 41: the parameter, not the type
            "  }",
            "}",
        ]
        dup = "package {0};\nimport p.Prim;\npublic class Dup {{ static Prim ONE; }}\n"
        result = analyze(
            {
                "src/p/Api.java": "\n".join(lines) + "\n",
                "src/a/Dup.java": dup.format("a"),
                "src/b/Dup.java": dup.format("b"),
            }
        )
        definitions = {d.id: d for d in result.definitions}
        sites = {s.id: s for s in result.call_sites}
        resolutions = resolve_calls(result)
        found = {
            sites[r.call_id].line: (
                definitions[r.target_id].line
                if r.target_id
                else sorted(definitions[c].line for c in r.candidates)
            )
            for r in resolutions
            if sites[r.call_id].callee.startswith("a.")
        }
        either = [15, 16]
        self.assertEqual(
            found,
            {
                26: 16,
                27: 17,
                28: either,
                29: either,
                30: either,
                31: 16,
                32: 19,
                33: 20,
                34: 16,
                35: either,
                36: 16,
                37: either,
                38: 22,
                41: either,
            },
        )
        rules = {
            sites[r.call_id].line: r.rule_id
            for r in resolutions
            if r.status == "linked" and sites[r.call_id].callee.startswith("a.")
        }
        self.assertEqual(
            rules, dict.fromkeys((26, 27, 31, 32, 33, 34, 36, 38), JAVA_FIELD_ARGUMENT)
        )
        fields = {
            d.symbol: d.fields
            for d in result.definitions
            if d.kind in {"Class", "Interface", "Enum"} and d.file == "src/p/Api.java"
        }
        self.assertEqual(
            fields["Prim"], (("ONE", "Prim"), ("COUNT", "int"), ("IN", "Inner"))
        )
        self.assertEqual(fields["Consts"], (("NONE", "Elem"),))
        self.assertEqual(
            fields["Kind"], (("A", "Kind"), ("B", "Kind"), ("LABEL", "int"))
        )
        self.assertIsNone(fields["Api"])
        site = next(s for s in sites.values() if s.line == 41)
        self.assertIsNone(site.argument_fields)
        site = next(s for s in sites.values() if s.line == 34)
        self.assertEqual(site.argument_fields, (("Prim", "ONE"),))

    def test_assignability_of_written_types(self):
        result = analyze(
            {
                "p/A.java": "package p;\n"
                "class Base {}\n"
                "class Sub extends Base {}\n"
                "class A {}\n"
            }
        )
        scope = _JavaScope(result, name_bindings(result).by_symbol)
        at = ("p/A.java", ("A",))
        for near, far, phase, expected in (
            # A type parameter (`?` of a method, `*` of a class) has no known
            # bounds, even against an array or a primitive.
            ("?", "int", 1, None),
            ("int[]", "*", 1, None),
            # Arrays of one dimension count: primitive elements must be the
            # same type, class elements subtypes.
            ("int[]", "int[]", 1, True),
            ("int[]", "long[]", 1, False),
            ("Sub[]", "Base[]", 1, True),
            # Any array is an Object; one of more dimensions than a
            # parameter's array may still be one, but never a primitive.
            ("Sub[][]", "Object", 1, True),
            ("Sub[][]", "Object[]", 1, None),
            ("Sub[]", "Base", 1, None),
            ("Sub[]", "int", 1, False),
            # Unboxing, then widening, in phase 2 only; an unknown class or
            # one with unread bases may unbox to nothing known.
            ("Integer", "long", 2, True),
            ("Integer", "int", 2, True),
            ("Integer", "short", 2, False),
            ("Integer", "int", 1, False),
            ("Missing", "int", 2, None),
        ):
            with self.subTest(near=near, far=far, phase=phase):
                self.assertIs(scope.subtype(near, at, far, at, phase), expected)

    def test_override_needs_the_same_known_parameter_types(self):
        def method(types):
            return SimpleNamespace(
                parameters=1,
                variadic=False,
                definition=SimpleNamespace(parameter_types=types),
            )

        self.assertTrue(_overridden(method(("int",)), [(1, False, ("int",))]))
        self.assertFalse(_overridden(method(("int",)), [(1, False, ("long",))]))
        # Unknown parameter types match only unknown ones.
        self.assertTrue(_overridden(method(None), [(1, False, None)]))
        self.assertFalse(_overridden(method(None), [(1, False, ("int",))]))
        self.assertFalse(_overridden(method(("int",)), [(1, True, ("int",))]))

    def test_overloads_ruled_out_or_left_open_by_what_types_are_known(self):
        lines = [
            "package p;",
            "class Foo {}",
            "record R() {}",
            "class Api {",
            "  void cl(Cloneable x) {}",  # 5
            "  void cl(int[] x) {}",  # 6
            "  void one(Foo x) {}",  # 7
            "  void one(int[] x) {}",  # 8
            "  void rec(Record x) {}",  # 9
            "  void rec(Object x) {}",  # 10
            "}",
            "class Main {",
            "  void m(Api a) {",
            "    a.cl(null);",  # 14: an array is Cloneable
            "    a.one(new Ext());",  # 15: an unknown class is no array
            "    a.rec(new R());",  # 16: a record extends java.lang.Record
            "  }",
            "}",
        ]
        result = analyze({"src/p/Api.java": "\n".join(lines) + "\n"})
        definitions = {d.id: d for d in result.definitions}
        sites = {s.id: s for s in result.call_sites}
        found = {
            sites[r.call_id].line: (
                definitions[r.target_id].line
                if r.target_id
                else sorted(definitions[c].line for c in r.candidates)
            )
            for r in resolve_calls(result)
            if sites[r.call_id].callee.startswith("a.")
        }
        self.assertEqual(found, {14: 6, 15: 7, 16: 9})

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

        # A type parameter is not the same type as written: both stay, and
        # `1L` selects fit(long), which applies without boxing.
        self.assertEqual(found["r.fit"], near("LongRange.fit"))
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
