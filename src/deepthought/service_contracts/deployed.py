"""Where the repository's code runs: Compose services and Kubernetes
workloads that build a directory of it, the host names they answer to and
the environment they set.

A Compose service builds its ``build`` context and answers to its own name
(``service_contracts.compose``). A Kubernetes container runs an image that a
Skaffold ``build.artifacts`` entry builds from one context; it answers to the
names of the Services in the same manifest directory and namespace whose
selector its pod template labels match. Environment values are literal text
(``value`` only, not ``valueFrom``); a value written with ``$`` is unknown.
Helm charts and kustomizations are read as ``helm template`` and
``kustomize build`` render them (``deploy_facts``): the objects one
rendering gives count as one manifest directory, that of its Chart.yaml or
kustomization file, and the YAML files they are made from are not read on
their own.
"""

from dataclasses import dataclass
from pathlib import PurePosixPath
import re

from .. import deploy_facts
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
    rendered = deploy_facts.collect(root)
    sources = []
    for path, relative in _yaml_files(root):
        if rendered.tool_input(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "apiVersion" not in text or "{{" in text:
            continue
        try:
            sources.append((relative, list(yaml.safe_load_all(text))))
        except yaml.YAMLError:
            continue
    sources += [(r.source, list(r.documents)) for r in rendered.renderings]
    for relative, documents in sources:
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


_ASPIRE_HOST = "DistributedApplication.CreateBuilder"
_ASPIRE_PROJECT = re.compile(
    r"\.(AddProject|AddDotnetProject)\s*"
    r"(?:<\s*(?:global::)?Projects\.(\w+)\s*>)?\s*\(\s*"
    r'"([^"\\]+)"\s*(?:,\s*@?"([^"\\]+)")?\s*[,)]'
)
_CS_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


def _skipped(relative):
    return any(p.startswith(".") or p in _SKIPPED for p in relative.parts[:-1])


def aspire_units(root):
    """Projects a .NET Aspire AppHost (a C# file calling
    ``DistributedApplication.CreateBuilder``) adds by name.

    ``AddProject<Projects.X>("name")`` runs the one ``.csproj`` of the
    repository whose file name, with every character that is not a letter,
    digit or ``_`` replaced by ``_``, is ``X``. ``AddProject("name", "path")``
    and ``AddDotnetProject("name", "path")`` run the project at ``path`` (its
    directory or ``.csproj``), relative to the AppHost's directory. Each
    answers to its name; its environment is not read."""
    projects = None
    units = []
    for path in sorted(root.rglob("*.cs")):
        relative = path.relative_to(root)
        if _skipped(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _ASPIRE_HOST not in text:
            continue
        text = _CS_COMMENTS.sub("", text)
        directory = relative.parent.as_posix()
        directory = "" if directory == "." else directory
        for match in _ASPIRE_PROJECT.finditer(text):
            call, generated, name, written = match.groups()
            context = None
            if generated is not None:
                if call != "AddProject" or written is not None:
                    continue
                if projects is None:
                    projects = {}
                    for project in sorted(root.rglob("*.csproj")):
                        where = project.relative_to(root)
                        if _skipped(where):
                            continue
                        key = re.sub(r"\W", "_", project.stem)
                        folder = where.parent.as_posix()
                        projects.setdefault(key, []).append(
                            "" if folder == "." else folder
                        )
                found = projects.get(generated, [])
                if len(found) == 1:
                    context = found[0]
            elif written is not None:
                target = join_directory(directory, written.replace("\\", "/"))
                if target is not None and target.endswith(".csproj"):
                    target = target.rpartition("/")[0]
                if target is not None and (root / target).is_dir():
                    context = target
            if context is not None:
                units.append(
                    Deployed(
                        f"{relative.as_posix()}:{name}", name, context, (name,), ()
                    )
                )
    return units


def _instructions(text):
    """(INSTRUCTION, arguments) of each Dockerfile instruction, with line
    continuations joined and comments dropped."""
    text = re.sub(r"\\[ \t]*\r?\n", " ", text)
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            word, _, rest = line.partition(" ")
            yield word.upper(), rest.strip()


def _words(text):
    """Shell or JSON-array words of an instruction's arguments."""
    text = text.strip()
    if text.startswith("["):
        return re.findall(r'"((?:[^"\\]|\\.)*)"', text)
    return [w.strip("\"'") for w in text.split()]


def _image_path(workdir, path):
    """``path`` made absolute against ``workdir`` and normalized."""
    parts = [] if path.startswith("/") else workdir.strip("/").split("/")
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
        else:
            parts.append(part)
    return "/" + "/".join(p for p in parts if p)


def dotnet_project(root, context, dockerfile):
    """The repository directory of the one .NET project a Dockerfile
    publishes, or None.

    The Dockerfile is followed stage by stage: ``WORKDIR`` (a stage built
    ``FROM`` an earlier stage keeps its directory and files), and
    ``COPY S D`` (or ``ADD``) of one directory S of the build context (``.``
    for all of it) to image directory D. Each
    ``RUN dotnet publish [project]`` publishes the project named (a
    ``.csproj`` path, its file name possibly a ``*`` pattern) or the one in
    the working directory; that image path is mapped back to the repository
    through the copied context. Every publish must name the same directory,
    and it must hold a ``.csproj``. Visual Studio's Dockerfiles build from
    the solution directory, so the context alone does not say which project
    the image runs."""
    if dockerfile is None:
        return None
    try:
        # Instructions are ASCII; comments may be in another encoding.
        text = (root / dockerfile).read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return None
    stages, workdir, copied, found, current = {}, "/", (), set(), None
    base_dir = root / context if context else root
    for instruction, arguments in _instructions(text):
        words = _words(arguments)
        if instruction == "FROM":
            image = words[0] if words else ""
            workdir, copied = stages.get(image.lower(), ("/", ()))
            if len(words) >= 3 and words[1].upper() == "AS":
                stages[words[2].lower()] = (workdir, copied)
                current = words[2].lower()
            else:
                current = None
            continue
        if instruction == "WORKDIR" and words:
            workdir = _image_path(workdir, words[0])
        elif instruction in ("COPY", "ADD"):
            plain = [w for w in words if not w.startswith("--")]
            # COPY --from copies from another image, not the context.
            source = plain[0].strip("/").removeprefix("./") if plain else ""
            source = "" if source == "." else source
            if (
                len(plain) == 2
                and not any(w.startswith("--from") for w in words)
                and not any(c in source for c in "*?[$")
                and ".." not in source.split("/")
                and (base_dir / source).is_dir()
            ):
                copied = copied + ((source, _image_path(workdir, plain[1])),)
        elif instruction == "RUN":
            for match in re.finditer(r"\bdotnet\s+publish\b([^&;|]*)", arguments):
                given = [w.strip("\"'") for w in match.group(1).split()]
                projects = [w for w in given if w.endswith(".csproj")]
                if len(projects) > 1:
                    return None
                directory = (
                    _image_path(workdir, projects[0].rpartition("/")[0] or ".")
                    if projects
                    else workdir
                )
                mapped = [
                    (len(target), index, source, target)
                    for index, (source, target) in enumerate(copied)
                    if directory == target
                    or directory.startswith(target.rstrip("/") + "/")
                ]
                if "*" in directory or not mapped:
                    return None
                _, _, source, target = max(mapped)
                rest = directory[len(target.rstrip("/")) :].strip("/")
                found.add("/".join(p for p in (source, rest) if p))
        if current is not None:
            stages[current] = (workdir, copied)
    if len(found) != 1:
        return None
    inner = next(iter(found))
    directory = "/".join(p for p in (context, inner) if p)
    base = root / directory if directory else root
    if not base.is_dir() or not any(base.glob("*.csproj")):
        return None
    return directory


def deployed_units(root):
    """Compose services, Kubernetes containers and Aspire projects that
    build a directory of the repository. A Compose service whose Dockerfile
    publishes one .NET project runs that project's directory."""
    units = [
        Deployed(
            unit.label,
            unit.name,
            dotnet_project(root, unit.context, unit.dockerfile) or unit.context,
            (unit.name,),
            unit.environment,
        )
        for unit in compose_units(root)
    ]
    return units + kubernetes_units(root) + aspire_units(root)


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
