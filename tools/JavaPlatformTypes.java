// Every supertype of each public type in a fixed set of JDK packages, from the
// running JDK's own classes (nothing outside the JDK is loaded), as JSON: each
// type's canonical name mapped to the simple names of the type and of every
// class and interface it extends or implements, Object included. The call
// resolver reads src/deepthought/java_platform.json, this tool's output, to
// know that a java.io.Writer is an Appendable and a java.io.Closeable is not
// when it chooses among overloads (docs/graph-port.md).
//
// Usage: java tools/JavaPlatformTypes.java > src/deepthought/java_platform.json

import java.lang.reflect.Modifier;
import java.net.URI;
import java.nio.file.FileSystem;
import java.nio.file.FileSystems;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.stream.Stream;

public class JavaPlatformTypes {
    // Package -> module.
    static final Map<String, String> PACKAGES = new TreeMap<>(Map.ofEntries(
            Map.entry("java.io", "java.base"),
            Map.entry("java.lang", "java.base"),
            Map.entry("java.lang.annotation", "java.base"),
            Map.entry("java.lang.reflect", "java.base"),
            Map.entry("java.math", "java.base"),
            Map.entry("java.net", "java.base"),
            Map.entry("java.nio", "java.base"),
            Map.entry("java.nio.charset", "java.base"),
            Map.entry("java.nio.file", "java.base"),
            Map.entry("java.sql", "java.sql"),
            Map.entry("java.text", "java.base"),
            Map.entry("java.time", "java.base"),
            Map.entry("java.time.format", "java.base"),
            Map.entry("java.time.temporal", "java.base"),
            Map.entry("java.util", "java.base"),
            Map.entry("java.util.concurrent", "java.base"),
            Map.entry("java.util.concurrent.atomic", "java.base"),
            Map.entry("java.util.concurrent.locks", "java.base"),
            Map.entry("java.util.function", "java.base"),
            Map.entry("java.util.regex", "java.base"),
            Map.entry("java.util.stream", "java.base")));

    public static void main(String[] args) throws Exception {
        FileSystem jrt = FileSystems.getFileSystem(URI.create("jrt:/"));
        Map<String, TreeSet<String>> found = new TreeMap<>();
        for (Map.Entry<String, String> entry : PACKAGES.entrySet()) {
            Path dir = jrt.getPath("modules", entry.getValue(),
                    entry.getKey().replace('.', '/'));
            List<Path> files;
            try (Stream<Path> listed = Files.list(dir)) {
                files = listed.sorted().toList();
            }
            for (Path file : files) {
                String name = file.getFileName().toString();
                if (!name.endsWith(".class") || name.equals("module-info.class")
                        || name.equals("package-info.class")) {
                    continue;
                }
                Class<?> type = Class.forName(
                        entry.getKey() + "." + name.substring(0, name.length() - 6),
                        false, ClassLoader.getSystemClassLoader());
                if (!visible(type)) {
                    continue;
                }
                TreeSet<String> names = new TreeSet<>();
                names.add("Object");
                Deque<Class<?>> stack = new ArrayDeque<>(List.of(type));
                while (!stack.isEmpty()) {
                    Class<?> current = stack.pop();
                    names.add(current.getSimpleName());
                    if (current.getSuperclass() != null) {
                        stack.push(current.getSuperclass());
                    }
                    for (Class<?> base : current.getInterfaces()) {
                        stack.push(base);
                    }
                }
                found.put(type.getCanonicalName(), names);
            }
        }
        // JSON, one type per line, keys sorted.
        StringBuilder out = new StringBuilder();
        out.append("{\n  \"jdk\": ").append(Runtime.version().feature())
                .append(",\n  \"packages\": [");
        for (String p : PACKAGES.keySet()) {
            out.append('"').append(p).append("\", ");
        }
        out.setLength(out.length() - 2);
        out.append("],\n  \"supertypes\": {\n");
        for (Map.Entry<String, TreeSet<String>> e : found.entrySet()) {
            out.append("    \"").append(e.getKey()).append("\": [");
            for (String n : e.getValue()) {
                out.append('"').append(n).append("\", ");
            }
            out.setLength(out.length() - 2);
            out.append("],\n");
        }
        out.setLength(out.length() - 2);
        out.append("\n  }\n}\n");
        System.out.print(out);
    }

    // A public type that source code can name: top level, or a member of one.
    static boolean visible(Class<?> type) {
        for (Class<?> c = type; c != null; c = c.getDeclaringClass()) {
            if (!Modifier.isPublic(c.getModifiers()) || c.isAnonymousClass()
                    || c.isLocalClass() || c.isSynthetic()) {
                return false;
            }
        }
        return type.getCanonicalName() != null;
    }
}
