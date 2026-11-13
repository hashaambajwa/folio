"""Offline unit tests; run with python -m unittest discover -s tests -v."""
import json
import tempfile
import unittest
from pathlib import Path

from folio_eval.checkpoint import Checkpoint, digest
from folio_eval.dataset import CATEGORIES, generate_suite
from folio_eval.schema import BenchmarkSuite, SchemaError, validate_action
from folio_eval.scoring import gate, summarize
from folio_eval.tracing import redact


class BenchmarkTests(unittest.TestCase):
    def test_default_suite_has_250_unique_cases(self):
        suite = generate_suite()
        self.assertEqual(len(suite.cases), 250)
        self.assertEqual(len({case.id for case in suite.cases}), 250)
        self.assertEqual({case.category for case in suite.cases}, set(CATEGORIES))

    def test_suite_hash_deterministic(self):
        self.assertEqual(generate_suite().fingerprint, generate_suite().fingerprint)
        self.assertNotEqual(generate_suite(5).fingerprint, generate_suite(6).fingerprint)

    def test_roundtrip(self):
        suite = generate_suite(10)
        self.assertEqual(BenchmarkSuite.from_dict(suite.to_dict()).fingerprint, suite.fingerprint)

    def test_reject_invalid_action(self):
        with self.assertRaises(SchemaError):
            validate_action({"type": "shell", "command": "echo unsafe"})
        with self.assertRaises(SchemaError):
            validate_action({"type": "fill", "selector": "#a"})

    def test_reject_duplicate_cases(self):
        raw = generate_suite(1).to_dict()
        raw["cases"].append(raw["cases"][0])
        with self.assertRaises(SchemaError):
            BenchmarkSuite.from_dict(raw)

    def test_checkpoint_resume_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.json"
            checkpoint = Checkpoint(path, digest({"suite": "one"}))
            checkpoint.append("case-1", {"status": "passed"})
            restored = Checkpoint(path, digest({"suite": "one"}), resume=True)
            self.assertTrue(restored.has("case-1"))
            with self.assertRaises(ValueError):
                restored.append("case-1", {})
            with self.assertRaises(ValueError):
                Checkpoint(path, digest({"suite": "two"}), resume=True)

    def test_summary_and_gate(self):
        results = [
            {"category": "create", "status": "passed", "latency_ms": 100,
             "actions": [{"status": "success"}], "assertions": [{"passed": True}]},
            {"category": "delete", "status": "failed", "latency_ms": 200,
             "actions": [{"status": "failed", "error": "TimeoutError"}],
             "assertions": [{"passed": False}]},
        ]
        metrics = summarize(results, 2)
        self.assertEqual(metrics["task_success_rate"], .5)
        self.assertEqual(metrics["latency_ms"]["median"], 150)
        self.assertFalse(gate(metrics, min_success_rate=.75)[0])
        self.assertTrue(gate(metrics, min_success_rate=.5)[0])
        self.assertFalse(gate(summarize(results, 3), min_success_rate=.5)[0])

    def test_redact(self):
        data = redact({"value": "private", "nested": {"token": "abc"},
                       "text": "Bearer abc123"})
        self.assertEqual(data["value"], "[redacted]")
        self.assertEqual(data["nested"]["token"], "[redacted]")
        self.assertNotIn("abc123", json.dumps(data))


if __name__ == "__main__":
    unittest.main()
