"""Compare PHP ``use`` imports and their resolution with PHP's own tools.

Facts: every ``.php`` file under ROOT is parsed by nikic/PHP-Parser
(tools/php_imports_php_parser.php), and each ``use`` clause must match one
of ours by file, line, kind, imported name and bound name. A difference on
or after a line where PHP-Parser reports a syntax error is put down to it.

Resolution: a class-like import is checked against Composer's class map
(``composer dump-autoload --optimize``, written to a temporary vendor
directory so ROOT is not changed), which maps each class Composer can
autoload to its file by Composer's own scan of the sources (``class_map_``
outcomes). A class Composer does not map (test fixtures, files outside the
autoload paths, a repository without composer.json) and a function import are
checked against the declarations PHP-Parser reads
(tools/php_declarations_php_parser.php; ``declared_`` and ``function_``
outcomes). An import is ``confirmed`` when we link it to the one declaring
file, ``ambiguous_agrees`` when several files declare it and we list exactly
those, ``agrees_unresolved`` when nothing declares it and we leave it
unresolved, ``namespace`` when we link it as a namespace and a type is
declared in that namespace, and ``contradicted`` otherwise.

Usage:
  python tools/compare_php_imports.py PHP_PARSER_DIR ROOT
"""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import tempfile

import deepthought
from deepthought.imports import resolve_imports

TOOLS = Path(__file__).parent
FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def run_php(script, library, root, files):
    for start in range(0, len(files), 200):
        output = subprocess.run(
            ["php", str(TOOLS / script), str(library), str(root)]
            + files[start : start + 200],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        yield from (json.loads(line) for line in output.splitlines())


def class_map(root):
    """Lowercased class name -> file relative to ROOT, by Composer."""
    if not (root / "composer.json").exists():
        return {}
    with tempfile.TemporaryDirectory() as vendor:
        subprocess.run(
            ["composer", "dump-autoload", "--optimize", "--no-scripts", "-n", "-q"],
            cwd=root,
            env={**os.environ, "COMPOSER_VENDOR_DIR": vendor},
            check=True,
        )
        output = subprocess.run(
            [
                "php",
                "-r",
                "echo json_encode(require $argv[1], JSON_UNESCAPED_SLASHES);",
                str(Path(vendor) / "composer" / "autoload_classmap.php"),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    found = {}
    for name, path in json.loads(output).items():
        path = Path(path)
        if path.is_relative_to(root):
            found[name.translate(FOLD)] = path.relative_to(root).as_posix()
    return found


def judge(ours, declared, namespace):
    """The outcome of one resolution, given the files declaring the name
    and whether a type is declared in a namespace of that name."""
    files = [link.target_file for link in ours.links]
    rules = {link.rule_id for link in ours.links}
    if len(declared) == 1:
        if ours.status == "linked" and files == sorted(declared):
            return "confirmed"
        if ours.status == "ambiguous" and set(declared) <= set(ours.candidates):
            return "ambiguous_includes"
        return "contradicted"
    if len(declared) > 1:
        if ours.status == "ambiguous" and set(ours.candidates) == set(declared):
            return "ambiguous_agrees"
        return "contradicted"
    if ours.status == "unresolved":
        return "agrees_unresolved"
    if rules == {"tree-sitter.php-use-namespace"} and namespace:
        return "namespace"
    return "contradicted"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("php_parser", type=Path)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    library = args.php_parser.resolve()
    result = deepthought.analyze_repository(
        root, repository_id="comparison", languages=("php",)
    )
    files = sorted(c.file for c in result.coverage if c.language == "php")

    theirs, errors = set(), set()
    for item in run_php("php_imports_php_parser.php", library, root, files):
        errors |= {(item["file"], n) for n in item["error_lines"]}
        theirs |= {
            (i["file"], i["line"], i["kind"], i["specifier"], i["local"])
            for i in item["imports"]
        }
    ours = {
        (i.file, i.line, i.kind, i.specifier, i.local_names[0]) for i in result.imports
    }
    counts, examples = Counter(), {}

    def count(outcome, example):
        counts[outcome] += 1
        examples.setdefault(outcome, []).append(example)

    for side, items in (
        ("tree_sitter_only", ours - theirs),
        ("php_parser_only", theirs - ours),
    ):
        for item in sorted(items):
            if any(f == item[0] and n <= item[1] for f, n in errors):
                count(side + "_syntax_error", list(item))
            else:
                count(side + "_unexplained", list(item))
    counts["imports_matched"] = len(ours & theirs)

    classes = class_map(root)
    types, functions = {}, {}
    for item in run_php("php_declarations_php_parser.php", library, root, files):
        for d in item["declarations"]:
            name = d["symbol"].translate(FOLD)
            if d["kind"] == "Function":
                functions.setdefault(name, set()).add(d["file"])
            elif d["kind"] != "Method":
                types.setdefault(name, set()).add(d["file"])
    spaces = {n.rpartition("\\")[0] for n in classes} | {
        n.rpartition(".")[0].replace(".", "\\") for n in types
    }
    by_id = {i.id: i for i in result.imports}
    for resolution in resolve_imports(result):
        item = by_id[resolution.import_id]
        example = [item.file, item.line, item.specifier]
        folded = item.specifier.translate(FOLD)
        name = folded.replace("\\", ".")
        if item.kind == "use_const":
            count("const_unresolved", example)
        elif item.kind == "use_function":
            outcome = judge(resolution, functions.get(name, set()), False)
            count("function_" + outcome, example)
        elif folded in classes:
            # Composer autoloads it from this file.
            outcome = judge(resolution, {classes[folded]}, False)
            count("class_map_" + outcome, example)
        else:
            namespace = any(n == folded or n.startswith(folded + "\\") for n in spaces)
            outcome = judge(resolution, types.get(name, set()), namespace)
            count("declared_" + outcome, example)
    print(
        json.dumps(
            {
                "files": len(files),
                "files_with_syntax_errors": len({f for f, _ in errors}),
                "imports": len(ours),
                "php_parser_imports": len(theirs),
                "class_map": len(classes),
                **dict(sorted(counts.items())),
                "examples": {
                    k: v[:15]
                    for k, v in sorted(examples.items())
                    if not k.endswith(("confirmed", "agrees_unresolved", "matched"))
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
