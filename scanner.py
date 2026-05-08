from __future__ import annotations

import asyncio
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
) -> dict:
    job_id = job_id or build_job_id(url)
    output_dir = Path(output_root) / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    screenshot_path = output_dir / "screenshot.png"
    scan_path = output_dir / "scan.json"
    console_errors: list[dict] = []
    page_errors: list[dict] = []
    viewport = dict(viewport or DEFAULT_VIEWPORT)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport=viewport)

        page.on("console", _capture_console_errors(console_errors))
        page.on("pageerror", lambda exc: page_errors.append({"message": str(exc)}))

        response_status = None
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            response_status = response.status if response else None
            try:
                await page.wait_for_load_state("networkidle", timeout=5_000)
            except PlaywrightTimeoutError:
                pass

            title = await page.title()
            dom_summary = await page.evaluate(_dom_summary_script())
            accessibility_snapshot = await page.accessibility.snapshot(interesting_only=True)
            final_url = page.url
            await page.screenshot(path=str(screenshot_path), full_page=True)
        finally:
            await browser.close()

    result = {
        "job_id": job_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input": {
            "url": url,
            "timeout_ms": timeout_ms,
            "viewport": viewport,
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
        "dom": dom_summary,
        "accessibility": _prune_accessibility_tree(accessibility_snapshot),
        "browser_errors": {
            "console": console_errors,
            "page": page_errors,
        },
    }

    scan_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def run_scan(url: str, **kwargs) -> dict:
    return asyncio.run(scan(url, **kwargs))


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
          && style.visibility !== "hidden"
          && Number(style.opacity) > 0;
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
