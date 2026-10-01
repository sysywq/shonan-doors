"""X Recent Search lead discovery for Shonan Doors.

X posts are discovery signals only. They are never used as factual sources for
published articles unless the account is independently verified as the official
source during the article research step.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://api.x.com/2/tweets/search/recent"

AREA_TERMS = {
    "藤沢": ["藤沢", "辻堂", "湘南台", "鵠沼", "江ノ島", "片瀬", "善行", "長後"],
    "鎌倉": ["鎌倉", "大船", "七里ヶ浜", "由比ヶ浜", "長谷", "腰越"],
    "茅ヶ崎": ["茅ヶ崎", "サザンビーチ", "香川", "浜見平"],
    "平塚": ["平塚", "湘南平", "ららぽーと湘南平塚"],
    "大磯": ["大磯"],
    "二宮": ["二宮"],
    "逗子": ["逗子"],
    "葉山": ["葉山"],
}

CHANGE_TERMS = [
    "オープン", "開店", "NEW OPEN", "ニューオープン", "閉店", "休業",
    "リニューアル", "移転", "工事", "建設", "開業", "新店舗", "新店",
]


def _query_for(area):
    places = " OR ".join(f'"{x}"' if " " in x else x for x in AREA_TERMS.get(area, [area]))
    changes = " OR ".join(f'"{x}"' if " " in x else x for x in CHANGE_TERMS)
    return f"({places}) ({changes}) lang:ja -is:retweet"


def _get(bearer, query, timeout=20):
    params = urllib.parse.urlencode({
        "query": query,
        "max_results": 10,
        "tweet.fields": "created_at,author_id,lang,public_metrics",
        "expansions": "author_id",
        "user.fields": "username,name,verified",
    })
    req = urllib.request.Request(
        f"{API_URL}?{params}",
        headers={"Authorization": f"Bearer {bearer}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def discover_x_leads(areas, max_candidates=12):
    """Return metadata-only leads from recent public X posts.

    Fail-soft: missing token or API errors simply yield fewer/no leads so the
    normal editorial discovery path can continue.
    """
    bearer = os.environ.get("X_BEARER_TOKEN", "").strip()
    if not bearer:
        return []

    max_queries = int(os.environ.get("X_MAX_QUERIES", "8"))
    found, seen = [], set()
    calls = 0

    for area in areas:
        if calls >= max_queries or len(found) >= max_candidates:
            break
        calls += 1
        try:
            data = _get(bearer, _query_for(area))
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
            continue

        users = {
            u.get("id"): u for u in (data.get("includes", {}).get("users", []) or [])
            if isinstance(u, dict)
        }
        for tweet in data.get("data", []) or []:
            if len(found) >= max_candidates:
                break
            tweet_id = tweet.get("id", "")
            text = " ".join(str(tweet.get("text", "")).split())
            if not tweet_id or not text or tweet_id in seen:
                continue
            seen.add(tweet_id)
            author = users.get(tweet.get("author_id"), {})
            username = author.get("username", "")
            lead_url = f"https://x.com/{username}/status/{tweet_id}" if username else f"https://x.com/i/web/status/{tweet_id}"
            metrics = tweet.get("public_metrics") or {}
            engagement = sum(int(metrics.get(k, 0) or 0) for k in ("like_count", "retweet_count", "reply_count", "quote_count"))
            excerpt = text[:120] + ("…" if len(text) > 120 else "")
            found.append({
                "id": f"x-{tweet_id}",
                "articleType": "news",
                "query": f"{area} {excerpt}",
                "titleIdea": excerpt,
                "area": area,
                "cat": "b",
                "subject": "",
                "sourceUrl": "",
                "searchIntent": "地域の新店・閉店・施設変化の事実を確認したい",
                "leadUrl": lead_url,
                "leadSourceType": "x_recent_search",
                "leadAuthor": username,
                "leadCreatedAt": tweet.get("created_at", ""),
                "leadEngagement": engagement,
            })
    return found
