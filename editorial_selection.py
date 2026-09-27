"""Daily editorial planning; observed metrics are bounded, biased signals."""
from collections import Counter
from math import log1p
import os

WEIGHTS = {"demand": 25, "timing": 20, "usefulness": 20,
           "originality": 15, "observed": 10, "exploration": 10}


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
    parts = {}
    for key in ("demand", "timing", "usefulness", "originality"):
        value = judgment.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= WEIGHTS[key]:
            raise ValueError(f"invalid editorial score: {key}")
        parts[key] = value
    parts["observed"] = observed_points(candidate, signal, today)
    parts["exploration"] = exploration_points(candidate, existing, today)
    return {"parts": parts, "total": round(sum(parts.values()), 2)}


def plan(candidates, judgments, signal, existing, today, target=5):
    """Rank 3+2 in October, 4+1 in November; retain backups for gate failures."""
    exploration_slots = 2 if period(today) == "broad_exploration" else 1 if period(today) == "light_optimization" else 0
    annotated = [(c, score(c, judgments.get(c["id"], {}), signal, existing, today)) for c in candidates]
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
    """Compatibility for the previous candidate-first test harness."""
    ranked = plan([dict(c, id=str(i)) for i, c in enumerate(drafts)],
                  {}, signal, existing, today, target)
    return [drafts[int(c["id"])] for c, _ in ranked[:target]]
