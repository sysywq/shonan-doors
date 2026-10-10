import unittest
from scripts.join_claude_cost_outcomes import join


class JoinClaudeCostOutcomesTests(unittest.TestCase):
    def test_direct_matches_and_shared_are_separate(self):
        calls = [{"run_id": "99", "article_id": 1, "estimated_usd": 0.3},
                 {"run_id": "99", "article_id": 2, "estimated_usd": 0.2},
                 {"run_id": "99", "article_id": None, "estimated_usd": 0.4},
                 {"run_id": "100", "article_id": 2, "estimated_usd": 0.1}]
        docs = [{"run_id": "99", "outcomes": [
            {"article_id": 1, "outcome": "confirmed_for_publication"},
            {"article_id": 2, "outcome": "held"}]}]
        result = join(calls, docs)["by_outcome"]
        self.assertEqual(result["held"]["estimated_usd"], 0.2)
        self.assertEqual(result["shared_or_unattributed"]["estimated_usd"], 0.4)
        self.assertEqual(result["unattributed_or_outcome_missing"]["estimated_usd"], 0.1)
        self.assertEqual(result["confirmed_for_publication"]["estimated_usd"], 0.3)
