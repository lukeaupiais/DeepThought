"""Compose services that build a module of the repository, as deployments.

Each Compose file is one deployment, read on its own as ``docker compose -f
FILE`` would read it (no ``extends``, ``include`` or ``-f`` merging). A
service whose ``build`` context is a directory of the repository runs the
code built there. Its ``environment`` is read as literal text (a value
written with ``$`` is unknown), and so are its bind-mounted ``volumes``,
which map container paths back to repository directories: a relative source
is relative to the Compose file, and ``$PWD``/``${PWD}`` is taken to be the
Compose file's directory, where ``docker compose`` is run from.
"""

from dataclasses import dataclass
from pathlib import PurePosixPath
import re

from ..component_dependencies import join_directory

# compose.yaml, docker-compose.yml and variants such as docker-compose-kafka.yml
# or compose.override.yaml.
COMPOSE_FILE = re.compile(r"^(?:docker-)?compose(?:[.-][A-Za-z0-9_.-]+)?\.ya?ml$")
_SKIPPED = frozenset({"node_modules", "target", "build", "vendor"})


@dataclass(frozen=True)
class ComposeUnit:
    file: str  # the Compose file, repository-relative
    name: str  # the Compose service name
    context: str  # the repository-relative build context ("" is the root)
    environment: tuple  # (name, value or None when not literal)
    volumes: tuple  # (repository directory or None, container path)

    @property
    def label(self):
        return f"{self.file}:{self.name}"

    def env(self, name):
        """The literal value of an environment variable, "" when unset, None
        when it is set but not literal."""
        for key, value in self.environment:
            if key == name:
                return value
        return ""

    def host_path(self, container_path):
        """The repository path a container path is mounted from, or None."""
        best = None
        for source, target in self.volumes:
            target = target.rstrip("/") or "/"
            if container_path == target or container_path.startswith(
                target.rstrip("/") + "/"
            ):
                if best is None or len(target) > len(best[1]):
                    best = (source, target)
        if best is None or best[0] is None:
            return None
        rest = container_path[len(best[1]) :].strip("/")
        return join_directory(best[0], rest) if rest else best[0]


def _environment(value):
    pairs = []
    if isinstance(value, dict):
        items = value.items()
    elif isinstance(value, list):
        items = [
            item.split("=", 1) if "=" in item else (item, None)
            for item in value
            if isinstance(item, str)
        ]
    else:
        return ()
    for key, text in items:
        if not isinstance(key, str):
            continue
        if isinstance(text, bool):
            text = str(text).lower()
        elif isinstance(text, (int, float)):
            text = str(text)
        if not isinstance(text, str) or "$" in text:
            text = None
        pairs.append((key.strip(), text))
    return tuple(pairs)


def _source(directory, source):
    if not isinstance(source, str):
        return None
    for written in ("${PWD}", "$PWD"):
        if source == written or source.startswith(written + "/"):
            source = "." + source[len(written) :]
            break
    if "$" in source or source.startswith(("/", "~")):
        return None
    if not source.startswith("."):
        return None  # a named volume
    return join_directory(directory, source)


def _volumes(directory, value):
    found = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            parts = item.split(":")
            if len(parts) < 2:
                continue
            found.append((_source(directory, parts[0]), parts[1]))
        elif isinstance(item, dict) and item.get("type", "bind") == "bind":
            target = item.get("target")
            if isinstance(target, str):
                found.append((_source(directory, item.get("source")), target))
    return tuple(found)


def compose_units(root):
    """Every Compose service in the repository that builds a directory of it,
    in file and service order."""
    try:
        import yaml
    except ImportError:  # pyyaml comes with the languages extra
        return []
    units = []
    for path in sorted(root.rglob("*.y*ml")):
        relative = path.relative_to(root)
        if not COMPOSE_FILE.match(path.name) or any(
            p.startswith(".") or p in _SKIPPED for p in relative.parts[:-1]
        ):
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
            continue
        directory = str(PurePosixPath(relative.as_posix()).parent)
        directory = "" if directory == "." else directory
        for name, service in sorted(data["services"].items()):
            if not isinstance(name, str) or not isinstance(service, dict):
                continue
            build = service.get("build")
            context = build.get("context", ".") if isinstance(build, dict) else build
            if not isinstance(context, str) or "$" in context or "://" in context:
                continue
            context = join_directory(directory, context)
            if context is None:
                continue
            units.append(
                ComposeUnit(
                    relative.as_posix(),
                    name,
                    context,
                    _environment(service.get("environment")),
                    _volumes(directory, service.get("volumes")),
                )
            )
    return units
