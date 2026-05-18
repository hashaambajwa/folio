from __future__ import annotations

import json
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VALIDATOR_VERSION = "0.1"
MIN_VIDEO_SIZE_BYTES = 10_000
MIN_VIDEO_DURATION_SECONDS = 8
MAX_VIDEO_DURATION_SECONDS = 180
MAX_SELECTED_PATHS = 8
MAX_SCENES = 45
MAX_RESET_SCENES = 6


def write_validation_report(
    run_paths: list[str | Path],
    output_path: str | Path | None = None,
    strict: bool = False,
) -> dict:
    report = validate_runs(run_paths, strict=strict)
    report_path = _default_report_path(run_paths, output_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report["artifacts"]["validation_json"] = str(report_path)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def validate_runs(run_paths: list[str | Path], strict: bool = False) -> dict:
    runs = [validate_run(run_path) for run_path in run_paths]
    summary = _summary_for_runs(runs, strict=strict)
    return {
        "version": VALIDATOR_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": summary["status"],
        "strict": strict,
        "summary": summary,
        "runs": runs,
        "artifacts": {
            "validation_json": None,
        },
    }


def validate_run(run_path: str | Path) -> dict:
    root = _run_root(run_path)
    artifacts = _artifact_paths(root)
    scan = _load_json(artifacts["scan_json"])
    plan = _load_json(artifacts["plan_json"])
    recording = _load_json(artifacts["recording_json"])
    render = _load_json(artifacts["render_json"])

    warnings: list[dict] = []
    failures: list[dict] = []
    metrics = {
        "scan": _scan_metrics(scan),
        "plan": _plan_metrics(plan),
        "recording": _recording_metrics(recording),
        "render": _render_metrics(render, artifacts["final_video"]),
    }

    _check_artifacts(artifacts, warnings, failures)
    _check_scan(metrics["scan"], warnings, failures)
    _check_plan(metrics["plan"], warnings, failures)
    _check_recording(metrics["recording"], warnings, failures)
    _check_render(metrics["render"], warnings, failures)
    _check_demo_shape(metrics, warnings)

    status = "failed" if failures else "warning" if warnings else "passed"
    return {
        "status": status,
        "job_id": _job_id(scan, plan, recording, render, root),
        "run_path": str(root),
        "source": {
            "url": _source_url(scan, plan, recording),
            "title": _source_title(scan, plan, recording),
        },
        "artifacts": {key: str(value) for key, value in artifacts.items()},
        "metrics": metrics,
        "warnings": warnings,
        "failures": failures,
    }


def _run_root(run_path: str | Path) -> Path:
    path = Path(run_path)
    if path.is_file():
        return path.parent
    return path


def _artifact_paths(root: Path) -> dict[str, Path]:
    return {
        "scan_json": root / "scan.json",
        "plan_json": root / "plan.json",
        "recording_json": root / "recording.json",
        "render_json": root / "render.json",
        "recording_video": root / "recording.webm",
        "final_video": root / "final.mp4",
    }


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"_load_error": "Invalid JSON"}


def _scan_metrics(scan: dict | None) -> dict:
    if not scan:
        return {"present": False}
    if scan.get("_load_error"):
        return {"present": True, "load_error": scan["_load_error"]}
    dom = scan.get("dom") or {}
    coverage_audit = scan.get("coverage_audit") or {}
    return {
        "present": True,
        "job_id": scan.get("job_id"),
        "state_count": len(scan.get("states") or []),
        "transition_count": len(scan.get("transitions") or []),
        "candidate_path_count": len(scan.get("candidate_paths") or []),
        "button_count": len(dom.get("buttons") or []),
        "input_count": len(dom.get("inputs") or []),
        "link_count": len(dom.get("links") or []),
        "form_count": len(dom.get("forms") or []),
        "coverage_audit_status": coverage_audit.get("status"),
        "coverage_audit_missing_feature_count": len(coverage_audit.get("missing_features") or []),
        "coverage_audit_resolved_missing_feature_count": len(coverage_audit.get("resolved_missing_features") or []),
        "coverage_audit_accepted_workflow_count": coverage_audit.get("accepted"),
        "coverage_audit_attempted_workflow_count": coverage_audit.get("attempted"),
        "source_context_status": (scan.get("source_context") or {}).get("status"),
    }


def _plan_metrics(plan: dict | None) -> dict:
    if not plan:
        return {"present": False}
    if plan.get("_load_error"):
        return {"present": True, "load_error": plan["_load_error"]}
    scenes = plan.get("scenes") or []
    coverage = (plan.get("planner") or {}).get("coverage") or {}
    scene_action_counts = [len(scene.get("actions") or []) for scene in scenes]
    action_type_counts = Counter(
        action.get("type")
        for scene in scenes
        for action in scene.get("actions") or []
        if action.get("type")
    )
    repeated_action_signatures = _repeated_action_signatures(scenes)
    reset_scenes = [scene for scene in scenes if _is_reset_scene(scene)]
    return {
        "present": True,
        "job_id": plan.get("job_id"),
        "mode": (plan.get("planner") or {}).get("mode"),
        "strategy": (plan.get("planner") or {}).get("strategy"),
        "scene_count": len(scenes),
        "action_count": sum(scene_action_counts),
        "action_type_counts": dict(sorted(action_type_counts.items())),
        "reset_scene_count": len(reset_scenes),
        "workflow_count": len({scene.get("workflow_index") for scene in scenes if scene.get("workflow_index")}),
        "coverage_status": coverage.get("status"),
        "candidate_path_count": coverage.get("candidate_path_count"),
        "selected_path_count": coverage.get("selected_path_count"),
        "missing_workflow_count": len(coverage.get("missing_workflows") or []),
        "uncovered_feature_count": len(coverage.get("uncovered_features") or []),
        "selected_path_ids": coverage.get("selected_path_ids") or (plan.get("planner") or {}).get("selected_path_ids") or [],
        "repeated_action_signatures": repeated_action_signatures,
    }


def _recording_metrics(recording: dict | None) -> dict:
    if not recording:
        return {"present": False}
    if recording.get("_load_error"):
        return {"present": True, "load_error": recording["_load_error"]}
    actions = recording.get("actions") or []
    action_status_counts = Counter(action.get("status") for action in actions)
    action_type_counts = Counter(action.get("type") for action in actions if action.get("type"))
    return {
        "present": True,
        "job_id": recording.get("job_id"),
        "status": recording.get("status"),
        "action_count": len(actions),
        "successful_action_count": action_status_counts.get("success", 0),
        "failed_action_count": sum(count for status, count in action_status_counts.items() if status != "success"),
        "action_status_counts": dict(sorted(action_status_counts.items())),
        "action_type_counts": dict(sorted(action_type_counts.items())),
        "failure": recording.get("failure"),
    }


def _render_metrics(render: dict | None, fallback_video_path: Path) -> dict:
    video_path = _render_video_path(render) or fallback_video_path
    size_bytes = video_path.stat().st_size if video_path and video_path.exists() else None
    duration_seconds = _video_duration_seconds(video_path) if video_path and video_path.exists() else None
    if not render:
        return {
            "present": False,
            "video_exists": bool(video_path and video_path.exists()),
            "video_path": str(video_path) if video_path else None,
            "size_bytes": size_bytes,
            "duration_seconds": duration_seconds,
        }
    if render.get("_load_error"):
        return {"present": True, "load_error": render["_load_error"]}
    return {
        "present": True,
        "job_id": render.get("job_id"),
        "status": render.get("status"),
        "video_exists": bool(video_path and video_path.exists()),
        "video_path": str(video_path) if video_path else None,
        "size_bytes": size_bytes or (render.get("output") or {}).get("size_bytes"),
        "duration_seconds": duration_seconds,
        "failure": render.get("failure"),
    }


def _render_video_path(render: dict | None) -> Path | None:
    if not render:
        return None
    video = (render.get("artifacts") or {}).get("video")
    return Path(video) if video else None


def _video_duration_seconds(video_path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    try:
        return round(float(completed.stdout.strip()), 3)
    except ValueError:
        return None


def _check_artifacts(artifacts: dict[str, Path], warnings: list[dict], failures: list[dict]) -> None:
    required_json = ("scan_json", "plan_json", "recording_json", "render_json")
    for key in required_json:
        if not artifacts[key].exists():
            failures.append(_issue("missing_artifact", f"Missing {key}: {artifacts[key]}"))
    if not artifacts["final_video"].exists():
        failures.append(_issue("missing_final_video", f"Missing final video: {artifacts['final_video']}"))
    if not artifacts["recording_video"].exists():
        warnings.append(_issue("missing_recording_video", f"Missing raw recording video: {artifacts['recording_video']}"))


def _check_scan(scan: dict, warnings: list[dict], failures: list[dict]) -> None:
    if not scan.get("present"):
        return
    if scan.get("load_error"):
        failures.append(_issue("invalid_scan_json", scan["load_error"]))
        return
    if scan.get("candidate_path_count", 0) == 0:
        failures.append(_issue("no_candidate_paths", "Scan produced no candidate paths."))
    elif scan.get("candidate_path_count", 0) < 3:
        warnings.append(_issue("few_candidate_paths", "Scan produced fewer than 3 candidate paths."))
    if scan.get("state_count", 0) <= 1 and scan.get("transition_count", 0) == 0:
        warnings.append(_issue("shallow_scan", "Scan did not discover post-interaction states."))
    if scan.get("coverage_audit_missing_feature_count", 0):
        warnings.append(
            _issue(
                "scan_missing_features",
                f"Coverage audit found {scan['coverage_audit_missing_feature_count']} missing feature(s).",
            )
        )


def _check_plan(plan: dict, warnings: list[dict], failures: list[dict]) -> None:
    if not plan.get("present"):
        return
    if plan.get("load_error"):
        failures.append(_issue("invalid_plan_json", plan["load_error"]))
        return
    if plan.get("scene_count", 0) == 0:
        failures.append(_issue("no_scenes", "Plan contains no scenes."))
    if plan.get("selected_path_count", 0) == 0:
        failures.append(_issue("no_selected_paths", "Plan selected no validated paths."))
    if plan.get("coverage_status") not in {None, "complete"}:
        warnings.append(_issue("partial_coverage", f"Coverage status is {plan.get('coverage_status')}."))
    if plan.get("missing_workflow_count", 0):
        warnings.append(_issue("missing_workflows", f"{plan['missing_workflow_count']} workflows remain missing."))
    if plan.get("uncovered_feature_count", 0):
        warnings.append(_issue("uncovered_features", f"{plan['uncovered_feature_count']} features remain uncovered."))
    if plan.get("selected_path_count", 0) > MAX_SELECTED_PATHS:
        warnings.append(_issue("many_selected_paths", f"{plan['selected_path_count']} selected paths may make the demo repetitive."))
    if plan.get("selected_path_count", 0) <= 1 and (plan.get("candidate_path_count") or 0) >= 8:
        warnings.append(_issue("narrow_plan", "Plan selected one or fewer paths despite many validated candidates."))
    if plan.get("scene_count", 0) > MAX_SCENES:
        warnings.append(_issue("many_scenes", f"{plan['scene_count']} scenes may make the demo too long."))
    if plan.get("reset_scene_count", 0) > MAX_RESET_SCENES:
        warnings.append(_issue("many_resets", f"{plan['reset_scene_count']} reset scenes may feel repetitive."))
    product_actions = sum(plan.get("action_type_counts", {}).get(action_type, 0) for action_type in ("click", "double_click", "fill", "press", "select"))
    if product_actions == 0:
        failures.append(_issue("no_product_actions", "Plan has no product interaction actions."))


def _check_recording(recording: dict, warnings: list[dict], failures: list[dict]) -> None:
    if not recording.get("present"):
        return
    if recording.get("load_error"):
        failures.append(_issue("invalid_recording_json", recording["load_error"]))
        return
    if recording.get("status") != "completed":
        failures.append(_issue("recording_failed", f"Recording status is {recording.get('status')}."))
    if recording.get("failed_action_count", 0):
        failures.append(_issue("failed_actions", f"{recording['failed_action_count']} recording actions failed."))
    if recording.get("action_count", 0) == 0:
        failures.append(_issue("no_recorded_actions", "Recording contains no actions."))
    elif recording.get("successful_action_count", 0) != recording.get("action_count", 0):
        warnings.append(_issue("partial_action_success", "Not every recorded action succeeded."))


def _check_render(render: dict, warnings: list[dict], failures: list[dict]) -> None:
    if render.get("present") and render.get("load_error"):
        failures.append(_issue("invalid_render_json", render["load_error"]))
        return
    if render.get("present") and render.get("status") != "completed":
        failures.append(_issue("render_failed", f"Render status is {render.get('status')}."))
    if not render.get("video_exists"):
        failures.append(_issue("video_missing", "Final video file does not exist."))
        return
    size_bytes = render.get("size_bytes") or 0
    if size_bytes < MIN_VIDEO_SIZE_BYTES:
        failures.append(_issue("video_too_small", f"Final video is only {size_bytes} bytes."))
    duration = render.get("duration_seconds")
    if duration is None:
        warnings.append(_issue("duration_unknown", "Could not determine final video duration."))
    elif duration < MIN_VIDEO_DURATION_SECONDS:
        warnings.append(_issue("video_too_short", f"Final video is only {duration} seconds."))
    elif duration > MAX_VIDEO_DURATION_SECONDS:
        warnings.append(_issue("video_too_long", f"Final video is {duration} seconds."))


def _check_demo_shape(metrics: dict, warnings: list[dict]) -> None:
    plan = metrics["plan"]
    recording = metrics["recording"]
    if not plan.get("present") or plan.get("load_error"):
        return

    repeated = [
        item
        for item in plan.get("repeated_action_signatures") or []
        if item.get("count", 0) >= 3 and item.get("type") in {"click", "fill", "press"}
    ]
    if repeated:
        warnings.append(
            _issue(
                "repeated_setup_actions",
                "Several workflows repeat the same setup actions; workflow chaining may improve demo polish.",
                details={"examples": repeated[:5]},
            )
        )

    if plan.get("workflow_count", 0) >= 3 and plan.get("reset_scene_count", 0) >= plan.get("workflow_count", 0) - 1:
        warnings.append(
            _issue(
                "reset_between_workflows",
                "Most workflows reset to a fresh state; this is valid but may feel less natural than a chained demo.",
            )
        )

    if recording.get("present"):
        navigate_count = recording.get("action_type_counts", {}).get("navigate", 0)
        if navigate_count > MAX_RESET_SCENES:
            warnings.append(_issue("many_recorded_navigations", f"Recording includes {navigate_count} navigation resets."))


def _repeated_action_signatures(scenes: list[dict]) -> list[dict]:
    counts = Counter()
    examples: dict[tuple, dict] = {}
    for scene in scenes:
        for action in scene.get("actions") or []:
            signature = (
                action.get("type"),
                action.get("selector"),
                str(action.get("value") or ""),
                str(action.get("key") or ""),
            )
            counts[signature] += 1
            examples.setdefault(signature, action)

    repeated = []
    for signature, count in counts.most_common():
        if count < 2:
            continue
        action = examples[signature]
        repeated.append(
            {
                "type": signature[0],
                "selector": signature[1],
                "value": signature[2] or None,
                "key": signature[3] or None,
                "description": action.get("description"),
                "count": count,
            }
        )
    return repeated[:20]


def _is_reset_scene(scene: dict) -> bool:
    if "reset" in str(scene.get("scene_id") or "").lower():
        return True
    return any(action.get("type") == "navigate" for action in scene.get("actions") or [])


def _summary_for_runs(runs: list[dict], strict: bool) -> dict:
    status_counts = Counter(run.get("status") for run in runs)
    failure_count = sum(len(run.get("failures") or []) for run in runs)
    warning_count = sum(len(run.get("warnings") or []) for run in runs)
    if failure_count or (strict and warning_count):
        status = "failed"
    elif warning_count:
        status = "warning"
    else:
        status = "passed"
    return {
        "status": status,
        "run_count": len(runs),
        "passed_count": status_counts.get("passed", 0),
        "warning_count": warning_count,
        "failed_count": status_counts.get("failed", 0),
        "failure_count": failure_count,
    }


def _job_id(*items: Any) -> str:
    for item in items:
        if isinstance(item, dict) and item.get("job_id"):
            return str(item["job_id"])
        if isinstance(item, Path):
            return item.name
    return "unknown"


def _source_url(scan: dict | None, plan: dict | None, recording: dict | None) -> str | None:
    if scan:
        return (scan.get("page") or {}).get("final_url") or (scan.get("input") or {}).get("url")
    if plan:
        return (plan.get("source") or {}).get("final_url") or (plan.get("source") or {}).get("url")
    if recording:
        return (recording.get("source") or {}).get("url")
    return None


def _source_title(scan: dict | None, plan: dict | None, recording: dict | None) -> str | None:
    if scan:
        return (scan.get("page") or {}).get("title")
    if plan:
        return (plan.get("project") or {}).get("title")
    if recording:
        return (recording.get("page") or {}).get("title")
    return None


def _issue(code: str, message: str, details: dict | None = None) -> dict:
    issue = {
        "code": code,
        "message": message,
    }
    if details:
        issue["details"] = details
    return issue


def _default_report_path(run_paths: list[str | Path], output_path: str | Path | None) -> Path:
    if output_path:
        return Path(output_path)
    if len(run_paths) == 1:
        return _run_root(run_paths[0]) / "validation.json"
    return Path("outputs") / "validation.json"
