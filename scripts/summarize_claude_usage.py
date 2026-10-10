"""Summarize Claude usage by Japan local day and stage, without API calls.

Usage: python scripts/summarize_claude_usage.py artifacts/claude_usage.jsonl
Estimated USD is not a reconciled Anthropic invoice.
"""
import argparse
import collections
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def summarize(paths):
    totals = collections.defaultdict(lambda: {
        "calls": 0, "estimated_usd": 0.0, "unknown_prices": 0,
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0},
    })
    seen = set()
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            # Same file can be supplied twice; do not double-count identical records.
            fingerprint = json.dumps(row, sort_keys=True, ensure_ascii=False)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            date_jst = datetime.fromisoformat(
                row["timestamp_utc"].replace("Z", "+00:00")
            ).astimezone(JST).date().isoformat()
            key = (date_jst, row["stage"], row.get("article_id"))
            item = totals[key]
            item["calls"] += 1
            if row.get("estimated_usd") is None:
                item["unknown_prices"] += 1
            else:
                item["estimated_usd"] += row["estimated_usd"]
            for token_type in item["tokens"]:
                item["tokens"][token_type] += int(row.get("tokens", {}).get(token_type, 0) or 0)
    return [
        {"date_jst": day, "stage": stage, "article_id": article_id, **values}
        for (day, stage, article_id), values in sorted(
            totals.items(), key=lambda item: (item[0][0], item[0][1], str(item[0][2]))
        )
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+")
    args = parser.parse_args()
    print(json.dumps(summarize(args.files), indent=2, ensure_ascii=False))
