"""Summarize recorded Claude API usage by UTC date and stage.

Usage: python scripts/summarize_claude_usage.py artifacts/claude_usage.jsonl
Prices are estimates, not reconciled Anthropic invoices.
"""
import argparse
import collections
import json
from pathlib import Path

def summarize(paths):
    totals = collections.defaultdict(lambda: {"calls": 0, "estimated_usd": 0.0, "unknown_prices": 0})
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            key = (row["timestamp_utc"][:10], row["stage"], row.get("article_id"))
            totals[key]["calls"] += 1
            if row.get("estimated_usd") is None:
                totals[key]["unknown_prices"] += 1
            else:
                totals[key]["estimated_usd"] += row["estimated_usd"]
    return [{"date_utc": day, "stage": stage, "article_id": article_id, **values} for (day, stage, article_id), values in sorted(totals.items(), key=lambda item: (item[0][0], item[0][1], str(item[0][2])))]

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+")
    args = parser.parse_args()
    print(json.dumps(summarize(args.files), indent=2, ensure_ascii=False))
