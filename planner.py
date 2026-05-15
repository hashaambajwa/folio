from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


PLAN_VERSION = "0.1"
DEFAULT_LLM_MODEL = "gpt-5.1"
MAX_BUTTON_SCENES = 3
MAX_TRANSITION_SCENES = 5
MAX_LLM_CANDIDATE_PATHS = 24
MAX_LLM_TEXT_BLOCKS = 40
MAX_LLM_INTERACTIVE_ELEMENTS = 60
MIN_LLM_COVERAGE_SCORE = 0.65
MIN_LLM_PRODUCT_WORKFLOW_SCORE = 0.45
REDUNDANT_PREFIX_SCORE_MARGIN = 0.08
GENERIC_FEATURE_LABELS = {
    "about",
    "calculators",
    "home",
    "rapidtables",
    "search",
    "send feedback",
    "submit feedback",
}
GENERIC_UTILITY_TERMS = (
    "about",
    "contact",
    "feedback",
    "message",
    "recommend",
    "search",
    "site",
    "support",
)
OUTCOME_FOCUS_TERMS = (
    "answer",
    "calculation",
    "complete",
    "gpa",
    "grade",
    "output",
    "preview",
    "required",
    "result",
    "score",
    "status",
    "summary",
    "total",
)
PRIMARY_OUTCOME_TERMS = (
    "additional grade",
    "average grade",
    "final exam grade",
    "gpa",
    "letter grade",
    "overall grade",
    "required",
    "result",
    "score",
    "total credits",
)
PRIMARY_OUTCOME_SELECTOR_TERMS = (
    "#avg",
    "#avglet",
    "#fg",
    "#final",
    "#gpa",
    "#letter",
    "#overall",
    "#result",
    "#score",
    "#total",
    'name="letter"',
)
SECONDARY_OUTCOME_TERMS = (
    "calculation",
    "explanation",
)
PRODUCT_TRANSITION_KINDS = {
    "input_submit",
    "llm_goal_workflow",
    "llm_workflow",
    "toggle",
}
SUPPORTED_PLAN_ACTION_TYPES = ["observe", "click", "double_click", "fill", "press", "select"]
SELECTOR_PLAN_ACTION_TYPES = {"click", "double_click", "fill", "press", "select"}


def load_scan(scan_path: str | Path) -> dict:
    return json.loads(Path(scan_path).read_text(encoding="utf-8"))


def build_plan(
    scan: dict,
    scan_path: str | Path | None = None,
    output_path: str | Path | None = None,
    mode: str = "heuristic",
    llm_model: str | None = None,
    fallback_to_heuristic: bool = True,
) -> dict:
    if mode not in {"heuristic", "llm"}:
        raise ValueError(f"Unsupported planner mode: {mode}")

    scan_path = Path(scan_path) if scan_path else None
    if mode == "llm":
        return _build_llm_plan(
            scan,
            scan_path=scan_path,
            output_path=output_path,
            model=llm_model,
            fallback_to_heuristic=fallback_to_heuristic,
        )

    return _build_heuristic_plan(scan, scan_path=scan_path, output_path=output_path)


def _build_heuristic_plan(
    scan: dict,
    scan_path: Path | None = None,
    output_path: str | Path | None = None,
    planner_overrides: dict | None = None,
) -> dict:
    plan_path = _default_plan_path(scan, scan_path, output_path)
    page = scan.get("page", {})
    dom = scan.get("dom", {})
    title = _page_title(page, dom)
    coverage = _build_coverage_plan(scan)
    scenes = _coverage_scenes(scan, coverage) or _build_heuristic_scenes(scan)
    planner = {
        "mode": "heuristic",
        "confidence": "fallback",
        "needs_review": True,
        "strategy": "coverage_heuristic" if coverage.get("selected_path_ids") else "heuristic",
        "coverage": coverage,
        "notes": [
            "Heuristic coverage plan generated from scanner-tested candidate paths.",
            "Presentation scenes group workflows by app surface and avoid replaying repeated navigation prefixes.",
            "LLM planner should replace or refine these scenes before production use.",
        ],
    }
    if planner_overrides:
        planner.update(planner_overrides)
    selected_path_ids = coverage.get("selected_path_ids", [])
    if selected_path_ids:
        planner["selected_path_ids"] = selected_path_ids
        planner.setdefault("selected_path_id", selected_path_ids[0])
    else:
        selected_path = _best_candidate_path(scan)
        if selected_path and "selected_path_id" not in planner:
            planner["selected_path_id"] = selected_path.get("path_id")

    return _assemble_plan(
        scan=scan,
        scan_path=scan_path,
        plan_path=plan_path,
        title=title,
        scenes=scenes,
        planner=planner,
    )


def _assemble_plan(
    scan: dict,
    scan_path: Path | None,
    plan_path: Path,
    title: str,
    scenes: list[dict],
    planner: dict,
) -> dict:
    page = scan.get("page", {})
    return {
        "version": PLAN_VERSION,
        "job_id": scan.get("job_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "planner": planner,
        "source": {
            "scan_json": str(scan_path) if scan_path else scan.get("artifacts", {}).get("scan_json"),
            "screenshot": scan.get("artifacts", {}).get("screenshot"),
            "url": scan.get("input", {}).get("url"),
            "final_url": page.get("final_url"),
        },
        "project": {
            "title": title,
            "host": _host_for(page.get("final_url") or scan.get("input", {}).get("url")),
        },
        "recording": {
            "viewport": scan.get("input", {}).get("viewport"),
            "default_action_delay_ms": 700,
        },
        "scenes": scenes,
        "artifacts": {
            "plan_json": str(plan_path),
        },
    }


def write_plan(
    scan_path: str | Path,
    output_path: str | Path | None = None,
    mode: str = "heuristic",
    llm_model: str | None = None,
    fallback_to_heuristic: bool = True,
) -> dict:
    scan = load_scan(scan_path)
    plan = build_plan(
        scan,
        scan_path=scan_path,
        output_path=output_path,
        mode=mode,
        llm_model=llm_model,
        fallback_to_heuristic=fallback_to_heuristic,
    )
    plan_path = Path(plan["artifacts"]["plan_json"])
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return plan


def _build_llm_plan(
    scan: dict,
    scan_path: Path | None,
    output_path: str | Path | None,
    model: str | None,
    fallback_to_heuristic: bool,
) -> dict:
    model = model or os.environ.get("FOLIO_LLM_MODEL") or DEFAULT_LLM_MODEL
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return _handle_llm_failure(
            scan,
            scan_path=scan_path,
            output_path=output_path,
            fallback_to_heuristic=fallback_to_heuristic,
            model=model,
            error="Missing OPENAI_API_KEY.",
        )

    try:
        ranker_payload = _call_openai_path_ranker(scan, api_key=api_key, model=model)
        coverage = _build_coverage_plan(scan, ranker_payload)
        if coverage.get("selected_path_ids"):
            plan_path = _default_plan_path(scan, scan_path, output_path)
            title = _page_title(scan.get("page", {}), scan.get("dom", {}))
            scenes = _coverage_scenes(scan, coverage, ranker_payload)
            if not scenes:
                raise ValueError("LLM coverage planner selected paths without usable scenes.")

            return _assemble_plan(
                scan=scan,
                scan_path=scan_path,
                plan_path=plan_path,
                title=title,
                scenes=scenes,
                planner={
                    "mode": "llm",
                    "provider": "openai",
                    "model": model,
                    "confidence": "llm",
                    "needs_review": True,
                    "fallback_used": False,
                    "strategy": "coverage",
                    "notes": [
                        "LLM ranked scanner-tested candidate paths; Folio selected every validated workflow needed for feature coverage.",
                        "Folio used canonical replay actions from selected paths.",
                        "Presentation scenes group workflows by app surface and avoid replaying repeated navigation prefixes.",
                        "Review coverage.uncovered_features and coverage.missing_workflows to drive deeper exploration.",
                    ],
                    "selected_path_id": coverage["selected_path_ids"][0],
                    "selected_path_ids": coverage["selected_path_ids"],
                    "selected_path_valid": True,
                    "scene_source": "coverage_candidate_paths",
                    "rationale": ranker_payload.get("rationale", ""),
                    "path_ranking": _path_ranking_for_plan(ranker_payload),
                    "coverage": coverage,
                },
            )

        llm_payload = _call_openai_planner(scan, api_key=api_key, model=model)
        selected_path = _selected_candidate_path(scan, llm_payload.get("selected_path_id"))
        if selected_path:
            scenes = _llm_selected_path_scenes(scan, selected_path, llm_payload)
        else:
            scenes = _normalize_llm_scenes(llm_payload)
        if not scenes:
            raise ValueError("LLM response did not include usable scenes.")

        plan_path = _default_plan_path(scan, scan_path, output_path)
        title = _page_title(scan.get("page", {}), scan.get("dom", {}))
        notes = [
            "LLM-generated plan using scanner DOM, source context, and replayable candidate paths.",
            "Review selectors before using on high-stakes demos.",
        ]
        if selected_path:
            notes.insert(1, "LLM selected a scanner-tested candidate path; Folio used canonical replay actions from that path.")
        elif llm_payload.get("selected_path_id"):
            notes.insert(1, "LLM selected a candidate path id that was not available; Folio used normalized LLM scenes instead.")

        return _assemble_plan(
            scan=scan,
            scan_path=scan_path,
            plan_path=plan_path,
            title=title,
            scenes=scenes,
            planner={
                "mode": "llm",
                "provider": "openai",
                "model": model,
                "confidence": "llm",
                "needs_review": True,
                "fallback_used": False,
                "notes": notes,
                "selected_path_id": llm_payload.get("selected_path_id"),
                "selected_path_valid": bool(selected_path),
                "scene_source": "selected_candidate_path" if selected_path else "llm_scenes",
                "rationale": llm_payload.get("rationale", ""),
                "path_ranking": _path_ranking_for_plan(ranker_payload),
            },
        )
    except Exception as exc:
        return _handle_llm_failure(
            scan,
            scan_path=scan_path,
            output_path=output_path,
            fallback_to_heuristic=fallback_to_heuristic,
            model=model,
            error=f"{type(exc).__name__}: {exc}",
        )


def _handle_llm_failure(
    scan: dict,
    scan_path: Path | None,
    output_path: str | Path | None,
    fallback_to_heuristic: bool,
    model: str,
    error: str,
) -> dict:
    if not fallback_to_heuristic:
        raise RuntimeError(error)

    return _build_heuristic_plan(
        scan,
        scan_path=scan_path,
        output_path=output_path,
        planner_overrides={
            "mode": "llm",
            "provider": "openai",
            "model": model,
            "confidence": "fallback",
            "needs_review": True,
            "fallback_used": True,
            "error": error,
            "notes": [
                "LLM planner failed, so Folio generated a heuristic fallback plan.",
                "Set OPENAI_API_KEY to enable real LLM planning.",
            ],
        },
    )


def _call_openai_planner(scan: dict, api_key: str, model: str) -> dict:
    return _call_openai_json(
        api_key=api_key,
        model=model,
        system_prompt=_llm_system_prompt(),
        context=_scan_context_for_llm(scan),
        schema_name="folio_demo_plan",
        schema=_llm_plan_schema(),
        max_output_tokens=4_000,
    )


def _call_openai_path_ranker(scan: dict, api_key: str, model: str) -> dict:
    return _call_openai_json(
        api_key=api_key,
        model=model,
        system_prompt=_llm_path_ranker_prompt(),
        context=_path_ranking_context_for_llm(scan),
        schema_name="folio_path_ranking",
        schema=_llm_path_rank_schema(),
        max_output_tokens=2_500,
    )


def _call_openai_json(
    api_key: str,
    model: str,
    system_prompt: str,
    context: dict,
    schema_name: str,
    schema: dict,
    max_output_tokens: int,
) -> dict:
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": model,
        "input": [
            {
                "role": "developer",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": json.dumps(context, indent=2),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": schema,
            }
        },
        "max_output_tokens": max_output_tokens,
    }
    request = urllib.request.Request(
        f"{base_url}/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=60, context=_https_context()) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API request failed with HTTP {exc.code}: {detail}") from exc

    output_text = _extract_openai_output_text(body)
    if not output_text:
        raise RuntimeError("OpenAI response did not include output text.")

    return json.loads(output_text)


def _https_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _extract_openai_output_text(body: dict) -> str:
    if body.get("output_text"):
        return body["output_text"]

    chunks = []
    for item in body.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(content["text"])
    return "".join(chunks)


def _llm_system_prompt() -> str:
    return (
        "You are Folio's demo planner. Create a concise browser demo plan that showcases "
        "the app's most valuable visible workflow. Return only JSON matching the provided schema. "
        "Use only selectors present in the provided scan context for click, double_click, fill, press, and select actions. "
        "Use discovered states and transitions to understand UI that appears after interaction. "
        "Prefer selecting the replayable candidate_paths entry that best demonstrates the product value. "
        "When selected_path_id is set, Folio will use the scanner-tested actions from that path; use scenes to provide clear demo wording. "
        "Use source_context when provided to infer the app's intended routes, features, and product language. "
        "Prefer reliable workflows over broad navigation. Avoid external links unless they are the core product."
    )


def _llm_path_ranker_prompt() -> str:
    return (
        "You are Folio's demo strategist. Rank only the provided scanner-tested candidate paths by how well "
        "they showcase the app's core product value. Favor workflows that operate on the main app surface and "
        "produce meaningful outcomes. Reject navigation, search, feedback, legal, account, and generic footer/header "
        "paths unless they are the product itself. If no candidate path demonstrates core functionality, set "
        "selected_path_id to null and explain the missing workflows. Return only JSON matching the schema."
    )


def _path_ranking_context_for_llm(scan: dict) -> dict:
    dom = scan.get("dom", {})
    return {
        "page": scan.get("page", {}),
        "source": {
            "url": scan.get("input", {}).get("url"),
            "final_url": scan.get("page", {}).get("final_url"),
        },
        "summary": dom.get("summary", {}),
        "headings": dom.get("headings", [])[:20],
        "text_blocks": dom.get("text_blocks", [])[:MAX_LLM_TEXT_BLOCKS],
        "source_context": _source_context_for_llm(scan.get("source_context")),
        "candidate_paths": [
            _candidate_path_for_ranker(path)
            for path in scan.get("candidate_paths", [])[:MAX_LLM_CANDIDATE_PATHS]
        ],
        "browser_errors": scan.get("browser_errors", {}),
    }


def _candidate_path_for_ranker(path: dict) -> dict:
    return {
        "path_id": path.get("path_id"),
        "heuristic_score": path.get("score"),
        "heuristic_reasons": path.get("selection_reasons", []),
        "final_url": path.get("final_url"),
        "same_origin": path.get("same_origin"),
        "same_path": path.get("same_path"),
        "labels": path.get("labels", []),
        "kinds": path.get("kinds", []),
        "quality_tags": path.get("quality_tags", []),
        "action_descriptions": [
            action.get("description") or action.get("type")
            for action in path.get("replay_actions", [])
        ],
        "transition_outcomes": [
            {
                "label": transition.get("label"),
                "kind": transition.get("kind"),
                "outcome_summary": transition.get("outcome_summary"),
            }
            for transition in path.get("transitions", [])
        ],
        "final_state_summary": path.get("final_state_summary", {}),
    }


def _scan_context_for_llm(scan: dict) -> dict:
    dom = scan.get("dom", {})
    return {
        "page": scan.get("page", {}),
        "source": {
            "url": scan.get("input", {}).get("url"),
            "final_url": scan.get("page", {}).get("final_url"),
        },
        "summary": dom.get("summary", {}),
        "headings": dom.get("headings", [])[:20],
        "text_blocks": dom.get("text_blocks", [])[:MAX_LLM_TEXT_BLOCKS],
        "source_context": _source_context_for_llm(scan.get("source_context")),
        "candidate_paths": [
            _candidate_path_for_llm(path)
            for path in scan.get("candidate_paths", [])[:MAX_LLM_CANDIDATE_PATHS]
        ],
        "states": [
            _state_for_llm(state)
            for state in scan.get("states", [])[:10]
        ],
        "transitions": scan.get("transitions", [])[:30],
        "interactive": [
            _element_for_llm(element, index)
            for index, element in enumerate(dom.get("interactive", [])[:MAX_LLM_INTERACTIVE_ELEMENTS], 1)
        ],
        "accessibility": scan.get("accessibility"),
        "browser_errors": scan.get("browser_errors", {}),
        "supported_action_types": SUPPORTED_PLAN_ACTION_TYPES,
    }


def _candidate_path_for_llm(path: dict) -> dict:
    return {
        "path_id": path.get("path_id"),
        "score": path.get("score"),
        "selection_reasons": path.get("selection_reasons", []),
        "final_state_id": path.get("final_state_id"),
        "depth": path.get("depth"),
        "final_url": path.get("final_url"),
        "same_origin": path.get("same_origin"),
        "same_path": path.get("same_path"),
        "route_fragment": path.get("route_fragment"),
        "labels": path.get("labels", []),
        "kinds": path.get("kinds", []),
        "quality_tags": path.get("quality_tags", []),
        "action_types": path.get("action_types", []),
        "replay_actions": path.get("replay_actions", []),
        "transitions": path.get("transitions", []),
        "final_state_summary": path.get("final_state_summary", {}),
    }


def _selected_candidate_path(scan: dict, path_id: str | None) -> dict | None:
    if not path_id:
        return None

    for path in scan.get("candidate_paths", []):
        if path.get("path_id") == path_id and path.get("transitions"):
            return path
    return None


def _ranker_selected_path_id(payload: dict) -> str | None:
    selected_path_id = payload.get("selected_path_id")
    if selected_path_id:
        return selected_path_id
    return None


def _ranker_scene_payload(scan: dict, selected_path: dict, payload: dict) -> dict:
    title = _page_title(scan.get("page", {}), scan.get("dom", {}))
    selected_reason = _ranked_path_reason(payload, selected_path.get("path_id"))
    interaction_scenes = []
    transitions = selected_path.get("transitions", [])
    for transition in transitions:
        label = _clean_transition_label(transition.get("label") or transition.get("kind") or "interaction")
        interaction_scenes.append(
            {
                "type": "interaction",
                "title": _transition_title(transition.get("kind") or "interaction", label),
                "goal": selected_reason or f"Demonstrate {label}.",
                "duration_seconds": 6,
                "success_criteria": "The scanner-tested transition completes and the resulting app state is visible.",
                "narration_hint": _outcome_narration_hint(transition),
            }
        )

    return {
        "scenes": [
            {
                "type": "intro",
                "title": f"Introduce {title}",
                "goal": payload.get("rationale") or f"Establish what {title} does.",
                "duration_seconds": 4,
                "success_criteria": "The app is loaded and the main screen is visible.",
                "narration_hint": payload.get("rationale") or f"Introduce the selected {title} workflow.",
            },
            *interaction_scenes,
            {
                "type": "outro",
                "title": "Wrap up",
                "goal": selected_reason or f"Close the demo after showing the selected {title} workflow.",
                "duration_seconds": 4,
                "success_criteria": "The final selected path state remains stable and readable.",
                "narration_hint": selected_reason or "Summarize the app value shown in the workflow.",
            },
        ]
    }


def _ranked_path_reason(payload: dict, path_id: str | None) -> str:
    for item in payload.get("ranked_paths", []):
        if item.get("path_id") == path_id:
            return (item.get("reason") or "").strip()
    return ""


def _outcome_narration_hint(transition: dict) -> str:
    summary = transition.get("outcome_summary") or {}
    added_text = summary.get("added_text") or []
    changed_controls = summary.get("changed_controls") or []
    if added_text:
        return f"Call out the new visible result: {', '.join(added_text[:3])}."
    if changed_controls:
        changed_names = [control.get("name") for control in changed_controls if control.get("name")]
        if changed_names:
            return f"Call out the changed control state for {', '.join(changed_names[:3])}."
    if summary.get("url_changed"):
        return "Explain why this navigation matters to the app workflow."
    return "Explain what this interaction contributes to the workflow."


def _path_ranking_for_plan(payload: dict) -> dict:
    return {
        "rationale": payload.get("rationale", ""),
        "ranked_paths": payload.get("ranked_paths", [])[:MAX_LLM_CANDIDATE_PATHS],
        "rejected_paths": payload.get("rejected_paths", [])[:MAX_LLM_CANDIDATE_PATHS],
        "missing_workflows": payload.get("missing_workflows", [])[:8],
    }


def _build_coverage_plan(scan: dict, ranker_payload: dict | None = None) -> dict:
    paths = [path for path in scan.get("candidate_paths", []) if path.get("transitions")]
    rank_map = {
        item.get("path_id"): item
        for item in (ranker_payload or {}).get("ranked_paths", [])
        if item.get("path_id")
    }
    rejected_map = {
        item.get("path_id"): item.get("reason", "")
        for item in (ranker_payload or {}).get("rejected_paths", [])
        if item.get("path_id")
    }
    selected_path_id = _ranker_selected_path_id(ranker_payload or {})

    path_items = [
        _coverage_path_item(path, rank_map.get(path.get("path_id")), rejected_map.get(path.get("path_id")))
        for path in paths
    ]
    path_items = [item for item in path_items if item]
    path_items.sort(key=lambda item: (item["demo_value_score"], item["heuristic_score"]), reverse=True)

    selected_items = []
    uncovered_features_by_id = {}
    selected_path_ids = []
    for item in path_items:
        force_selected = selected_path_id and item["path_id"] == selected_path_id
        if _coverage_item_is_selected(item, force_selected=force_selected):
            selected_path_ids.append(item["path_id"])
            selected_items.append(item)
            continue

        if _coverage_item_is_uncovered(item):
            uncovered = uncovered_features_by_id.setdefault(
                item["feature_id"],
                {
                    "feature_id": item["feature_id"],
                    "title": item["feature_title"],
                    "candidate_path_ids": [],
                    "reason": _coverage_uncovered_reason(item),
                },
            )
            uncovered["candidate_path_ids"].append(item["path_id"])

    redundant_paths = []
    if selected_path_ids:
        paths_by_id = {path.get("path_id"): path for path in paths if path.get("path_id")}
        selected_path_ids, redundant_paths = _prune_redundant_prefix_path_ids(
            selected_path_ids,
            selected_items,
            paths_by_id,
        )

    selected_path_id_set = set(selected_path_ids)
    covered_features = [
        _coverage_feature_for_item(item)
        for item in selected_items
        if item["path_id"] in selected_path_id_set
    ]
    covered_feature_ids = {feature["feature_id"] for feature in covered_features}
    uncovered_features = [
        feature
        for feature_id, feature in uncovered_features_by_id.items()
        if feature_id not in covered_feature_ids
    ]
    if selected_path_ids:
        selected_path_ids = _presentation_ordered_path_ids(
            selected_path_ids,
            paths_by_id,
            _recording_start_url(scan),
        )
        selected_order = {path_id: index for index, path_id in enumerate(selected_path_ids)}
        covered_features.sort(key=lambda feature: selected_order.get(feature["path_id"], len(selected_order)))

    missing_workflows = (ranker_payload or {}).get("missing_workflows", [])[:8]
    status = "complete" if selected_path_ids and not uncovered_features and not missing_workflows else "partial"
    if not selected_path_ids:
        status = "none"

    return {
        "status": status,
        "coverage_confidence": "llm" if ranker_payload else "heuristic",
        "selected_path_ids": selected_path_ids,
        "covered_features": covered_features,
        "uncovered_features": uncovered_features,
        "missing_workflows": missing_workflows,
        "redundant_paths": redundant_paths,
        "rejected_paths": [
            {"path_id": path_id, "reason": reason}
            for path_id, reason in rejected_map.items()
        ],
        "candidate_path_count": len(paths),
        "selected_path_count": len(selected_path_ids),
    }


def _coverage_feature_for_item(item: dict) -> dict:
    return {
        "feature_id": item["feature_id"],
        "title": item["feature_title"],
        "path_id": item["path_id"],
        "demo_value_score": item["demo_value_score"],
        "reason": item["reason"],
        "quality_tags": item["quality_tags"],
        "workflow_kind": item["workflow_kind"],
    }


def _prune_redundant_prefix_path_ids(
    selected_path_ids: list[str],
    selected_items: list[dict],
    paths_by_id: dict[str, dict],
) -> tuple[list[str], list[dict]]:
    item_by_id = {item["path_id"]: item for item in selected_items}
    pruned_by: dict[str, str] = {}
    for path_id in selected_path_ids:
        covering_path_id = _covering_prefix_path_id(path_id, selected_path_ids, item_by_id, paths_by_id)
        if covering_path_id:
            pruned_by[path_id] = covering_path_id

    kept_path_ids = [path_id for path_id in selected_path_ids if path_id not in pruned_by]
    redundant_paths = [
        {
            "path_id": path_id,
            "covered_by_path_id": covered_by_path_id,
            "reason": "A selected richer workflow contains this path as its opening sequence.",
        }
        for path_id, covered_by_path_id in pruned_by.items()
    ]
    return kept_path_ids, redundant_paths


def _covering_prefix_path_id(
    path_id: str,
    selected_path_ids: list[str],
    item_by_id: dict[str, dict],
    paths_by_id: dict[str, dict],
) -> str | None:
    path = paths_by_id.get(path_id)
    item = item_by_id.get(path_id)
    if not path or not item or item.get("workflow_kind") != "product_workflow":
        return None

    signature = _path_transition_signature(path)
    if not signature:
        return None

    candidates = []
    for other_path_id in selected_path_ids:
        if other_path_id == path_id:
            continue
        other_path = paths_by_id.get(other_path_id)
        other_item = item_by_id.get(other_path_id)
        if not other_path or not other_item:
            continue
        if other_item.get("feature_id") != item.get("feature_id"):
            continue
        if other_item.get("workflow_kind") != "product_workflow":
            continue
        if other_item.get("demo_value_score", 0) + REDUNDANT_PREFIX_SCORE_MARGIN < item.get("demo_value_score", 0):
            continue
        other_signature = _path_transition_signature(other_path)
        if len(other_signature) <= len(signature):
            continue
        if other_signature[: len(signature)] != signature:
            continue
        candidates.append(other_path_id)

    if not candidates:
        return None

    return max(
        candidates,
        key=lambda candidate_id: (
            item_by_id.get(candidate_id, {}).get("demo_value_score", 0),
            len(paths_by_id.get(candidate_id, {}).get("transitions", [])),
        ),
    )


def _path_transition_signature(path: dict) -> list[tuple]:
    return [_transition_signature(transition) for transition in path.get("transitions", [])]


def _transition_signature(transition: dict) -> tuple:
    actions = tuple(
        (
            action.get("type"),
            action.get("selector"),
            str(action.get("value") or ""),
            str(action.get("key") or ""),
        )
        for action in transition.get("actions", [])
    )
    return (
        transition.get("kind"),
        _clean_transition_label(transition.get("label") or ""),
        transition.get("to"),
        actions,
    )


def _coverage_path_item(path: dict, rank_item: dict | None, rejected_reason: str | None) -> dict:
    path_id = path.get("path_id")
    feature_title = _path_feature_title(path)
    heuristic_score = int(path.get("score") or 0)
    demo_value_score = _coverage_demo_value(path, rank_item)
    reason = (rank_item or {}).get("reason") or "; ".join(path.get("selection_reasons", [])[:2])
    quality_tags = path.get("quality_tags", [])
    workflow_kind = _path_workflow_kind(path)
    has_presentable_outcome = _path_has_presentable_outcome(path)
    return {
        "path_id": path_id,
        "feature_id": _slug(feature_title),
        "feature_title": feature_title,
        "heuristic_score": heuristic_score,
        "demo_value_score": demo_value_score,
        "reason": reason,
        "rejected_reason": rejected_reason,
        "quality_tags": quality_tags,
        "workflow_kind": workflow_kind,
        "has_presentable_outcome": has_presentable_outcome,
    }


def _coverage_demo_value(path: dict, rank_item: dict | None) -> float:
    if rank_item and isinstance(rank_item.get("demo_value_score"), (int, float)):
        return max(0.0, min(1.0, float(rank_item["demo_value_score"])))

    score = int(path.get("score") or 0)
    if _path_has_product_workflow(path):
        return max(0.55, min(0.9, score / 50))
    return min(0.45, score / 80)


def _coverage_item_is_selected(item: dict, force_selected: bool = False) -> bool:
    if item["rejected_reason"]:
        return False
    if item["workflow_kind"] == "product_workflow" and not item.get("has_presentable_outcome"):
        return False
    if force_selected:
        return True
    if item["demo_value_score"] >= MIN_LLM_COVERAGE_SCORE:
        return True
    return item["demo_value_score"] >= MIN_LLM_PRODUCT_WORKFLOW_SCORE and item["workflow_kind"] == "product_workflow"


def _coverage_item_is_uncovered(item: dict) -> bool:
    if item["workflow_kind"] == "product_workflow":
        return True
    if item["rejected_reason"]:
        return False
    return item["demo_value_score"] >= MIN_LLM_COVERAGE_SCORE


def _coverage_uncovered_reason(item: dict) -> str:
    if item["rejected_reason"]:
        return item["rejected_reason"]
    if item["workflow_kind"] == "product_workflow" and not item.get("has_presentable_outcome"):
        return "Validated actions did not expose a clear presentable output for this workflow."
    return "No validated product workflow selected for this feature yet."


def _path_has_product_workflow(path: dict) -> bool:
    quality_tags = set(path.get("quality_tags", []))
    kinds = set(path.get("kinds", []))
    action_types = set(path.get("action_types", []))
    if "llm_guided_workflow" in quality_tags or {"llm_workflow", "llm_goal_workflow"} & kinds:
        return True
    if _path_looks_generic_utility(path):
        return False
    if "creates_then_mutates" in quality_tags:
        return True
    if bool({"fill", "select"} & action_types) and bool({"click", "double_click", "press"} & action_types):
        return True
    return False


def _path_looks_generic_utility(path: dict) -> bool:
    text = " ".join(
        [
            *[str(label or "") for label in path.get("labels", [])],
            *[str(action.get("description") or "") for action in path.get("replay_actions", [])],
            str(path.get("final_state_summary", {}).get("title") or ""),
        ]
    ).lower()
    return any(term in text for term in GENERIC_UTILITY_TERMS)


def _path_workflow_kind(path: dict) -> str:
    if _path_has_product_workflow(path):
        return "product_workflow"
    if path.get("same_origin") and path.get("final_url"):
        return "navigation"
    return "utility"


def _path_has_presentable_outcome(path: dict) -> bool:
    for transition in path.get("transitions", []):
        if transition.get("kind") not in PRODUCT_TRANSITION_KINDS and not _transition_has_form_submission(transition):
            continue

        summary = transition.get("outcome_summary") or {}
        if summary.get("added_text") or summary.get("removed_text"):
            return True

        action_selectors = {
            action.get("selector")
            for action in transition.get("actions", [])
            if action.get("selector")
        }
        for control in summary.get("changed_controls") or []:
            selectors = control.get("selectors") or []
            if _control_matches_action_selector(selectors, action_selectors) and transition.get("kind") != "toggle":
                continue
            if _control_has_changed_value(control):
                return True

    return False


def _transition_has_form_submission(transition: dict) -> bool:
    action_types = [action.get("type") for action in transition.get("actions", [])]
    return bool({"fill", "select"} & set(action_types)) and bool({"click", "double_click", "press"} & set(action_types))


def _control_matches_action_selector(selectors: list[str], action_selectors: set[str]) -> bool:
    return any(selector in action_selectors for selector in selectors)


def _path_feature_title(path: dict) -> str:
    labels = [_clean_transition_label(label) for label in path.get("labels", []) if label]
    for label in labels:
        normalized = label.lower()
        if normalized in GENERIC_FEATURE_LABELS or normalized.startswith("submit "):
            continue
        return label

    final_summary = path.get("final_state_summary", {})
    title = _clean_transition_label(final_summary.get("title") or "")
    if title:
        return title
    if labels:
        return labels[0]
    return path.get("path_id") or "workflow"


def _coverage_scenes(scan: dict, coverage: dict, ranker_payload: dict | None = None) -> list[dict]:
    selected_path_ids = coverage.get("selected_path_ids", [])
    if not selected_path_ids:
        return []

    paths_by_id = {
        path.get("path_id"): path
        for path in scan.get("candidate_paths", [])
        if path.get("path_id")
    }
    title = _page_title(scan.get("page", {}), scan.get("dom", {}))
    start_url = _recording_start_url(scan)
    scenes = [_intro_scene(title, scan.get("page", {}).get("final_url"))]
    ordered_path_ids = _presentation_ordered_path_ids(selected_path_ids, paths_by_id, start_url)

    for index, path_id in enumerate(ordered_path_ids, 1):
        path = paths_by_id.get(path_id)
        if not path:
            continue

        if index > 1:
            reset_url = _path_work_surface_url(path, start_url)
            if reset_url:
                scenes.append(_workflow_reset_scene(path, index, reset_url))

        transitions = _presentation_transitions_for_path(path, include_navigation=index == 1)
        path_scenes = _transition_scenes_for_path(
            transitions,
            source_path_id=path_id,
            max_transitions=None,
        )
        reason = _ranked_path_reason(ranker_payload or {}, path_id)
        for scene in path_scenes:
            scene["scene_id"] = f"workflow-{index}-{scene['scene_id']}"
            scene["workflow_index"] = index
            scene["feature_title"] = _path_feature_title(path)
            if reason:
                scene["goal"] = reason
                scene["narration_hint"] = reason
        scenes.extend(path_scenes)

    scenes.append(_outro_scene(title))
    return scenes


def _presentation_ordered_path_ids(
    selected_path_ids: list[str],
    paths_by_id: dict[str, dict],
    fallback_url: str | None,
) -> list[str]:
    groups: dict[str, list[str]] = {}
    for path_id in selected_path_ids:
        path = paths_by_id.get(path_id)
        if not path:
            continue
        group_key = _path_work_surface_group_key(path, fallback_url)
        groups.setdefault(group_key, []).append(path_id)

    def group_score(item: tuple[str, list[str]]) -> tuple:
        _, path_ids = item
        best_path = max(
            (paths_by_id[path_id] for path_id in path_ids if path_id in paths_by_id),
            key=lambda path: (int(path.get("score") or 0), len(path.get("transitions", []))),
        )
        return (int(best_path.get("score") or 0), len(path_ids))

    ordered_path_ids = []
    for _, path_ids in sorted(groups.items(), key=group_score, reverse=True):
        ordered_path_ids.extend(
            sorted(
                path_ids,
                key=lambda path_id: (
                    int(paths_by_id.get(path_id, {}).get("score") or 0),
                    len(paths_by_id.get(path_id, {}).get("transitions", [])),
                ),
                reverse=True,
            )
        )
    return ordered_path_ids


def _presentation_transitions_for_path(path: dict, include_navigation: bool) -> list[dict]:
    transitions = path.get("transitions", [])
    if include_navigation:
        return transitions

    product_index = _first_product_transition_index(transitions)
    if product_index is None:
        return transitions
    return transitions[product_index:]


def _first_product_transition_index(transitions: list[dict]) -> int | None:
    for index, transition in enumerate(transitions):
        if transition.get("kind") in PRODUCT_TRANSITION_KINDS:
            return index
        action_types = {action.get("type") for action in transition.get("actions", []) if action.get("type")}
        if action_types & {"fill", "select"}:
            return index
    return None


def _path_work_surface_url(path: dict, fallback_url: str | None) -> str | None:
    transitions = path.get("transitions", [])
    product_index = _first_product_transition_index(transitions)
    if product_index is not None:
        before_url = (transitions[product_index].get("outcome_summary") or {}).get("before_url")
        if before_url:
            return before_url

    final_url = path.get("final_url")
    if final_url:
        return final_url
    return _path_start_url(path, fallback_url)


def _path_work_surface_group_key(path: dict, fallback_url: str | None) -> str:
    url = _path_work_surface_url(path, fallback_url) or fallback_url or path.get("path_id") or "workflow"
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return url


def _workflow_reset_scene(path: dict, index: int, url: str | None) -> dict:
    feature_title = _path_feature_title(path)
    return _scene(
        scene_id=f"workflow-{index}-reset",
        title=f"Start {feature_title}",
        goal=f"Return to a known app state before demonstrating {feature_title}.",
        scene_type="overview",
        duration_seconds=2,
        actions=[
            {
                "action_id": f"navigate-workflow-{index}",
                "type": "navigate",
                "url": url,
                "description": f"Navigate to the starting state for {feature_title}.",
            }
        ],
        success_criteria="The next workflow starts from a stable app state.",
        narration_hint=f"Move to the next feature area: {feature_title}.",
        source_path_id=path.get("path_id"),
    )


def _recording_start_url(scan: dict) -> str | None:
    return scan.get("page", {}).get("final_url") or scan.get("input", {}).get("url")


def _path_start_url(path: dict, fallback_url: str | None) -> str | None:
    for transition in path.get("transitions", []):
        before_url = (transition.get("outcome_summary") or {}).get("before_url")
        if before_url:
            return before_url
    return fallback_url


def _llm_selected_path_scenes(scan: dict, selected_path: dict, payload: dict) -> list[dict]:
    page = scan.get("page", {})
    title = _page_title(page, scan.get("dom", {}))
    path_id = selected_path.get("path_id")
    transition_scenes = _transition_scenes_for_path(
        selected_path.get("transitions", []),
        source_path_id=path_id,
    )
    if not transition_scenes:
        return []

    scenes = [
        _apply_scene_copy(
            _intro_scene(title, page.get("final_url")),
            _first_scene_copy(payload, {"intro"}),
        )
    ]

    interaction_copies = _scene_copies(payload, {"interaction", "overview"})
    for scene, copy in zip(transition_scenes, interaction_copies):
        scenes.append(_apply_scene_copy(scene, copy))
    if len(interaction_copies) < len(transition_scenes):
        scenes.extend(transition_scenes[len(interaction_copies):])

    scenes.append(
        _apply_scene_copy(
            _outro_scene(title),
            _first_scene_copy(payload, {"outro"}),
        )
    )
    return scenes


def _first_scene_copy(payload: dict, scene_types: set[str]) -> dict | None:
    copies = _scene_copies(payload, scene_types)
    return copies[0] if copies else None


def _scene_copies(payload: dict, scene_types: set[str]) -> list[dict]:
    copies = []
    for scene in payload.get("scenes", []):
        if scene.get("type") not in scene_types:
            continue
        copy = _scene_copy(scene)
        if copy:
            copies.append(copy)
    return copies


def _scene_copy(scene: dict) -> dict:
    copy = {}
    for key in ("title", "goal", "success_criteria", "narration_hint"):
        value = scene.get(key)
        if isinstance(value, str) and value.strip():
            copy[key] = value.strip()
    if scene.get("duration_seconds"):
        copy["duration_seconds"] = _bounded_duration(scene.get("duration_seconds"))
    return copy


def _apply_scene_copy(scene: dict, copy: dict | None) -> dict:
    if not copy:
        return scene

    merged = dict(scene)
    for key in ("title", "goal", "duration_seconds", "success_criteria", "narration_hint"):
        if key in copy:
            merged[key] = copy[key]
    return merged


def _bounded_duration(value: object) -> int:
    try:
        duration = int(value)
    except (TypeError, ValueError):
        return 5
    return max(2, min(12, duration))


def _source_context_for_llm(source_context: dict | None) -> dict | None:
    if not source_context:
        return None

    return {
        "status": source_context.get("status"),
        "root_name": source_context.get("root_name"),
        "summary": source_context.get("summary", {}),
        "diagnostics": source_context.get("diagnostics", {}),
        "framework_hints": source_context.get("framework_hints", []),
        "package": source_context.get("package"),
        "tree": source_context.get("tree", [])[:160],
        "inspected_files": source_context.get("inspected_files", [])[:80],
        "readmes": [
            {
                "path": readme.get("path"),
                "text": (readme.get("text") or "")[:2_500],
            }
            for readme in source_context.get("readmes", [])[:2]
        ],
        "routes": source_context.get("routes", [])[:60],
        "components": source_context.get("components", [])[:80],
        "ui_strings": source_context.get("ui_strings", [])[:80],
    }


def _state_for_llm(state: dict) -> dict:
    dom = state.get("dom", {})
    return {
        "state_id": state.get("state_id"),
        "url": state.get("url"),
        "depth": state.get("depth"),
        "parent_state_id": state.get("parent_state_id"),
        "transition": state.get("transition"),
        "summary": dom.get("summary", {}),
        "headings": dom.get("headings", [])[:12],
        "text_blocks": dom.get("text_blocks", [])[:MAX_LLM_TEXT_BLOCKS],
        "interactive": [
            _element_for_llm(element, index)
            for index, element in enumerate(dom.get("interactive", [])[:MAX_LLM_INTERACTIVE_ELEMENTS], 1)
        ],
        "accessibility": state.get("accessibility"),
    }


def _element_for_llm(element: dict, index: int) -> dict:
    return {
        "index": index,
        "tag": element.get("tag"),
        "type": element.get("type"),
        "role": element.get("role"),
        "text": element.get("text"),
        "label": element.get("label"),
        "accessible_name": element.get("accessible_name"),
        "href": element.get("href"),
        "selectors": element.get("selectors", []),
        "attributes": element.get("attributes", {}),
        "bounds": element.get("bounds", {}),
    }


def _llm_plan_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["rationale", "selected_path_id", "scenes"],
        "properties": {
            "rationale": {"type": "string"},
            "selected_path_id": {"type": ["string", "null"]},
            "scenes": {
                "type": "array",
                "minItems": 2,
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "scene_id",
                        "title",
                        "type",
                        "goal",
                        "duration_seconds",
                        "actions",
                        "success_criteria",
                        "narration_hint",
                    ],
                    "properties": {
                        "scene_id": {"type": "string"},
                        "title": {"type": "string"},
                        "type": {"type": "string", "enum": ["intro", "interaction", "overview", "outro"]},
                        "goal": {"type": "string"},
                        "duration_seconds": {"type": "integer", "minimum": 2, "maximum": 12},
                        "success_criteria": {"type": "string"},
                        "narration_hint": {"type": "string"},
                        "actions": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 8,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["action_id", "type", "description", "selector", "value", "key", "allow_hidden"],
                                "properties": {
                                    "action_id": {"type": "string"},
                                    "type": {"type": "string", "enum": SUPPORTED_PLAN_ACTION_TYPES},
                                    "description": {"type": "string"},
                                    "selector": {"type": ["string", "null"]},
                                    "value": {"type": ["string", "null"]},
                                    "key": {"type": ["string", "null"]},
                                    "allow_hidden": {"type": "boolean"},
                                },
                            },
                        },
                    },
                },
            },
        },
    }


def _llm_path_rank_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["rationale", "selected_path_id", "ranked_paths", "rejected_paths", "missing_workflows"],
        "properties": {
            "rationale": {"type": "string"},
            "selected_path_id": {"type": ["string", "null"]},
            "ranked_paths": {
                "type": "array",
                "maxItems": MAX_LLM_CANDIDATE_PATHS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path_id", "demo_value_score", "reason"],
                    "properties": {
                        "path_id": {"type": "string"},
                        "demo_value_score": {"type": "number", "minimum": 0, "maximum": 1},
                        "reason": {"type": "string"},
                    },
                },
            },
            "rejected_paths": {
                "type": "array",
                "maxItems": MAX_LLM_CANDIDATE_PATHS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path_id", "reason"],
                    "properties": {
                        "path_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
            "missing_workflows": {
                "type": "array",
                "maxItems": 8,
                "items": {"type": "string"},
            },
        },
    }


def _normalize_llm_scenes(payload: dict) -> list[dict]:
    scenes = []
    for scene in payload.get("scenes", []):
        actions = [_normalize_llm_action(action) for action in scene.get("actions", [])]
        actions = [action for action in actions if action]
        if not actions:
            actions = [
                {
                    "action_id": "observe-scene",
                    "type": "observe",
                    "description": "Show this scene state.",
                }
            ]

        scene_id = _slug(scene.get("scene_id") or scene.get("title") or f"scene-{len(scenes) + 1}")
        scenes.append(
            _scene(
                scene_id=scene_id,
                title=(scene.get("title") or scene_id).strip(),
                goal=(scene.get("goal") or "Show this part of the app.").strip(),
                scene_type=scene.get("type") if scene.get("type") in {"intro", "interaction", "overview", "outro"} else "overview",
                duration_seconds=int(scene.get("duration_seconds") or 5),
                actions=actions,
                success_criteria=(scene.get("success_criteria") or "The scene completes without errors.").strip(),
                narration_hint=(scene.get("narration_hint") or "Explain what the viewer is seeing.").strip(),
            )
        )
    return scenes


def _normalize_llm_action(action: dict) -> dict | None:
    action_type = action.get("type")
    if action_type not in SUPPORTED_PLAN_ACTION_TYPES:
        return None

    normalized = {
        "action_id": _slug(action.get("action_id") or action_type),
        "type": action_type,
        "description": (action.get("description") or f"{action_type} action").strip(),
    }
    selector = action.get("selector")
    if action_type in SELECTOR_PLAN_ACTION_TYPES:
        if not selector:
            return None
        normalized["selector"] = selector
    elif action_type == "observe" and selector:
        normalized["selector"] = selector
    if action_type in {"fill", "select"}:
        normalized["value"] = action.get("value") or "Demo value"
    if action_type == "press":
        normalized["key"] = action.get("key") or "Enter"
    if action.get("allow_hidden"):
        normalized["allow_hidden"] = True
    return normalized


def _default_plan_path(
    scan: dict,
    scan_path: Path | None,
    output_path: str | Path | None,
) -> Path:
    if output_path:
        return Path(output_path)

    artifact_path = scan.get("artifacts", {}).get("scan_json")
    if artifact_path:
        return Path(artifact_path).with_name("plan.json")

    if scan_path:
        return scan_path.with_name("plan.json")

    output_dir = scan.get("artifacts", {}).get("output_dir", "outputs")
    return Path(output_dir) / "plan.json"


def _build_heuristic_scenes(scan: dict) -> list[dict]:
    page = scan.get("page", {})
    dom = scan.get("dom", {})
    title = _page_title(page, dom)
    scenes = [_intro_scene(title, page.get("final_url"))]

    transition_scenes = _transition_path_scenes(scan)
    if transition_scenes:
        scenes.extend(transition_scenes)
    else:
        primary_input = _first_actionable_input(dom.get("inputs", []))
        if primary_input:
            scenes.append(_input_scene(primary_input))

        for index, button in enumerate(_meaningful_buttons(dom.get("buttons", []))[:MAX_BUTTON_SCENES], 1):
            scenes.append(_button_scene(button, index))

        links = _meaningful_links(dom.get("links", []))
        if links:
            scenes.append(_navigation_scene(links[:5]))

    if len(scenes) == 1:
        scenes.append(_static_walkthrough_scene(dom))

    scenes.append(_outro_scene(title))
    return scenes


def _transition_path_scenes(scan: dict) -> list[dict]:
    path = _best_candidate_path(scan)
    if path:
        return _transition_scenes_for_path(path.get("transitions", []), source_path_id=path.get("path_id"))

    state = _best_transition_state(scan)
    if not state:
        return []

    states_by_id = {
        candidate.get("state_id"): candidate
        for candidate in scan.get("states", [])
        if candidate.get("state_id")
    }
    transitions = _state_path_transitions(state, states_by_id)
    return _transition_scenes_for_path(transitions)


def _best_candidate_path(scan: dict) -> dict | None:
    paths = [path for path in scan.get("candidate_paths", []) if path.get("transitions")]
    if not paths:
        return None

    return max(
        paths,
        key=lambda path: (
            int(path.get("score") or 0),
            int(path.get("depth") or 0),
            len(path.get("replay_actions", [])),
        ),
    )


def _transition_scenes_for_path(
    transitions: list[dict],
    source_path_id: str | None = None,
    max_transitions: int | None = MAX_TRANSITION_SCENES,
) -> list[dict]:
    scenes = []
    selected_transitions = transitions if max_transitions is None else transitions[:max_transitions]
    for index, transition in enumerate(selected_transitions, 1):
        scene = _transition_scene(transition, index, source_path_id=source_path_id)
        if scene:
            scenes.append(scene)
    return scenes


def _best_transition_state(scan: dict) -> dict | None:
    states = [
        state
        for state in scan.get("states", [])
        if state.get("state_id") != "initial" and state.get("replay_actions")
    ]
    if not states:
        return None

    start_url = scan.get("page", {}).get("final_url") or scan.get("input", {}).get("url")
    states_by_id = {
        state.get("state_id"): state
        for state in scan.get("states", [])
        if state.get("state_id")
    }

    def score(state: dict) -> tuple:
        transitions = _state_path_transitions(state, states_by_id)
        kinds = {transition.get("kind") for transition in transitions if transition.get("kind")}
        action_types = {
            action.get("type")
            for action in state.get("replay_actions", [])
            if action.get("type")
        }
        same_path_bonus = 8 if _same_url_path(start_url, state.get("url")) else 0
        route_penalty = 0 if _same_url_origin(start_url, state.get("url")) else -20
        fragment_bonus = _route_fragment_bonus(state.get("url"))
        return (
            same_path_bonus + route_penalty + fragment_bonus + len(kinds) * 4 + len(action_types) * 2,
            len(transitions),
            len(state.get("replay_actions", [])),
        )

    return max(states, key=score)


def _state_path_transitions(state: dict, states_by_id: dict[str, dict]) -> list[dict]:
    transitions = []
    current = state
    seen = set()
    while current and current.get("state_id") not in seen:
        seen.add(current.get("state_id"))
        transition = current.get("transition")
        if transition:
            transitions.append(transition)
        parent_id = current.get("parent_state_id")
        current = states_by_id.get(parent_id)

    return list(reversed(transitions))


def _transition_scene(transition: dict, index: int, source_path_id: str | None = None) -> dict | None:
    actions = [
        _probe_action_for_plan(action, transition, action_index)
        for action_index, action in enumerate(transition.get("actions", []), 1)
    ]
    actions = [action for action in actions if action]
    if not actions:
        return None

    _attach_outcome_focus_action(actions, transition)
    kind = transition.get("kind") or "interaction"
    label = transition.get("label") or kind
    clean_label = _clean_transition_label(label)
    return _scene(
        scene_id=f"probe-{index}-{_slug(clean_label)}",
        title=_transition_title(kind, clean_label),
        goal=f"Show the app response for {clean_label}.",
        scene_type="interaction",
        duration_seconds=6,
        actions=actions,
        success_criteria="The interaction completes and the next UI state is visible.",
        narration_hint=f"Explain what changes after {clean_label}.",
        source_transition=transition,
        source_path_id=source_path_id,
    )


def _probe_action_for_plan(action: dict, transition: dict, index: int) -> dict | None:
    action_type = action.get("type")
    if action_type not in SUPPORTED_PLAN_ACTION_TYPES:
        return None

    planned = {
        "action_id": f"{_slug(transition.get('candidate_id') or transition.get('label') or 'probe')}-{index}",
        "type": action_type,
        "description": action.get("description") or f"{action_type} during probe.",
    }
    if action_type in SELECTOR_PLAN_ACTION_TYPES:
        selector = action.get("selector")
        if not selector:
            return None
        planned["selector"] = selector
    elif action_type == "observe" and action.get("selector"):
        planned["selector"] = action.get("selector")
    if action_type in {"fill", "select"}:
        planned["value"] = action.get("value") or "Demo value"
    if action_type == "press":
        planned["key"] = action.get("key") or "Enter"
    if action.get("allow_hidden"):
        planned["allow_hidden"] = True
    if action_type == "observe" and action.get("duration_seconds"):
        planned["duration_seconds"] = _bounded_duration(action.get("duration_seconds"))
    return planned


def _attach_outcome_focus_action(actions: list[dict], transition: dict) -> None:
    focus = _outcome_focus_for_transition(transition)
    if not focus:
        return

    description = f"Keep {focus['label']} in view."
    if actions and actions[-1].get("type") == "observe":
        actions[-1].setdefault("selector", focus["selector"])
        actions[-1].setdefault("duration_seconds", 2)
        if not actions[-1].get("description"):
            actions[-1]["description"] = description
        return

    actions.append(
        {
            "action_id": f"{_slug(transition.get('candidate_id') or transition.get('label') or 'probe')}-outcome",
            "type": "observe",
            "description": description,
            "selector": focus["selector"],
            "duration_seconds": 2,
        }
    )


def _outcome_focus_for_transition(transition: dict) -> dict | None:
    summary = transition.get("outcome_summary") or {}
    if summary.get("url_changed"):
        return None

    changed_controls = summary.get("changed_controls") or []
    if not changed_controls:
        return None

    action_selectors = {
        action.get("selector")
        for action in transition.get("actions", [])
        if action.get("selector")
    }
    candidates = []
    for index, control in enumerate(changed_controls):
        selectors = control.get("selectors") or []
        selector = selectors[0] if selectors else None
        if not selector:
            continue
        matches_action_selector = _control_matches_action_selector(selectors, action_selectors)

        changed_text = _changed_control_after_text(control)
        if not changed_text:
            continue

        name = (control.get("name") or "").strip()
        haystack = f"{name} {changed_text} {selector}".lower()
        score = 0
        if not matches_action_selector:
            score += 100
        else:
            score -= 30
        score += max(0, 20 - index)
        if any(term in haystack for term in PRIMARY_OUTCOME_TERMS):
            score += 60
        elif any(term in haystack for term in OUTCOME_FOCUS_TERMS):
            score += 40
        if any(term in haystack for term in PRIMARY_OUTCOME_SELECTOR_TERMS):
            score += 45
        if any(term in haystack for term in SECONDARY_OUTCOME_TERMS):
            score -= 20
        if len(changed_text) > 12:
            score += 10

        candidates.append(
            {
                "score": score,
                "selector": selector,
                "label": name or changed_text[:48],
            }
        )

    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate["score"])


def _changed_control_after_text(control: dict) -> str:
    parts = []
    for change in (control.get("changes") or {}).values():
        before = str(change.get("before") or "").strip()
        after = str(change.get("after") or "").strip()
        if after and after != before:
            parts.append(after)
    return " ".join(parts)


def _control_has_changed_value(control: dict) -> bool:
    for change in (control.get("changes") or {}).values():
        before = str(change.get("before") or "").strip()
        after = str(change.get("after") or "").strip()
        if after and after != before:
            return True
    return False


def _clean_transition_label(label: str) -> str:
    return re.sub(r"\s+", " ", label).strip() or "interaction"


def _transition_title(kind: str, label: str) -> str:
    if kind == "input_submit":
        return label.capitalize()
    if kind == "toggle":
        if label.lower().startswith("toggle"):
            return label
        return f"Toggle {label}"
    if kind == "click":
        return f"Open {label}"
    return label.capitalize()


def _intro_scene(title: str, final_url: str | None) -> dict:
    return _scene(
        scene_id="intro",
        title="Introduce the app",
        goal=f"Establish what {title} is before interacting with it.",
        scene_type="intro",
        duration_seconds=4,
        actions=[
            {
                "action_id": "observe-homepage",
                "type": "observe",
                "description": "Show the loaded app landing state.",
            }
        ],
        success_criteria="The app is loaded and the title or main screen is visible.",
        narration_hint=f"Introduce {title} and what the viewer is about to see.",
        target_url=final_url,
    )


def _input_scene(element: dict) -> dict:
    selector = _first_selector(element)
    label = _element_name(element, fallback="primary input")
    value = _sample_value_for_input(element)
    actions = [
        {
            "action_id": "focus-input",
            "type": "click",
            "selector": selector,
            "description": f"Focus the {label}.",
        },
        {
            "action_id": "fill-input",
            "type": "fill",
            "selector": selector,
            "value": value,
            "description": f"Enter sample text into the {label}.",
        },
    ]

    if element.get("type") in (None, "", "search", "text"):
        actions.append(
            {
                "action_id": "submit-input",
                "type": "press",
                "selector": selector,
                "key": "Enter",
                "description": "Submit the input if the app supports enter-to-submit.",
            }
        )

    return _scene(
        scene_id=f"use-{_slug(label)}",
        title=f"Use {label}",
        goal=f"Show the viewer how the app responds when a user enters {label}.",
        scene_type="interaction",
        duration_seconds=8,
        actions=actions,
        success_criteria="The app accepts the input and visibly responds or stores the value.",
        narration_hint=f"Explain the main workflow around the {label}.",
        target_element=element,
    )


def _button_scene(element: dict, index: int) -> dict:
    selector = _first_selector(element)
    label = _element_name(element, fallback=f"button {index}")

    return _scene(
        scene_id=f"click-{_slug(label)}",
        title=f"Click {label}",
        goal=f"Demonstrate the result of clicking {label}.",
        scene_type="interaction",
        duration_seconds=6,
        actions=[
            {
                "action_id": "click-button",
                "type": "click",
                "selector": selector,
                "description": f"Click {label}.",
            }
        ],
        success_criteria="The click causes a visible state change, navigation, or confirmation.",
        narration_hint=f"Call out what {label} does for the user.",
        target_element=element,
    )


def _navigation_scene(links: list[dict]) -> dict:
    link_names = [_element_name(link, fallback="link") for link in links]
    return _scene(
        scene_id="review-navigation",
        title="Review navigation",
        goal="Show the major navigation or reference links found in the app.",
        scene_type="overview",
        duration_seconds=5,
        actions=[
            {
                "action_id": "observe-links",
                "type": "observe",
                "description": f"Show available links: {', '.join(link_names)}.",
            }
        ],
        success_criteria="The relevant navigation or support links are visible.",
        narration_hint="Summarize where users can go next from this screen.",
        target_elements=links,
    )


def _static_walkthrough_scene(dom: dict) -> dict:
    heading_names = [heading.get("text") for heading in dom.get("headings", []) if heading.get("text")]
    description = ", ".join(heading_names[:3]) or "the visible page content"

    return _scene(
        scene_id="static-overview",
        title="Show visible content",
        goal="Provide a useful overview when no clear interactive controls are visible.",
        scene_type="overview",
        duration_seconds=5,
        actions=[
            {
                "action_id": "observe-content",
                "type": "observe",
                "description": f"Show {description}.",
            }
        ],
        success_criteria="The viewer can understand the visible page content.",
        narration_hint="Explain what is visible and why it matters.",
    )


def _outro_scene(title: str) -> dict:
    return _scene(
        scene_id="outro",
        title="Wrap up",
        goal=f"Close the demo with the main takeaway for {title}.",
        scene_type="outro",
        duration_seconds=4,
        actions=[
            {
                "action_id": "observe-final-state",
                "type": "observe",
                "description": "Hold on the final app state.",
            }
        ],
        success_criteria="The final app state is stable and readable.",
        narration_hint=f"Summarize the value shown in the {title} walkthrough.",
    )


def _scene(
    scene_id: str,
    title: str,
    goal: str,
    scene_type: str,
    duration_seconds: int,
    actions: list[dict],
    success_criteria: str,
    narration_hint: str,
    **extra,
) -> dict:
    scene = {
        "scene_id": scene_id,
        "title": title,
        "type": scene_type,
        "goal": goal,
        "duration_seconds": duration_seconds,
        "actions": actions,
        "success_criteria": success_criteria,
        "narration_hint": narration_hint,
    }
    scene.update({key: value for key, value in extra.items() if value is not None})
    return scene


def _page_title(page: dict, dom: dict) -> str:
    title = (page.get("title") or "").strip()
    if title:
        return title

    for heading in dom.get("headings", []):
        text = (heading.get("text") or "").strip()
        if text:
            return text

    return "this app"


def _host_for(url: str | None) -> str | None:
    if not url:
        return None

    return urlparse(url).netloc or None


def _same_url_origin(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False

    parsed_left = urlparse(left)
    parsed_right = urlparse(right)
    return parsed_left.scheme == parsed_right.scheme and parsed_left.netloc == parsed_right.netloc


def _same_url_path(left: str | None, right: str | None) -> bool:
    if not _same_url_origin(left, right):
        return False

    return urlparse(left).path == urlparse(right).path


def _route_fragment_bonus(url: str | None) -> int:
    if not url:
        return 0

    fragment = urlparse(url).fragment.strip("/")
    return 2 if fragment else 0


def _first_actionable_input(inputs: list[dict]) -> dict | None:
    ignored_types = {"button", "checkbox", "color", "file", "hidden", "image", "radio", "range", "reset", "submit"}
    for element in inputs:
        input_type = (element.get("type") or "text").lower()
        if input_type not in ignored_types and _first_selector(element):
            return element
    return None


def _meaningful_buttons(buttons: list[dict]) -> list[dict]:
    return [button for button in buttons if _first_selector(button)]


def _meaningful_links(links: list[dict]) -> list[dict]:
    return [link for link in links if _element_name(link) and link.get("href")]


def _sample_value_for_input(element: dict) -> str:
    input_type = (element.get("type") or "text").lower()
    label = _element_name(element).lower()
    selector_text = " ".join(element.get("selectors") or []).lower()
    hint_text = f"{label} {selector_text}"

    if input_type == "email" or "email" in hint_text:
        return "demo@example.com"
    if input_type == "password":
        return "demo-password"
    if input_type in {"number", "range"}:
        return "42"
    if input_type == "search" or "search" in hint_text:
        return "Toronto"
    if "todo" in hint_text or "task" in hint_text:
        return "Ship the Folio demo"

    return "Build a polished demo video"


def _element_name(element: dict, fallback: str = "") -> str:
    for key in ("accessible_name", "text", "label", "name"):
        value = (element.get(key) or "").strip()
        if value:
            return value
    return fallback


def _first_selector(element: dict) -> str | None:
    selectors = element.get("selectors") or []
    return selectors[0] if selectors else None


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug or "scene"
