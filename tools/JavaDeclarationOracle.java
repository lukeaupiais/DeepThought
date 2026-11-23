// Lists Java declarations as javac's own parser sees them.
//
// For every class, interface, enum, record, annotation type, method,
// constructor and annotation element, prints one JSON line with the file, the
// lexically qualified name (enclosing named declarations joined by '.'), the
// kind, and the start and end of the declaration as UTF-16 character offsets.
// Only parsing is used; nothing is compiled or resolved.
//
// Usage: java tools/JavaDeclarationOracle.java ROOT > declarations.jsonl

import com.sun.source.tree.ClassTree;
import com.sun.source.tree.CompilationUnitTree;
import com.sun.source.tree.MethodTree;
import com.sun.source.tree.Tree;
import com.sun.source.util.JavacTask;
import com.sun.source.util.SourcePositions;
import com.sun.source.util.TreeScanner;
import com.sun.source.util.Trees;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.List;
import java.util.stream.Stream;
import javax.lang.model.element.Modifier;
import javax.tools.JavaCompiler;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;

public class JavaDeclarationOracle {
    static String quote(String text) {
        StringBuilder out = new StringBuilder("\"");
        for (char c : text.toCharArray()) {
            if (c == '"' || c == '\\') out.append('\\').append(c);
            else if (c < 0x20) out.append(String.format("\\u%04x", (int) c));
            else out.append(c);
        }
        return out.append('"').toString();
    }

    public static void main(String[] args) throws Exception {
        Path root = Path.of(args[0]).toAbsolutePath().normalize();
        List<Path> files;
        try (Stream<Path> walk = Files.walk(root)) {
            files = walk.filter(p -> p.toString().endsWith(".java") && Files.isRegularFile(p))
                .sorted().toList();
        }
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        StandardJavaFileManager manager = compiler.getStandardFileManager(null, null, null);
        for (Path file : files) {
            JavacTask task = (JavacTask) compiler.getTask(
                null, manager, d -> {}, List.of("-proc:none", "--enable-preview", "--release",
                String.valueOf(Runtime.version().feature())), null,
                manager.getJavaFileObjects(file));
            SourcePositions positions = Trees.instance(task).getSourcePositions();
            String relative = root.relativize(file).toString().replace('\\', '/');
            for (CompilationUnitTree unit : task.parse()) {
                new TreeScanner<Void, Void>() {
                    final Deque<String> scope = new ArrayDeque<>();
                    final Deque<String> owners = new ArrayDeque<>();

                    void emit(Tree node, String name, String kind) {
                        String symbol = scope.isEmpty() ? name
                            : String.join(".", (Iterable<String>) () -> scope.descendingIterator()) + "." + name;
                        System.out.println("{\"file\":" + quote(relative) + ",\"symbol\":" + quote(symbol)
                            + ",\"kind\":" + quote(kind)
                            + ",\"start\":" + positions.getStartPosition(unit, node)
                            + ",\"end\":" + positions.getEndPosition(unit, node) + "}");
                    }

                    @Override
                    public Void visitClass(ClassTree node, Void unused) {
                        String name = node.getSimpleName().toString();
                        if (name.isEmpty()) {
                            // Anonymous classes are bodies, not named declarations.
                            owners.push("");
                            super.visitClass(node, null);
                            owners.pop();
                            return null;
                        }
                        String kind = switch (node.getKind()) {
                            case INTERFACE -> "Interface";
                            case ENUM -> "Enum";
                            case RECORD -> "Record";
                            case ANNOTATION_TYPE -> "Annotation";
                            default -> "Class";
                        };
                        emit(node, name, kind);
                        scope.push(name);
                        owners.push(name);
                        super.visitClass(node, null);
                        owners.pop();
                        scope.pop();
                        return null;
                    }

                    @Override
                    public Void visitMethod(MethodTree node, Void unused) {
                        String name = node.getName().toString();
                        if (positions.getStartPosition(unit, node) < 0) {
                            return null;  // Generated, such as a record's canonical constructor.
                        }
                        boolean constructor = name.equals("<init>");
                        if (constructor) name = owners.peek();
                        emit(node, name, constructor ? "Constructor" : "Method");
                        scope.push(name);
                        super.visitMethod(node, null);
                        scope.pop();
                        return null;
                    }
                }.scan(unit, null);
            }
        }
    }
}
