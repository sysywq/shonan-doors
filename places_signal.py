"""Google Places lead discovery for Shonan Doors.

This module treats Places as a discovery signal only. Article facts must still be
verified against official primary sources before publication.
"""
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

API_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.businessStatus",
    "places.openingDate",
    "places.primaryType",
])

AREA_QUERY = {
    "藤沢": "藤沢市 神奈川県",
    "鎌倉": "鎌倉市 神奈川県",
    "茅ヶ崎": "茅ヶ崎市 神奈川県",
    "平塚": "平塚市 神奈川県",
    "大磯": "大磯町 神奈川県",
    "二宮": "二宮町 神奈川県",
    "逗子": "逗子市 神奈川県",
    "葉山": "葉山町 神奈川県",
}

GOURMET_TYPES = {
    "restaurant", "cafe", "coffee_shop", "bakery", "bar", "meal_takeaway",
    "ice_cream_shop", "dessert_shop", "ramen_restaurant", "sushi_restaurant",
}


def _opening_date(value):
    if not isinstance(value, dict):
        return ""
    y, m, d = value.get("year"), value.get("month"), value.get("day")
    if not all(isinstance(v, int) for v in (y, m, d)):
        return ""
    return f"{y:04d}-{m:02d}-{d:02d}"


def _post(api_key, payload, timeout=20):
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": FIELD_MASK,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def discover_future_openings(areas, max_candidates=12, include_operational=False, state_path='data/places_seen.json'):
    """Return FUTURE_OPENING places as metadata-only news leads.

    Fail-soft by design: if the key is absent or an individual query fails, the
    normal editorial discovery pipeline still runs.
    """
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "").strip()
    if not api_key:
        return []

    terms = [x.strip() for x in os.environ.get(
        "PLACES_SEARCH_TERMS", "restaurant,cafe,store"
    ).split(",") if x.strip()]
    max_queries = int(os.environ.get("PLACES_MAX_QUERIES", "24"))
    found, seen = [], set()
    state_file = Path(state_path)
    previous = set()
    if include_operational and state_file.exists():
        try:
            previous = set(json.loads(state_file.read_text(encoding="utf-8")).get("place_ids", []))
        except (OSError, ValueError, TypeError, AttributeError):
            include_operational = False
    baseline = include_operational and not state_file.exists()
    observed = set()
    calls = 0

    for area in areas:
        location = AREA_QUERY.get(area)
        if not location:
            continue
        for term in terms:
            if calls >= max_queries:
                break
            calls += 1
            payload = {
                "textQuery": f"{term} {location}",
                "includeFutureOpeningBusinesses": True,
                "maxResultCount": 20,
                "languageCode": "ja",
                "regionCode": "JP",
            }
            try:
                data = _post(api_key, payload)
            except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
                continue
            for place in data.get("places", []):
                status = place.get("businessStatus")
                if status not in ("FUTURE_OPENING", "OPERATIONAL"):
                    continue
                place_id = place.get("id", "")
                if not place_id:
                    continue
                if status == "OPERATIONAL":
                    observed.add(place_id)
                    if not include_operational or baseline or place_id in previous:
                        continue
                if place_id in seen or len(found) >= max_candidates:
                    continue
                name = ((place.get("displayName") or {}).get("text") or "").strip()
                if not name:
                    continue
                seen.add(place_id)
                primary_type = place.get("primaryType", "")
                cat = "g" if primary_type in GOURMET_TYPES or "restaurant" in primary_type else "b"
                opening_date = _opening_date(place.get("openingDate")) if status == "FUTURE_OPENING" else ""
                is_future = status == "FUTURE_OPENING"
                lead_url = f"https://www.google.com/maps/place/?q=place_id:{place_id}"
                found.append({
                    "id": f"places-{place_id}",
                    "articleType": "news",
                    "query": f"{name} {area} 開店",
                    "titleIdea": (f"{name} - {area}でオープン予定" if is_future
                                  else f"{name} - {area}の新規発見店舗（開業日未確認）"),
                    "area": area,
                    "cat": cat,
                    "subject": name,
                    "sourceUrl": "",
                    "searchIntent": "新店の開店日・場所・特徴を知る",
                    "leadUrl": lead_url,
                    "leadSourceType": ("google_places_future_opening" if is_future
                                       else "google_places_newly_observed"),
                    "openingDate": opening_date,
                    "addressHint": place.get("formattedAddress", ""),
                    "placeId": place_id,
                })
        if calls >= max_queries:
            break
    if include_operational:
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(
                json.dumps({"place_ids": sorted(previous | observed)}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    return found
