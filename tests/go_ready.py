"""Skip for tests that need the Go toolchain.

TEMPORARY: the tools are required (a missing Go toolchain raises
DeployToolsMissing); this skip exists only because the offline acceptance
sandbox does not provision Go yet. Remove it when #323/#344 land.
"""

import shutil
import unittest

needs_go = unittest.skipUnless(shutil.which("go"), "the Go toolchain is not installed")
