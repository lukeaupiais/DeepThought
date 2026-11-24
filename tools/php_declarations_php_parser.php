<?php
// PHP declarations as nikic/PHP-Parser reads them, one JSON object per
// file: the file, the lines of syntax errors, and each declaration's file,
// line and byte column of its name, name, kind and symbol. The scoping
// rules are those of deepthought.syntax.extract_php, applied to
// PHP-Parser's tree; see tools/compare_php_declarations_with_php_parser.py.
//
// Usage: php tools/php_declarations_php_parser.php PHP_PARSER_DIR ROOT FILE...

[, $library, $root] = $argv;
$files = array_slice($argv, 3);
spl_autoload_register(function ($class) use ($library) {
    if (str_starts_with($class, 'PhpParser\\')) {
        require $library . '/lib/' . str_replace('\\', '/', $class) . '.php';
    }
});

use PhpParser\ErrorHandler\Collecting;
use PhpParser\Node;
use PhpParser\ParserFactory;

const TYPES = [
    Node\Stmt\Class_::class => 'Class',
    Node\Stmt\Interface_::class => 'Interface',
    Node\Stmt\Trait_::class => 'Trait',
    Node\Stmt\Enum_::class => 'Enum',
];

function declaration($name, $kind, $symbol, $file, $code)
{
    $position = $name->getStartFilePos();
    $newline = strrpos(substr($code, 0, $position), "\n");
    return [
        'file' => $file,
        'line' => $name->getStartLine(),
        'column' => $newline === false ? $position : $position - $newline - 1,
        'name' => $name->toString(),
        'kind' => $kind,
        'symbol' => implode('.', $symbol),
    ];
}

// $scope names what a declaration here belongs to; $namespace is the
// current namespace, where a function or class declared in a body goes; and
// $path is the lexical path an anonymous class's methods are named by.
function visit($node, $scope, $inType, $namespace, $path, $file, $code, &$out)
{
    if (is_array($node)) {
        foreach ($node as $child) {
            visit($child, $scope, $inType, $namespace, $path, $file, $code, $out);
        }
        return;
    }
    if (!$node instanceof Node) {
        return;
    }
    $kind = TYPES[get_class($node)] ?? null;
    if ($kind !== null) {
        if ($node->name === null) {
            // An anonymous class: its methods are named by where it is.
            visit($node->stmts, $path, true, $namespace, $path, $file, $code, $out);
            return;
        }
        $inner = [...$scope, $node->name->toString()];
        $out[] = declaration($node->name, $kind, $inner, $file, $code);
        visit($node->stmts, $inner, true, $namespace, $inner, $file, $code, $out);
        return;
    }
    if ($node instanceof Node\Stmt\Function_ || $node instanceof Node\Stmt\ClassMethod) {
        $symbol = [...$scope, $node->name->toString()];
        $out[] = declaration(
            $node->name, $inType ? 'Method' : 'Function', $symbol, $file, $code
        );
        // What its body declares goes to the namespace when it runs.
        visit($node->stmts, $namespace, false, $namespace, $symbol, $file, $code, $out);
        return;
    }
    if ($node instanceof Node\Stmt\Namespace_) {
        $names = $node->name === null ? [] : $node->name->getParts();
        visit($node->stmts, $names, false, $names, $names, $file, $code, $out);
        return;
    }
    foreach ($node->getSubNodeNames() as $name) {
        visit($node->$name, $scope, $inType, $namespace, $path, $file, $code, $out);
    }
}

$parser = (new ParserFactory())->createForNewestSupportedVersion();
foreach ($files as $file) {
    $code = file_get_contents($root . '/' . $file);
    $errors = new Collecting();
    $statements = $parser->parse($code, $errors) ?? [];
    $out = [];
    visit($statements, [], false, [], [], $file, $code, $out);
    $lines = array_map(fn($e) => $e->getStartLine(), $errors->getErrors());
    echo json_encode(
        ['file' => $file, 'declarations' => $out, 'error_lines' => $lines],
        JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE
    ), "\n";
}
