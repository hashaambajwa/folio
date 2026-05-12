from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


RECORDER_VERSION = "0.1"
SUPPORTED_ACTIONS = {"observe", "click", "fill", "press", "select", "navigate"}
DEFAULT_ACTION_TIMEOUT_MS = 10_000
DEFAULT_OBSERVE_SECONDS = 1.5
VIEWPORT_SETTLE_MS = 250
FIELD_ACTION_DELAY_MS = 250


def load_plan(plan_path: str | Path) -> dict:
    return json.loads(Path(plan_path).read_text(encoding="utf-8"))


async def record(
    plan_path: str | Path,
    output_path: str | Path | None = None,
    headless: bool = True,
    action_timeout_ms: int = DEFAULT_ACTION_TIMEOUT_MS,
    slow_mo_ms: int = 0,
) -> dict:
    plan_path = Path(plan_path)
    plan = load_plan(plan_path)
    return await record_plan(
        plan,
        plan_path=plan_path,
        output_path=output_path,
        headless=headless,
        action_timeout_ms=action_timeout_ms,
        slow_mo_ms=slow_mo_ms,
    )


def run_record(plan_path: str | Path, **kwargs) -> dict:
    return asyncio.run(record(plan_path, **kwargs))


async def record_plan(
    plan: dict,
    plan_path: str | Path | None = None,
    output_path: str | Path | None = None,
    headless: bool = True,
    action_timeout_ms: int = DEFAULT_ACTION_TIMEOUT_MS,
    slow_mo_ms: int = 0,
) -> dict:
    plan_path = Path(plan_path) if plan_path else None
    output_path = _default_recording_path(plan, plan_path, output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    video_dir = output_path.parent / "video-temp"
    video_dir.mkdir(parents=True, exist_ok=True)
    video_path = output_path.with_name("recording.webm")

    action_results = []
    started_at = datetime.now(timezone.utc)
    status = "completed"
    failure = None
    page_title = None
    final_url = None
    generated_video_path = None

    browser = None
    context = None
    page = None

    async with async_playwright() as p:
        try:
            viewport = _viewport_for(plan)
            browser = await p.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
            context = await browser.new_context(
                viewport=viewport,
                record_video_dir=str(video_dir),
                record_video_size=viewport,
            )
            page = await context.new_page()

            start_url = _start_url(plan)
            await page.goto(start_url, wait_until="domcontentloaded", timeout=action_timeout_ms)
            await _wait_for_settle(page)

            for scene in plan.get("scenes", []):
                for action in scene.get("actions", []):
                    action_result = await _execute_action(
                        page,
                        scene,
                        action,
                        action_timeout_ms=action_timeout_ms,
                        default_delay_ms=plan.get("recording", {}).get("default_action_delay_ms", 700),
                    )
                    action_results.append(action_result)
                    if action_result["status"] != "success":
                        status = "failed"
                        failure = action_result
                        break

                if status == "failed":
                    break

            page_title = await page.title()
            final_url = page.url
        except Exception as exc:
            status = "failed"
            failure = {
                "status": "failed",
                "error": type(exc).__name__,
                "message": str(exc),
            }
        finally:
            if page and page.video:
                generated_video_path = await _video_path_after_close(page, context)
                context = None
            elif context:
                await context.close()

            if browser:
                await browser.close()

    if generated_video_path:
        _move_video(generated_video_path, video_path)

    result = {
        "version": RECORDER_VERSION,
        "job_id": plan.get("job_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "plan_json": str(plan_path) if plan_path else plan.get("artifacts", {}).get("plan_json"),
            "url": _start_url(plan),
        },
        "page": {
            "title": page_title,
            "final_url": final_url,
        },
        "artifacts": {
            "recording_json": str(output_path),
            "video": str(video_path) if video_path.exists() else None,
        },
        "actions": action_results,
        "failure": failure,
    }

    output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


async def _execute_action(
    page,
    scene: dict,
    action: dict,
    action_timeout_ms: int,
    default_delay_ms: int,
) -> dict:
    action_type = action.get("type")
    result = {
        "scene_id": scene.get("scene_id"),
        "action_id": action.get("action_id"),
        "type": action_type,
        "description": action.get("description"),
        "status": "success",
    }
    if action.get("selector"):
        result["selector"] = action.get("selector")
    if action_type == "navigate" and action.get("url"):
        result["url"] = action.get("url")
    if action_type in {"fill", "select"} and action.get("value") is not None:
        result["value"] = action.get("value")
    if action_type == "press" and action.get("key"):
        result["key"] = action.get("key")

    if action_type not in SUPPORTED_ACTIONS:
        return {
            **result,
            "status": "failed",
            "error": "UnsupportedAction",
            "message": f"Unsupported action type: {action_type}",
        }

    try:
        if action_type == "observe":
            if action.get("selector"):
                await _ready_locator(page, action, action_timeout_ms, scroll_policy="observe")
            duration_seconds = min(
                action.get("duration_seconds") or scene.get("duration_seconds", DEFAULT_OBSERVE_SECONDS),
                4,
            )
            await page.wait_for_timeout(int(duration_seconds * 1000))
        elif action_type == "click":
            locator = await _ready_locator(page, action, action_timeout_ms, scroll_policy="minimal")
            try:
                await locator.click(timeout=action_timeout_ms)
            except Exception:
                if await _click_associated_label(page, action.get("selector")):
                    pass
                elif not action.get("allow_hidden"):
                    raise
                else:
                    await page.evaluate(
                        "(selector) => document.querySelector(selector)?.click()",
                        action.get("selector"),
                    )
            await _wait_for_settle(page)
        elif action_type == "fill":
            locator = await _ready_locator(page, action, action_timeout_ms, scroll_policy="none")
            await locator.fill(action.get("value", ""), timeout=action_timeout_ms)
            await page.wait_for_timeout(min(default_delay_ms, FIELD_ACTION_DELAY_MS))
        elif action_type == "select":
            locator = await _ready_locator(page, action, action_timeout_ms, scroll_policy="none")
            await locator.select_option(value=action.get("value", ""), timeout=action_timeout_ms)
            await page.wait_for_timeout(min(default_delay_ms, FIELD_ACTION_DELAY_MS))
        elif action_type == "press":
            locator = await _ready_locator(page, action, action_timeout_ms, scroll_policy="none")
            await locator.press(action.get("key", "Enter"), timeout=action_timeout_ms)
            await _wait_for_settle(page)
        elif action_type == "navigate":
            url = action.get("url")
            if not url:
                raise ValueError("Navigate action requires a url")
            await page.goto(url, wait_until="domcontentloaded", timeout=action_timeout_ms)
            await _wait_for_settle(page)

        result["url_after"] = page.url
        return result
    except Exception as exc:
        return {
            **result,
            "status": "failed",
            "error": type(exc).__name__,
            "message": str(exc),
            "url_after": page.url,
        }


async def _ready_locator(page, action: dict, action_timeout_ms: int, scroll_policy: str = "minimal"):
    selector = action.get("selector")
    if not selector:
        raise ValueError("Action requires a selector")

    locator = page.locator(selector).first
    wait_state = "attached" if action.get("allow_hidden") else "visible"
    await locator.wait_for(state=wait_state, timeout=action_timeout_ms)
    if not action.get("allow_hidden") and scroll_policy != "none":
        await _keep_locator_in_recording_view(page, locator, scroll_policy)
    return locator


async def _keep_locator_in_recording_view(page, locator, scroll_policy: str) -> None:
    did_scroll = await locator.evaluate(
        """(element, scrollPolicy) => {
            const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
            const viewportWidth = window.innerWidth || document.documentElement.clientWidth;
            const topMargin = Math.min(120, Math.max(72, viewportHeight * 0.14));
            const bottomMargin = Math.min(120, Math.max(72, viewportHeight * 0.14));

            const isInViewport = (rect) =>
                rect.bottom > 0 &&
                rect.top < viewportHeight &&
                rect.right > 0 &&
                rect.left < viewportWidth;

            const isFullyVisible = (rect) =>
                rect.top >= 0 &&
                rect.bottom <= viewportHeight &&
                rect.left >= 0 &&
                rect.right <= viewportWidth;

            let rect = element.getBoundingClientRect();
            if (scrollPolicy === "observe") {
                const fitsWithMargins = rect.height <= viewportHeight - topMargin - bottomMargin;
                const clearlyVisible = fitsWithMargins
                    ? rect.top >= topMargin && rect.bottom <= viewportHeight - bottomMargin
                    : rect.top >= topMargin && rect.top <= viewportHeight * 0.35;
                if (clearlyVisible && rect.right > 0 && rect.left < viewportWidth) {
                    return false;
                }

                const maxY = Math.max(0, document.documentElement.scrollHeight - viewportHeight);
                const rawTargetY = fitsWithMargins
                    ? window.scrollY + rect.top - ((viewportHeight - rect.height) / 2)
                    : window.scrollY + rect.top - topMargin;
                const targetY = Math.max(0, Math.min(maxY, rawTargetY));
                if (Math.abs(targetY - window.scrollY) < 8 && isInViewport(rect)) {
                    return false;
                }

                const startingY = window.scrollY;
                window.scrollTo({top: targetY, behavior: "auto"});
                return Math.abs(window.scrollY - startingY) >= 8;
            }

            if (isFullyVisible(rect)) {
                return false;
            }
            const startingY = window.scrollY;
            element.scrollIntoView({block: "nearest", inline: "nearest", behavior: "auto"});
            return Math.abs(window.scrollY - startingY) >= 8;
        }""",
        scroll_policy,
    )
    if did_scroll:
        await page.wait_for_timeout(VIEWPORT_SETTLE_MS)


async def _click_associated_label(page, selector: str | None) -> bool:
    if not selector:
        return False
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


async def _wait_for_settle(page) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=2_000)
    except PlaywrightTimeoutError:
        pass

    await page.wait_for_timeout(300)


async def _video_path_after_close(page, context) -> Path | None:
    video = page.video
    if context:
        await context.close()

    if not video:
        return None

    return Path(await video.path())


def _move_video(generated_video_path: Path, video_path: Path) -> None:
    if generated_video_path == video_path:
        return

    if video_path.exists():
        video_path.unlink()

    shutil.move(str(generated_video_path), str(video_path))
    try:
        generated_video_path.parent.rmdir()
    except OSError:
        pass


def _default_recording_path(
    plan: dict,
    plan_path: Path | None,
    output_path: str | Path | None,
) -> Path:
    if output_path:
        return Path(output_path)

    artifact_path = plan.get("artifacts", {}).get("plan_json")
    if artifact_path:
        return Path(artifact_path).with_name("recording.json")

    if plan_path:
        return plan_path.with_name("recording.json")

    return Path("outputs") / "recording.json"


def _start_url(plan: dict) -> str:
    source = plan.get("source", {})
    url = source.get("final_url") or source.get("url")
    if not url:
        raise ValueError("Plan source must include final_url or url")
    return url


def _viewport_for(plan: dict) -> dict:
    viewport = plan.get("recording", {}).get("viewport") or {}
    width = int(viewport.get("width") or 1440)
    height = int(viewport.get("height") or 900)
    return {"width": width, "height": height}
