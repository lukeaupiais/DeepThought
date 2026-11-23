"""Trusted reference-location grader for the public SCIP tool pilot."""

from deepthought.evaluation import Grader, register_grader
from deepthought.models import validate_location

KEYS = ("target", "file", "line", "column", "end_line", "end_column")


def fact(row):
    if not isinstance(row, dict) or set(row) != set(KEYS):
        raise ValueError(
            "Reference rows require target,file,line,column,end_line,end_column"
        )
    if not isinstance(row["target"], str) or not row["target"]:
        raise ValueError("Reference target must be a nonempty source API name")
    validate_location(
        row["file"],
        row["line"],
        row["column"],
        row["end_line"],
        row["end_column"],
        "utf-16",
    )
    return tuple(row[key] for key in KEYS)


def validate_expected(scenario):
    expected = [fact(row) for row in scenario["expected"]]
    if len(set(expected)) != len(expected):
        raise ValueError("Duplicate reference expectations")
    if any(
        row["file"] not in scenario["source"]["files"] for row in scenario["expected"]
    ):
        raise ValueError("Reference oracle escapes selected source")


def score(scenario, answer):
    expected = {fact(row) for row in scenario["expected"]}
    invalid = []
    observed = set()
    duplicates = []
    if (
        not isinstance(answer, dict)
        or set(answer) - {"references", "limitations"}
        or not isinstance(answer.get("references"), list)
    ):
        invalid.append("Answer requires references and optional limitations only")
    else:
        for row in answer["references"]:
            try:
                value = fact(row)
                if value in observed:
                    duplicates.append(row)
                observed.add(value)
            except (ValueError, TypeError) as exc:
                invalid.append(str(exc))
        limits = answer.get("limitations", [])
        if not isinstance(limits, list) or any(not isinstance(v, str) for v in limits):
            invalid.append("Limitations must be strings")

    def rows(values):
        return [dict(zip(KEYS, row)) for row in sorted(values)]

    return {
        "quality_pass": not (
            expected - observed or observed - expected or invalid or duplicates
        ),
        "correct_count": len(expected & observed),
        "expected_count": len(expected),
        "precision": len(expected & observed) / len(observed) if observed else 0.0,
        "recall": len(expected & observed) / len(expected),
        "missing": rows(expected - observed),
        "extra": rows(observed - expected),
        "invalid": invalid,
        "duplicates": duplicates,
    }


register_grader(
    "scip-reference-locations-v1",
    Grader(
        validate_expected,
        score,
        'Return JSON {"references":[{"target": "requested public API name", "file":"repository-relative path", '
        '"line":1,"column":0,"end_line":1,"end_column":1}],"limitations":["optional source-grounded qualifications"]}. '
        "Lines are 1-based; columns are 0-based UTF-16 code units; end positions are exclusive. "
        "Find semantic references to the requested public export, including import/re-export specifiers, "
        "and code/type uses that bind to it. Exclude its own declaration names, unrelated same-spelling "
        "symbols, comments and string examples. Every occurrence is a separate row. Reference facts "
        "do not establish execution or complete runtime impact.",
    ),
)
