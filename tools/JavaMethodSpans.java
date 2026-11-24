// The span of every method and constructor declared in the .java files under
// ROOT, from javac's parser (nothing is attributed or written): one
// tab-separated row per declaration with the file (relative to ROOT), the line
// of its name, its first line (annotations and modifiers included), its last
// line and its name (a constructor is named by its class). tools/impact_truth.py
// reads these as the callables of a corpus whose call truth comes from javac
// (tools/JavaCallTruth.java) rather than a SCIP index.
//
// Usage: java tools/JavaMethodSpans.java ROOT > spans.tsv
//
// With --initializers it writes, instead, one row per field initializer and
// initializer block of a named class: the file, the line of the class's name,
// the member's first and last line, and the class's name. That code runs when
// the class is constructed or loaded, so impact_truth.py makes the class the
// caller of the calls in it (javac compiles instance initializers into every
// constructor, and a class without one has no other callable to hold them).
//
// Usage: java tools/JavaMethodSpans.java ROOT --initializers > init.tsv

import com.sun.source.tree.BlockTree;
import com.sun.source.tree.ClassTree;
import com.sun.source.tree.CompilationUnitTree;
import com.sun.source.tree.LineMap;
import com.sun.source.tree.MethodTree;
import com.sun.source.tree.Tree;
import com.sun.source.tree.VariableTree;
import com.sun.source.util.JavacTask;
import com.sun.source.util.SourcePositions;
import com.sun.source.util.TreePathScanner;
import com.sun.source.util.Trees;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Deque;
import java.util.List;
import java.util.stream.Stream;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;

public class JavaMethodSpans {
    public static void main(String[] args) throws Exception {
        Path root = Path.of(args[0]).toAbsolutePath().normalize();
        boolean initializers = args.length > 1 && args[1].equals("--initializers");
        List<Path> paths;
        try (Stream<Path> walk = Files.walk(root)) {
            paths = walk.filter(p -> p.toString().endsWith(".java"))
                    .filter(p -> !p.toString().contains("/.git/"))
                    .sorted().toList();
        }
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        StandardJavaFileManager files = compiler.getStandardFileManager(null, null, null);
        Iterable<? extends JavaFileObject> units = files.getJavaFileObjectsFromPaths(paths);
        JavacTask task = (JavacTask) compiler.getTask(
                null, files, diagnostic -> {}, List.of("-proc:none"), null, units);
        Trees trees = Trees.instance(task);
        SourcePositions positions = trees.getSourcePositions();
        List<String> rows = new ArrayList<>();
        for (CompilationUnitTree unit : task.parse()) {
            String file = root.relativize(Path.of(unit.getSourceFile().toUri())).toString();
            LineMap lines = unit.getLineMap();
            Deque<String> classes = new ArrayDeque<>();
            new TreePathScanner<Void, Void>() {
                @Override
                public Void visitClass(ClassTree tree, Void unused) {
                    String name = tree.getSimpleName().toString();
                    if (initializers && !name.isEmpty()) {
                        int nameLine = nameLine(tree, name);
                        for (Tree member : tree.getMembers()) {
                            boolean runs = member instanceof BlockTree
                                    || (member instanceof VariableTree v
                                            && v.getInitializer() != null);
                            long start = positions.getStartPosition(unit, member);
                            long end = positions.getEndPosition(unit, member);
                            if (runs && start >= 0 && end >= 0) {
                                rows.add(file + "\t" + nameLine + "\t"
                                        + lines.getLineNumber(start) + "\t"
                                        + lines.getLineNumber(end) + "\t" + name);
                            }
                        }
                    }
                    classes.push(name);
                    try {
                        return super.visitClass(tree, unused);
                    } finally {
                        classes.pop();
                    }
                }

                // The line of the class's name: javac's position for a class
                // is its keyword, so look for the name from there.
                private int nameLine(ClassTree tree, String name) {
                    int from = ((com.sun.tools.javac.tree.JCTree) tree).pos;
                    String source;
                    try {
                        source = unit.getSourceFile().getCharContent(true).toString();
                    } catch (java.io.IOException e) {
                        throw new java.io.UncheckedIOException(e);
                    }
                    java.util.regex.Matcher matcher = java.util.regex.Pattern
                            .compile("\\b" + java.util.regex.Pattern.quote(name) + "\\b")
                            .matcher(source);
                    return (int) lines.getLineNumber(matcher.find(from) ? matcher.start() : from);
                }

                @Override
                public Void visitMethod(MethodTree tree, Void unused) {
                    if (initializers) {
                        return super.visitMethod(tree, unused);
                    }
                    long start = positions.getStartPosition(unit, tree);
                    long end = positions.getEndPosition(unit, tree);
                    int nameAt = ((com.sun.tools.javac.tree.JCTree) tree).pos;
                    String name = tree.getName().contentEquals("<init>")
                            ? classes.peek() : tree.getName().toString();
                    if (start >= 0 && end >= 0 && name != null && !name.isEmpty()) {
                        rows.add(file + "\t" + lines.getLineNumber(nameAt) + "\t"
                                + lines.getLineNumber(start) + "\t"
                                + lines.getLineNumber(end) + "\t" + name);
                    }
                    return super.visitMethod(tree, unused);
                }
            }.scan(unit, null);
        }
        Collections.sort(rows);
        System.out.println(initializers
                ? "# file\tclass_name_line\tfirst_line\tlast_line\tclass"
                : "# file\tname_line\tfirst_line\tlast_line\tname");
        rows.forEach(System.out::println);
    }
}
