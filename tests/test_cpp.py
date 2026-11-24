"""C++ declarations (opt-in language)."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought import analyze_repository
from deepthought.syntax import declaration_sources, make_parser, prepare_source

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_cpp"
)
SOURCE = """#define M 1
namespace a::b { namespace { int hidden(); } }
namespace n {
template <typename T> class C : public B {
 public:
  C();
  ~ C();
  void f() {}
  const T& get() const;
  operator const char*() const;
  bool operator==(const C&) const;
  friend void swap(C&, C&) {}
  struct In { int x; };
  using V = T;
  void* operator new(unsigned long);
};
template <> struct hash<int> { int operator()(int) const; };
typedef int W;
enum class E { X };
void free_fn(int);
void (*pointer)(int);
}
extern "C" { int in_c(void); }
template <typename T> n::C<T>::C() {}
void n::free_fn(int) {}
void Other::outside() {}
union U { int a; };
struct S { struct { int y; } unnamed; };
void body() {
#define IN_BODY 1
}
"""


def analyze(files, languages=("cpp",)):
    with tempfile.TemporaryDirectory() as directory:
        for name, content in files.items():
            path = Path(directory) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        options = {} if languages is None else {"languages": languages}
        return analyze_repository(directory, repository_id="fixture", **options)


@unittest.skipUnless(READY, "C++ grammar is not installed")
class CppTests(unittest.TestCase):
    def test_declarations_in_namespaces_and_classes(self):
        result = analyze({"a.cc": SOURCE})
        self.assertEqual(result.status, "succeeded")
        self.maxDiff = None
        self.assertEqual(
            [(d.line, d.symbol, d.kind) for d in result.definitions],
            [
                (1, "M", "Macro"),
                # `a::b` opens two namespaces; an anonymous one adds no scope.
                (2, "a", "Namespace"),
                (2, "a.b", "Namespace"),
                (2, "a.b.hidden", "Function"),
                (3, "n", "Namespace"),
                (4, "n.C", "Class"),
                (6, "n.C.C", "Method"),
                (7, "n.C.~C", "Method"),
                (8, "n.C.f", "Method"),
                # A reference return type holds its declarator in no field.
                (9, "n.C.get", "Method"),
                (10, "n.C.operator const char*", "Method"),
                (11, "n.C.operator==", "Method"),
                # A friend is declared in the namespace around its class.
                (12, "n.swap", "Function"),
                (13, "n.C.In", "Struct"),
                (14, "n.C.V", "TypeAlias"),
                (15, "n.C.operator new", "Method"),
                (17, "n.hash", "Struct"),
                (17, "n.hash.operator()", "Method"),
                (18, "n.W", "TypeAlias"),
                (19, "n.E", "Enum"),
                (20, "n.free_fn", "Function"),
                # Out of line: a written scope that is a namespace this
                # file opens makes a function, any other a method.
                (23, "in_c", "Function"),
                (24, "n.C.C", "Method"),
                (25, "n.free_fn", "Function"),
                (26, "Other.outside", "Method"),
                (27, "U", "Union"),
                (28, "S", "Struct"),
                (29, "body", "Function"),
                # A macro defined in a body holds for the rest of the file.
                (30, "IN_BODY", "Macro"),
            ],
        )

    def test_names_after_macros_the_grammar_does_not_know(self):
        result = analyze(
            {
                "a.h": "class API Plain { public: void f(); };\n"
                "class API Derived : public Base { int x; };\n"
                "class API Other : public Base { public: void f(); };\n"
                "class Locked {\n"
                "  void f() REQUIRES(mu_);\n"
                "  int *g() REQUIRES(mu_);\n"
                "  const T& h() REQUIRES(mu_);\n"
                "  int (*fp)() REQUIRES(mu_);\n"
                "  int *x GUARDED_BY(mu_);\n"
                "};\n"
                "API Status Get(int a);\n"
                "#define BAD(x) \\\n  f(x, /* c */ \\\n  y)\n"
                "API int (prefixed) (int a);\n"
            }
        )
        self.assertEqual(
            [(d.line, d.symbol, d.kind) for d in result.definitions],
            [
                (1, "Plain", "Class"),
                (1, "Plain.f", "Method"),
                (2, "Derived", "Class"),
                # Here the name is in an error node before the first base.
                (3, "Other", "Class"),
                (3, "Other.f", "Method"),
                (4, "Locked", "Class"),
                # The annotation is not the declared name.
                (5, "Locked.f", "Method"),
                (6, "Locked.g", "Method"),
                (7, "Locked.h", "Method"),
                # Pointers to functions and fields are not declarations.
                # The type after the macro is in an error node.
                (11, "Get", "Function"),
                (15, "prefixed", "Function"),
                # A macro the grammar could not read is left out.
            ],
        )

    def test_class_macros_are_blanked_before_parsing(self):
        cases = {
            b"class API A {": b"class     A {",
            b"struct X_EXPORT B\n  : public C {": b"struct          B\n  : public C {",
            b"union U_API U {": b"union       U {",
            # Not a macro: the class's name, `final`, an enum's base.
            b"class D final {": b"class D final {",
            b"class API E final : F {": b"class     E final : F {",
            b"class G {": b"class G {",
            b"enum class H : int {": b"enum class H : int {",
            b"class I;": b"class I;",
            # Comments and literals are kept.
            b"// class API J {\n": b"// class API J {\n",
            b"/* class API K {\n */": b"/* class API K {\n */",
            b'"class API L {"': b'"class API L {"',
            b'\'"\' class API M { ""': b'\'"\' class     M { ""',
            # A line holding only an upper-case name with an underscore.
            b"FMT_BEGIN_EXPORT\nclass N {": b"                \nclass N {",
            b"x;\n  API_CALL  \r\nint f();": b"x;\n            \r\nint f();",
            b"LAST_LINE": b"         ",
            # Not one: a name without an underscore, lower case, a macro's
            # continued line, a line with more on it.
            b"\nNOUNDERSCORE\n": b"\nNOUNDERSCORE\n",
            b"\nlower_case\n": b"\nlower_case\n",
            b"#define X \\\n  FMT_A \\\n": b"#define X \\\n  FMT_A \\\n",
            b"\nFMT_A int f();\n": b"\nFMT_A int f();\n",
            b"/*\nFMT_A\n*/": b"/*\nFMT_A\n*/",
        }
        for written, parsed in cases.items():
            self.assertEqual(prepare_source("cpp", written), parsed, written)
        self.assertEqual(prepare_source("c", b"struct API S {"), b"struct API S {")

    def test_text_the_grammar_misreads_declares_no_function(self):
        result = analyze(
            {
                "a.h": "class S {\n"
                "  enum : unsigned { mask = 1 };\n"
                "  MACRO S(int n) noexcept\n"
                "      : size_(n), cap_(n) {}\n"
                "  int bits : 3, more : 2;\n"
                "};\n"
                "struct T { int a; } make();\n"
                "FMT_BEGIN_EXPORT\n"
                "template <typename C> class View { int size() const; };\n"
            }
        )
        self.assertEqual(
            [(d.line, d.symbol, d.kind) for d in result.definitions],
            [
                (1, "S", "Class"),
                # An unnamed enum: the grammar reads `enum :` with a missing
                # name.
                # Member initializers after a macro read as bit-field
                # declarators; the constructor is kept.
                (3, "S.S", "Method"),
                # A function cannot return a class defined in its
                # declaration.
                (7, "T", "Struct"),
                # A macro alone on its line is blanked.
                (9, "View", "Class"),
                (9, "View.size", "Method"),
            ],
        )

    def test_headers_are_cpp_when_cpp_is_selected(self):
        files = {"a.h": "int f(void);\n", "b.c": "int g(void);\n"}
        both = analyze(files, languages=("c", "cpp"))
        self.assertEqual(
            [(c.file, c.language) for c in both.coverage],
            [("a.h", "cpp"), ("b.c", "c")],
        )
        alone = analyze(files, languages=("c",))
        self.assertEqual(
            [(c.file, c.language) for c in alone.coverage],
            [("a.h", "c"), ("b.c", "c")],
        )

    def test_cpp_is_opt_in(self):
        default = analyze({"a.cc": SOURCE, "a.py": "x = 1\n"}, languages=None)
        self.assertEqual([c.file for c in default.coverage], ["a.py"])

    def test_declaration_sources_start_at_the_template(self):
        content = b"template <typename T>\nT id(T x) { return x; }\n"
        sources = declaration_sources(
            make_parser("cpp", ".cc").parse(content),
            content,
            "fixture",
            "a.cc",
            language="cpp",
        )
        self.assertEqual(
            [(s["source_start_line"], s["name_range_utf8"]) for s in sources.values()],
            [(1, [2, 2, 2, 4])],
        )


if __name__ == "__main__":
    unittest.main()
