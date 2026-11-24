<?php
// Ground-truth PHP calls from PHP's own tokenizer (token_get_all).
//
// For every .php file under ROOT (or the files given), records the calls PHP
// resolves by name, at the line of the callee's name, with the declaration
// they reach, in the call truth format of tools/ground_truth.py: file, line,
// name, target_file, target_line, kind.
//
// - f(), A\f(), \A\f(), namespace\f(): PHP's name resolution (namespace and
//   `use function` imports; an unqualified name falls back to the global
//   function when the namespace has none).
// - new C: the class C and its constructor, own, from a trait or inherited.
// - C::m(), self::m(), static::m(), parent::m(): the method m that C (the
//   enclosing class for self and static, its parent for parent) declares,
//   uses from a trait or inherits. static is read as the enclosing class.
// - $this->m() and $this?->m(): the same lookup from the enclosing class.
//
// A target is the line of the declaration's name, with kind `callable`. A
// function or class that PHP itself provides (function_exists, class_exists
// on a name the repository does not declare) has the target `<external>`,
// kind `external`: linking it to a repository declaration is contradicted.
// Calls whose target the repository declares more than once, names declared
// only outside the repository (Composer dependencies), methods the class
// hierarchy does not declare (__call), calls on other expressions, `$this`
// in a trait method calling a method the trait does not declare, and calls in
// anonymous classes are left out, so they are unjudged. A file that is not
// valid PHP is listed on stderr and not indexed.
//
// Usage: php tools/php_call_truth.php ROOT [FILE ...] > calls.tsv

const NAMES = [T_STRING, T_NAME_QUALIFIED, T_NAME_FULLY_QUALIFIED, T_NAME_RELATIVE];
const SKIP = [T_WHITESPACE, T_COMMENT, T_DOC_COMMENT];

function code(array $tokens): array {
    // Significant tokens only, as [id, text, line]; one-character tokens get id 0.
    $out = [];
    foreach ($tokens as $t) {
        if (is_array($t)) {
            if (!in_array($t[0], SKIP, true)) {
                $out[] = [$t[0], $t[1], $t[2]];
            }
        } else {
            $out[] = [0, $t, null];
        }
    }
    $line = 1;
    foreach ($out as &$t) {
        if ($t[2] === null) {
            $t[2] = $line;
        }
        $line = $t[2];
    }
    return $out;
}

function lower(string $name): string {
    return strtolower(ltrim($name, '\\'));
}

// Resolve a class name as PHP does at compile time.
function class_name(string $written, string $namespace, array $uses): string {
    if ($written[0] === '\\') {
        return lower($written);
    }
    if (stripos($written, 'namespace\\') === 0) {
        return lower(($namespace === '' ? '' : $namespace . '\\') . substr($written, 10));
    }
    $parts = explode('\\', $written);
    $first = strtolower($parts[0]);
    if (isset($uses['class'][$first])) {
        $parts[0] = $uses['class'][$first];
        return lower(implode('\\', $parts));
    }
    return lower(($namespace === '' ? '' : $namespace . '\\') . $written);
}

// Candidate function names, most specific first.
function function_names(string $written, string $namespace, array $uses): array {
    if ($written[0] === '\\') {
        return [lower($written)];
    }
    if (stripos($written, 'namespace\\') === 0) {
        return [lower(($namespace === '' ? '' : $namespace . '\\') . substr($written, 10))];
    }
    if (strpos($written, '\\') !== false) {
        return [class_name($written, $namespace, $uses)];
    }
    $key = strtolower($written);
    if (isset($uses['function'][$key])) {
        return [lower($uses['function'][$key])];
    }
    return $namespace === ''
        ? [$key]
        : [lower($namespace . '\\' . $written), $key];
}

// Walk one file: namespaces, imports, class-likes, functions, and calls with
// the context they are written in.
function scan(string $file, array $tokens): array {
    $t = code($tokens);
    $n = count($t);
    $namespace = '';
    $uses = ['class' => [], 'function' => []];
    $depth = 0;
    $classes = [];     // stack of [name|null, kind, body depth]
    $functions = [];   // stack of body depths of functions (methods included)
    $decls = ['classes' => [], 'functions' => []];
    $calls = [];
    $pendingClass = null;
    $pendingFunction = false;
    for ($i = 0; $i < $n; $i++) {
        [$id, $text, $line] = $t[$i];
        $prev = $i > 0 ? $t[$i - 1] : [0, '', 0];
        if ($id === T_NAMESPACE && in_array($t[$i + 1][0] ?? 0, [T_STRING, T_NAME_QUALIFIED], true)) {
            $namespace = $t[$i + 1][1];
            $uses = ['class' => [], 'function' => []];
            $i++;
            continue;
        }
        if ($id === T_NAMESPACE && ($t[$i + 1][1] ?? '') === '{') {
            $namespace = '';
            $uses = ['class' => [], 'function' => []];
            continue;
        }
        if ($id === T_USE && empty($classes) && empty($functions) && !$pendingFunction && $prev[1] !== ')') {
            // A top-level import: use [function|const] A\B [as C], ... ;
            $kind = 'class';
            $j = $i + 1;
            if ($t[$j][0] === T_FUNCTION) {
                $kind = 'function';
                $j++;
            } elseif ($t[$j][0] === T_CONST) {
                $kind = 'const';
                $j++;
            }
            $prefix = '';
            $group = false;
            for (; $j < $n && $t[$j][1] !== ';'; $j++) {
                if ($t[$j][1] === '{') {
                    $group = true;
                    continue;
                }
                if ($t[$j][1] === '}' || $t[$j][1] === ',') {
                    continue;
                }
                if (in_array($t[$j][0], NAMES, true)) {
                    $name = $t[$j][1];
                    if (!$group && ($t[$j + 1][1] ?? '') === '{') {
                        $prefix = rtrim($name, '\\') . '\\';
                        continue;
                    }
                    if (!$group && substr($name, -1) === '\\') {
                        $prefix = $name;
                        continue;
                    }
                    $entryKind = $kind;
                    if ($group && in_array($t[$j - 1][0], [T_FUNCTION, T_CONST], true)) {
                        $entryKind = $t[$j - 1][0] === T_FUNCTION ? 'function' : 'const';
                    }
                    $full = ltrim(($group ? $prefix : '') . $name, '\\');
                    $alias = substr($full, strrpos('\\' . $full, '\\'));
                    if (($t[$j + 1][0] ?? 0) === T_AS) {
                        $alias = $t[$j + 2][1];
                        $j += 2;
                    }
                    if ($entryKind !== 'const') {
                        $uses[$entryKind][strtolower($alias)] = $full;
                    }
                }
            }
            $i = $j;
            continue;
        }
        if (in_array($id, [T_CLASS, T_INTERFACE, T_TRAIT, T_ENUM], true)
            && !($id === T_CLASS && $prev[0] === T_DOUBLE_COLON)
            && !($id === T_ENUM && ($t[$i + 1][0] ?? 0) !== T_STRING)) {
            $anonymous = $id === T_CLASS && $prev[0] === T_NEW
                || ($id === T_CLASS && ($t[$i + 1][0] ?? 0) !== T_STRING);
            $name = $anonymous ? null : lower(($namespace === '' ? '' : $namespace . '\\') . $t[$i + 1][1]);
            $info = ['file' => $file, 'line' => $anonymous ? $line : $t[$i + 1][2],
                'kind' => $id, 'parent' => null, 'traits' => [], 'methods' => []];
            for ($j = $i + 1; $j < $n && $t[$j][1] !== '{'; $j++) {
                if ($t[$j][0] === T_EXTENDS && $id === T_CLASS) {
                    $info['parent'] = class_name($t[$j + 1][1], $namespace, $uses);
                }
            }
            $pendingClass = [$name, $info];
            continue;
        }
        if ($id === T_USE && !empty($classes) && $depth === end($classes)[2] && empty($functions)) {
            // Traits a class-like uses.
            [$cname] = end($classes);
            for ($j = $i + 1; $j < $n && !in_array($t[$j][1], [';', '{'], true); $j++) {
                if (in_array($t[$j][0], NAMES, true) && $cname !== null) {
                    $decls['classes'][$cname]['traits'][] = class_name($t[$j][1], $namespace, $uses);
                }
            }
            continue;
        }
        if ($id === T_FUNCTION) {
            $k = ($t[$i + 1][1] ?? '') === '&' ? $i + 2 : $i + 1;
            if (($t[$k][0] ?? 0) === T_STRING) {
                $fname = $t[$k][1];
                if (!empty($classes) && $depth === end($classes)[2] && empty($functions)) {
                    [$cname] = end($classes);
                    if ($cname !== null) {
                        $decls['classes'][$cname]['methods'][strtolower($fname)] = [$file, $t[$k][2]];
                    }
                } elseif (empty($classes) && empty($functions)) {
                    $full = lower(($namespace === '' ? '' : $namespace . '\\') . $fname);
                    $decls['functions'][$full][] = [$file, $t[$k][2]];
                }
                $i = $k;
            }
            $pendingFunction = true;
            continue;
        }
        if ($id === T_FN) {
            continue;
        }
        if ($text === ';' && $pendingFunction) {
            $pendingFunction = false; // abstract or interface method
            continue;
        }
        if ($text === '{' || $id === T_CURLY_OPEN || $id === T_DOLLAR_OPEN_CURLY_BRACES) {
            $depth++;
            if ($pendingClass !== null && $text === '{') {
                [$name, $info] = $pendingClass;
                if ($name !== null) {
                    if (isset($decls['classes'][$name])) {
                        $decls['duplicates'][$name] = true;
                    }
                    $decls['classes'][$name] = $info;
                }
                $classes[] = [$name, $info['kind'], $depth];
                $pendingClass = null;
            } elseif ($pendingFunction && $text === '{') {
                $functions[] = $depth;
                $pendingFunction = false;
            }
            continue;
        }
        if ($text === '}') {
            if (!empty($functions) && end($functions) === $depth) {
                array_pop($functions);
            } elseif (!empty($classes) && end($classes)[2] === $depth) {
                array_pop($classes);
            }
            $depth--;
            continue;
        }
        $context = [
            'namespace' => $namespace,
            'uses' => $uses,
            'class' => empty($classes) ? false : end($classes)[0],
            'classKind' => empty($classes) ? null : end($classes)[1],
        ];
        $next = $t[$i + 1] ?? [0, '', 0];
        // new C(...) / new C
        if ($id === T_NEW && (in_array($next[0], NAMES, true) || in_array(strtolower($next[1]), ['self', 'static', 'parent'], true))) {
            $calls[] = ['new', $next[1], $next[2], $context];
            $i++;
            continue;
        }
        // C::m(...)
        if (in_array($id, NAMES, true) || in_array(strtolower($text), ['self', 'static', 'parent'], true)) {
            if ($next[0] === T_DOUBLE_COLON && ($t[$i + 2][0] ?? 0) === T_STRING && ($t[$i + 3][1] ?? '') === '(') {
                $calls[] = ['static', $text, $t[$i + 2][2], $context, $t[$i + 2][1]];
                $i += 2;
                continue;
            }
        }
        // f(...)
        if (in_array($id, NAMES, true) && $next[1] === '('
            && !in_array($prev[0], [T_FUNCTION, T_NEW, T_OBJECT_OPERATOR, T_NULLSAFE_OBJECT_OPERATOR, T_DOUBLE_COLON, T_CONST], true)
            && $prev[1] !== '&') {
            $calls[] = ['function', $text, $line, $context];
            continue;
        }
        // $this->m(...)
        if ($id === T_VARIABLE && $text === '$this'
            && in_array($next[0], [T_OBJECT_OPERATOR, T_NULLSAFE_OBJECT_OPERATOR], true)
            && ($t[$i + 2][0] ?? 0) === T_STRING && ($t[$i + 3][1] ?? '') === '(') {
            $calls[] = ['this', $t[$i + 2][1], $t[$i + 2][2], $context];
            $i += 2;
            continue;
        }
    }
    return [$decls, $calls];
}

// The method `$method` as class `$class` has it: declared, from a trait, or
// inherited. [file, line] or null.
function find_method(array $classes, ?string $class, string $method, array $seen = []) {
    if ($class === null || !isset($classes[$class]) || isset($seen[$class])) {
        return null;
    }
    $seen[$class] = true;
    $info = $classes[$class];
    if (isset($info['methods'][$method])) {
        return $info['methods'][$method];
    }
    foreach ($info['traits'] as $trait) {
        $found = find_method($classes, $trait, $method, $seen);
        if ($found !== null) {
            return $found;
        }
    }
    return find_method($classes, $info['parent'], $method, $seen);
}

// Whether the hierarchy of $class reaches a class the repository does not
// declare (a dependency), so a missing method may live there.
function open_hierarchy(array $classes, ?string $class, array $seen = []): bool {
    if ($class === null) {
        return false;
    }
    if (!isset($classes[$class]) || isset($seen[$class])) {
        return true;
    }
    $seen[$class] = true;
    foreach (array_merge($classes[$class]['traits'], [$classes[$class]['parent']]) as $up) {
        if ($up !== null && open_hierarchy($classes, $up, $seen)) {
            return true;
        }
    }
    return false;
}

$root = realpath($argv[1]);
$files = array_slice($argv, 2);
if (!$files) {
    $iterator = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS));
    foreach ($iterator as $entry) {
        $path = $entry->getPathname();
        if (substr($path, -4) === '.php' && strpos($path, '/.git/') === false && strpos($path, '/vendor/') === false) {
            $files[] = substr($path, strlen($root) + 1);
        }
    }
}
sort($files);
$scanned = [];
$classes = [];
$functions = [];
$duplicates = [];
foreach ($files as $file) {
    $source = file_get_contents($root . '/' . $file);
    try {
        $tokens = token_get_all($source, TOKEN_PARSE);
    } catch (ParseError $error) {
        fwrite(STDERR, "$file: unparsed\n");
        continue;
    }
    [$decls, $calls] = scan($file, $tokens);
    foreach ($decls['classes'] as $name => $info) {
        if (isset($classes[$name])) {
            $duplicates[$name] = true;
        }
        $classes[$name] = $info;
    }
    foreach (array_keys($decls['duplicates'] ?? []) as $name) {
        $duplicates[$name] = true;
    }
    foreach ($decls['functions'] as $name => $places) {
        foreach ($places as $place) {
            $functions[$name][] = $place;
        }
    }
    $scanned[$file] = $calls;
}

$rows = [];
$drop = [];
foreach ($scanned as $file => $calls) {
    foreach ($calls as $call) {
        [$kind, $written, $line, $context] = $call;
        $target = null;
        $name = $written;
        if ($kind === 'function') {
            $name = substr($written, strrpos('\\' . $written, '\\'));
            foreach (function_names($written, $context['namespace'], $context['uses']) as $candidate) {
                if (isset($functions[$candidate])) {
                    $target = count($functions[$candidate]) === 1 ? [[...$functions[$candidate][0], 'callable']] : false;
                    break;
                }
                if (function_exists($candidate) && (new ReflectionFunction($candidate))->isInternal()) {
                    $target = [['<external>', 0, 'external']];
                    break;
                }
            }
        } else {
            $own = $context['class'];
            $lowered = strtolower($written);
            if (in_array($lowered, ['self', 'static'], true)) {
                $class = $own === false ? null : $own;
            } elseif ($lowered === 'parent') {
                $class = ($own !== false && $own !== null && isset($classes[$own])) ? $classes[$own]['parent'] : null;
            } elseif ($kind === 'this') {
                $class = ($own === false || $context['classKind'] === T_TRAIT) ? null : $own;
            } else {
                $class = class_name($written, $context['namespace'], $context['uses']);
            }
            if ($kind === 'this' && $own !== false && $own !== null && $context['classKind'] === T_TRAIT) {
                // In a trait, $this is the using class: judge only the
                // trait's own methods.
                $method = strtolower($written);
                if (isset($classes[$own]['methods'][$method])) {
                    $target = [[...$classes[$own]['methods'][$method], 'callable']];
                }
                $name = $written;
            } elseif ($class !== null && isset($duplicates[$class])) {
                $target = false;
            } elseif ($kind === 'new') {
                $name = substr($written, strrpos('\\' . $written, '\\'));
                if ($class !== null && isset($classes[$class])) {
                    $target = [[$classes[$class]['file'], $classes[$class]['line'], 'callable']];
                    $constructor = find_method($classes, $class, '__construct');
                    if ($constructor !== null) {
                        $target[] = [...$constructor, 'callable'];
                    } elseif (open_hierarchy($classes, $class)) {
                        $target = false; // the constructor may come from a dependency
                    }
                } elseif ($class !== null && class_exists($class, false) && (new ReflectionClass($class))->isInternal()) {
                    $target = [['<external>', 0, 'external']];
                }
            } else {
                $method = $kind === 'static' ? $call[4] : $written;
                $name = $method;
                $found = find_method($classes, $class, strtolower($method));
                if ($found !== null) {
                    $target = [[...$found, 'callable']];
                }
            }
        }
        $key = "$file\t$line\t$name";
        if ($target === false) {
            $drop[$key] = true;
        } elseif ($target !== null) {
            $set = array_map(fn($one) => implode("\t", $one), $target);
            sort($set);
            // Two calls of one name on one line that resolve differently
            // cannot be told apart by the scorer: leave them out.
            if (isset($rows[$key]) && $rows[$key] !== $set) {
                $drop[$key] = true;
            }
            $rows[$key] = $set;
        }
    }
}
echo "# file\tline\tname\ttarget_file\ttarget_line\tkind\n";
foreach (array_keys($scanned) as $file) {
    echo "# indexed\t$file\n";
}
ksort($rows);
foreach ($rows as $key => $targets) {
    if (isset($drop[$key])) {
        continue;
    }
    foreach ($targets as $target) {
        echo "$key\t$target\n";
    }
}
