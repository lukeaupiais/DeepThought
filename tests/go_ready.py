"""Skip for tests that need the deployment renderers built.

TEMPORARY: the tools are required (a missing Go toolchain raises
DeployToolsMissing); this skip exists only because the offline acceptance
sandbox has neither Go nor the renderers' pinned modules provisioned yet: Go
may be on its PATH, but with no network the first build cannot fetch the
modules. A build error once the modules are present still fails the tests.
Remove it when #323/#344 land.
"""

import functools
import shutil
import unittest

from deepthought import deploy_facts


@functools.lru_cache(maxsize=None)
def _unavailable():
    if not shutil.which("go"):
        return "the Go toolchain is not installed"
    try:
        deploy_facts.build()
    except deploy_facts.DeployModulesUnavailable as error:
        return str(error).splitlines()[0]
    return None


def needs_go(item):
    reason = _unavailable()
    return unittest.skip(reason)(item) if reason else item
