"""Installing the engine's pinned tools into its cache.

The engine needs some tools at exact versions: Vue's parser
(``@vue/compiler-sfc``), Jelly (``@cs-au-dk/jelly``) and the Go toolchain's
``callgraph`` (``golang.org/x/tools``). Each module that runs one looks for
it where it is configured or installed; when it is not there at the pinned
version, the engine installs it itself, with npm or ``go install``, into
``<cache>/tools`` (the cache is ``$DEEPTHOUGHT_CACHE``, else
``$XDG_CACHE_HOME/deepthought``, else ``~/.cache/deepthought``). An install
goes into a scratch directory beside its place and is renamed into place, so
a concurrent run never sees half of one. An install that cannot run (no npm,
no go, no network, a failed build) raises ``ToolInstallFailed``: the engine
never goes on without a tool it needs.
"""

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .prisma_schema import _cache_directory

INSTALL_TIMEOUT = 900
_VERSION = re.compile(r"v?[0-9A-Za-z.+-]+")


class ToolInstallFailed(RuntimeError):
    """A pinned tool could not be installed."""


def tools_directory(environ=None):
    environ = os.environ if environ is None else environ
    return _cache_directory(environ) / "tools"


def _install(target, ready, command, environ, what, extra=None):
    """Run ``command`` (a function of the scratch directory) and rename the
    scratch directory to ``target``; ``ready(directory)`` says whether an
    install is complete."""
    if ready(target):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = tempfile.mkdtemp(dir=target.parent, prefix=".install-")
    try:
        try:
            completed = subprocess.run(
                command(scratch),
                capture_output=True,
                text=True,
                timeout=INSTALL_TIMEOUT,
                env={**environ, **(extra(scratch) if extra else {})},
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ToolInstallFailed(f"installing {what} failed: {error}") from None
        if completed.returncode or not ready(Path(scratch)):
            raise ToolInstallFailed(
                f"installing {what} failed: "
                + (completed.stderr or completed.stdout).strip()[-600:]
            )
        try:
            os.replace(scratch, target)
        except OSError:
            # Another run installed it first.
            if not ready(target):
                raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return target


def npm_package(package, version, environ=None):
    """The directory holding ``node_modules/<package>`` at ``version``,
    installed with npm into the cache when missing."""
    environ = os.environ if environ is None else environ
    if not _VERSION.fullmatch(version):
        raise ToolInstallFailed(f"not a version: {version!r}")
    npm = shutil.which("npm", path=environ.get("PATH"))
    what = f"{package}@{version}"
    if npm is None:
        raise ToolInstallFailed(f"installing {what} needs npm, which is not on PATH")
    target = tools_directory(environ) / "npm" / package.replace("/", "__") / version

    def ready(directory):
        return (Path(directory) / "node_modules" / package / "package.json").is_file()

    return _install(
        target,
        ready,
        lambda scratch: [
            npm,
            "install",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "--no-save",
            "--no-package-lock",
            "--prefix",
            scratch,
            what,
        ],
        environ,
        what,
    )


def go_command(module, command, version, environ=None, go="go"):
    """The ``command`` binary built from ``module`` at ``version`` (``go
    install <module>/cmd/<command>@<version>``), installed into the cache
    when missing."""
    environ = os.environ if environ is None else environ
    if not _VERSION.fullmatch(version):
        raise ToolInstallFailed(f"not a version: {version!r}")
    found = shutil.which(go, path=environ.get("PATH"))
    what = f"{module}/cmd/{command}@{version}"
    if found is None:
        raise ToolInstallFailed(f"installing {what} needs go, which is not on PATH")
    target = (
        tools_directory(environ)
        / "go"
        / f"{module}/cmd/{command}".replace("/", "__")
        / version
    )
    name = command + (".exe" if os.name == "nt" else "")

    def ready(directory):
        return (Path(directory) / name).is_file()

    # GOBIN is the scratch directory; GOFLAGS is dropped, so the analysis
    # environment's flags (-mod=readonly) do not reach the install.
    environ = {k: v for k, v in environ.items() if k != "GOFLAGS"}
    return (
        _install(
            target,
            ready,
            lambda scratch: [found, "install", what],
            environ,
            what,
            extra=lambda scratch: {"GOBIN": scratch},
        )
        / name
    )
