"""Generate transparent, reproducible task variants for the local fixture."""
from __future__ import annotations

import json
from pathlib import Path

from folio_eval.schema import BenchmarkSuite

FIXTURE_URL = "fixture://tasks"
CATEGORIES = ("create", "complete", "edit", "delete", "filter")


def _add(label: str) -> list[dict]:
    return [{"type": "fill", "selector": "#new-task", "value": label},
            {"type": "click", "selector": "#add-task"}]


def _item(label: str) -> str:
    # The generator only constructs alphanumeric labels, safe in this selector.
    return f'li[data-name="{label}"]'


def make_case(index: int, category: str) -> dict:
    if category not in CATEGORIES:
        raise ValueError(f"Unknown benchmark category: {category}")
    label = f"folio task {index:04d}"
    other = f"other task {index:04d}"
    actions = _add(label)
    selector = _item(label)
    assertions: list[dict] = []
    if category == "create":
        assertions = [{"kind": "visible", "selector": selector},
                      {"kind": "text_equals", "selector": "#status", "value": "1 tasks"}]
    elif category == "complete":
        actions.append({"type": "click", "selector": f'{selector} input[type="checkbox"]'})
        assertions = [{"kind": "checked", "selector": f'{selector} input[type="checkbox"]', "value": True},
                      {"kind": "visible", "selector": f"{selector}.completed"}]
    elif category == "edit":
        changed = f"renamed task {index:04d}"
        actions += [{"type": "click", "selector": f"{selector} .edit"},
                    {"type": "fill", "selector": "#edit-input", "value": changed},
                    {"type": "click", "selector": "#save-edit"}]
        assertions = [{"kind": "visible", "selector": _item(changed)},
                      {"kind": "count", "selector": selector, "value": 0}]
    elif category == "delete":
        actions.append({"type": "click", "selector": f"{selector} .delete"})
        assertions = [{"kind": "count", "selector": selector, "value": 0},
                      {"kind": "text_equals", "selector": "#status", "value": "0 tasks"}]
    else:
        actions += _add(other)
        actions.append({"type": "fill", "selector": "#filter-box", "value": label})
        assertions = [{"kind": "visible", "selector": selector},
                      {"kind": "not_visible", "selector": _item(other)},
                      {"kind": "text_equals", "selector": "#status", "value": "2 tasks"}]
    return {"id": f"{category}-{index:04d}", "category": category,
            "goal": f"{category} a task in the local task manager ({label})",
            "start_url": FIXTURE_URL, "actions": actions, "assertions": assertions}


def generate_suite(count: int = 250, name: str = "folio-browser-v1") -> BenchmarkSuite:
    if not 1 <= count <= 10000:
        raise ValueError("count must be between 1 and 10000")
    cases = [make_case(i + 1, CATEGORIES[i % len(CATEGORIES)]) for i in range(count)]
    return BenchmarkSuite.from_dict({"version": 1, "name": name, "cases": cases})


def save_suite(path: str | Path, count: int = 250) -> BenchmarkSuite:
    suite = generate_suite(count)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(suite.to_dict(), indent=2) + "\n", encoding="utf-8")
    return suite
