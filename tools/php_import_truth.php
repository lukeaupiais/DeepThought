<?php
// Ground-truth PHP `use` imports from PHP's own tokenizer and Composer.
//
// For every .php file under ROOT that PHP parses (token_get_all with
// TOKEN_PARSE), prints one row per name a top-level `use` imports, in the
// module-reference truth format of tools/ground_truth.py: file, line,
// `import`, target_file, 0, kind. Trait uses inside a class-like body and
// closure `use (...)` are not imports.
//
// The target of a class-like name is the file Composer's class map gives it
// (CLASSMAP: the autoload_classmap.php that `composer dump-autoload
// --optimize --no-scripts --no-plugins` writes), else the files whose
// tokens declare it. A `use function` targets the files declaring that
// function. A name no file declares, as a class or as a namespace holding
// one, is `external`. `use const` is not listed: constants are not
// declarations. Names compare ignoring ASCII case, as PHP does.
//
// Files under vendor/ and node_modules/ and dot directories are skipped.
//
// Usage: php tools/php_import_truth.php ROOT [CLASSMAP] > imports.tsv

function significant(array $tokens, int $i, int $step): ?int {
    for ($j = $i + $step; $j >= 0 && $j < count($tokens); $j += $step) {
        $t = $tokens[$j];
        if (is_array($t) && in_array($t[0], [T_WHITESPACE, T_COMMENT, T_DOC_COMMENT], true)) {
            continue;
        }
        return $j;
    }
    return null;
}

function php_files(string $root): array {
    $found = [];
    $it = new RecursiveIteratorIterator(
        new RecursiveCallbackFilterIterator(
            new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS),
            function ($file) {
                $name = $file->getFilename();
                if ($file->isDir()) {
                    return $name[0] !== '.' && $name !== 'vendor' && $name !== 'node_modules';
                }
                return str_ends_with($name, '.php');
            }
        )
    );
    foreach ($it as $file) {
        $found[] = substr($file->getPathname(), strlen(rtrim($root, '/')) + 1);
    }
    sort($found);
    return $found;
}

$NAME = [T_STRING, T_NAME_QUALIFIED, T_NAME_FULLY_QUALIFIED];
$TYPES = [T_CLASS, T_INTERFACE, T_TRAIT, T_ENUM];

$root = rtrim($argv[1], '/');
$classmap = [];
if (isset($argv[2])) {
    foreach (require $argv[2] as $class => $path) {
        $real = realpath($path);
        if ($real !== false && str_starts_with($real, realpath($root) . '/')) {
            $classmap[strtolower($class)] = substr($real, strlen(realpath($root)) + 1);
        }
    }
}

$tokens_of = [];
$types = [];      // lowercased class-like name => [file => true]
$functions = [];  // lowercased function name => [file => true]
$namespaces = []; // lowercased namespace => [file => true]
foreach (php_files($root) as $file) {
    try {
        $tokens = token_get_all(file_get_contents("$root/$file"), TOKEN_PARSE);
    } catch (Throwable $e) {
        fwrite(STDERR, "unparsed\t$file\n");
        continue;
    }
    $tokens_of[$file] = $tokens;
    $namespace = '';
    $depth = 0;
    $class_depths = [];
    $pending_class = false;
    for ($i = 0; $i < count($tokens); $i++) {
        $t = $tokens[$i];
        if ($t === '{' || (is_array($t) && in_array($t[0], [T_CURLY_OPEN, T_DOLLAR_OPEN_CURLY_BRACES], true))) {
            $depth++;
            if ($pending_class) {
                $class_depths[] = $depth;
                $pending_class = false;
            }
        } elseif ($t === '}') {
            if ($class_depths && end($class_depths) === $depth) {
                array_pop($class_depths);
            }
            $depth--;
        } elseif (is_array($t) && $t[0] === T_NAMESPACE) {
            $j = significant($tokens, $i, 1);
            $namespace = ($j !== null && is_array($tokens[$j]) && in_array($tokens[$j][0], $NAME, true))
                ? $tokens[$j][1] : '';
        } elseif (is_array($t) && in_array($t[0], $TYPES, true)) {
            $prev = significant($tokens, $i, -1);
            if ($prev !== null && is_array($tokens[$prev]) && $tokens[$prev][0] === T_DOUBLE_COLON) {
                continue; // X::class
            }
            $pending_class = true;
            $j = significant($tokens, $i, 1);
            if ($j !== null && is_array($tokens[$j]) && $tokens[$j][0] === T_STRING) {
                $full = strtolower(ltrim("$namespace\\" . $tokens[$j][1], '\\'));
                $types[$full][$file] = true;
                $parts = explode('\\', $full);
                for ($k = 1; $k < count($parts); $k++) {
                    $namespaces[implode('\\', array_slice($parts, 0, $k))][$file] = true;
                }
            }
        } elseif (is_array($t) && $t[0] === T_FUNCTION && !$class_depths) {
            $j = significant($tokens, $i, 1);
            if ($j !== null && $tokens[$j] === '&') {
                $j = significant($tokens, $j, 1);
            }
            if ($j !== null && is_array($tokens[$j]) && $tokens[$j][0] === T_STRING) {
                $functions[strtolower(ltrim("$namespace\\" . $tokens[$j][1], '\\'))][$file] = true;
            }
        }
    }
}

function targets(string $name, string $kind, array $classmap, array $types, array $functions, array $namespaces): array {
    $key = strtolower(ltrim($name, '\\'));
    if ($kind === 'function') {
        return array_keys($functions[$key] ?? []);
    }
    if (isset($classmap[$key])) {
        return [$classmap[$key]];
    }
    return array_keys($types[$key] ?? $namespaces[$key] ?? []);
}

foreach ($tokens_of as $file => $tokens) {
    echo "# indexed\t$file\n";
}
foreach ($tokens_of as $file => $tokens) {
    $depth = 0;
    $namespace_depths = [];
    $pending_namespace = false;
    for ($i = 0; $i < count($tokens); $i++) {
        $t = $tokens[$i];
        if ($t === '{' || (is_array($t) && in_array($t[0], [T_CURLY_OPEN, T_DOLLAR_OPEN_CURLY_BRACES], true))) {
            $depth++;
            if ($pending_namespace) {
                $namespace_depths[] = $depth;
                $pending_namespace = false;
            }
            continue;
        }
        if ($t === '}') {
            if ($namespace_depths && end($namespace_depths) === $depth) {
                array_pop($namespace_depths);
            }
            $depth--;
            continue;
        }
        if ($t === ';') {
            $pending_namespace = false;
        }
        if (is_array($t) && $t[0] === T_NAMESPACE) {
            $pending_namespace = true;
            continue;
        }
        // An import is a `use` outside every brace but a namespace's.
        if (!is_array($t) || $t[0] !== T_USE || $depth !== count($namespace_depths)) {
            continue;
        }
        $prev = significant($tokens, $i, -1);
        if ($prev !== null && $tokens[$prev] === ')') {
            continue;
        }
        $kind = 'class';
        $j = significant($tokens, $i, 1);
        if (is_array($tokens[$j]) && $tokens[$j][0] === T_FUNCTION) {
            $kind = 'function';
            $j = significant($tokens, $j, 1);
        } elseif (is_array($tokens[$j]) && $tokens[$j][0] === T_CONST) {
            $kind = 'const';
            $j = significant($tokens, $j, 1);
        }
        $prefix = '';
        $names = [];
        $clause_kind = $kind;
        $expect_name = true;
        for (; $j !== null && $tokens[$j] !== ';'; $j = significant($tokens, $j, 1)) {
            $u = $tokens[$j];
            if (is_array($u) && $u[0] === T_NS_SEPARATOR) {
                continue;
            }
            if ($u === '{') {
                // A group: the name read so far is the prefix of each member.
                $prefix = $last . '\\';
                array_pop($names);
                $expect_name = true;
                continue;
            }
            if ($u === ',' || $u === '}') {
                $expect_name = true;
                $clause_kind = $kind;
                continue;
            }
            if (is_array($u) && $u[0] === T_AS) {
                $expect_name = false;
                $j = significant($tokens, $j, 1); // the alias
                continue;
            }
            if (is_array($u) && $u[0] === T_FUNCTION) {
                $clause_kind = 'function';
                continue;
            }
            if (is_array($u) && $u[0] === T_CONST) {
                $clause_kind = 'const';
                continue;
            }
            if ($expect_name && is_array($u) && in_array($u[0], $NAME, true)) {
                $last = $u[1];
                $names[] = [$prefix . $u[1], $u[2], $clause_kind];
                $expect_name = false;
            }
        }
        foreach ($names as [$name, $line, $name_kind]) {
            if ($name_kind === 'const') {
                continue;
            }
            $found = targets($name, $name_kind, $classmap, $types, $functions, $namespaces);
            if (!$found) {
                echo "$file\t$line\timport\t-\t0\texternal\n";
            }
            sort($found);
            foreach ($found as $target) {
                echo "$file\t$line\timport\t$target\t0\tmodule\n";
            }
        }
        $i = $j ?? $i;
    }
}
