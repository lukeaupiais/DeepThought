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
from .. import inheritance as base_rules
from ..engine import RUST_ITEM_MACROS, analyze_repository
from ..syntax import (
    DEFAULT_LANGUAGES,
    LANGUAGES,
    RustExpander,
    declaration_sources,
    make_parser,
    prepare_source,
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
                parsers[key].parse(prepare_source(item.language, content)),
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
    "initializes it with 'new' of that class, the class found as for a "
    "static method's C, links to the instance method m that class declares "
    "or inherits through 'extends'. A field of that name in a class on the "
    "way, or a base that names no repository class, stops the lookup.",
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
    call_rules.JAVA_STATIC_FIELD_RECEIVER: "In a Java call 'T.F.name' where no "
    "variable or field named by the first name of T can be in scope (none "
    "declared around the call, and no type around it may inherit one), T "
    "denotes a type by Java's lookup order that declares the static field or "
    "enum constant F, whose declaration writes a class type U (an enum "
    "constant: its enum); U declares or inherits the method, chosen among "
    "overloads as other Java calls are.",
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
BASE_RULES[base_rules.RUST_SUPERTRAIT] = register_rule(
    EvidenceRule(
        base_rules.RUST_SUPERTRAIT,
        "resolved",
        "A Rust trait's supertrait (a bound of the trait or of 'where Self:') "
        "names a trait, its path read from the trait's module as a use path "
        "is (after the items and uses of the function bodies around it).",
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


def _evidence(rule):
    return {"evidence_type": rule.evidence_type, "rule_id": rule.rule_id}


@register_tool
class TreeSitterTool(AnalysisTool):
    def __init__(self, repo_path):
        super().__init__(repo_path)
        self._definitions: dict[str, dict] = {}
        self.last_analysis = None
        self.last_reuse = {}

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

    async def analyze(
        self,
        repo_url,
        languages=None,
        file_patterns=None,
        skip_patterns=None,
        previous=None,
    ):
        """Analyze the workspace; ``previous`` is an earlier AnalysisResult.

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
        undisclosed = {}
        for resolution in resolutions:
            if resolution.status != "linked":
                item = imports[resolution.import_id]
                undisclosed.setdefault(item.file, {}).setdefault(
                    resolution.status + "_imports", set()
                ).add(item.specifier)
        declared_packages = {
            item.file: {item.kind: item.name} for item in result.packages
        }
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
        qualified = {
            d.id: f"{repo_url}::{d.file}::{d.symbol}::{d.id}"
            for d in result.definitions
        }
        kinds = {d.id: d.kind for d in result.definitions}
        sites = {site.id: site for site in result.call_sites}
        call_counts = {}
        for resolution in call_rules.resolve_calls(result):
            key = resolution.status + (
                ":" + resolution.reason if resolution.reason else ""
            )
            call_counts[key] = call_counts.get(key, 0) + 1
            if resolution.status != "linked":
                continue
            site = sites[resolution.call_id]
            if site.caller_id is None:
                source = (["File"], files[site.file])
            else:
                source = (
                    [kinds[site.caller_id], "Symbol"],
                    qualified[site.caller_id],
                )
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
                        "call_kind": site.kind,
                        "line": site.line,
                        "column": site.column,
                        **_evidence(CALL_RULES[resolution.rule_id]),
                    },
                )
            )
        bases = {item.id: item for item in result.supertypes}
        base_counts = {}
        for resolution in base_rules.resolve_supertypes(result):
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
                "source_facts_schema_version": result.schema_version,
                "import_resolution": status_counts,
                "call_resolution": dict(sorted(call_counts.items())),
                "supertype_resolution": dict(sorted(base_counts.items())),
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
