// Resolves Java method calls and instance creations with javac's attribution.
//
// Parses, enters and attributes the .java files of one source root with the
// given source path (nothing is written), then prints one JSON line per method
// invocation and `new` expression: the file, the start line and UTF-16 column
// of the expression, and, when javac bound it to a declaration in a source
// file, that file, the declaration's lexically qualified name and start line.
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
import javax.lang.model.element.TypeElement;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;

public class JavaCallOracle {
    static Path repository;
    static Trees trees;

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
                        + ",\"column\":" + (lines.getColumnNumber(start) - 1) + marker
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
                    emit(node, trees.getElement(getCurrentPath()));
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
