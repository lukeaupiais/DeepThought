"""Where the repository's code runs: Compose services and Kubernetes
workloads that build a directory of it, the host names they answer to and
the environment they set.

A Compose service builds its ``build`` context and answers to its own name
(``service_contracts.compose``). A Kubernetes container runs an image that a
Skaffold ``build.artifacts`` entry builds from one context; it answers to the
names of the Services in the same manifest directory and namespace whose
selector its pod template labels match. Environment values are literal text
(``value`` only, not ``valueFrom``); a value written with ``$`` is unknown.
Helm templates and other files that are not plain YAML are not read.
"""

from dataclasses import dataclass
from pathlib import PurePosixPath
import re

from ..component_dependencies import join_directory
from .compose import compose_units

_SKIPPED = frozenset({"node_modules", "target", "build", "vendor"})
_TEMPLATES = {
    "Deployment": ("spec", "template"),
    "StatefulSet": ("spec", "template"),
    "DaemonSet": ("spec", "template"),
    "ReplicaSet": ("spec", "template"),
    "Job": ("spec", "template"),
    "CronJob": ("spec", "jobTemplate", "spec", "template"),
    "Pod": (),
}


@dataclass(frozen=True)
class Deployed:
    label: str  # "file:name" (Compose) or "file:Kind/name" (Kubernetes)
    name: str  # the Compose service or workload name
    context: str  # the repository directory built ("" is the root)
    hosts: tuple  # host names it answers to
    environment: tuple  # (name, literal value or None)
    # A Kubernetes container's command and args: None when not set, () when
    # set but not a list of strings.
    command: tuple | None = None
    args: tuple | None = None

    def env(self, name):
        """The literal value, "" when unset, None when not literal."""
        for key, value in self.environment:
            if key == name:
                return value
        return ""


def _path(value, keys):
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _repository(image):
    """An image reference without tag or digest."""
    image = image.split("@", 1)[0]
    head, _, tail = image.rpartition("/")
    return (head + "/" if head else "") + tail.split(":", 1)[0]


def _container_env(container):
    pairs = []
    for item in container.get("env") or []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        value = item.get("value")
        if isinstance(value, (int, float, bool)):
            value = str(value).lower() if isinstance(value, bool) else str(value)
        if not isinstance(value, str) or "$" in value:
            value = None
        pairs.append((item["name"], value))
    return tuple(pairs)


def _strings(container, key):
    value = container.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return ()
    return tuple(value)


def _yaml_files(root):
    for path in sorted(root.rglob("*.y*ml")):
        relative = path.relative_to(root)
        if any(p.startswith(".") or p in _SKIPPED for p in relative.parts[:-1]):
            continue
        yield path, relative.as_posix()


def kubernetes_units(root):
    """Containers of Kubernetes workloads whose image a Skaffold artifact
    builds from exactly one context."""
    try:
        import yaml
    except ImportError:
        return []
    artifacts, workloads, services = {}, [], []
    for path, relative in _yaml_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "apiVersion" not in text or "{{" in text:
            continue
        try:
            documents = list(yaml.safe_load_all(text))
        except yaml.YAMLError:
            continue
        directory = str(PurePosixPath(relative).parent)
        directory = "" if directory == "." else directory
        queue = list(documents)
        while queue:
            document = queue.pop(0)
            if not isinstance(document, dict):
                continue
            kind = document.get("kind")
            if kind == "List" and isinstance(document.get("items"), list):
                queue[:0] = document["items"]
                continue
            version = document.get("apiVersion")
            if not isinstance(version, str):
                continue
            if kind == "Config" and version.startswith("skaffold/"):
                for artifact in _path(document, ("build", "artifacts")) or []:
                    if not isinstance(artifact, dict):
                        continue
                    image = artifact.get("image")
                    context = artifact.get("context", ".")
                    if not isinstance(image, str) or not isinstance(context, str):
                        continue
                    target = join_directory(directory, context)
                    if target is not None and "$" not in context:
                        artifacts.setdefault(_repository(image), set()).add(target)
                continue
            name = _path(document, ("metadata", "name"))
            if not isinstance(name, str) or not name:
                continue
            namespace = _path(document, ("metadata", "namespace"))
            namespace = namespace if isinstance(namespace, str) else None
            if kind == "Service":
                selector = _path(document, ("spec", "selector"))
                if isinstance(selector, dict) and selector:
                    services.append((directory, namespace, name, _pairs(selector)))
            elif kind in _TEMPLATES:
                template = (
                    document if kind == "Pod" else _path(document, _TEMPLATES[kind])
                )
                labels = _pairs(_path(template, ("metadata", "labels")))
                containers = _path(template, ("spec", "containers"))
                for container in containers if isinstance(containers, list) else []:
                    if isinstance(container, dict) and isinstance(
                        container.get("image"), str
                    ):
                        workloads.append(
                            (
                                relative,
                                directory,
                                namespace,
                                kind,
                                name,
                                labels,
                                container,
                            )
                        )
    units = []
    for relative, directory, namespace, kind, name, labels, container in workloads:
        contexts = artifacts.get(_repository(container["image"]), set())
        if len(contexts) != 1:
            continue
        hosts = sorted(
            service
            for where, space, service, selector in services
            if where == directory
            and space == namespace
            and set(selector) <= set(labels)
        )
        units.append(
            Deployed(
                f"{relative}:{kind}/{name}",
                name,
                next(iter(contexts)),
                tuple(hosts),
                _container_env(container),
                _strings(container, "command"),
                _strings(container, "args"),
            )
        )
    return units


def _pairs(mapping):
    if not isinstance(mapping, dict):
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in mapping.items() if v is not None))


def deployed_units(root):
    """Compose services and Kubernetes containers that build a directory of
    the repository."""
    units = [
        Deployed(unit.label, unit.name, unit.context, (unit.name,), unit.environment)
        for unit in compose_units(root)
    ]
    return units + kubernetes_units(root)


class Deployments:
    """The units that run each directory, and the services they name."""

    def __init__(self, units):
        self.units = units
        self._by_context = {}
        for unit in units:
            self._by_context.setdefault(unit.context, []).append(unit)

    def running(self, file):
        """The units built from the nearest directory above ``file`` that
        any unit builds."""
        parts = PurePosixPath(file).parent.parts
        for end in range(len(parts), -1, -1):
            found = self._by_context.get("/".join(parts[:end]))
            if found:
                return found
        return []

    def service(self, file):
        """The one name the units running ``file`` share, else None."""
        names = {u.name for u in self.running(file)}
        return next(iter(names)) if len(names) == 1 else None

    def hosts(self):
        """{host: service} for each host name that units of one service
        answer to; a host claimed by two services names neither."""
        claimed = {}
        for context, units in self._by_context.items():
            names = {u.name for u in units}
            if len(names) != 1:
                continue
            for unit in units:
                for host in unit.hosts:
                    claimed.setdefault(host, set()).add(next(iter(names)))
        return {h: next(iter(s)) for h, s in claimed.items() if len(s) == 1}


# -- Locust's --host -------------------------------------------------------

_ENTRYPOINT = re.compile(r"(?im)^\s*(ENTRYPOINT|CMD)\s+(.+?)\s*$")
_HOST_OPTION = re.compile(
    r"""(?:^|\s)(?:--host[=\s]|-H\s)\s*(?:"([^"]*)"|'([^']*)'|(\S+))"""
)
_VARIABLE = re.compile(r"\$\{(\w+)(?::?-[^}]*)?\}|\$(\w+)")


def _command_text(written):
    """A Dockerfile instruction's command: exec form (a JSON array) joined
    with spaces, shell form as written."""
    written = written.strip()
    if written.startswith("["):
        import json

        try:
            parts = json.loads(written)
        except ValueError:
            return None
        if not isinstance(parts, list) or not all(isinstance(p, str) for p in parts):
            return None
        return " ".join(parts)
    return written


def locust_host(command, unit):
    """The URL a ``locust`` command line's ``--host``/``-H`` gives, with
    ``${VAR}`` taken from the unit's environment; None when the command does
    not run locust, sets no host, or a variable is unset or not literal."""
    if command is None or not re.search(r"(?:^|[\s/])locust(?:\s|$)", command):
        return None
    match = _HOST_OPTION.search(command)
    if match is None:
        return None
    written = next(g for g in match.groups() if g is not None)
    unknown = False

    def substitute(found):
        nonlocal unknown
        value = unit.env(found.group(1) or found.group(2))
        if not value:
            unknown = True
            return ""
        return value

    host = _VARIABLE.sub(substitute, written)
    return None if unknown or not host else host


def _dockerfile(root, context):
    """{"ENTRYPOINT": text, "CMD": text} of the last of each instruction in
    ``context/Dockerfile`` (None for one not readable); None without a
    Dockerfile."""
    path = root / context / "Dockerfile" if context else root / "Dockerfile"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    text = re.sub(r"\\\n", " ", text)
    found = {}
    for match in _ENTRYPOINT.finditer(text):
        found[match.group(1).upper()] = _command_text(match.group(2))
    return found


def command_line(root, unit):
    """The command line the unit's container runs, as one string, or None
    when it cannot be read: a Kubernetes ``command`` replaces the image's
    ENTRYPOINT and CMD, ``args`` replaces its CMD; otherwise the Dockerfile
    of the unit's context gives them."""
    if unit.command == () or unit.args == ():
        return None
    if unit.command is not None:
        return " ".join(unit.command + (unit.args or ()))
    image = _dockerfile(root, unit.context)
    if image is None:
        return None
    entrypoint, cmd = image.get("ENTRYPOINT"), image.get("CMD")
    if unit.args is not None:
        cmd = " ".join(unit.args)
    if "ENTRYPOINT" in image and entrypoint is None:
        return None
    parts = [p for p in (entrypoint, cmd) if p]
    return " ".join(parts) if parts else None
