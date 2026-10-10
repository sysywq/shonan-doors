import unittest
from scripts.export_claude_cost_outcomes import extract


class OutcomeExportTests(unittest.TestCase):
    def test_preserves_status_and_does_not_claim_live_publication(self):
        result = extract({"date": "2026-10-11", "accepted_ids": [201],
                          "held": [{"id": 202, "title": "Held", "verdict": "unconfirmed"}],
                          "gate_rejected": [{"title": "Rejected", "reasons": ["source"]}],
                          "freshness_rejected": [{"title": "Stale"}]}, "42")
        self.assertEqual([x["outcome"] for x in result["outcomes"]],
                         ["confirmed_for_publication", "held", "gate_rejected", "freshness_rejected"])
        self.assertEqual(result["run_id"], "42")
        self.assertIn("not verified HTTP 200", result["status_note"])
