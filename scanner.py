from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from source_context import (
    MAX_COMPONENTS,
    MAX_FILE_BYTES,
    MAX_FILE_CHARS,
    MAX_README_CHARS,
    MAX_README_FILES,
    MAX_ROUTES,
    MAX_SOURCE_FILES,
    MAX_TREE_ENTRIES,
    MAX_UI_STRINGS,
    build_source_context,
)


DEFAULT_VIEWPORT = {"width": 1440, "height": 900}
MAX_ELEMENTS_PER_GROUP = 100
MAX_TEXT_BLOCKS = 80
MAX_ACCESSIBILITY_NODES = 120
MAX_ACCESSIBILITY_DEPTH = 4
DEFAULT_PROBE_DEPTH = 3
DEFAULT_MAX_STATES = 16
DEFAULT_MAX_ACTIONS_PER_STATE = 5
DEFAULT_LLM_MODEL = "gpt-5.1"
MAX_CANDIDATE_PATHS = 30
MAX_OUTCOME_ITEMS = 12
MAX_LLM_EXPANSIONS = 2
MAX_LLM_EXPANSION_ACTIONS = 8
MAX_LLM_EXPANSION_STATES = 10
MAX_LLM_EXPANSION_ELEMENTS = 80
MAX_LLM_EXPLORATION_GOALS = 12
MAX_LLM_GOAL_WORKFLOWS = 4
MAX_LLM_GOAL_STATES = 16
MAX_LLM_GOAL_VALIDATIONS = 8
MAX_LLM_GOAL_REPAIRS = 4
MAX_LLM_GOAL_REPAIR_CANDIDATES = 2
MAX_LLM_COVERAGE_AUDIT_WORKFLOWS = 8
MAX_LLM_COVERAGE_AUDIT_FEATURES = 16
MAX_LLM_OUTCOME_REPAIRS = 4
MAX_LLM_OUTCOME_REPAIR_CANDIDATES = 2
PROBE_ACTION_TIMEOUT_MS = 8_000
LLM_ACTION_TYPES = ["observe", "click", "double_click", "fill", "press", "select"]
SELECTOR_LLM_ACTION_TYPES = {"click", "double_click", "fill", "press", "select"}
POINTER_LLM_ACTION_TYPES = {"click", "double_click"}
REVEALING_LLM_ACTION_TYPES = {"click", "double_click", "press", "select"}
POST_REVEAL_DYNAMIC_SELECTOR_ACTION_TYPES = {"fill", "press", "select"}
STATE_CHANGING_KIND_BONUSES = {
    "input_submit": 6,
    "toggle": 8,
    "llm_workflow": 24,
    "llm_goal_workflow": 24,
}
HIGH_RISK_WORDS = {
    "account",
    "billing",
    "buy",
    "checkout",
    "credentials",
    "fdbk",
    "feedback",
    "invite",
    "logout",
    "password",
    "pay",
    "payment",
    "purchase",
    "send",
    "sign out",
    "subscribe",
    "token",
}
CONDITIONAL_RISK_WORDS = {
    "cancel",
    "clear",
    "delete",
    "remove",
    "reset",
}
SAFE_LOCAL_PRODUCT_ACTION_TERMS = {
    "active",
    "calculator",
    "clear completed",
    "clear-completed",
    "complete",
    "completed",
    "destroy",
    "entry",
    "edit",
    "editing",
    "field",
    "filter",
    "form",
    "input",
    "item",
    "list",
    "label",
    "note",
    "row",
    "selection",
    "task",
    "todo",
    "todo-item",
}
CLICK_ONLY_WORKFLOW_TERMS = SAFE_LOCAL_PRODUCT_ACTION_TERMS | {
    "apply",
    "calculate",
    "compute",
    "convert",
    "export",
    "mode",
    "option",
    "preview",
    "save",
    "sort",
    "tab",
    "toggle",
    "view",
}
RISK_WORDS = HIGH_RISK_WORDS | CONDITIONAL_RISK_WORDS


def build_job_id(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc or parsed.path or "scan"
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", host).strip("-").lower()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}-{slug or 'scan'}"


async def scan(
    url: str,
    output_root: str | Path = "outputs",
    job_id: str | None = None,
    timeout_ms: int = 30_000,
    viewport: dict[str, int] | None = None,
    probe_depth: int = DEFAULT_PROBE_DEPTH,
    max_states: int = DEFAULT_MAX_STATES,
    max_actions_per_state: int = DEFAULT_MAX_ACTIONS_PER_STATE,
    source_root: str | Path | None = None,
    source_max_tree_entries: int = MAX_TREE_ENTRIES,
    source_max_files: int = MAX_SOURCE_FILES,
    source_max_readme_files: int = MAX_README_FILES,
    source_max_readme_chars: int = MAX_README_CHARS,
    source_max_routes: int = MAX_ROUTES,
    source_max_components: int = MAX_COMPONENTS,
    source_max_ui_strings: int = MAX_UI_STRINGS,
    source_max_file_chars: int = MAX_FILE_CHARS,
    source_max_file_bytes: int = MAX_FILE_BYTES,
    llm_expand: bool = False,
    llm_goals: bool = False,
    validate_goals: bool = False,
    repair_goals: bool = False,
    coverage_audit: bool = False,
    repair_outcomes: bool = False,
    llm_model: str | None = None,
    max_llm_expansions: int = MAX_LLM_EXPANSIONS,
    max_llm_goals: int = MAX_LLM_EXPLORATION_GOALS,
    max_goal_validations: int = MAX_LLM_GOAL_VALIDATIONS,
    max_goal_repairs: int = MAX_LLM_GOAL_REPAIRS,
    max_coverage_audit_workflows: int = MAX_LLM_COVERAGE_AUDIT_WORKFLOWS,
    max_outcome_repairs: int = MAX_LLM_OUTCOME_REPAIRS,
) -> dict:
    job_id = job_id or build_job_id(url)
    output_dir = Path(output_root) / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    screenshot_path = output_dir / "screenshot.png"
    scan_path = output_dir / "scan.json"
    console_errors: list[dict] = []
    page_errors: list[dict] = []
    viewport = dict(viewport or DEFAULT_VIEWPORT)
    states: list[dict] = []
    transitions: list[dict] = []
    candidate_paths: list[dict] = []
    llm_expansion: dict = {"status": "disabled"}
    exploration_goals: dict = {"status": "disabled"}
    goal_validation: dict = {"status": "disabled"}
    coverage_audit_result: dict = {"status": "disabled"}
    outcome_repair: dict = {"status": "disabled"}
    source_context = build_source_context(
        source_root,
        max_tree_entries=source_max_tree_entries,
        max_source_files=source_max_files,
        max_readme_files=source_max_readme_files,
        max_readme_chars=source_max_readme_chars,
        max_routes=source_max_routes,
        max_components=source_max_components,
        max_ui_strings=source_max_ui_strings,
        max_file_chars=source_max_file_chars,
        max_file_bytes=source_max_file_bytes,
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(viewport=viewport)
        page = await context.new_page()

        _attach_page_listeners(page, console_errors, page_errors)

        response_status = None
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            response_status = response.status if response else None
            await _wait_for_settle(page)

            initial_state = await _capture_state(
                page,
                state_id="initial",
                output_dir=output_dir,
                screenshot_path=screenshot_path,
                depth=0,
                parent_state_id=None,
                transition=None,
                replay_actions=[],
            )
            states.append(initial_state)
            final_url = initial_state["url"]
            title = initial_state["title"]

            if probe_depth > 0 and max_states > 1:
                transitions = await _explore_states(
                    browser=browser,
                    start_url=final_url,
                    output_dir=output_dir,
                    states=states,
                    viewport=viewport,
                    console_errors=console_errors,
                    page_errors=page_errors,
                    probe_depth=probe_depth,
                    max_states=max_states,
                    max_actions_per_state=max_actions_per_state,
                    timeout_ms=timeout_ms,
                )
                candidate_paths = _candidate_paths_for_states(final_url, states)

            if llm_expand:
                llm_expansion = await _expand_states_with_llm(
                    browser=browser,
                    start_url=final_url,
                    output_dir=output_dir,
                    states=states,
                    transitions=transitions,
                    candidate_paths=candidate_paths,
                    viewport=viewport,
                    console_errors=console_errors,
                    page_errors=page_errors,
                    timeout_ms=timeout_ms,
                    source_context=source_context,
                    model=llm_model,
                    max_expansions=max_llm_expansions,
                )
                candidate_paths = _candidate_paths_for_states(final_url, states)

            if llm_goals or validate_goals or repair_goals or coverage_audit:
                exploration_goals = _generate_llm_exploration_goals(
                    start_url=final_url,
                    states=states,
                    candidate_paths=candidate_paths,
                    source_context=source_context,
                    model=llm_model,
                    max_goals=max_llm_goals,
                )

            if validate_goals or repair_goals:
                goal_validation = await _validate_exploration_goal_candidates(
                    browser=browser,
                    start_url=final_url,
                    output_dir=output_dir,
                    states=states,
                    transitions=transitions,
                    exploration_goals=exploration_goals,
                    viewport=viewport,
                    console_errors=console_errors,
                    page_errors=page_errors,
                    timeout_ms=timeout_ms,
                    max_validations=max_goal_validations,
                )

                if repair_goals:
                    repairs = await _repair_failed_goal_candidates(
                        browser=browser,
                        start_url=final_url,
                        output_dir=output_dir,
                        states=states,
                        transitions=transitions,
                        exploration_goals=exploration_goals,
                        goal_validation=goal_validation,
                        viewport=viewport,
                        console_errors=console_errors,
                        page_errors=page_errors,
                        timeout_ms=timeout_ms,
                        model=llm_model,
                        max_repairs=max_goal_repairs,
                    )
                    goal_validation["repairs"] = repairs
                    goal_validation["total_attempted"] = goal_validation.get("attempted", 0) + repairs.get("attempted", 0)
                    goal_validation["total_accepted"] = goal_validation.get("accepted", 0) + repairs.get("accepted", 0)
                candidate_paths = _candidate_paths_for_states(final_url, states)

            if coverage_audit:
                candidate_paths = _candidate_paths_for_states(final_url, states)
                coverage_audit_result = await _audit_coverage_with_llm(
                    browser=browser,
                    start_url=final_url,
                    output_dir=output_dir,
                    states=states,
                    transitions=transitions,
                    candidate_paths=candidate_paths,
                    exploration_goals=exploration_goals,
                    goal_validation=goal_validation,
                    viewport=viewport,
                    console_errors=console_errors,
                    page_errors=page_errors,
                    timeout_ms=timeout_ms,
                    source_context=source_context,
                    model=llm_model,
                    max_workflows=max_coverage_audit_workflows,
                )
                candidate_paths = _candidate_paths_for_states(final_url, states)

            if repair_outcomes:
                candidate_paths = _candidate_paths_for_states(final_url, states)
                outcome_repair = await _repair_unclear_outcome_workflows(
                    browser=browser,
                    start_url=final_url,
                    output_dir=output_dir,
                    states=states,
                    transitions=transitions,
                    candidate_paths=candidate_paths,
                    viewport=viewport,
                    console_errors=console_errors,
                    page_errors=page_errors,
                    timeout_ms=timeout_ms,
                    model=llm_model,
                    max_repairs=max_outcome_repairs,
                )
                candidate_paths = _candidate_paths_for_states(final_url, states)
        finally:
            await context.close()
            await browser.close()

    initial_state = states[0]

    result = {
        "job_id": job_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input": {
            "url": url,
            "timeout_ms": timeout_ms,
            "viewport": viewport,
            "probe_depth": probe_depth,
            "max_states": max_states,
            "max_actions_per_state": max_actions_per_state,
            "llm_expand": llm_expand,
            "llm_goals": llm_goals,
            "validate_goals": validate_goals,
            "repair_goals": repair_goals,
            "coverage_audit": coverage_audit,
            "repair_outcomes": repair_outcomes,
            "llm_model": llm_model,
            "max_llm_expansions": max_llm_expansions,
            "max_llm_goals": max_llm_goals,
            "max_goal_validations": max_goal_validations,
            "max_goal_repairs": max_goal_repairs,
            "max_coverage_audit_workflows": max_coverage_audit_workflows,
            "max_outcome_repairs": max_outcome_repairs,
            "source_root": str(source_root) if source_root else None,
            "source_limits": {
                "max_tree_entries": source_max_tree_entries,
                "max_source_files": source_max_files,
                "max_readme_files": source_max_readme_files,
                "max_readme_chars": source_max_readme_chars,
                "max_routes": source_max_routes,
                "max_components": source_max_components,
                "max_ui_strings": source_max_ui_strings,
                "max_file_chars": source_max_file_chars,
                "max_file_bytes": source_max_file_bytes,
            }
            if source_root
            else None,
        },
        "page": {
            "title": title,
            "final_url": final_url,
            "response_status": response_status,
        },
        "artifacts": {
            "output_dir": str(output_dir),
            "screenshot": str(screenshot_path),
            "scan_json": str(scan_path),
        },
        "dom": initial_state["dom"],
        "accessibility": initial_state["accessibility"],
        "states": states,
        "transitions": transitions,
        "candidate_paths": candidate_paths,
        "llm_expansion": llm_expansion,
        "exploration_goals": exploration_goals,
        "goal_validation": goal_validation,
        "coverage_audit": coverage_audit_result,
        "outcome_repair": outcome_repair,
        "source_context": source_context,
        "browser_errors": {
            "console": console_errors,
            "page": page_errors,
        },
    }

    scan_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def run_scan(url: str, **kwargs) -> dict:
    return asyncio.run(scan(url, **kwargs))


def _attach_page_listeners(page, console_errors: list[dict], page_errors: list[dict]) -> None:
    page.on("console", _capture_console_errors(console_errors))
    page.on("pageerror", lambda exc: page_errors.append({"message": str(exc)}))


async def _capture_state(
    page,
    state_id: str,
    output_dir: Path,
    screenshot_path: Path,
    depth: int,
    parent_state_id: str | None,
    transition: dict | None,
    replay_actions: list[dict],
) -> dict:
    title = await page.title()
    dom_summary = await page.evaluate(_dom_summary_script())
    accessibility_snapshot = await page.accessibility.snapshot(interesting_only=True)
    await page.screenshot(path=str(screenshot_path), full_page=True)

    return {
        "state_id": state_id,
        "title": title,
        "url": page.url,
        "depth": depth,
        "parent_state_id": parent_state_id,
        "transition": transition,
        "replay_actions": replay_actions,
        "artifacts": {
            "screenshot": str(screenshot_path),
        },
        "dom": dom_summary,
        "accessibility": _prune_accessibility_tree(accessibility_snapshot),
        "signature": _state_signature(page.url, dom_summary),
    }


async def _explore_states(
    browser,
    start_url: str,
    output_dir: Path,
    states: list[dict],
    viewport: dict[str, int],
    console_errors: list[dict],
    page_errors: list[dict],
    probe_depth: int,
    max_states: int,
    max_actions_per_state: int,
    timeout_ms: int,
) -> list[dict]:
    transitions: list[dict] = []
    signatures = {states[0]["signature"]: states[0]["state_id"]}
    cursor = 0

    while cursor < len(states) and len(states) < max_states:
        current_state = states[cursor]
        cursor += 1

        if current_state["depth"] >= probe_depth:
            continue

        candidates = _action_candidates_for_state(current_state, start_url)[:max_actions_per_state]
        for candidate in candidates:
            if len(states) >= max_states:
                break

            transition = {
                "from": current_state["state_id"],
                "to": None,
                "candidate_id": candidate["candidate_id"],
                "label": candidate["label"],
                "kind": candidate["kind"],
                "actions": candidate["actions"],
                "status": "pending",
            }

            trial = await _run_probe_candidate(
                browser=browser,
                start_url=start_url,
                output_dir=output_dir,
                viewport=viewport,
                parent_state=current_state,
                candidate=candidate,
                console_errors=console_errors,
                page_errors=page_errors,
                timeout_ms=timeout_ms,
                next_index=len(states),
            )

            if trial["status"] != "success":
                transition["status"] = trial["status"]
                transition["error"] = trial.get("error")
                if trial.get("failure"):
                    transition["failure"] = trial["failure"]
                transitions.append(transition)
                continue

            next_state = trial["state"]
            transition["outcome_summary"] = _transition_outcome_summary(current_state, next_state)
            duplicate_state_id = signatures.get(next_state["signature"])
            if duplicate_state_id:
                transition["status"] = "duplicate"
                transition["to"] = duplicate_state_id
                transitions.append(transition)
                continue

            signatures[next_state["signature"]] = next_state["state_id"]
            transition["status"] = "success"
            transition["to"] = next_state["state_id"]
            next_state["transition"] = transition
            states.append(next_state)
            transitions.append(transition)

    return transitions


async def _expand_states_with_llm(
    browser,
    start_url: str,
    output_dir: Path,
    states: list[dict],
    transitions: list[dict],
    candidate_paths: list[dict],
    viewport: dict[str, int],
    console_errors: list[dict],
    page_errors: list[dict],
    timeout_ms: int,
    source_context: dict | None,
    model: str | None,
    max_expansions: int,
) -> dict:
    model = model or os.environ.get("FOLIO_LLM_MODEL") or DEFAULT_LLM_MODEL
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return {
            "status": "skipped",
            "reason": "Missing OPENAI_API_KEY.",
            "model": model,
            "accepted": 0,
            "attempted": 0,
        }

    try:
        payload = _call_openai_workflow_expander(
            _workflow_expansion_context(
                start_url=start_url,
                states=states,
                candidate_paths=candidate_paths,
                source_context=source_context,
            ),
            api_key=api_key,
            model=model,
        )
    except Exception as exc:
        return {
            "status": "failed",
            "model": model,
            "error": f"{type(exc).__name__}: {exc}",
            "accepted": 0,
            "attempted": 0,
        }

    states_by_id = {
        state.get("state_id"): state
        for state in states
        if state.get("state_id")
    }
    candidates, rejected = _normalize_llm_workflow_candidates(
        payload,
        states_by_id=states_by_id,
        max_expansions=max_expansions,
    )
    signatures = {
        state.get("signature"): state.get("state_id")
        for state in states
        if state.get("signature") and state.get("state_id")
    }
    accepted, attempted, results = await _validate_workflow_candidates(
        browser=browser,
        start_url=start_url,
        output_dir=output_dir,
        states=states,
        transitions=transitions,
        viewport=viewport,
        console_errors=console_errors,
        page_errors=page_errors,
        timeout_ms=timeout_ms,
        candidates=candidates,
        states_by_id=states_by_id,
        signatures=signatures,
        default_origin="llm",
    )

    return {
        "status": "completed",
        "model": model,
        "rationale": payload.get("rationale", ""),
        "attempted": attempted,
        "accepted": accepted,
        "rejected": rejected,
        "results": results,
    }


async def _validate_workflow_candidates(
    browser,
    start_url: str,
    output_dir: Path,
    states: list[dict],
    transitions: list[dict],
    viewport: dict[str, int],
    console_errors: list[dict],
    page_errors: list[dict],
    timeout_ms: int,
    candidates: list[dict],
    states_by_id: dict[str, dict] | None = None,
    signatures: dict[str, str] | None = None,
    default_origin: str = "llm",
) -> tuple[int, int, list[dict]]:
    states_by_id = states_by_id or {
        state.get("state_id"): state
        for state in states
        if state.get("state_id")
    }
    signatures = signatures or {
        state.get("signature"): state.get("state_id")
        for state in states
        if state.get("signature") and state.get("state_id")
    }

    accepted = 0
    attempted = 0
    results = []
    for candidate in candidates:
        parent_state = states_by_id.get(candidate["parent_state_id"])
        if not parent_state:
            results.append(
                {
                    **_llm_expansion_result(candidate, {"status": "skipped", "error": "Unknown parent_state_id."}),
                    "status": "skipped",
                }
            )
            continue

        attempted += 1
        transition = {
            "from": parent_state["state_id"],
            "to": None,
            "candidate_id": candidate["candidate_id"],
            "label": candidate["label"],
            "kind": candidate["kind"],
            "goal": candidate.get("goal"),
            "expected_outcome": candidate.get("expected_outcome"),
            "repair_note": candidate.get("repair_note"),
            "requested_parent_state_id": candidate.get("requested_parent_state_id"),
            "exploration_goal_id": candidate.get("exploration_goal_id"),
            "feature": candidate.get("feature"),
            "feature_id": candidate.get("feature_id"),
            "confidence": candidate.get("confidence"),
            "repaired_from": candidate.get("repaired_from"),
            "origin": candidate.get("origin") or default_origin,
            "actions": candidate["actions"],
            "status": "pending",
        }
        trial = await _run_probe_candidate(
            browser=browser,
            start_url=start_url,
            output_dir=output_dir,
            viewport=viewport,
            parent_state=parent_state,
            candidate=candidate,
            console_errors=console_errors,
            page_errors=page_errors,
            timeout_ms=timeout_ms,
            next_index=len(states),
        )

        if trial["status"] != "success":
            transition["status"] = trial["status"]
            transition["error"] = trial.get("error")
            if trial.get("failure"):
                transition["failure"] = trial["failure"]
            transitions.append(transition)
            results.append(_llm_expansion_result(candidate, transition))
            continue

        next_state = trial["state"]
        transition["outcome_summary"] = _transition_outcome_summary(parent_state, next_state)
        if _is_llm_candidate(candidate) and not _has_meaningful_llm_outcome(
            transition["outcome_summary"],
            actions=candidate.get("actions", []),
        ):
            transition["status"] = "no_meaningful_outcome"
            transition["to"] = next_state["state_id"]
            transitions.append(transition)
            results.append(_llm_expansion_result(candidate, transition))
            continue

        duplicate_state_id = signatures.get(next_state["signature"])
        if duplicate_state_id and not _has_meaningful_llm_outcome(
            transition["outcome_summary"],
            actions=candidate.get("actions", []),
        ):
            transition["status"] = "duplicate"
            transition["to"] = duplicate_state_id
            transitions.append(transition)
            results.append(_llm_expansion_result(candidate, transition))
            continue

        if duplicate_state_id:
            transition["duplicate_of"] = duplicate_state_id
        else:
            signatures[next_state["signature"]] = next_state["state_id"]
        transition["status"] = "success"
        transition["to"] = next_state["state_id"]
        next_state["transition"] = transition
        states.append(next_state)
        states_by_id[next_state["state_id"]] = next_state
        transitions.append(transition)
        accepted += 1
        results.append(_llm_expansion_result(candidate, transition))

    return accepted, attempted, results


def _llm_expansion_result(candidate: dict, transition: dict) -> dict:
    return {
        "candidate_id": candidate.get("candidate_id"),
        "parent_state_id": candidate.get("parent_state_id"),
        "requested_parent_state_id": candidate.get("requested_parent_state_id"),
        "label": candidate.get("label"),
        "goal": candidate.get("goal"),
        "exploration_goal_id": candidate.get("exploration_goal_id"),
        "feature": candidate.get("feature"),
        "feature_id": candidate.get("feature_id"),
        "confidence": candidate.get("confidence"),
        "repaired_from": candidate.get("repaired_from"),
        "repair_note": candidate.get("repair_note"),
        "status": transition.get("status"),
        "to": transition.get("to"),
        "duplicate_of": transition.get("duplicate_of"),
        "error": transition.get("error"),
        "failure": _failure_result_for_scan(transition.get("failure")),
    }


def _is_llm_candidate(candidate: dict) -> bool:
    return str(candidate.get("origin") or "").startswith("llm") or str(candidate.get("kind") or "").startswith("llm")


def _has_meaningful_llm_outcome(outcome_summary: dict | None, actions: list[dict] | None = None) -> bool:
    if not outcome_summary:
        return False
    if outcome_summary.get("url_changed") or outcome_summary.get("added_text") or outcome_summary.get("removed_text"):
        return True

    self_filled_selectors = {
        action.get("selector")
        for action in actions or []
        if action.get("type") in {"fill", "select"} and action.get("selector")
    }
    for control in outcome_summary.get("changed_controls", []):
        selectors = set(control.get("selectors") or [])
        if selectors and selectors & self_filled_selectors:
            continue
        changes = control.get("changes", {})
        for change in changes.values():
            before = change.get("before")
            after = change.get("after")
            if str(before or "").strip() != str(after or "").strip() and str(after or "").strip():
                return True
    return False


async def _run_probe_candidate(
    browser,
    start_url: str,
    output_dir: Path,
    viewport: dict[str, int],
    parent_state: dict,
    candidate: dict,
    console_errors: list[dict],
    page_errors: list[dict],
    timeout_ms: int,
    next_index: int,
) -> dict:
    context = await browser.new_context(viewport=viewport)
    page = await context.new_page()
    _attach_page_listeners(page, console_errors, page_errors)

    try:
        try:
            await page.goto(start_url, wait_until="domcontentloaded", timeout=timeout_ms)
            await _wait_for_settle(page)
        except Exception as exc:
            return {"status": "navigation_failed", "error": f"{type(exc).__name__}: {exc}"}

        replay_result = await _execute_probe_actions(page, parent_state.get("replay_actions", []))
        if replay_result["status"] != "success":
            failure = await _capture_probe_failure(
                page=page,
                output_dir=output_dir,
                parent_state=parent_state,
                candidate=candidate,
                action_result=replay_result,
                stage="replay",
            )
            return {"status": "replay_failed", "error": replay_result.get("error"), "failure": failure}

        action_result = await _execute_probe_actions(page, candidate["actions"])
        if action_result["status"] != "success":
            failure = await _capture_probe_failure(
                page=page,
                output_dir=output_dir,
                parent_state=parent_state,
                candidate=candidate,
                action_result=action_result,
                stage="candidate",
            )
            return {"status": "action_failed", "error": action_result.get("error"), "failure": failure}

        state_id = f"state-{next_index}-{_slug(candidate['label'])}"
        screenshot_path = output_dir / f"{state_id}.png"
        replay_actions = parent_state.get("replay_actions", []) + candidate["actions"]
        next_state = await _capture_state(
            page,
            state_id=state_id,
            output_dir=output_dir,
            screenshot_path=screenshot_path,
            depth=parent_state["depth"] + 1,
            parent_state_id=parent_state["state_id"],
            transition=None,
            replay_actions=replay_actions,
        )
        return {"status": "success", "state": next_state}
    finally:
        await context.close()


async def _execute_probe_actions(page, actions: list[dict]) -> dict:
    completed_actions = []
    for index, action in enumerate(actions):
        try:
            action_type = action["type"]
            selector = action.get("selector")
            if action_type in SELECTOR_LLM_ACTION_TYPES:
                if not selector:
                    return {
                        "status": "failed",
                        "error": "Missing selector",
                        "failed_action_index": index,
                        "failed_action": action,
                        "completed_actions": completed_actions,
                    }
                locator = page.locator(selector).first
                wait_state = "attached" if action.get("allow_hidden") else "visible"
                await locator.wait_for(state=wait_state, timeout=PROBE_ACTION_TIMEOUT_MS)
                if not action.get("allow_hidden"):
                    await locator.scroll_into_view_if_needed(timeout=PROBE_ACTION_TIMEOUT_MS)

            if action_type == "click":
                try:
                    await locator.click(timeout=PROBE_ACTION_TIMEOUT_MS)
                except Exception:
                    if await _click_associated_label(page, selector):
                        pass
                    elif not action.get("allow_hidden"):
                        raise
                    else:
                        await page.evaluate(
                            "(selector) => document.querySelector(selector)?.click()",
                            selector,
                        )
            elif action_type == "double_click":
                await locator.dblclick(timeout=PROBE_ACTION_TIMEOUT_MS)
            elif action_type == "fill":
                await locator.fill(action.get("value", ""), timeout=PROBE_ACTION_TIMEOUT_MS)
            elif action_type == "select":
                await locator.select_option(value=action.get("value", ""), timeout=PROBE_ACTION_TIMEOUT_MS)
            elif action_type == "press":
                await locator.press(action.get("key", "Enter"), timeout=PROBE_ACTION_TIMEOUT_MS)
            elif action_type == "observe":
                await page.wait_for_timeout(500)
            else:
                return {
                    "status": "failed",
                    "error": f"Unsupported probe action: {action_type}",
                    "failed_action_index": index,
                    "failed_action": action,
                    "completed_actions": completed_actions,
                }

            await _wait_for_settle(page)
            completed_actions.append(action)
        except Exception as exc:
            return {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "failed_action_index": index,
                "failed_action": action,
                "completed_actions": completed_actions,
            }

    return {"status": "success"}


async def _capture_probe_failure(
    page,
    output_dir: Path,
    parent_state: dict,
    candidate: dict,
    action_result: dict,
    stage: str,
) -> dict:
    failure_id = _slug(
        f"failure-{parent_state.get('state_id')}-{candidate.get('candidate_id')}-{stage}-{action_result.get('failed_action_index')}"
    )
    screenshot_path = output_dir / f"{failure_id}.png"
    dom_path = output_dir / f"{failure_id}.dom.json"

    title = ""
    dom_summary = {}
    screenshot_artifact = None
    dom_artifact = None
    try:
        title = await page.title()
    except Exception:
        title = ""

    try:
        dom_summary = await page.evaluate(_dom_summary_script())
        dom_path.write_text(json.dumps(dom_summary, indent=2), encoding="utf-8")
        dom_artifact = str(dom_path)
    except Exception:
        dom_summary = {}

    try:
        await page.screenshot(path=str(screenshot_path), full_page=False)
        screenshot_artifact = str(screenshot_path)
    except Exception:
        screenshot_artifact = None

    visible_context = _state_for_expander(
        {
            "state_id": f"{parent_state.get('state_id')}:failure",
            "title": title,
            "url": page.url,
            "depth": parent_state.get("depth"),
            "parent_state_id": parent_state.get("state_id"),
            "replay_actions": parent_state.get("replay_actions", []),
            "dom": dom_summary,
        }
    )
    return {
        "stage": stage,
        "url": page.url,
        "title": title,
        "parent_state_id": parent_state.get("state_id"),
        "candidate_id": candidate.get("candidate_id"),
        "label": candidate.get("label"),
        "error": action_result.get("error"),
        "failed_action_index": action_result.get("failed_action_index"),
        "failed_action": action_result.get("failed_action"),
        "completed_actions": action_result.get("completed_actions", []),
        "artifacts": {
            "screenshot": screenshot_artifact,
            "dom_json": dom_artifact,
        },
        "visible_context": visible_context,
    }


def _failure_result_for_scan(failure: dict | None) -> dict | None:
    if not failure:
        return None

    return {
        "stage": failure.get("stage"),
        "url": failure.get("url"),
        "title": failure.get("title"),
        "parent_state_id": failure.get("parent_state_id"),
        "candidate_id": failure.get("candidate_id"),
        "label": failure.get("label"),
        "error": failure.get("error"),
        "failed_action_index": failure.get("failed_action_index"),
        "failed_action": failure.get("failed_action"),
        "completed_actions": failure.get("completed_actions", []),
        "artifacts": failure.get("artifacts", {}),
        "visible_context": failure.get("visible_context", {}),
    }


async def _click_associated_label(page, selector: str) -> bool:
    return bool(
        await page.evaluate(
            """(selector) => {
                const element = document.querySelector(selector);
                if (!element || !element.id) return false;
                const label = document.querySelector(`label[for="${CSS.escape(element.id)}"]`);
                if (!label) return false;
                label.click();
                return true;
            }""",
            selector,
        )
    )


def _call_openai_workflow_expander(context: dict, api_key: str, model: str) -> dict:
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": model,
        "input": [
            {
                "role": "developer",
                "content": _llm_workflow_expander_prompt(),
            },
            {
                "role": "user",
                "content": json.dumps(context, indent=2),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "folio_workflow_expansion",
                "strict": True,
                "schema": _llm_workflow_expansion_schema(),
            }
        },
        "max_output_tokens": 3_000,
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


def _generate_llm_exploration_goals(
    start_url: str,
    states: list[dict],
    candidate_paths: list[dict],
    source_context: dict | None,
    model: str | None,
    max_goals: int,
) -> dict:
    model = model or os.environ.get("FOLIO_LLM_MODEL") or DEFAULT_LLM_MODEL
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return {
            "status": "skipped",
            "reason": "Missing OPENAI_API_KEY.",
            "model": model,
            "goal_count": 0,
            "workflow_candidate_count": 0,
        }

    try:
        payload = _call_openai_exploration_goals(
            _exploration_goal_context(
                start_url=start_url,
                states=states,
                candidate_paths=candidate_paths,
                source_context=source_context,
                max_goals=max_goals,
            ),
            api_key=api_key,
            model=model,
            max_goals=max_goals,
        )
    except Exception as exc:
        return {
            "status": "failed",
            "model": model,
            "error": f"{type(exc).__name__}: {exc}",
            "goal_count": 0,
            "workflow_candidate_count": 0,
        }

    states_by_id = {
        state.get("state_id"): state
        for state in states
        if state.get("state_id")
    }
    normalized = _normalize_exploration_goal_payload(
        payload,
        states_by_id=states_by_id,
        max_goals=max_goals,
    )
    normalized.update(
        {
            "status": "completed",
            "model": model,
            "goal_count": len(normalized.get("goals", [])),
            "workflow_candidate_count": sum(
                len(goal.get("workflow_candidates", []))
                for goal in normalized.get("goals", [])
            ),
        }
    )
    return normalized


async def _validate_exploration_goal_candidates(
    browser,
    start_url: str,
    output_dir: Path,
    states: list[dict],
    transitions: list[dict],
    exploration_goals: dict,
    viewport: dict[str, int],
    console_errors: list[dict],
    page_errors: list[dict],
    timeout_ms: int,
    max_validations: int,
) -> dict:
    if exploration_goals.get("status") != "completed":
        return {
            "status": "skipped",
            "reason": f"Exploration goals were not completed: {exploration_goals.get('status')}",
            "attempted": 0,
            "accepted": 0,
            "results": [],
        }

    candidates = _goal_validation_candidates(exploration_goals, max_validations=max_validations)
    if not candidates:
        return {
            "status": "completed",
            "attempted": 0,
            "accepted": 0,
            "results": [],
        }

    states_by_id = {
        state.get("state_id"): state
        for state in states
        if state.get("state_id")
    }
    signatures = {
        state.get("signature"): state.get("state_id")
        for state in states
        if state.get("signature") and state.get("state_id")
    }
    accepted, attempted, results = await _validate_workflow_candidates(
        browser=browser,
        start_url=start_url,
        output_dir=output_dir,
        states=states,
        transitions=transitions,
        viewport=viewport,
        console_errors=console_errors,
        page_errors=page_errors,
        timeout_ms=timeout_ms,
        candidates=candidates,
        states_by_id=states_by_id,
        signatures=signatures,
        default_origin="llm_goal",
    )
    _attach_goal_validation_results(exploration_goals, results)
    return {
        "status": "completed",
        "attempted": attempted,
        "accepted": accepted,
        "selected_candidate_count": len(candidates),
        "results": results,
    }


async def _repair_failed_goal_candidates(
    browser,
    start_url: str,
    output_dir: Path,
    states: list[dict],
    transitions: list[dict],
    exploration_goals: dict,
    goal_validation: dict,
    viewport: dict[str, int],
    console_errors: list[dict],
    page_errors: list[dict],
    timeout_ms: int,
    model: str | None,
    max_repairs: int,
) -> dict:
    if goal_validation.get("status") != "completed":
        return {
            "status": "skipped",
            "reason": f"Goal validation was not completed: {goal_validation.get('status')}",
            "attempted": 0,
            "accepted": 0,
            "results": [],
        }

    model = model or os.environ.get("FOLIO_LLM_MODEL") or DEFAULT_LLM_MODEL
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return {
            "status": "skipped",
            "reason": "Missing OPENAI_API_KEY.",
            "model": model,
            "attempted": 0,
            "accepted": 0,
            "results": [],
        }

    failed_results = _repairable_goal_failures(goal_validation, max_repairs=max_repairs)
    if not failed_results:
        return {
            "status": "completed",
            "model": model,
            "selected_failure_count": 0,
            "attempted": 0,
            "accepted": 0,
            "results": [],
            "requests": [],
            "rejected": [],
        }

    states_by_id = {
        state.get("state_id"): state
        for state in states
        if state.get("state_id")
    }
    signatures = {
        state.get("signature"): state.get("state_id")
        for state in states
        if state.get("signature") and state.get("state_id")
    }
    workflow_lookup = _exploration_goal_workflow_lookup(exploration_goals)
    repair_candidates: list[dict] = []
    requests: list[dict] = []
    rejected: list[dict] = []

    for failed_result in failed_results:
        workflow_entry = workflow_lookup.get(failed_result.get("candidate_id"))
        if not workflow_entry:
            rejected.append(
                {
                    "candidate_id": failed_result.get("candidate_id"),
                    "reason": "Could not find the original exploration goal workflow.",
                }
            )
            continue

        goal = workflow_entry["goal"]
        workflow = workflow_entry["workflow"]
        parent_state = states_by_id.get(failed_result.get("parent_state_id"))
        failure = failed_result.get("failure") or {}
        if not parent_state:
            rejected.append(
                {
                    "candidate_id": failed_result.get("candidate_id"),
                    "reason": "Could not find the original parent state.",
                }
            )
            continue
        if not failure:
            rejected.append(
                {
                    "candidate_id": failed_result.get("candidate_id"),
                    "reason": "No failure artifact was captured for this workflow.",
                }
            )
            continue

        context = _goal_repair_context(
            start_url=start_url,
            goal=goal,
            workflow=workflow,
            failed_result=failed_result,
            parent_state=parent_state,
            failure=failure,
        )
        screenshot_path = (failure.get("artifacts") or {}).get("screenshot")
        try:
            payload = _call_openai_goal_repair(
                context,
                api_key=api_key,
                model=model,
                screenshot_path=screenshot_path,
            )
        except Exception as exc:
            requests.append(
                {
                    "candidate_id": failed_result.get("candidate_id"),
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        candidates, candidate_rejections = _normalize_goal_repair_candidates(
            payload=payload,
            goal=goal,
            workflow=workflow,
            failed_result=failed_result,
            parent_state=parent_state,
            failure=failure,
        )
        repair_candidates.extend(candidates)
        rejected.extend(candidate_rejections)
        requests.append(
            {
                "candidate_id": failed_result.get("candidate_id"),
                "status": "completed",
                "rationale": _clean_label(payload.get("rationale") or ""),
                "abandoned_reason": _clean_label(payload.get("abandoned_reason") or ""),
                "proposed_candidate_count": len(payload.get("repaired_workflow_candidates", [])),
                "accepted_for_validation": len(candidates),
            }
        )

    if not repair_candidates:
        return {
            "status": "completed",
            "model": model,
            "selected_failure_count": len(failed_results),
            "attempted": 0,
            "accepted": 0,
            "results": [],
            "requests": requests,
            "rejected": rejected,
        }

    _append_goal_repair_workflows(exploration_goals, repair_candidates)
    accepted, attempted, results = await _validate_workflow_candidates(
        browser=browser,
        start_url=start_url,
        output_dir=output_dir,
        states=states,
        transitions=transitions,
        viewport=viewport,
        console_errors=console_errors,
        page_errors=page_errors,
        timeout_ms=timeout_ms,
        candidates=repair_candidates,
        states_by_id=states_by_id,
        signatures=signatures,
        default_origin="llm_goal_repair",
    )
    _attach_goal_validation_results(exploration_goals, results)
    return {
        "status": "completed",
        "model": model,
        "selected_failure_count": len(failed_results),
        "attempted": attempted,
        "accepted": accepted,
        "results": results,
        "requests": requests,
        "rejected": rejected,
    }


def _repairable_goal_failures(goal_validation: dict, max_repairs: int) -> list[dict]:
    failures = []
    for result in goal_validation.get("results", []):
        if len(failures) >= max(0, max_repairs):
            break
        if result.get("status") not in {"action_failed", "replay_failed"}:
            continue
        if not result.get("failure"):
            continue
        failures.append(result)
    return failures


def _exploration_goal_workflow_lookup(exploration_goals: dict) -> dict[str, dict]:
    lookup = {}
    for goal in exploration_goals.get("goals", []):
        for workflow in goal.get("workflow_candidates", []):
            candidate_id = workflow.get("candidate_id")
            if candidate_id:
                lookup[candidate_id] = {"goal": goal, "workflow": workflow}
    return lookup


def _goal_repair_context(
    start_url: str,
    goal: dict,
    workflow: dict,
    failed_result: dict,
    parent_state: dict,
    failure: dict,
) -> dict:
    return {
        "start_url": start_url,
        "task": "Repair a single failed workflow so it can be replayed from the original parent state and validated by Playwright.",
        "supported_actions": LLM_ACTION_TYPES,
        "rules": [
            "Use only selectors from parent_state or failure.visible_context.",
            "Return only actions that should run after parent_state.replay_actions; do not repeat those parent replay actions.",
            "Include prerequisite clicks or double_clicks only when they happen after the parent state and reveal later controls.",
            "Do not repeat a selector that failed because it was hidden or detached when a visible replacement selector exists.",
            "Prefer visible form fields and result-producing controls over navigation, site search, footer links, account flows, destructive actions, or ads.",
            "If the workflow cannot be repaired safely with the supplied selectors, return no repaired workflow candidates and explain why.",
        ],
        "goal": {
            "goal_id": goal.get("goal_id"),
            "title": goal.get("title"),
            "feature": goal.get("feature"),
            "priority": goal.get("priority"),
            "hypothesis": goal.get("hypothesis"),
            "expected_outcome": goal.get("expected_outcome"),
            "evidence": goal.get("evidence", []),
        },
        "original_workflow": {
            "candidate_id": workflow.get("candidate_id"),
            "parent_state_id": workflow.get("parent_state_id"),
            "label": workflow.get("label"),
            "confidence": workflow.get("confidence"),
            "actions": workflow.get("actions", []),
        },
        "failed_validation": {
            "status": failed_result.get("status"),
            "error": failed_result.get("error"),
            "failed_action_index": failure.get("failed_action_index"),
            "failed_action": failure.get("failed_action"),
            "completed_actions": failure.get("completed_actions", []),
            "failure_url": failure.get("url"),
            "failure_title": failure.get("title"),
        },
        "parent_state": _state_for_expander(parent_state),
        "failure": {
            "artifacts": failure.get("artifacts", {}),
            "visible_context": failure.get("visible_context", {}),
        },
    }


def _normalize_goal_repair_candidates(
    payload: dict,
    goal: dict,
    workflow: dict,
    failed_result: dict,
    parent_state: dict,
    failure: dict,
) -> tuple[list[dict], list[dict]]:
    candidates = []
    rejected = []
    extra_selector_maps = [
        _selectors_for_visible_context(failure.get("visible_context", {}))
    ]
    original_candidate_id = failed_result.get("candidate_id") or workflow.get("candidate_id") or "goal"

    for index, raw_candidate in enumerate(payload.get("repaired_workflow_candidates", [])[:MAX_LLM_GOAL_REPAIR_CANDIDATES], 1):
        requested_parent_state_id = raw_candidate.get("parent_state_id")
        if requested_parent_state_id and requested_parent_state_id != parent_state.get("state_id"):
            rejected.append(
                {
                    "candidate_id": original_candidate_id,
                    "label": raw_candidate.get("label"),
                    "reason": f"Repair changed parent_state_id from {parent_state.get('state_id')} to {requested_parent_state_id}.",
                }
            )
            continue

        actions, errors = _normalize_llm_workflow_actions(
            raw_candidate,
            parent_state,
            additional_selector_maps=extra_selector_maps,
        )
        if errors:
            rejected.append(
                {
                    "candidate_id": original_candidate_id,
                    "label": raw_candidate.get("label"),
                    "reason": "; ".join(errors),
                }
            )
            continue
        if not _is_meaningful_workflow(actions, raw_candidate):
            rejected.append(
                {
                    "candidate_id": original_candidate_id,
                    "label": raw_candidate.get("label"),
                    "reason": "Repaired workflow must include a form submission or safe in-app click workflow.",
                }
            )
            continue

        label = _clean_label(raw_candidate.get("label") or f"Repaired workflow {index}")
        confidence = _enum_value(raw_candidate.get("confidence"), {"high", "medium", "low"}, "medium")
        repair_strategy = _clean_label(raw_candidate.get("repair_strategy") or payload.get("rationale") or "")
        candidates.append(
            {
                "candidate_id": f"repair:{original_candidate_id}:{index}:{_slug(label)}",
                "parent_state_id": parent_state.get("state_id"),
                "requested_parent_state_id": workflow.get("requested_parent_state_id") or workflow.get("parent_state_id"),
                "kind": "llm_goal_workflow",
                "label": label,
                "goal": goal.get("hypothesis") or goal.get("title"),
                "expected_outcome": goal.get("expected_outcome"),
                "exploration_goal_id": goal.get("goal_id"),
                "feature": goal.get("feature"),
                "confidence": confidence,
                "repaired_from": original_candidate_id,
                "repair_note": repair_strategy,
                "origin": "llm_goal_repair",
                "priority": 5,
                "bounds": {},
                "actions": actions,
            }
        )

    return candidates, rejected


def _append_goal_repair_workflows(exploration_goals: dict, repair_candidates: list[dict]) -> None:
    goals_by_id = {
        goal.get("goal_id"): goal
        for goal in exploration_goals.get("goals", [])
        if goal.get("goal_id")
    }
    for candidate in repair_candidates:
        goal = goals_by_id.get(candidate.get("exploration_goal_id"))
        if not goal:
            continue
        goal.setdefault("workflow_candidates", []).append(
            {
                "candidate_id": candidate.get("candidate_id"),
                "parent_state_id": candidate.get("parent_state_id"),
                "requested_parent_state_id": candidate.get("requested_parent_state_id"),
                "label": candidate.get("label"),
                "confidence": candidate.get("confidence"),
                "requires_validation": True,
                "repair_note": candidate.get("repair_note"),
                "repaired_from": candidate.get("repaired_from"),
                "actions": candidate.get("actions", []),
            }
        )
    exploration_goals["workflow_candidate_count"] = sum(
        len(goal.get("workflow_candidates", []))
        for goal in exploration_goals.get("goals", [])
    )


async def _audit_coverage_with_llm(
    browser,
    start_url: str,
    output_dir: Path,
    states: list[dict],
    transitions: list[dict],
    candidate_paths: list[dict],
    exploration_goals: dict,
    goal_validation: dict,
    viewport: dict[str, int],
    console_errors: list[dict],
    page_errors: list[dict],
    timeout_ms: int,
    source_context: dict | None,
    model: str | None,
    max_workflows: int,
) -> dict:
    model = model or os.environ.get("FOLIO_LLM_MODEL") or DEFAULT_LLM_MODEL
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return {
            "status": "skipped",
            "reason": "Missing OPENAI_API_KEY.",
            "model": model,
            "attempted": 0,
            "accepted": 0,
            "results": [],
        }

    try:
        payload = _call_openai_coverage_audit(
            _coverage_audit_context(
                start_url=start_url,
                states=states,
                candidate_paths=candidate_paths,
                exploration_goals=exploration_goals,
                goal_validation=goal_validation,
                source_context=source_context,
                max_workflows=max_workflows,
            ),
            api_key=api_key,
            model=model,
            max_workflows=max_workflows,
        )
    except Exception as exc:
        return {
            "status": "failed",
            "model": model,
            "error": f"{type(exc).__name__}: {exc}",
            "attempted": 0,
            "accepted": 0,
            "results": [],
        }

    states_by_id = {
        state.get("state_id"): state
        for state in states
        if state.get("state_id")
    }
    signatures = {
        state.get("signature"): state.get("state_id")
        for state in states
        if state.get("signature") and state.get("state_id")
    }
    normalized = _normalize_coverage_audit_payload(
        payload=payload,
        states_by_id=states_by_id,
        candidate_paths=candidate_paths,
        max_workflows=max_workflows,
    )
    candidates = normalized.pop("workflow_candidates", [])
    if not candidates:
        normalized.update(
            {
                "status": "completed",
                "model": model,
                "attempted": 0,
                "accepted": 0,
                "selected_candidate_count": 0,
                "results": [],
            }
        )
        return normalized

    accepted, attempted, results = await _validate_workflow_candidates(
        browser=browser,
        start_url=start_url,
        output_dir=output_dir,
        states=states,
        transitions=transitions,
        viewport=viewport,
        console_errors=console_errors,
        page_errors=page_errors,
        timeout_ms=timeout_ms,
        candidates=candidates,
        states_by_id=states_by_id,
        signatures=signatures,
        default_origin="llm_coverage_audit",
    )
    normalized.update(
        {
            "status": "completed",
            "model": model,
            "attempted": attempted,
            "accepted": accepted,
            "selected_candidate_count": len(candidates),
            "results": results,
        }
    )
    _resolve_audited_missing_features(normalized, results)
    return normalized


async def _repair_unclear_outcome_workflows(
    browser,
    start_url: str,
    output_dir: Path,
    states: list[dict],
    transitions: list[dict],
    candidate_paths: list[dict],
    viewport: dict[str, int],
    console_errors: list[dict],
    page_errors: list[dict],
    timeout_ms: int,
    model: str | None,
    max_repairs: int,
) -> dict:
    model = model or os.environ.get("FOLIO_LLM_MODEL") or DEFAULT_LLM_MODEL
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return {
            "status": "skipped",
            "reason": "Missing OPENAI_API_KEY.",
            "model": model,
            "attempted": 0,
            "accepted": 0,
            "results": [],
        }

    states_by_id = {
        state.get("state_id"): state
        for state in states
        if state.get("state_id")
    }
    failures = _repairable_outcome_failures(
        candidate_paths=candidate_paths,
        transitions=transitions,
        max_repairs=max_repairs,
    )
    if not failures:
        return {
            "status": "completed",
            "model": model,
            "selected_failure_count": 0,
            "attempted": 0,
            "accepted": 0,
            "results": [],
            "requests": [],
            "rejected": [],
        }

    repair_candidates: list[dict] = []
    requests: list[dict] = []
    rejected: list[dict] = []

    for failure in failures:
        transition = failure["transition"]
        parent_state = states_by_id.get(transition.get("from"))
        if not parent_state:
            rejected.append(
                {
                    "candidate_id": transition.get("candidate_id"),
                    "path_id": failure.get("path_id"),
                    "reason": "Could not find the transition parent state.",
                }
            )
            continue

        screenshot_path = _outcome_failure_screenshot_path(
            output_dir=output_dir,
            failure=failure,
            parent_state=parent_state,
            states_by_id=states_by_id,
        )
        context = _outcome_repair_context(
            start_url=start_url,
            failure=failure,
            parent_state=parent_state,
        )
        try:
            payload = _call_openai_outcome_repair(
                context,
                api_key=api_key,
                model=model,
                screenshot_path=screenshot_path,
            )
        except Exception as exc:
            requests.append(
                {
                    "candidate_id": transition.get("candidate_id"),
                    "path_id": failure.get("path_id"),
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        candidates, candidate_rejections = _normalize_outcome_repair_candidates(
            payload=payload,
            failure=failure,
            parent_state=parent_state,
        )
        repair_candidates.extend(candidates)
        rejected.extend(candidate_rejections)
        requests.append(
            {
                "candidate_id": transition.get("candidate_id"),
                "path_id": failure.get("path_id"),
                "status": "completed",
                "rationale": _clean_label(payload.get("rationale") or ""),
                "abandoned_reason": _clean_label(payload.get("abandoned_reason") or ""),
                "proposed_candidate_count": len(payload.get("repaired_workflow_candidates", [])),
                "accepted_for_validation": len(candidates),
            }
        )

    if not repair_candidates:
        return {
            "status": "completed",
            "model": model,
            "selected_failure_count": len(failures),
            "attempted": 0,
            "accepted": 0,
            "results": [],
            "requests": requests,
            "rejected": rejected,
        }

    signatures = {
        state.get("signature"): state.get("state_id")
        for state in states
        if state.get("signature") and state.get("state_id")
    }
    accepted, attempted, results = await _validate_workflow_candidates(
        browser=browser,
        start_url=start_url,
        output_dir=output_dir,
        states=states,
        transitions=transitions,
        viewport=viewport,
        console_errors=console_errors,
        page_errors=page_errors,
        timeout_ms=timeout_ms,
        candidates=repair_candidates,
        states_by_id=states_by_id,
        signatures=signatures,
        default_origin="llm_outcome_repair",
    )
    return {
        "status": "completed",
        "model": model,
        "selected_failure_count": len(failures),
        "attempted": attempted,
        "accepted": accepted,
        "results": results,
        "requests": requests,
        "rejected": rejected,
    }


def _repairable_outcome_failures(
    candidate_paths: list[dict],
    transitions: list[dict],
    max_repairs: int,
) -> list[dict]:
    failures = []
    seen = set()

    for path in candidate_paths:
        if len(failures) >= max(0, max_repairs):
            break
        if not _path_is_repairable_product_workflow(path):
            continue
        if _path_has_presentable_scan_outcome(path):
            continue
        transition = _repair_transition_for_path(path)
        if not transition:
            continue
        key = transition.get("candidate_id") or path.get("path_id")
        if key in seen:
            continue
        seen.add(key)
        failures.append(
            {
                "path_id": path.get("path_id"),
                "reason": "Workflow executed but did not expose a clear changed result for the demo.",
                "path": path,
                "transition": transition,
            }
        )

    for transition in transitions:
        if len(failures) >= max(0, max_repairs):
            break
        if transition.get("status") != "no_meaningful_outcome":
            continue
        key = transition.get("candidate_id")
        if not key or key in seen:
            continue
        seen.add(key)
        failures.append(
            {
                "path_id": None,
                "reason": "Workflow was rejected because it only changed filled inputs or did not expose a result.",
                "path": None,
                "transition": _path_transition(transition),
            }
        )

    return failures


def _path_is_repairable_product_workflow(path: dict) -> bool:
    kinds = set(path.get("kinds", []))
    if {"llm_workflow", "llm_goal_workflow"} & kinds:
        return True
    action_types = set(path.get("action_types", []))
    return bool({"fill", "select"} & action_types) and bool({"click", "double_click", "press"} & action_types)


def _repair_transition_for_path(path: dict) -> dict | None:
    for transition in reversed(path.get("transitions", [])):
        if _transition_is_product_workflow(transition):
            return transition
    return None


def _transition_is_product_workflow(transition: dict) -> bool:
    if transition.get("kind") in {"input_submit", "llm_workflow", "llm_goal_workflow"}:
        return True
    action_types = {action.get("type") for action in transition.get("actions", [])}
    return bool({"fill", "select"} & action_types) and bool({"click", "double_click", "press"} & action_types)


def _path_has_presentable_scan_outcome(path: dict) -> bool:
    return any(
        _transition_has_presentable_scan_outcome(transition)
        for transition in path.get("transitions", [])
        if _transition_is_product_workflow(transition)
    )


def _transition_has_presentable_scan_outcome(transition: dict) -> bool:
    summary = transition.get("outcome_summary") or {}
    if summary.get("url_changed") or summary.get("added_text") or summary.get("removed_text") or summary.get("added_controls"):
        return True

    action_selectors = {
        action.get("selector")
        for action in transition.get("actions", [])
        if action.get("type") in {"fill", "select"} and action.get("selector")
    }
    for control in summary.get("changed_controls") or []:
        selectors = set(control.get("selectors") or [])
        if selectors and selectors & action_selectors:
            continue
        for change in (control.get("changes") or {}).values():
            before = str(change.get("before") or "").strip()
            after = str(change.get("after") or "").strip()
            if after and after != before:
                return True
    return False


def _outcome_failure_screenshot_path(
    output_dir: Path,
    failure: dict,
    parent_state: dict,
    states_by_id: dict[str, dict],
) -> str | None:
    transition = failure.get("transition") or {}
    to_state_id = transition.get("to")
    to_state = states_by_id.get(to_state_id)
    screenshot = ((to_state or {}).get("artifacts") or {}).get("screenshot")
    if screenshot:
        return screenshot

    if to_state_id:
        candidate_path = output_dir / f"{to_state_id}.png"
        if candidate_path.exists():
            return str(candidate_path)

    return (parent_state.get("artifacts") or {}).get("screenshot")


def _outcome_repair_context(start_url: str, failure: dict, parent_state: dict) -> dict:
    transition = failure.get("transition") or {}
    return {
        "start_url": start_url,
        "goal": "Repair a mechanically successful workflow so it produces a clear, visible changed output for a demo video.",
        "failure_reason": failure.get("reason"),
        "path": _candidate_path_for_audit(failure.get("path") or {}) if failure.get("path") else None,
        "failed_transition": {
            "candidate_id": transition.get("candidate_id"),
            "label": transition.get("label"),
            "kind": transition.get("kind"),
            "goal": transition.get("goal"),
            "expected_outcome": transition.get("expected_outcome"),
            "feature": transition.get("feature"),
            "feature_id": transition.get("feature_id"),
            "actions": transition.get("actions", []),
            "outcome_summary": _outcome_summary_for_audit(transition.get("outcome_summary")),
        },
        "parent_state": _state_for_audit(parent_state),
        "requirements": [
            "Return actions that replay from parent_state.state_id.",
            "The repaired workflow must include a decisive submit/calculate/apply action when the app requires one.",
            "The result must visibly change an output, result, preview, saved item, filtered list, status, or summary.",
            "Do not return a workflow that only fills user inputs or focuses controls.",
            "Avoid account, payment, destructive, feedback, and external navigation actions.",
        ],
        "supported_actions": LLM_ACTION_TYPES,
    }


def _normalize_outcome_repair_candidates(
    payload: dict,
    failure: dict,
    parent_state: dict,
) -> tuple[list[dict], list[dict]]:
    candidates = []
    rejected = []
    transition = failure.get("transition") or {}
    original_candidate_id = transition.get("candidate_id") or failure.get("path_id") or "outcome"

    for index, raw_candidate in enumerate(payload.get("repaired_workflow_candidates", [])[:MAX_LLM_OUTCOME_REPAIR_CANDIDATES], 1):
        requested_parent_state_id = raw_candidate.get("parent_state_id")
        if requested_parent_state_id and requested_parent_state_id != parent_state.get("state_id"):
            rejected.append(
                {
                    "candidate_id": original_candidate_id,
                    "label": raw_candidate.get("label"),
                    "reason": f"Repair changed parent_state_id from {parent_state.get('state_id')} to {requested_parent_state_id}.",
                }
            )
            continue

        actions, errors = _normalize_llm_workflow_actions(raw_candidate, parent_state)
        if errors:
            rejected.append(
                {
                    "candidate_id": original_candidate_id,
                    "label": raw_candidate.get("label"),
                    "reason": "; ".join(errors),
                }
            )
            continue
        if not _is_meaningful_workflow(actions, raw_candidate):
            rejected.append(
                {
                    "candidate_id": original_candidate_id,
                    "label": raw_candidate.get("label"),
                    "reason": "Outcome repair must include a form submission or safe in-app click workflow.",
                }
            )
            continue

        label = _clean_label(raw_candidate.get("label") or f"Outcome repair {index}")
        repair_strategy = _clean_label(raw_candidate.get("repair_strategy") or payload.get("rationale") or "")
        candidates.append(
            {
                "candidate_id": f"outcome-repair:{original_candidate_id}:{index}:{_slug(label)}",
                "parent_state_id": parent_state.get("state_id"),
                "requested_parent_state_id": requested_parent_state_id or parent_state.get("state_id"),
                "kind": "llm_goal_workflow",
                "label": label,
                "goal": _clean_label(raw_candidate.get("goal") or transition.get("goal") or label),
                "expected_outcome": _clean_label(raw_candidate.get("expected_outcome") or transition.get("expected_outcome") or ""),
                "feature_id": _slug(raw_candidate.get("feature_id") or transition.get("feature_id") or transition.get("feature") or label),
                "feature": _clean_label(raw_candidate.get("feature") or transition.get("feature") or ""),
                "confidence": _enum_value(raw_candidate.get("confidence"), {"high", "medium", "low"}, "medium"),
                "repaired_from": original_candidate_id,
                "repair_note": repair_strategy,
                "origin": "llm_outcome_repair",
                "priority": 5,
                "bounds": {},
                "actions": actions,
            }
        )

    return candidates, rejected


def _normalize_coverage_audit_payload(
    payload: dict,
    states_by_id: dict[str, dict],
    candidate_paths: list[dict],
    max_workflows: int,
) -> dict:
    known_path_ids = {
        path.get("path_id")
        for path in candidate_paths
        if path.get("path_id")
    }
    covered_features = [
        {
            "feature_id": _slug(feature.get("feature_id") or feature.get("title") or "covered-feature"),
            "title": _clean_label(feature.get("title") or "Covered feature"),
            "confidence": _enum_value(feature.get("confidence"), {"high", "medium", "low"}, "medium"),
            "evidence": _clean_string_list(feature.get("evidence"), limit=6),
            "related_path_ids": [
                path_id
                for path_id in _clean_string_list(feature.get("related_path_ids"), limit=8)
                if path_id in known_path_ids
            ],
        }
        for feature in payload.get("covered_features", [])[:MAX_LLM_COVERAGE_AUDIT_FEATURES]
    ]
    missing_features = [
        {
            "feature_id": _slug(feature.get("feature_id") or feature.get("title") or "missing-feature"),
            "title": _clean_label(feature.get("title") or "Missing feature"),
            "priority": _enum_value(feature.get("priority"), {"high", "medium", "low"}, "medium"),
            "reason": _clean_label(feature.get("reason") or ""),
            "blockers": _clean_string_list(feature.get("blockers"), limit=6),
            "evidence": _clean_string_list(feature.get("evidence"), limit=6),
        }
        for feature in payload.get("missing_features", [])[:MAX_LLM_COVERAGE_AUDIT_FEATURES]
    ]
    low_value_paths = [
        {
            "path_id": path.get("path_id"),
            "reason": _clean_label(path.get("reason") or ""),
        }
        for path in payload.get("low_value_paths", [])[:MAX_CANDIDATE_PATHS]
        if path.get("path_id") in known_path_ids
    ]
    candidates, rejected = _normalize_coverage_audit_workflow_candidates(
        payload=payload,
        states_by_id=states_by_id,
        max_workflows=max_workflows,
    )
    return {
        "rationale": _clean_label(payload.get("rationale") or ""),
        "app_coverage_summary": _clean_label(payload.get("app_coverage_summary") or ""),
        "covered_features": covered_features,
        "missing_features": missing_features,
        "low_value_paths": low_value_paths,
        "workflow_candidates": candidates,
        "rejected_workflow_candidates": rejected,
    }


def _normalize_coverage_audit_workflow_candidates(
    payload: dict,
    states_by_id: dict[str, dict],
    max_workflows: int,
) -> tuple[list[dict], list[dict]]:
    candidates = []
    rejected = []
    for index, raw_candidate in enumerate(payload.get("workflow_candidates", [])[:max(0, max_workflows)], 1):
        requested_parent_state_id = raw_candidate.get("parent_state_id")
        parent_state = states_by_id.get(requested_parent_state_id)
        repair_note = None
        drop_unavailable = False
        if not parent_state:
            parent_state, repair_note = _best_state_for_llm_workflow(raw_candidate, states_by_id)
            drop_unavailable = bool(parent_state)
        elif _workflow_has_unavailable_selectors(raw_candidate, parent_state):
            repaired_state, repair_note = _best_state_for_llm_workflow(raw_candidate, states_by_id)
            if repaired_state and repaired_state.get("state_id") != parent_state.get("state_id"):
                parent_state = repaired_state
                drop_unavailable = True

        if not parent_state:
            rejected.append(
                {
                    "label": raw_candidate.get("label"),
                    "feature_id": raw_candidate.get("feature_id"),
                    "reason": "Unknown parent_state_id and no alternate state had enough selector coverage.",
                }
            )
            continue

        actions, errors = _normalize_llm_workflow_actions(
            raw_candidate,
            parent_state,
            drop_unavailable=drop_unavailable,
        )
        if errors:
            rejected.append(
                {
                    "label": raw_candidate.get("label"),
                    "feature_id": raw_candidate.get("feature_id"),
                    "parent_state_id": requested_parent_state_id,
                    "reason": "; ".join(errors),
                }
            )
            continue
        if not _is_meaningful_workflow(actions, raw_candidate):
            rejected.append(
                {
                    "label": raw_candidate.get("label"),
                    "feature_id": raw_candidate.get("feature_id"),
                    "parent_state_id": requested_parent_state_id,
                    "reason": "Workflow candidate must include a form submission or safe in-app click workflow.",
                }
            )
            continue

        feature_id = _slug(raw_candidate.get("feature_id") or raw_candidate.get("feature") or raw_candidate.get("label") or "coverage")
        label = _clean_label(raw_candidate.get("label") or f"Coverage audit workflow {index}")
        parent_state_id = parent_state.get("state_id")
        candidates.append(
            {
                "candidate_id": f"coverage-audit:{feature_id}:{parent_state_id}:{_slug(label)}",
                "parent_state_id": parent_state_id,
                "requested_parent_state_id": requested_parent_state_id,
                "kind": "llm_goal_workflow",
                "label": label,
                "goal": _clean_label(raw_candidate.get("goal") or raw_candidate.get("expected_outcome") or label),
                "expected_outcome": _clean_label(raw_candidate.get("expected_outcome") or ""),
                "feature_id": feature_id,
                "feature": _clean_label(raw_candidate.get("feature") or raw_candidate.get("feature_id") or feature_id),
                "confidence": _enum_value(raw_candidate.get("confidence"), {"high", "medium", "low"}, "medium"),
                "repair_note": repair_note or "Proposed by coverage audit for missing or under-covered functionality.",
                "origin": "llm_coverage_audit",
                "priority": 5,
                "bounds": {},
                "actions": actions,
            }
        )

    return candidates, rejected


def _resolve_audited_missing_features(audit: dict, results: list[dict]) -> None:
    accepted_by_feature_id = {
        result.get("feature_id"): result
        for result in results
        if result.get("status") == "success" and result.get("feature_id")
    }
    if not accepted_by_feature_id:
        return

    remaining = []
    resolved = audit.setdefault("resolved_missing_features", [])
    for feature in audit.get("missing_features", []):
        result = accepted_by_feature_id.get(feature.get("feature_id"))
        if not result:
            remaining.append(feature)
            continue
        resolved.append(
            {
                **feature,
                "status": "validated_by_audit",
                "candidate_id": result.get("candidate_id"),
                "state_id": result.get("to"),
            }
        )

    audit["missing_features"] = remaining


def _goal_validation_candidates(exploration_goals: dict, max_validations: int) -> list[dict]:
    priority_order = {"high": 0, "medium": 1, "low": 2}
    goals = sorted(
        exploration_goals.get("goals", []),
        key=lambda goal: (
            priority_order.get(goal.get("priority"), 1),
            goal.get("goal_id") or "",
        ),
    )
    candidates = []
    for goal in goals:
        if goal.get("status") != "needs_validation":
            continue
        for workflow in goal.get("workflow_candidates", []):
            if len(candidates) >= max(0, max_validations):
                return candidates
            candidates.append(
                {
                    "candidate_id": workflow.get("candidate_id"),
                    "parent_state_id": workflow.get("parent_state_id"),
                    "requested_parent_state_id": workflow.get("requested_parent_state_id"),
                    "kind": "llm_goal_workflow",
                    "label": workflow.get("label") or goal.get("title") or "Exploration goal workflow",
                    "goal": goal.get("hypothesis") or goal.get("title"),
                    "expected_outcome": goal.get("expected_outcome"),
                    "exploration_goal_id": goal.get("goal_id"),
                    "feature": goal.get("feature"),
                    "confidence": workflow.get("confidence"),
                    "repair_note": workflow.get("repair_note"),
                    "origin": "llm_goal",
                    "priority": 4,
                    "bounds": {},
                    "actions": workflow.get("actions", []),
                }
            )
    return candidates


def _attach_goal_validation_results(exploration_goals: dict, results: list[dict]) -> None:
    results_by_id = {
        result.get("candidate_id"): result
        for result in results
        if result.get("candidate_id")
    }
    for goal in exploration_goals.get("goals", []):
        goal_results = []
        for workflow in goal.get("workflow_candidates", []):
            result = results_by_id.get(workflow.get("candidate_id"))
            if not result:
                continue
            workflow["validation"] = {
                "status": result.get("status"),
                "state_id": result.get("to"),
                "duplicate_of": result.get("duplicate_of"),
                "error": result.get("error"),
                "failure": result.get("failure"),
            }
            goal_results.append(result)

        if goal_results:
            if any(result.get("status") == "success" for result in goal_results):
                goal["status"] = "validated"
            elif all(result.get("status") in {"action_failed", "replay_failed", "duplicate"} for result in goal_results):
                goal["status"] = "needs_validation"


def _call_openai_exploration_goals(context: dict, api_key: str, model: str, max_goals: int) -> dict:
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": model,
        "input": [
            {
                "role": "developer",
                "content": _llm_exploration_goal_prompt(),
            },
            {
                "role": "user",
                "content": json.dumps(context, indent=2),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "folio_exploration_goals",
                "strict": True,
                "schema": _llm_exploration_goal_schema(max_goals),
            }
        },
        "max_output_tokens": 10_000,
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
        with urllib.request.urlopen(request, timeout=180, context=_https_context()) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API request failed with HTTP {exc.code}: {detail}") from exc

    output_text = _extract_openai_output_text(body)
    if not output_text:
        raise RuntimeError("OpenAI response did not include output text.")

    return json.loads(output_text)


def _call_openai_goal_repair(
    context: dict,
    api_key: str,
    model: str,
    screenshot_path: str | None,
) -> dict:
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    user_content = [
        {
            "type": "input_text",
            "text": json.dumps(context, indent=2),
        }
    ]
    image_url = _image_data_url(screenshot_path)
    if image_url:
        user_content.append(
            {
                "type": "input_image",
                "image_url": image_url,
                "detail": "low",
            }
        )

    payload = {
        "model": model,
        "input": [
            {
                "role": "developer",
                "content": _llm_goal_repair_prompt(),
            },
            {
                "role": "user",
                "content": user_content,
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "folio_goal_repair",
                "strict": True,
                "schema": _llm_goal_repair_schema(),
            }
        },
        "max_output_tokens": 4_000,
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
        with urllib.request.urlopen(request, timeout=180, context=_https_context()) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API request failed with HTTP {exc.code}: {detail}") from exc

    output_text = _extract_openai_output_text(body)
    if not output_text:
        raise RuntimeError("OpenAI response did not include output text.")

    return json.loads(output_text)


def _call_openai_outcome_repair(
    context: dict,
    api_key: str,
    model: str,
    screenshot_path: str | None,
) -> dict:
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    user_content = [
        {
            "type": "input_text",
            "text": json.dumps(context, indent=2),
        }
    ]
    image_url = _image_data_url(screenshot_path)
    if image_url:
        user_content.append(
            {
                "type": "input_image",
                "image_url": image_url,
                "detail": "low",
            }
        )

    payload = {
        "model": model,
        "input": [
            {
                "role": "developer",
                "content": _llm_outcome_repair_prompt(),
            },
            {
                "role": "user",
                "content": user_content,
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "folio_outcome_repair",
                "strict": True,
                "schema": _llm_outcome_repair_schema(),
            }
        },
        "max_output_tokens": 4_000,
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
        with urllib.request.urlopen(request, timeout=180, context=_https_context()) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API request failed with HTTP {exc.code}: {detail}") from exc

    output_text = _extract_openai_output_text(body)
    if not output_text:
        raise RuntimeError("OpenAI response did not include output text.")

    return json.loads(output_text)


def _call_openai_coverage_audit(context: dict, api_key: str, model: str, max_workflows: int) -> dict:
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": model,
        "input": [
            {
                "role": "developer",
                "content": _llm_coverage_audit_prompt(),
            },
            {
                "role": "user",
                "content": json.dumps(context, indent=2),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "folio_coverage_audit",
                "strict": True,
                "schema": _llm_coverage_audit_schema(max_workflows),
            }
        },
        "max_output_tokens": 8_000,
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
        with urllib.request.urlopen(request, timeout=180, context=_https_context()) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API request failed with HTTP {exc.code}: {detail}") from exc

    output_text = _extract_openai_output_text(body)
    if not output_text:
        raise RuntimeError("OpenAI response did not include output text.")

    return json.loads(output_text)


def _image_data_url(path: str | None) -> str | None:
    if not path:
        return None
    image_path = Path(path)
    if not image_path.exists() or image_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        return None

    mime_type = "image/png"
    if image_path.suffix.lower() in {".jpg", ".jpeg"}:
        mime_type = "image/jpeg"
    elif image_path.suffix.lower() == ".webp":
        mime_type = "image/webp"

    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


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


def _llm_workflow_expander_prompt() -> str:
    return (
        "You are Folio's workflow expansion strategist. Propose a small number of safe, bounded browser workflows "
        "that demonstrate core app functionality missing from the current scanner-tested candidate paths. Use only "
        "state_id values and selectors provided in the context. Prefer main content workflows that create, calculate, "
        "filter, toggle, save, search, preview, clear local completed items, delete local in-app items, or otherwise "
        "produce visible product outcomes. Use double_click for safe in-app edit/reveal gestures when the UI or text "
        "explicitly indicates double-click editing. A fill/press selector may appear only after an earlier click or "
        "double_click if the workflow is exposing an edit field or similar local control. Use select actions for select/dropdown "
        "elements. Avoid feedback forms, generic "
        "site search, legal/account/navigation/footer/header actions unless those are the actual product. Return only "
        "JSON matching the schema. If a workflow starts from a discovered tool page, set parent_state_id to that "
        "tool page's state and include only actions that can run from that state; do not include navigation actions "
        "from the start page. Actions run after Folio replays the parent state, so do not repeat parent_state replay actions. "
        "Prefer simple text/number fields plus a Calculate, Apply, Search, Save, or Submit button "
        "over complex selects when both are available. Folio will validate every proposed action in Playwright before using it."
    )


def _llm_exploration_goal_prompt() -> str:
    return (
        "You are Folio's product exploration strategist. Your job is to understand the application as a product, "
        "map its meaningful feature areas, and propose exploration goals that would maximize functional coverage. "
        "Use the provided states, candidate paths, source hints, routes, text, forms, and selectors. Distinguish "
        "validated workflows from missing or partially discovered functionality. Prefer core product workflows over "
        "generic search, feedback, legal, account, header, footer, and site navigation unless those are the product. "
        "Safe local mutations such as completing, filtering, clearing completed local items, deleting an in-app item, "
        "or resetting a calculator/form can be valid core workflows when they produce visible results. "
        "Use double_click for safe in-app editing gestures when page text or DOM evidence indicates double-click editing; "
        "selectors for fields revealed by that double_click may be included as later fill/press actions and will be validated in Playwright. "
        "If you propose browser actions, use only selectors present in the provided state context and set the correct "
        "parent_state_id. Actions run after Folio has replayed that parent state's replay_actions, so do not repeat them. "
        "If a feature appears important but no usable selectors or state are available, return it as "
        "needs_discovery with blockers instead of inventing selectors. Return only JSON matching the schema. Folio "
        "will treat every workflow candidate as untrusted until Playwright validates it. Keep each description, "
        "hypothesis, evidence item, blocker, and outcome concise. For already validated features, include workflow "
        "candidates only for meaningfully distinct variations that are not already covered by candidate_paths."
    )


def _llm_goal_repair_prompt() -> str:
    return (
        "You are Folio's workflow repair strategist. A Playwright-validated product workflow failed after partial "
        "execution. Use the original parent state, failure DOM context, exact error, completed actions, failed action, "
        "and screenshot to repair the workflow. The returned actions must replay from the original parent_state_id, "
        "not from the failure state; do not repeat parent_state.replay_actions because Folio replays them before your actions. "
        "Include only prerequisite actions after the parent state that reveal controls used later. Use only "
        "selectors provided in parent_state or failure.visible_context. Prefer changing the smallest useful part of "
        "the workflow: replace hidden or stale controls with visible controls, switch fill/select action types when "
        "the DOM evidence shows a better control type, use double_click when the UI requires it to enter edit mode, "
        "and add an observe action only when the UI needs a short pause. "
        "Do not invent selectors, do not use account/payment/navigation actions or destructive actions outside the "
        "local product surface, and do not chase generic "
        "site chrome unless it is the product itself. If the workflow cannot be safely repaired with the supplied "
        "evidence, return an empty repaired_workflow_candidates array and a concise abandoned_reason. Return only JSON "
        "matching the schema."
    )


def _llm_outcome_repair_prompt() -> str:
    return (
        "You are Folio's outcome repair strategist. A workflow replayed without browser errors, but it failed as a "
        "demo because it only filled inputs or did not expose a clear changed product result. Repair the workflow so "
        "it produces a visible, presentable outcome: an output field, result text, generated preview, saved item, "
        "filtered list, status, or summary that changes after the decisive action. The returned actions must replay "
        "from the supplied parent_state_id, not from the after-state screenshot. Use only selectors present in the "
        "parent_state context, except for fields revealed by an earlier safe click or double_click in the same workflow. "
        "Do not repeat parent_state.replay_actions because Folio replays them before your actions. "
        "Include the submit/calculate/apply/search/save action when the app needs one. Prefer "
        "simple, reliable field values that satisfy required inputs and avoid impossible calculations or blank-output "
        "cases. Do not return workflows that merely fill fields, focus controls, scroll, open site chrome, submit "
        "feedback, use accounts/payments, or perform destructive actions outside the local product surface. Safe local "
        "mutations such as clearing completed items, deleting a local item, or resetting a calculator/form are allowed "
        "when they are the app's feature and produce a visible result. If the supplied state cannot produce a "
        "clear result safely, return an empty repaired_workflow_candidates array and explain abandoned_reason. Return "
        "only JSON matching the schema."
    )


def _llm_coverage_audit_prompt() -> str:
    return (
        "You are Folio's coverage auditor. Review the app summary, discovered states, validated candidate paths, "
        "goal validation results, and source hints. Decide what meaningful product functionality is already covered, "
        "what is still missing or under-covered, and which existing paths are low-value navigation or site chrome. "
        "Propose only safe, bounded workflows that address missing core product functionality and can replay from one "
        "of the provided state_id values using selectors present in that state. Prefer workflows that calculate, create, "
        "transform, filter, preview, export, save locally, clear completed local items, delete local in-app items, "
        "reset calculators/forms, edit existing local items via double_click when indicated, or otherwise produce visible in-app outcomes. "
        "Fields revealed by an earlier safe double_click may be used as later fill/press actions. Avoid feedback, "
        "contact, account, payment, destructive actions outside the local product surface, legal, header, footer, generic search, and external navigation unless "
        "the scanned app is specifically about that function. Do not duplicate already validated workflows unless the "
        "new workflow covers a meaningfully different mode, input type, branch, or result surface. If an important "
        "feature has usable selectors, propose a workflow for it until max_workflow_candidates is reached; do not stop "
        "after only two or three workflows when more safe product features are reachable. If an important feature cannot "
        "be safely reached with the current selectors, report it as a missing feature with blockers instead of inventing "
        "selectors. Candidate actions run after Folio has replayed the parent state's replay_actions, so do not repeat them. "
        "Every high- or medium-priority missing_features item with empty blockers must have at least one "
        "workflow_candidates item using the same feature_id. If no safe candidate can be created, keep the feature in "
        "missing_features but include concrete blockers. Folio will validate every workflow candidate in Playwright before using it. "
        "Return only JSON matching the schema."
    )


def _workflow_expansion_context(
    start_url: str,
    states: list[dict],
    candidate_paths: list[dict],
    source_context: dict | None,
) -> dict:
    return {
        "start_url": start_url,
        "source_context": _source_context_for_expander(source_context),
        "candidate_paths": [
            {
                "path_id": path.get("path_id"),
                "score": path.get("score"),
                "labels": path.get("labels", []),
                "kinds": path.get("kinds", []),
                "quality_tags": path.get("quality_tags", []),
                "final_state_id": path.get("final_state_id"),
                "final_url": path.get("final_url"),
                "transition_outcomes": [
                    {
                        "label": transition.get("label"),
                        "kind": transition.get("kind"),
                        "outcome_summary": transition.get("outcome_summary"),
                    }
                    for transition in path.get("transitions", [])
                ],
            }
            for path in candidate_paths[:MAX_CANDIDATE_PATHS]
        ],
        "states": [
            _state_for_expander(state)
            for state in states[:MAX_LLM_EXPANSION_STATES]
        ],
        "supported_actions": LLM_ACTION_TYPES,
    }


def _exploration_goal_context(
    start_url: str,
    states: list[dict],
    candidate_paths: list[dict],
    source_context: dict | None,
    max_goals: int,
) -> dict:
    return {
        "start_url": start_url,
        "goal": "Map app functionality and propose validation goals for maximum product coverage.",
        "limits": {
            "max_goals": max_goals,
            "max_workflow_candidates_per_goal": MAX_LLM_GOAL_WORKFLOWS,
            "supported_actions": LLM_ACTION_TYPES,
        },
        "source_context": _source_context_for_expander(source_context),
        "candidate_paths": [
            _candidate_path_for_audit(path)
            for path in candidate_paths[:MAX_CANDIDATE_PATHS]
        ],
        "states": [
            _state_for_goal_generation(state)
            for state in states[:MAX_LLM_GOAL_STATES]
        ],
    }


def _coverage_audit_context(
    start_url: str,
    states: list[dict],
    candidate_paths: list[dict],
    exploration_goals: dict,
    goal_validation: dict,
    source_context: dict | None,
    max_workflows: int,
) -> dict:
    return {
        "start_url": start_url,
        "goal": "Audit validated app-functionality coverage and propose only missing, high-value workflows.",
        "limits": {
            "max_workflow_candidates": max_workflows,
            "supported_actions": LLM_ACTION_TYPES,
        },
        "source_context": _source_context_for_expander(source_context),
        "exploration_goals": _exploration_goals_for_audit(exploration_goals),
        "goal_validation": _goal_validation_for_audit(goal_validation),
        "candidate_paths": [
            _candidate_path_for_audit(path)
            for path in candidate_paths[:MAX_CANDIDATE_PATHS]
        ],
        "states": [
            _state_for_audit(state)
            for state in states[:MAX_LLM_EXPANSION_STATES]
        ],
    }


def _exploration_goals_for_audit(exploration_goals: dict) -> dict:
    if exploration_goals.get("status") == "disabled":
        return {"status": "disabled"}

    return {
        "status": exploration_goals.get("status"),
        "app_summary": exploration_goals.get("app_summary"),
        "feature_areas": [
            {
                "feature_id": feature.get("feature_id"),
                "title": feature.get("title"),
                "validation_status": feature.get("validation_status"),
                "evidence": feature.get("evidence", []),
                "related_path_ids": feature.get("related_path_ids", []),
            }
            for feature in exploration_goals.get("feature_areas", [])[:MAX_LLM_COVERAGE_AUDIT_FEATURES]
        ],
        "goals": [
            {
                "goal_id": goal.get("goal_id"),
                "title": goal.get("title"),
                "feature": goal.get("feature"),
                "priority": goal.get("priority"),
                "status": goal.get("status"),
                "expected_outcome": goal.get("expected_outcome"),
                "workflow_candidates": [
                    {
                        "candidate_id": workflow.get("candidate_id"),
                        "label": workflow.get("label"),
                        "parent_state_id": workflow.get("parent_state_id"),
                        "validation": _compact_validation_for_audit(workflow.get("validation", {})),
                    }
                    for workflow in goal.get("workflow_candidates", [])[:MAX_LLM_GOAL_WORKFLOWS + MAX_LLM_GOAL_REPAIR_CANDIDATES]
                ],
                "blockers": goal.get("blockers", []),
            }
            for goal in exploration_goals.get("goals", [])[:MAX_LLM_COVERAGE_AUDIT_FEATURES]
        ],
        "deprioritized_goals": exploration_goals.get("deprioritized_goals", []),
        "rejected_workflow_candidates": exploration_goals.get("rejected_workflow_candidates", [])[:20],
    }


def _goal_validation_for_audit(goal_validation: dict) -> dict:
    if goal_validation.get("status") == "disabled":
        return {"status": "disabled"}

    return {
        "status": goal_validation.get("status"),
        "attempted": goal_validation.get("attempted", 0),
        "accepted": goal_validation.get("accepted", 0),
        "results": [
            _validation_result_for_audit(result)
            for result in goal_validation.get("results", [])[:20]
        ],
        "repairs": {
            "status": (goal_validation.get("repairs") or {}).get("status"),
            "attempted": (goal_validation.get("repairs") or {}).get("attempted", 0),
            "accepted": (goal_validation.get("repairs") or {}).get("accepted", 0),
            "results": [
                _validation_result_for_audit(result)
                for result in (goal_validation.get("repairs") or {}).get("results", [])[:12]
            ],
        },
    }


def _validation_result_for_audit(result: dict) -> dict:
    return {
        "candidate_id": result.get("candidate_id"),
        "label": result.get("label"),
        "goal": result.get("goal"),
        "feature": result.get("feature"),
        "status": result.get("status"),
        "to": result.get("to"),
        "duplicate_of": result.get("duplicate_of"),
        "error": _truncate_text(result.get("error"), limit=320),
    }


def _compact_validation_for_audit(validation: dict) -> dict:
    if not validation:
        return {}

    return {
        "status": validation.get("status"),
        "state_id": validation.get("state_id"),
        "duplicate_of": validation.get("duplicate_of"),
        "error": _truncate_text(validation.get("error"), limit=320),
    }


def _candidate_path_for_audit(path: dict) -> dict:
    return {
        "path_id": path.get("path_id"),
        "score": path.get("score"),
        "labels": path.get("labels", []),
        "kinds": path.get("kinds", []),
        "quality_tags": path.get("quality_tags", []),
        "action_types": path.get("action_types", []),
        "final_state_id": path.get("final_state_id"),
        "final_url": path.get("final_url"),
        "selection_reasons": path.get("selection_reasons", [])[:4],
        "transition_outcomes": [
            {
                "label": transition.get("label"),
                "kind": transition.get("kind"),
                "origin": transition.get("origin"),
                "goal": _truncate_text(transition.get("goal"), limit=180),
                "expected_outcome": _truncate_text(transition.get("expected_outcome"), limit=180),
                "feature": transition.get("feature"),
                "outcome_summary": _outcome_summary_for_audit(transition.get("outcome_summary")),
            }
            for transition in path.get("transitions", [])
        ],
        "final_state_summary": _state_summary_for_audit(path.get("final_state_summary", {})),
    }


def _outcome_summary_for_audit(outcome_summary: dict | None) -> dict | None:
    if not outcome_summary:
        return None

    return {
        "url_changed": outcome_summary.get("url_changed"),
        "added_text": outcome_summary.get("added_text", [])[:6],
        "removed_text": outcome_summary.get("removed_text", [])[:3],
        "added_controls": outcome_summary.get("added_controls", [])[:6],
        "removed_controls": outcome_summary.get("removed_controls", [])[:3],
        "changed_controls": [
            {
                "name": control.get("name"),
                "selectors": control.get("selectors", [])[:1],
                "changes": control.get("changes", {}),
            }
            for control in outcome_summary.get("changed_controls", [])[:6]
        ],
        "counts": outcome_summary.get("counts", {}),
    }


def _state_summary_for_audit(summary: dict) -> dict:
    return {
        "title": summary.get("title"),
        "text_blocks": summary.get("text_blocks", [])[:8],
        "headings": summary.get("headings", [])[:4],
        "interactive": [
            {
                "name": item.get("name"),
                "tag": item.get("tag"),
                "type": item.get("type"),
                "role": item.get("role"),
                "selectors": item.get("selectors", [])[:1],
                "checked": item.get("checked"),
                "disabled": item.get("disabled"),
            }
            for item in summary.get("interactive", [])[:10]
        ],
    }


def _source_context_for_expander(source_context: dict | None) -> dict | None:
    if not source_context:
        return None

    return {
        "summary": source_context.get("summary", {}),
        "framework_hints": source_context.get("framework_hints", []),
        "routes": source_context.get("routes", [])[:30],
        "components": source_context.get("components", [])[:40],
        "ui_strings": source_context.get("ui_strings", [])[:40],
    }


def _state_for_expander(state: dict) -> dict:
    dom = state.get("dom", {})
    return {
        "state_id": state.get("state_id"),
        "title": state.get("title"),
        "url": state.get("url"),
        "depth": state.get("depth"),
        "parent_state_id": state.get("parent_state_id"),
        "replay_actions": state.get("replay_actions", []),
        "text_blocks": dom.get("text_blocks", [])[:30],
        "headings": dom.get("headings", [])[:12],
        "interactive": [
            _element_for_expander(element, index)
            for index, element in enumerate(dom.get("interactive", [])[:MAX_LLM_EXPANSION_ELEMENTS], 1)
        ],
        "forms": [
            {
                "selectors": form.get("selectors", [])[:2],
                "fields": [
                    _element_for_expander(field, field_index)
                    for field_index, field in enumerate(form.get("fields", [])[:30], 1)
                ],
            }
            for form in dom.get("forms", [])[:8]
        ],
    }


def _state_for_goal_generation(state: dict) -> dict:
    dom = state.get("dom", {})
    return {
        "state_id": state.get("state_id"),
        "title": state.get("title"),
        "url": state.get("url"),
        "depth": state.get("depth"),
        "parent_state_id": state.get("parent_state_id"),
        "replay_actions": state.get("replay_actions", []),
        "text_blocks": dom.get("text_blocks", [])[:24],
        "headings": dom.get("headings", [])[:10],
        "interactive": [
            _element_for_audit(element, index)
            for index, element in enumerate(dom.get("interactive", [])[:60], 1)
        ],
        "forms": [],
    }


def _state_for_audit(state: dict) -> dict:
    dom = state.get("dom", {})
    return {
        "state_id": state.get("state_id"),
        "title": state.get("title"),
        "url": state.get("url"),
        "depth": state.get("depth"),
        "parent_state_id": state.get("parent_state_id"),
        "text_blocks": dom.get("text_blocks", [])[:8],
        "headings": dom.get("headings", [])[:4],
        "interactive": [
            _element_for_audit(element, index)
            for index, element in enumerate(dom.get("interactive", [])[:18], 1)
        ],
        "forms": [
            {
                "selectors": form.get("selectors", [])[:1],
                "fields": [
                    _element_for_audit(field, field_index)
                    for field_index, field in enumerate(form.get("fields", [])[:10], 1)
                ],
            }
            for form in dom.get("forms", [])[:3]
        ],
    }


def _element_for_audit(element: dict, index: int) -> dict:
    attrs = element.get("attributes", {})
    return {
        "index": index,
        "name": _truncate_text(_element_name(element), limit=90),
        "tag": element.get("tag"),
        "type": element.get("type"),
        "role": element.get("role"),
        "text": _truncate_text(element.get("text"), limit=90),
        "href": element.get("href"),
        "selectors": element.get("selectors", [])[:2],
        "attributes": {
            "id": attrs.get("id"),
            "placeholder": attrs.get("placeholder"),
            "checked": attrs.get("checked"),
            "disabled": attrs.get("disabled"),
            "readonly": attrs.get("readonly"),
            "aria_label": attrs.get("aria_label"),
        },
    }


def _element_for_expander(element: dict, index: int) -> dict:
    return {
        "index": index,
        "name": _element_name(element),
        "tag": element.get("tag"),
        "type": element.get("type"),
        "role": element.get("role"),
        "text": element.get("text"),
        "href": element.get("href"),
        "selectors": element.get("selectors", []),
        "attributes": element.get("attributes", {}),
        "bounds": element.get("bounds", {}),
    }


def _llm_workflow_expansion_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["rationale", "workflow_candidates", "rejected_goals"],
        "properties": {
            "rationale": {"type": "string"},
            "workflow_candidates": {
                "type": "array",
                "maxItems": MAX_LLM_EXPANSIONS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["parent_state_id", "label", "goal", "expected_outcome", "actions"],
                    "properties": {
                        "parent_state_id": {"type": "string"},
                        "label": {"type": "string"},
                        "goal": {"type": "string"},
                        "expected_outcome": {"type": "string"},
                        "actions": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_LLM_EXPANSION_ACTIONS,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["type", "description", "selector", "value", "key", "allow_hidden"],
                                "properties": {
                                    "type": {"type": "string", "enum": LLM_ACTION_TYPES},
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
            "rejected_goals": {
                "type": "array",
                "maxItems": 8,
                "items": {"type": "string"},
            },
        },
    }


def _llm_exploration_goal_schema(max_goals: int) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["rationale", "app_summary", "feature_areas", "exploration_goals", "deprioritized_goals"],
        "properties": {
            "rationale": {"type": "string"},
            "app_summary": {"type": "string"},
            "feature_areas": {
                "type": "array",
                "maxItems": max_goals,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "feature_id",
                        "title",
                        "description",
                        "validation_status",
                        "evidence",
                        "related_state_ids",
                        "related_path_ids",
                    ],
                    "properties": {
                        "feature_id": {"type": "string"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "validation_status": {
                            "type": "string",
                            "enum": ["validated", "partial", "needs_validation", "needs_discovery", "not_app_core"],
                        },
                        "evidence": {
                            "type": "array",
                            "maxItems": 6,
                            "items": {"type": "string"},
                        },
                        "related_state_ids": {
                            "type": "array",
                            "maxItems": 8,
                            "items": {"type": "string"},
                        },
                        "related_path_ids": {
                            "type": "array",
                            "maxItems": 8,
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "exploration_goals": {
                "type": "array",
                "maxItems": max_goals,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "goal_id",
                        "title",
                        "feature",
                        "priority",
                        "status",
                        "start_state_id",
                        "hypothesis",
                        "expected_outcome",
                        "evidence",
                        "related_path_ids",
                        "workflow_candidates",
                        "blockers",
                    ],
                    "properties": {
                        "goal_id": {"type": "string"},
                        "title": {"type": "string"},
                        "feature": {"type": "string"},
                        "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                        "status": {
                            "type": "string",
                            "enum": ["validated", "needs_validation", "needs_discovery", "blocked", "deprioritized"],
                        },
                        "start_state_id": {"type": ["string", "null"]},
                        "hypothesis": {"type": "string"},
                        "expected_outcome": {"type": "string"},
                        "evidence": {
                            "type": "array",
                            "maxItems": 6,
                            "items": {"type": "string"},
                        },
                        "related_path_ids": {
                            "type": "array",
                            "maxItems": 8,
                            "items": {"type": "string"},
                        },
                        "workflow_candidates": {
                            "type": "array",
                            "maxItems": MAX_LLM_GOAL_WORKFLOWS,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "parent_state_id",
                                    "label",
                                    "confidence",
                                    "actions",
                                ],
                                "properties": {
                                    "parent_state_id": {"type": "string"},
                                    "label": {"type": "string"},
                                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                                    "actions": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": MAX_LLM_EXPANSION_ACTIONS,
                                        "items": {
                                            "type": "object",
                                            "additionalProperties": False,
                                            "required": ["type", "description", "selector", "value", "key", "allow_hidden"],
                                            "properties": {
                                                "type": {"type": "string", "enum": LLM_ACTION_TYPES},
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
                        "blockers": {
                            "type": "array",
                            "maxItems": 6,
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "deprioritized_goals": {
                "type": "array",
                "maxItems": 8,
                "items": {"type": "string"},
            },
        },
    }


def _llm_goal_repair_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["rationale", "repaired_workflow_candidates", "abandoned_reason"],
        "properties": {
            "rationale": {"type": "string"},
            "repaired_workflow_candidates": {
                "type": "array",
                "maxItems": MAX_LLM_GOAL_REPAIR_CANDIDATES,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "parent_state_id",
                        "label",
                        "confidence",
                        "repair_strategy",
                        "actions",
                    ],
                    "properties": {
                        "parent_state_id": {"type": "string"},
                        "label": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "repair_strategy": {"type": "string"},
                        "actions": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_LLM_EXPANSION_ACTIONS,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["type", "description", "selector", "value", "key", "allow_hidden"],
                                "properties": {
                                    "type": {"type": "string", "enum": LLM_ACTION_TYPES},
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
            "abandoned_reason": {"type": "string"},
        },
    }


def _llm_outcome_repair_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["rationale", "repaired_workflow_candidates", "abandoned_reason"],
        "properties": {
            "rationale": {"type": "string"},
            "repaired_workflow_candidates": {
                "type": "array",
                "maxItems": MAX_LLM_OUTCOME_REPAIR_CANDIDATES,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "parent_state_id",
                        "label",
                        "feature",
                        "feature_id",
                        "confidence",
                        "repair_strategy",
                        "goal",
                        "expected_outcome",
                        "actions",
                    ],
                    "properties": {
                        "parent_state_id": {"type": "string"},
                        "label": {"type": "string"},
                        "feature": {"type": "string"},
                        "feature_id": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "repair_strategy": {"type": "string"},
                        "goal": {"type": "string"},
                        "expected_outcome": {"type": "string"},
                        "actions": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_LLM_EXPANSION_ACTIONS,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["type", "description", "selector", "value", "key", "allow_hidden"],
                                "properties": {
                                    "type": {"type": "string", "enum": LLM_ACTION_TYPES},
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
            "abandoned_reason": {"type": "string"},
        },
    }


def _llm_coverage_audit_schema(max_workflows: int) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "rationale",
            "app_coverage_summary",
            "covered_features",
            "missing_features",
            "low_value_paths",
            "workflow_candidates",
        ],
        "properties": {
            "rationale": {"type": "string"},
            "app_coverage_summary": {"type": "string"},
            "covered_features": {
                "type": "array",
                "maxItems": MAX_LLM_COVERAGE_AUDIT_FEATURES,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["feature_id", "title", "confidence", "evidence", "related_path_ids"],
                    "properties": {
                        "feature_id": {"type": "string"},
                        "title": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "evidence": {
                            "type": "array",
                            "maxItems": 6,
                            "items": {"type": "string"},
                        },
                        "related_path_ids": {
                            "type": "array",
                            "maxItems": 8,
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "missing_features": {
                "type": "array",
                "maxItems": MAX_LLM_COVERAGE_AUDIT_FEATURES,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["feature_id", "title", "priority", "reason", "evidence", "blockers"],
                    "properties": {
                        "feature_id": {"type": "string"},
                        "title": {"type": "string"},
                        "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                        "reason": {"type": "string"},
                        "evidence": {
                            "type": "array",
                            "maxItems": 6,
                            "items": {"type": "string"},
                        },
                        "blockers": {
                            "type": "array",
                            "maxItems": 6,
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "low_value_paths": {
                "type": "array",
                "maxItems": MAX_CANDIDATE_PATHS,
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
            "workflow_candidates": {
                "type": "array",
                "maxItems": max(0, max_workflows),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "feature_id",
                        "feature",
                        "parent_state_id",
                        "label",
                        "goal",
                        "expected_outcome",
                        "confidence",
                        "actions",
                    ],
                    "properties": {
                        "feature_id": {"type": "string"},
                        "feature": {"type": "string"},
                        "parent_state_id": {"type": "string"},
                        "label": {"type": "string"},
                        "goal": {"type": "string"},
                        "expected_outcome": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "actions": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_LLM_EXPANSION_ACTIONS,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["type", "description", "selector", "value", "key", "allow_hidden"],
                                "properties": {
                                    "type": {"type": "string", "enum": LLM_ACTION_TYPES},
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


def _normalize_llm_workflow_candidates(
    payload: dict,
    states_by_id: dict[str, dict],
    max_expansions: int,
) -> tuple[list[dict], list[dict]]:
    candidates = []
    rejected = []
    for index, raw_candidate in enumerate(payload.get("workflow_candidates", []), 1):
        if len(candidates) >= max(0, max_expansions):
            break

        requested_parent_state_id = raw_candidate.get("parent_state_id")
        parent_state = states_by_id.get(requested_parent_state_id)
        if not parent_state:
            parent_state, repair_note = _best_state_for_llm_workflow(raw_candidate, states_by_id)
            if not parent_state:
                rejected.append(_rejected_llm_candidate(raw_candidate, "Unknown parent_state_id."))
                continue
            drop_unavailable = True
        else:
            repair_note = None
            drop_unavailable = False
            if _workflow_has_unavailable_selectors(raw_candidate, parent_state):
                repaired_state, repair_note = _best_state_for_llm_workflow(raw_candidate, states_by_id)
                if repaired_state and repaired_state.get("state_id") != parent_state.get("state_id"):
                    parent_state = repaired_state
                    drop_unavailable = True

        parent_state_id = parent_state.get("state_id")
        if not parent_state_id:
            rejected.append(_rejected_llm_candidate(raw_candidate, "Repaired parent state has no state_id."))
            continue

        actions, errors = _normalize_llm_workflow_actions(
            raw_candidate,
            parent_state,
            drop_unavailable=drop_unavailable,
        )
        if errors:
            rejected.append(_rejected_llm_candidate(raw_candidate, "; ".join(errors)))
            continue
        if not _is_meaningful_workflow(actions, raw_candidate):
            rejected.append(_rejected_llm_candidate(raw_candidate, "Workflow must include a form submission or safe in-app click workflow."))
            continue

        label = _clean_label(raw_candidate.get("label") or f"LLM workflow {index}")
        candidates.append(
            {
                "candidate_id": f"llm-workflow:{parent_state_id}:{_slug(label)}",
                "parent_state_id": parent_state_id,
                "requested_parent_state_id": requested_parent_state_id,
                "kind": "llm_workflow",
                "label": label,
                "goal": _clean_label(raw_candidate.get("goal") or label),
                "expected_outcome": _clean_label(raw_candidate.get("expected_outcome") or ""),
                "repair_note": repair_note,
                "priority": 5,
                "bounds": {},
                "actions": actions,
            }
        )

    return candidates, rejected


def _normalize_exploration_goal_payload(
    payload: dict,
    states_by_id: dict[str, dict],
    max_goals: int,
) -> dict:
    feature_areas = []
    for raw_feature in payload.get("feature_areas", [])[:max_goals]:
        feature_areas.append(
            {
                "feature_id": _slug(raw_feature.get("feature_id") or raw_feature.get("title") or "feature"),
                "title": _clean_label(raw_feature.get("title") or "Feature"),
                "description": _clean_label(raw_feature.get("description") or ""),
                "validation_status": _enum_value(
                    raw_feature.get("validation_status"),
                    {"validated", "partial", "needs_validation", "needs_discovery", "not_app_core"},
                    "needs_discovery",
                ),
                "evidence": _clean_string_list(raw_feature.get("evidence"), limit=6),
                "related_state_ids": _known_state_ids(raw_feature.get("related_state_ids"), states_by_id, limit=8),
                "related_path_ids": _clean_string_list(raw_feature.get("related_path_ids"), limit=8),
            }
        )

    goals = []
    rejected_candidates = []
    for raw_goal in payload.get("exploration_goals", [])[:max_goals]:
        goal_id = _slug(raw_goal.get("goal_id") or raw_goal.get("title") or "exploration-goal")
        workflow_candidates, rejected = _normalize_goal_workflow_candidates(raw_goal, states_by_id, goal_id)
        rejected_candidates.extend(rejected)

        start_state_id = raw_goal.get("start_state_id")
        if start_state_id not in states_by_id:
            start_state_id = None

        blockers = _clean_string_list(raw_goal.get("blockers"), limit=6)
        if raw_goal.get("workflow_candidates") and not workflow_candidates:
            blockers.append("No workflow candidates survived selector normalization; validation needs a deeper scan.")

        goals.append(
            {
                "goal_id": goal_id,
                "title": _clean_label(raw_goal.get("title") or goal_id),
                "feature": _clean_label(raw_goal.get("feature") or raw_goal.get("title") or goal_id),
                "priority": _enum_value(raw_goal.get("priority"), {"high", "medium", "low"}, "medium"),
                "status": _normalized_goal_status(raw_goal.get("status"), workflow_candidates, blockers),
                "start_state_id": start_state_id,
                "hypothesis": _clean_label(raw_goal.get("hypothesis") or ""),
                "expected_outcome": _clean_label(raw_goal.get("expected_outcome") or ""),
                "evidence": _clean_string_list(raw_goal.get("evidence"), limit=6),
                "related_path_ids": _clean_string_list(raw_goal.get("related_path_ids"), limit=8),
                "workflow_candidates": workflow_candidates,
                "blockers": blockers[:6],
            }
        )

    return {
        "rationale": _clean_label(payload.get("rationale") or ""),
        "app_summary": _clean_label(payload.get("app_summary") or ""),
        "feature_areas": feature_areas,
        "goals": goals,
        "deprioritized_goals": _clean_string_list(payload.get("deprioritized_goals"), limit=8),
        "rejected_workflow_candidates": rejected_candidates,
    }


def _normalize_goal_workflow_candidates(
    raw_goal: dict,
    states_by_id: dict[str, dict],
    goal_id: str,
) -> tuple[list[dict], list[dict]]:
    normalized_candidates = []
    rejected = []
    for index, raw_candidate in enumerate(raw_goal.get("workflow_candidates", [])[:MAX_LLM_GOAL_WORKFLOWS], 1):
        requested_parent_state_id = raw_candidate.get("parent_state_id")
        parent_state = states_by_id.get(requested_parent_state_id)
        repair_note = None
        drop_unavailable = False
        if not parent_state:
            parent_state, repair_note = _best_state_for_llm_workflow(raw_candidate, states_by_id)
            drop_unavailable = bool(parent_state)
        elif _workflow_has_unavailable_selectors(raw_candidate, parent_state):
            repaired_state, repair_note = _best_state_for_llm_workflow(raw_candidate, states_by_id)
            if repaired_state and repaired_state.get("state_id") != parent_state.get("state_id"):
                parent_state = repaired_state
                drop_unavailable = True

        if not parent_state:
            rejected.append(
                {
                    "goal_id": goal_id,
                    "label": raw_candidate.get("label"),
                    "reason": "Unknown parent_state_id and no alternate state had enough selector coverage.",
                }
            )
            continue

        actions, errors = _normalize_llm_workflow_actions(
            raw_candidate,
            parent_state,
            drop_unavailable=drop_unavailable,
        )
        if errors:
            rejected.append(
                {
                    "goal_id": goal_id,
                    "label": raw_candidate.get("label"),
                    "parent_state_id": requested_parent_state_id,
                    "reason": "; ".join(errors),
                }
            )
            continue
        if not _is_meaningful_workflow(actions, raw_candidate):
            rejected.append(
                {
                    "goal_id": goal_id,
                    "label": raw_candidate.get("label"),
                    "parent_state_id": requested_parent_state_id,
                    "reason": "Workflow candidate must include a form submission or safe in-app click workflow.",
                }
            )
            continue

        label = _clean_label(raw_candidate.get("label") or f"Workflow {index}")
        parent_state_id = parent_state.get("state_id")
        normalized_candidates.append(
            {
                "candidate_id": f"exploration-goal:{goal_id}:{_slug(label)}",
                "parent_state_id": parent_state_id,
                "requested_parent_state_id": requested_parent_state_id,
                "label": label,
                "confidence": _enum_value(raw_candidate.get("confidence"), {"high", "medium", "low"}, "medium"),
                "requires_validation": True,
                "repair_note": repair_note,
                "actions": actions,
            }
        )

    return normalized_candidates, rejected


def _normalized_goal_status(status: str | None, workflow_candidates: list[dict], blockers: list[str]) -> str:
    normalized = _enum_value(
        status,
        {"validated", "needs_validation", "needs_discovery", "blocked", "deprioritized"},
        "needs_discovery",
    )
    if workflow_candidates and normalized in {"needs_discovery", "blocked"}:
        return "needs_validation"
    if blockers and normalized == "needs_validation" and not workflow_candidates:
        return "needs_discovery"
    return normalized


def _enum_value(value: str | None, allowed: set[str], fallback: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else fallback


def _clean_string_list(values: object, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    cleaned = []
    for value in values:
        text = _clean_label(value)
        if text:
            cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return cleaned


def _known_state_ids(values: object, states_by_id: dict[str, dict], limit: int) -> list[str]:
    return [
        state_id
        for state_id in _clean_string_list(values, limit=limit)
        if state_id in states_by_id
    ][:limit]


def _normalize_llm_workflow_actions(
    raw_candidate: dict,
    parent_state: dict,
    drop_unavailable: bool = False,
    additional_selector_maps: list[dict[str, dict]] | None = None,
) -> tuple[list[dict], list[str]]:
    selector_map = _selectors_for_state(parent_state)
    if additional_selector_maps:
        for additional_map in additional_selector_maps:
            selector_map.update(additional_map)
    actions = []
    errors = []
    for index, raw_action in enumerate(raw_candidate.get("actions", [])[:MAX_LLM_EXPANSION_ACTIONS], 1):
        action_type = raw_action.get("type")
        if action_type not in LLM_ACTION_TYPES:
            errors.append(f"Unsupported action type at {index}: {action_type}")
            continue

        action = {
            "type": action_type,
            "description": _clean_label(raw_action.get("description") or f"{action_type} action."),
            "origin": "llm",
        }
        selector = raw_action.get("selector")
        if action_type in SELECTOR_LLM_ACTION_TYPES:
            if not selector:
                errors.append(f"Selector is not available in parent state at {index}: {selector}")
                continue
            element = selector_map.get(selector)
            if not element:
                if _dynamic_selector_allowed(raw_action, actions, raw_candidate):
                    action["selector"] = selector
                elif drop_unavailable:
                    continue
                else:
                    errors.append(f"Selector is not available in parent state at {index}: {selector}")
                    continue
            else:
                if action_type == "fill" and (element.get("tag") or "").lower() == "select":
                    action_type = "select"
                    action["type"] = "select"
                if action_type in POINTER_LLM_ACTION_TYPES:
                    risk_reason = _click_action_risk_reason(element, parent_state, action, raw_candidate, selector)
                    if risk_reason:
                        errors.append(f"Risky {action_type} selector at {index}: {selector} ({risk_reason})")
                        continue
                action["selector"] = selector
        if action_type in {"fill", "select"}:
            value = str(raw_action.get("value") or "").strip()
            if not value:
                errors.append(f"{action_type} action missing value at {index}: {selector}")
                continue
            action["value"] = value[:120]
        if action_type == "press":
            action["key"] = str(raw_action.get("key") or "Enter")[:40]
        if raw_action.get("allow_hidden"):
            action["allow_hidden"] = True
        actions.append(action)

    return _strip_parent_replay_prefix(actions, parent_state.get("replay_actions", [])), errors


def _strip_parent_replay_prefix(actions: list[dict], replay_actions: list[dict]) -> list[dict]:
    if not actions or not replay_actions:
        return actions

    prefix_length = 0
    for candidate_action, replay_action in zip(actions, replay_actions):
        if not _actions_equivalent_for_replay_prefix(candidate_action, replay_action):
            break
        prefix_length += 1

    if prefix_length == 0:
        return actions
    return actions[prefix_length:]


def _actions_equivalent_for_replay_prefix(left: dict, right: dict) -> bool:
    if left.get("type") != right.get("type"):
        return False
    if left.get("selector") != right.get("selector"):
        return False
    action_type = left.get("type")
    if action_type in {"fill", "select"}:
        return str(left.get("value") or "") == str(right.get("value") or "")
    if action_type == "press":
        return str(left.get("key") or "Enter") == str(right.get("key") or "Enter")
    return True


def _dynamic_selector_allowed(raw_action: dict, previous_actions: list[dict], raw_candidate: dict) -> bool:
    action_type = raw_action.get("type")
    selector = str(raw_action.get("selector") or "").strip()
    if action_type not in POST_REVEAL_DYNAMIC_SELECTOR_ACTION_TYPES or not selector:
        return False
    if not any(action.get("type") in REVEALING_LLM_ACTION_TYPES for action in previous_actions):
        return False

    text = _workflow_policy_text(
        [
            {
                "type": action_type,
                "selector": selector,
                "description": raw_action.get("description"),
                "key": raw_action.get("key"),
            }
        ],
        raw_candidate,
    )
    if _has_high_risk_word(text):
        return False
    return _selector_looks_safe_for_dynamic_use(selector)


def _selector_looks_safe_for_dynamic_use(selector: str) -> bool:
    if len(selector) > 240:
        return False
    if re.search(r"[\n\r;{}]", selector):
        return False
    return True


def _workflow_has_unavailable_selectors(raw_candidate: dict, state: dict) -> bool:
    selector_map = _selectors_for_state(state)
    previous_actions = []
    for action in raw_candidate.get("actions", []):
        if (
            action.get("type") in SELECTOR_LLM_ACTION_TYPES
            and action.get("selector") not in selector_map
            and not _dynamic_selector_allowed(action, previous_actions, raw_candidate)
        ):
            return True
        previous_actions.append(action)
    return False


def _best_state_for_llm_workflow(raw_candidate: dict, states_by_id: dict[str, dict]) -> tuple[dict | None, str | None]:
    best_state = None
    best_score = 0
    best_matched = 0
    for state in states_by_id.values():
        selector_map = _selectors_for_state(state)
        score = 0
        matched = 0
        for action in raw_candidate.get("actions", []):
            selector = action.get("selector")
            if not selector or selector not in selector_map:
                continue
            matched += 1
            action_type = action.get("type")
            if action_type == "fill":
                score += 3
            elif action_type in POINTER_LLM_ACTION_TYPES or action_type == "press":
                score += 2
            else:
                score += 1

        if score > best_score:
            best_state = state
            best_score = score
            best_matched = matched

    if not best_state or best_score < 5 or best_matched < 2:
        return None, None

    return (
        best_state,
        f"Rebased workflow from {raw_candidate.get('parent_state_id')} to {best_state.get('state_id')} based on selector coverage.",
    )


def _selectors_for_state(state: dict) -> dict[str, dict]:
    return _selectors_for_dom(state.get("dom", {}))


def _selectors_for_dom(dom: dict) -> dict[str, dict]:
    selector_map = {}
    for element in dom.get("interactive", []):
        for selector in element.get("selectors", []) or []:
            selector_map[selector] = element
    return selector_map


def _selectors_for_visible_context(context: dict) -> dict[str, dict]:
    selector_map = {}
    for element in context.get("interactive", []):
        for selector in element.get("selectors", []) or []:
            selector_map[selector] = element
    for form in context.get("forms", []):
        for field in form.get("fields", []):
            for selector in field.get("selectors", []) or []:
                selector_map[selector] = field
    return selector_map


def _click_action_risk_reason(
    element: dict,
    state: dict,
    action: dict | None = None,
    candidate: dict | None = None,
    selector: str | None = None,
) -> str | None:
    action_text = _workflow_policy_text([action] if action else [], element=element, selector=selector)
    full_text = _workflow_policy_text([action] if action else [], candidate, element=element, selector=selector)
    if _has_high_risk_word(action_text) or (
        _has_high_risk_word(full_text) and not _has_safe_local_product_context(action_text)
    ):
        return "high-risk account/payment/feedback wording"
    if _has_conditional_risk_word(full_text) and not _has_safe_local_product_context(full_text):
        return "mutation wording without local product context"
    href = element.get("href")
    state_url = state.get("url")
    if href and state_url and not _is_same_app_scope_url(href, state_url):
        return "external navigation"
    return None


def _click_action_is_risky(element: dict, state: dict) -> bool:
    return _click_action_risk_reason(element, state) is not None


def _is_meaningful_workflow(actions: list[dict], candidate: dict | None = None) -> bool:
    action_types = {action.get("type") for action in actions}
    if bool({"fill", "select"} & action_types) and bool({"click", "double_click", "press"} & action_types):
        return True
    return _has_safe_click_only_workflow_intent(actions, candidate)


def _has_safe_click_only_workflow_intent(actions: list[dict], candidate: dict | None = None) -> bool:
    if not any(action.get("type") in {"click", "double_click", "press"} for action in actions):
        return False

    text = _workflow_policy_text(actions, candidate)
    if _has_high_risk_word(text):
        return False
    if _has_conditional_risk_word(text):
        return _has_safe_local_product_context(text)
    return _has_policy_word(text, CLICK_ONLY_WORKFLOW_TERMS)


def _workflow_policy_text(
    actions: list[dict],
    candidate: dict | None = None,
    element: dict | None = None,
    selector: str | None = None,
) -> str:
    parts: list[str] = []
    if candidate:
        for key in ("label", "goal", "expected_outcome", "feature", "feature_id", "hypothesis", "repair_strategy"):
            parts.append(str(candidate.get(key) or ""))
    for action in actions:
        if not action:
            continue
        for key in ("description", "selector", "key"):
            parts.append(str(action.get(key) or ""))
    if selector:
        parts.append(selector)
    if element:
        attrs = element.get("attributes") or {}
        parts.extend(
            [
                _element_name(element),
                str(element.get("tag") or ""),
                str(element.get("type") or ""),
                str(element.get("role") or ""),
                " ".join(element.get("selectors") or []),
                str(element.get("href") or ""),
                str(attrs.get("id") or ""),
                str(attrs.get("title") or ""),
                str(attrs.get("placeholder") or ""),
                str(attrs.get("aria_label") or ""),
                " ".join(attrs.get("class_names") or []),
            ]
        )
    return re.sub(r"\s+", " ", " ".join(parts).lower()).strip()


def _has_safe_local_product_context(text: str) -> bool:
    return not _has_high_risk_word(text) and _has_policy_word(text, SAFE_LOCAL_PRODUCT_ACTION_TERMS)


def _rejected_llm_candidate(candidate: dict, reason: str) -> dict:
    return {
        "parent_state_id": candidate.get("parent_state_id"),
        "label": candidate.get("label"),
        "reason": reason,
    }


def _clean_label(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _truncate_text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    text = _clean_label(str(value))
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)]}..."


async def _wait_for_settle(page) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=2_000)
    except PlaywrightTimeoutError:
        pass

    await page.wait_for_timeout(300)


def _action_candidates_for_state(state: dict, start_url: str) -> list[dict]:
    candidates = []
    seen = set()
    replay_actions = state.get("replay_actions", [])
    for element in state.get("dom", {}).get("interactive", []):
        selector = _first_selector(element)
        if not selector:
            continue

        candidate = None
        if _is_checkbox_or_toggle(element):
            candidate = _click_candidate(element, kind="toggle", priority=10)
        elif _is_actionable_text_input(element) and not _selector_was_filled(element, replay_actions):
            candidate = _input_submit_candidate(element, priority=20)
        elif _is_safe_click(element, start_url):
            priority = _click_priority(element, start_url)
            candidate = _click_candidate(element, kind="click", priority=priority)

        if not candidate:
            continue

        key = (candidate["kind"], tuple((action.get("type"), action.get("selector"), action.get("value"), action.get("key")) for action in candidate["actions"]))
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)

    return sorted(candidates, key=lambda candidate: (candidate["priority"], candidate["bounds"].get("y", 0), candidate["bounds"].get("x", 0)))


def _candidate_paths_for_states(start_url: str, states: list[dict]) -> list[dict]:
    states_by_id = {
        state.get("state_id"): state
        for state in states
        if state.get("state_id")
    }
    paths = []
    for state in states:
        if state.get("state_id") == "initial" or not state.get("replay_actions"):
            continue

        transitions = _state_path_transitions(state, states_by_id)
        if not transitions:
            continue

        path = _candidate_path_for_state(start_url, state, transitions)
        paths.append(path)

    paths.sort(key=lambda path: (-path["score"], path["depth"], path["path_id"]))
    return paths[:MAX_CANDIDATE_PATHS]


def _state_path_transitions(state: dict, states_by_id: dict[str, dict]) -> list[dict]:
    transitions = []
    current = state
    seen = set()
    while current and current.get("state_id") not in seen:
        seen.add(current.get("state_id"))
        transition = current.get("transition")
        if transition:
            transitions.append(_path_transition(transition))
        parent_id = current.get("parent_state_id")
        current = states_by_id.get(parent_id)

    return list(reversed(transitions))


def _candidate_path_for_state(start_url: str, state: dict, transitions: list[dict]) -> dict:
    labels = [transition.get("label") for transition in transitions if transition.get("label")]
    kinds = sorted({transition.get("kind") for transition in transitions if transition.get("kind")})
    quality_tags = _candidate_path_quality_tags(transitions)
    replay_actions = state.get("replay_actions", [])
    action_types = sorted({action.get("type") for action in replay_actions if action.get("type")})
    final_url = state.get("url")
    same_origin = _is_same_origin_url(final_url, start_url)
    same_path = _same_url_path(final_url, start_url)
    score, reasons = _candidate_path_score(
        start_url=start_url,
        final_url=final_url,
        transitions=transitions,
        kinds=kinds,
        action_types=action_types,
    )

    return {
        "path_id": f"path-{state.get('state_id')}",
        "score": score,
        "selection_reasons": reasons,
        "start_state_id": "initial",
        "final_state_id": state.get("state_id"),
        "depth": state.get("depth"),
        "final_url": final_url,
        "same_origin": same_origin,
        "same_path": same_path,
        "route_fragment": urlparse(final_url or "").fragment,
        "labels": labels,
        "kinds": kinds,
        "quality_tags": quality_tags,
        "action_types": action_types,
        "replay_actions": replay_actions,
        "transitions": transitions,
        "final_state_summary": _state_summary_for_path(state),
    }


def _path_transition(transition: dict) -> dict:
    return {
        "from": transition.get("from"),
        "to": transition.get("to"),
        "candidate_id": transition.get("candidate_id"),
        "label": transition.get("label"),
        "kind": transition.get("kind"),
        "goal": transition.get("goal"),
        "expected_outcome": transition.get("expected_outcome"),
        "repair_note": transition.get("repair_note"),
        "requested_parent_state_id": transition.get("requested_parent_state_id"),
        "exploration_goal_id": transition.get("exploration_goal_id"),
        "feature": transition.get("feature"),
        "feature_id": transition.get("feature_id"),
        "confidence": transition.get("confidence"),
        "repaired_from": transition.get("repaired_from"),
        "duplicate_of": transition.get("duplicate_of"),
        "origin": transition.get("origin"),
        "actions": transition.get("actions", []),
        "status": transition.get("status"),
        "outcome_summary": transition.get("outcome_summary"),
    }


def _transition_outcome_summary(before_state: dict, after_state: dict) -> dict:
    before_dom = before_state.get("dom", {})
    after_dom = after_state.get("dom", {})
    before_controls = _controls_by_identity(before_dom.get("interactive", []))
    after_controls = _controls_by_identity(after_dom.get("interactive", []))

    added_control_keys = [key for key in after_controls if key not in before_controls]
    removed_control_keys = [key for key in before_controls if key not in after_controls]
    common_control_keys = [key for key in after_controls if key in before_controls]

    changed_controls = []
    for key in common_control_keys:
        before_control = before_controls[key]
        after_control = after_controls[key]
        changes = _control_state_changes(before_control, after_control)
        if changes:
            changed_controls.append(
                {
                    "name": after_control.get("name") or before_control.get("name") or key,
                    "selectors": after_control.get("selectors") or before_control.get("selectors") or [],
                    "changes": changes,
                }
            )

    added_text = _ordered_difference(
        _normalized_text_items(after_dom.get("text_blocks", [])),
        _normalized_text_items(before_dom.get("text_blocks", [])),
    )
    removed_text = _ordered_difference(
        _normalized_text_items(before_dom.get("text_blocks", [])),
        _normalized_text_items(after_dom.get("text_blocks", [])),
    )

    return {
        "url_changed": before_state.get("url") != after_state.get("url"),
        "before_url": before_state.get("url"),
        "after_url": after_state.get("url"),
        "added_text": added_text[:MAX_OUTCOME_ITEMS],
        "removed_text": removed_text[:MAX_OUTCOME_ITEMS],
        "added_controls": [
            _control_label(after_controls[key])
            for key in added_control_keys[:MAX_OUTCOME_ITEMS]
        ],
        "removed_controls": [
            _control_label(before_controls[key])
            for key in removed_control_keys[:MAX_OUTCOME_ITEMS]
        ],
        "changed_controls": changed_controls[:MAX_OUTCOME_ITEMS],
        "counts": {
            "before_text_blocks": len(before_dom.get("text_blocks", [])),
            "after_text_blocks": len(after_dom.get("text_blocks", [])),
            "before_interactive": len(before_dom.get("interactive", [])),
            "after_interactive": len(after_dom.get("interactive", [])),
        },
    }


def _normalized_text_items(items: list[str]) -> list[str]:
    normalized = []
    seen = set()
    for item in items:
        text = re.sub(r"\s+", " ", str(item or "")).strip()
        if len(text) <= 1 or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _ordered_difference(left: list[str], right: list[str]) -> list[str]:
    right_set = set(right)
    return [item for item in left if item not in right_set]


def _controls_by_identity(elements: list[dict]) -> dict[str, dict]:
    controls = {}
    for index, element in enumerate(elements):
        key = _control_identity(element, index)
        controls[key] = _control_summary(element)
    return controls


def _control_identity(element: dict, index: int) -> str:
    selector = _first_selector(element)
    if selector:
        return f"selector:{selector}"

    name = _element_name(element)
    tag = element.get("tag") or ""
    role = element.get("role") or ""
    input_type = element.get("type") or ""
    bounds = element.get("bounds", {})
    if name:
        return f"named:{tag}:{role}:{input_type}:{name}"
    return f"bounds:{tag}:{role}:{input_type}:{bounds.get('x')}:{bounds.get('y')}:{index}"


def _control_summary(element: dict) -> dict:
    attrs = element.get("attributes", {})
    return {
        "name": _element_name(element),
        "tag": element.get("tag"),
        "type": element.get("type"),
        "role": element.get("role"),
        "text": element.get("text"),
        "selectors": element.get("selectors", [])[:2],
        "checked": attrs.get("checked"),
        "disabled": attrs.get("disabled"),
        "selected": attrs.get("selected"),
        "expanded": attrs.get("aria_expanded"),
    }


def _control_label(control: dict) -> str:
    name = control.get("name") or control.get("text")
    if name:
        return name

    selectors = control.get("selectors") or []
    selector = selectors[0] if selectors else None
    if selector:
        return selector

    return " ".join(
        value
        for value in (control.get("tag"), control.get("type"), control.get("role"))
        if value
    ) or "control"


def _control_state_changes(before_control: dict, after_control: dict) -> dict:
    changes = {}
    for key in ("name", "text", "checked", "disabled", "selected", "expanded"):
        if before_control.get(key) != after_control.get(key):
            changes[key] = {
                "before": before_control.get(key),
                "after": after_control.get(key),
            }
    return changes


def _candidate_path_score(
    start_url: str,
    final_url: str | None,
    transitions: list[dict],
    kinds: list[str],
    action_types: list[str],
) -> tuple[int, list[str]]:
    score = len(transitions)
    reasons = []

    if _same_url_path(final_url, start_url):
        score += 8
        reasons.append("stays on the scanned app path")
    elif _is_same_origin_url(final_url, start_url):
        score -= 3
        reasons.append("moves to a different same-origin path")
    else:
        score -= 20
        reasons.append("leaves the scanned origin")

    fragment = urlparse(final_url or "").fragment.strip("/")
    if fragment:
        score += 2
        reasons.append(f"ends on route fragment {fragment}")

    if kinds:
        score += len(kinds) * 4
        reasons.append(f"covers {', '.join(kinds)}")
    if action_types:
        score += len(action_types) * 2
        reasons.append(f"uses {', '.join(action_types)} actions")

    kind_set = set(kinds)
    for kind, bonus in STATE_CHANGING_KIND_BONUSES.items():
        if kind in kind_set:
            score += bonus
            reasons.append(f"includes state-changing {kind} interaction")

    if {"input_submit", "toggle"}.issubset(kind_set):
        score += 8
        reasons.append("creates content and then changes its state")

    final_kind = transitions[-1].get("kind") if transitions else None
    if final_kind in STATE_CHANGING_KIND_BONUSES:
        score += 4
        reasons.append("ends with a state-changing interaction")

    return score, reasons


def _candidate_path_quality_tags(transitions: list[dict]) -> list[str]:
    kinds = [transition.get("kind") for transition in transitions if transition.get("kind")]
    kind_set = set(kinds)
    tags = []

    if "input_submit" in kind_set:
        tags.append("submits_input")
    if "toggle" in kind_set:
        tags.append("changes_state")
    if {"llm_workflow", "llm_goal_workflow"} & kind_set:
        tags.append("llm_guided_workflow")
    if {"input_submit", "toggle"}.issubset(kind_set):
        tags.append("creates_then_mutates")
    if kinds and kinds[-1] in STATE_CHANGING_KIND_BONUSES:
        tags.append("state_changing_finish")

    return tags


def _state_summary_for_path(state: dict) -> dict:
    dom = state.get("dom", {})
    return {
        "title": state.get("title"),
        "text_blocks": dom.get("text_blocks", [])[:16],
        "headings": dom.get("headings", [])[:8],
        "interactive": [
            {
                "name": _element_name(element),
                "tag": element.get("tag"),
                "type": element.get("type"),
                "role": element.get("role"),
                "selectors": element.get("selectors", [])[:2],
                "checked": element.get("attributes", {}).get("checked"),
                "disabled": element.get("attributes", {}).get("disabled"),
            }
            for element in dom.get("interactive", [])[:16]
        ],
    }


def _input_submit_candidate(element: dict, priority: int) -> dict:
    selector = _first_selector(element)
    label = _element_name(element, fallback="input")
    value = _sample_value_for_input(element)
    return {
        "candidate_id": f"input-submit:{selector}",
        "kind": "input_submit",
        "label": f"submit {label}",
        "priority": priority,
        "bounds": element.get("bounds", {}),
        "actions": [
            {"type": "click", "selector": selector, "description": f"Focus {label}."},
            {"type": "fill", "selector": selector, "value": value, "description": f"Enter sample value for {label}."},
            {"type": "press", "selector": selector, "key": "Enter", "description": f"Submit {label}."},
        ],
    }


def _click_candidate(element: dict, kind: str, priority: int) -> dict:
    selector = _first_selector(element)
    label = _element_name(element, fallback=kind)
    action = {"type": "click", "selector": selector, "description": f"Click {label}."}
    if kind == "toggle":
        action["allow_hidden"] = True

    return {
        "candidate_id": f"{kind}:{selector}",
        "kind": kind,
        "label": label,
        "priority": priority,
        "bounds": element.get("bounds", {}),
        "actions": [action],
    }


def _click_priority(element: dict, start_url: str) -> int:
    role = (element.get("role") or "").lower()
    href = element.get("href")
    if role in {"tab", "menuitem", "option"}:
        return 30
    if href and _same_page_or_hash_link(href, start_url):
        return 35
    if (element.get("tag") or "").lower() == "button" or role == "button":
        return 40
    return 60


def _is_actionable_text_input(element: dict) -> bool:
    tag = (element.get("tag") or "").lower()
    input_type = (element.get("type") or "text").lower()
    if tag not in {"input", "textarea"}:
        return False
    if input_type in {"button", "checkbox", "file", "hidden", "image", "password", "radio", "range", "reset", "submit"}:
        return False
    hint = " ".join(
        [
            _element_name(element),
            " ".join(element.get("selectors") or []),
            str(element.get("attributes", {}).get("placeholder") or ""),
            str(element.get("name") or ""),
            str((element.get("form_context") or {}).get("label") or ""),
        ]
    ).lower()
    if _has_risk_word(hint):
        return False
    return not _is_disabled(element)


def _selector_was_filled(element: dict, replay_actions: list[dict]) -> bool:
    selectors = set(element.get("selectors") or [])
    if not selectors:
        return False

    return any(
        action.get("type") == "fill" and action.get("selector") in selectors
        for action in replay_actions
    )


def _is_checkbox_or_toggle(element: dict) -> bool:
    tag = (element.get("tag") or "").lower()
    role = (element.get("role") or "").lower()
    input_type = (element.get("type") or "").lower()
    class_names = " ".join(element.get("attributes", {}).get("class_names") or []).lower()
    if _is_disabled(element):
        return False
    return (
        (tag == "input" and input_type in {"checkbox", "radio"})
        or role in {"checkbox", "switch"}
        or "toggle" in class_names
    )


def _is_safe_click(element: dict, start_url: str) -> bool:
    if _is_disabled(element):
        return False
    label = _element_name(element).lower()
    if not label or _has_risk_word(label):
        return False

    tag = (element.get("tag") or "").lower()
    role = (element.get("role") or "").lower()
    href = element.get("href")
    if href:
        return _is_same_app_scope_url(href, start_url)

    return tag == "button" or role in {"button", "tab", "menuitem", "option"}


def _is_disabled(element: dict) -> bool:
    attrs = element.get("attributes", {})
    return bool(attrs.get("disabled") or attrs.get("readonly"))


def _has_risk_word(label: str) -> bool:
    return _has_policy_word(label, RISK_WORDS)


def _has_high_risk_word(label: str) -> bool:
    return _has_policy_word(label, HIGH_RISK_WORDS)


def _has_conditional_risk_word(label: str) -> bool:
    return _has_policy_word(label, CONDITIONAL_RISK_WORDS)


def _has_policy_word(label: str, words: set[str]) -> bool:
    normalized = re.sub(r"\s+", " ", str(label or "").lower())
    for word in words:
        needle = word.lower()
        if " " in needle or "-" in needle:
            if needle in normalized:
                return True
            continue
        if re.search(rf"\b{re.escape(needle)}\b", normalized):
            return True
    return False


def _is_same_origin(href: str, start_url: str) -> bool:
    return _is_same_origin_url(href, start_url)


def _is_same_app_scope_url(left: str | None, right: str | None) -> bool:
    if not _is_same_origin_url(left, right):
        return False

    left_path = urlparse(left or "").path or "/"
    prefix = _app_path_prefix(right or "")
    return left_path.startswith(prefix)


def _app_path_prefix(url: str) -> str:
    path = urlparse(url or "").path or "/"
    if path == "/":
        return "/"
    if path.endswith("/"):
        return path
    directory = path.rsplit("/", 1)[0]
    return f"{directory}/" if directory else "/"


def _is_same_origin_url(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False

    parsed_left = urlparse(left)
    parsed_right = urlparse(right)
    return parsed_left.scheme == parsed_right.scheme and parsed_left.netloc == parsed_right.netloc


def _same_url_path(left: str | None, right: str | None) -> bool:
    if not _is_same_origin_url(left, right):
        return False

    return urlparse(left).path == urlparse(right).path


def _same_page_or_hash_link(href: str, start_url: str) -> bool:
    return _same_url_path(href, start_url)


def _state_signature(url: str, dom: dict) -> str:
    interactive = [
        {
            "name": _element_name(element),
            "tag": element.get("tag"),
            "type": element.get("type"),
            "role": element.get("role"),
            "text": element.get("text"),
            "selectors": element.get("selectors", [])[:2],
            "checked": element.get("attributes", {}).get("checked"),
            "selected": element.get("attributes", {}).get("selected"),
            "disabled": element.get("attributes", {}).get("disabled"),
            "expanded": element.get("attributes", {}).get("aria_expanded"),
        }
        for element in dom.get("interactive", [])[:MAX_ELEMENTS_PER_GROUP]
    ]
    payload = {
        "url": url,
        "text_blocks": dom.get("text_blocks", [])[:60],
        "interactive": interactive,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _sample_value_for_input(element: dict) -> str:
    input_type = (element.get("type") or "text").lower()
    hint = " ".join(
        [
            _element_name(element),
            " ".join(element.get("selectors") or []),
            str(element.get("attributes", {}).get("placeholder") or ""),
        ]
    ).lower()

    if input_type == "email" or "email" in hint:
        return "demo@example.com"
    if input_type in {"number", "range"}:
        return "42"
    if input_type == "search" or "search" in hint:
        return "Toronto"
    if any(word in hint for word in ("todo", "task", "item")):
        return "Ship the Folio demo"
    if "name" in hint:
        return "Folio Demo"

    return "Folio demo input"


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
    return slug or "state"


def _capture_console_errors(console_errors: list[dict]):
    def capture(message):
        if message.type != "error":
            return

        console_errors.append(
            {
                "type": message.type,
                "text": message.text,
                "location": message.location,
            }
        )

    return capture


def _prune_accessibility_tree(
    node: dict | None,
    max_nodes: int = MAX_ACCESSIBILITY_NODES,
    max_depth: int = MAX_ACCESSIBILITY_DEPTH,
) -> dict | None:
    if not node:
        return None

    count = 0

    def prune(current: dict, depth: int) -> dict | None:
        nonlocal count
        if count >= max_nodes or depth > max_depth:
            return None

        count += 1
        pruned = {
            key: current[key]
            for key in ("role", "name", "value", "description", "level", "checked", "selected", "disabled")
            if key in current
        }

        children = []
        for child in current.get("children", []):
            pruned_child = prune(child, depth + 1)
            if pruned_child:
                children.append(pruned_child)

        if children:
            pruned["children"] = children

        return pruned

    return prune(node, 0)


def _dom_summary_script() -> str:
    return f"""
    () => {{
      const maxElements = {MAX_ELEMENTS_PER_GROUP};
      const maxTextBlocks = {MAX_TEXT_BLOCKS};

      const cleanText = (value) => (value || "")
        .replace(/\\s+/g, " ")
        .trim()
        .slice(0, 240);

      const isVisible = (element) => {{
        const rect = element.getBoundingClientRect();
        const style = window.getComputedStyle(element);
        return rect.width > 0
          && rect.height > 0
          && style.display !== "none"
          && style.visibility !== "hidden";
      }};

      const uniqueSelector = (selector) => {{
        try {{
          return document.querySelectorAll(selector).length === 1;
        }} catch {{
          return false;
        }}
      }};

      const attrSelector = (tag, attr, value) => {{
        if (!value) return null;
        const selector = `${{tag}}[${{attr}}="${{CSS.escape(value)}}"]`;
        return uniqueSelector(selector) ? selector : null;
      }};

      const cssPath = (element) => {{
        const parts = [];
        let current = element;

        while (current && current.nodeType === Node.ELEMENT_NODE && current !== document.body) {{
          const tag = current.tagName.toLowerCase();

          if (current.id) {{
            const selector = `#${{CSS.escape(current.id)}}`;
            if (uniqueSelector(selector)) {{
              parts.unshift(selector);
              break;
            }}
          }}

          let index = 1;
          let sibling = current.previousElementSibling;
          while (sibling) {{
            if (sibling.tagName === current.tagName) {{
              index += 1;
            }}
            sibling = sibling.previousElementSibling;
          }}

          parts.unshift(`${{tag}}:nth-of-type(${{index}})`);
          current = current.parentElement;
        }}

        return parts.join(" > ");
      }};

      const selectorCandidates = (element) => {{
        const tag = element.tagName.toLowerCase();
        const candidates = [];

        if (element.id) candidates.push(`#${{CSS.escape(element.id)}}`);
        for (const attr of ["data-testid", "data-test", "data-cy", "name", "aria-label", "placeholder", "title"]) {{
          const selector = attrSelector(tag, attr, element.getAttribute(attr));
          if (selector) candidates.push(selector);
        }}

        const classNames = Array.from(element.classList || []).slice(0, 2);
        if (classNames.length) {{
          const selector = `${{tag}}.${{classNames.map((name) => CSS.escape(name)).join(".")}}`;
          if (uniqueSelector(selector)) candidates.push(selector);
        }}

        const path = cssPath(element);
        if (path) candidates.push(path);

        return Array.from(new Set(candidates)).slice(0, 5);
      }};

      const boundsFor = (element) => {{
        const rect = element.getBoundingClientRect();
        return {{
          x: Math.round(rect.x),
          y: Math.round(rect.y),
          width: Math.round(rect.width),
          height: Math.round(rect.height),
        }};
      }};

      const labelFor = (element) => {{
        const labels = [];
        if (element.id) {{
          const explicitLabel = document.querySelector(`label[for="${{CSS.escape(element.id)}}"]`);
          if (explicitLabel) labels.push(explicitLabel.innerText);
        }}

        const wrappingLabel = element.closest("label");
        if (wrappingLabel) labels.push(wrappingLabel.innerText);

        const parentLabel = element.parentElement?.querySelector("label");
        if (parentLabel) labels.push(parentLabel.innerText);

        return cleanText(labels.find((label) => cleanText(label)) || "");
      }};

      const formContextFor = (element) => {{
        const form = element.closest("form");
        if (!form) return null;

        const heading = form.querySelector("h1, h2, h3, legend");
        return {{
          selectors: selectorCandidates(form),
          label: cleanText(heading ? heading.innerText : ""),
          method: (form.method || "get").toLowerCase(),
          action: form.action || null,
        }};
      }};

      const attributesFor = (element) => {{
        const classNames = Array.from(element.classList || []).slice(0, 8);
        return {{
          id: element.id || null,
          class_names: classNames,
          data_testid: element.getAttribute("data-testid"),
          data_test: element.getAttribute("data-test"),
          data_cy: element.getAttribute("data-cy"),
          aria_label: element.getAttribute("aria-label"),
          aria_expanded: element.getAttribute("aria-expanded"),
          aria_controls: element.getAttribute("aria-controls"),
          placeholder: element.getAttribute("placeholder"),
          autocomplete: element.getAttribute("autocomplete"),
          disabled: Boolean(element.disabled || element.getAttribute("aria-disabled") === "true"),
          required: Boolean(element.required || element.getAttribute("aria-required") === "true"),
          readonly: Boolean(element.readOnly),
          checked: typeof element.checked === "boolean" ? element.checked : null,
          selected: typeof element.selected === "boolean" ? element.selected : null,
        }};
      }};

      const describe = (element) => {{
        const tag = element.tagName.toLowerCase();
        const label = cleanText(
          element.getAttribute("aria-label")
          || labelFor(element)
          || element.getAttribute("placeholder")
          || element.getAttribute("title")
        );
        const text = cleanText(element.innerText || element.value || element.getAttribute("aria-label"));
        return {{
          tag,
          type: element.getAttribute("type"),
          role: element.getAttribute("role"),
          text,
          label,
          accessible_name: label || text,
          name: element.getAttribute("name"),
          href: element.href || null,
          attributes: attributesFor(element),
          form_context: formContextFor(element),
          selectors: selectorCandidates(element),
          bounds: boundsFor(element),
        }};
      }};

      const collect = (query) => Array.from(document.querySelectorAll(query))
        .filter(isVisible)
        .slice(0, maxElements)
        .map(describe);

      const forms = Array.from(document.querySelectorAll("form"))
        .filter(isVisible)
        .slice(0, maxElements)
        .map((form) => ({{
          selectors: selectorCandidates(form),
          action: form.action || null,
          method: (form.method || "get").toLowerCase(),
          fields: Array.from(form.querySelectorAll("input, textarea, select"))
            .filter(isVisible)
            .slice(0, 30)
            .map(describe),
        }}));

      const textBlocks = Array.from((document.body?.innerText || "").split("\\n"))
        .map(cleanText)
        .filter((text) => text.length > 1)
        .slice(0, maxTextBlocks);

      const interactive = collect('button, [role="button"], a[href], input:not([type="hidden"]), textarea, select, summary, label, [contenteditable="true"], [role="textbox"], [tabindex]:not([tabindex="-1"])')
        .sort((left, right) => {{
          if (left.bounds.y !== right.bounds.y) return left.bounds.y - right.bounds.y;
          return left.bounds.x - right.bounds.x;
        }});

      return {{
        summary: {{
          visible_text_block_count: textBlocks.length,
          heading_count: collect("h1, h2, h3").length,
          interactive_count: interactive.length,
        }},
        text_blocks: textBlocks,
        headings: collect("h1, h2, h3").map((heading) => ({{
          level: heading.tag,
          text: heading.text,
          selectors: heading.selectors,
          bounds: heading.bounds,
        }})),
        buttons: collect('button, [role="button"], input[type="button"], input[type="submit"], input[type="reset"]'),
        links: collect("a[href]"),
        inputs: collect('input:not([type="hidden"]), textarea, select'),
        forms,
        interactive,
      }};
    }}
    """
