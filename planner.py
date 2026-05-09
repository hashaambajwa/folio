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
MAX_LLM_CANDIDATE_PATHS = 12
MAX_LLM_TEXT_BLOCKS = 40
MAX_LLM_INTERACTIVE_ELEMENTS = 60


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
    scenes = _build_heuristic_scenes(scan)
    planner = {
        "mode": "heuristic",
        "confidence": "fallback",
        "needs_review": True,
        "notes": [
            "Heuristic plan generated without app-specific reasoning.",
            "LLM planner should replace or refine these scenes before production use.",
        ],
    }
    if planner_overrides:
        planner.update(planner_overrides)
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
        llm_payload = _call_openai_planner(scan, api_key=api_key, model=model)
        scenes = _normalize_llm_scenes(llm_payload)
        if not scenes:
            raise ValueError("LLM response did not include usable scenes.")

        plan_path = _default_plan_path(scan, scan_path, output_path)
        title = _page_title(scan.get("page", {}), scan.get("dom", {}))
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
                "notes": [
                    "LLM-generated plan using scanner DOM, source context, and replayable candidate paths.",
                    "Review selectors before using on high-stakes demos.",
                ],
                "selected_path_id": llm_payload.get("selected_path_id"),
                "rationale": llm_payload.get("rationale", ""),
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
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": model,
        "input": [
            {
                "role": "developer",
                "content": _llm_system_prompt(),
            },
            {
                "role": "user",
                "content": json.dumps(_scan_context_for_llm(scan), indent=2),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "folio_demo_plan",
                "strict": True,
                "schema": _llm_plan_schema(),
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
        "Use only selectors present in the provided scan context for click, fill, and press actions. "
        "Use discovered states and transitions to understand UI that appears after interaction. "
        "Prefer selecting a replayable candidate_paths entry and copying its actions instead of inventing actions. "
        "Use source_context when provided to infer the app's intended routes, features, and product language. "
        "Prefer reliable workflows over broad navigation. Avoid external links unless they are the core product."
    )


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
        "supported_action_types": ["observe", "click", "fill", "press"],
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
        "action_types": path.get("action_types", []),
        "replay_actions": path.get("replay_actions", []),
        "transitions": path.get("transitions", []),
        "final_state_summary": path.get("final_state_summary", {}),
    }


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
                                    "type": {"type": "string", "enum": ["observe", "click", "fill", "press"]},
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
    if action_type not in {"observe", "click", "fill", "press"}:
        return None

    normalized = {
        "action_id": _slug(action.get("action_id") or action_type),
        "type": action_type,
        "description": (action.get("description") or f"{action_type} action").strip(),
    }
    selector = action.get("selector")
    if action_type in {"click", "fill", "press"}:
        if not selector:
            return None
        normalized["selector"] = selector
    if action_type == "fill":
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


def _transition_scenes_for_path(transitions: list[dict], source_path_id: str | None = None) -> list[dict]:
    scenes = []
    for index, transition in enumerate(transitions[:MAX_TRANSITION_SCENES], 1):
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
    if action_type not in {"observe", "click", "fill", "press"}:
        return None

    planned = {
        "action_id": f"{_slug(transition.get('candidate_id') or transition.get('label') or 'probe')}-{index}",
        "type": action_type,
        "description": action.get("description") or f"{action_type} during probe.",
    }
    if action_type in {"click", "fill", "press"}:
        selector = action.get("selector")
        if not selector:
            return None
        planned["selector"] = selector
    if action_type == "fill":
        planned["value"] = action.get("value") or "Demo value"
    if action_type == "press":
        planned["key"] = action.get("key") or "Enter"
    if action.get("allow_hidden"):
        planned["allow_hidden"] = True
    return planned


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
