"""Compose projects and Kubernetes objects as the deployment tools render them.

``deployfacts/main.go`` is built with the Go toolchain against pinned
releases of the tools that define these formats and renders the repository's
deployment descriptions with them:

- Compose files with compose-go, the loader of ``docker compose``: the
  default file compose-go discovers in a directory (``compose.yaml``,
  ``compose.yml``, ``docker-compose.yml``, ``docker-compose.yaml``) together
  with the override file it discovers there, as ``docker compose`` reads a
  directory, and every other Compose file on its own, as
  ``docker compose -f FILE`` does;
  ``extends``, ``include``, profiles and ``${VARIABLE}`` interpolation are
  the loader's. Variables come from the project's ``.env`` file (and
  ``PWD`` is the project directory, where ``docker compose`` runs); the
  process environment is never read, and a variable nothing sets is
  ``PLACEHOLDER``.
- Helm charts with Helm's own install action in client-only dry-run mode,
  which is what ``helm template`` runs: default values, release name
  ``release-name``, no cluster. A chart under another chart's ``charts/``
  is rendered as part of it.
- Kustomizations with kustomize's krusty, which is what ``kustomize build``
  runs: no plugins, no Helm inflation, loading restricted to the
  kustomization's tree. A ``Component`` is only rendered by the
  kustomizations that use it.

The tools are required. The helper is built once per source and toolchain
into the cache directory; the first build downloads the pinned modules from
the Go module proxy. Without a Go toolchain ``DeployToolsMissing`` is raised.
Nothing of the repository is built, pulled or run.
"""

from dataclasses import dataclass, field
from functools import lru_cache
from hashlib import sha256
from importlib import resources
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile

PLACEHOLDER = "\x00"  # text a variable nothing sets would have held
# The helper's placeholder: compose-go reads NUL as the end of a volume spec.
_HELPER_PLACEHOLDER = "\ue000"

# compose.yaml, docker-compose.yml and variants such as docker-compose-kafka.yml
# or compose.override.yaml.
COMPOSE_FILE = re.compile(r"^(?:docker-)?compose(?:[.-][A-Za-z0-9_.-]+)?\.ya?ml$")
_KUSTOMIZATION = ("kustomization.yaml", "kustomization.yml", "Kustomization")
_SKIPPED = frozenset({"node_modules", "target", "build", "vendor"})
_SOURCES = ("main.go", "go.mod", "go.sum")


class DeployToolsMissing(RuntimeError):
    """The Go toolchain, which builds the deployment tools, is not installed."""


class DeployModulesUnavailable(DeployToolsMissing):
    """The pinned Compose, Helm and kustomize modules are neither in the Go
    module cache nor downloadable (no network on a first build)."""


@dataclass(frozen=True)
class ComposeService:
    name: str
    image: str | None
    context: str | None  # repository-relative build context ("" is the root)
    dockerfile: str | None  # repository-relative
    depends_on: tuple
    environment: tuple  # (name, value or None); values may hold PLACEHOLDER
    # (source, target): a bind's source, relative when inside the
    # repository; None for a volume that is not a bind mount.
    volumes: tuple
    profiles: tuple


@dataclass(frozen=True)
class ComposeProject:
    file: str  # the first Compose file, repository-relative
    files: tuple  # every file the project read
    services: tuple
    missing: tuple  # variables nothing set
    unread_env_files: bool = False


@dataclass(frozen=True)
class Rendering:
    source: str  # the Chart.yaml or kustomization file, repository-relative
    tool: str  # "helm" or "kustomize"
    documents: tuple  # the rendered YAML documents (parsed)


@dataclass
class DeployFacts:
    compose: list = field(default_factory=list)
    renderings: list = field(default_factory=list)
    # (description, tool, message) of descriptions a tool could not render.
    errors: list = field(default_factory=list)
    charts: set = field(default_factory=set)  # chart directories
    kustomizations: set = field(default_factory=set)  # kustomization directories
    # Files kustomize read to render a kustomization, repository-relative.
    inputs: set = field(default_factory=set)
    # Directories of kustomizations kustomize could not render.
    unrendered: set = field(default_factory=set)

    def tool_input(self, file):
        """Whether a YAML file is a chart's or a kustomization's input (read
        by the tool, not as a manifest of its own): any file of a chart's
        tree, a file kustomize read for a kustomization, or a file next to
        a kustomization kustomize could not render. A manifest next to a
        kustomization that does not list it is deployed on its own."""
        if file in self.inputs:
            return True
        directory = str(PurePosixPath(file).parent)
        directory = "" if directory == "." else directory
        if directory in self.unrendered:
            return True
        parts = PurePosixPath(file).parts[:-1]
        return any(
            "/".join(parts[:end]) in self.charts for end in range(len(parts) + 1)
        )


def _cache_directory():
    configured = os.environ.get("DEEPTHOUGHT_CACHE")
    if configured:
        return Path(configured)
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "deepthought"


def _go():
    found = os.environ.get("DEEPTHOUGHT_GO") or shutil.which("go")
    if not found:
        raise DeployToolsMissing(
            "the Go toolchain is required to build the Compose, Helm and "
            "kustomize renderers"
        )
    return found


def build(timeout=1200):
    """The deployfacts binary for this source and toolchain, built into the
    cache directory when missing."""
    go = _go()
    package = resources.files(__package__).joinpath("deployfacts")
    sources = {name: package.joinpath(name).read_bytes() for name in _SOURCES}
    environment = dict(os.environ)
    environment.update(
        GOFLAGS="-mod=readonly", GOWORK="off", CGO_ENABLED="0", GOTELEMETRY="off"
    )
    version = subprocess.run(
        [go, "env", "GOVERSION", "GOOS", "GOARCH"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env=environment,
    ).stdout
    digest = sha256(b"\0".join(sources[n] for n in _SOURCES) + version.encode())
    binary = _cache_directory() / f"deployfacts-{digest.hexdigest()[:16]}"
    if binary.is_file():
        return binary
    binary.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="deepthought-deployfacts-") as scratch:
        for name, content in sources.items():
            (Path(scratch) / name).write_bytes(content)
        output = Path(scratch) / "deployfacts"
        try:
            subprocess.run(
                [go, "mod", "download"],
                cwd=scratch,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
                env=environment,
            )
        except subprocess.CalledProcessError as error:
            raise DeployModulesUnavailable(
                "could not download the deployment renderers' pinned modules: "
                + error.stderr[-2000:]
            ) from error
        try:
            subprocess.run(
                [go, "build", "-trimpath", "-o", str(output), "."],
                cwd=scratch,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
                env=environment,
            )
        except subprocess.CalledProcessError as error:
            raise DeployToolsMissing(
                "could not build the deployment renderers: " + error.stderr[-2000:]
            ) from error
        staged = binary.with_name(binary.name + f".{os.getpid()}")
        shutil.copy2(output, staged)
        os.replace(staged, binary)
    return binary


def _walk(root):
    """Repository-relative (directory, file names), skipping hidden and
    build directories."""
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in _SKIPPED)
        relative = Path(directory).relative_to(root).as_posix()
        yield ("" if relative == "." else relative), sorted(names)


def _join(directory, name):
    return f"{directory}/{name}" if directory else name


def requests(root):
    """What to render: Compose projects, charts and kustomizations."""
    found, charts = [], set()
    for directory, names in _walk(root):
        compose = [n for n in names if COMPOSE_FILE.match(n)]
        if compose:
            # The helper splits them into projects as docker compose does.
            found.append(
                {
                    "kind": "compose",
                    "directory": directory,
                    "files": [_join(directory, f) for f in compose],
                }
            )
        if "Chart.yaml" in names:
            charts.add(directory)
        kustomization = next((n for n in _KUSTOMIZATION if n in names), None)
        if (
            kustomization is not None
            and _kind_of(Path(root), _join(directory, kustomization)) != "Component"
        ):
            found.append(
                {
                    "kind": "kustomize",
                    "directory": directory,
                    "file": _join(directory, kustomization),
                }
            )
    for chart in sorted(charts):
        parts = PurePosixPath(chart).parts
        if any("/".join(parts[:end]) in charts for end in range(len(parts))):
            continue  # a subchart, rendered with its parent
        found.append({"kind": "helm", "directory": chart})
    return found


def _signature(root):
    """Sizes and modification times of the files ``requests`` would read."""
    items = []
    for directory, names in _walk(root):
        for name in names:
            if name.endswith((".yaml", ".yml", ".env", ".tpl")) or name in (
                "Kustomization",
                ".env",
            ):
                stat = (Path(root) / directory / name).stat()
                items.append((_join(directory, name), stat.st_size, stat.st_mtime_ns))
    return tuple(items)


def collect(root, *, timeout=1800):
    """DeployFacts of the repository at ``root``."""
    root = Path(root).resolve()
    return _collect(str(root), _signature(root), timeout)


@lru_cache(maxsize=8)
def _collect(root, signature, timeout):
    import yaml

    facts = DeployFacts()
    wanted = requests(Path(root))
    if not wanted:
        return facts
    for item in wanted:
        if item["kind"] == "helm":
            facts.charts.add(item["directory"])
        elif item["kind"] == "kustomize":
            facts.kustomizations.add(item["directory"])
    run = subprocess.run(
        [str(build()), root],
        input="".join(
            json.dumps({k: v for k, v in item.items() if k != "file"}) + "\n"
            for item in wanted
        ),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    files = {(item["kind"], item["directory"]): item.get("file") for item in wanted}
    for line in run.stdout.splitlines():
        item = json.loads(line.replace(_HELPER_PLACEHOLDER, "\\u0000"))
        request = item.get("request") or {}
        kind, directory = request.get("kind"), request.get("directory", "")
        if item["kind"] == "error":
            if kind == "compose" and "empty compose file" in item["message"]:
                continue
            described = (request.get("files") or [directory])[0]
            facts.errors.append((described, kind, item["message"]))
            if kind == "kustomize":
                facts.unrendered.add(directory)
        elif item["kind"] == "compose":
            facts.compose.append(_project(item))
        else:
            source = (
                _join(directory, "Chart.yaml")
                if kind == "helm"
                else files[(kind, directory)]
            )
            try:
                documents = tuple(
                    d for d in yaml.safe_load_all(item.get("manifests", "")) if d
                )
            except yaml.YAMLError as error:
                facts.errors.append((source, kind, str(error)))
                continue
            facts.renderings.append(Rendering(source, kind, documents))
            facts.inputs.update(item.get("inputs") or ())
    return facts


def _kind_of(root, file):
    """The kind a kustomization file declares ("" when unreadable): a
    ``Component`` is not rendered on its own."""
    if file is None:
        return ""
    try:
        import yaml

        data = yaml.safe_load((root / file).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - kustomize reports it when rendering
        return ""
    kind = data.get("kind") if isinstance(data, dict) else None
    return kind if isinstance(kind, str) else ""


def _project(item):
    services = []
    for s in item.get("services") or ():
        environment = s.get("environment") or {}
        services.append(
            ComposeService(
                s["name"],
                s.get("image") or None,
                s.get("context"),
                s.get("dockerfile"),
                tuple(s.get("depends_on") or ()),
                tuple(sorted(environment.items())),
                tuple(
                    (v.get("source") if v.get("type") == "bind" else None, v["target"])
                    for v in s.get("volumes") or ()
                ),
                tuple(s.get("profiles") or ()),
            )
        )
    files = tuple(item.get("files") or item["request"]["files"])
    return ComposeProject(
        item["request"]["files"][0],
        files,
        tuple(services),
        tuple(item.get("missing") or ()),
        bool(item.get("unread_env_files")),
    )
