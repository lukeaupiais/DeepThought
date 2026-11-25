"""Dependencies that manifests declare, and the in-repository components they
name.

Each declaration is read as written. It is linked to a component in the same
repository by one rule: a path the manifest gives (resolved), a workspace
reference to a uniquely named member (resolved), or, failing those, an equal
package name (name_matched, since a registry may supply that name instead).
"""

import json
import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import PurePosixPath

# Rule names; the tool registers each with its evidence type.
PATH = {
    "cargo_package": "cargo-path-dependency",
    "go_module": "go-replace-directory",
    "node_package": "npm-file-dependency",
    "python_project": "python-path-source",
}
PATCH = "cargo-patch-path"
WORKSPACE = {
    "cargo_package": "cargo-workspace-dependency",
    "node_package": "npm-workspace-protocol",
    "python_project": "python-workspace-source",
}
NAME = "dependency-name"
COMPOSE_DEPENDS_ON = "compose-depends-on"

_CARGO_SECTIONS = ("dependencies", "dev-dependencies", "build-dependencies")
_NPM_SECTIONS = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)
_PEP508_NAME = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
_MAVEN_NS = re.compile(r"^\{[^}]*\}")


@dataclass(frozen=True)
class Dependency:
    manifest: str
    section: str
    # The package name the declaration refers to (after any rename).
    name: str
    # A directory the manifest gives, relative to the manifest's directory.
    path: str | None = None
    # True when the declaration defers to its workspace.
    workspace: bool = False
    # Compose depends_on: the service that declares it.
    service: str | None = None


def normalize_python(name):
    """PEP 503 normalized project name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _toml(text):
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def _cargo(manifest, data):
    found = []
    tables = [(section, data.get(section)) for section in _CARGO_SECTIONS]
    for cfg, target in sorted((data.get("target") or {}).items()):
        if isinstance(target, dict):
            tables += [
                (f"target.{cfg}.{section}", target.get(section))
                for section in _CARGO_SECTIONS
            ]
    workspace = data.get("workspace")
    if isinstance(workspace, dict):
        tables.append(("workspace.dependencies", workspace.get("dependencies")))
    for registry, table in sorted((data.get("patch") or {}).items()):
        tables.append((f"patch.{registry}", table))
    for section, table in tables:
        if not isinstance(table, dict):
            continue
        for key, value in sorted(table.items()):
            if isinstance(value, dict):
                name = value.get("package", key)
                path = value.get("path")
                found.append(
                    Dependency(
                        manifest,
                        section,
                        name if isinstance(name, str) else key,
                        path if isinstance(path, str) and path else None,
                        value.get("workspace") is True,
                    )
                )
            else:
                found.append(Dependency(manifest, section, key))
    return found


def _go_directives(text):
    """(verb, tokens) for each go.mod directive line, blocks expanded."""
    block = None
    for raw in text.split("\n"):
        tokens = raw.split("//", 1)[0].split()
        if not tokens:
            continue
        if block is not None:
            if tokens == [")"]:
                block = None
            else:
                yield block, tokens
        elif len(tokens) == 2 and tokens[1] == "(":
            block = tokens[0]
        else:
            yield tokens[0], tokens[1:]


def _go(manifest, text):
    required, replaced = [], {}
    for verb, tokens in _go_directives(text):
        if verb == "require" and tokens:
            required.append(tokens[0].strip('"'))
        elif verb == "replace" and "=>" in tokens:
            arrow = tokens.index("=>")
            old = tokens[0].strip('"')
            new = tokens[arrow + 1 :]
            # Only a filesystem path (no version) replaces with a directory.
            # (modfile.IsDirectoryPath: ".", "..", or a ./ ../ / prefix.)
            target = new[0].strip('"') if len(new) == 1 else ""
            if target in {".", ".."} or target.startswith(("./", "../", "/")):
                replaced[old] = target
    return [
        Dependency(manifest, "require", module, replaced.get(module))
        for module in required
    ]


def _npm(manifest, data):
    found = []
    for section in _NPM_SECTIONS:
        table = data.get(section)
        if not isinstance(table, dict):
            continue
        for name, spec in sorted(table.items()):
            if not isinstance(spec, str):
                continue
            path = None
            for prefix in ("file:", "link:"):
                if spec.startswith(prefix):
                    path = spec[len(prefix) :] or None
            found.append(
                Dependency(manifest, section, name, path, spec.startswith("workspace:"))
            )
    return found


def _python(manifest, data):
    found = []
    project = data.get("project")
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    sources = (tool.get("uv") or {}).get("sources") or {}
    sources = {
        normalize_python(k): v for k, v in sources.items() if isinstance(v, dict)
    }

    def add(section, requirement):
        match = (
            _PEP508_NAME.match(requirement) if isinstance(requirement, str) else None
        )
        if match is None:
            return
        name = normalize_python(match.group(1))
        source = sources.get(name, {})
        path = source.get("path")
        found.append(
            Dependency(
                manifest,
                section,
                name,
                path if isinstance(path, str) and path else None,
                source.get("workspace") is True,
            )
        )

    if isinstance(project, dict):
        for requirement in project.get("dependencies") or []:
            add("project.dependencies", requirement)
        optional = project.get("optional-dependencies") or {}
        for extra, requirements in sorted(optional.items()):
            for requirement in requirements or []:
                add(f"project.optional-dependencies.{extra}", requirement)
    poetry = tool.get("poetry")
    if isinstance(poetry, dict):
        tables = [("tool.poetry.dependencies", poetry.get("dependencies"))]
        for group, value in sorted((poetry.get("group") or {}).items()):
            if isinstance(value, dict):
                tables.append(
                    (
                        f"tool.poetry.group.{group}.dependencies",
                        value.get("dependencies"),
                    )
                )
        for section, table in tables:
            if not isinstance(table, dict):
                continue
            for key, value in sorted(table.items()):
                if key == "python":
                    continue
                path = value.get("path") if isinstance(value, dict) else None
                found.append(
                    Dependency(
                        manifest,
                        section,
                        normalize_python(key),
                        path if isinstance(path, str) and path else None,
                    )
                )
    return found


def maven_coordinates(project):
    """(groupId, artifactId) of a pom's project element; groupId may come
    from the parent."""
    own = {_MAVEN_NS.sub("", c.tag): (c.text or "").strip() for c in project}
    parent = next((c for c in project if _MAVEN_NS.sub("", c.tag) == "parent"), None)
    group = own.get("groupId")
    if not group and parent is not None:
        group = next(
            (
                (c.text or "").strip()
                for c in parent
                if _MAVEN_NS.sub("", c.tag) == "groupId"
            ),
            None,
        )
    return group or None, own.get("artifactId") or None


def _maven(manifest, project):
    found = []
    for child in project:
        if _MAVEN_NS.sub("", child.tag) != "dependencies":
            continue
        for dependency in child:
            if _MAVEN_NS.sub("", dependency.tag) != "dependency":
                continue
            fields = {
                _MAVEN_NS.sub("", c.tag): (c.text or "").strip() for c in dependency
            }
            if fields.get("groupId") and fields.get("artifactId"):
                found.append(
                    Dependency(
                        manifest,
                        "dependencies." + (fields.get("scope") or "compile"),
                        fields["groupId"] + ":" + fields["artifactId"],
                    )
                )
    return found


def extract_dependencies(manifest: str, content: bytes) -> list[Dependency]:
    """Dependencies one manifest declares; an unreadable manifest has none
    (extract_components reports it)."""
    name = PurePosixPath(manifest).name
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return []
    if name == "go.mod":
        return _go(manifest, text)
    if name == "package.json":
        try:
            data = json.loads(text)
        except ValueError:
            return []
        return _npm(manifest, data) if isinstance(data, dict) else []
    if name == "pom.xml":
        try:
            project = ElementTree.fromstring(content)
        except ElementTree.ParseError:
            return []
        return _maven(manifest, project)
    if name in {"Cargo.toml", "pyproject.toml"}:
        data = _toml(text)
        if data is None:
            return []
        return (_cargo if name == "Cargo.toml" else _python)(manifest, data)
    return []


def _is_table(dependency):
    """Cargo workspace and patch tables: path sources, not dependencies."""
    return dependency.section == "workspace.dependencies" or (
        dependency.section.startswith("patch.")
    )


def join_directory(root, path):
    """A manifest-relative directory as a repository-relative one, or None
    when it leaves the repository or is absolute."""
    if path.startswith("/") or "\\" in path:
        return None
    parts = [p for p in root.split("/") if p]
    for part in path.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


@dataclass(frozen=True)
class Link:
    source: object  # Component
    target: object  # Component
    dependency: Dependency
    rule: str


def resolve_dependencies(components, dependencies, coordinates=None):
    """Link each declared dependency to the component it names, if any.

    ``coordinates`` maps a Maven component's manifest to "group:artifact".
    Returns (links, counts); a dependency on no in-repository component is
    ``external``, one whose name several components share is ``ambiguous``.
    """
    coordinates = coordinates or {}
    by_manifest = {}
    for component in components:
        by_manifest.setdefault(component.manifest, []).append(component)
    by_root = {(c.kind, c.root): c for c in components}
    by_name = {}
    for component in components:
        name = coordinates.get(component.manifest, component.name)
        if name is None:
            continue
        if component.kind == "python_project":
            name = normalize_python(name)
        by_name.setdefault((component.kind, name), []).append(component)
    # Cargo [workspace.dependencies] and [patch.*] paths, by manifest
    # directory.
    workspace_paths, patch_paths = {}, {}
    for dependency in dependencies:
        if not _is_table(dependency) or not dependency.path:
            continue
        root = str(PurePosixPath(dependency.manifest).parent)
        root = "" if root == "." else root
        target = join_directory(root, dependency.path)
        if target is not None:
            paths = (
                workspace_paths
                if dependency.section == "workspace.dependencies"
                else patch_paths
            )
            paths[(root, dependency.name)] = target

    def enclosing(paths, source, name):
        parts = source.root.split("/") if source.root else []
        for depth in range(len(parts), -1, -1):
            path = paths.get(("/".join(parts[:depth]), name))
            if path is not None:
                return path
        return None

    links, counts = [], {"linked": 0, "external": 0, "ambiguous": 0}
    for dependency in dependencies:
        if _is_table(dependency):
            continue
        for source in by_manifest.get(dependency.manifest, []):
            if source.kind == "compose_service":
                if source.name != dependency.service:
                    continue
                # depends_on names a service of the same file.
                target = next(
                    (
                        c
                        for c in by_manifest[dependency.manifest]
                        if c.name == dependency.name
                    ),
                    None,
                )
                if target is None or target == source:
                    counts["external"] += 1
                else:
                    counts["linked"] += 1
                    links.append(Link(source, target, dependency, COMPOSE_DEPENDS_ON))
                continue
            target, rule = None, None
            if dependency.path is not None:
                directory = join_directory(source.root, dependency.path)
                target = by_root.get((source.kind, directory))
                rule = PATH[source.kind] if target else None
            elif dependency.workspace and source.kind == "cargo_package":
                # The nearest enclosing workspace that declares the name.
                path = enclosing(workspace_paths, source, dependency.name)
                if path is not None:
                    target = by_root.get((source.kind, path))
                    rule = WORKSPACE[source.kind] if target else None
            if target is None and source.kind == "cargo_package":
                # A [patch] in an enclosing manifest replaces the registry
                # package of that name with a path.
                path = enclosing(patch_paths, source, dependency.name)
                if path is not None:
                    target = by_root.get((source.kind, path))
                    rule = PATCH if target else None
            if target is None and dependency.path is None:
                named = by_name.get((source.kind, dependency.name), [])
                if len(named) > 1:
                    counts["ambiguous"] += 1
                    continue
                if named:
                    target = named[0]
                    rule = (
                        WORKSPACE[source.kind]
                        if dependency.workspace
                        and source.kind in {"node_package", "python_project"}
                        else NAME
                    )
            if target is None or target == source:
                counts["external"] += 1
                continue
            counts["linked"] += 1
            links.append(Link(source, target, dependency, rule))
    return links, counts
