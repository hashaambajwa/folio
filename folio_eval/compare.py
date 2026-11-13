"""Compare independently measured benchmark reports on identical datasets."""
from __future__ import annotations

import json
from pathlib import Path


def compare_reports(left: dict, right: dict) -> dict:
    if left.get("suite_sha256") != right.get("suite_sha256"):
        raise ValueError("Cannot compare reports from different benchmark suites")
    left_results = {r["case_id"]: r for r in left.get("results", [])}
    right_results = {r["case_id"]: r for r in right.get("results", [])}
    if left_results.keys() != right_results.keys():
        raise ValueError("Cannot compare incomplete or nonmatching case populations")
    if not left_results:
        raise ValueError("No cases to compare")
    improved = sorted(k for k in left_results
                      if left_results[k]["status"] != "passed" and right_results[k]["status"] == "passed")
    regressed = sorted(k for k in left_results
                       if left_results[k]["status"] == "passed" and right_results[k]["status"] != "passed")
    unchanged = len(left_results) - len(improved) - len(regressed)
    before = sum(r["status"] == "passed" for r in left_results.values())
    after = sum(r["status"] == "passed" for r in right_results.values())
    count = len(left_results)
    return {
        "suite_sha256": left["suite_sha256"],
        "cases": count,
        "before": {"passed": before, "success_rate": round(before / count, 4)},
        "after": {"passed": after, "success_rate": round(after / count, 4)},
        "change_percentage_points": round((after - before) / count * 100, 2),
        "improved_case_ids": improved,
        "regressed_case_ids": regressed,
        "unchanged_count": unchanged,
        "notes": "Paired outcome comparison, not a statistical significance claim or attribution.",
    }


def compare_paths(before: str | Path, after: str | Path) -> dict:
    left = json.loads(Path(before).read_text(encoding="utf-8"))
    right = json.loads(Path(after).read_text(encoding="utf-8"))
    return compare_reports(left, right)
