from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


DEFAULT_VIEWPORT = {"width": 1440, "height": 900}
MAX_ELEMENTS_PER_GROUP = 100
MAX_TEXT_BLOCKS = 80
MAX_ACCESSIBILITY_NODES = 120
MAX_ACCESSIBILITY_DEPTH = 4
DEFAULT_PROBE_DEPTH = 3
DEFAULT_MAX_STATES = 16
DEFAULT_MAX_ACTIONS_PER_STATE = 5
PROBE_ACTION_TIMEOUT_MS = 8_000
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
            if action_type in {"click", "fill", "press"}:
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
    parsed_href = urlparse(href)
    parsed_start = urlparse(start_url)
    return parsed_href.scheme == parsed_start.scheme and parsed_href.netloc == parsed_start.netloc


def _same_page_or_hash_link(href: str, start_url: str) -> bool:
    parsed_href = urlparse(href)
    parsed_start = urlparse(start_url)
    return _is_same_origin(href, start_url) and parsed_href.path == parsed_start.path


def _state_signature(url: str, dom: dict) -> str:
    interactive = [
        {
            "name": _element_name(element),
            "tag": element.get("tag"),
            "type": element.get("type"),
            "role": element.get("role"),
            "selectors": element.get("selectors", [])[:2],
            "checked": element.get("attributes", {}).get("checked"),
            "selected": element.get("attributes", {}).get("selected"),
            "expanded": element.get("attributes", {}).get("aria_expanded"),
        }
        for element in dom.get("interactive", [])[:50]
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
