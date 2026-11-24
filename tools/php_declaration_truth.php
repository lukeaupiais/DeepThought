<?php
// Ground-truth PHP declarations from PHP's own tokenizer (token_get_all).
//
// For every .php file under ROOT (or the files given), prints one
// tab-separated row per class, interface, trait, enum and named function in
// the declaration-truth format of tools/c_declaration_truth.py: file, line,
// name, kind, begin_line. Kinds: `type`, `method` (a function in a class-like
// body, anonymous classes included) and `function`; begin_line is the line of
// the first attribute or modifier. Closures, arrow functions, anonymous
// classes themselves, `X::class` and `use function` imports are skipped. A
// file that is not valid PHP (php -l) is listed as unparsed on stderr and not
// indexed.
//
// Usage: php tools/php_declaration_truth.php ROOT [FILE ...] > truth.tsv

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

// The line a declaration begins on: its first modifier or attribute.
function begin_line(array $tokens, int $i): int {
    $modifiers = [T_ABSTRACT, T_FINAL, T_PUBLIC, T_PROTECTED, T_PRIVATE, T_STATIC, T_READONLY];
    $begin = $tokens[$i][2];
    $j = significant($tokens, $i, -1);
    while ($j !== null) {
        $t = $tokens[$j];
        if (is_array($t) && in_array($t[0], $modifiers, true)) {
            $begin = $t[2];
        } elseif ($t === ']') {
            // Walk back over one attribute group, #[ ... ].
            $nesting = 0;
            for ($k = $j; $k >= 0; $k--) {
                $u = $tokens[$k];
                if ($u === ']') {
                    $nesting++;
                } elseif ($u === '[') {
                    $nesting--;
                } elseif (is_array($u) && $u[0] === T_ATTRIBUTE) {
                    if (--$nesting === 0) {
                        break;
                    }
                }
            }
            if ($k < 0) {
                break; // an array literal, not an attribute
            }
            $begin = $tokens[$k][2];
            $j = $k;
        } else {
            break;
        }
        $j = significant($tokens, $j, -1);
    }
    return $begin;
}

function declarations(string $code, string $file): array {
    $tokens = token_get_all($code);
    $rows = [];
    $depth = 0;
    $types = [];          // brace depth at which each open class-like body began
    $pendingType = false; // a class-like name was read; its body opens next
    $line = 1;
    $typeKinds = [T_CLASS, T_INTERFACE, T_TRAIT, T_ENUM];
    foreach ($tokens as $i => $t) {
        if (is_array($t)) {
            $line = $t[2];
            if (in_array($t[0], $typeKinds, true)) {
                $before = significant($tokens, $i, -1);
                $after = significant($tokens, $i, 1);
                $prev = $before === null ? null : $tokens[$before];
                $next = $after === null ? null : $tokens[$after];
                if (is_array($prev) && $prev[0] === T_DOUBLE_COLON) {
                    continue; // X::class
                }
                if (is_array($prev) && $prev[0] === T_NEW) {
                    $pendingType = true; // new class: unnamed, but its functions are methods
                    continue;
                }
                if (is_array($next) && $next[0] === T_STRING) {
                    $rows[] = [$file, $next[2], $next[1], 'type', begin_line($tokens, $i)];
                    $pendingType = true;
                }
            } elseif ($t[0] === T_FUNCTION) {
                $before = significant($tokens, $i, -1);
                if ($before !== null && is_array($tokens[$before]) && $tokens[$before][0] === T_USE) {
                    continue; // use function Foo\bar; imports, not declares
                }
                $after = significant($tokens, $i, 1);
                $amp = $after === null ? null : $tokens[$after];
                if ($amp === '&' || (is_array($amp) && $amp[1] === '&')) {
                    $after = significant($tokens, $after, 1);
                }
                $next = $after === null ? null : $tokens[$after];
                // A method may be named by a reserved word (`function list()`).
                if (is_array($next) && preg_match('/^[A-Za-z_\x80-\xff][\w\x80-\xff]*$/', $next[1])) {
                    $inType = $types && end($types) === $depth - 1;
                    $rows[] = [$file, $next[2], $next[1], $inType ? 'method' : 'function', begin_line($tokens, $i)];
                }
            } elseif (in_array($t[0], [T_CURLY_OPEN, T_DOLLAR_OPEN_CURLY_BRACES], true)) {
                $depth++;
            }
            continue;
        }
        if ($t === '{') {
            if ($pendingType) {
                $types[] = $depth;
                $pendingType = false;
            }
            $depth++;
        } elseif ($t === '}') {
            $depth--;
            if ($types && end($types) === $depth) {
                array_pop($types);
            }
        }
    }
    return $rows;
}

$root = rtrim(realpath($argv[1]), '/');
$files = array_slice($argv, 2);
if (!$files) {
    $it = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS));
    foreach ($it as $path) {
        $p = $path->getPathname();
        if (str_ends_with($p, '.php') && !str_contains($p, '/.git/')) {
            $files[] = substr($p, strlen($root) + 1);
        }
    }
}
sort($files);
$rows = [];
$indexed = [];
foreach ($files as $file) {
    $path = "$root/$file";
    exec('php -l ' . escapeshellarg($path) . ' 2>&1', $out, $status);
    if ($status !== 0) {
        fwrite(STDERR, "$file: unparsed\n");
        continue;
    }
    $indexed[] = $file;
    foreach (declarations(file_get_contents($path), $file) as $row) {
        $rows[implode("\t", $row)] = $row;
    }
}
echo "# file\tline\tname\tkind\tbegin_line\n";
foreach ($indexed as $f) {
    echo "# indexed\t$f\n";
}
ksort($rows);
foreach (array_keys($rows) as $row) {
    echo "$row\n";
}
