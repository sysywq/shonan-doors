import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from scripts.claude_cost_meter import metered_create

class UsageMeterTests(unittest.TestCase):
    def test_cache_preserves_payload_and_explicit_override(self):
        client = SimpleNamespace(messages=SimpleNamespace(create=Mock()))
        messages = [{"role": "user", "content": "Check the official source"}]
        tools = [{"type": "web_search_20250305", "name": "web_search"}]
        with patch("scripts.claude_cost_meter.record_usage"):
            metered_create(client, stage="fact_audit", messages=messages, tools=tools)
            sent = client.messages.create.call_args.kwargs
            self.assertEqual(sent["cache_control"], {"type": "ephemeral"})
            self.assertIs(sent["messages"], messages)
            self.assertIs(sent["tools"], tools)
            metered_create(client, stage="fact_audit", cache_control=None)
            self.assertIsNone(client.messages.create.call_args.kwargs["cache_control"])

    def test_records_usage_without_second_api_call(self):
        with tempfile.TemporaryDirectory() as folder:
            import os
            old = os.environ.get("CLAUDE_USAGE_LOG")
            os.environ["CLAUDE_USAGE_LOG"] = str(Path(folder) / "usage.jsonl")
            try:
                response = SimpleNamespace(model="claude-sonnet-4-6", usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0))
                class Messages:
                    calls = 0
                    def create(self, **kwargs):
                        self.calls += 1
                        return response
                client = SimpleNamespace(messages=Messages())
                self.assertIs(metered_create(client, stage="fact_audit", article_id=123, model="claude-sonnet-4-6"), response)
                self.assertEqual(client.messages.calls, 1)
                row = json.loads(Path(os.environ["CLAUDE_USAGE_LOG"]).read_text())
                self.assertEqual(row["tokens"]["input"], 100)
                self.assertEqual(row["stage"], "fact_audit")
                self.assertEqual(row["article_id"], 123)
            finally:
                if old is None: os.environ.pop("CLAUDE_USAGE_LOG", None)
                else: os.environ["CLAUDE_USAGE_LOG"] = old

if __name__ == "__main__":
    unittest.main()

class SearchPricingTests(unittest.TestCase):
    def test_search_fees_are_added_to_token_estimate(self):
        from scripts.claude_cost_meter import record_usage
        with tempfile.TemporaryDirectory() as d:
            response=SimpleNamespace(model='claude-sonnet-4-6',usage=SimpleNamespace(input_tokens=1000,output_tokens=0,server_tool_use=SimpleNamespace(web_search_requests=3)))
            row=record_usage(response,stage='news',path=Path(d)/'usage.jsonl')
            self.assertAlmostEqual(row['estimated_usd'],.033)
            self.assertEqual(row['web_search_requests'],3)
