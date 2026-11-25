"""Compose services that build a module of the repository, as deployments.

Compose projects are what the Compose loader makes of the files
(``deploy_facts``: a file with its override, ``extends``, ``include`` and
``${VARIABLE}`` interpolation from ``.env``). A service whose ``build``
context is a directory of the repository runs the code built there. Its
``environment`` values are the loader's; a value holding a variable nothing
sets is unknown. Its bind-mounted ``volumes`` map container paths back to
repository directories.
"""

from dataclasses import dataclass

from .. import deploy_facts
from ..component_dependencies import join_directory

COMPOSE_FILE = deploy_facts.COMPOSE_FILE


@dataclass(frozen=True)
class ComposeUnit:
    file: str  # the Compose file, repository-relative
    name: str  # the Compose service name
    context: str  # the repository-relative build context ("" is the root)
    environment: tuple  # (name, value or None when not literal)
    volumes: tuple  # (repository directory or None, container path)
    dockerfile: str | None = None  # the repository-relative Dockerfile

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


def _known(value):
    return None if value is None or deploy_facts.PLACEHOLDER in value else value


def compose_units(root):
    """Every Compose service in the repository that builds a directory of it,
    in file and service order."""
    units = []
    for project in sorted(deploy_facts.collect(root).compose, key=lambda p: p.file):
        for service in project.services:
            if service.context is None or deploy_facts.PLACEHOLDER in service.context:
                continue
            dockerfile = service.dockerfile
            if dockerfile is not None and deploy_facts.PLACEHOLDER in dockerfile:
                dockerfile = None
            units.append(
                ComposeUnit(
                    project.file,
                    service.name,
                    service.context,
                    tuple((k, _known(v)) for k, v in service.environment),
                    tuple(
                        (
                            None
                            if source is None
                            or source.startswith(("/", "~"))
                            or deploy_facts.PLACEHOLDER in source
                            else source,
                            target,
                        )
                        for source, target in service.volumes
                    ),
                    dockerfile,
                )
            )
    return units
