"""Playwright execution with per-case isolation, bounded actions and outcome assertions."""
from __future__ import annotations

import time
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from folio_eval.schema import BenchmarkCase, validate_action
from folio_eval.tracing import TraceWriter


def _origin(url: str) -> tuple:
    parsed = urlparse(url)
    return parsed.scheme, parsed.hostname, parsed.port


def _safe_url(url: str, base_url: str) -> str:
    if url.startswith("fixture://"):
        return base_url.rstrip("/") + "/"
    if _origin(url) != _origin(base_url):
        raise ValueError("Cross-origin navigation is disabled for local benchmark")
    return url


def execute_action(page, action: dict, base_url: str) -> dict:
    action = validate_action(action)
    kind = action["type"]
    started = time.perf_counter()
    selectors = [action.get("selector")] + action.get("alternate_selectors", [])
    if kind == "navigate":
        page.goto(_safe_url(action["url"], base_url), wait_until="domcontentloaded")
    elif kind == "observe":
        if action.get("selector"):
            page.locator(action["selector"]).first.wait_for(state="visible", timeout=action.get("timeout_ms", 3000))
        page.wait_for_timeout(int(1000 * action.get("duration_seconds", 0)))
    else:
        error = None
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                timeout = action.get("timeout_ms", 2000)
                if kind == "click":
                    loc.click(timeout=timeout)
                elif kind == "double_click":
                    loc.dblclick(timeout=timeout)
                elif kind == "fill":
                    loc.fill(action["value"], timeout=timeout)
                elif kind == "press":
                    loc.press(action.get("key", "Enter"), timeout=timeout)
                elif kind == "select":
                    loc.select_option(value=action["value"], timeout=timeout)
                error = None
                break
            except PlaywrightError as exc:
                error = exc
        if error is not None:
            raise error
    return {"type": kind, "status": "success",
            "latency_ms": round((time.perf_counter() - started) * 1000, 2)}


def evaluate_assertion(page, assertion: dict) -> dict:
    kind = assertion["kind"]
    selector = assertion["selector"]
    loc = page.locator(selector)
    try:
        if kind == "visible":
            actual = loc.first.is_visible() if loc.count() else False
            passed = actual
        elif kind == "not_visible":
            actual = not any(loc.nth(i).is_visible() for i in range(loc.count()))
            passed = actual
        elif kind == "count":
            actual = loc.count()
            passed = actual == assertion["value"]
        elif kind == "checked":
            actual = loc.first.is_checked()
            passed = actual == assertion["value"]
        elif kind == "value":
            actual = loc.first.input_value()
            passed = actual == assertion["value"]
        elif kind == "text_equals":
            actual = loc.first.inner_text()
            passed = actual.strip() == assertion["value"]
        elif kind == "text_contains":
            actual = loc.first.inner_text()
            passed = assertion["value"] in actual
        else:
            raise ValueError(f"Unknown assertion: {kind}")
        return {"kind": kind, "selector": selector, "passed": passed, "actual": actual}
    except PlaywrightError as exc:
        return {"kind": kind, "selector": selector, "passed": False,
                "error": type(exc).__name__}


def execute_case(browser, case: BenchmarkCase, base_url: str, *,
                 actions: list[dict] | None = None,
                 trace: TraceWriter | None = None,
                 timeout_ms: int = 15000) -> dict:
    started = time.perf_counter()
    context = browser.new_context( viewport={"width": 1280, "height": 800})
    outcome: dict = {"case_id": case.id, "category": case.category,
                     "status": "failed", "actions": [], "assertions": [],
                     "recovered_actions": 0}
    try:
        # Strictly block third-party requests and external navigation.
        context.route("**/*", lambda route: route.continue_()
                      if _origin(route.request.url) == _origin(base_url) else route.abort())
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        page.goto(_safe_url(case.start_url, base_url), wait_until="domcontentloaded")
        selected_actions = list(case.actions) if actions is None else [validate_action(a) for a in actions]
        if trace:
            trace.emit("case.started", case_id=case.id, action_count=len(selected_actions))
        for index, action in enumerate(selected_actions):
            try:
                result = execute_action(page, action, base_url)
                if action.get("alternate_selectors"):
                    # A completed action with configured fallbacks is not necessarily a recovered action.
                    result["fallback_configured"] = True
                outcome["actions"].append(result)
                if trace:
                    trace.emit("tool.completed", case_id=case.id, index=index, type=action["type"],
                               latency_ms=result["latency_ms"])
            except Exception as exc:
                outcome["actions"].append({"type": action["type"], "status": "failed",
                                           "error": type(exc).__name__, "message": str(exc)[:300]})
                if trace:
                    trace.emit("tool.failed", case_id=case.id, index=index, type=action["type"],
                               error=type(exc).__name__)
                break
        outcome["assertions"] = [evaluate_assertion(page, assertion) for assertion in case.assertions]
        outcome["status"] = ("passed" if len(outcome["actions"]) == len(selected_actions)
                             and all(a["status"] == "success" for a in outcome["actions"])
                             and all(a["passed"] for a in outcome["assertions"]) else "failed")
    except Exception as exc:
        outcome["error"] = {"type": type(exc).__name__, "message": str(exc)[:300]}
    finally:
        outcome["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
        if trace:
            trace.emit("case.completed", case_id=case.id, status=outcome["status"],
                       latency_ms=outcome["latency_ms"])
        context.close()
    return outcome


def execute_suite_cases(cases, base_url: str, callback, *,
                        candidate: dict | None = None, trace: TraceWriter | None = None,
                        headless: bool = True, timeout_ms: int = 15000) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            for case in cases:
                actions = candidate.get(case.id) if candidate is not None else None
                callback(case, execute_case(browser, case, base_url, actions=actions,
                                            trace=trace, timeout_ms=timeout_ms))
        finally:
            browser.close()
