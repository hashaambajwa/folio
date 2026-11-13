"""Capture privacy-conscious, append-only agent evaluation events."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

_SECRET = re.compile(r"(?i)(bearer\s+\S+|sk-[A-Za-z0-9_-]{12,}|api[_-]?key\s*[:=]\s*\S+)")
_SENSITIVE_KEYS = {"value", "password", "token", "authorization", "cookie", "secret", "api_key"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): ("[redacted]" if str(k).lower() in _SENSITIVE_KEYS else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET.sub("[redacted]", value)[:1000]
    return value


class TraceWriter:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, *, case_id: str, **fields: Any) -> None:
        payload = {"timestamp_ns": time.time_ns(), "event": event, "case_id": case_id,
                   **redact(fields)}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
