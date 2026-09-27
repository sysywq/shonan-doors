#!/usr/bin/env python3
"""Private Sheets metrics -> bounded editorial signals. No raw metrics enter git.

Daily output is a temporary JSON file consumed by generate_articles.py. Weekly
appends proposals to AI_Analysis; it never edits source metrics or articles.
"""
import argparse
import collections
import datetime as dt
import json
import os
import re
import sys
import tempfile
import unicodedata
from urllib.parse import urlsplit, unquote

SIGNAL_PATH = os.environ.get("GROWTH_SIGNAL_PATH", os.path.join(tempfile.gettempdir(), "shonan_growth_signal.json"))
TABS = ("GSC_Daily", "GSC_Query_Page", "GA4_Daily", "GA4_Page", "GA4_Landing", "Article_Master", "AI_Analysis", "Action_Log")
SITE = "www.shonandoors.com"


def rows(values):
    if not values:
        return []
    keys = values[0]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate Sheet headers")
    return [dict(zip(keys, row + [""] * (len(keys) - len(row)))) for row in values[1:] if row]


def day(value):
    if isinstance(value, (int, float)):
        return (dt.date(1899, 12, 30) + dt.timedelta(days=int(value))).isoformat()
    return str(value)[:10]


def number(value):
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def canonical(value):
    value = str(value or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme and (parsed.scheme not in ("http", "https") or parsed.hostname not in (SITE, "shonandoors.com")):
        return ""
    path = unquote(parsed.path if parsed.scheme else value.split("?")[0].split("#")[0])
    path = re.sub(r"/+", "/", path)
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/") + "/" if path != "/" else "/"


def query_key(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")).casefold())


def read_sheet(spreadsheet_id):
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    session = AuthorizedSession(credentials)
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values:batchGet"
    response = session.get(url, params=[("ranges", f"'{tab}'!A1:R") for tab in TABS] +
                           [("valueRenderOption", "UNFORMATTED_VALUE")], timeout=45)
    response.raise_for_status()
    data = response.json().get("valueRanges", [])
    if len(data) != len(TABS):
        raise ValueError("missing Sheet tabs")
    return {tab: entry.get("values", []) for tab, entry in zip(TABS, data)}


def append_analysis(spreadsheet_id, records):
    if not records:
        return
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    session = AuthorizedSession(credentials)
    endpoint = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/AI_Analysis!A:M:append"
    response = session.post(endpoint, params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                            json={"values": records}, timeout=45)
    response.raise_for_status()


def append_action(spreadsheet_id, record):
    """Append one completed action; never record an unmerged draft."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    session = AuthorizedSession(credentials)
    endpoint = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/Action_Log!A:K:append"
    response = session.post(endpoint, params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                            json={"values": [record]}, timeout=45)
    response.raise_for_status()


def analyze(tables, articles, today=None):
    """Use complete GSC dates, page-level aggregation, and conservative thresholds.

    GSC query/page is a sampled detail view. Daily totals are never inferred by
    summing that table. GA4 and GSC time zones are kept separate.
    """
    today = today or dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
    master = rows(tables["Article_Master"])
    by_path, by_id = {}, {str(a["id"]): a for a in articles}
    for entry in master:
        aid = str(entry.get("Article_ID", ""))
        path = canonical(entry.get("Canonical_Path") or entry.get("URL"))
        if aid in by_id and path:
            by_path[path] = aid
    for aid, entry in by_id.items():
        by_path.setdefault(canonical("/articles/" + entry["slug"] + "/"), aid)

    gsc_dates = sorted({day(r.get("Date")) for r in rows(tables["GSC_Daily"])
                        if day(r.get("Date")) < today.isoformat()})
    # A 7-v-7 trend requires two complete, consecutive periods; partial data
    # cannot justify a trend claim. Query-level scores use observed dates only.
    complete = len(gsc_dates) >= 14 and all(
        (dt.date.fromisoformat(gsc_dates[-14]) + dt.timedelta(days=i)).isoformat() == d
        for i, d in enumerate(gsc_dates[-14:]))
    window = set(gsc_dates[-7:])
    grouped = collections.defaultdict(lambda: [0.0, 0.0, 0.0])
    page_queries = collections.defaultdict(set)
    unmapped = 0
    for record in rows(tables["GSC_Query_Page"]):
        date = day(record.get("Date"))
        if date not in window:
            continue
        aid = by_path.get(canonical(record.get("Page")))
        if not aid:
            unmapped += 1
            continue
        article = by_id[aid]
        if article.get("mergedInto"):
            target = str(article["mergedInto"])
            if target not in by_id:
                continue
            aid = target
        query = str(record.get("Query", "")).strip()
        if not query:
            continue
        clicks = number(record.get("Clicks"))
        impressions = number(record.get("Impressions"))
        bucket = grouped[(aid, query_key(query), query)]
        bucket[0] += clicks
        bucket[1] += impressions
        bucket[2] += impressions * number(record.get("Average_Position"))
        page_queries[query_key(query)].add(aid)

    opportunities = []
    for (aid, _, query), (clicks, impressions, position_sum) in grouped.items():
        if impressions < 20:
            continue
        position = position_sum / impressions
        ctr = clicks / impressions
        if not (3 <= position <= 20) or ctr >= 0.08:
            continue
        a = by_id[aid]
        if a.get("mergedInto") or not a.get("slug"):
            continue
        # Shared queries signal a possible cannibalization review, not an
        # automatic merge. Score is capped so a single query cannot dominate.
        kind = "CANNIBALIZATION_REVIEW" if len(page_queries[query_key(query)]) > 1 else "TITLE_META_REVIEW"
        score = min(100, round(min(impressions, 200) / 4 + (20 - position) * 1.5 + (0.08 - ctr) * 100))
        opportunities.append({"article_id": int(aid), "query": query, "action": kind,
                              "impressions": round(impressions), "clicks": round(clicks),
                              "position": round(position, 1), "ctr": round(ctr, 3), "score": score})
    opportunities.sort(key=lambda o: (-o["score"], -o["impressions"], o["article_id"]))

    # No extrapolation from the incomplete query detail to site-wide demand.
    # Discovery hints only: the existing source and duplication gates remain.
    hints = [o["query"] for o in opportunities[:5]]
    return {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "gsc_period": [gsc_dates[-7], gsc_dates[-1]] if gsc_dates else [],
            "trend_available": complete, "unmapped_gsc_rows": unmapped,
            "opportunities": opportunities[:30], "discovery_hints": hints}


def write_signal(signal):
    directory = os.path.dirname(SIGNAL_PATH)
    fd, tmp = tempfile.mkstemp(prefix=".growth_", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(signal, f, ensure_ascii=False)
        os.replace(tmp, SIGNAL_PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def proposals(signal, existing):
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="seconds")
    period = signal.get("gsc_period") or []
    if len(period) != 2:
        return []
    ids = {str(r.get("Analysis_ID")) for r in rows(existing)}
    output = []
    for o in signal["opportunities"][:10]:
        key = f"growth-{period[-1]}-{o['article_id']}-{o['action']}-{query_key(o['query'])[:24]}"
        if key in ids:
            continue
        observation = f"GSC detail: {o['impressions']} impressions, {o['clicks']} clicks, CTR {o['ctr']:.1%}, position {o['position']} for {o['query']}"
        action = "重複クエリの検索意図とcanonicalを人が確認" if o["action"] == "CANNIBALIZATION_REVIEW" else "一次情報を確認し、既存記事のtitle/dek/本文に検索意図を反映する案をレビュー"
        output.append([key, now, period[0], period[-1], "Article", str(o["article_id"]), observation,
                       "検索での露出に対してクリックが少ない可能性", "見出しと検索意図に差がある可能性。因果関係は未確認",
                       action, "High" if o["score"] >= 65 else "Medium", "Low", "Proposed"])
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("daily", "weekly"))
    parser.add_argument("--snapshot", help="offline JSON fixture; never commit real metrics")
    args = parser.parse_args()
    spreadsheet_id = os.environ.get("GROWTH_SPREADSHEET_ID", "")
    if not args.snapshot and not spreadsheet_id:
        print("Growth: Sheets not configured; skip", file=sys.stderr)
        return
    try:
        tables = json.load(open(args.snapshot, encoding="utf-8")) if args.snapshot else read_sheet(spreadsheet_id)
        with open(os.path.join(os.path.dirname(__file__), "data/articles.json"), encoding="utf-8") as f:
            articles = json.load(f)
        signal = analyze(tables, articles)
        write_signal(signal)
        print(f"Growth: {len(signal['opportunities'])} review opportunities; trend={signal['trend_available']}")
        if args.mode == "weekly" and not args.snapshot:
            items = proposals(signal, tables["AI_Analysis"])
            append_analysis(spreadsheet_id, items)
            print(f"Growth: appended {len(items)} proposals")
    except Exception as exc:
        # Analytics must not block editorial publication; a weekly run surfaces
        # failure rather than silently claiming success.
        if args.mode == "weekly":
            raise
        print(f"::warning::Growth data unavailable ({type(exc).__name__}); editorial baseline continues", file=sys.stderr)


if __name__ == "__main__":
    main()
