"""Which edge families a snapshot links for each language, and how.

One record per language analyzed and edge family (``CALLS``, ``IMPORTS``)
says whether a deterministic resolver (a compiler-backed index or a type
checker) decided that family in every file of the language, in some, or in
none, so a reader knows where the parser's name rules stand in and a source
search may still be needed.
"""

from dataclasses import dataclass

# A record's status.
LINKED = "linked"  # a deterministic resolver covers every file
PARTIAL = "partial"  # it covers some files
NOT_LINKED = "not_linked"  # it covers none
NO_SITES = "no_sites"  # the language has no site of the family

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
            "reason": self.reason,
        }


def edge_coverage(result, parser, calls, imports):
    """Records for ``result``'s analyzed languages.

    ``parser`` names the parser's rules (``tree-sitter@<version>``).
    ``calls`` and ``imports`` are triples of a family's sites ((id, file)),
    their resolutions (with ``status``) by site id, and the producer
    (``name@version``) of each site a deterministic resolver decided, by
    site id; a site with no producer was decided by the parser's rules.
    """
    languages = {}
    for coverage in result.coverage:
        if coverage.status != "failed":
            languages.setdefault(coverage.language, set()).add(coverage.file)
    records = []
    for family, (sites, statuses, producers) in (
        ("CALLS", calls),
        ("IMPORTS", imports),
    ):
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
            counts = {"linked": 0, "ambiguous": 0, "unresolved": 0}
            for s in own:
                counts[statuses[s]] += 1
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
                    **counts,
                )
            )
    return records
