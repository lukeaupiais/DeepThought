"""Edge evidence contract: every stored relationship names the rule that made it.

A relationship records how its endpoints were linked, never how likely the link
is. Each rule declares one evidence type, so a tool cannot label a name match as
resolved edge by edge. Confidence or probability properties are rejected.

- ``resolved``: the link is exact (compiler/indexer symbol identity, or a
  structural fact such as a file containing a parsed declaration).
- ``pattern_matched``: a declared pattern matched (for example a route string).
- ``name_matched``: the link rests on equal names without proof of identity.
"""

import re
from dataclasses import dataclass

EVIDENCE_TYPES = frozenset({"resolved", "pattern_matched", "name_matched"})

# "<producer>.<rule>", lowercase, at least two dot-separated segments.
_RULE_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+(?:-[a-z0-9]+)*)+")

# Scores are not evidence; refuse them rather than store them beside it.
FORBIDDEN_PROPERTIES = frozenset({"confidence", "probability"})


@dataclass(frozen=True)
class EvidenceRule:
    rule_id: str
    evidence_type: str
    description: str

    def __post_init__(self):
        if not isinstance(self.rule_id, str) or not _RULE_ID.fullmatch(self.rule_id):
            raise ValueError(
                "rule_id must be lowercase '<producer>.<rule>' segments: "
                + repr(self.rule_id)
            )
        if self.evidence_type not in EVIDENCE_TYPES:
            raise ValueError(
                "evidence_type must be one of " + ", ".join(sorted(EVIDENCE_TYPES))
            )
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("Evidence rules need a description")


_RULES: dict[str, EvidenceRule] = {}


def register_rule(rule: EvidenceRule) -> EvidenceRule:
    """Register a rule once; an identical re-registration is a no-op."""
    if not isinstance(rule, EvidenceRule):
        raise TypeError("Expected an EvidenceRule")
    existing = _RULES.get(rule.rule_id)
    if existing is not None and existing != rule:
        raise ValueError("Conflicting definition for evidence rule " + rule.rule_id)
    _RULES[rule.rule_id] = rule
    return rule


def get_rule(rule_id: str) -> EvidenceRule:
    try:
        return _RULES[rule_id]
    except (KeyError, TypeError):
        raise ValueError("Unknown evidence rule: " + repr(rule_id)) from None


def registered_rules() -> dict[str, EvidenceRule]:
    return dict(sorted(_RULES.items()))


def validate_edge_evidence(properties) -> EvidenceRule:
    """Check one relationship's properties against the rule it names."""
    if not isinstance(properties, dict):
        raise TypeError("Relationship properties must be a dictionary")
    forbidden = sorted(FORBIDDEN_PROPERTIES & properties.keys())
    if forbidden:
        raise ValueError(
            "Relationships record evidence, not scores; remove " + ", ".join(forbidden)
        )
    if "evidence_type" not in properties or "rule_id" not in properties:
        raise ValueError("Relationships require evidence_type and rule_id properties")
    rule = get_rule(properties["rule_id"])
    if properties["evidence_type"] != rule.evidence_type:
        raise ValueError(
            f"Rule {rule.rule_id} declares {rule.evidence_type} evidence, "
            f"not {properties['evidence_type']!r}"
        )
    return rule
