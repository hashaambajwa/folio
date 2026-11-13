"""Paired outcome comparison regression tests."""
import unittest

from folio_eval.compare import compare_reports


class ComparisonTests(unittest.TestCase):
    @staticmethod
    def report(outcomes, sha="same"):
        return {"suite_sha256": sha, "results": [
            {"case_id": case, "status": status} for case, status in outcomes.items()
        ]}

    def test_improvements_and_regressions(self):
        before = self.report({"a": "failed", "b": "passed", "c": "passed"})
        after = self.report({"a": "passed", "b": "failed", "c": "passed"})
        result = compare_reports(before, after)
        self.assertEqual(result["improved_case_ids"], ["a"])
        self.assertEqual(result["regressed_case_ids"], ["b"])
        self.assertEqual(result["change_percentage_points"], 0)
        self.assertEqual(result["unchanged_count"], 1)

    def test_reject_mismatched_suites_and_cases(self):
        initial = self.report({"a": "passed"})
        with self.assertRaises(ValueError):
            compare_reports(initial, self.report({"a": "passed"}, sha="different"))
        with self.assertRaises(ValueError):
            compare_reports(initial, self.report({"b": "passed"}))
        with self.assertRaises(ValueError):
            compare_reports(self.report({}), self.report({}))


if __name__ == "__main__":
    unittest.main()
