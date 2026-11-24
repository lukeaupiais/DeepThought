// Resolves Java method calls and instance creations with javac's attribution.
//
// Parses, enters and attributes the .java files of one source root with the
// given source path (nothing is written), then prints one JSON line per method
// invocation and `new` expression: the file, the start line and UTF-16 column
// of the expression (counted from the line start, so a tab is one column,
// unlike LineMap.getColumnNumber, which expands tabs), and, when javac bound
// it to a declaration in a source file, that file, the declaration's lexically qualified name and start line.
// A `new` expression is reported by the class it creates. Each extends or
// implements entry of a class, interface, enum or record is reported too,
// marked "base", by the type it names. Attribution errors
// from missing libraries are ignored; calls javac cannot bind have no target.
//
// Usage:
//   java --add-exports jdk.compiler/com.sun.tools.javac.code=ALL-UNNAMED \
//     --add-exports jdk.compiler/com.sun.tools.javac.api=ALL-UNNAMED \
//     tools/JavaCallOracle.java REPOSITORY ROOT [SOURCEPATH_ROOT ...]

import com.sun.source.tree.ClassTree;
import com.sun.source.tree.CompilationUnitTree;
import com.sun.source.tree.LineMap;
import com.sun.source.tree.MethodInvocationTree;
import com.sun.source.tree.MethodTree;
import com.sun.source.tree.VariableTree;
import com.sun.source.tree.NewClassTree;
import com.sun.source.tree.Tree;
import com.sun.source.util.JavacTask;
import com.sun.source.util.SourcePositions;
import com.sun.source.util.TreePath;
import com.sun.source.util.TreePathScanner;
import com.sun.source.util.Trees;
import com.sun.tools.javac.code.Symbol.ClassSymbol;
import java.io.File;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Stream;
import javax.lang.model.element.Element;
import javax.lang.model.element.ElementKind;
import javax.lang.model.element.ExecutableElement;
import javax.lang.model.element.PackageElement;
import javax.lang.model.type.ArrayType;
import javax.lang.model.type.DeclaredType;
import javax.lang.model.type.TypeKind;
import javax.lang.model.type.TypeMirror;
import javax.lang.model.type.WildcardType;
import javax.lang.model.element.TypeElement;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;

public class JavaCallOracle {
    static Path repository;
    static Trees trees;

    /** Whether a type is, or is built from, one javac could not resolve. */
    static boolean erroneous(TypeMirror type) {
        if (type == null) return false;
        if (type.getKind() == TypeKind.ERROR) return true;
        if (type instanceof ArrayType array) return erroneous(array.getComponentType());
        if (type instanceof WildcardType wildcard) {
            return erroneous(wildcard.getExtendsBound()) || erroneous(wildcard.getSuperBound());
        }
        if (type instanceof DeclaredType declared) {
            for (TypeMirror argument : declared.getTypeArguments()) {
                if (erroneous(argument)) return true;
            }
        }
        return false;
    }

    static String quote(String text) {
        StringBuilder out = new StringBuilder("\"");
        for (char c : text.toCharArray()) {
            if (c == '"' || c == '\\') out.append('\\').append(c);
            else if (c < 0x20) out.append(String.format("\\u%04x", (int) c));
            else out.append(c);
        }
        return out.append('"').toString();
    }

    static String target(Element element) {
        if (element == null) return null;
        Element top = element;
        while (top != null && !(top instanceof TypeElement && top.getEnclosingElement() instanceof PackageElement)) {
            top = top.getEnclosingElement();
        }
        if (!(top instanceof ClassSymbol type) || type.sourcefile == null
                || type.sourcefile.getKind() != JavaFileObject.Kind.SOURCE) return null;
        Path file = Path.of(type.sourcefile.getName()).toAbsolutePath().normalize();
        if (!file.startsWith(repository)) return null;
        TreePath path = trees.getPath(element);
        if (path == null) return null;  // Implicit, such as a default constructor.
        List<String> names = new ArrayList<>();
        for (Element e = element; e != null && !(e instanceof PackageElement); e = e.getEnclosingElement()) {
            // Lexical names, as tools/JavaDeclarationOracle.java gives them:
            // anonymous classes and fields (an enum constant's body) add none.
            if (e.getKind().isField() || e.getSimpleName().isEmpty()) continue;
            String name = e.getKind() == ElementKind.CONSTRUCTOR
                ? e.getEnclosingElement().getSimpleName().toString() : e.getSimpleName().toString();
            names.add(0, name);
        }
        CompilationUnitTree unit = path.getCompilationUnit();
        long start = trees.getSourcePositions().getStartPosition(unit, path.getLeaf());
        return "\"target_file\":" + quote(repository.relativize(file).toString().replace('\\', '/'))
            + ",\"target_symbol\":" + quote(String.join(".", names))
            + ",\"target_line\":" + unit.getLineMap().getLineNumber(start);
    }

    public static void main(String[] args) throws Exception {
        repository = Path.of(args[0]).toAbsolutePath().normalize();
        Path root = repository.resolve(args[1]);
        List<String> sourcepath = new ArrayList<>();
        for (int i = 1; i < args.length; i++) sourcepath.add(repository.resolve(args[i]).toString());
        List<Path> files;
        try (Stream<Path> walk = Files.walk(root)) {
            files = walk.filter(p -> p.toString().endsWith(".java") && Files.isRegularFile(p))
                .sorted().toList();
        }
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        StandardJavaFileManager manager = compiler.getStandardFileManager(null, null, null);
        JavacTask task = (JavacTask) compiler.getTask(
            null, manager, d -> {}, List.of("-proc:none", "-implicit:none",
                "-sourcepath", String.join(File.pathSeparator, sourcepath)),
            null, manager.getJavaFileObjectsFromPaths(files));
        trees = Trees.instance(task);
        List<CompilationUnitTree> units = new ArrayList<>();
        task.parse().forEach(units::add);
        try {
            task.analyze();
        } catch (RuntimeException | AssertionError e) {
            // javac can stop on broken input; attributed trees remain usable.
        }
        SourcePositions positions = trees.getSourcePositions();
        for (CompilationUnitTree unit : units) {
            String file = repository.relativize(Path.of(unit.getSourceFile().getName()).toAbsolutePath().normalize())
                .toString().replace('\\', '/');
            LineMap lines = unit.getLineMap();
            new TreePathScanner<Void, Void>() {
                void emit(Tree node, Element element) {
                    emit(node, element, "");
                }

                void emit(Tree node, Element element, String marker) {
                    long start = positions.getStartPosition(unit, node);
                    String bound = null;
                    try {
                        bound = target(element);
                    } catch (RuntimeException e) {
                        bound = null;
                    }
                    System.out.println("{\"file\":" + quote(file) + ",\"line\":" + lines.getLineNumber(start)
                        + ",\"column\":" + (start - lines.getStartPosition(lines.getLineNumber(start))) + marker
                        + (bound == null ? "" : "," + bound) + "}");
                }

                @Override
                public Void visitClass(ClassTree node, Void unused) {
                    List<Tree> bases = new ArrayList<>();
                    if (node.getExtendsClause() != null) bases.add(node.getExtendsClause());
                    bases.addAll(node.getImplementsClause());
                    for (Tree base : bases) {
                        Element type = trees.getElement(new TreePath(getCurrentPath(), base));
                        emit(base, type, ",\"base\":true");
                    }
                    return super.visitClass(node, null);
                }

                @Override
                public Void visitMethodInvocation(MethodInvocationTree node, Void unused) {
                    Element method = trees.getElement(getCurrentPath());
                    boolean uncertain = false;
                    if (method instanceof ExecutableElement chosen
                            && chosen.getEnclosingElement() instanceof TypeElement owner) {
                        // Among overloads, a type javac could not resolve (no
                        // class path is given) leaves its choice to error
                        // recovery: such a call is reported as uncertain.
                        // Declared and inherited, read from the source where
                        // there is one: javac drops a method whose erroneous
                        // signature clashes with another's.
                        int overloads = 0;
                        List<TypeMirror> parameters = new ArrayList<>();
                        List<TypeElement> types = new ArrayList<>(List.of(owner));
                        for (int i = 0; i < types.size(); i++) {
                            TypeElement type = types.get(i);
                            TreePath declared = trees.getPath(type);
                            if (declared != null && declared.getLeaf() instanceof ClassTree body) {
                                for (Tree member : body.getMembers()) {
                                    if (member instanceof MethodTree other
                                            && other.getName().equals(chosen.getSimpleName())) {
                                        overloads++;
                                        for (VariableTree parameter : other.getParameters()) {
                                            parameters.add(trees.getTypeMirror(
                                                new TreePath(new TreePath(new TreePath(declared, other), parameter),
                                                    parameter.getType())));
                                        }
                                    }
                                }
                            } else {
                                for (Element member : type.getEnclosedElements()) {
                                    if (member instanceof ExecutableElement other
                                            && other.getSimpleName().equals(chosen.getSimpleName())) {
                                        overloads++;
                                        other.getParameters().forEach(p -> parameters.add(p.asType()));
                                    }
                                }
                            }
                            for (TypeMirror base : task.getTypes().directSupertypes(type.asType())) {
                                if (task.getTypes().asElement(base) instanceof TypeElement next
                                        && !types.contains(next)) {
                                    types.add(next);
                                }
                            }
                        }
                        if (overloads > 1) {
                            for (Tree argument : node.getArguments()) {
                                uncertain |= erroneous(trees.getTypeMirror(new TreePath(getCurrentPath(), argument)));
                            }
                            for (TypeMirror parameter : parameters) {
                                uncertain |= erroneous(parameter);
                            }
                        }
                    }
                    emit(node, method, uncertain ? ",\"uncertain\":true" : "");
                    return super.visitMethodInvocation(node, null);
                }

                @Override
                public Void visitNewClass(NewClassTree node, Void unused) {
                    Element constructor = trees.getElement(getCurrentPath());
                    Element created = constructor == null ? null : constructor.getEnclosingElement();
                    // Anonymous classes are reported by the type they extend.
                    if (created instanceof TypeElement type && type.getNestingKind().name().equals("ANONYMOUS")) {
                        created = trees.getElement(new TreePath(getCurrentPath(), node.getIdentifier()));
                    }
                    emit(node, created);
                    return super.visitNewClass(node, null);
                }
            }.scan(unit, null);
        }
    }
}
