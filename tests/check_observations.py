"""Fixture acceptance checks for analyzer observations, not a public engine schema.

Run: python tests/check_observations.py observations.json --language python
Observations are a test-only projection with definitions (id/file/symbol/kind/line),
relationships (kind/source/target), unresolved (file/symbol/line), and coverage
(file/status). Endpoints contain file/symbol. Lines are one-based. Analyzer
adapters stay outside this test oracle; no private source import is required.
"""

import argparse
import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def validate(observed, expected):
    """Return acceptance failures; absence and ambiguity cannot pass as resolution."""
    required = {"definitions", "relationships", "unresolved", "coverage"}
    if not isinstance(observed, dict):
        return ["Observations must be an object"]
    if not required.issubset(observed):
        return [
            "Missing observation collections: "
            + ", ".join(sorted(required - observed.keys()))
        ]
    if any(not isinstance(observed[key], list) for key in required):
        return ["Observation collections must be lists"]
    if any(not isinstance(item, dict) for key in required for item in observed[key]):
        return ["Observation records must be objects"]

    failures = []
    definitions = observed["definitions"]
    identities = {}
    for definition in definitions:
        if not {"id", "file", "symbol", "kind", "line"}.issubset(definition):
            failures.append("Incomplete definition: " + repr(definition))
            continue
        identity = definition["id"]
        location = (definition["file"], definition["symbol"])
        if not identity:
            failures.append("Empty definition identity: " + repr(location))
        if identity in identities:
            failures.append("Repeated/colliding definition identity: " + str(identity))
        identities[identity] = location

    for definition in expected["definitions"]:
        matches = [
            item
            for item in definitions
            if all(item.get(k) == v for k, v in definition.items())
        ]
        if len(matches) != 1:
            failures.append(
                f"Definition needs one exact match, found {len(matches)}: {definition}"
            )

    relationships = observed["relationships"]

    def matches(actual, wanted):
        return all(actual.get(key) == value for key, value in wanted.items())

    for relation in expected["required_relationships"]:
        if not any(matches(item, relation) for item in relationships):
            failures.append("Missing relationship: " + repr(relation))
    for relation in expected["forbidden_relationships"]:
        if any(matches(item, relation) for item in relationships):
            failures.append("Forbidden relationship: " + repr(relation))
    for unresolved in expected["unresolved"]:
        if not any(matches(item, unresolved) for item in observed["unresolved"]):
            failures.append("Missing unresolved diagnostic: " + repr(unresolved))

    by_file = {}
    for record in observed["coverage"]:
        if not {"file", "status"}.issubset(record):
            failures.append("Incomplete coverage record: " + repr(record))
            continue
        if record["file"] in by_file:
            failures.append("Repeated coverage file: " + record["file"])
        by_file[record["file"]] = record["status"]
    for file in {d["file"] for d in expected["definitions"]}:
        if by_file.get(file) != "analyzed":
            failures.append("Missing/non-analyzed file coverage: " + file)
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    parser.add_argument("--language", required=True, choices=("python", "typescript"))
    args = parser.parse_args()
    expected = json.loads((FIXTURES / "expected.json").read_text())["languages"][
        args.language
    ]
    try:
        observed = json.loads(args.observations.read_text())
        failures = validate(observed, expected)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f"Invalid observations: {exc}\n")
    for failure in failures:
        print(failure)
    print(f"{args.language}: {len(failures)} acceptance failures")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
