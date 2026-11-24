<?php
// PHP `use` imports as nikic/PHP-Parser reads them, one JSON object per
// file: the file, the lines of syntax errors, and each clause's line (of its
// `use` statement), kind (use, use_function or use_const), imported name and
// bound name. See tools/compare_php_imports.py.
//
// Usage: php tools/php_imports_php_parser.php PHP_PARSER_DIR ROOT FILE...

[, $library, $root] = $argv;
$files = array_slice($argv, 3);
spl_autoload_register(function ($class) use ($library) {
    if (str_starts_with($class, 'PhpParser\\')) {
        require $library . '/lib/' . str_replace('\\', '/', $class) . '.php';
    }
});

use PhpParser\ErrorHandler\Collecting;
use PhpParser\Node;
use PhpParser\Node\Stmt\GroupUse;
use PhpParser\Node\Stmt\Use_;
use PhpParser\NodeFinder;
use PhpParser\ParserFactory;

const KINDS = [
    Use_::TYPE_NORMAL => 'use',
    Use_::TYPE_FUNCTION => 'use_function',
    Use_::TYPE_CONSTANT => 'use_const',
];

$parser = (new ParserFactory())->createForNewestSupportedVersion();
$finder = new NodeFinder();
foreach ($files as $file) {
    $errors = new Collecting();
    $statements = $parser->parse(file_get_contents($root . '/' . $file), $errors) ?? [];
    $out = [];
    $uses = $finder->find(
        $statements, fn($n) => $n instanceof Use_ || $n instanceof GroupUse
    );
    foreach ($uses as $use) {
        foreach ($use->uses as $item) {
            $type = $item->type !== Use_::TYPE_UNKNOWN ? $item->type : $use->type;
            $name = $use instanceof GroupUse
                ? Node\Name::concat($use->prefix, $item->name)
                : $item->name;
            $out[] = [
                'file' => $file,
                'line' => $use->getStartLine(),
                'kind' => KINDS[$type],
                'specifier' => $name->toString(),
                'local' => $item->getAlias()->toString(),
            ];
        }
    }
    $lines = array_map(fn($e) => $e->getStartLine(), $errors->getErrors());
    echo json_encode(
        ['file' => $file, 'imports' => $out, 'error_lines' => $lines],
        JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE
    ), "\n";
}
