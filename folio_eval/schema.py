"""Strict, versioned benchmark inputs. Never execute arbitrary model-supplied code."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ACTION_TYPES = {"click", "double_click", "fill", "press", "select", "navigate", "observe"}
ASSERTION_TYPES = {"visible", "not_visible", "text_equals", "text_contains", "value", "checked", "count"}
_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$")


class SchemaError(ValueError):
    """Invalid or unsafe benchmark input."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SchemaError(message)


def validate_action(raw: Any) -> dict:
    _require(isinstance(raw, dict), "Action must be an object")
    action = dict(raw)
    kind = action.get("type")
    _require(kind in ACTION_TYPES, f"Unsupported action: {kind!r}")
    _require(set(action) <= {"type", "selector", "value", "key", "url", "timeout_ms", "alternate_selectors", "duration_seconds", "action_id", "description"}, "Unknown action fields")
    if kind in {"click", "double_click", "fill", "press", "select"}:
        _require(isinstance(action.get("selector"), str) and bool(action["selector"].strip()), "Selector required")
    if kind == "navigate":
        _require(isinstance(action.get("url"), str) and bool(action["url"].strip()), "Navigate URL required")
    if kind in {"fill", "select"}:
        _require(isinstance(action.get("value"), str), "Fill/select value must be a string")
    if "key" in action:
        _require(isinstance(action["key"], str) and len(action["key"]) <= 64, "Invalid key")
    if "timeout_ms" in action:
        _require(type(action["timeout_ms"]) is int and 100 <= action["timeout_ms"] <= 60000, "Invalid action timeout")
    if "duration_seconds" in action:
        _require(isinstance(action["duration_seconds"], (int, float)) and 0 <= action["duration_seconds"] <= 5, "Invalid observe duration")
    if "alternate_selectors" in action:
        choices = action["alternate_selectors"]
        _require(isinstance(choices, list) and len(choices) <= 3 and all(isinstance(s, str) and 0 < len(s) <= 300 for s in choices), "Invalid fallback selectors")
    if "selector" in action:
        _require(len(action["selector"]) <= 300, "Selector exceeds maximum length")
    if "value" in action:
        _require(len(action["value"]) <= 1000, "Value exceeds maximum length")
    return action


def validate_assertion(raw: Any) -> dict:
    _require(isinstance(raw, dict), "Assertion must be an object")
    item = dict(raw)
    kind = item.get("kind")
    _require(kind in ASSERTION_TYPES, f"Unsupported assertion kind: {kind!r}")
    _require(isinstance(item.get("selector"), str) and 0 < len(item["selector"]) <= 300, "Assertion selector required")
    _require(set(item) <= {"kind", "selector", "value"}, "Unknown assertion fields")
    if kind in {"text_equals", "text_contains", "value"}:
        _require(isinstance(item.get("value"), str), "Assertion value must be a string")
    if kind == "count":
        _require(type(item.get("value")) is int and item["value"] >= 0, "Count must be a nonnegative integer")
    if kind == "checked":
        _require(type(item.get("value")) is bool, "Checked must be a boolean")
    return item


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    goal: str
    start_url: str
    actions: tuple[dict, ...]
    assertions: tuple[dict, ...]
    category: str = "general"

    @classmethod
    def from_dict(cls, raw: dict) -> "BenchmarkCase":
        _require(isinstance(raw, dict), "Case must be an object")
        _require(set(raw) <= {"id", "goal", "start_url", "actions", "assertions", "category"}, "Unknown case fields")
        _require(isinstance(raw.get("id"), str) and bool(_ID_RE.fullmatch(raw["id"])), "Invalid case id")
        _require(isinstance(raw.get("goal"), str) and 0 < len(raw["goal"]) <= 500, "Invalid goal")
        _require(isinstance(raw.get("start_url"), str) and 0 < len(raw["start_url"]) <= 2000, "Invalid URL")
        actions = raw.get("actions")
        assertions = raw.get("assertions")
        _require(isinstance(actions, list) and 0 < len(actions) <= 30, "Expected 1-30 actions")
        _require(isinstance(assertions, list) and 0 < len(assertions) <= 20, "Expected 1-20 assertions")
        category = raw.get("category", "general")
        _require(isinstance(category, str) and bool(_ID_RE.fullmatch(category)), "Invalid category")
        return cls(raw["id"], raw["goal"], raw["start_url"],
                   tuple(validate_action(a) for a in actions),
                   tuple(validate_assertion(a) for a in assertions), category)

    def to_dict(self) -> dict:
        return {"id": self.id, "goal": self.goal, "start_url": self.start_url,
                "category": self.category, "actions": list(self.actions),
                "assertions": list(self.assertions)}


@dataclass(frozen=True)
class BenchmarkSuite:
    name: str
    cases: tuple[BenchmarkCase, ...]

    @classmethod
    def from_dict(cls, raw: dict) -> "BenchmarkSuite":
        _require(isinstance(raw, dict) and raw.get("version") == 1, "Expected suite version 1")
        _require(isinstance(raw.get("name"), str) and bool(_ID_RE.fullmatch(raw["name"])), "Invalid suite name")
        items = raw.get("cases")
        _require(isinstance(items, list) and 0 < len(items) <= 10000, "Expected 1-10000 cases")
        cases = tuple(BenchmarkCase.from_dict(item) for item in items)
        ids = [c.id for c in cases]
        _require(len(ids) == len(set(ids)), "Duplicate case IDs")
        return cls(raw["name"], cases)

    @classmethod
    def load(cls, path: str | Path) -> "BenchmarkSuite":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_dict(self) -> dict:
        return {"version": 1, "name": self.name, "cases": [c.to_dict() for c in self.cases]}

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
