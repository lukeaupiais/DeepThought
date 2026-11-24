"""Mutation testing of call resolution against the ground-truth corpus.

Each mutant changes one operator in a resolution module (``==`` to ``!=``,
``<`` to ``<=``, ``in`` to ``not in``, ``and`` to ``or``, dropping a ``not``,
...). A mutant is *killed* when scoring the corpus with it gives fewer
confirmed or more contradicted links for any rule than the unmutated code, or
when it raises or runs out of time. A mutant the corpus does not kill is run
against the unit tests of call resolution; one that survives both is logic
nothing checks (or a change with no effect, which the report cannot tell
apart: read it).

Usage:
  python tools/mutate_resolution.py [--module calls] [--limit N] [--cache DIR]
      [--corpus NAME ...] [--lines A-B ...] [--reanalyze]

The corpus checkouts are fetched as by ``ground_truth.py check``. By default
analyses are computed once and cached, since mutants of a resolution module
only change resolution. ``--reanalyze`` runs each mutant on a copy of the
package and analyzes the corpora again, for modules such as ``syntax`` whose
facts are read during analysis. ``--corpus`` limits the corpora and
``--lines`` the mutants (by the line of the mutated expression), so a run can
target the code one change added.
"""

import argparse
import ast
import json
import os
from pathlib import Path
import pickle
import shutil
import subprocess
import sys
import tempfile
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ground_truth  # noqa: E402

SWAPS = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.And: ast.Or,
    ast.Or: ast.And,
}
UNIT_TESTS = (
    "tests.test_calls",
    "tests.test_go",
    "tests.test_java",
    "tests.test_javascript",
    "tests.test_rust",
    "tests.test_inheritance",
    "tests.test_imports",
)


def sites(tree):
    """Every mutable operator, in source order: (node, field, index)."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                if type(op) in SWAPS:
                    found.append((node, "ops", i))
        elif isinstance(node, ast.BoolOp) and type(node.op) in SWAPS:
            found.append((node, "op", None))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            found.append((node, "not", None))
    return sorted(
        found,
        key=lambda s: (s[0].lineno, s[0].col_offset, s[1], s[2] or 0),
    )


def mutant(source, index):
    """The mutated source tree and a description of mutant ``index``."""
    tree = ast.parse(source)
    node, field, i = sites(tree)[index]
    before = ast.unparse(node)
    if field == "ops":
        node.ops[i] = SWAPS[type(node.ops[i])]()
    elif field == "op":
        node.op = SWAPS[type(node.op)]()
    else:
        # `not x` -> `x`, in place in the parent.
        replacement = node.operand
        for parent in ast.walk(tree):
            for name, value in ast.iter_fields(parent):
                if value is node:
                    setattr(parent, name, replacement)
                elif isinstance(value, list):
                    for j, item in enumerate(value):
                        if item is node:
                            value[j] = replacement
        node = replacement
    ast.fix_missing_locations(tree)
    function = next(
        (
            f.name
            for f in ast.walk(tree)
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
            and f.lineno <= node.lineno <= (f.end_lineno or f.lineno)
            and not any(
                isinstance(g, (ast.FunctionDef, ast.AsyncFunctionDef))
                and g is not f
                and f.lineno < g.lineno <= node.lineno <= (g.end_lineno or g.lineno)
                for g in ast.walk(f)
            )
        ),
        None,
    )
    return tree, {
        "line": node.lineno,
        "function": function,
        "before": before[:120],
        "after": ast.unparse(node)[:120] if field != "not" else before[4:124],
    }


def scores(analyses):
    return {
        name: ground_truth.score(result, truth, examples=0)[0]
        for name, (result, truth) in analyses.items()
    }


def killed_by_corpus(reference, mutated):
    for name, report in mutated.items():
        if ground_truth.regressions(report, reference[name]):
            return name
    return None


def mutated_package(module, tree, scratch):
    """A copy of the package with ``module`` replaced by ``tree``."""
    import deepthought

    package = Path(deepthought.__file__).parent
    copy_root = Path(scratch, "deepthought")
    shutil.copytree(package, copy_root, ignore=shutil.ignore_patterns("__pycache__"))
    (copy_root / f"{module}.py").write_text(ast.unparse(tree), "utf-8")
    return scratch


def reanalyze_worker(cache):
    """Score corpora analyzed by the package on ``sys.path`` (a mutant copy)."""
    with open(cache, "rb") as stream:
        corpora, reference = pickle.load(stream)
    analyses = {
        corpus["name"]: (
            ground_truth.analyze(
                workspace, corpus["languages"], corpus.get("exclude", ())
            ),
            truth,
        )
        for corpus, workspace, truth in corpora
    }
    killer = killed_by_corpus(reference, scores(analyses))
    print(json.dumps({"killed_by": killer}))


def worker(module, index, cache):
    import deepthought

    path = Path(deepthought.__file__).with_name(f"{module}.py")
    tree, _ = mutant(path.read_text("utf-8"), index)
    name = f"deepthought.{module}"
    replaced = types.ModuleType(name)
    replaced.__file__ = str(path)
    replaced.__package__ = "deepthought"
    exec(compile(tree, str(path), "exec"), replaced.__dict__)
    sys.modules[name] = replaced
    setattr(deepthought, module, replaced)
    with open(cache, "rb") as stream:
        analyses, reference = pickle.load(stream)
    killer = killed_by_corpus(reference, scores(analyses))
    print(json.dumps({"killed_by": killer}))


def unit_tests_kill(module, tree, timeout):
    with tempfile.TemporaryDirectory() as scratch:
        mutated_package(module, tree, scratch)
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "unittest", *UNIT_TESTS],
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": scratch},
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return True
    return completed.returncode != 0


def line_range(text):
    first, _, last = text.partition("-")
    return int(first), int(last or first)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", default="calls")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--cache", type=Path, default=Path(".ground-truth-cache"))
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--corpus", action="append", default=[])
    parser.add_argument("--lines", action="append", default=[], type=line_range)
    parser.add_argument("--reanalyze", action="store_true")
    parser.add_argument("--worker", type=int, help=argparse.SUPPRESS)
    parser.add_argument(
        "--reanalyze-worker", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument("--analyses", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.reanalyze_worker:
        reanalyze_worker(args.analyses)
        return 0
    if args.worker is not None:
        worker(args.module, args.worker, args.analyses)
        return 0

    import deepthought

    source = Path(deepthought.__file__).with_name(f"{args.module}.py").read_text()
    found = sites(ast.parse(source))
    indices = [
        index
        for index, (node, _, _) in enumerate(found)
        if not args.lines or any(a <= node.lineno <= b for a, b in args.lines)
    ]
    if args.limit is not None:
        indices = indices[: args.limit]
    manifest = json.loads((ground_truth.CORPUS / "manifest.json").read_text())
    corpora = [
        c for c in manifest["corpora"] if not args.corpus or c["name"] in args.corpus
    ]
    unknown = set(args.corpus) - {c["name"] for c in corpora}
    if unknown:
        parser.error(f"unknown corpus: {', '.join(sorted(unknown))}")
    analyses = {}
    fetched = []
    for corpus in corpora:
        workspace = ground_truth.fetch(corpus, args.cache)
        truth = ground_truth.read_truth(ground_truth.CORPUS / corpus["truth"])
        fetched.append((corpus, str(workspace), truth))
        analyses[corpus["name"]] = (
            ground_truth.analyze(
                workspace, corpus["languages"], corpus.get("exclude", ())
            ),
            truth,
        )
    reference = scores(analyses)
    results = []
    with tempfile.TemporaryDirectory() as scratch:
        cache = Path(scratch, "analyses.pickle")
        with open(cache, "wb") as stream:
            pickle.dump(
                (fetched, reference) if args.reanalyze else (analyses, reference),
                stream,
            )
        for index in indices:
            tree, description = mutant(source, index)
            with tempfile.TemporaryDirectory() as package:
                if args.reanalyze:
                    mutated_package(args.module, tree, package)
                    command = [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--reanalyze-worker",
                        "--analyses",
                        str(cache),
                    ]
                    env = {**os.environ, "PYTHONPATH": package}
                else:
                    command = [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--module",
                        args.module,
                        "--worker",
                        str(index),
                        "--analyses",
                        str(cache),
                    ]
                    env = None
                try:
                    completed = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        timeout=args.timeout,
                        env=env,
                    )
                    if completed.returncode:
                        outcome = "killed_by_error"
                    else:
                        last = completed.stdout.splitlines()[-1]
                        killer = json.loads(last)["killed_by"]
                        outcome = f"killed_by_corpus:{killer}" if killer else None
                except subprocess.TimeoutExpired:
                    outcome = "killed_by_timeout"
            if outcome is None:
                outcome = (
                    "killed_by_unit_tests"
                    if unit_tests_kill(args.module, tree, args.timeout)
                    else "survived"
                )
            results.append({"mutant": index, **description, "outcome": outcome})
            print(json.dumps(results[-1]), file=sys.stderr, flush=True)
    summary = {}
    for r in results:
        key = r["outcome"].split(":")[0]
        summary[key] = summary.get(key, 0) + 1
    print(
        json.dumps(
            {
                "module": args.module,
                "mutants": len(results),
                **dict(sorted(summary.items())),
                "survived": [r for r in results if r["outcome"] == "survived"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
