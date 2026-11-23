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
