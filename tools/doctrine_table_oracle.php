<?php
// Doctrine entity tables, from PHP's own tokenizer, checked against the
// schema the repository's Doctrine migrations build.
//
// For every .php file under ROOT/src (vendor/ and dot directories skipped),
// a class carrying an `#[ORM\Entity]` attribute or an `@ORM\Entity` docblock
// annotation is an entity. Its table is the `name` of its `ORM\Table`
// attribute or annotation (unquoted), else its short class name under NAMING, the
// Doctrine naming strategy the repository configures (`default` keeps the
// name, `underscore` snake-cases it, `underscore_number_aware` also splits
// before digits). Mapped superclasses and embeddables are not entities. An
// entity in an inheritance hierarchy (`InheritanceType`), with a table name
// that is not a literal, or declared in a test is listed under not_counted
// with the reason. Literal `ORM\JoinTable` names in an entity's body are its
// join_tables.
//
// The migrations' up() methods (MIGRATIONS, default ROOT/migrations, in file
// name order) are read for `CREATE TABLE`, `RENAME TABLE ... TO`, `DROP
// TABLE` and the schema calls createTable, renameTable and dropTable. Each
// model records whether its table is in the resulting schema (`migrated`),
// which Doctrine's schema tool wrote from the same metadata: a label whose
// table is not migrated is suspect and is reported on stderr.
//
// No repository code is run.
//
// Usage: php tools/doctrine_table_oracle.php ROOT NAMING [MIGRATIONS] > labels.json

function significant(array $tokens, int $i, int $step): ?int {
    for ($j = $i + $step; $j >= 0 && $j < count($tokens); $j += $step) {
        $t = $tokens[$j];
        if (is_array($t) && in_array($t[0], [T_WHITESPACE, T_COMMENT], true)) {
            continue;
        }
        return $j;
    }
    return null;
}

function php_files(string $root): array {
    if (!is_dir($root)) {
        return [];
    }
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
        $found[] = $file->getPathname();
    }
    sort($found);
    return $found;
}

function table_name(string $class, string $naming): string {
    if ($naming === 'default') {
        return $class;
    }
    $pattern = $naming === 'underscore_number_aware'
        ? '/(?<=[a-z0-9])([A-Z])|(?<=[a-zA-Z])([0-9])/'
        : '/(?<=[a-z])([A-Z])/';
    return strtolower(preg_replace($pattern, '_$1$2', $class));
}

// The text of each attribute group or docblock annotation before a class.
function class_metadata(array $tokens, int $class): string {
    $text = '';
    for ($j = significant($tokens, $class, -1); $j !== null; $j = significant($tokens, $j, -1)) {
        $t = $tokens[$j];
        if (is_array($t) && in_array($t[0], [T_FINAL, T_ABSTRACT, T_READONLY], true)) {
            continue;
        }
        if (is_array($t) && $t[0] === T_DOC_COMMENT) {
            $text = $t[1] . "\n" . $text;
            continue;
        }
        if ($t === ']') {
            // Walk back to the #[ that opens this group.
            $nesting = 0;
            $group = '';
            for ($k = $j; $k >= 0; $k--) {
                $u = $tokens[$k];
                $group = (is_array($u) ? $u[1] : $u) . $group;
                if ($u === ']') {
                    $nesting++;
                } elseif ($u === '[' || (is_array($u) && $u[0] === T_ATTRIBUTE)) {
                    if (--$nesting === 0) {
                        break;
                    }
                }
            }
            if ($k < 0) {
                break;
            }
            $text = $group . "\n" . $text;
            $j = $k;
            continue;
        }
        break;
    }
    return $text;
}

// The literal `ORM\JoinTable` names declared in a class body.
function join_tables(array $tokens, int $class): array {
    $body = '';
    $depth = 0;
    for ($k = $class; $k < count($tokens); $k++) {
        $u = $tokens[$k];
        if ($u === '{' || (is_array($u) && in_array($u[0], [T_CURLY_OPEN, T_DOLLAR_OPEN_CURLY_BRACES], true))) {
            $depth++;
        } elseif ($u === '}' && --$depth === 0) {
            break;
        }
        if ($depth > 0) {
            $body .= is_array($u) ? $u[1] : $u;
        }
    }
    preg_match_all('/ORM\\\\JoinTable\s*\(\s*name\s*[:=]\s*([\'"])([^\'"]+)\1/', $body, $m);
    $names = array_values(array_unique($m[2]));
    sort($names);
    return $names;
}

$root = rtrim($argv[1], '/');
$naming = $argv[2];
$migrations = $argv[3] ?? "$root/migrations";
$models = [];
$not_counted = [];
$roots = [];   // short names of inheritance roots
$parents = []; // entity => short name of the class it extends
foreach (php_files("$root/src") as $path) {
    $file = substr($path, strlen($root) + 1);
    try {
        $tokens = token_get_all(file_get_contents($path), TOKEN_PARSE);
    } catch (Throwable $e) {
        fwrite(STDERR, "unparsed\t$file\n");
        continue;
    }
    $namespace = '';
    foreach ($tokens as $i => $t) {
        if (is_array($t) && $t[0] === T_NAMESPACE) {
            $j = significant($tokens, $i, 1);
            $namespace = is_array($tokens[$j]) ? $tokens[$j][1] : '';
        }
        if (!is_array($t) || $t[0] !== T_CLASS) {
            continue;
        }
        $prev = significant($tokens, $i, -1);
        if ($prev !== null && is_array($tokens[$prev]) && in_array($tokens[$prev][0], [T_DOUBLE_COLON, T_NEW], true)) {
            continue;
        }
        $j = significant($tokens, $i, 1);
        if (!is_array($tokens[$j]) || $tokens[$j][0] !== T_STRING) {
            continue;
        }
        $short = $tokens[$j][1];
        $class = ltrim("$namespace\\$short", '\\');
        $meta = class_metadata($tokens, $i);
        if (!preg_match('/(?:#\[|[\s,(]|@)ORM\\\\Entity\b/', $meta)) {
            continue;
        }
        if (preg_match('/ORM\\\\InheritanceType\b/', $meta)) {
            $not_counted[] = ['class' => $class, 'reason' => 'inheritance'];
            $roots[strtolower($short)] = true;
            continue;
        }
        if (str_contains($file, '/Tests/') || str_starts_with($file, 'tests/')) {
            $not_counted[] = ['class' => $class, 'reason' => 'test_only'];
            continue;
        }
        if (preg_match('/ORM\\\\Table\s*\((.*?)\)\s*[\]\n*]/s', $meta, $m)) {
            // `name:` (or `name=` in an annotation), or the first argument,
            // with Doctrine's identifier quoting (`\`name\``) removed.
            if (preg_match('/\bname\s*[:=]\s*([\'"])([^\'"]*)\1/', $m[1], $n)
                || preg_match('/^\s*([\'"])([^\'"]*)\1\s*(?:,|$)/', $m[1], $n)) {
                $table = trim($n[2], '`');
            } elseif (preg_match('/\bname\s*[:=]/', $m[1])) {
                $not_counted[] = ['class' => $class, 'reason' => 'table_name_not_literal'];
                continue;
            } else {
                $table = table_name($short, $naming);
            }
        } else {
            $table = table_name($short, $naming);
        }
        $k = significant($tokens, $j, 1);
        if ($k !== null && is_array($tokens[$k]) && $tokens[$k][0] === T_EXTENDS) {
            $base = $tokens[significant($tokens, $k, 1)][1];
            $parents[$class] = strtolower(substr(strrchr('\\' . $base, '\\'), 1));
        }
        $models[] = [
            'class' => $class,
            'files' => [$file],
            'table' => $table,
            'join_tables' => join_tables($tokens, $i),
        ];
    }
}

// An entity below an inheritance root (matched by the short name of the
// class it extends, through any number of entities) shares or joins the
// root's table: not counted.
$short_of = [];
foreach ($models as $model) {
    $short_of[strtolower(substr(strrchr('\\' . $model['class'], '\\'), 1))] = $model['class'];
}
$kept = [];
foreach ($models as $model) {
    $seen = [];
    for ($c = $model['class']; isset($parents[$c]) && !isset($seen[$c]); $c = $short_of[$parents[$c]] ?? '') {
        $seen[$c] = true;
        if (isset($roots[$parents[$c]])) {
            $not_counted[] = ['class' => $model['class'], 'reason' => 'inheritance'];
            continue 2;
        }
    }
    $kept[] = $model;
}
$models = $kept;

// The schema the migrations' up() methods leave.
$schema = [];
$migration_files = php_files($migrations);
foreach ($migration_files as $path) {
    $tokens = token_get_all(file_get_contents($path));
    // up(), else one platform's: mySQLUp() as written by migrations that
    // split up() per database platform.
    $methods = [];
    foreach ($tokens as $i => $t) {
        if (is_array($t) && $t[0] === T_FUNCTION) {
            $j = significant($tokens, $i, 1);
            if (is_array($tokens[$j]) && preg_match('/^(up|\w+Up)$/i', $tokens[$j][1])) {
                $methods[strtolower($tokens[$j][1])] ??= $j;
            }
        }
    }
    $up = $methods['up'] ?? $methods['mysqlup'] ?? (reset($methods) ?: null);
    if ($up === null) {
        continue;
    }
    $body = '';
    $depth = 0;
    for ($k = $up; $k < count($tokens); $k++) {
        $u = $tokens[$k];
        if ($u === '{' || (is_array($u) && in_array($u[0], [T_CURLY_OPEN, T_DOLLAR_OPEN_CURLY_BRACES], true))) {
            $depth++;
        } elseif ($u === '}') {
            if (--$depth === 0) {
                break;
            }
        }
        if ($depth > 0 && !(is_array($u) && in_array($u[0], [T_COMMENT, T_DOC_COMMENT], true))) {
            $body .= is_array($u) ? $u[1] : $u;
        }
    }
    $name = '[`"\']?([A-Za-z0-9_]+)[`"\']?';
    preg_match_all(
        "/CREATE TABLE (?:IF NOT EXISTS )?$name|RENAME TABLE $name TO $name|ALTER TABLE $name RENAME TO $name"
        . "|DROP TABLE (?:IF EXISTS )?$name|createTable\(\s*$name|renameTable\(\s*$name\s*,\s*$name|dropTable\(\s*$name/i",
        $body,
        $found,
        PREG_SET_ORDER
    );
    foreach ($found as $f) {
        $f = array_pad($f, 13, '');
        if ($f[1] !== '') {
            $schema[strtolower($f[1])] = true;
        } elseif ($f[2] !== '') {
            unset($schema[strtolower($f[2])]);
            $schema[strtolower($f[3])] = true;
        } elseif ($f[4] !== '') {
            unset($schema[strtolower($f[4])]);
            $schema[strtolower($f[5])] = true;
        } elseif ($f[6] !== '') {
            unset($schema[strtolower($f[6])]);
        } elseif ($f[7] !== '') {
            $schema[strtolower($f[7])] = true;
        } elseif ($f[8] !== '') {
            unset($schema[strtolower($f[8])]);
            $schema[strtolower($f[9])] = true;
        } elseif ($f[10] !== '') {
            unset($schema[strtolower($f[10])]);
        }
    }
}
foreach ($models as &$model) {
    $model['migrated'] = isset($schema[strtolower($model['table'])]);
    foreach ($model['join_tables'] as $join) {
        if ($migration_files && !isset($schema[strtolower($join)])) {
            fwrite(STDERR, "not migrated\t{$model['class']}\t$join\n");
        }
    }
    if ($migration_files && !$model['migrated']) {
        fwrite(STDERR, "not migrated\t{$model['class']}\t{$model['table']}\n");
    }
}
unset($model);
usort($models, fn($a, $b) => strcmp($a['class'], $b['class']));
usort($not_counted, fn($a, $b) => strcmp($a['class'], $b['class']));
$tables = array_keys($schema);
sort($tables);
echo json_encode([
    'oracle' => 'PHP ' . PHP_VERSION . ' token_get_all; Doctrine migrations up()',
    'naming' => $naming,
    'models' => $models,
    'not_counted' => $not_counted,
    'migrated_tables' => $tables,
], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . "\n";
