"""Falsify reference graders and compare locked gold with a fresh source oracle."""

import argparse
from copy import deepcopy
import json
from pathlib import Path

from scip_eval_grader import fact, score, validate_expected


def check(scenario, oracle):
    validate_expected(scenario)
    target = scenario["expected"][0]["target"]
    source = oracle["targets"][target]
    expected = {fact(row) for row in scenario["expected"]}
    actual = {
        fact(
            {"target": target, **{key: row[key] for key in row if key != "source_text"}}
        )
        for row in source["references"]
    }
    if actual != expected:
        raise ValueError("Fresh source oracle differs from frozen reference gold")
    selected = scenario["source"]["files"]
    if any(selected.get(name) != sha for name, sha in oracle["source_sha256"].items()):
        raise ValueError("Oracle source hashes differ from scenario inputs")
    good = {"references": deepcopy(scenario["expected"])}
    if not score(scenario, good)["quality_pass"]:
        raise ValueError("Perfect answer rejected")
    missing = deepcopy(good)
    missing["references"].pop()
    wrong = deepcopy(good)
    wrong["references"][0]["column"] += 1
    extra = deepcopy(good)
    extra["references"].append({"target": target, **source["definitions"][0]})
    duplicate = deepcopy(good)
    duplicate["references"].append(deepcopy(duplicate["references"][0]))
    invalid = {"references": [{"target": target}]}
    controls = {
        "missing": missing,
        "wrong_location": wrong,
        "own_declaration_as_extra": extra,
        "duplicate": duplicate,
        "invalid": invalid,
    }
    rejected = {
        name: not score(scenario, answer)["quality_pass"]
        for name, answer in controls.items()
    }
    if not all(rejected.values()):
        raise ValueError("Grader accepted a falsification control")
    return {
        "target": target,
        "expected": len(expected),
        "source_oracle_matches": True,
        "controls_rejected": rejected,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("oracle", type=Path)
    parser.add_argument("scenarios", type=Path, nargs="+")
    args = parser.parse_args()
    oracle = json.loads(args.oracle.read_text())
    print(
        json.dumps(
            [check(json.loads(path.read_text()), oracle) for path in args.scenarios],
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
