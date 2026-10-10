"""Join archived Claude API calls with exported gate outcomes.

Conservative accounting: direct article-linked calls can be classified;
shared calls remain unallocated rather than being called rejected spend.
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path
from scripts.report_claude_costs import usage_rows


def join(usage, outcome_documents):
    outcomes = {}
    for document in outcome_documents:
        run_id = str(document.get("run_id", ""))
        for row in document.get("outcomes", []):
            article_id = row.get("article_id")
            if article_id is not None:
                outcomes[(run_id, str(article_id))] = row.get("outcome", "unknown")
    summary = defaultdict(lambda: {"calls": 0, "estimated_usd": 0.0, "unknown_price_calls": 0})
    for call in usage:
        key = (str(call.get("run_id", "")), str(call.get("article_id")))
        status = outcomes.get(key, "unattributed_or_outcome_missing")
        if call.get("article_id") is None:
            status = "shared_or_unattributed"
        item = summary[status]
        item["calls"] += 1
        if call.get("estimated_usd") is None:
            item["unknown_price_calls"] += 1
        else:
            item["estimated_usd"] += float(call["estimated_usd"])
    return {"by_outcome": {k: {**v, "estimated_usd": round(v["estimated_usd"], 6)}
                           for k, v in sorted(summary.items())},
            "limitations": "Only direct article_id + run_id matches are attributable; shared and missing records cannot be assigned to rejected articles. Gate-confirmed is not live HTTP verification."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--usage", type=Path, required=True)
    parser.add_argument("--outcomes", type=Path, nargs="+", required=True)
    args = parser.parse_args()
    documents = [json.loads(p.read_text(encoding="utf-8")) for p in args.outcomes]
    print(json.dumps(join(usage_rows(args.usage), documents), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
