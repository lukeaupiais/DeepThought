"""Compare manifest components and file membership with the build tools.

For each repository given, the components tool's results are checked against
the tools that own each manifest format:

- Go: ``go list -m`` names each go.mod's module, and ``go list -e ./...`` gives
  each package directory's module directory (module downloads may be needed); every .go file there must be
  PART_OF that module.
- Cargo: ``cargo metadata --no-deps --offline`` lists each package with its
  targets; every package must be a component of the same name, and each
  target's root file PART_OF it.
- npm/Node: ``module.findPackageJSON`` (Node 22.14+) gives the package.json
  Node resolves for a file; every JavaScript, TypeScript and JSON file must be
  PART_OF the node package that manifest declares.
- Maven: ``mvn help:evaluate`` gives each pom.xml's artifactId (optional,
  ``--maven``; it may download the help plugin).

Prints one JSON object of confirmed/contradicted/unchecked counts and
contradiction samples. Python projects have no such oracle and are not checked.

    python tools/compare_components_with_build_tools.py REPO [REPO ...] [--maven]
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from deepthought.components import (
    SKIPPED_DIRECTORIES,
    enclosing_components,
    scan_components,
)

NODE_SUFFIXES = (".js", ".cjs", ".mjs", ".jsx", ".ts", ".cts", ".mts", ".tsx", ".json")
NODE_ORACLE = r"""
const { findPackageJSON } = require("node:module");
const { pathToFileURL } = require("node:url");
const lines = require("node:fs").readFileSync(0, "utf8").split("\n").filter(Boolean);
const out = {};
for (const file of lines) {
  try { out[file] = findPackageJSON(pathToFileURL(file)) ?? null; }
  catch (error) { out[file] = "error:" + error.code; }
}
process.stdout.write(JSON.stringify(out));
"""


def files_under(root):
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRECTORIES)
        for name in sorted(names):
            yield (Path(directory) / name).relative_to(root).as_posix()


def nearest(path, components, kind):
    found = [c for c in enclosing_components(path, components) if c.kind == kind]
    return found[0] if found else None


class Tally:
    def __init__(self):
        self.counts = {}
        self.samples = {}

    def add(self, check, ok, detail=None):
        bucket = self.counts.setdefault(check, {"confirmed": 0, "contradicted": 0})
        bucket["confirmed" if ok else "contradicted"] += 1
        if not ok and len(self.samples.setdefault(check, [])) < 10:
            self.samples[check].append(detail)

    def skip(self, check, reason):
        bucket = self.counts.setdefault(check, {"confirmed": 0, "contradicted": 0})
        bucket.setdefault("unchecked", {}).setdefault(reason, 0)
        bucket["unchecked"][reason] += 1


def run(command, cwd, **env):
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        check=False,
        timeout=600,
    )


def check_go(root, components, tally):
    env = {"GOWORK": "off"}
    for module in [c for c in components if c.kind == "go_module"]:
        directory = root / module.root
        named = run(["go", "list", "-m", "-f", "{{.Path}}"], directory, **env)
        if named.returncode:
            tally.skip("go.module-name", "go list failed")
        else:
            tally.add(
                "go.module-name",
                named.stdout.strip() == module.name,
                [module.manifest, module.name, named.stdout.strip()],
            )
        listed = run(
            [
                "go",
                "list",
                "-e",
                "-f",
                "{{.Dir}}\t{{if .Module}}{{.Module.Dir}}{{end}}",
                "./...",
            ],
            directory,
            **env,
        )
        for line in listed.stdout.splitlines():
            package_dir, _, module_dir = line.partition("\t")
            if not module_dir:
                tally.skip("go.file-module", "package without module")
                continue
            expected = Path(module_dir).resolve().relative_to(root).as_posix()
            expected = "" if expected == "." else expected
            for go_file in sorted(Path(package_dir).glob("*.go")):
                path = go_file.resolve().relative_to(root).as_posix()
                found = nearest(path, components, "go_module")
                tally.add(
                    "go.file-module",
                    found is not None and found.root == expected,
                    [path, found and found.root, expected],
                )


def check_cargo(root, components, tally):
    seen = set()
    by_manifest = {c.manifest: c for c in components if c.kind == "cargo_package"}
    for manifest in sorted(
        {c.manifest for c in components if c.kind == "cargo_package"}
    ):
        metadata = run(
            ["cargo", "metadata", "--no-deps", "--offline", "--format-version", "1"],
            root / Path(manifest).parent,
        )
        if metadata.returncode:
            tally.skip("cargo.package", "cargo metadata failed")
            continue
        for package in json.loads(metadata.stdout)["packages"]:
            path = Path(package["manifest_path"]).resolve()
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if relative in seen:
                continue
            seen.add(relative)
            component = by_manifest.get(relative)
            tally.add(
                "cargo.package",
                component is not None and component.name == package["name"],
                [relative, package["name"], component and component.name],
            )
            for target in package["targets"]:
                source = Path(target["src_path"]).resolve()
                try:
                    source = source.relative_to(root).as_posix()
                except ValueError:
                    tally.skip("cargo.target-root", "outside repository")
                    continue
                found = nearest(source, components, "cargo_package")
                tally.add(
                    "cargo.target-root",
                    found is not None and found.manifest == relative,
                    [source, found and found.manifest, relative],
                )


def check_node(root, components, tally, files):
    selected = [f for f in files if f.endswith(NODE_SUFFIXES)]
    if not selected:
        return
    answer = subprocess.run(
        ["node", "-e", NODE_ORACLE],
        input="\n".join(str(root / f) for f in selected),
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
    )
    resolved = json.loads(answer.stdout)
    for path in selected:
        manifest = resolved[str(root / path)]
        if manifest is not None and manifest.startswith("error:"):
            tally.skip("node.file-package", manifest)
            continue
        if manifest is not None:
            manifest = Path(
                manifest[len("file://") :]
                if manifest.startswith("file://")
                else manifest
            )
            try:
                manifest = manifest.resolve().relative_to(root).as_posix()
            except ValueError:
                manifest = None
        found = nearest(path, components, "node_package")
        tally.add(
            "node.file-package",
            (found.manifest if found else None) == manifest,
            [path, found and found.manifest, manifest],
        )


def check_maven(root, components, tally):
    for module in [c for c in components if c.kind == "maven_module"]:
        answer = run(
            [
                "mvn",
                "-q",
                "-N",
                "help:evaluate",
                "-Dexpression=project.artifactId",
                "-DforceStdout",
            ],
            root / module.root,
        )
        if answer.returncode:
            tally.skip("maven.artifact-id", "mvn failed")
            continue
        tally.add(
            "maven.artifact-id",
            answer.stdout.strip() == module.name,
            [module.manifest, module.name, answer.stdout.strip()],
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("repositories", nargs="+", type=Path)
    parser.add_argument("--maven", action="store_true")
    parser.add_argument("--exclude", action="append", default=[])
    args = parser.parse_args(argv)
    report = {}
    for repository in args.repositories:
        root = repository.resolve()
        scan = scan_components(root, exclude=tuple(args.exclude))
        tally = Tally()
        files = list(files_under(root))
        check_go(root, scan.components, tally)
        check_cargo(root, scan.components, tally)
        check_node(root, scan.components, tally, files)
        if args.maven:
            check_maven(root, scan.components, tally)
        kinds = {}
        for component in scan.components:
            kinds[component.kind] = kinds.get(component.kind, 0) + 1
        report[root.name] = {
            "components": dict(sorted(kinds.items())),
            "diagnostics": [list(d) for d in scan.diagnostics],
            "checks": tally.counts,
            "contradictions": tally.samples,
        }
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    print()
    contradicted = sum(
        bucket["contradicted"]
        for entry in report.values()
        for bucket in entry["checks"].values()
    )
    return 1 if contradicted else 0


if __name__ == "__main__":
    raise SystemExit(main())
