"""C declarations (opt-in language)."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought import analyze_repository
from deepthought.syntax import declaration_sources, make_parser

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_c"
)
SOURCE = """static int *f(int a) { return 0; }
int g(void), *g2(void), v;
int (*h(int))(void) { return 0; }
struct S { int a; struct T { int b; } t; union { int c; } u; };
struct S2;
typedef struct { int x; } P, *PP;
typedef int (*cb)(int);
union U { int a; };
enum E { A, B };
enum { C };
#define M(x) x
#define N 1
#ifdef X
void k(void) {}
#else
void k(int) {}
#endif
extern int v2;
void (*fp)(void);
int (paren)(int);
API int (prefixed) (int a);
API void spaced (int a);
API T recovered (int a) { return 0; }
void body(void) {
    struct Local { int a; };
    typedef int local_t;
    int nested(int);
#define IN_BODY 1
}
struct Holder {
#define IN_STRUCT 2
    int a;
};
int returns_function(T x)(int y);
#ifdef __cplusplus
namespace ns {
#endif
int after_namespace(void);
"""


def analyze(files, languages=("c",)):
    with tempfile.TemporaryDirectory() as directory:
        for name, content in files.items():
            path = Path(directory) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        options = {} if languages is None else {"languages": languages}
        return analyze_repository(directory, repository_id="fixture", **options)


@unittest.skipUnless(READY, "C grammar is not installed")
class CTests(unittest.TestCase):
    def test_declarations_at_file_scope(self):
        result = analyze(
            {
                "a.c": SOURCE,
                "a.h": 'int from_header(void);\nextern "C" { int in_extern(void); }\n',
            }
        )
        # The grammar does not know the `API` macro.
        self.assertEqual(result.status, "partial")
        self.maxDiff = None
        self.assertEqual(
            [(d.file, d.line, d.symbol, d.kind) for d in result.definitions],
            [
                ("a.c", 1, "f", "Function"),
                # Prototypes; a variable is not a declaration.
                ("a.c", 2, "g", "Function"),
                ("a.c", 2, "g2", "Function"),
                # A function returning a pointer to a function.
                ("a.c", 3, "h", "Function"),
                # Nested structs are file scope in C; unnamed ones and
                # forward declarations are not declarations.
                ("a.c", 4, "S", "Struct"),
                ("a.c", 4, "T", "Struct"),
                ("a.c", 6, "P", "TypeAlias"),
                ("a.c", 6, "PP", "TypeAlias"),
                ("a.c", 7, "cb", "TypeAlias"),
                ("a.c", 8, "U", "Union"),
                ("a.c", 9, "E", "Enum"),
                ("a.c", 11, "M", "Macro"),
                ("a.c", 12, "N", "Macro"),
                # Every preprocessor branch.
                ("a.c", 14, "k", "Function"),
                ("a.c", 16, "k", "Function"),
                # A pointer to a function is a variable; parentheses group.
                ("a.c", 20, "paren", "Function"),
                # After a macro the grammar does not know.
                ("a.c", 21, "prefixed", "Function"),
                ("a.c", 22, "spaced", "Function"),
                ("a.c", 23, "recovered", "Function"),
                # Items in a function body are local; a macro is not.
                ("a.c", 24, "body", "Function"),
                ("a.c", 28, "IN_BODY", "Macro"),
                ("a.c", 30, "Holder", "Struct"),
                ("a.c", 31, "IN_STRUCT", "Macro"),
                # Not `API int (name)(...)`: its parameter has a name.
                ("a.c", 34, "returns_function", "Function"),
                # A C++ namespace is not a function.
                ("a.c", 38, "after_namespace", "Function"),
                ("a.h", 1, "from_header", "Function"),
                ("a.h", 2, "in_extern", "Function"),
            ],
        )
        self.assertEqual(len({d.id for d in result.definitions if d.symbol == "k"}), 2)
        self.assertEqual(
            (result.imports, result.call_sites, result.supertypes), ((), (), ())
        )

    def test_c_is_opt_in(self):
        default = analyze({"a.c": SOURCE, "a.py": "x = 1\n"}, languages=None)
        self.assertEqual([c.file for c in default.coverage], ["a.py"])

    def test_declarations_in_unparsed_text_are_left_out(self):
        # A comment in a macro's continued line is not parsed by the grammar.
        result = analyze(
            {
                "a.c": "#define check(L,p)  \\\n"
                "  f(L, \\\n"
                "    t = s(L, p),  /* save */ \\\n"
                "    p = r(L, t))\n"
                "int after(void);\n"
            }
        )
        self.assertEqual(result.status, "partial")
        self.assertEqual([d.symbol for d in result.definitions], ["after"])

    def test_function_declarators_left_in_unparsed_text_are_functions(self):
        # Braces opened in both branches of an `#if` leave the grammar
        # unable to parse the file: a later definition's type, declarator
        # and body are separate items of the error node.
        result = analyze(
            {
                "a.c": "char *a(void) {\n#if A\n if (x) {\n#else\n if (y) {\n"
                "#endif\n  return 1;\n }\n}\n"
                "static double\nulp\n (struct c* C, U *x)\n{\n return 0;\n}\n",
                # A pointer to a function is not one.
                "b.c": "int (*p)(void) {\n#if A\n if (x) {\n#else\n if (y) {\n"
                "#endif\n  return 1;\n }\n}\n",
            }
        )
        self.assertEqual(
            [(d.file, d.symbol, d.line) for d in result.definitions],
            [("a.c", "a", 1), ("a.c", "ulp", 10)],
        )

    def test_cplusplus_namespaces_hold_file_scope_items(self):
        # Without the C++ branch, `namespace n {` reads as a function
        # definition with no parameters.
        result = analyze(
            {
                "a.h": "#ifdef __cplusplus\nnamespace n {\nnamespace m {\n#endif\n"
                "int inside(void);\nstruct In { int a; };\n"
                "#ifdef __cplusplus\n} }\n#endif\n"
            }
        )
        self.assertEqual(
            [(d.symbol, d.kind) for d in result.definitions],
            [("inside", "Function"), ("In", "Struct")],
        )

    def test_declaration_sources_name_the_declared_name(self):
        content = b"API int (prefixed) (int a);\nstruct S { int a; };\n"
        sources = declaration_sources(
            make_parser("c", ".c").parse(content),
            content,
            "fixture",
            "a.c",
            language="c",
        )
        self.assertEqual(
            sorted((s["source_code"], s["name_range_utf8"]) for s in sources.values()),
            [
                ("API int (prefixed) (int a);", [1, 9, 1, 17]),
                ("struct S { int a; }", [2, 7, 2, 8]),
            ],
        )


if __name__ == "__main__":
    unittest.main()
