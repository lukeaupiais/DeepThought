"""Components that build manifests declare, and the files under their roots.

A component is a unit a build tool defines by a manifest file: a Python
project, an npm package, a Go module, a Cargo package or a Maven module. Its
root is the manifest's directory. Nothing is weighed or scored: a manifest
either declares a component or it does not, and a file belongs to the nearest
enclosing root of each kind.
"""

import json
import os
import re
import stat
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from fnmatch import fnmatchcase
from hashlib import sha256
from pathlib import Path, PurePosixPath

from .models import is_relative_file
from .syntax import extract_go_module

# Manifest file name -> component kind.
MANIFESTS = {
    "pyproject.toml": "python_project",
    "package.json": "node_package",
    "go.mod": "go_module",
    "Cargo.toml": "cargo_package",
    "pom.xml": "maven_module",
}
# Compose files: compose.yaml, docker-compose.yml and their variants such as
# compose.override.yaml or docker-compose.prod.yml; each declares services.
COMPOSE_FILE = re.compile(r"^(?:docker-)?compose(?:\.[A-Za-z0-9_.-]+)?\.ya?ml$")
PACKAGE_KINDS = frozenset(MANIFESTS.values())
DEPLOYMENT_KINDS = frozenset({"compose_service", "k8s_workload", "k8s_service"})
COMPONENT_KINDS = PACKAGE_KINDS | DEPLOYMENT_KINDS
YAML_FILE = re.compile(r"\.ya?ml$")
# Directories the source analyzer does not enter either.
SKIPPED_DIRECTORIES = frozenset({".git", ".venv", "node_modules", "__pycache__"})
_MAVEN_NS = re.compile(r"^\{[^}]*\}")


@dataclass(frozen=True)
class Component:
    kind: str
    manifest: str
    # The declared name; None for a package.json without one, which still
    # marks a package root.
    name: str | None
    # One-based line of the declaring table or directive, when the manifest
    # format gives one.
    line: int | None = None
    # Compose services: the repository-relative build context directory
    # ("" is the root), or None when the service builds nothing here.
    context: str | None = None
    # Compose services: the image the service names, as written.
    image: str | None = None
    # Kubernetes objects: metadata.namespace as written (None when absent),
    # the pod template's labels (workloads) or the selector (Services), and
    # the container images (workloads), as sorted tuples.
    namespace: str | None = None
    labels: tuple = ()
    selector: tuple = ()
    images: tuple = ()

    def __post_init__(self):
        if self.kind not in COMPONENT_KINDS:
            raise ValueError("Unsupported component kind")
        if not is_relative_file(self.manifest):
            raise ValueError("Manifest path must be canonical and relative")
        file_name = PurePosixPath(self.manifest).name
        if self.kind == "compose_service":
            valid = bool(COMPOSE_FILE.match(file_name))
        elif self.kind in DEPLOYMENT_KINDS:
            valid = bool(YAML_FILE.search(file_name))
        else:
            valid = MANIFESTS.get(file_name) == self.kind
        if not valid:
            raise ValueError("Unsupported manifest file")
        if self.kind in DEPLOYMENT_KINDS and self.name is None:
            raise ValueError("Deployment units are named")
        if self.kind not in {"compose_service", "k8s_workload"} and (
            self.context is not None
        ):
            raise ValueError("Only Compose services and workloads have a context")
        if self.kind != "compose_service" and self.image is not None:
            raise ValueError("Only Compose services have an image")
        if self.kind not in {"k8s_workload", "k8s_service"} and (
            self.namespace,
            self.labels,
            self.selector,
            self.images,
        ) != (None, (), (), ()):
            raise ValueError("Only Kubernetes objects have labels or images")
        if (
            self.context is not None
            and self.context != ""
            and not is_relative_file(self.context)
        ):
            raise ValueError("Build contexts are canonical relative directories")
        if self.name is not None and (
            not isinstance(self.name, str) or not self.name.strip()
        ):
            raise ValueError("Component names must be nonempty strings")
        if self.line is not None and (type(self.line) is not int or self.line < 1):
            raise ValueError("Component lines are positive integers")

    @property
    def root(self) -> str | None:
        """The directory whose files belong to the component, repository-
        relative ("" is the root): a package's manifest directory, a Compose
        service's build context, a workload's Skaffold artifact context, or
        None for a unit that builds nothing here."""
        if self.kind in DEPLOYMENT_KINDS:
            return self.context
        parent = PurePosixPath(self.manifest).parent.as_posix()
        return "" if parent == "." else parent


def _toml(text):
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        try:
            import tomli as tomllib
        except ImportError:
            raise ImportError("Provision tomli to read TOML manifests") from None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def _header_line(text, table):
    pattern = re.compile(
        r"^\s*\[\s*" + re.escape(table).replace(r"\.", r"\s*\.\s*") + r"\s*\]"
    )
    for row, line in enumerate(text.split("\n")):
        if pattern.match(line):
            return row + 1
    return None


def extract_components(manifest: str, content: bytes) -> list[Component] | None:
    """The components one manifest declares; None when it cannot be read.

    - ``pyproject.toml``: ``[project] name``, else ``[tool.poetry] name``; a
      file with neither (tool configuration only) declares nothing.
    - ``package.json``: a JSON object; every one marks a package root, named
      by its ``name`` string when it has one.
    - ``go.mod``: the ``module`` directive's path.
    - ``Cargo.toml``: ``[package] name``; a virtual workspace declares nothing.
    - ``pom.xml``: the project's own ``artifactId``.
    """
    if COMPOSE_FILE.match(PurePosixPath(manifest).name):
        from .deployments import extract_compose

        found = extract_compose(manifest, content)
        return None if found is None else found[0]
    kind = MANIFESTS.get(PurePosixPath(manifest).name)
    if kind is None:
        raise ValueError("Unsupported manifest file")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if kind == "go_module":
        clauses = extract_go_module(content, "components", manifest)
        if not clauses:
            return None
        return [Component(kind, manifest, clauses[0].name, clauses[0].line)]
    if kind == "node_package":
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        name = data.get("name")
        name = name if isinstance(name, str) and name.strip() else None
        return [Component(kind, manifest, name)]
    if kind == "maven_module":
        try:
            project = ElementTree.fromstring(content)
        except ElementTree.ParseError:
            return None
        if _MAVEN_NS.sub("", project.tag) != "project":
            return None
        for child in project:
            if _MAVEN_NS.sub("", child.tag) == "artifactId":
                name = (child.text or "").strip()
                return [Component(kind, manifest, name)] if name else None
        return None
    data = _toml(text)
    if data is None:
        return None
    if kind == "cargo_package":
        package = data.get("package")
        if not isinstance(package, dict):
            return []
        name = package.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        return [Component(kind, manifest, name, _header_line(text, "package"))]
    for table, value in (
        ("project", data.get("project")),
        ("tool.poetry", data.get("tool", {}).get("poetry")),
    ):
        if isinstance(value, dict):
            name = value.get("name")
            if isinstance(name, str) and name.strip():
                return [Component(kind, manifest, name, _header_line(text, table))]
    return []


@dataclass(frozen=True)
class ManifestScan:
    components: tuple[Component, ...]
    # (path, sha256) of each manifest read, in path order.
    manifests: tuple[tuple[str, str], ...]
    # (path, message) for each manifest that could not be read.
    diagnostics: tuple[tuple[str, str], ...]
    # Dependencies the manifests declare, as written.
    dependencies: tuple = ()
    # Maven module manifest -> "groupId:artifactId".
    coordinates: tuple[tuple[str, str], ...] = ()
    # (image, context) for each Skaffold build artifact.
    artifacts: tuple[tuple[str, str], ...] = ()
    # Whether other YAML files were read for Kubernetes objects (PyYAML).
    kubernetes: bool = True


def scan_components(
    root: str | Path,
    *,
    include: tuple[str, ...] = ("*",),
    exclude: tuple[str, ...] = (),
    max_file_bytes: int = 2_000_000,
) -> ManifestScan:
    """Read every manifest under ``root`` that the patterns select."""
    root = Path(root).resolve(strict=True)
    from .component_dependencies import extract_dependencies, maven_coordinates

    from . import deployments

    components, manifests, diagnostics = [], [], []
    dependencies, coordinates, artifacts = [], [], []
    kubernetes = deployments.yaml_available()

    def directory_error(exc):
        diagnostics.append(("", "Could not enumerate a selected directory"))

    for directory, dirs, names, directory_fd in os.fwalk(
        root, follow_symlinks=False, onerror=directory_error
    ):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRECTORIES)
        for name in sorted(names):
            other_yaml = (
                kubernetes
                and name not in MANIFESTS
                and not COMPOSE_FILE.match(name)
                and YAML_FILE.search(name)
            )
            if name not in MANIFESTS and not COMPOSE_FILE.match(name):
                if not other_yaml:
                    continue
            relative = (Path(directory) / name).relative_to(root).as_posix()
            if not any(fnmatchcase(relative, p) for p in include) or any(
                fnmatchcase(relative, p) for p in exclude
            ):
                continue
            if not is_relative_file(relative):
                diagnostics.append((relative, "Manifest path is not portable"))
                continue
            try:
                fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
                with os.fdopen(fd, "rb") as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise OSError("Manifest must be a regular file")
                    content = source.read(max_file_bytes + 1)
            except OSError:
                diagnostics.append((relative, "Manifest could not be read"))
                continue
            if other_yaml:
                # Any other YAML file may hold Kubernetes objects or a
                # Skaffold config; one that does not, or does not parse
                # (for example a Helm template), is not a manifest.
                if len(content) > max_file_bytes:
                    continue
                found = deployments.extract_kubernetes(relative, content)
                if found is None:
                    continue
                manifests.append((relative, sha256(content).hexdigest()))
                objects, built = found
                components.extend(objects)
                artifacts.extend(built)
                continue
            if len(content) > max_file_bytes:
                diagnostics.append((relative, "Manifest exceeds max_file_bytes"))
                continue
            manifests.append((relative, sha256(content).hexdigest()))
            try:
                declared = extract_components(relative, content)
            except ImportError as exc:  # tomli on Python 3.10, or PyYAML
                diagnostics.append((relative, str(exc)))
                continue
            if declared is None:
                diagnostics.append(
                    (relative, "Manifest is not valid or declares no name")
                )
                continue
            components.extend(declared)
            dependencies.extend(extract_dependencies(relative, content))
            if name == "pom.xml" and declared:
                group, artifact = maven_coordinates(ElementTree.fromstring(content))
                if group and artifact:
                    coordinates.append((relative, group + ":" + artifact))
    components = deployments.place_workloads(components, artifacts)
    return ManifestScan(
        tuple(components),
        tuple(manifests),
        tuple(diagnostics),
        tuple(dependencies),
        tuple(coordinates),
        tuple(sorted(set(artifacts))),
        kubernetes,
    )


def enclosing_components(path: str, components) -> list[Component]:
    """For each kind, the components whose root is nearest above ``path``.

    A file belongs to the nearest enclosing root of each kind, as Go, Cargo,
    npm, Maven and Python build front ends look up the manifest of the
    directory a file is in. Results are ordered by kind.
    """
    nearest = {}
    for component in components:
        root = component.root
        if root is None or (root and not path.startswith(root + "/")):
            continue
        depth = len(root.split("/")) if root else 0
        best = nearest.get(component.kind)
        if best is None or depth > best[0]:
            nearest[component.kind] = (depth, [component])
        elif depth == best[0]:
            best[1].append(component)
    return [
        component
        for kind in sorted(nearest)
        for component in sorted(nearest[kind][1], key=lambda c: c.manifest)
    ]
