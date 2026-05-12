from __future__ import annotations

import asyncio
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
PROBE_ACTION_TIMEOUT_MS = 8_000
STATE_CHANGING_KIND_BONUSES = {
    "input_submit": 6,
    "toggle": 8,
    "llm_workflow": 24,
}
RISK_WORDS = {
    "account",
    "billing",
    "buy",
    "cancel",
    "checkout",
    "clear",
    "delete",
    "invite",
    "logout",
    "pay",
    "payment",
    "purchase",
    "remove",
    "reset",
    "send",
    "sign out",
    "subscribe",
}


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
    llm_model: str | None = None,
    max_llm_expansions: int = MAX_LLM_EXPANSIONS,
    max_llm_goals: int = MAX_LLM_EXPLORATION_GOALS,
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

            if llm_goals:
                exploration_goals = _generate_llm_exploration_goals(
                    start_url=final_url,
                    states=states,
                    candidate_paths=candidate_paths,
                    source_context=source_context,
                    model=llm_model,
                    max_goals=max_llm_goals,
                )
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
            "llm_model": llm_model,
            "max_llm_expansions": max_llm_expansions,
            "max_llm_goals": max_llm_goals,
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
    accepted = 0
    attempted = 0
    results = []

    for candidate in candidates:
        parent_state = states_by_id.get(candidate["parent_state_id"])
        if not parent_state:
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
            "origin": "llm",
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
            transitions.append(transition)
            results.append(_llm_expansion_result(candidate, transition))
            continue

        next_state = trial["state"]
        transition["outcome_summary"] = _transition_outcome_summary(parent_state, next_state)
        duplicate_state_id = signatures.get(next_state["signature"])
        if duplicate_state_id and not _has_meaningful_llm_outcome(transition["outcome_summary"]):
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

    return {
        "status": "completed",
        "model": model,
        "rationale": payload.get("rationale", ""),
        "attempted": attempted,
        "accepted": accepted,
        "rejected": rejected,
        "results": results,
    }


def _llm_expansion_result(candidate: dict, transition: dict) -> dict:
    return {
        "candidate_id": candidate.get("candidate_id"),
        "parent_state_id": candidate.get("parent_state_id"),
        "requested_parent_state_id": candidate.get("requested_parent_state_id"),
        "label": candidate.get("label"),
        "goal": candidate.get("goal"),
        "repair_note": candidate.get("repair_note"),
        "status": transition.get("status"),
        "to": transition.get("to"),
        "duplicate_of": transition.get("duplicate_of"),
        "error": transition.get("error"),
    }


def _has_meaningful_llm_outcome(outcome_summary: dict | None) -> bool:
    if not outcome_summary:
        return False
    if outcome_summary.get("url_changed") or outcome_summary.get("added_text") or outcome_summary.get("removed_text"):
        return True

    for control in outcome_summary.get("changed_controls", []):
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
        await page.goto(start_url, wait_until="domcontentloaded", timeout=timeout_ms)
        await _wait_for_settle(page)

        replay_result = await _execute_probe_actions(page, parent_state.get("replay_actions", []))
        if replay_result["status"] != "success":
            return {"status": "replay_failed", "error": replay_result.get("error")}

        action_result = await _execute_probe_actions(page, candidate["actions"])
        if action_result["status"] != "success":
            return {"status": "action_failed", "error": action_result.get("error")}

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
    for action in actions:
        try:
            action_type = action["type"]
            selector = action.get("selector")
            if action_type in {"click", "fill", "press", "select"}:
                if not selector:
                    return {"status": "failed", "error": "Missing selector"}
                locator = page.locator(selector).first
                wait_state = "attached" if action.get("allow_hidden") else "visible"
                await locator.wait_for(state=wait_state, timeout=PROBE_ACTION_TIMEOUT_MS)
                if not action.get("allow_hidden"):
                    await locator.scroll_into_view_if_needed(timeout=PROBE_ACTION_TIMEOUT_MS)

            if action_type == "click":
                try:
                    await locator.click(timeout=PROBE_ACTION_TIMEOUT_MS)
                except Exception:
                    if not action.get("allow_hidden"):
                        raise
                    await page.evaluate(
                        "(selector) => document.querySelector(selector)?.click()",
                        selector,
                    )
            elif action_type == "fill":
                await locator.fill(action.get("value", ""), timeout=PROBE_ACTION_TIMEOUT_MS)
            elif action_type == "select":
                await locator.select_option(value=action.get("value", ""), timeout=PROBE_ACTION_TIMEOUT_MS)
            elif action_type == "press":
                await locator.press(action.get("key", "Enter"), timeout=PROBE_ACTION_TIMEOUT_MS)
            elif action_type == "observe":
                await page.wait_for_timeout(500)
            else:
                return {"status": "failed", "error": f"Unsupported probe action: {action_type}"}

            await _wait_for_settle(page)
        except Exception as exc:
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}

    return {"status": "success"}


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
        with urllib.request.urlopen(request, timeout=75, context=_https_context()) as response:
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


def _llm_workflow_expander_prompt() -> str:
    return (
        "You are Folio's workflow expansion strategist. Propose a small number of safe, bounded browser workflows "
        "that demonstrate core app functionality missing from the current scanner-tested candidate paths. Use only "
        "state_id values and selectors provided in the context. Prefer main content workflows that create, calculate, "
        "filter, save, search, preview, or otherwise produce visible product outcomes. Use select actions for select/dropdown "
        "elements. Avoid feedback forms, generic "
        "site search, legal/account/navigation/footer/header actions unless those are the actual product. Return only "
        "JSON matching the schema. If a workflow starts from a discovered tool page, set parent_state_id to that "
        "tool page's state and include only actions that can run from that state; do not include navigation actions "
        "from the start page. Prefer simple text/number fields plus a Calculate, Apply, Search, Save, or Submit button "
        "over complex selects when both are available. Folio will validate every proposed action in Playwright before using it."
    )


def _llm_exploration_goal_prompt() -> str:
    return (
        "You are Folio's product exploration strategist. Your job is to understand the application as a product, "
        "map its meaningful feature areas, and propose exploration goals that would maximize functional coverage. "
        "Use the provided states, candidate paths, source hints, routes, text, forms, and selectors. Distinguish "
        "validated workflows from missing or partially discovered functionality. Prefer core product workflows over "
        "generic search, feedback, legal, account, header, footer, and site navigation unless those are the product. "
        "If you propose browser actions, use only selectors present in the provided state context and set the correct "
        "parent_state_id. If a feature appears important but no usable selectors or state are available, return it as "
        "needs_discovery with blockers instead of inventing selectors. Return only JSON matching the schema. Folio "
        "will treat every workflow candidate as untrusted until Playwright validates it. Keep each description, "
        "hypothesis, evidence item, blocker, and outcome concise. For already validated features, include workflow "
        "candidates only for meaningfully distinct variations that are not already covered by candidate_paths."
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
        "supported_actions": ["observe", "click", "fill", "press", "select"],
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
            "supported_actions": ["observe", "click", "fill", "press", "select"],
        },
        "source_context": _source_context_for_expander(source_context),
        "candidate_paths": [
            {
                "path_id": path.get("path_id"),
                "score": path.get("score"),
                "labels": path.get("labels", []),
                "kinds": path.get("kinds", []),
                "quality_tags": path.get("quality_tags", []),
                "action_types": path.get("action_types", []),
                "final_state_id": path.get("final_state_id"),
                "final_url": path.get("final_url"),
                "selection_reasons": path.get("selection_reasons", []),
                "transition_outcomes": [
                    {
                        "label": transition.get("label"),
                        "kind": transition.get("kind"),
                        "goal": transition.get("goal"),
                        "expected_outcome": transition.get("expected_outcome"),
                        "outcome_summary": transition.get("outcome_summary"),
                    }
                    for transition in path.get("transitions", [])
                ],
            }
            for path in candidate_paths[:MAX_CANDIDATE_PATHS]
        ],
        "states": [
            _state_for_expander(state)
            for state in states[:MAX_LLM_GOAL_STATES]
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
                                    "type": {"type": "string", "enum": ["observe", "click", "fill", "press", "select"]},
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
                                                "type": {"type": "string", "enum": ["observe", "click", "fill", "press", "select"]},
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
        if not _is_meaningful_workflow(actions):
            rejected.append(_rejected_llm_candidate(raw_candidate, "Workflow must include a fill action and a click or press action."))
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
        if not _is_meaningful_workflow(actions):
            rejected.append(
                {
                    "goal_id": goal_id,
                    "label": raw_candidate.get("label"),
                    "parent_state_id": requested_parent_state_id,
                    "reason": "Workflow candidate must include fill/select and click/press actions.",
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
) -> tuple[list[dict], list[str]]:
    selector_map = _selectors_for_state(parent_state)
    actions = []
    errors = []
    for index, raw_action in enumerate(raw_candidate.get("actions", [])[:MAX_LLM_EXPANSION_ACTIONS], 1):
        action_type = raw_action.get("type")
        if action_type not in {"observe", "click", "fill", "press", "select"}:
            errors.append(f"Unsupported action type at {index}: {action_type}")
            continue

        action = {
            "type": action_type,
            "description": _clean_label(raw_action.get("description") or f"{action_type} action."),
            "origin": "llm",
        }
        selector = raw_action.get("selector")
        if action_type in {"click", "fill", "press", "select"}:
            if not selector or selector not in selector_map:
                if drop_unavailable:
                    continue
                errors.append(f"Selector is not available in parent state at {index}: {selector}")
                continue
            element = selector_map[selector]
            if action_type == "fill" and (element.get("tag") or "").lower() == "select":
                action_type = "select"
                action["type"] = "select"
            if action_type == "click" and _click_action_is_risky(element, parent_state):
                errors.append(f"Risky click selector at {index}: {selector}")
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

    return actions, errors


def _workflow_has_unavailable_selectors(raw_candidate: dict, state: dict) -> bool:
    selector_map = _selectors_for_state(state)
    for action in raw_candidate.get("actions", []):
        if action.get("type") in {"click", "fill", "press", "select"} and action.get("selector") not in selector_map:
            return True
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
            elif action_type in {"click", "press"}:
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
    selector_map = {}
    for element in state.get("dom", {}).get("interactive", []):
        for selector in element.get("selectors", []) or []:
            selector_map[selector] = element
    return selector_map


def _click_action_is_risky(element: dict, state: dict) -> bool:
    label = _element_name(element).lower()
    if _has_risk_word(label):
        return True
    href = element.get("href")
    state_url = state.get("url")
    return bool(href and state_url and not _is_same_origin_url(href, state_url))


def _is_meaningful_workflow(actions: list[dict]) -> bool:
    action_types = {action.get("type") for action in actions}
    return bool({"fill", "select"} & action_types) and bool({"click", "press"} & action_types)


def _rejected_llm_candidate(candidate: dict, reason: str) -> dict:
    return {
        "parent_state_id": candidate.get("parent_state_id"),
        "label": candidate.get("label"),
        "reason": reason,
    }


def _clean_label(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


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
    if "llm_workflow" in kind_set:
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
        return _is_same_origin(href, start_url)

    return tag == "button" or role in {"button", "tab", "menuitem", "option"}


def _is_disabled(element: dict) -> bool:
    attrs = element.get("attributes", {})
    return bool(attrs.get("disabled") or attrs.get("readonly"))


def _has_risk_word(label: str) -> bool:
    normalized = re.sub(r"\s+", " ", label.lower())
    return any(word in normalized for word in RISK_WORDS)


def _is_same_origin(href: str, start_url: str) -> bool:
    return _is_same_origin_url(href, start_url)


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

      const interactive = collect('button, [role="button"], a[href], input:not([type="hidden"]), textarea, select, summary, [tabindex]:not([tabindex="-1"])')
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
