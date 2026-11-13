"""Crash-safe local run artifacts and honest resume semantics."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


def digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def atomic_json(path: str | Path, data: object) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".folio-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Checkpoint:
    def __init__(self, path: str | Path, fingerprint: str, *, resume: bool = False):
        self.path = Path(path)
        self.fingerprint = fingerprint
        if resume and self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if self.data.get("fingerprint") != fingerprint:
                raise ValueError("Cannot resume: suite, candidate or configuration changed")
        else:
            self.data = {"version": 1, "fingerprint": fingerprint, "cases": {}}
            atomic_json(self.path, self.data)

    def has(self, case_id: str) -> bool:
        return case_id in self.data["cases"]

    def append(self, case_id: str, result: dict) -> None:
        if self.has(case_id):
            raise ValueError(f"Case already recorded: {case_id}")
        self.data["cases"][case_id] = result
        atomic_json(self.path, self.data)

    @property
    def results(self) -> list[dict]:
        return list(self.data["cases"].values())
