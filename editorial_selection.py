"""Daily editorial planning; observed metrics are bounded, biased signals."""
from collections import Counter
from math import isfinite, log1p
import os
import re

WEIGHTS = {"demand": 25, "timing": 20, "usefulness": 20,
           "originality": 15, "observed": 10, "exploration": 10}
JUDGED_KEYS = ("demand", "timing", "usefulness", "originality")
NUMERIC_TEXT = re.compile(r"[+-]?\d+(?:\.\d+)?")


def validate_judgment(raw):
    """AIの採点1件を検証する。戻り値 (judgment or None, problems, notes)。
    欠落・null・範囲外・数値でない値は 0 に変換したり丸めたりせず、その候補を不正として扱う
    (problems に理由)。"18" のような文字列数値だけは数値として読み、notes に残す。"""
    if not isinstance(raw, dict):
        return None, [f"採点がオブジェクトではない(型={type(raw).__name__})"], []
    clean, problems, notes = {}, [], []
    for key in JUDGED_KEYS:
        value = raw.get(key)
        if key not in raw or value is None:
            problems.append(f"{key}が欠落/null")
            continue
        if isinstance(value, str) and NUMERIC_TEXT.fullmatch(value.strip()):
            notes.append(f"{key}が文字列数値({value!r})のため数値として読んだ")
            value = float(value.strip())
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            problems.append(f"{key}が数値ではない({str(value)[:40]!r})")
            continue
        if not 0 <= value <= WEIGHTS[key]:
            problems.append(f"{key}={value} が範囲外(0〜{WEIGHTS[key]})")
            continue
        clean[key] = int(value) if float(value).is_integer() else value
    return (None if problems else clean), problems, notes


def period(today):
    if today <= "2026-10-31":
        return "broad_exploration"
    if today <= "2026-11-30":
        return "light_optimization"
    # Do not automatically increase reliance on biased early metrics on Dec 1.
    # A November review must explicitly activate the revised weighting.
    return "post_review" if os.environ.get("EDITORIAL_NOV_REVIEW_APPROVED") == "1" else "light_optimization"


def observed_points(candidate, signal, today):
    segments = signal.get("editorial_segments", []) if isinstance(signal, dict) else []
    row = next((m for m in segments if m.get("area") == candidate.get("area")
                and m.get("cat") == candidate.get("cat")), {})
    impressions = min(300, max(0, float(row.get("gsc_impressions", 0) or 0)))
    clicks = min(30, max(0, float(row.get("gsc_clicks", 0) or 0)))
    views = min(300, max(0, float(row.get("ga4_views", 0) or 0)))
    engaged = min(60, max(0, float(row.get("ga4_engaged_sessions", 0) or 0)))
    confidence = min(1, (impressions + views) / 150)
    raw = (2 * log1p(impressions) / log1p(300) + 3 * log1p(clicks) / log1p(30)
           + 2 * log1p(views) / log1p(300) + 3 * log1p(engaged) / log1p(60))
    cap = 4 if period(today) == "broad_exploration" else 6 if period(today) == "light_optimization" else 10
    return round(min(cap, confidence * raw), 2)


def exploration_points(candidate, existing, today):
    if period(today) == "post_review":
        return 0
    recent = [a for a in existing if a.get("date", "") >= "2026-09-01"]
    areas = Counter(a.get("area") for a in recent)
    cats = Counter(a.get("cat") for a in recent)
    types = Counter(a.get("articleType") for a in recent)
    value = 4 / (1 + areas[candidate.get("area")]) + 4 / (1 + cats[candidate.get("cat")])
    value += 2 / (1 + types[candidate.get("articleType")])
    return round(value, 2)


def score(candidate, judgment, signal, existing, today):
    clean, problems, _notes = validate_judgment(judgment)
    if problems:
        raise ValueError(f"invalid editorial score for {candidate.get('id')}: {' / '.join(problems)}")
    parts = dict(clean)
    parts["observed"] = observed_points(candidate, signal, today)
    parts["exploration"] = exploration_points(candidate, existing, today)
    return {"parts": parts, "total": round(sum(parts.values()), 2)}


def plan(candidates, judgments, signal, existing, today, target=5):
    """Rank 3+2 in October, 4+1 in November; retain backups for gate failures."""
    exploration_slots = 2 if period(today) == "broad_exploration" else 1 if period(today) == "light_optimization" else 0
    # 採点が無い候補は 0点として混ぜず、順位付けから外す(呼び出し元が除外理由をログに残す)
    annotated = [(c, score(c, judgments[c["id"]], signal, existing, today))
                 for c in candidates if c["id"] in judgments]
    regular = sorted(annotated, key=lambda x: (-x[1]["total"], x[0]["id"]))
    chosen, seen = [], set()

    def take(ranked, count, slot):
        for candidate, result in ranked:
            if count <= 0:
                break
            if candidate["id"] in seen:
                continue
            if sum(c["area"] == candidate["area"] and c["cat"] == candidate["cat"] for c, _ in chosen) >= 2:
                continue
            chosen.append((candidate, dict(result, slot=slot)))
            seen.add(candidate["id"])
            count -= 1

    take(regular, target - exploration_slots, "regular")
    exploratory = sorted(annotated, key=lambda x: (-x[1]["parts"]["exploration"], -x[1]["total"], x[0]["id"]))
    take(exploratory, exploration_slots, "exploration")
    take(regular, target - len(chosen), "regular")
    return chosen + [(c, dict(s, slot="backup")) for c, s in regular if c["id"] not in seen]


def select(drafts, existing, signal, target, today):
    """Compatibility for the previous candidate-first test harness.
    AIの採点を使わない経路なので、4項目は明示的に同点(0)にして観測・探索だけで並べる。"""
    neutral = {k: 0 for k in JUDGED_KEYS}
    ranked = plan([dict(c, id=str(i)) for i, c in enumerate(drafts)],
                  {str(i): neutral for i in range(len(drafts))}, signal, existing, today, target)
    return [drafts[int(c["id"])] for c, _ in ranked[:target]]
