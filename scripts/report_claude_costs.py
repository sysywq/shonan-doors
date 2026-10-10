"""Offline, ChatGPT-readable Claude cost and publication outcome report.

Examples:
 python scripts/report_claude_costs.py --usage data/claude-cost/usage.jsonl --articles data/articles.json --month 2026-10
 python scripts/report_claude_costs.py --usage data/claude-cost/usage.jsonl --articles data/articles.json --date 2026-10-11

Read-only; no Anthropic calls. Estimates are not invoices. Unattributed costs are
reported separately and included in cohort cost per published article.
"""
import argparse
import collections
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def usage_rows(path):
    seen_ids = set()
    seen_legacy = set()
    for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        call_id = row.get("call_id")
        if call_id:
            if call_id in seen_ids:
                continue
            seen_ids.add(call_id)
        else:
            # Exact duplicate legacy lines are ambiguous; mark in report.
            fingerprint = line.strip()
            if fingerprint in seen_legacy:
                continue
            seen_legacy.add(fingerprint)
        ts = datetime.fromisoformat(row["timestamp_utc"].replace("Z", "+00:00"))
        if ts.tzinfo is None:
            raise ValueError(f"usage line {lineno}: timestamp must have timezone")
        row["_day"] = ts.astimezone(JST).date().isoformat()
        yield row


def report(rows, articles, *, date=None, month=None):
    if date and month:
        raise ValueError("Choose --date or --month")
    def selected(day):
        return day == date if date else day.startswith(month) if month else True

    costs = collections.defaultdict(lambda: {"calls": 0, "estimated_usd": 0.0, "unknown_price_calls": 0,
                                             "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}})
    by_article = collections.defaultdict(float)
    by_day = collections.defaultdict(float)
    unattributed = 0.0
    unknown = 0
    for row in rows:
        day = row["_day"]
        if not selected(day):
            continue
        stage = row.get("stage") or "unknown"
        model = row.get("model") or "unknown"
        item = costs[(day, stage, model)]
        item["calls"] += 1
        value = row.get("estimated_usd")
        if value is None:
            item["unknown_price_calls"] += 1
            unknown += 1
        else:
            item["estimated_usd"] += float(value)
            by_day[day] += float(value)
            if row.get("article_id") is not None:
                by_article[str(row["article_id"])] += float(value)
            else:
                unattributed += float(value)
        for token in item["tokens"]:
            item["tokens"][token] += int((row.get("tokens") or {}).get(token) or 0)

    # articles.json includes all historical articles; date is publication cohort.
    # Presence in main is evidence of merge, NOT a live HTTP 200 check.
    published = collections.defaultdict(list)
    for article in articles:
        day = article.get("date")
        if isinstance(day, str) and selected(day):
            published[day].append({"id": article.get("id"), "title": article.get("title"),
                                   "slug": article.get("slug"),
                                   "url": "https://www.shonandoors.com/articles/" + str(article["slug"]) + "/"
                                   if article.get("slug") else None,
                                   "direct_estimated_usd": round(by_article.get(str(article.get("id")), 0), 6)})

    days = sorted(set(by_day) | set(published))
    daily = []
    for day in days:
        count = len(published.get(day, []))
        daily.append({"date_jst": day, "estimated_usd": round(by_day.get(day, 0), 6),
                      "articles_in_main": count,
                      "estimated_usd_per_article_in_main": round(by_day[day] / count, 6) if count else None,
                      "articles": published.get(day, [])})

    stages = [{"date_jst": day, "stage": stage, "model": model,
               **{**item, "estimated_usd": round(item["estimated_usd"], 6)}}
              for (day, stage, model), item in sorted(costs.items())]
    return {
        "period": {"date": date, "month": month},
        "estimated_total_usd": round(sum(by_day.values()), 6),
        "unattributed_estimated_usd": round(unattributed, 6),
        "unknown_price_calls": unknown,
        "cost_by_day_stage_model": stages,
        "daily_publication_cohorts": daily,
        "limitations": [
            "Estimated API prices, not Anthropic billed totals; unknown-price calls excluded from USD totals.",
            "articles_in_main is a repository publication proxy, not verified live HTTP 200.",
            "Costs grouped by API-call JST day; article date may differ if runs cross midnight.",
            "Shared discovery, stock and audit calls without article_id cannot be assigned to individual articles.",
            "Held/rejected costs cannot be determined from published articles alone; do not label unattributed costs as waste.",
            "Legacy records without call_id may have ambiguous duplicate identity.",
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--usage", type=Path, required=True)
    parser.add_argument("--articles", type=Path, required=True)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--date")
    scope.add_argument("--month")
    args = parser.parse_args()
    result = report(usage_rows(args.usage), load_json(args.articles), date=args.date, month=args.month)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
