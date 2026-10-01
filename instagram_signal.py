"""Instagram Business Discovery signals for Shonan Doors.

Requires Instagram API with Facebook Login:
- INSTAGRAM_FACEBOOK_ACCESS_TOKEN
- INSTAGRAM_BUSINESS_USER_ID

The current Instagram-Login token (graph.instagram.com) is intentionally not
used here because Business Discovery / hashtag discovery are Facebook-Login
capabilities.

Instagram content is a discovery signal. Publication still goes through the
existing source policy and Fact Audit.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

GRAPH_VERSION = os.environ.get("INSTAGRAM_GRAPH_VERSION", "v25.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

CHANGE_TERMS = (
    "オープン", "open", "開店", "新店", "新店舗", "閉店", "休業", "移転",
    "リニューアル", "renewal", "開業", "新発売", "発売", "新メニュー",
    "期間限定", "開催", "イベント", "出店", "popup", "pop-up", "ポップアップ",
)

DEFAULT_HASHTAGS = (
    "藤沢", "辻堂", "湘南台", "鎌倉", "茅ヶ崎", "平塚", "逗子", "葉山", "大磯", "二宮",
)

AREA_HINTS = {
    "藤沢": ("藤沢", "辻堂", "湘南台", "鵠沼", "江ノ島", "片瀬", "善行", "長後"),
    "鎌倉": ("鎌倉", "大船", "七里ヶ浜", "由比ヶ浜", "長谷", "腰越"),
    "茅ヶ崎": ("茅ヶ崎", "サザンビーチ", "香川", "浜見平"),
    "平塚": ("平塚", "湘南平"),
    "大磯": ("大磯",),
    "二宮": ("二宮",),
    "逗子": ("逗子",),
    "葉山": ("葉山",),
}


def _get(url, token, params=None, timeout=20):
    q = dict(params or {})
    q["access_token"] = token
    req = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(q)}",
        headers={"User-Agent": "ShonanDoors/1.0"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _handle_from_url(value):
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlparse(str(value))
    except ValueError:
        return ""
    if "instagram.com" not in parsed.netloc.lower():
        return ""
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return ""
    handle = parts[0].lstrip("@")
    if handle.lower() in {"p", "reel", "stories", "explore"}:
        return ""
    return re.sub(r"[^A-Za-z0-9._]", "", handle)


def watchlist_from_articles(existing_articles):
    """Build an official-account watchlist from verified article metadata."""
    result = {}
    for article in existing_articles:
        sns = article.get("snsLinks") or {}
        url = sns.get("instagram") or article.get("instagram") or ""
        handle = _handle_from_url(url)
        if not handle:
            continue
        key = handle.casefold()
        if key not in result:
            result[key] = {
                "handle": handle,
                "area": article.get("area", ""),
                "cat": article.get("cat", "b"),
                "knownSubject": (article.get("subjectNames") or [article.get("title", "")])[0],
            }
    extra = [x.strip().lstrip("@") for x in os.environ.get("INSTAGRAM_WATCH_HANDLES", "").split(",") if x.strip()]
    for handle in extra:
        key = handle.casefold()
        result.setdefault(key, {"handle": handle, "area": "", "cat": "b", "knownSubject": ""})
    return list(result.values())


def _business_discovery(token, ig_user_id, handle, media_limit=5):
    fields = (
        f"business_discovery.username({handle})"
        "{username,name,website,biography,"
        f"media.limit({media_limit})"
        "{id,caption,permalink,timestamp,media_type}}"
    )
    return _get(f"{GRAPH_BASE}/{ig_user_id}", token, {"fields": fields})


def _recent(timestamp, days):
    if not timestamp:
        return False
    try:
        stamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    return stamp >= datetime.now(timezone.utc) - timedelta(days=days)


def _contains_change_signal(text):
    low = str(text or "").casefold()
    return any(term.casefold() in low for term in CHANGE_TERMS)


def _infer_area(text, fallback=""):
    if fallback:
        return fallback
    value = str(text or "")
    for area, hints in AREA_HINTS.items():
        if any(h in value for h in hints):
            return area
    return ""


def discover_watchlist_leads(existing_articles, max_candidates=10):
    """Monitor recent posts from known official professional accounts."""
    token = os.environ.get("INSTAGRAM_FACEBOOK_ACCESS_TOKEN", "").strip()
    ig_user_id = os.environ.get("INSTAGRAM_BUSINESS_USER_ID", "").strip()
    if not token or not ig_user_id:
        return []

    days = int(os.environ.get("INSTAGRAM_SIGNAL_DAYS", "14"))
    max_accounts = int(os.environ.get("INSTAGRAM_MAX_ACCOUNTS", "20"))
    leads, seen_media = [], set()

    for item in watchlist_from_articles(existing_articles)[:max_accounts]:
        if len(leads) >= max_candidates:
            break
        try:
            data = _business_discovery(token, ig_user_id, item["handle"])
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
            continue
        discovered = data.get("business_discovery") or {}
        account_name = discovered.get("name") or discovered.get("username") or item["handle"]
        for media in ((discovered.get("media") or {}).get("data") or []):
            media_id = media.get("id", "")
            caption = " ".join(str(media.get("caption", "")).split())
            if (not media_id or media_id in seen_media or not _recent(media.get("timestamp", ""), days)
                    or not _contains_change_signal(caption)):
                continue
            area = _infer_area(caption, item.get("area", ""))
            if not area:
                continue
            seen_media.add(media_id)
            excerpt = caption[:140] + ("…" if len(caption) > 140 else "")
            leads.append({
                "id": f"ig-watch-{media_id}",
                "articleType": "news",
                "query": f"{account_name} {area} {excerpt}",
                "titleIdea": excerpt or f"{account_name} の最新情報",
                "area": area,
                "cat": item.get("cat") if item.get("cat") in {"b", "g", "e", "l", "c", "t", "p"} else "b",
                "subject": "",
                "sourceUrl": media.get("permalink", ""),
                "searchIntent": "公式Instagramの最新発表内容を確認したい",
                "leadUrl": media.get("permalink", ""),
                "leadSourceType": "instagram_business_discovery",
                "leadAuthor": discovered.get("username") or item["handle"],
                "leadCreatedAt": media.get("timestamp", ""),
            })
            if len(leads) >= max_candidates:
                break
    return leads


def _hashtag_id(token, ig_user_id, hashtag):
    data = _get(f"{GRAPH_BASE}/ig_hashtag_search", token, {"user_id": ig_user_id, "q": hashtag})
    values = data.get("data") or []
    return values[0].get("id", "") if values else ""


def discover_hashtag_leads(max_candidates=8):
    """Optional public-hashtag discovery for Facebook-Login capable apps."""
    token = os.environ.get("INSTAGRAM_FACEBOOK_ACCESS_TOKEN", "").strip()
    ig_user_id = os.environ.get("INSTAGRAM_BUSINESS_USER_ID", "").strip()
    if not token or not ig_user_id:
        return []

    tags = [x.strip().lstrip("#") for x in os.environ.get(
        "INSTAGRAM_HASHTAGS", ",".join(DEFAULT_HASHTAGS)
    ).split(",") if x.strip()]
    max_tags = int(os.environ.get("INSTAGRAM_MAX_HASHTAGS", "5"))
    days = int(os.environ.get("INSTAGRAM_SIGNAL_DAYS", "14"))
    leads, seen = [], set()

    for tag in tags[:max_tags]:
        if len(leads) >= max_candidates:
            break
        try:
            hid = _hashtag_id(token, ig_user_id, tag)
            if not hid:
                continue
            data = _get(
                f"{GRAPH_BASE}/{hid}/recent_media",
                token,
                {"user_id": ig_user_id, "fields": "id,caption,permalink,timestamp,media_type", "limit": 20},
            )
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
            continue
        for media in data.get("data") or []:
            media_id = media.get("id", "")
            caption = " ".join(str(media.get("caption", "")).split())
            if (not media_id or media_id in seen or not _recent(media.get("timestamp", ""), days)
                    or not _contains_change_signal(caption)):
                continue
            area = _infer_area(caption, tag if tag in AREA_HINTS else "")
            if not area:
                continue
            seen.add(media_id)
            excerpt = caption[:140] + ("…" if len(caption) > 140 else "")
            leads.append({
                "id": f"ig-hashtag-{media_id}",
                "articleType": "news",
                "query": f"#{tag} {area} {excerpt}",
                "titleIdea": excerpt or f"#{tag} の最新情報",
                "area": area,
                "cat": "b",
                "subject": "",
                "sourceUrl": "",
                "searchIntent": "Instagram上の地域変化シグナルの一次情報を確認したい",
                "leadUrl": media.get("permalink", ""),
                "leadSourceType": "instagram_hashtag_search",
                "leadAuthor": "",
                "leadCreatedAt": media.get("timestamp", ""),
            })
            if len(leads) >= max_candidates:
                break
    return leads


def discover_instagram_leads(existing_articles, max_candidates=12):
    """Combine watchlist + hashtag leads, deduplicated by media id/url."""
    leads = discover_watchlist_leads(existing_articles, max_candidates=max_candidates)
    if len(leads) < max_candidates:
        leads += discover_hashtag_leads(max_candidates=max_candidates - len(leads))
    seen, result = set(), []
    for lead in leads:
        key = lead.get("leadUrl") or lead.get("id")
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(lead)
    return result[:max_candidates]
