"""Services that Compose files declare.

Each Compose file is read on its own, as ``docker compose -f FILE`` would read
it: files combined with several ``-f`` options, ``extends`` and ``include``
are not merged, and ``${VARIABLE}`` interpolation is not performed (a build
context that needs it has no root here).
"""

from pathlib import PurePosixPath

from .component_dependencies import Dependency, join_directory
from .components import Component


def _yaml(text):
    try:
        import yaml
    except ImportError:
        raise ImportError("Provision PyYAML to read Compose files") from None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def _context(directory, build):
    """The repository-relative build context, or None when there is none here."""
    if isinstance(build, str):
        context = build
    elif isinstance(build, dict):
        context = build.get("context", ".")
    else:
        return None
    if not isinstance(context, str) or not context:
        return None
    # Remote contexts and interpolated paths have no directory here.
    if "$" in context or "://" in context or context.startswith("git@"):
        return None
    return join_directory(directory, context)


def extract_compose(manifest: str, content: bytes):
    """(services, depends_on declarations), or None when the file is not a
    Compose file with a ``services`` mapping."""
    try:
        data = _yaml(content.decode("utf-8"))
    except UnicodeDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        return None
    directory = str(PurePosixPath(manifest).parent)
    directory = "" if directory == "." else directory
    services, dependencies = [], []
    for name, service in sorted(data["services"].items()):
        if not isinstance(name, str) or not name.strip():
            return None
        service = service if isinstance(service, dict) else {}
        image = service.get("image")
        services.append(
            Component(
                "compose_service",
                manifest,
                name,
                context=_context(directory, service.get("build")),
                image=image if isinstance(image, str) and image.strip() else None,
            )
        )
        depends_on = service.get("depends_on")
        if isinstance(depends_on, dict):
            depends_on = list(depends_on)
        for target in depends_on if isinstance(depends_on, list) else []:
            if isinstance(target, str):
                dependencies.append(
                    Dependency(manifest, "depends_on", target, service=name)
                )
    return services, dependencies


def image_name(image):
    """The repository's last path segment, without tag or digest:
    ``registry:5000/team/api:1.2`` is ``api``."""
    image = image.split("@", 1)[0]
    last = image.rsplit("/", 1)[-1]
    return last.split(":", 1)[0] or None


BUILD_CONTEXT = "compose-build-context"
IMAGE_NAME = "compose-image-name"


def resolve_deployments(components):
    """(service, component, rule) for each package component a Compose
    service builds (its build context is the component's root) or, failing
    that, runs by image name (the image's last path segment equals the name of
    exactly one package component)."""
    from .components import PACKAGE_KINDS

    packages = [c for c in components if c.kind in PACKAGE_KINDS]
    by_root, by_name = {}, {}
    for component in packages:
        by_root.setdefault(component.root, []).append(component)
        if component.name:
            short = component.name.rsplit("/", 1)[-1]
            by_name.setdefault(short, []).append(component)
    found = []
    for service in components:
        if service.kind != "compose_service":
            continue
        if service.context is not None and service.context in by_root:
            for target in sorted(by_root[service.context], key=lambda c: c.manifest):
                found.append((service, target, BUILD_CONTEXT))
            continue
        if service.context is None and service.image:
            named = by_name.get(image_name(service.image), [])
            if len(named) == 1:
                found.append((service, named[0], IMAGE_NAME))
    return found


# Kubernetes kinds whose objects run pods, and where each keeps its pod
# template's metadata.
WORKLOAD_TEMPLATES = {
    "Deployment": ("spec", "template"),
    "StatefulSet": ("spec", "template"),
    "DaemonSet": ("spec", "template"),
    "ReplicaSet": ("spec", "template"),
    "Job": ("spec", "template"),
    "CronJob": ("spec", "jobTemplate", "spec", "template"),
    "Pod": (),
}
SERVICE_SELECTOR = "k8s-service-selector"
SKAFFOLD_CONTEXT = "k8s-skaffold-context"
K8S_IMAGE_NAME = "k8s-image-name"


def yaml_available():
    try:
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


def _path(value, keys):
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _pairs(mapping):
    if not isinstance(mapping, dict):
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in mapping.items() if v is not None))


def _documents(text):
    import yaml

    try:
        return list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        return None


def extract_kubernetes(manifest: str, content: bytes):
    """(objects, Skaffold artifacts) one YAML file declares, or None when it
    declares neither (or is not valid YAML).

    Objects are Kubernetes workloads (Deployment, StatefulSet, DaemonSet,
    ReplicaSet, Job, CronJob, Pod), named ``Kind/namespace/name`` or
    ``Kind/name``, and Services, named the same way; ``List`` items count.
    Skaffold ``Config`` documents give ``build.artifacts`` (image, context).
    """
    try:
        documents = _documents(content.decode("utf-8"))
    except UnicodeDecodeError:
        return None
    if documents is None:
        return None
    directory = str(PurePosixPath(manifest).parent)
    directory = "" if directory == "." else directory
    objects, artifacts = [], []
    queue = list(documents)
    while queue:
        document = queue.pop(0)
        if not isinstance(document, dict) or not isinstance(
            document.get("apiVersion"), str
        ):
            continue
        kind = document.get("kind")
        if kind == "List" and isinstance(document.get("items"), list):
            queue[:0] = document["items"]
            continue
        if kind == "Config" and document["apiVersion"].startswith("skaffold/"):
            for artifact in _path(document, ("build", "artifacts")) or []:
                if not isinstance(artifact, dict):
                    continue
                image, context = artifact.get("image"), artifact.get("context", ".")
                if isinstance(image, str) and isinstance(context, str):
                    target = join_directory(directory, context)
                    if target is not None and "$" not in context:
                        artifacts.append((image, target))
            continue
        name = _path(document, ("metadata", "name"))
        if not isinstance(name, str) or not name or "{{" in name:
            continue  # Unnamed, or an unrendered template.
        namespace = _path(document, ("metadata", "namespace"))
        namespace = namespace if isinstance(namespace, str) and namespace else None
        label = "/".join(p for p in (kind, namespace, name) if p)
        if kind == "Service":
            selector = _pairs(_path(document, ("spec", "selector")))
            objects.append(
                Component(
                    "k8s_service",
                    manifest,
                    label,
                    namespace=namespace,
                    selector=selector,
                )
            )
        elif kind in WORKLOAD_TEMPLATES:
            template = _path(document, WORKLOAD_TEMPLATES[kind]) or {}
            if kind == "Pod":
                template = document
            containers = []
            for key in ("initContainers", "containers"):
                found = _path(template, ("spec", key))
                containers += found if isinstance(found, list) else []
            images = sorted(
                {
                    c["image"]
                    for c in containers
                    if isinstance(c, dict) and isinstance(c.get("image"), str)
                }
            )
            objects.append(
                Component(
                    "k8s_workload",
                    manifest,
                    label,
                    namespace=namespace,
                    labels=_pairs(_path(template, ("metadata", "labels"))),
                    images=tuple(images),
                )
            )
    if not objects and not artifacts:
        return None
    return objects, artifacts


def _repository(image):
    """An image reference without tag or digest."""
    image = image.split("@", 1)[0]
    head, _, tail = image.rpartition("/")
    return (head + "/" if head else "") + tail.split(":", 1)[0]


def place_workloads(components, artifacts):
    """Give each workload the build context of the Skaffold artifacts its
    images name, when they name exactly one context."""
    from dataclasses import replace

    contexts = {}
    for image, context in artifacts:
        contexts.setdefault(_repository(image), set()).add(context)
    placed = []
    for component in components:
        if component.kind == "k8s_workload":
            found = set()
            for image in component.images:
                found |= contexts.get(_repository(image), set())
            if len(found) == 1:
                component = replace(component, context=found.pop())
        placed.append(component)
    return placed


def resolve_selectors(components):
    """(Service, workload) pairs: the Service's nonempty selector is a subset
    of the workload's pod template labels, in the same namespace as written,
    declared in the same directory (one set of manifests applied together;
    alternative sets in other directories are not mixed)."""
    services = [c for c in components if c.kind == "k8s_service" and c.selector]
    workloads = [c for c in components if c.kind == "k8s_workload"]
    found = []
    for service in services:
        for workload in workloads:
            if (
                service.namespace == workload.namespace
                and PurePosixPath(service.manifest).parent
                == PurePosixPath(workload.manifest).parent
                and set(service.selector) <= set(workload.labels)
            ):
                found.append((service, workload))
    return found


def resolve_workload_deployments(components):
    """(workload, package component, rule): the package whose root is the
    workload's Skaffold context (resolved), or else, for a workload with one
    image, the one package named like the image (name_matched)."""
    from .components import PACKAGE_KINDS

    packages = [c for c in components if c.kind in PACKAGE_KINDS]
    by_root, by_name = {}, {}
    for component in packages:
        by_root.setdefault(component.root, []).append(component)
        if component.name:
            by_name.setdefault(component.name.rsplit("/", 1)[-1], []).append(component)
    found = []
    for workload in components:
        if workload.kind != "k8s_workload":
            continue
        if workload.context is not None:
            for target in sorted(
                by_root.get(workload.context, []), key=lambda c: c.manifest
            ):
                found.append((workload, target, SKAFFOLD_CONTEXT))
            continue
        if len(workload.images) == 1:
            named = by_name.get(image_name(workload.images[0]), [])
            if len(named) == 1:
                found.append((workload, named[0], K8S_IMAGE_NAME))
    return found


# Images (last path segment, see image_name) of database servers whose
# tables or collections data contracts read.
DATABASE_IMAGES = frozenset(
    {
        "cockroach",
        "libsql-server",
        "mariadb",
        "mongo",
        "mysql",
        "percona-server",
        "postgis",
        "postgres",
        "timescaledb",
    }
)


def _environment(service):
    """The literal values of a service's ``environment`` (mapping or list
    form); ``env_file`` is not read."""
    environment = service.get("environment")
    if isinstance(environment, dict):
        values = environment.values()
    elif isinstance(environment, list):
        values = [
            item.split("=", 1)[1]
            for item in environment
            if isinstance(item, str) and "=" in item
        ]
    else:
        return []
    return [v for v in values if isinstance(v, str) and "$" not in v]


def _database_url(value):
    """(host, database) of a connection URL, or None. ``jdbc:`` is dropped
    first; the database is the first path segment ("" when there is none)."""
    from urllib.parse import urlsplit

    value = value.strip()
    if value.startswith("jdbc:"):
        value = value[len("jdbc:") :]
    if "://" not in value:
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        return None
    if not host:
        return None
    return host, parts.path.lstrip("/").split("/", 1)[0]


def compose_databases(manifest: str, content: bytes):
    """(build context, database key) for each Compose service that builds a
    context here and has an environment value that is a connection URL whose
    host is a service of the same file running a database image. The key
    names the Compose file's directory, the database service and the URL's
    database: ``compose:<dir>#<service>/<database>``. None when the file is
    not a Compose file."""
    try:
        data = _yaml(content.decode("utf-8"))
    except UnicodeDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        return None
    directory = str(PurePosixPath(manifest).parent)
    directory = "" if directory == "." else directory
    services = {
        n: s if isinstance(s, dict) else {} for n, s in data["services"].items()
    }
    databases = {
        name
        for name, service in services.items()
        if isinstance(service.get("image"), str)
        and image_name(service["image"]) in DATABASE_IMAGES
    }
    found = []
    for name, service in sorted(services.items(), key=lambda item: str(item[0])):
        context = _context(directory, service.get("build"))
        if context is None:
            continue
        for value in _environment(service):
            url = _database_url(value)
            if url is not None and url[0] in databases:
                found.append((context, f"compose:{directory or '.'}#{url[0]}/{url[1]}"))
    return sorted(set(found))
