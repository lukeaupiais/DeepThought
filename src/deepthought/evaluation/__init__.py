"""Public, offline tool evaluation; grading labels never enter actor contexts.

This file-handoff harness does not sandbox an agent or authenticate usage. Callers
must isolate actor inputs from the scenario oracle and supply real usage receipts
when reporting model performance. Scripted controls validate the harness only.
"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path, PurePosixPath
import re
import subprocess
import time


FACT_KEYS = ("file", "symbol", "kind", "line")
USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _digest(value):
    return sha256(_json(value).encode()).hexdigest()


def _relative(value):
    return (
        isinstance(value, str)
        and bool(value)
        and "\\" not in value
        and "\0" not in value
        and not PurePosixPath(value).is_absolute()
        and not re.match(r"^[A-Za-z]:", value)
        and all(p not in {"", ".", ".."} for p in value.split("/"))
        and not any(c in value for c in "*?[]")
    )


def _fact(value):
    if not isinstance(value, dict) or set(value) != set(FACT_KEYS):
        raise ValueError("Claims require exactly file, symbol, kind and line")
    if not _relative(value["file"]):
        raise ValueError("Claim file must be a canonical relative path")
    if any(
        not isinstance(value[k], str) or not value[k].strip()
        for k in ("symbol", "kind")
    ):
        raise ValueError("Claim symbol and kind must be nonempty strings")
    if type(value["line"]) is not int or value["line"] < 1:
        raise ValueError("Claim line must be a positive integer")
    return tuple(value[k] for k in FACT_KEYS)


@dataclass(frozen=True)
class Grader:
    """Trusted code registered by the harness owner, never loaded from a pack."""

    validate_expected: object
    score: object
    instructions: str


_GRADERS = {}


def register_grader(name, grader):
    """Register a versioned grader; scenario JSON cannot execute/import code."""
    if not isinstance(name, str) or not name.strip() or name in _GRADERS:
        raise ValueError("Invalid or duplicate grader identity")
    if (
        not isinstance(grader, Grader)
        or not callable(grader.validate_expected)
        or not callable(grader.score)
        or not isinstance(grader.instructions, str)
        or not grader.instructions.strip()
    ):
        raise ValueError("Grader requires validation, scoring and actor instructions")
    _GRADERS[name] = grader


def _grader(scenario):
    name = scenario.get("grader", "declarations-v1")
    if not isinstance(name, str) or name not in _GRADERS:
        raise ValueError("Unknown trusted grader")
    return _GRADERS[name]


def _validate_declarations(value):
    facts = [_fact(row) for row in value["expected"]]
    if len(set(facts)) != len(facts):
        raise ValueError("Duplicate expected facts")
    if any(row[0] not in value["source"]["files"] for row in facts):
        raise ValueError("Expected facts must reference manifest files")


def load_scenario(path):
    """Load a reviewed JSON scenario pack, without executing code or fetching data."""
    p = Path(path)
    if p.is_symlink() or not p.is_file() or p.stat().st_size > 1_000_000:
        raise ValueError("Scenario must be a regular JSON file <=1MB")
    return validate_scenario(json.loads(p.read_text()))


def validate_scenario(value):
    required = {
        "schema_version",
        "id",
        "tool",
        "task",
        "languages",
        "source",
        "expected",
    }
    if not isinstance(value, dict) or not required <= set(value):
        raise ValueError("Invalid scenario fields")
    if set(value) - required - {"metadata", "grader"}:
        raise ValueError("Unknown scenario fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Unsupported scenario version")
    for key in ("id", "tool", "task"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError("Scenario identifiers and task must be nonempty")
    langs = value["languages"]
    if (
        not isinstance(langs, list)
        or not langs
        or any(not isinstance(x, str) or not x for x in langs)
        or len(set(langs)) != len(langs)
    ):
        raise ValueError("Scenario languages must be unique strings")
    source = value["source"]
    if not isinstance(source, dict) or not {"kind", "files"} <= set(source):
        raise ValueError("Invalid source manifest")
    if set(source) - {"kind", "files", "repository", "revision"}:
        raise ValueError("Unknown source manifest fields")
    if source["kind"] not in {"synthetic", "public_repository"}:
        raise ValueError("Only synthetic or explicitly public sources are supported")
    if source["kind"] == "public_repository":
        if not re.fullmatch(
            r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?",
            source.get("repository", ""),
        ):
            raise ValueError("Public source requires a credential-free GitHub URL")
        if not re.fullmatch(r"[0-9a-f]{40}", source.get("revision", "")):
            raise ValueError("Public source requires a full pinned revision")
    files = source["files"]
    if not isinstance(files, dict) or not files or len(files) > 10_000:
        raise ValueError("Scenario needs a bounded nonempty source manifest")
    for name, digest in files.items():
        if (
            not _relative(name)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("Invalid source path or SHA256")
    if not isinstance(value["expected"], list) or not value["expected"]:
        raise ValueError("Independent expected facts must be nonempty")
    _grader(value).validate_expected(value)
    if "metadata" in value and not isinstance(value["metadata"], dict):
        raise ValueError("Metadata must be an object")
    _json(value)
    return deepcopy(value)


def _verify_source(scenario, workspace):
    root = Path(workspace).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Workspace must be an existing directory")
    source_bytes = 0
    for name, expected in scenario["source"]["files"].items():
        path = root
        for part in name.split("/"):
            path = path / part
            if path.is_symlink():
                raise ValueError("Manifest source paths cannot contain symlinks")
        if not path.is_file() or path.stat().st_size > 4_000_000:
            raise ValueError("Manifest source file missing or over4MB")
        with path.open("rb") as stream:
            content = stream.read(4_000_001)
        source_bytes += len(content)
        if len(content) > 4_000_000 or source_bytes > 100_000_000:
            raise ValueError("Selected source exceeds file or100MB aggregate budget")
        if sha256(content).hexdigest() != expected:
            raise ValueError("Source fingerprint mismatch: " + name)
    revision = None
    if scenario["source"]["kind"] == "public_repository":

        def git(*args, binary=False):
            result = subprocess.run(
                ["git", "--no-optional-locks", "-C", str(root), *args],
                check=True,
                capture_output=True,
                text=not binary,
                timeout=10,
            )
            return result.stdout if binary else result.stdout.strip()

        try:
            if Path(git("rev-parse", "--show-toplevel")).resolve() != root:
                raise ValueError("Public workspace must be the repository root")
            revision = git("rev-parse", "--verify", "HEAD")
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("Cannot verify public checkout revision") from exc
        if revision != scenario["source"]["revision"]:
            raise ValueError("Public checkout revision differs from pinned scenario")
        try:
            committed_bytes = 0
            for name, expected in scenario["source"]["files"].items():
                object_name = revision + ":" + name
                size = int(git("cat-file", "-s", object_name))
                committed_bytes += size
                if size > 4_000_000 or committed_bytes > 100_000_000:
                    raise ValueError("Pinned source exceeds file or aggregate budget")
                content = git("cat-file", "blob", object_name, binary=True)
                if len(content) != size or sha256(content).hexdigest() != expected:
                    raise ValueError(
                        "Public source bytes differ from pinned commit: " + name
                    )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(
                "Selected public file is not a blob in the pinned commit"
            ) from exc
    return {
        "workspace": str(root),
        "files_sha256": _digest(scenario["source"]["files"]),
        "revision": revision,
    }


def prepare(scenario, workspace, *, registry=None):
    """Run a registered tool and prepare separate actor contexts plus private oracle.

    The returned preparation contains private grading material. Give actors ONLY
    one member of contexts. This separation is not an OS security boundary.
    """
    scenario = (
        load_scenario(scenario)
        if isinstance(scenario, (str, Path))
        else validate_scenario(scenario)
    )
    before = _verify_source(scenario, workspace)
    if registry is None:
        from deepthought.analysis_tools import tool_registry

        registry = tool_registry
    tool = registry.get_tool(scenario["tool"], Path(before["workspace"]))
    if tool is None:
        raise ValueError("Unknown analysis tool: " + scenario["tool"])
    if any(not tool.supports_language(x) for x in scenario["languages"]):
        raise ValueError("Scenario requests an unsupported tool language")
    started = time.monotonic()

    async def run_tool():
        try:
            if not await tool.check_availability():
                raise ValueError("Requested analysis tool is unavailable")
            return await tool.analyze(
                scenario["source"].get("repository", "synthetic://" + scenario["id"]),
                languages=set(scenario["languages"]),
                file_patterns=list(scenario["source"]["files"]),
            )
        finally:
            await tool.cleanup()

    output = asyncio.run(run_tool())
    data = output.to_dict()
    _json(data)
    after = _verify_source(scenario, workspace)
    if before != after:
        raise ValueError("Source changed during tool preparation")
    metrics = data.get("metadata", {}).get("custom_metrics", {})
    status = metrics.get("analysis_status", "unknown")
    model_tokens = metrics.get("model_tokens")
    if model_tokens is not None and (type(model_tokens) is not int or model_tokens < 0):
        raise ValueError("Tool model_tokens must be a nonnegative integer or unknown")
    common = {
        "schema_version": 1,
        "scenario_id": scenario["id"],
        "task": scenario["task"],
        "workspace": before["workspace"],
        "files": sorted(scenario["source"]["files"]),
        "instructions": "Read source in place without changing files or running builds. "
        + _grader(scenario).instructions,
    }
    contexts = {
        "baseline": {**common, "condition": "baseline"},
        "treatment": {**common, "condition": "treatment", "tool_output": data},
    }
    provenance = {
        "source": before,
        "scenario_sha256": _digest(scenario),
        "tool": {"name": tool.metadata.name, "version": tool.metadata.version},
        "tool_output_sha256": _digest(data),
        "tool_status": status,
        "actor_context_sha256": {
            arm: _digest(context) for arm, context in contexts.items()
        },
    }
    return {
        "schema_version": 1,
        "scenario": scenario,
        "provenance": provenance,
        "input_id": _digest(provenance),
        "contexts": contexts,
        "preparation": {
            "wall_seconds": time.monotonic() - started,
            "model_tokens": model_tokens,
        },
    }


def _grade_declarations(scenario, answer):
    """Compare declaration claims to independently reviewed labels."""
    expected = {_fact(row) for row in scenario["expected"]}
    invalid = []
    duplicates = []
    observed = set()
    relationships = []
    if not isinstance(answer, dict) or set(answer) - {
        "claims",
        "relationships",
        "limitations",
    }:
        invalid.append("Answer must contain only claims, relationships and limitations")
    if not isinstance(answer, dict) or not isinstance(answer.get("claims"), list):
        invalid.append("Answer claims must be a list")
    else:
        for row in answer["claims"]:
            try:
                fact = _fact(row)
                if fact in observed:
                    duplicates.append(row)
                observed.add(fact)
            except ValueError as exc:
                invalid.append(str(exc))
        rels = answer.get("relationships", [])
        if not isinstance(rels, list):
            invalid.append("Relationships must be a list")
        else:
            relationships = rels
        limits = answer.get("limitations", [])
        if not isinstance(limits, list) or any(not isinstance(x, str) for x in limits):
            invalid.append("Limitations must be strings")

    def rows(facts):
        return [dict(zip(FACT_KEYS, fact)) for fact in sorted(facts)]

    missing, extra = expected - observed, observed - expected
    return {
        "quality_pass": not (
            missing or extra or invalid or duplicates or relationships
        ),
        "missing": rows(missing),
        "extra": rows(extra),
        "invalid": invalid,
        "duplicates": duplicates,
        "unsupported_relationships": relationships,
        "expected_count": len(expected),
        "correct_count": len(expected & observed),
        "precision": len(expected & observed) / len(observed) if observed else 0.0,
        "recall": len(expected & observed) / len(expected),
    }


register_grader(
    "declarations-v1",
    Grader(
        validate_expected=_validate_declarations,
        score=_grade_declarations,
        instructions=(
            "Return JSON with claims [{file,symbol,kind,line}], relationships [] "
            "and optional limitations. Use these exact case-sensitive kind labels: "
            "Class, Function, Method, Interface, Enum, TypeAlias, Namespace. "
            "For Python, a named function directly in a class's lexical scope is "
            "Method; a function nested inside another function or method is "
            "Function. Async declarations use the same Function or Method labels, "
            "not separate async kinds. For TypeScript, method declarations and "
            "signatures are Method; named functions and identifier-bound arrow "
            "functions are Function. Join enclosing named declarations and the "
            "declaration name with dots for symbol. Use the 1-based start line of "
            "the declaration syntax, excluding decorators; for an identifier-bound "
            "arrow function use its variable declarator's start line. Report "
            "declarations only; no inferred calls or execution claims."
        ),
    ),
)


def grade(scenario, answer):
    """Use the independently registered grader, never caller-provided pass flags."""
    scenario = validate_scenario(scenario)
    result = _grader(scenario).score(scenario, deepcopy(answer))
    if not isinstance(result, dict) or type(result.get("quality_pass")) is not bool:
        raise ValueError("Grader must return an explicit boolean quality_pass")
    _json(result)
    return result


def _usage(usage):
    if usage is not None and not isinstance(usage, dict):
        raise ValueError("Usage must be an object or unknown")
    usage = usage or {}
    if set(usage) - set(USAGE_KEYS):
        raise ValueError("Unknown usage fields")
    result = {}
    for key in USAGE_KEYS:
        value = usage.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("Token counts must be nonnegative integers or unknown")
        result[key] = value
    for subset, total in [
        ("cached_input_tokens", "input_tokens"),
        ("reasoning_output_tokens", "output_tokens"),
    ]:
        if (
            result[subset] is not None
            and result[total] is not None
            and result[subset] > result[total]
        ):
            raise ValueError("Token subset exceeds total")
    return result


def finalize_record(
    prepared,
    arm,
    answer,
    *,
    usage=None,
    agent=None,
    execution=None,
    kind="scripted_control",
):
    """Grade first, then attach explicitly caller-reported telemetry; no billing inference."""
    if arm not in {"baseline", "treatment"} or kind not in {
        "agent",
        "scripted_control",
    }:
        raise ValueError("Unknown arm or execution kind")
    scenario = validate_scenario(prepared["scenario"])
    if (
        _digest(scenario) != prepared["provenance"]["scenario_sha256"]
        or _digest(prepared["provenance"]) != prepared["input_id"]
    ):
        raise ValueError("Preparation identity mismatch")
    contexts = prepared.get("contexts")
    if not isinstance(contexts, dict) or set(contexts) != {"baseline", "treatment"}:
        raise ValueError("Preparation needs both original actor contexts")
    for name, context in contexts.items():
        if _digest(context) != prepared["provenance"]["actor_context_sha256"].get(name):
            raise ValueError("Actor context changed after preparation")
    if (
        _digest(contexts["treatment"].get("tool_output"))
        != prepared["provenance"]["tool_output_sha256"]
    ):
        raise ValueError("Tool output differs from prepared identity")
    quality = grade(scenario, answer)
    source_ok, source_error = True, None
    try:
        _verify_source(scenario, prepared["provenance"]["source"]["workspace"])
    except (OSError, ValueError) as exc:
        source_ok, source_error = False, str(exc)
    agent = deepcopy(
        agent if agent is not None else {"model": "unknown", "settings": {}}
    )
    if (
        not isinstance(agent, dict)
        or set(agent) != {"model", "settings"}
        or not isinstance(agent["model"], str)
        or not agent["model"]
        or not isinstance(agent["settings"], dict)
    ):
        raise ValueError("Agent requires model and settings")
    if execution is None:
        execution = {
            "status": "unknown" if kind == "agent" else "completed",
            "wall_seconds": None,
        }
    if (
        not isinstance(execution, dict)
        or set(execution) != {"status", "wall_seconds"}
        or execution["status"]
        not in {
            "completed",
            "failed",
            "timed_out",
            "cancelled",
            "unknown",
        }
    ):
        raise ValueError("Invalid execution receipt")
    wall = execution["wall_seconds"]
    if wall is not None and (
        type(wall) not in {int, float} or not math.isfinite(wall) or wall < 0
    ):
        raise ValueError("Wall time must be finite, nonnegative or unknown")
    record = {
        "schema_version": 1,
        "arm": arm,
        "kind": kind,
        "input_id": prepared["input_id"],
        "provenance": deepcopy(prepared["provenance"]),
        "scenario": scenario,
        "actor_context_sha256": prepared["provenance"]["actor_context_sha256"][arm],
        "answer": deepcopy(answer),
        "quality": quality,
        "quality_sha256": _digest(quality),
        "agent": agent,
        "execution": deepcopy(execution),
        "usage": _usage(usage),
        "usage_provenance": "caller_reported_not_authenticated",
        "source_unchanged": source_ok,
        "source_error": source_error,
        "preparation": deepcopy(prepared["preparation"]),
    }
    record["record_id"] = _digest(record)
    return record


record = finalize_record


def _check_record(value):
    record = deepcopy(value)
    claimed = record.pop("record_id", None)
    if _digest(record) != claimed:
        raise ValueError("Record identity mismatch")
    if record["schema_version"] != 1:
        raise ValueError("Unsupported record version")
    scenario = validate_scenario(record["scenario"])
    if (
        _digest(scenario) != record["provenance"]["scenario_sha256"]
        or _digest(record["provenance"]) != record["input_id"]
    ):
        raise ValueError("Record provenance mismatch")
    if record["actor_context_sha256"] != record["provenance"][
        "actor_context_sha256"
    ].get(record["arm"]):
        raise ValueError("Record actor context identity mismatch")
    quality = grade(scenario, record["answer"])
    if quality != record["quality"] or _digest(quality) != record["quality_sha256"]:
        raise ValueError("Caller cannot override quality grading")
    _usage(record["usage"])
    return record


def compare(baseline_record, treatment_record):
    """Describe matched outcomes, retaining failures, unknowns and control-only runs."""
    b, t = _check_record(baseline_record), _check_record(treatment_record)
    if b["arm"] != "baseline" or t["arm"] != "treatment":
        raise ValueError("Pair requires baseline then treatment")
    if b["input_id"] != t["input_id"] or _digest(b["agent"]) != _digest(t["agent"]):
        raise ValueError("Pair has different inputs, model or settings")
    result = {
        "schema_version": 1,
        "status": "not_comparable",
        "token_reduction": None,
        "absolute_tokens_saved": None,
        "benefit_demonstrated": False,
        "usage_provenance": "caller_reported_not_authenticated",
        "arms": {},
        "limits": [
            "One descriptive pair is not statistical or enterprise validation.",
            "Actor context separation is not a sandbox or oracle confidentiality guarantee.",
            "Record hashes identify content, not authenticity; independent receipts/review remain required.",
            "Preparation time is separate and must be counted for cold-use conclusions.",
        ],
    }
    totals = []
    for name, item in [("baseline", b), ("treatment", t)]:
        u = item["usage"]
        total = (
            u["input_tokens"] + u["output_tokens"]
            if all(u[k] is not None for k in ("input_tokens", "output_tokens"))
            else None
        )
        complete = item["execution"]["status"] == "completed" and total is not None
        totals.append(total if complete else None)
        result["arms"][name] = {
            "quality": item["quality"],
            "execution": item["execution"],
            "usage": u,
            "total_tokens": total if complete else None,
            "reported_tokens": total,
            "primary_usage_complete": complete,
            "source_unchanged": item["source_unchanged"],
            "kind": item["kind"],
            "record_id": (baseline_record if name == "baseline" else treatment_record)[
                "record_id"
            ],
        }
    result["observed_pair_tokens"] = (
        sum(totals) if all(v is not None for v in totals) else None
    )
    result["known_reported_tokens"] = sum(
        row["reported_tokens"]
        for row in result["arms"].values()
        if row["reported_tokens"] is not None
    )
    result["known_reported_tokens_scope"] = (
        "Known caller-reported counts only; incomplete/unknown activity may add cost."
    )
    result["unknown_or_incomplete_usage"] = any(v is None for v in totals)
    result["preparation"] = t["preparation"]
    result["token_reduction_scope"] = "actor_input_plus_output_only"
    upstream = t["preparation"].get("model_tokens")
    if upstream is not None and (type(upstream) is not int or upstream < 0):
        raise ValueError("Invalid upstream model-token accounting")
    result["pipeline_tokens"] = {
        "baseline": totals[0],
        "treatment": totals[1] + upstream
        if totals[1] is not None and upstream is not None
        else None,
        "scope": "One cold preparation charged to treatment; actor usage remains caller-reported.",
    }
    if any(item["kind"] != "agent" for item in (b, t)):
        result["status"] = "scripted_control_only"
    elif any(item["execution"]["status"] != "completed" for item in (b, t)):
        result["status"] = "incomplete_execution"
    elif not all(
        item["quality"]["quality_pass"] and item["source_unchanged"] for item in (b, t)
    ):
        result["status"] = "quality_or_source_failure"
    elif t["provenance"]["tool_status"] != "succeeded":
        result["status"] = "incomplete_tool_output"
    elif b["agent"]["model"] == "unknown":
        result["status"] = "unknown_agent"
    elif any(v is None for v in totals):
        result["status"] = "unknown_usage"
    elif totals[0] == 0:
        result["status"] = "zero_baseline_usage"
    else:
        result["token_reduction"] = 1 - totals[1] / totals[0]
        result["absolute_tokens_saved"] = totals[0] - totals[1]
        result["status"] = (
            "observed_caller_reported_reduction"
            if totals[1] < totals[0]
            else "observed_caller_reported_no_reduction"
        )
    return result
