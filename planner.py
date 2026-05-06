import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


PLAN_VERSION = "0.1"
MAX_BUTTON_SCENES = 3


def load_scan(scan_path: str | Path) -> dict:
    return json.loads(Path(scan_path).read_text(encoding="utf-8"))


def build_plan(
    scan: dict,
    scan_path: str | Path | None = None,
    output_path: str | Path | None = None,
    mode: str = "heuristic",
) -> dict:
    if mode != "heuristic":
        raise ValueError(f"Unsupported planner mode: {mode}")

    scan_path = Path(scan_path) if scan_path else None
    plan_path = _default_plan_path(scan, scan_path, output_path)
    page = scan.get("page", {})
    dom = scan.get("dom", {})
    title = _page_title(page, dom)
    scenes = _build_heuristic_scenes(scan)

    return {
        "version": PLAN_VERSION,
        "job_id": scan.get("job_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "planner": {
            "mode": mode,
            "confidence": "fallback",
            "needs_review": True,
            "notes": [
                "Heuristic plan generated without app-specific reasoning.",
                "LLM planner should replace or refine these scenes before production use.",
            ],
        },
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
) -> dict:
    scan = load_scan(scan_path)
    plan = build_plan(scan, scan_path=scan_path, output_path=output_path, mode=mode)
    plan_path = Path(plan["artifacts"]["plan_json"])
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return plan


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
    for key in ("text", "label", "name"):
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
