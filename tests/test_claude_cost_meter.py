import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from scripts.claude_cost_meter import metered_create

class UsageMeterTests(unittest.TestCase):
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
                self.assertIs(metered_create(client, stage="fact_audit", model="claude-sonnet-4-6"), response)
                self.assertEqual(client.messages.calls, 1)
                row = json.loads(Path(os.environ["CLAUDE_USAGE_LOG"]).read_text())
                self.assertEqual(row["tokens"]["input"], 100)
                self.assertEqual(row["stage"], "fact_audit")
            finally:
                if old is None: os.environ.pop("CLAUDE_USAGE_LOG", None)
                else: os.environ["CLAUDE_USAGE_LOG"] = old

if __name__ == "__main__":
    unittest.main()
