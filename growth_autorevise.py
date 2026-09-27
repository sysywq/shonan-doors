#!/usr/bin/env python3
"""Conservative, resumable title/dek revision and outcome measurement.

Metrics never enter git. A proposal is not evidence of causation; changes are
allowed only after a larger, complete observation window and full source audit.
"""
import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path

from growth_feedback import analyze, append_action, day, query_key, read_sheet, rows

ROOT = Path(__file__).resolve().parent
ARTICLES = ROOT / "data/articles.json"
META = Path(os.environ.get("GROWTH_AUTOREVISE_META", "/tmp/shonan_growth_autorevise.json"))
JST = dt.timezone(dt.timedelta(hours=9))


def today():
    return dt.datetime.now(JST).date()


def key(period, aid, query):
    digest = hashlib.sha256(query_key(query).encode()).hexdigest()[:12]
    return f"growth-auto-{period}-{aid}-{digest}"


def eligible(tables, articles, signal, now):
    """Only mature, non-event pages with unaddressed search intent qualify."""
    if not signal["trend_available"] or len(signal["gsc_period"]) != 2:
        return []
    latest = dt.date.fromisoformat(signal["gsc_period"][-1])
    if (now - latest).days > 5:
        return []
    by_id = {int(a["id"]): a for a in articles}
    prior = rows(tables["Action_Log"])
    output = []
    for opportunity in signal["opportunities"]:
        if opportunity["action"] != "TITLE_META_REVIEW" or opportunity["impressions"] < 100:
            continue
        if opportunity["clicks"] >= 8 or opportunity["ctr"] >= 0.04:
            continue
        a = by_id.get(opportunity["article_id"])
        if not a or a.get("mergedInto") or a.get("eventStartDate") or a.get("eventEndDate"):
            continue
        try:
            published = dt.date.fromisoformat(day(a["date"]))
        except (ValueError, KeyError):
            continue
        if (now - published).days < 28:
            continue
        query = opportunity["query"]
        if query_key(query) in query_key(a.get("title", "")):
            continue
        # Prevent repeated edits of an article until there is an outcome review.
        if any(str(p.get("Article_ID")) == str(a["id"]) and
               str(p.get("Action_Type", "")).startswith("AUTO_TITLE_DEK") for p in prior):
            continue
        output.append((opportunity, a))
    return output[:1]  # one controlled change per run


def draft(client, article, query):
    """Return only revised title/dek; body, URL, IDs and source data are immutable."""
    response = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=600,
        system=("あなたは湘南の地域メディアの編集者。既存記事の事実・意味を保ち、検索意図を正確に表すtitle/dekのみ提案する。"
                "検索語は事実の根拠ではない。記事本文や一次情報にない事実、断定、数字を追加しない。"
                "JSONのtitleとdekだけを返す。変更が不適切なら両方を空文字にする。"),
        messages=[{"role": "user", "content": json.dumps({
            "query": query, "title": article.get("title", ""), "dek": article.get("dek", ""),
            "body": article.get("body", "")[:8000], "sources": article.get("sources", [])}, ensure_ascii=False)}])
    raw = "".join(getattr(b, "text", "") for b in response.content)
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {"title", "dek"}:
        raise ValueError("unexpected draft format")
    title, dek = data["title"], data["dek"]
    if not isinstance(title, str) or not isinstance(dek, str):
        raise ValueError("non-text draft")
    if not (10 <= len(title) <= 100 and 20 <= len(dek) <= 250):
        return None
    if title == article.get("title") and dek == article.get("dek"):
        return None
    # A meaningful match, rather than blindly adding search keywords.
    terms = [query_key(t) for t in re.split(r"\s+", query) if len(query_key(t)) >= 2]
    if not terms or not all(t in query_key(title + dek) for t in terms):
        return None
    candidate = copy.deepcopy(article)
    candidate.update(title=title, dek=dek)
    return candidate


def prepare(tables, articles, client, now):
    signal = analyze(tables, articles, today=now)
    selected = eligible(tables, articles, signal, now)
    if not selected:
        return None
    opportunity, old = selected[0]
    candidate = draft(client, old, opportunity["query"])
    if candidate is None:
        return None
    from publish_gate import check_draft
    gate = check_draft(candidate, client=client)
    if not gate["passed"]:
        print(f"Fact Audit hold: article {old['id']}, {gate['verdict']}")
        return None
    checked = gate["entry"]
    # Auto-fix may touch body or other fields; only title/dek are in scope.
    if set(checked) != set(old) or any(checked.get(k) != old.get(k) for k in old if k not in ("title", "dek")):
        print("Fact Audit altered fields outside title/dek; skip")
        return None
    terms = [query_key(t) for t in re.split(r"\s+", opportunity["query"]) if len(query_key(t)) >= 2]
    if not terms or not all(t in query_key(checked["title"] + checked["dek"]) for t in terms):
        print("Audited draft no longer addresses query; skip")
        return None
    aid = key(signal["gsc_period"][-1], old["id"], opportunity["query"])
    return {
        "action_id": aid, "article_id": old["id"], "slug": old["slug"],
        "before": {"title": old["title"], "dek": old["dek"]},
        "after": {"title": checked["title"], "dek": checked["dek"]},
        "query": opportunity["query"], "baseline": {k: opportunity[k] for k in
            ("impressions", "clicks", "ctr", "position")},
        "period": signal["gsc_period"], "date": now.isoformat(),
    }


def write_article(meta, articles):
    for a in articles:
        if a["id"] == meta["article_id"]:
            a.update(meta["after"], updated=meta["date"])
            ARTICLES.write_text(json.dumps(articles, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return
    raise ValueError("article disappeared")


def log_record(meta):
    url = f"https://www.shonandoors.com/articles/{meta['slug']}/"
    reason = (f"GSC {meta['period'][0]}–{meta['period'][1]}: {meta['query']}; "
              f"{meta['baseline']['impressions']} impressions, {meta['baseline']['clicks']} clicks, "
              f"position {meta['baseline']['position']}")
    return [meta["action_id"], dt.datetime.now(JST).isoformat(timespec="seconds"),
            str(meta["article_id"]), url, "AUTO_TITLE_DEK", reason,
            json.dumps(meta["before"], ensure_ascii=False), json.dumps(meta["after"], ensure_ascii=False),
            "github-actions[bot]", meta["action_id"],
            "Fact Audit confirmed; CI passed; merged. 14-day outcome pending. Baseline is sampled query/page detail."]


def append_once(spreadsheet_id, tables, meta):
    if meta["action_id"] in {str(r.get("Action_ID")) for r in rows(tables["Action_Log"])}:
        print("Action already logged")
        return
    append_action(spreadsheet_id, log_record(meta))


def outcome(tables, now):
    """Evaluate comparable query/page detail after 14 days; record without causal claims."""
    from growth_feedback import canonical, number
    gsc = rows(tables["GSC_Query_Page"])
    complete_dates = {day(r.get("Date")) for r in rows(tables["GSC_Daily"])}
    actions = rows(tables["Action_Log"])
    result = []
    for action in actions:
        if action.get("Action_Type") != "AUTO_TITLE_DEK":
            continue
        aid = str(action.get("Action_ID", ""))
        if any(r.get("Action_Type") == "AUTO_TITLE_DEK_OUTCOME" and r.get("Experiment_ID") == aid for r in actions):
            continue
        try:
            start = dt.date.fromisoformat(day(action["Timestamp"])) + dt.timedelta(days=1)
        except (ValueError, KeyError):
            continue
        days = [(start + dt.timedelta(days=i)).isoformat() for i in range(14)]
        if days[-1] >= now.isoformat() or not set(days).issubset(complete_dates):
            continue
        # GSC query/page detail is sampled. No data means no comparable outcome.
        match = re.search(r"GSC .*?: (.*?); \d+ impressions", str(action.get("Reason", "")))
        if not match:
            continue
        query = query_key(match.group(1))
        path = canonical(action.get("URL"))
        detail = [r for r in gsc if day(r.get("Date")) in days and
                  canonical(r.get("Page")) == path and query_key(r.get("Query")) == query]
        if not detail:
            continue
        clicks = sum(number(r.get("Clicks")) for r in detail)
        impressions = sum(number(r.get("Impressions")) for r in detail)
        if impressions <= 0:
            continue
        baseline = re.search(r"(\d+) impressions, (\d+) clicks", str(action.get("Reason", "")))
        before = ""
        if baseline and int(baseline.group(1)):
            before = f"; baseline 7-day CTR {int(baseline.group(2)) / int(baseline.group(1)):.1%}"
        note = (f"14-day observed GSC detail {days[0]}–{days[-1]}: "
                f"{int(impressions)} impressions, {int(clicks)} clicks, CTR {clicks / impressions:.1%}; "
                f"{before}; not a causal estimate; sampled detail may be incomplete")
        result.append([aid + "-outcome", dt.datetime.now(JST).isoformat(timespec="seconds"),
                       action.get("Article_ID", ""), action.get("URL", ""), "AUTO_TITLE_DEK_OUTCOME",
                       action.get("Reason", ""), "", "", "github-actions[bot]", aid, note])
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "record", "measure"))
    args = parser.parse_args()
    sheet_id = os.environ["GROWTH_SPREADSHEET_ID"]
    tables = read_sheet(sheet_id)
    if args.mode == "record":
        meta = json.loads(META.read_text(encoding="utf-8"))
        append_once(sheet_id, tables, meta)
    elif args.mode == "measure":
        for item in outcome(tables, today()):
            append_action(sheet_id, item)
            print(f"Outcome logged: {item[0]}")
    else:
        import anthropic
        articles = json.loads(ARTICLES.read_text(encoding="utf-8"))
        # Selection happens before constructing a paid client.
        signal = analyze(tables, articles, today=today())
        if not eligible(tables, articles, signal, today()):
            print("No eligible revision; no article changes")
            return
        meta = prepare(tables, articles, anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"]), today())
        if meta:
            write_article(meta, articles)
            META.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            print(f"Prepared article {meta['article_id']} with Fact Audit confirmed")
        else:
            print("No publishable revision")


if __name__ == "__main__":
    main()
