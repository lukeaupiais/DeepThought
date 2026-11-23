// Resolves Java imports with javac's own attribution.
//
// Parses and enters the .java files of one source root (nothing is compiled
// or written) with the given source path, then prints one JSON line per import: the importing
// file, the import's line, and the source files javac bound it to. A type
// import binds the file declaring the type; a wildcard or static import of a
// package or type binds every source file of that package, or the type's
// file. Imports javac binds to no source file (the JDK, missing libraries)
// have no targets. Missing libraries are reported by javac and ignored here.
//
// Usage:
//   java --add-exports jdk.compiler/com.sun.tools.javac.code=ALL-UNNAMED \
//     --add-exports jdk.compiler/com.sun.tools.javac.api=ALL-UNNAMED \
//     tools/JavaImportOracle.java REPOSITORY ROOT [SOURCEPATH_ROOT ...]
// ROOT and the source path roots are relative to REPOSITORY; ROOT comes first.

import com.sun.source.tree.CompilationUnitTree;
import com.sun.source.tree.ImportTree;
import com.sun.source.tree.MemberSelectTree;
import com.sun.source.tree.Tree;
import com.sun.source.util.JavacTask;
import com.sun.source.util.TreePath;
import com.sun.source.util.Trees;
import com.sun.tools.javac.api.JavacTaskImpl;
import com.sun.tools.javac.code.Symbol.ClassSymbol;
import java.io.File;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.TreeSet;
import java.util.stream.Stream;
import javax.lang.model.element.Element;
import javax.lang.model.element.PackageElement;
import javax.lang.model.element.TypeElement;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;

public class JavaImportOracle {
    static Path repository;

    static String quote(String text) {
        StringBuilder out = new StringBuilder("\"");
        for (char c : text.toCharArray()) {
            if (c == '"' || c == '\\') out.append('\\').append(c);
            else if (c < 0x20) out.append(String.format("\\u%04x", (int) c));
            else out.append(c);
        }
        return out.append('"').toString();
    }

    static void add(TreeSet<String> targets, Element element) {
        while (element != null && !(element instanceof TypeElement && element.getEnclosingElement() instanceof PackageElement)) {
            element = element.getEnclosingElement();
        }
        if (element instanceof ClassSymbol type && type.sourcefile != null
                && type.sourcefile.getKind() == JavaFileObject.Kind.SOURCE) {
            Path file = Path.of(type.sourcefile.getName()).toAbsolutePath().normalize();
            if (file.startsWith(repository)) {
                targets.add(repository.relativize(file).toString().replace('\\', '/'));
            }
        }
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
        Trees trees = Trees.instance(task);
        List<CompilationUnitTree> units = new ArrayList<>();
        task.parse().forEach(units::add);
        // Entering resolves imports; method bodies need no attribution.
        ((JavacTaskImpl) task).enter();
        for (CompilationUnitTree unit : units) {
            String file = repository.relativize(Path.of(unit.getSourceFile().getName()).toAbsolutePath().normalize())
                .toString().replace('\\', '/');
            TreePath unitPath = new TreePath(unit);
            for (ImportTree item : unit.getImports()) {
                if (item.getQualifiedIdentifier().getKind() != Tree.Kind.MEMBER_SELECT) continue;
                MemberSelectTree name = (MemberSelectTree) item.getQualifiedIdentifier();
                TreePath importPath = new TreePath(unitPath, item);
                boolean wildcard = name.getIdentifier().contentEquals("*");
                Tree subject = wildcard || item.isStatic() ? name.getExpression() : name;
                Element element = trees.getElement(new TreePath(new TreePath(importPath, name), subject));
                TreeSet<String> targets = new TreeSet<>();
                if (element instanceof PackageElement pkg) {
                    for (Element member : pkg.getEnclosedElements()) add(targets, member);
                } else {
                    add(targets, element);
                }
                long line = unit.getLineMap().getLineNumber(
                    trees.getSourcePositions().getStartPosition(unit, item));
                StringBuilder out = new StringBuilder("{\"file\":" + quote(file) + ",\"line\":" + line + ",\"targets\":[");
                String separator = "";
                for (String target : targets) {
                    out.append(separator).append(quote(target));
                    separator = ",";
                }
                System.out.println(out.append("]}"));
            }
        }
    }
}
