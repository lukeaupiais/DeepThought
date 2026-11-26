"""Which repository paths hold tests or examples, by layout and naming.

An impact answer marks each dependent with these roles, and a caller may
leave the roles it does not want out. The roles come from where a file is
and what it is called, not from what it contains:

- ``test``: under a directory named ``test``, ``tests``, ``__tests__``,
  ``spec`` or ``testdata`` (which covers Maven and Gradle ``src/test/``), or
  in a .NET test project directory (``Foo.Tests/``, ``Foo.UnitTests/``), or
  named as a test by its language's convention: ``*_test.go``,
  ``test_*.py``, ``*_test.py``, ``*Test`` and ``*Tests`` Java, Kotlin and C#
  files, and ``*.test.*`` and ``*.spec.*`` JavaScript and TypeScript files.
  Go's ``testdata`` holds test inputs, so it counts as ``test`` here.
- ``example``: under a directory named ``example``, ``examples``,
  ``sample`` or ``samples``, or Go's ignored ``_example`` and ``_examples``.

Other directories (``benchmarks/``, ``runtime-tests/``) stay unclassified:
a role is given only where a layout or naming convention says so.

``ROLE_PATTERNS`` writes each rule as a regular expression that a whole path
matches (Neo4j's ``=~``), so a query can filter what ``source_roles``
classifies.
"""

import re

_TEST_DIRECTORIES = ("test", "tests", "__tests__", "spec", "testdata")
# Go ignores directories whose names start with "_", so chi and others keep
# examples that are not part of the module in "_examples".
_EXAMPLE_DIRECTORIES = (
    "example",
    "examples",
    "sample",
    "samples",
    "_example",
    "_examples",
)


def _directories(names):
    return "(?:.*/)?(?:" + "|".join(re.escape(n) for n in names) + ")/.*"


ROLE_PATTERNS = {
    "test": "|".join(
        (
            _directories(_TEST_DIRECTORIES),
            # .NET test projects: Foo.Tests/, Foo.UnitTests/, Foo.Test/
            r"(?:.*/)?[^/]*\.[A-Za-z]*Tests?/.*",
            r"(?:.*/)?[^/]*Tests?\.(?:java|kt|cs)",
            r".*_test\.go",
            r"(?:.*/)?test_[^/]*\.py",
            r".*_test\.py",
            r"(?:.*/)?[^/]*\.(?:test|spec)\.[cm]?[jt]sx?",
        )
    ),
    "example": _directories(_EXAMPLE_DIRECTORIES),
}
# A file a generator wrote, by Linguist's rules on its content (see
# generated_files); it has no path pattern.
GENERATED_ROLE = "generated"
ROLES = frozenset(ROLE_PATTERNS)


def source_roles(path):
    """The roles of a repository path, sorted; empty for other code."""
    if not path:
        return []
    return [
        role
        for role, pattern in sorted(ROLE_PATTERNS.items())
        if re.fullmatch(pattern, path)
    ]


def exclusion_pattern(roles):
    """One pattern a whole path matches when it has any of ``roles``; None
    when ``roles`` is None. Raises ValueError for anything else than a
    nonempty subset of ``ROLES``."""
    if roles is None:
        return None
    if isinstance(roles, str) or not isinstance(roles, (list, tuple, set, frozenset)):
        raise ValueError("exclude_roles must be a list of roles or None")
    selected = sorted(set(roles))
    if not selected or not set(selected) <= ROLES:
        raise ValueError(
            "exclude_roles must be a nonempty subset of "
            + ", ".join(sorted(ROLES | {GENERATED_ROLE}))
        )
    return "|".join(f"(?:{ROLE_PATTERNS[role]})" for role in selected)
