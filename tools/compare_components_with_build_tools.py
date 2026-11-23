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
- Dependencies: ``cargo metadata --all-features`` (with its resolve graph) gives the
  in-repository packages each package depends on, and ``go mod edit -json``
  each module's directory replacements of required modules; every REQUIRES
  link with resolved evidence must be one of them and each of them must be
  linked. A Yarn Berry ``yarn.lock`` at a repository root does the same for
  npm packages: each workspace's locked dependencies that resolve to
  ``workspace:`` entries (needs PyYAML). Name-matched links are counted as
  confirmed or refuted.
- Compose: ``docker compose -f FILE --profile '*' config --format json`` gives each file's
  services, build contexts and depends_on; every service, context and
  compose-depends-on link must match (optional, ``--compose``).
- Maven: ``mvn help:evaluate`` gives each pom.xml's artifactId (optional,
  ``--maven``; it may download the help plugin).

Prints one JSON object of confirmed/contradicted/unchecked counts and
contradiction samples. Python projects have no such oracle and are not checked.

    python tools/compare_components_with_build_tools.py REPO [REPO ...] [--maven]
"""

import argparse
from fnmatch import fnmatchcase
import json
import os
import posixpath
from pathlib import PurePosixPath
from urllib.parse import quote, unquote
from pathlib import Path
import subprocess
import sys

from deepthought.component_dependencies import NAME, resolve_dependencies
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


def files_under(root, exclude=()):
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRECTORIES)
        for name in sorted(names):
            path = (Path(directory) / name).relative_to(root).as_posix()
            if not any(fnmatchcase(path, pattern) for pattern in exclude):
                yield path


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


def check_dependencies(root, scan, tally):
    links, _ = resolve_dependencies(
        scan.components, scan.dependencies, dict(scan.coordinates)
    )
    ours = {}
    for link in links:
        if link.source.kind == "compose_service":
            continue  # Checked by check_compose.
        key = (link.source.kind, link.source.manifest, link.target.manifest)
        # A resolved rule wins over a name match for the same pair.
        if ours.get(key) != "resolved":
            ours[key] = "name_matched" if link.rule == NAME else "resolved"
    expected, checked = set(), set()
    env = {"GOWORK": "off"}
    for module in [c for c in scan.components if c.kind == "go_module"]:
        edited = run(["go", "mod", "edit", "-json"], root / module.root, **env)
        if edited.returncode:
            tally.skip("deps.go", "go mod edit failed")
            continue
        checked.add(("go_module", module.manifest))
        data = json.loads(edited.stdout)
        required = {r["Path"] for r in data.get("Require") or []}
        for replace in data.get("Replace") or []:
            new = replace["New"]
            if replace["Old"]["Path"] not in required or new.get("Version"):
                continue
            target = (root / module.root / new["Path"]).resolve() / "go.mod"
            try:
                target = target.relative_to(root).as_posix()
            except ValueError:
                continue
            expected.add(("go_module", module.manifest, target))
    seen = set()
    for manifest in sorted(
        {c.manifest for c in scan.components if c.kind == "cargo_package"}
    ):
        if manifest in seen:
            continue
        metadata = run(
            ["cargo", "metadata", "--all-features", "--format-version", "1"],
            root / Path(manifest).parent,
        )
        if metadata.returncode:
            tally.skip("deps.cargo", "cargo metadata failed")
            continue
        data = json.loads(metadata.stdout)
        local = {}
        for package in data["packages"]:
            if package["source"] is not None:
                continue
            try:
                local[package["id"]] = (
                    Path(package["manifest_path"])
                    .resolve()
                    .relative_to(root)
                    .as_posix()
                )
            except ValueError:
                continue
        seen.update(local.values())
        checked.update(("cargo_package", m) for m in local.values())
        for node in (data.get("resolve") or {}).get("nodes", []):
            if node["id"] not in local:
                continue
            for dep in node["deps"]:
                if dep["pkg"] in local:
                    expected.add(
                        ("cargo_package", local[node["id"]], local[dep["pkg"]])
                    )
    lock = root / "yarn.lock"
    if lock.is_file():
        try:
            import yaml
        except ImportError:
            tally.skip("deps.node", "PyYAML missing")
        else:
            entries = yaml.safe_load(lock.read_text())
            resolution = {}
            for keys, entry in entries.items():
                if isinstance(entry, dict) and "resolution" in entry:
                    for key in keys.split(", "):
                        resolution[key.strip()] = entry["resolution"]

            def workspace_manifest(value):
                if "@link:" in value and "::locator=" in value:
                    # A link: path, relative to the locating workspace.
                    path, _, locator = value.partition("::locator=")
                    base = workspace_manifest(unquote(locator))
                    if base is None:
                        return None
                    base = str(PurePosixPath(base).parent)
                    target = posixpath.normpath(
                        posixpath.join(base, path.rpartition("@link:")[2])
                    )
                    return "package.json" if target == "." else target + "/package.json"
                _, _, directory = value.rpartition("@workspace:")
                if not directory or directory == value:
                    return None
                return (
                    "package.json"
                    if directory == "."
                    else directory.rstrip("/") + "/package.json"
                )

            for entry in entries.values():
                if not isinstance(entry, dict):
                    continue
                locator = entry.get("resolution", "")
                if "@link:" in locator:
                    continue  # Yarn does not install a link's dependencies.
                source = workspace_manifest(locator)
                if source is None:
                    continue
                checked.add(("node_package", source))
                for name, spec in (entry.get("dependencies") or {}).items():
                    descriptor = f"{name}@{spec}"
                    target = workspace_manifest(
                        resolution.get(descriptor)
                        or resolution.get(
                            descriptor + "::locator=" + quote(locator, safe="")
                        )
                        or ""
                    )
                    if target is not None and target != source:
                        expected.add(("node_package", source, target))
    for key, evidence in sorted(ours.items()):
        if key[:2] not in checked:
            tally.skip("deps." + evidence, "no oracle for " + key[0])
            continue
        tally.add("deps." + evidence, key in expected, list(key))
    for key in sorted(expected):
        tally.add("deps.oracle-linked", key in ours, list(key))


def check_compose(root, scan, tally):
    from deepthought.component_dependencies import (
        COMPOSE_DEPENDS_ON,
        resolve_dependencies,
    )

    links, _ = resolve_dependencies(scan.components, scan.dependencies)
    ours = {}
    for link in links:
        if link.rule == COMPOSE_DEPENDS_ON:
            ours.setdefault((link.source.manifest, link.source.name), set()).add(
                link.target.name
            )
    services = {}
    for c in scan.components:
        if c.kind == "compose_service":
            services.setdefault(c.manifest, {})[c.name] = c
    for manifest, declared in sorted(services.items()):
        answer = run(
            [
                "docker",
                "compose",
                "-f",
                manifest,
                "--profile",
                "*",
                "config",
                "--format",
                "json",
            ],
            root,
        )
        if answer.returncode:
            tally.skip("compose.service", "docker compose config failed")
            continue
        config = json.loads(answer.stdout).get("services", {})
        tally.add(
            "compose.service",
            sorted(config) == sorted(declared),
            [manifest, sorted(declared), sorted(config)],
        )
        for name, service in sorted(config.items()):
            component = declared.get(name)
            if component is None:
                continue
            build = service.get("build")
            context = None
            if isinstance(build, dict) and build.get("context"):
                try:
                    context = (
                        Path(build["context"]).resolve().relative_to(root).as_posix()
                    )
                    context = "" if context == "." else context
                except ValueError:
                    context = None
            if component.context is None and context is not None:
                tally.skip("compose.context", "context not resolved here")
            else:
                tally.add(
                    "compose.context",
                    component.context == context,
                    [manifest, name, component.context, context],
                )
            depends = sorted(service.get("depends_on") or {})
            tally.add(
                "compose.depends-on",
                sorted(ours.get((manifest, name), ())) == depends,
                [manifest, name, sorted(ours.get((manifest, name), ())), depends],
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
    parser.add_argument("--compose", action="store_true")
    parser.add_argument("--exclude", action="append", default=[])
    args = parser.parse_args(argv)
    report = {}
    for repository in args.repositories:
        root = repository.resolve()
        scan = scan_components(root, exclude=tuple(args.exclude))
        tally = Tally()
        files = list(files_under(root, tuple(args.exclude)))
        check_go(root, scan.components, tally)
        check_cargo(root, scan.components, tally)
        check_node(root, scan.components, tally, files)
        check_dependencies(root, scan, tally)
        if args.compose:
            check_compose(root, scan, tally)
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
        for check, bucket in entry["checks"].items()
        # A name match the build resolves elsewhere is what name_matched means.
        if check != "deps.name_matched"
    )
    return 1 if contradicted else 0


if __name__ == "__main__":
    raise SystemExit(main())
