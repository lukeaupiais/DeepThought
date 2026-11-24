// Ground-truth Java calls for tools/ground_truth.py, from javac's attribution.
//
// Parses, enters and attributes the .java files under ROOT with every
// SOURCEPATH_ROOT on the source path (nothing is written). For each method
// invocation and `new` expression, prints one tab-separated row in the
// ground-truth format: the file, line and name of the called name token (the
// method name, or the simple name of the class created), then where javac
// binds it: the file and name line of a declaration in the repository with
// kind `callable`, or `-  0  external` for a declaration outside it. A call
// javac cannot bind (a missing library) is not printed. A `new` expression
// binds its constructor and its class (only the class when no constructor is
// declared), as a constructor in a SCIP index does.
//
// With --bases first, prints instead one row per extends or implements entry
// of a class, interface, enum or record: the type's file, name line and name,
// then the bound supertype with kind `type` (or `external`).
//
// With --chains first, prints instead only the explicit constructor
// invocations, `this(...)` and `super(...)`, bound to a constructor of the
// repository: the row's name is `this` or `super`. Call sites leave them out
// (they name no declaration), but a constructor that delegates depends on the
// one it calls, which impact truth needs.
//
// Usage:
//   java --add-exports jdk.compiler/com.sun.tools.javac.tree=ALL-UNNAMED \
//     --add-exports jdk.compiler/com.sun.tools.javac.util=ALL-UNNAMED \
//     --add-exports jdk.compiler/com.sun.tools.javac.code=ALL-UNNAMED \
//     tools/JavaCallTruth.java [--bases | --chains] REPOSITORY ROOT [SOURCEPATH_ROOT ...]

import com.sun.source.tree.ClassTree;
import com.sun.source.tree.CompilationUnitTree;
import com.sun.source.tree.ExpressionTree;
import com.sun.source.tree.IdentifierTree;
import com.sun.source.tree.LineMap;
import com.sun.source.tree.MemberSelectTree;
import com.sun.source.tree.MethodInvocationTree;
import com.sun.source.tree.NewClassTree;
import com.sun.source.tree.ParameterizedTypeTree;
import com.sun.source.tree.Tree;
import com.sun.source.util.JavacTask;
import com.sun.source.util.SourcePositions;
import com.sun.source.util.TreePath;
import com.sun.source.util.TreePathScanner;
import com.sun.source.util.Trees;
import com.sun.tools.javac.code.Symbol;
import com.sun.tools.javac.tree.JCTree;
import com.sun.tools.javac.tree.TreeInfo;
import java.io.File;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Stream;
import javax.lang.model.element.Element;
import javax.lang.model.element.ElementKind;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;

public class JavaCallTruth {
    static Path repository;
    static Trees trees;

    // "file\tline\tkind" of the declaration's name, or null when unbound.
    static String target(Element element) {
        if (element == null || element.asType() == null
                || element.asType().getKind() == javax.lang.model.type.TypeKind.ERROR) {
            return null;
        }
        if (element.getKind() == ElementKind.CONSTRUCTOR) {
            TreePath declared = trees.getPath(element);
            if (declared == null) {
                // An implicit constructor: the call creates its class.
                element = element.getEnclosingElement();
            }
        }
        TreePath path = trees.getPath(element);
        if (path == null) {
            return "-\t0\texternal";
        }
        CompilationUnitTree unit = path.getCompilationUnit();
        if (unit.getSourceFile().getKind() != JavaFileObject.Kind.SOURCE) {
            return "-\t0\texternal";
        }
        Path file = Path.of(unit.getSourceFile().getName()).toAbsolutePath().normalize();
        if (!file.startsWith(repository)) {
            return "-\t0\texternal";
        }
        // The name's position within the declaration itself.
        int position = TreeInfo.diagnosticPositionFor((Symbol) element, (JCTree) path.getLeaf())
            .getPreferredPosition();
        return repository.relativize(file).toString().replace('\\', '/') + "\t"
            + unit.getLineMap().getLineNumber(position) + "\tcallable";
    }

    public static void main(String[] arguments) throws Exception {
        boolean bases = arguments.length > 0 && arguments[0].equals("--bases");
        boolean chains = arguments.length > 0 && arguments[0].equals("--chains");
        String[] args = bases || chains
            ? java.util.Arrays.copyOfRange(arguments, 1, arguments.length) : arguments;
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
                void emit(Tree name, String text, Element element) {
                    String bound;
                    try {
                        bound = target(element);
                    } catch (RuntimeException e) {
                        bound = null;
                    }
                    long end = positions.getEndPosition(unit, name);
                    boolean chain = text.equals("super") || text.equals("this");
                    // super(...) and this(...) name no declaration: printed
                    // only with --chains, and then nothing else is.
                    if (bound == null || end < 0 || chain != chains) return;
                    long line = lines.getLineNumber(end - text.length());
                    if (chain) {
                        if (!bound.endsWith("\tcallable")) return;
                        System.out.println(file + "\t" + line + "\t" + text + "\t" + bound);
                        return;
                    }
                    System.out.println(file + "\t" + line + "\t" + text + "\t" + bound);
                    if (element != null && element.getKind() == ElementKind.CONSTRUCTOR) {
                        // A call that names a class is a construction of it.
                        String type = target(element.getEnclosingElement());
                        if (type != null && !type.equals(bound)) {
                            System.out.println(file + "\t" + line + "\t" + text + "\t" + type);
                        }
                    }
                }

                // The tree whose last token is the called name.
                void name(ExpressionTree tree, Element element) {
                    if (tree instanceof ParameterizedTypeTree parameterized) {
                        tree = (ExpressionTree) parameterized.getType();
                    }
                    if (tree instanceof IdentifierTree identifier) {
                        emit(tree, identifier.getName().toString(), element);
                    } else if (tree instanceof MemberSelectTree select) {
                        emit(tree, select.getIdentifier().toString(), element);
                    }
                }

                @Override
                public Void visitClass(ClassTree node, Void unused) {
                    if (bases && !node.getSimpleName().isEmpty()) {
                        Element type = trees.getElement(getCurrentPath());
                        String self = type == null ? null : target(type);
                        List<Tree> entries = new ArrayList<>();
                        if (node.getExtendsClause() != null) entries.add(node.getExtendsClause());
                        entries.addAll(node.getImplementsClause());
                        for (Tree entry : entries) {
                            Tree named = entry instanceof ParameterizedTypeTree p ? p.getType() : entry;
                            String bound;
                            try {
                                bound = target(trees.getElement(new TreePath(getCurrentPath(), named)));
                            } catch (RuntimeException e) {
                                bound = null;
                            }
                            if (self == null || bound == null || self.startsWith("-")) continue;
                            String[] at = self.split("\t");
                            System.out.println(at[0] + "\t" + at[1] + "\t" + node.getSimpleName()
                                + "\t" + bound.replace("\tcallable", "\ttype"));
                        }
                    }
                    return super.visitClass(node, unused);
                }

                @Override
                public Void visitMethodInvocation(MethodInvocationTree node, Void unused) {
                    if (bases) return super.visitMethodInvocation(node, unused);
                    name(node.getMethodSelect(), trees.getElement(getCurrentPath()));
                    return super.visitMethodInvocation(node, unused);
                }

                @Override
                public Void visitNewClass(NewClassTree node, Void unused) {
                    if (!bases && !chains && node.getClassBody() == null) {
                        name(node.getIdentifier(), trees.getElement(getCurrentPath()));
                    }
                    return super.visitNewClass(node, unused);
                }
            }.scan(unit, null);
        }
    }
}
