"""Trusted graders for milestone agent-ab-1 (evals/milestones/agent-ab-1.json).

Each grader scores an actor's JSON answer against truth taken from the
ground-truth corpora (impact truth, contract labels, call truth, source at the
pin), never against engine output. Importing this module registers them with
``deepthought.evaluation``:

- ``affected-set-v1``: the locations a change affects (U1-U3).
- ``change-plan-v1``: must-edit and re-test sets of a planned change (P1-P3).
- ``change-check-v1``: the call sites or clients a patch misses (C1-C3).
- ``exact-fact-v1``: plain facts, for the control tasks (K1-K3).

A scenario's ``expected`` rows carry a ``role``. Locations are
``{"role", "file", "line"}`` with an optional ``span: [first, last]`` (a
function's lines) and ``label``. An answer location matches a truth location
in the same file when its line is in the span, or within ``tolerance`` lines
(``metadata.tolerance``, default 3) of the truth line; matching is one to one.
Rows with role ``unjudged`` (dependents the oracle cannot rule on) are neither
expected nor wrong, as in the ground-truth gate. Every grader reports
``recall``, ``precision``, ``f1`` and ``wrong_claims`` (answer items that match
no truth row), and passes only when the answer is exactly right.
"""

from deepthought.evaluation import Grader, register_grader

LOCATION_ROLES = {
    "affected-set-v1": {"affected"},
    "change-plan-v1": {"must_edit", "retest"},
    "change-check-v1": {"omission", "covered"},
}


def _location(row, where):
    if not isinstance(row, dict):
        raise ValueError(f"{where}: a location is an object")
    file, line = row.get("file"), row.get("line")
    if not isinstance(file, str) or not file or file.startswith("/") or ".." in file:
        raise ValueError(f"{where}: file must be a repository-relative path")
    if type(line) is not int or line < 1:
        raise ValueError(f"{where}: line must be a positive integer")
    span = row.get("span")
    if span is not None and not (
        isinstance(span, list)
        and len(span) == 2
        and all(type(x) is int for x in span)
        and span[0] <= line <= span[1]
    ):
        raise ValueError(f"{where}: span must be [first, last] around line")
    return file, line, tuple(span) if span else None


def _tolerance(scenario):
    value = scenario.get("metadata", {}).get("tolerance", 3)
    if type(value) is not int or value < 0:
        raise ValueError("metadata.tolerance must be a nonnegative integer")
    return value


def _validate_locations(name):
    allowed = LOCATION_ROLES[name] | {"unjudged"}

    def validate(scenario):
        _tolerance(scenario)
        seen, scored = set(), 0
        for row in scenario["expected"]:
            if not isinstance(row, dict) or row.get("role") not in allowed:
                raise ValueError(f"{name}: role must be one of {sorted(allowed)}")
            key = (row["role"], *_location(row, "expected")[:2])
            if key in seen:
                raise ValueError(f"{name}: duplicate truth row {key}")
            seen.add(key)
            scored += row["role"] != "unjudged" and row["role"] != "covered"
        if not scored:
            raise ValueError(f"{name}: truth needs at least one scored row")

    return validate


def _near(truth, file, line, tolerance):
    t_file, t_line, span = truth
    if t_file != file:
        return None
    if span and span[0] <= line <= span[1]:
        return 0
    distance = abs(t_line - line)
    return distance if distance <= tolerance else None


def _match(answers, truths, tolerance):
    """One-to-one matching, nearest first: {answer index: truth index}."""
    pairs = sorted(
        (distance, a, t)
        for a, (file, line, _) in enumerate(answers)
        for t, truth in enumerate(truths)
        if (distance := _near(truth, file, line, tolerance)) is not None
    )
    matched, used = {}, set()
    for _, a, t in pairs:
        if a not in matched and t not in used:
            matched[a] = t
            used.add(t)
    return matched


def _answer_locations(answer, key, invalid):
    rows = answer.get(key) if isinstance(answer, dict) else None
    if rows is None:
        invalid.append(f"answer needs a list {key}")
        return []
    if not isinstance(rows, list):
        invalid.append(f"{key} must be a list")
        return []
    found, seen = [], set()
    for i, row in enumerate(rows):
        try:
            item = _location(row, f"{key}[{i}]")
        except ValueError as exc:
            invalid.append(str(exc))
            continue
        if item[:2] not in seen:
            seen.add(item[:2])
            found.append(item)
    return found


def _ratio(a, b):
    return a / b if b else 0.0


def _f1(precision, recall):
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _set_score(answers, truths, unjudged, tolerance):
    """Matches of answers against truths; unmatched answers that hit an
    unjudged row are left out of precision."""
    matched = _match(answers, truths, tolerance)
    rest = [a for i, a in enumerate(answers) if i not in matched]
    neutral = _match(rest, unjudged, tolerance)
    wrong = [a for i, a in enumerate(rest) if i not in neutral]
    judged = len(matched) + len(wrong)
    precision = _ratio(len(matched), judged)
    recall = _ratio(len(set(matched.values())), len(truths))
    return {
        "expected_count": len(truths),
        "correct_count": len(matched),
        "unjudged_count": len(neutral),
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "wrong": [{"file": f, "line": ln} for f, ln, _ in wrong],
        "missing": [
            {"file": t[0], "line": t[1]}
            for i, t in enumerate(truths)
            if i not in set(matched.values())
        ],
    }


def _truth(scenario, role):
    return [
        _location(row, "expected")
        for row in scenario["expected"]
        if row["role"] == role
    ]


def _extra_keys(answer, allowed, invalid):
    if not isinstance(answer, dict):
        invalid.append("answer must be a JSON object")
    elif set(answer) - allowed - {"limitations"}:
        invalid.append(f"unknown answer fields {sorted(set(answer) - allowed)}")


def _score_affected(scenario, answer):
    invalid = []
    _extra_keys(answer, {"items"}, invalid)
    result = _set_score(
        _answer_locations(answer, "items", invalid),
        _truth(scenario, "affected"),
        _truth(scenario, "unjudged"),
        _tolerance(scenario),
    )
    result["wrong_claims"] = len(result["wrong"])
    result["invalid"] = invalid
    result["quality_pass"] = not invalid and result["f1"] == 1.0
    return result


def _score_plan(scenario, answer):
    invalid = []
    _extra_keys(answer, {"must_edit", "retest"}, invalid)
    tolerance = _tolerance(scenario)
    must, retest = _truth(scenario, "must_edit"), _truth(scenario, "retest")
    unjudged = _truth(scenario, "unjudged")
    edits = _answer_locations(answer, "must_edit", invalid)
    tests = _answer_locations(answer, "retest", invalid)
    edit = _set_score(edits, must, retest + unjudged, tolerance)
    # A function listed under either heading counts once over the union.
    union = _set_score(
        edits + [t for t in tests if t[:2] not in {e[:2] for e in edits}],
        must + retest,
        unjudged,
        tolerance,
    )
    test = _set_score(tests, retest, must + unjudged, tolerance)
    result = {
        "must_edit": edit,
        "retest_recall": test["recall"] if retest else None,
        "precision": union["precision"],
        "recall": union["recall"],
        "f1": union["f1"],
        "wrong": union["wrong"],
        "wrong_claims": len(union["wrong"]),
        "invalid": invalid,
    }
    result["quality_pass"] = not invalid and union["f1"] == 1.0 and edit["f1"] == 1.0
    return result


def _score_check(scenario, answer):
    invalid = []
    _extra_keys(answer, {"complete", "missed"}, invalid)
    complete = answer.get("complete") if isinstance(answer, dict) else None
    if type(complete) is not bool:
        invalid.append("answer needs complete: true or false")
    result = _set_score(
        _answer_locations(answer, "missed", invalid),
        _truth(scenario, "omission"),
        _truth(scenario, "unjudged"),
        _tolerance(scenario),
    )
    # Reporting a site the patch already updates is a false alarm too.
    result["false_alarms"] = len(result["wrong"])
    result["wrong_claims"] = result["false_alarms"]
    result["says_complete"] = complete
    result["invalid"] = invalid
    result["quality_pass"] = not invalid and complete is False and result["f1"] == 1.0
    return result


def _normal(value):
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        return tuple(sorted((k, _normal(v)) for k, v in value.items()))
    raise ValueError("fact values are strings, numbers, booleans or objects")


def _validate_facts(scenario):
    keys = set()
    for row in scenario["expected"]:
        if (
            not isinstance(row, dict)
            or row.get("role") != "fact"
            or not isinstance(row.get("key"), str)
            or "value" not in row
        ):
            raise ValueError("exact-fact-v1: rows are {role: fact, key, value}")
        if row["key"] in keys:
            raise ValueError("exact-fact-v1: duplicate key")
        keys.add(row["key"])
        values = row["value"] if isinstance(row["value"], list) else [row["value"]]
        optional = row.get("optional", [])
        if not isinstance(optional, list) or (optional and values is not row["value"]):
            raise ValueError("exact-fact-v1: optional values need a list fact")
        for value in values + optional:
            _normal(value)


def _score_facts(scenario, answer):
    invalid = []
    _extra_keys(answer, {"facts"}, invalid)
    facts = answer.get("facts") if isinstance(answer, dict) else None
    if not isinstance(facts, dict):
        invalid.append("answer needs an object facts")
        facts = {}
    correct = expected_units = answered_units = 0
    wrong, missing = [], []
    for row in scenario["expected"]:
        key, truth = row["key"], row["value"]
        many = isinstance(truth, list)
        expected = {_normal(v) for v in truth} if many else {_normal(truth)}
        expected_units += len(expected)
        if key not in facts:
            missing.append(key)
            continue
        given = facts[key]
        try:
            got = (
                {_normal(v) for v in given}
                if many and isinstance(given, list)
                else ({_normal(given)})
            )
        except (ValueError, TypeError):
            invalid.append(f"facts.{key} has an unsupported value")
            continue
        # Values the truth marks optional are neither right nor wrong.
        got -= {_normal(v) for v in row.get("optional", [])} - expected
        answered_units += len(got)
        correct += len(expected & got)
        if got != expected:
            wrong.append(key)
    precision = _ratio(correct, answered_units)
    recall = _ratio(correct, expected_units)
    return {
        "expected_count": expected_units,
        "correct_count": correct,
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "wrong": wrong,
        "wrong_claims": answered_units - correct,
        "missing": missing,
        "invalid": invalid,
        "quality_pass": not invalid and not wrong and not missing,
    }


LOCATION_FORMAT = '{"file": "repository-relative path", "line": 1-based line} objects'

register_grader(
    "affected-set-v1",
    Grader(
        _validate_locations("affected-set-v1"),
        _score_affected,
        'Return one JSON object {"items": [...], "limitations": [strings]}. '
        f"items are {LOCATION_FORMAT}: for a function, the line of its "
        "declaration; for a call site, client or handler, the line of the call "
        "or handler. List each location once. Only list locations you have "
        "established from the code; every wrong item lowers the score.",
    ),
)
register_grader(
    "change-plan-v1",
    Grader(
        _validate_locations("change-plan-v1"),
        _score_plan,
        'Return one JSON object {"must_edit": [...], "retest": [...], '
        f'"limitations": [strings]}}. Both lists hold {LOCATION_FORMAT}, the '
        "line of each function's declaration (or of the call site or handler "
        "for cross-service tasks). must_edit: what has to change for the plan "
        "to work; retest: what else, up to three calls away, should be "
        "re-tested. List each location once, under one heading.",
    ),
)
register_grader(
    "change-check-v1",
    Grader(
        _validate_locations("change-check-v1"),
        _score_check,
        'Return one JSON object {"complete": true or false, "missed": [...], '
        f'"limitations": [strings]}}. missed holds {LOCATION_FORMAT}, one per '
        "call site or client the patch should have changed but did not, at the "
        "line in the unpatched source. Do not list sites the patch already "
        "changes.",
    ),
)
register_grader(
    "exact-fact-v1",
    Grader(
        _validate_facts,
        _score_facts,
        'Return one JSON object {"facts": {key: value}, "limitations": '
        "[strings]} with exactly the keys the task names. Values are strings, "
        "integers, or lists of them (or of objects) where the task asks for a "
        "list.",
    ),
)
