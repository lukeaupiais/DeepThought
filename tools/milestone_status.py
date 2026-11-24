"""Report how far the ground-truth baseline is from a milestone's entry criteria.

Reads a milestone file (evals/milestones/*.json) and the committed baseline
(evals/ground_truth/baseline.json) and prints one line per measurable
criterion: impact recall/precision per corpus and service-pair recall/false
pairs per contract corpus. Held-out corpora, capabilities and graders are
listed as manual items; this tool does not judge them.

Usage:
  python tools/milestone_status.py evals/milestones/agent-ab-1.json \
      [--baseline evals/ground_truth/baseline.json] [--json OUT]

Exit status is 0 when every measurable criterion passes, 1 otherwise.
"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent


def _pairs(scores):
    """Sum service-pair counts over every kind scored for one contract corpus."""
    labelled = predicted = true_positive = 0
    for kind in scores.values():
        pairs = kind.get("service_pairs") if isinstance(kind, dict) else None
        if not pairs:
            continue
        labelled += pairs.get("labelled_in_scope", 0)
        predicted += pairs.get("predicted", 0)
        true_positive += pairs.get("true_positive", 0)
    return labelled, predicted, true_positive


def evaluate(milestone, baseline):
    rows = []
    for item in milestone.get("impact", []):
        impact = baseline.get(item["corpus"], {}).get("impact")
        if impact is None:
            rows.append(
                {
                    "criterion": f"impact {item['corpus']}",
                    "passed": False,
                    "detail": "no impact score in baseline",
                }
            )
            continue
        recall, precision = impact["recall"], impact["precision"]
        passed = recall >= item["min_recall"] and precision >= item["min_precision"]
        rows.append(
            {
                "criterion": f"impact {item['corpus']}",
                "passed": passed,
                "detail": f"recall {recall:.4f} (need {item['min_recall']}), "
                f"precision {precision:.4f} (need {item['min_precision']})",
            }
        )
    contracts = baseline.get("contracts", {})
    for item in milestone.get("contracts", {}).get("corpora", []):
        scores = contracts.get(item["corpus"])
        if scores is None:
            rows.append(
                {
                    "criterion": f"pairs {item['corpus']}",
                    "passed": False,
                    "detail": "no contract score in baseline",
                }
            )
            continue
        labelled, predicted, true_positive = _pairs(scores)
        false_pairs = predicted - true_positive
        recall = true_positive / labelled if labelled else 1.0
        passed = (
            recall >= item["min_pair_recall"] and false_pairs <= item["max_false_pairs"]
        )
        rows.append(
            {
                "criterion": f"pairs {item['corpus']}",
                "passed": passed,
                "detail": f"{true_positive}/{labelled} pairs (need "
                f"{item['min_pair_recall']}), {false_pairs} false",
            }
        )
    manual = ["held-out corpora: " + milestone.get("held_out", {}).get("rule", "")]
    manual += milestone.get("capabilities", [])
    return rows, manual


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("milestone", type=Path)
    parser.add_argument(
        "--baseline", type=Path, default=ROOT / "evals/ground_truth/baseline.json"
    )
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    milestone = json.loads(args.milestone.read_text())
    baseline = json.loads(args.baseline.read_text())
    rows, manual = evaluate(milestone, baseline)
    for row in rows:
        mark = "PASS" if row["passed"] else "FAIL"
        print(f"{mark}  {row['criterion']}: {row['detail']}")
    for item in manual:
        print(f"MANUAL  {item}")
    if args.json:
        args.json.write_text(json.dumps({"criteria": rows, "manual": manual}, indent=2))
    return 0 if all(row["passed"] for row in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
