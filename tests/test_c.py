"""C declarations and calls (opt-in language)."""

import dataclasses
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought import analyze_repository
from deepthought.calls import C_CALL_EXTERNAL, C_CALL_STATIC, resolve_calls
from deepthought.imports import C_INCLUDE_RELATIVE, resolve_imports
from deepthought.models import AnalysisResult
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


@unittest.skipUnless(READY, "C grammar is not installed")
class CCallTests(unittest.TestCase):
    def test_calls_bind_as_the_linker_binds_them(self):
        result = analyze(
            {
                "lib.c": (
                    "static int helper(int);\n"
                    "int helper(int x) { return x; }\n"
                    "int api(void) { return helper(1) + shared(); }\n"
                    "static int only_here(void) { return 0; }\n"
                    "int twice(void) { return 0; }\n"  # 5
                ),
                "main.c": (
                    "int helper(int x) { return x + 1; }\n"
                    "int shared(void) { return 2; }\n"
                    "int (*hook)(void) = 0;\n"
                    "#define MAC(x) (x)\n"
                    "int main(int argc, char **argv) {\n"  # 5
                    "    int (*local)(void) = 0;\n"
                    '    api(); helper(2); only_here(); printf("");\n'
                    "    hook(); local(); MAC(1); twice();\n"
                    "    s.f(); (api)(); argc;\n"
                    "#ifdef X\n"  # 10
                    "    api();\n"
                    "#endif\n"
                    "    return 0;\n"
                    "}\n"
                    "int run(int (*cb)(void)) { return cb() + local() + cpp(); }\n"  # 15
                ),
                "other.c": "int twice(void) { return 1; }\n",
                "other.cpp": "int cpp() { return 1; }\n",
            },
            ("c", "cpp"),
        )
        names = {d.id: (d.file, d.symbol) for d in result.definitions}
        sites = {c.id: c for c in result.call_sites}
        found = {
            (sites[r.call_id].file, sites[r.call_id].line, sites[r.call_id].column): (
                sites[r.call_id].callee,
                names.get(r.target_id),
                r.rule_id or r.reason,
            )
            for r in resolve_calls(result)
        }
        self.maxDiff = None
        self.assertEqual(
            found,
            {
                # Internal linkage: a later definition without `static` too.
                ("lib.c", 3, 23): ("helper", ("lib.c", "helper"), C_CALL_STATIC),
                ("lib.c", 3, 35): ("shared", ("main.c", "shared"), C_CALL_EXTERNAL),
                # The one external definition; a static one elsewhere is not.
                ("main.c", 7, 4): ("api", ("lib.c", "api"), C_CALL_EXTERNAL),
                ("main.c", 7, 11): ("helper", ("main.c", "helper"), C_CALL_EXTERNAL),
                ("main.c", 7, 22): ("only_here", None, "not_defined"),
                ("main.c", 7, 35): ("printf", None, "not_defined"),
                # Variables and macros of that name; two definitions.
                ("main.c", 8, 4): ("hook", None, "local_binding"),
                ("main.c", 8, 12): ("local", None, "local_binding"),
                ("main.c", 8, 21): ("MAC", None, "macro"),
                ("main.c", 8, 29): ("twice", None, "multiple_declarations"),
                # Every preprocessor branch.
                ("main.c", 11, 4): ("api", ("lib.c", "api"), C_CALL_EXTERNAL),
                # A parameter; another function's local; a C++ function.
                ("main.c", 15, 34): ("cb", None, "local_binding"),
                ("main.c", 15, 41): ("local", None, "not_defined"),
                ("main.c", 15, 51): ("cpp", None, "not_defined"),
            },
        )
        callers = {
            (c.file, c.line, c.column): names[c.caller_id] for c in result.call_sites
        }
        self.assertEqual(callers[("main.c", 7, 4)], ("main.c", "main"))
        flags = {
            (d.file, d.symbol, d.line): (d.static, d.prototype)
            for d in result.definitions
            if d.kind == "Function"
        }
        self.assertEqual(flags[("lib.c", "helper", 1)], (True, True))
        self.assertEqual(flags[("lib.c", "helper", 2)], (None, None))
        self.assertEqual(flags[("lib.c", "only_here", 4)], (True, None))
        self.assertEqual(AnalysisResult.from_json(result.to_json()), result)
        self.assertNotIn('"prototype": null', result.to_json())
        with self.assertRaises(ValueError):
            dataclasses.replace(result.definitions[0], prototype=False)

    def test_includes_bring_static_functions_into_the_unit(self):
        result = analyze(
            {
                "lib/core.c": (
                    "static int hidden(void) { return 0; }\n"
                    "int pub(void) { return hidden(); }\n"
                ),
                "tests/common.h": (
                    '#include "../lib/core.c"\n'
                    "#include <stdio.h>\n"
                    '#include "missing.h"\n'
                    "#ifdef X\n"
                    '#include "../lib/./core.c"\n'
                    "#endif\n"
                    '#include "common.h"\n'
                ),
                "tests/t.c": (
                    '#include "common.h"\nint main(void) { return hidden() + pub(); }\n'
                ),
                "other.c": "int main2(void) { return hidden(); }\n",
            }
        )
        resolved = {r.import_id: r for r in resolve_imports(result)}
        self.assertEqual(
            [
                (
                    i.file,
                    i.line,
                    i.specifier,
                    i.kind,
                    [link.target_file for link in resolved[i.id].links],
                    resolved[i.id].reason,
                )
                for i in sorted(result.imports, key=lambda i: (i.file, i.line))
            ],
            [
                ("tests/common.h", 1, "../lib/core.c", "include", ["lib/core.c"], None),
                ("tests/common.h", 3, "missing.h", "include", [], "not_in_repository"),
                (
                    "tests/common.h",
                    5,
                    "../lib/./core.c",
                    "include",
                    ["lib/core.c"],
                    None,
                ),
                # A cycle of includes.
                ("tests/common.h", 7, "common.h", "include", ["tests/common.h"], None),
                ("tests/t.c", 1, "common.h", "include", ["tests/common.h"], None),
            ],
        )
        self.assertEqual(
            {
                resolved[i.id].links[0].rule_id
                for i in result.imports
                if resolved[i.id].links
            },
            {C_INCLUDE_RELATIVE},
        )
        names = {d.id: (d.file, d.symbol) for d in result.definitions}
        sites = {c.id: c for c in result.call_sites}
        found = {
            (sites[r.call_id].file, sites[r.call_id].column): (
                names.get(r.target_id),
                r.rule_id or r.reason,
            )
            for r in resolve_calls(result)
        }
        self.assertEqual(
            found,
            {
                ("lib/core.c", 23): (("lib/core.c", "hidden"), C_CALL_STATIC),
                # The unit `tests/t.c` starts includes `lib/core.c`, through
                # `tests/common.h`.
                ("tests/t.c", 24): (("lib/core.c", "hidden"), C_CALL_STATIC),
                ("tests/t.c", 35): (("lib/core.c", "pub"), C_CALL_EXTERNAL),
                ("other.c", 25): (None, "not_defined"),
            },
        )
