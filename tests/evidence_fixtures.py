"""Evidence rule for synthetic test edges; real tools register their own rules."""

from deepthought.analysis_tools.evidence import EvidenceRule, register_rule

FIXTURE_RULE = register_rule(
    EvidenceRule("test.fixture-edge", "resolved", "Edge supplied by a test fixture.")
)
FIXTURE_EVIDENCE = {"evidence_type": "resolved", "rule_id": FIXTURE_RULE.rule_id}
