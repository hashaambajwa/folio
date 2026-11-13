"""Pure, transparent evaluation aggregation and regression gates."""
from __future__ import annotations

import math
import statistics
from collections import Counter


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    index = (len(sorted_values) - 1) * fraction
    before, after = math.floor(index), math.ceil(index)
    return round(sorted_values[before] * (after - index) + sorted_values[after] * (index - before), 2)


def summarize(results: list[dict], expected_count: int | None = None) -> dict:
    count = len(results)
    passed = sum(r.get("status") == "passed" for r in results)
    actions = [a for result in results for a in result.get("actions", [])]
    assertions = [a for result in results for a in result.get("assertions", [])]
    latencies = [r["latency_ms"] for r in results if isinstance(r.get("latency_ms"), (int, float))]
    groups: dict[str, dict] = {}
    for category in sorted({r.get("category", "unknown") for r in results}):
        group = [r for r in results if r.get("category", "unknown") == category]
        groups[category] = {"total": len(group), "passed": sum(r.get("status") == "passed" for r in group)}
        groups[category]["pass_rate"] = round(groups[category]["passed"] / len(group), 4)
    return {
        "completed": count,
        "expected": expected_count,
        "complete": expected_count is None or count == expected_count,
        "passed": passed,
        "failed": count - passed,
        "task_success_rate": round(passed / count, 4) if count else None,
        "tool_success_rate": round(sum(a.get("status") == "success" for a in actions) / len(actions), 4) if actions else None,
        "assertion_pass_rate": round(sum(a.get("passed") is True for a in assertions) / len(assertions), 4) if assertions else None,
        "latency_ms": {
            "median": round(statistics.median(latencies), 2) if latencies else None,
            "p95": _percentile(latencies, .95),
            "mean": round(statistics.mean(latencies), 2) if latencies else None,
        },
        "failures_by_type": dict(sorted(Counter(
            a.get("error", "Unknown") for a in actions if a.get("status") != "success"
        ).items())),
        "categories": groups,
        "note": "Rates describe evaluated cases only. No LLM, token costs, model quality improvement or recovery is inferred.",
    }


def gate(summary: dict, *, min_success_rate: float = 1.0, require_complete: bool = True) -> tuple[bool, str]:
    if not 0 <= min_success_rate <= 1:
        raise ValueError("min_success_rate must be between 0 and 1")
    if require_complete and not summary["complete"]:
        return False, "Benchmark incomplete"
    rate = summary["task_success_rate"]
    if rate is None or rate < min_success_rate:
        return False, f"Task success {rate} below threshold {min_success_rate}"
    return True, "Passed"
