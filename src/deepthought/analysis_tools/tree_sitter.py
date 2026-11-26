"""TreeSitterTool backed by the existing standalone declaration extractor.

This bounded port supports Python, TypeScript/TSX and (opt-in) JavaScript/JSX,
Go and Java declarations, static file imports, name-matched calls, and
declared bases.
It does not port the legacy confidence-scored CALLS output, CFG/data-flow
output or claim additional languages.
"""

from hashlib import sha256
import importlib.util
from pathlib import Path
import time
from copy import deepcopy

from .._version import __version__
from .. import calls as call_rules
from .. import imports as import_rules
from .. import go_interfaces
from .. import inheritance as base_rules
from ..engine import RUST_ITEM_MACROS, analyze_repository
from ..generated_files import generated_files
from .. import scip_calls
from ..signature_types import signature_types
from ..scip_calls import ScipReferences, bind_calls
from .. import scip_bases
from ..edge_coverage import edge_coverage
from ..fastapi_dependencies import dependency_uses
from ..pytest_fixtures import FixtureUse
from ..python_ast_facts import ast_facts
from ..python_runtime import read_ast_facts
from .. import scip_imports
from ..syntax import (
    DEFAULT_LANGUAGES,
    LANGUAGES,
    RustExpander,
    declaration_sources,
    make_parser,
    parse_source,
)
from .base import AnalysisTool, ToolCapability, ToolMetadata
from .contracts import (
    NodeData,
    RelationshipData,
    StandardizedOutput,
    ToolExecutionMetadata,
)
from .evidence import EvidenceRule, register_rule
from .registry import register_tool

CONTAINS_RULE = register_rule(
    EvidenceRule(
        "tree-sitter.repository-contains-file",
        "resolved",
        "The file was selected and analyzed under the repository root.",
    )
)
DEFINES_RULE = register_rule(
    EvidenceRule(
        "tree-sitter.file-defines-declaration",
        "resolved",
        "The parse tree of the file contains this named declaration.",
    )
)
NESTS_RULE = register_rule(
    EvidenceRule(
        "tree-sitter.declaration-contains-declaration",
        "resolved",
        "The parse tree places the inner declaration within the outer one; the "
        "outer is the innermost declaration whose span encloses it.",
    )
)

PACKAGE_RULES = {
    rule.rule_id: rule
    for rule in (
        register_rule(
            EvidenceRule(
                scip_imports.SCIP_PYTHON_IMPORT_PACKAGE,
                "resolved",
                "scip-python (pyright) resolves the module an absolute import "
                "names to an installed distribution or the standard library, "
                "which the index names with its version; the package's own "
                "files are not analyzed.",
            )
        ),
        register_rule(
            EvidenceRule(
                scip_imports.SCIP_GO_IMPORT_PACKAGE,
                "resolved",
                "scip-go (the go command's package loading) resolves the import "
                "path to a package of another module, or of the standard "
                "library, which the index names with the module's version; its "
                "files are not analyzed.",
            )
        ),
        register_rule(
            EvidenceRule(
                scip_imports.SCIP_SCRIPT_IMPORT_PACKAGE,
                "resolved",
                "scip-typescript (the TypeScript compiler's module resolution) "
                "resolves the specifier to a file of an npm package, which the "
                "index names with the version its package.json declares; the "
                "package's files are not analyzed.",
            )
        ),
    )
}
FIXTURE_PARAMETER_RULE = register_rule(
    EvidenceRule(
        "pytest.fixture-parameter",
        "pattern_matched",
        "A test or fixture parameter names a pytest fixture, found as pytest "
        "looks it up: the class, the module, then conftest.py in each "
        "directory up to the root (deepthought.pytest_fixtures).",
    )
)
FIXTURE_MARK_RULE = register_rule(
    EvidenceRule(
        "pytest.fixture-usefixtures",
        "pattern_matched",
        "@pytest.mark.usefixtures on a test or test class names a pytest "
        "fixture, found as pytest looks it up.",
    )
)
FIXTURE_AUTOUSE_RULE = register_rule(
    EvidenceRule(
        "pytest.fixture-autouse",
        "pattern_matched",
        "A pytest fixture declared autouse=True in the test's class, module or "
        "a conftest.py above it, which pytest runs for every test that sees it.",
    )
)
_FIXTURE_RULES = {
    "parameter": FIXTURE_PARAMETER_RULE,
    "usefixtures": FIXTURE_MARK_RULE,
    "autouse": FIXTURE_AUTOUSE_RULE,
}
DEPENDENCY_RULE = register_rule(
    EvidenceRule(
        "fastapi.depends",
        "resolved",
        "The SCIP index resolves a call to FastAPI's own Depends or Security, "
        "and the dependency written in it to one parsed declaration "
        "(deepthought.fastapi_dependencies); FastAPI calls it for the "
        "endpoint, router or dependency the call belongs to.",
    )
)

UNLINKED_CALL_RULE = register_rule(
    EvidenceRule(
        "tree-sitter.call-site-unlinked",
        "resolved",
        "The parse tree places this call inside the declaration (or at the top "
        "level of the file); no call rule links it to exactly one declaration, "
        "for the reason the call site records.",
    )
)

SIGNATURE_TYPE_RULE = register_rule(
    EvidenceRule(
        "scip.signature-type-reference",
        "resolved",
        "A SCIP index records a reference inside the declaration's signature "
        "(the parse tree's span of the declaration up to its body: receiver, "
        "type parameters, parameters with their annotations and defaults, "
        "result type) to a symbol it defines at a type declaration of the "
        "repository (class, interface, struct, type alias, enum).",
    )
)

IMPORT_RULES = {
    rule.rule_id: rule
    for rule in (
        register_rule(
            EvidenceRule(
                import_rules.PYTHON_RELATIVE_MODULE,
                "resolved",
                "A relative import names a package or module file analyzed at "
                "that path; a package directory takes precedence over a module.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.PYTHON_ABSOLUTE_MODULE,
                "name_matched",
                "The dotted module name equals the path of an analyzed file under "
                "the repository root or src/; the runtime import path is not checked.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.PYTHON_FROM_IMPORT_SUBMODULE,
                "name_matched",
                "A name imported from a package equals an analyzed submodule; the "
                "package itself may define that name instead.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.TYPESCRIPT_RELATIVE_MODULE,
                "resolved",
                "A relative specifier resolves to an analyzed file in TypeScript's "
                "extension and index lookup order, then to JavaScript sources as "
                "with allowJs; package.json files are not read.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.JAVASCRIPT_RELATIVE_FILE,
                "resolved",
                "A relative specifier with a JavaScript extension names an "
                "analyzed file at exactly that path.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.JAVASCRIPT_EXTENSIONLESS_MODULE,
                "pattern_matched",
                "An extensionless relative specifier matches an analyzed file by "
                "the bundler/CommonJS lookup (.js, .jsx, .mjs, .cjs, then index); "
                "Node ES modules would not resolve it, and bundler config is not read.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.TYPESCRIPT_PATH_ALIAS,
                "resolved",
                "A bare specifier matches a compilerOptions.paths pattern of the "
                "nearest tsconfig.json or jsconfig.json (exact, else the longest "
                "wildcard prefix) and a target resolves to an analyzed file in "
                "TypeScript's lookup order, a directory through its package.json "
                "typings, types and main; include, exclude and extended configs "
                "are not read.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.JAVASCRIPT_PATH_ALIAS,
                "pattern_matched",
                "A JavaScript file's bare specifier resolves through the nearest "
                "jsconfig.json or tsconfig.json paths as TypeScript resolves it; "
                "the runtime resolves it only through a matching bundler alias, "
                "which is not read.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.SCRIPT_DECLARATION_SOURCE,
                "pattern_matched",
                "A path alias reaches a directory whose package.json typings or "
                "types names a declaration file that is not analyzed; the link "
                "is to the JavaScript file of the same name beside it, which the "
                "declaration file describes by convention.",
            )
        ),
        register_rule(
            EvidenceRule(
                scip_imports.SCIP_PYTHON_IMPORT_MODULE,
                "resolved",
                "scip-python (pyright) resolves the module an absolute import "
                "names from the interpreter's search path (the project's source "
                "roots, installed dependencies, the standard library) to a "
                "document of the index, which is the analyzed file.",
            )
        ),
        register_rule(
            EvidenceRule(
                scip_imports.SCIP_GO_IMPORT_MODULE,
                "resolved",
                "scip-go (the go command's package loading, through go.mod and "
                "replace directives) resolves the import path to a package whose "
                "files the index defines; the link reaches each of its non-test "
                "files.",
            )
        ),
        register_rule(
            EvidenceRule(
                scip_imports.SCIP_SCRIPT_IMPORT_MODULE,
                "resolved",
                "scip-typescript (the TypeScript compiler's module resolution, "
                "through tsconfig, package.json and workspaces) resolves the "
                "specifier to a document of the index, which is the analyzed "
                "file.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.GO_MODULE_PACKAGE,
                "resolved",
                "A Go import path inside the importing file's own module (the "
                "nearest go.mod) names the directory at that path below the "
                "module root; the link reaches every non-test file of its "
                "package. vendor/, replace directives and go.work are not read.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.JAVA_PACKAGE_MEMBER,
                "resolved",
                "A Java import names a package or type that analyzed files "
                "declare under exactly one source root (the directory above "
                "the package path); a type links to its declaring file, a "
                "package wildcard to every file of the package.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.RUST_MODULE_FILE,
                "resolved",
                "A Rust 'mod name;' includes the file Cargo's module layout "
                "gives: name.rs or name/mod.rs beside a crate root (a file a "
                "Cargo.toml target names, lib.rs, "
                "main.rs, build.rs, or an undeclared file in src/bin or in "
                "tests, benches or examples outside src) or mod.rs, and in "
                "the directory named after any other declaring file; a "
                "#[path] attribute names the file from the declaring file's "
                "directory. Exactly one such file exists.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.RUST_CRATE_ROOT,
                "resolved",
                "A Cargo.toml package's library ([lib] path, default "
                "src/lib.rs) or a [[bin]], [[test]], [[example]] or [[bench]] "
                "path names its crate root file, read from the manifest's "
                "directory, and that file exists.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.RUST_USE_DECLARED,
                "resolved",
                "A Rust use path, read from the declaring module (crate, self, "
                "super, a local module or type, or a workspace library by "
                "crate name), goes through the crate's module tree of mod "
                "declarations to a module whose file declares the next name, "
                "or names that module itself.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.RUST_USE_MODULE,
                "resolved",
                "A Rust use path goes through the crate's module tree to a "
                "module whose file neither declares the next name nor "
                "re-exports a declaration of it (a constant, static or "
                "generated item); the link is to that module's file.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.RUST_USE_REEXPORT,
                "resolved",
                "A Rust use path reaches a module that does not declare the "
                "next name but binds it with its own use (or, when none does, "
                "its glob uses supply it); following those uses reaches the "
                "file that declares it. Each #[cfg] meaning is linked.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.RUST_USE_EXPORTED_MACRO,
                "resolved",
                "A Rust use path names a macro at a crate's root, and a "
                "#[macro_export] macro_rules! of that name is defined in a file "
                "of that crate, which places it there.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.PHP_USE_DECLARED,
                "resolved",
                "A PHP 'use' or 'use function' names, ignoring ASCII case, a "
                "class, interface, trait, enum or function that exactly one "
                "analyzed file declares outside any function body. Composer "
                "autoload configuration is not read.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.C_INCLUDE_RELATIVE,
                "resolved",
                "A C '#include \"path\"' names the analyzed file at that path "
                "relative to the directory of the including file, which the "
                "compiler searches first. Include paths given to the compiler "
                "are not read.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.PHP_USE_NAMESPACE,
                "resolved",
                "A PHP 'use' names no declared class-like type but a namespace "
                "that analyzed files declare types or functions in; the link "
                "reaches every such file.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.RUST_EXTERN_CRATE,
                "resolved",
                "A Rust 'extern crate name;' names the one workspace library "
                "with that crate name.",
            )
        ),
        register_rule(
            EvidenceRule(
                import_rules.JAVA_SAME_SOURCE_ROOT,
                "name_matched",
                "Several source roots declare the imported package or type; "
                "the importing file's own source root is chosen. Which root "
                "a build puts on the class path is not read.",
            )
        ),
    )
}


def _declaration_sources(root, result):
    """Declaration source text from the analyzed bytes, and files that changed.

    Each analyzed file is read again; it contributes only if its digest equals
    the one the analysis recorded, so stored text always matches the facts.
    """
    sources, changed, parsers = {}, [], {}
    item_macros = [
        name
        for name in dict(result.analyzer_versions).get(RUST_ITEM_MACROS, "").split(",")
        if name
    ]
    for item in result.coverage:
        suffix = Path(item.file).suffix
        if (
            item.status not in {"analyzed", "partial"}
            or item.digest is None
            or suffix not in LANGUAGES.get(item.language, ((),))[0]
        ):
            continue
        try:
            content = (Path(root) / item.file).read_bytes()
        except OSError:
            content = None
        if content is None or sha256(content).hexdigest() != item.digest:
            changed.append(item.file)
            continue
        key = (item.language, suffix)
        if key not in parsers:
            parsers[key] = make_parser(item.language, suffix)
        sources.update(
            declaration_sources(
                parse_source(parsers[key], item.language, suffix, content),
                content,
                result.repository_id,
                item.file,
                expander=RustExpander(content, item_macros)
                if item.language == "rust"
                else None,
                language=item.language,
            )
        )
    return sources, sorted(changed)


_CALL_RULE_TEXT = {
    call_rules.CALL_LEXICAL_NAME: "A bare callee name equals a function or class "
    "declared in an enclosing lexical scope of the same file; in JavaScript "
    "and TypeScript, the declaration that the call's block and function "
    "scopes give.",
    call_rules.CALL_IMPORTED_NAME: "A bare callee name is bound by an import "
    "whose module file declares that name at top level.",
    call_rules.CALL_MODULE_ATTRIBUTE: "A callee 'module.name' uses a module "
    "import binding whose file declares that name at top level; a Go import "
    "without an alias binds the imported package's declared name.",
    call_rules.CALL_PYTHON_REEXPORT: "A Python callee bound by an import, bare "
    "or as 'module.name', names a module that does not declare the name but "
    "binds it with exactly one 'from M import name'; that import is followed, "
    "step by step, to the module that declares it at top level.",
    call_rules.CALL_JS_REEXPORT: "A JavaScript or TypeScript callee bound by "
    "an import, bare or as 'module.name', names a module that does not "
    "declare the name but re-exports it with 'export { name } from' (aliases "
    "applied) or, failing that, 'export * from'; each step is followed to the "
    "module that declares it. Default exports are not followed, and a name "
    "several star exports give is ambiguous.",
    call_rules.CALL_JS_STATIC_METHOD: "A JavaScript or TypeScript callee "
    "'C.m' whose C is the class declared where the call's scopes see C, or "
    "the one class a named import binds to C (followed as for an imported "
    "name), and m a method that class declares 'static'. Static methods it "
    "inherits are not followed.",
    call_rules.CALL_JS_TYPED_RECEIVER: "A JavaScript or TypeScript 'x.m()' "
    "where the declaration of x the call sees annotates it with a class or "
    "top-level interface (also 'T | undefined' or 'T | null') or "
    "initializes it with 'new' of a class, the type found as for a static "
    "method's C, links to the method m that class declares or inherits "
    "through 'extends', or that interface or the interfaces it extends "
    "declare. A field of that name in a class on the way, or a base that "
    "names no repository class or interface, stops the lookup.",
    call_rules.CALL_JS_MEMBER_CHAIN: "A JavaScript or TypeScript 'x.a.m()', "
    "'this.a.m()' or 'y.m()' where y is declared from such a chain (or "
    "destructured from it): the class x holds, as for a typed receiver, or "
    "the class around an instance method for 'this', then the class each "
    "field or getter on the way is annotated with or constructs, declared "
    "or inherited through 'extends', links to the instance method m of the "
    "last one.",
    call_rules.CALL_PYTHON_TYPED_RECEIVER: "A Python 'x.m()' where x is a "
    "parameter annotated with a repository class, or a local whose one "
    "binding calls or annotates that class, links to m declared on the "
    "class or inherited through its declared bases.",
    call_rules.PYTHON_CALL_SUPER: "A Python 'super().m()' written directly in a "
    "method of class C links to the m that C's declared bases declare or "
    "inherit, in the method resolution order after C (depth first, left to "
    "right, with no class reached twice); a base that names no repository "
    "class stops the lookup.",
    call_rules.PYTHON_CALL_SELF_INHERITED: "A Python 'self.m()' in a class that "
    "declares no m links to the m its declared bases declare or inherit, in "
    "the method resolution order (depth first, left to right, with no class "
    "reached twice); a base that names no repository class stops the lookup. "
    "A subclass's override is not followed.",
    call_rules.CALL_SELF_METHOD: "A self.name or this.name callee equals a "
    "method declared in the enclosing class body; inheritance is not followed.",
    call_rules.CALL_PRIVATE_NAME: "A JavaScript or TypeScript 'obj.#m()' "
    "names a private method that the innermost enclosing class declares, "
    "whatever 'obj' is; private names are scoped to the class body.",
    call_rules.CALL_PACKAGE_NAME: "A bare callee name equals a function declared "
    "at top level by another file in the same directory with the same package "
    "clause; build constraints are not evaluated.",
    call_rules.GO_CALL_CONVERSION: "A Go call 'T(x)' whose bare callee names no "
    "function of the package but one named type declared at top level by a file "
    "of the same directory and package clause is a conversion to that type; build "
    "constraints are not evaluated.",
    call_rules.JAVA_ENCLOSING_METHOD: "A bare Java method name is declared by "
    "the innermost enclosing type that declares or inherits a method of that "
    "name; argument types are not checked.",
    call_rules.JAVA_STATIC_IMPORT: "A bare Java method name is a method of a "
    "type the file imports statically, by name or with a wildcard.",
    call_rules.JAVA_TYPE_MEMBER: "In a Java call 'T.name', T denotes a type by "
    "Java's lookup order (enclosing and local types, single-type imports, the "
    "package, wildcard imports, then a qualified name) that declares the "
    "method; a variable that obscures the type is not checked.",
    call_rules.JAVA_TYPED_RECEIVER: "In a Java call 'x.name' or 'this.x.name', "
    "the declaration of the local variable, parameter or field x in scope "
    "writes a class type T (or 'var x = new T()'), T denotes a type by Java's "
    "lookup order, and T declares or inherits the method; argument types are "
    "not checked.",
    call_rules.JAVA_CHAINED_RECEIVER: "In a Java call 'e.name' whose receiver e "
    "is itself a method call, e's call links to a method whose declared return "
    "type T (written where that method is declared) denotes a type by Java's "
    "lookup order; for 'new T(...).name', T as written. T declares or inherits "
    "the method, chosen among overloads as other Java calls are.",
    call_rules.PYTHON_CALL_ATTRIBUTE_RECEIVER: "In a Python call "
    "'x.a.name' (any number of attributes), x is the first parameter of an "
    "instance method directly in a class body, not rebound (the class around "
    "it), or has a class as for 'x.name' with a typed receiver; each "
    "attribute's class is the one the first class declaring that name in the "
    "method resolution order gives it: its annotation in the class body or "
    "on 'self.a: T = ...', or else, when every assignment its instance "
    "methods make is 'self.a = p' from a parameter 'p: T', 'self.a = T(...)' "
    "or None, and the class assigns it nothing else, that one T; the last "
    "class declares or inherits the method through its declared bases.",
    call_rules.PYTHON_CALL_RESULT_RECEIVER: "In a Python call 'x.name' where "
    "the one binding of the local x in its function is 'x = f(...)' (x is no "
    "parameter and the function imports nothing), the call f(...) links to a "
    "function or method whose return annotation names one repository class "
    "(T, m.T, Optional[T], T | None), looked up where that function is "
    "defined; that class declares or inherits the method through its "
    "declared bases.",
    call_rules.JAVA_STATIC_FIELD_RECEIVER: "In a Java call 'T.F.name' where no "
    "variable or field named by the first name of T can be in scope (none "
    "declared around the call, and no type around it may inherit one), T "
    "denotes a type by Java's lookup order that declares the static field or "
    "enum constant F, whose declaration writes a class type U (an enum "
    "constant: its enum); U declares or inherits the method, chosen among "
    "overloads as other Java calls are.",
    call_rules.JAVA_GETCLASS_ARGUMENT: "A Java call among overloads that only "
    "an argument 'e.getClass()' or 'getClass()' tells apart, with the "
    "supertypes of JDK types as for the JDK-supertypes rule: Object declares "
    "getClass() final (JLS 4.3.2), so that argument is a java.lang.Class, "
    "and exactly one overload is the one javac chooses (JLS 15.12.2).",
    call_rules.JAVA_JDK_SUPERTYPES: "A Java call among overloads that only the "
    "supertypes of JDK types tell apart: for a type that a single-type import, "
    "a canonical java. name, or java.lang and the file's on-demand imports "
    "name in one of the JDK packages java_platform.json lists in full, its "
    "supertypes are those the JDK declares (tools/JavaPlatformTypes.java, JDK "
    "21), a repository enum or record extends java.lang.Enum or "
    "java.lang.Record, and a JDK type is no repository subtype; with those, "
    "exactly one overload is the one javac chooses (JLS 15.12.2).",
    call_rules.JAVA_TYPE_NAME: "A Java 'new T(...)' names a type by Java's "
    "lookup order; the link reaches the type's declaration.",
    call_rules.JAVA_INHERITED_METHOD: "A Java method name, bare, after 'this.' "
    "or after a type 'T.', is not declared by the type found but by one of "
    "its declared bases, followed through the repository; a nearer method "
    "with the same parameter count overrides it, and argument types are not "
    "checked.",
    call_rules.JAVA_PLATFORM_ARGUMENT: "Java overloads that the argument types "
    "written leave open are told apart because an argument's type is a "
    "single-type import from a 'java.' package, which only the JDK defines, "
    "and so is no subtype of a repository type; the method is found by the "
    "lookup the call would otherwise use.",
    call_rules.JAVA_FIELD_ARGUMENT: "Java overloads that the argument types "
    "written leave open are told apart by an argument 'X.f' whose X no "
    "variable or field declared around the call in its file names: X is "
    "taken as the type it names, and f as that type's static field or enum "
    "constant with its declared type. A field named X that the enclosing "
    "class inherits would be the one named instead; the method is found by "
    "the lookup the call would otherwise use.",
}
CALL_RULES = {
    rule_id: register_rule(EvidenceRule(rule_id, "name_matched", text))
    for rule_id, text in _CALL_RULE_TEXT.items()
}
# Rust has no overloading: a call path read like a use path names one
# declaration in each configuration. A Go variable's type is the one its
# declaration writes, and a type's own methods come before promoted ones.
CALL_RULES.update(
    {
        rule_id: register_rule(EvidenceRule(rule_id, "resolved", text))
        for rule_id, text in {
            call_rules.GO_CALL_TYPED_RECEIVER: "A Go call 'x.M()' or "
            "'x.f.M()' where x is declared in the function around the call (a "
            "parameter, receiver, named result, 'var x T', or 'x := T{...}', "
            "'&T{...}', 'new(T)' or 'x := e', the innermost declaration "
            "visible at the call) links to the method M declared on the "
            "receiver's type T, in the package that declares T. The type is "
            "followed from what is written: a variable's declaration, a "
            "struct field's declared type, the first declared result of a "
            "method or of a function 'F()' or 'pkg.F()', the element of a "
            "slice or array (of a named slice type too), a type assertion, a "
            "conversion 'T(e)', the first result of a function literal bound to a "
            "local; a "
            "step that names nothing, or more than one declaration, leaves the "
            "call unresolved. Promoted methods of embedded fields are not "
            "followed.",
            call_rules.RUST_CALL_PATH: "A Rust call's path, read from the "
            "caller's module as a use path is (through modules, uses and globs, "
            "after the uses written in the function bodies around the call), "
            "names a function or tuple struct declared there.",
            call_rules.RUST_CALL_LOCAL_ITEM: "A Rust call's single name is a "
            "function or tuple struct declared in a function body around the "
            "call.",
            call_rules.RUST_CALL_ASSOCIATED: "A Rust call 'Type::f' or "
            "'Self::f' names a type (read as a call's path is, or the type of "
            "the impl around the call) whose impls declare 'f': an inherent "
            "impl's first, else a trait impl's or a provided method of a trait "
            "it implements.",
            call_rules.RUST_CALL_SELF_METHOD: "A Rust 'self.m()' in an impl "
            "names 'm' as 'Self::m' does; in a trait body, the trait's own "
            "method 'm'.",
            call_rules.RUST_CALL_TYPED_RECEIVER: "A Rust 'x.m()' where the "
            "innermost declaration of the local x (a parameter or 'let' that "
            "binds x alone) writes a type 'A', '&A', 'm::A<T>' or 'Self' names "
            "'m' as 'A::m' does, read from the call.",
            call_rules.RUST_CALL_RESULT_RECEIVER: "A Rust 'x.m()' whose "
            "receiver is the result of a call, written in place ('T::new().m()', "
            "'x.a().m()') or bound by an untyped 'let x = T::new();', that "
            "links to a function whose return type writes a type 'A', '&A', "
            "'m::A<T>' or 'Self' names 'm' as 'A::m' does, 'A' read where the "
            "function is declared.",
            call_rules.RUST_CALL_ENUM_VARIANT: "A Rust 'E::V(...)' or "
            "'Self::V(...)' where 'E' (read as a call's path is) or the type of "
            "the impl around the call is an enum that declares the variant "
            "'V' names that variant, ahead of any associated function.",
            call_rules.C_CALL_STATIC: "A C call 'f(...)' in a file whose "
            "translation unit (the file and the files its quoted includes "
            "reach) declares a function 'f' 'static' names the unit's "
            "definition of 'f', unless a parameter, local or file-scope variable, or a macro "
            "anywhere in the repository's C files, is named 'f'.",
            call_rules.C_CALL_EXTERNAL: "A C call 'f(...)' in a file whose "
            "translation unit declares no function 'f' 'static' names the one definition of 'f' "
            "without 'static' in the repository's C files, unless a parameter, "
            "local or file-scope variable, or a macro anywhere in those files, "
            "is named 'f'.",
        }.items()
    }
)

_BASE_RULE_TEXT = {
    base_rules.BASE_LEXICAL_NAME: "A base named in a class or interface "
    "declaration equals a class or interface declared in an enclosing lexical "
    "scope of the same file.",
    base_rules.BASE_IMPORTED_NAME: "A base name is bound by an import whose "
    "module file declares a class or interface of that name at top level.",
    base_rules.BASE_MODULE_ATTRIBUTE: "A base 'module.Name' uses a module import "
    "binding whose file declares a class or interface of that name at top level.",
    base_rules.JAVA_BASE_TYPE: "A Java extends or implements entry names a "
    "type by Java's lookup order (enclosing and local types, single-type "
    "imports, the package, wildcard imports, then a fully qualified name), "
    "evaluated where the declaration appears.",
}
BASE_RULES = {
    rule_id: register_rule(EvidenceRule(rule_id, "name_matched", text))
    for rule_id, text in _BASE_RULE_TEXT.items()
}
BASE_RULES[scip_bases.SCIP_PYTHON_BASE] = register_rule(
    EvidenceRule(
        scip_bases.SCIP_PYTHON_BASE,
        "resolved",
        "scip-python records, at the last name of a base written in a Python "
        "class statement, a reference to the symbol of exactly one class "
        "declared in the analyzed files: the base as Python's import and name "
        "lookup bind it.",
    )
)
BASE_RULES[scip_bases.SCIP_TYPESCRIPT_BASE] = register_rule(
    EvidenceRule(
        scip_bases.SCIP_TYPESCRIPT_BASE,
        "resolved",
        "scip-typescript records, at the last name of an extends or implements "
        "entry of a TypeScript or JavaScript class or interface, a reference "
        "to the symbol of exactly one class or interface declared in the "
        "analyzed files: the base as the TypeScript compiler binds it.",
    )
)
BASE_RULES[base_rules.RUST_SUPERTRAIT] = register_rule(
    EvidenceRule(
        base_rules.RUST_SUPERTRAIT,
        "resolved",
        "A Rust trait's supertrait (a bound of the trait or of 'where Self:') "
        "names a trait, its path read from the trait's module as a use path "
        "is (after the items and uses of the function bodies around it).",
    )
)
GO_INTERFACE_RULE = register_rule(
    EvidenceRule(
        go_interfaces.GO_INTERFACE_METHOD_SET,
        "name_matched",
        "A Go type (or a pointer to it) declared in the analyzed files "
        "declares, for each method of an interface of the same module (its "
        "own, and those of the interfaces it embeds: of the same module, "
        "'error', or exported standard library interfaces), exactly one "
        "method of that name whose parameter and result types are written "
        "alike once each named type is tied to its package; an unexported "
        "method only in the interface's package. The type IMPLEMENTS the "
        "interface, and each interface method DISPATCHES_TO the type's method.",
    )
)
BASE_RULES[base_rules.RUST_IMPL_TRAIT] = register_rule(
    EvidenceRule(
        base_rules.RUST_IMPL_TRAIT,
        "resolved",
        "A Rust 'impl Trait for Type' block: the type's path names a struct, "
        "enum, union or type alias and the trait's path names a trait, each "
        "read from the impl's module as a use path is (after the items and "
        "uses of the function bodies around the impl).",
    )
)


# Call targets read from a compiler-backed SCIP index. Each edge also names
# the indexer that produced it and the digest of the index's receipt.
CALL_RULES.update(
    {
        rule_id: register_rule(EvidenceRule(rule_id, "resolved", text))
        for rule_id, text in {
            scip_calls.SCIP_CALL_OCCURRENCE: "A call site in a file a SCIP "
            "index covers links to the declaration of the symbol the index "
            "records at the start of the callee's last name: the parsed "
            "declaration whose span holds the symbol's SCIP definition and "
            "whose name is the one written there.",
            scip_calls.SCIP_CALL_LOCAL_SYMBOL: "As scip.call-occurrence, for a "
            "symbol the index declares local to the file (a nested function, "
            "a local class or a variable bound to a function).",
            scip_calls.SCIP_CALL_CONSTRUCTOR: "A construction the index "
            "resolves to a class ('new C()'), or to a class and its "
            "constructor together, links to the one constructor the class "
            "declares.",
            scip_calls.SCIP_CALL_OVERLOAD_IMPLEMENTATION: "A call the index "
            "resolves to overload signatures (TypeScript overloads, Python "
            "@overload) links to the one implementation declared beside "
            "them; more than one leaves the call ambiguous.",
        }.items()
    }
)


def _call_name(callee):
    """The name a call site calls: the last name of its callee
    (``c.req.param`` calls ``param``)."""
    return callee.rsplit(".", 1)[-1]


def _count(values):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _evidence(rule):
    return {"evidence_type": rule.evidence_type, "rule_id": rule.rule_id}


def _produced(producer):
    """The indexer (``name@version``) and receipt digest behind a call an
    index decided; nothing for the parser's."""
    if producer is None:
        return {}
    return {"producer": producer[0], "receipt": producer[1]}


@register_tool
class TreeSitterTool(AnalysisTool):
    def __init__(self, repo_path):
        super().__init__(repo_path)
        self._definitions: dict[str, dict] = {}
        self.last_analysis = None
        self.last_reuse = {}
        self.last_scip_report = None

    def _get_metadata(self):
        return ToolMetadata(
            name="tree-sitter",
            version=__version__,
            capabilities={
                ToolCapability.AST_EXTRACTION,
                ToolCapability.SYMBOL_INDEXING,
            },
            supported_languages={
                "c",
                "cpp",
                "csharp",
                "go",
                "java",
                "javascript",
                "php",
                "python",
                "ruby",
                "rust",
                "typescript",
            },
            requires_binary=False,
            binary_name=None,
            performance_class="unmeasured",
        )

    async def check_availability(self):
        return importlib.util.find_spec("tree_sitter") is not None and any(
            importlib.util.find_spec(name) is not None
            for name in (
                "tree_sitter_go",
                "tree_sitter_java",
                "tree_sitter_javascript",
                "tree_sitter_python",
                "tree_sitter_typescript",
            )
        )

    def _index(self, repo_url, languages, file_patterns, skip_patterns):
        import shutil
        import tempfile

        from ..indexers import index_for_analysis

        directory = tempfile.mkdtemp(prefix="deepthought-scip-")
        try:
            # Ingested indexes live in memory; the files are not needed after.
            self.last_scip_report, indexes = index_for_analysis(
                self.repo_path,
                directory,
                repository_id=repo_url,
                languages=None if languages is None else sorted(languages),
                include=file_patterns,
                exclude=skip_patterns,
            )
        finally:
            shutil.rmtree(directory, ignore_errors=True)
        return indexes

    async def analyze(
        self,
        repo_url,
        languages=None,
        file_patterns=None,
        skip_patterns=None,
        previous=None,
        scip_indexes=None,
    ):
        """Analyze the workspace; ``previous`` is an earlier AnalysisResult.

        Call sites in the files a SCIP index covers take their targets from it
        (``deepthought.scip_calls``). By default the analysis first runs the
        pinned indexers (``deepthought.indexers.index_for_analysis``) into a
        scratch directory, and a missing or failing indexer stops it; the
        report is kept on ``last_scip_report``. ``scip_indexes`` instead passes
        indexes a caller already ran: pairs of an ingested index (with the
        workspace's file paths) and the digest of its receipt.

        Unchanged files reuse ``previous`` facts (see analyze_repository); the
        output equals a full run. The analysis and reuse counts are kept on
        ``last_analysis`` and ``last_reuse``, outside the published metadata.
        """
        self._definitions = {}
        started = time.monotonic()
        self.last_reuse = {}
        result = analyze_repository(
            self.repo_path,
            repository_id=repo_url,
            languages=tuple(
                sorted(DEFAULT_LANGUAGES if languages is None else languages)
            ),
            include=("*",) if file_patterns is None else tuple(file_patterns),
            exclude=() if skip_patterns is None else tuple(skip_patterns),
            previous=previous,
            reuse=self.last_reuse,
        )
        self.last_analysis = result
        self.last_scip_report = None
        if scip_indexes is None and result.status == "failed":
            # Nothing was parsed (no grammars installed): no call to bind.
            scip_indexes = ()
        if scip_indexes is None:
            scip_indexes = self._index(
                repo_url, languages, file_patterns, skip_patterns
            )
        # Preserve the original engine's Repository -> File -> declaration
        # projection. Extraction still belongs to the unchanged SDK parser.
        repository_name = f"{repo_url}::repository"
        nodes = [
            NodeData(
                ["Repository"],
                {
                    "qualified_name": repository_name,
                    "repo_url": repo_url,
                    "name": repo_url,
                    "url": repo_url,
                },
            )
        ]
        relationships = []
        files = {}
        imports = {item.id: item for item in result.imports}
        # Configuration facts (tsconfig paths, package.json entries) steer
        # resolution; they are not imports of their file.
        resolutions = tuple(
            r
            for r in import_rules.resolve_imports(result)
            if r.reason != import_rules.RESOLUTION_CONFIGURATION
        )
        # Imports in files an index covers resolve as the language's own
        # tooling resolves them; external modules name their package.
        languages_by_file = {c.file: c.language for c in result.coverage}
        language_of = {
            item.id: languages_by_file.get(item.file) for item in result.imports
        }
        scip_bound = scip_imports.bind_imports(
            result,
            scip_indexes,
            self.repo_path,
            {r.import_id: r for r in resolutions},
        )
        resolutions = tuple(
            scip_bound[r.import_id][0] if r.import_id in scip_bound else r
            for r in resolutions
        )
        undisclosed = {}
        for resolution in resolutions:
            if resolution.status != "linked":
                item = imports[resolution.import_id]
                key = (
                    "external_imports"
                    if resolution.reason == scip_imports.EXTERNAL
                    else resolution.status + "_imports"
                )
                undisclosed.setdefault(item.file, {}).setdefault(key, set()).add(
                    item.specifier
                )
        declared_packages = {
            item.file: {item.kind: item.name} for item in result.packages
        }
        # Linguist's rules (and the repository's own linguist-generated
        # attributes) say which files a generator wrote; nothing is parsed
        # when the analysis failed.
        generated = (
            {}
            if result.status == "failed"
            else generated_files(
                self.repo_path,
                [
                    c.file
                    for c in result.coverage
                    if c.status in {"analyzed", "partial"}
                ],
            )
        )
        for coverage in result.coverage:
            file_name = f"{repo_url}::{coverage.file}::file"
            files[coverage.file] = file_name
            nodes.append(
                NodeData(
                    ["File"],
                    {
                        "qualified_name": file_name,
                        "repo_url": repo_url,
                        "path": coverage.file,
                        "file_path": coverage.file,
                        "name": coverage.file,
                        "language": coverage.language,
                        "coverage_status": coverage.status,
                        **(
                            {"file_sha256": coverage.digest}
                            if coverage.digest is not None
                            else {}
                        ),
                        **declared_packages.get(coverage.file, {}),
                        **(
                            {"generated_by": generated[coverage.file]}
                            if coverage.file in generated
                            else {}
                        ),
                        **{
                            key: sorted(values)
                            for key, values in sorted(
                                undisclosed.get(coverage.file, {}).items()
                            )
                        },
                    },
                )
            )
            relationships.append(
                RelationshipData(
                    "CONTAINS",
                    ["Repository"],
                    {"qualified_name": repository_name, "repo_url": repo_url},
                    ["File"],
                    {"qualified_name": file_name, "repo_url": repo_url},
                    _evidence(CONTAINS_RULE),
                )
            )
        sources, changed = _declaration_sources(self.repo_path, result)
        for definition in result.definitions:
            qualified_name = (
                f"{repo_url}::{definition.file}::{definition.symbol}::{definition.id}"
            )
            properties = {
                "qualified_name": qualified_name,
                "repo_url": repo_url,
                "definition_id": definition.id,
                "file_path": definition.file,
                "name": definition.symbol,
                "display_name": definition.symbol,
                "kind": definition.kind,
                "start_line": definition.line,
                "end_line": definition.end_line,
                "column": definition.column,
                "end_column": definition.end_column,
                "coordinate_encoding": definition.coordinate_encoding,
                **sources.get(definition.id, {}),
            }
            nodes.append(
                NodeData(labels=[definition.kind, "Symbol"], properties=properties)
            )
            relationships.append(
                RelationshipData(
                    "DEFINES",
                    ["File"],
                    {"qualified_name": files[definition.file], "repo_url": repo_url},
                    [definition.kind, "Symbol"],
                    {"qualified_name": qualified_name, "repo_url": repo_url},
                    _evidence(DEFINES_RULE),
                )
            )
        # Each declaration's owner is the innermost other declaration whose span
        # strictly encloses it in the same file.
        spans = {}
        for d in result.definitions:
            spans.setdefault(d.file, []).append(
                ((d.line, d.column), (d.end_line, d.end_column), d)
            )
        for file, items in spans.items():
            # Parse-tree spans nest, so a stack of open spans gives each owner.
            items.sort(key=lambda item: (item[0], tuple(-v for v in item[1])))
            open_spans = []
            for start, end, inner in items:
                while open_spans and open_spans[-1][1] < end:
                    open_spans.pop()
                owner = next(
                    (
                        outer
                        for _, outer_end, outer in reversed(open_spans)
                        if (outer_end, outer.line, outer.column) != (end, *start)
                    ),
                    None,
                )
                open_spans.append((start, end, inner))
                if owner is None:
                    continue
                relationships.append(
                    RelationshipData(
                        "CONTAINS",
                        [owner.kind, "Symbol"],
                        {
                            "qualified_name": f"{repo_url}::{owner.file}::{owner.symbol}::{owner.id}",
                            "repo_url": repo_url,
                        },
                        [inner.kind, "Symbol"],
                        {
                            "qualified_name": f"{repo_url}::{inner.file}::{inner.symbol}::{inner.id}",
                            "repo_url": repo_url,
                        },
                        {"line": inner.line, **_evidence(NESTS_RULE)},
                    )
                )
        # Tests and fixtures depend on the pytest fixtures they request.
        python_files = {
            c.file: (self.repo_path / c.file).read_text(
                encoding="utf-8", errors="replace"
            )
            for c in result.coverage
            if c.language == "python"
        }
        # The pytest and FastAPI rules read Python's ast, in the newest
        # Python the repository declares; a file it still cannot parse is
        # reported.
        if python_files:
            facts, reader = read_ast_facts(self.repo_path, python_files)
        else:
            facts, reader = ast_facts({}), None
        interpreter = ".".join(facts["python"].split(".")[:2])
        unparsed = facts["unparsed"]
        by_line = {
            (d.file, d.line): d
            for d in result.definitions
            if d.kind in {"Function", "Method", "Class"}
        }
        for use in (
            FixtureUse(
                **{
                    **f,
                    "consumer": tuple(f["consumer"]),
                    "fixture": tuple(f["fixture"]),
                }
            )
            for f in facts["fixtures"]
        ):
            consumer = by_line.get(use.consumer)
            fixture = by_line.get(use.fixture)
            if consumer is None or fixture is None:
                continue
            relationships.append(
                RelationshipData(
                    "USES_FIXTURE",
                    [consumer.kind, "Symbol"],
                    {
                        "qualified_name": f"{repo_url}::{consumer.file}::{consumer.symbol}::{consumer.id}",
                        "repo_url": repo_url,
                    },
                    [fixture.kind, "Symbol"],
                    {
                        "qualified_name": f"{repo_url}::{fixture.file}::{fixture.symbol}::{fixture.id}",
                        "repo_url": repo_url,
                    },
                    {
                        "line": use.line,
                        "name": use.name,
                        **_evidence(_FIXTURE_RULES[use.rule]),
                    },
                )
            )
        # Endpoints and routers depend on the FastAPI dependencies they inject.
        references = ScipReferences(result, scip_indexes, self.repo_path)
        for use in dependency_uses(facts["dependencies"], references):
            file, line = use.consumer
            if line is None:
                source = (["File"], files[file])
            else:
                consumer = by_line.get(use.consumer)
                if consumer is None:
                    continue
                source = (
                    [consumer.kind, "Symbol"],
                    f"{repo_url}::{consumer.file}::{consumer.symbol}::{consumer.id}",
                )
            target = use.dependency
            relationships.append(
                RelationshipData(
                    "USES_DEPENDENCY",
                    source[0],
                    {"qualified_name": source[1], "repo_url": repo_url},
                    [target.kind, "Symbol"],
                    {
                        "qualified_name": f"{repo_url}::{target.file}::{target.symbol}::{target.id}",
                        "repo_url": repo_url,
                    },
                    {
                        "line": use.line,
                        "column": use.column,
                        "name": use.name,
                        "injector": use.injector,
                        **({"alias": use.alias} if use.alias else {}),
                        **_evidence(DEPENDENCY_RULE),
                        **_produced(references.tool(file)),
                    },
                )
            )
        for resolution in resolutions:
            item = imports[resolution.import_id]
            for link in resolution.links:
                relationships.append(
                    RelationshipData(
                        "IMPORTS",
                        ["File"],
                        {"qualified_name": files[item.file], "repo_url": repo_url},
                        ["File"],
                        {
                            "qualified_name": files[link.target_file],
                            "repo_url": repo_url,
                        },
                        {
                            "import_id": item.id,
                            "specifier": item.specifier,
                            "import_kind": item.kind,
                            "names": list(item.names),
                            "line": item.line,
                            **_evidence(IMPORT_RULES[link.rule_id]),
                        },
                    )
                )
        packages = {}
        for import_id, (_, found) in sorted(scip_bound.items()):
            item = imports[import_id]
            for package in found:
                name = (
                    f"{repo_url}::{package.ecosystem}-package::"
                    f"{package.name}::{package.version}"
                )
                if name not in packages:
                    packages[name] = package
                    nodes.append(
                        NodeData(
                            ["ExternalPackage"],
                            {
                                "qualified_name": name,
                                "repo_url": repo_url,
                                "name": package.name,
                                "version": package.version,
                                "ecosystem": package.ecosystem,
                            },
                        )
                    )
                relationships.append(
                    RelationshipData(
                        "IMPORTS_PACKAGE",
                        ["File"],
                        {"qualified_name": files[item.file], "repo_url": repo_url},
                        ["ExternalPackage"],
                        {"qualified_name": name, "repo_url": repo_url},
                        {
                            "import_id": item.id,
                            "specifier": item.specifier,
                            "import_kind": item.kind,
                            "names": list(item.names),
                            "modules": list(package.modules),
                            "line": item.line,
                            **_evidence(PACKAGE_RULES[package.rule_id]),
                        },
                    )
                )
        qualified = {
            d.id: f"{repo_url}::{d.file}::{d.symbol}::{d.id}"
            for d in result.definitions
        }
        kinds = {d.id: d.kind for d in result.definitions}
        sites = {site.id: site for site in result.call_sites}
        call_counts, resolver_counts = {}, {}
        binding = bind_calls(result, scip_indexes, self.repo_path)
        for resolution in binding.resolutions:
            producer = binding.producers.get(resolution.call_id)
            key = resolution.status + (
                ":" + resolution.reason if resolution.reason else ""
            )
            call_counts[key] = call_counts.get(key, 0) + 1
            # Separately by who decided: the index (its "nothing here"
            # included), the parser kept over a suspect index target, or the
            # parser in a file no index covers.
            by_resolver = resolver_counts.setdefault(
                binding.resolver(resolution.call_id), {}
            )
            by_resolver[key] = by_resolver.get(key, 0) + 1
            site = sites[resolution.call_id]
            if site.caller_id is None:
                source = (["File"], files[site.file])
            else:
                source = (
                    [kinds[site.caller_id], "Symbol"],
                    qualified[site.caller_id],
                )
            if resolution.status != "linked":
                # The call is a fact of the code even when no rule names its
                # target: kept so that linked and unlinked calls of a name
                # can be counted together.
                call_name = f"{repo_url}::{site.file}::call::{site.id}"
                nodes.append(
                    NodeData(
                        ["UnresolvedCall"],
                        {
                            "qualified_name": call_name,
                            "repo_url": repo_url,
                            "call_id": site.id,
                            "callee": site.callee,
                            "name": _call_name(site.callee),
                            "call_kind": site.kind,
                            "file_path": site.file,
                            "line": site.line,
                            "column": site.column,
                            "end_line": site.end_line,
                            "end_column": site.end_column,
                            "status": resolution.status,
                            # Which resolver left it unlinked, and why.
                            "resolver": "tree-sitter" if producer is None else "scip",
                            **_produced(producer),
                            **(
                                {"reason": resolution.reason}
                                if resolution.reason
                                else {}
                            ),
                            **(
                                {
                                    "candidates": [
                                        qualified[c] for c in resolution.candidates
                                    ]
                                }
                                if resolution.candidates
                                else {}
                            ),
                        },
                    )
                )
                relationships.append(
                    RelationshipData(
                        "HAS_UNRESOLVED_CALL",
                        source[0],
                        {"qualified_name": source[1], "repo_url": repo_url},
                        ["UnresolvedCall"],
                        {"qualified_name": call_name, "repo_url": repo_url},
                        {"line": site.line, **_evidence(UNLINKED_CALL_RULE)},
                    )
                )
                continue
            relationships.append(
                RelationshipData(
                    "CALLS",
                    source[0],
                    {"qualified_name": source[1], "repo_url": repo_url},
                    [kinds[resolution.target_id], "Symbol"],
                    {
                        "qualified_name": qualified[resolution.target_id],
                        "repo_url": repo_url,
                    },
                    {
                        "call_id": site.id,
                        "callee": site.callee,
                        "name": _call_name(site.callee),
                        "call_kind": site.kind,
                        "line": site.line,
                        "column": site.column,
                        **_produced(producer),
                        **_evidence(CALL_RULES[resolution.rule_id]),
                    },
                )
            )
        named_types = signature_types(result, scip_indexes, self.repo_path)
        for fact in named_types:
            relationships.append(
                RelationshipData(
                    "SIGNATURE_USES",
                    [kinds[fact.definition_id], "Symbol"],
                    {
                        "qualified_name": qualified[fact.definition_id],
                        "repo_url": repo_url,
                    },
                    [kinds[fact.type_id], "Symbol"],
                    {"qualified_name": qualified[fact.type_id], "repo_url": repo_url},
                    {
                        "line": fact.line,
                        "column": fact.column,
                        **_evidence(SIGNATURE_TYPE_RULE),
                    },
                )
            )
        bases = {item.id: item for item in result.supertypes}
        base_counts = {}
        for base_binding in scip_bases.bind_supertypes(
            result, base_rules.resolve_supertypes(result), references
        ):
            resolution = base_binding.resolution
            key = resolution.status + (
                ":" + resolution.reason if resolution.reason else ""
            )
            base_counts[key] = base_counts.get(key, 0) + 1
            if resolution.status != "linked":
                continue
            item = bases[resolution.call_id]
            relationships.append(
                RelationshipData(
                    "INHERITS_FROM",
                    [kinds[item.owner_id], "Symbol"],
                    {"qualified_name": qualified[item.owner_id], "repo_url": repo_url},
                    [kinds[resolution.target_id], "Symbol"],
                    {
                        "qualified_name": qualified[resolution.target_id],
                        "repo_url": repo_url,
                    },
                    {
                        "supertype_id": item.id,
                        "base": item.name,
                        "heritage": item.kind,
                        "line": item.line,
                        "column": item.column,
                        **_produced(base_binding.producer),
                        **_evidence(BASE_RULES[resolution.rule_id]),
                    },
                )
            )
        impls = {item.id: item for item in result.impls}
        for resolution in base_rules.resolve_rust_impls(result):
            if resolution.trait is None:
                continue
            item = impls[resolution.impl_id]
            if resolution.type.status != "linked":
                key = (
                    "type_"
                    + resolution.type.status
                    + (":" + resolution.type.reason if resolution.type.reason else "")
                )
            else:
                key = resolution.trait.status + (
                    ":" + resolution.trait.reason if resolution.trait.reason else ""
                )
            base_counts[key] = base_counts.get(key, 0) + 1
            if key != "linked":
                continue
            owner, trait = resolution.type.target_id, resolution.trait.target_id
            relationships.append(
                RelationshipData(
                    "INHERITS_FROM",
                    [kinds[owner], "Symbol"],
                    {"qualified_name": qualified[owner], "repo_url": repo_url},
                    [kinds[trait], "Symbol"],
                    {"qualified_name": qualified[trait], "repo_url": repo_url},
                    {
                        "impl_id": item.id,
                        "base": item.trait,
                        "heritage": "impl",
                        "line": item.line,
                        "column": item.column,
                        **_evidence(BASE_RULES[base_rules.RUST_IMPL_TRAIT]),
                    },
                )
            )
        implementations = go_interfaces.resolve_go_implementations(result)
        for item in implementations:
            relationships.append(
                RelationshipData(
                    "IMPLEMENTS",
                    [kinds[item.type_id], "Symbol"],
                    {"qualified_name": qualified[item.type_id], "repo_url": repo_url},
                    [kinds[item.interface_id], "Symbol"],
                    {
                        "qualified_name": qualified[item.interface_id],
                        "repo_url": repo_url,
                    },
                    _evidence(GO_INTERFACE_RULE),
                )
            )
        # A call of an interface method can run the method each implementing
        # type declares: the interface method dispatches to it.
        dispatches = sorted({pair for item in implementations for pair in item.methods})
        for interface_method, method in dispatches:
            relationships.append(
                RelationshipData(
                    "DISPATCHES_TO",
                    [kinds[interface_method], "Symbol"],
                    {
                        "qualified_name": qualified[interface_method],
                        "repo_url": repo_url,
                    },
                    [kinds[method], "Symbol"],
                    {"qualified_name": qualified[method], "repo_url": repo_url},
                    _evidence(GO_INTERFACE_RULE),
                )
            )
        # Which families a deterministic resolver linked, per language.
        coverage_records = edge_coverage(
            result,
            f"tree-sitter@{__version__}",
            (
                [(site.id, site.file) for site in result.call_sites],
                {r.call_id: r.status for r in binding.resolutions},
                {key: value[0] for key, value in binding.producers.items()},
            ),
            (
                [(r.import_id, imports[r.import_id].file) for r in resolutions],
                {r.import_id: r.status for r in resolutions},
                # Every import is decided by the parser's rules.
                {},
            ),
        )
        for record in coverage_records:
            nodes.append(
                NodeData(
                    ["EdgeCoverage"],
                    {
                        "qualified_name": f"{repo_url}::edge-coverage::"
                        f"{record.family}::{record.language}",
                        "repo_url": repo_url,
                        **{
                            key: value
                            for key, value in record.to_dict().items()
                            if value is not None
                        },
                    },
                )
            )
        status_counts = dict.fromkeys(("linked", "ambiguous", "unresolved"), 0)
        for resolution in resolutions:
            status_counts[resolution.status] += 1
        artifact = result.to_dict()
        diagnostics = artifact["diagnostics"]
        metadata = ToolExecutionMetadata(
            tool_name=self.metadata.name,
            tool_version=self.metadata.version,
            execution_time_seconds=time.monotonic() - started,
            languages_processed=sorted({c.language for c in result.coverage}),
            files_processed=len(result.coverage),
            errors=[d["message"] for d in diagnostics]
            if result.status == "failed"
            else [],
            warnings=[d["message"] for d in diagnostics]
            + [
                f"python_ast_unparsed: {file}: Python {interpreter}'s ast cannot "
                f"parse it ({error}), so no pytest fixture or FastAPI dependency "
                "is read from it"
                for file, error in unparsed.items()
            ]
            + [
                f"python_interpreter_unavailable: {error}"
                for error in (reader.errors if reader else ())
            ]
            if result.status != "failed"
            else [],
            custom_metrics={
                "analysis_status": result.status,
                "model_tokens": 0,
                "snapshot_id": result.snapshot_id,
                "repository_id": result.repository_id,
                "coverage": artifact["coverage"],
                "diagnostics": diagnostics,
                "analyzer_versions": artifact["analyzer_versions"],
                "capabilities": artifact["capabilities"],
                # Unlinked calls are stored as UnresolvedCall nodes.
                "unresolved_call_sites": True,
                "source_facts_schema_version": result.schema_version,
                "import_resolution": status_counts,
                # Of those, the imports an index decided, by language.
                "scip_imports": {
                    language: _count(
                        r.reason or r.status
                        for key, (r, _) in scip_bound.items()
                        if language_of[key] == language
                    )
                    for language in sorted({language_of[key] for key in scip_bound})
                },
                "external_packages": len(packages),
                "call_resolution": dict(sorted(call_counts.items())),
                "call_resolution_by_resolver": {
                    resolver: dict(sorted(counts.items()))
                    for resolver, counts in sorted(resolver_counts.items())
                },
                "supertype_resolution": dict(sorted(base_counts.items())),
                "signature_types": len(named_types),
                "go_implementations": len(implementations),
                "python_ast_unparsed": {
                    "python": interpreter,
                    "files": sorted(unparsed),
                },
                "python_interpreter": {
                    "version": facts["python"],
                    "declared": list(reader.declared) if reader else [],
                    "managed": bool(reader and reader.executable),
                    "errors": list(reader.errors) if reader else [],
                },
                "source_context": {
                    "declarations": len(sources),
                    "files_changed_since_analysis": changed,
                },
                "scope": "named declarations, static file imports, and "
                "name-matched calls and declared bases; no control or data-flow "
                "inference",
            },
        )
        output = StandardizedOutput(
            nodes=nodes, relationships=relationships, metadata=metadata
        )
        self._definitions = {
            n.properties["qualified_name"]: deepcopy(n.properties)
            for n in nodes
            if "Symbol" in n.labels
        }
        return output

    async def get_symbol_definitions(self):
        return deepcopy(self._definitions)

    async def cleanup(self):
        self._definitions = {}
