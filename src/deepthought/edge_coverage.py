"""Which edge families a snapshot links for each language, and how.

One record per language analyzed and edge family (``CALLS``, ``IMPORTS``)
says whether a deterministic resolver (a compiler-backed index or a type
checker) decided that family in every file of the language, in some, or in
none, so a reader knows where the parser's name rules stand in and a source
search may still be needed.
"""

from dataclasses import dataclass, field

# A record's status.
LINKED = "linked"  # a deterministic resolver covers every file
PARTIAL = "partial"  # it covers some files
NOT_LINKED = "not_linked"  # it covers none
NO_SITES = "no_sites"  # the language has no site of the family

# Reasons a deterministic resolver gives for a site whose target it found
# outside the repository (a library, the standard library, another
# repository): the site is decided, it just links to nothing here.
EXTERNAL_REASONS = frozenset(
    {
        "scip_external",  # the SCIP index's symbol is another package's
        "pyright_external",  # pyright's definition is outside the workspace
        "compiler_external_module",  # the TypeScript compiler's module is
    }
)

# Why a record is not linked.
PARSER_RULES = "parser_rules"  # the parser's name rules decided the rest
NO_INDEX = "no_index"  # with no parser rule either: nothing decided them


@dataclass(frozen=True)
class EdgeCoverage:
    language: str
    family: str
    status: str
    resolver: tuple[str, ...]
    files: int
    files_linked: int
    linked: int
    ambiguous: int
    unresolved: int
    reason: str | None = None
    # Sites a resolver placed outside the repository; not in ``unresolved``.
    external: int = 0
    # The reasons of the ambiguous and unresolved sites, by count.
    unresolved_reasons: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "language": self.language,
            "family": self.family,
            "status": self.status,
            "resolver": list(self.resolver),
            "files": self.files,
            "files_linked": self.files_linked,
            "linked": self.linked,
            "ambiguous": self.ambiguous,
            "unresolved": self.unresolved,
            "external": self.external,
            "unresolved_reasons": dict(self.unresolved_reasons),
            "reason": self.reason,
        }


def edge_coverage(result, parser, calls, imports):
    """Records for ``result``'s analyzed languages.

    ``parser`` names the parser's rules (``tree-sitter@<version>``).
    ``calls`` and ``imports`` hold a family's sites ((id, file)), their
    resolution statuses by site id, the producer (``name@version``) of each
    site a deterministic resolver decided, by site id (a site with no
    producer was decided by the parser's rules), and optionally the reason
    of each site left unlinked, by site id.

    An unlinked site whose resolver gave an ``EXTERNAL_REASONS`` reason is
    counted as ``external``, not ``unresolved``: it calls or imports code
    outside the repository. ``unresolved_reasons`` counts the reasons of
    the remaining ambiguous and unresolved sites, so a reader can tell a
    library call from a gap in the repository's own code.
    """
    languages = {}
    for coverage in result.coverage:
        if coverage.status != "failed":
            languages.setdefault(coverage.language, set()).add(coverage.file)
    records = []
    for family, facts in (
        ("CALLS", calls),
        ("IMPORTS", imports),
    ):
        sites, statuses, producers = facts[:3]
        reasons = facts[3] if len(facts) > 3 else {}
        by_file = {}
        for site_id, file in sites:
            by_file.setdefault(file, []).append(site_id)
        for language, files in sorted(languages.items()):
            own = [s for f in sorted(files) for s in by_file.get(f, ())]
            decided = {
                f
                for f in files
                if by_file.get(f)
                and all(producers.get(s) is not None for s in by_file[f])
            }
            with_sites = {f for f in files if by_file.get(f)}
            resolver = {producers[s] for s in own if producers.get(s) is not None}
            if any(producers.get(s) is None for s in own):
                resolver.add(parser)
            counts = {"linked": 0, "ambiguous": 0, "unresolved": 0, "external": 0}
            gaps = {}
            for s in own:
                status = statuses[s]
                reason = reasons.get(s)
                if status != "linked" and reason in EXTERNAL_REASONS:
                    status = "external"
                elif status != "linked":
                    key = reason or "unknown"
                    gaps[key] = gaps.get(key, 0) + 1
                counts[status] += 1
            if not own:
                status, reason = NO_SITES, None
            elif decided == with_sites:
                status, reason = LINKED, None
            else:
                status = PARTIAL if decided else NOT_LINKED
                reason = PARSER_RULES
            records.append(
                EdgeCoverage(
                    language=language,
                    family=family,
                    status=status,
                    resolver=tuple(sorted(resolver)),
                    files=len(with_sites),
                    files_linked=len(decided),
                    reason=reason,
                    unresolved_reasons=dict(sorted(gaps.items())),
                    **counts,
                )
            )
    return records
