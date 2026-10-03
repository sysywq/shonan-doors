# -*- coding: utf-8 -*-
"""article_to_video_manifest: 公開済み記事 → short動画manifest(JSON)。

manifest は「何を見せるか」だけを持ち、「どう見せるか」(字幕デザイン・BGM・音声・
テンプレート・動画生成AI)は持たない。renderer はこの manifest だけを入力にするので、
動画テイストが決まったら renderer だけを差し替えればよい。

テキストは記事本文・dek・タイトルからの抜粋のみで作る(AIで新しい事実を書き足さない)。
記事は公開前に Fact Audit を通過しているため、抜粋の範囲なら一次情報との整合が保たれる。
"""
import hashlib
import json
import re
from datetime import datetime, timezone

from . import articles as art

SCHEMA_VERSION = 1


def _truncate(text, limit):
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip("、。 ") + "…"


def _first_sentence(text):
    text = (text or "").strip()
    m = re.search(r"^(.+?[。！？!?])", text)
    return m.group(1) if m else text


def _hashtag(word):
    w = re.sub(r"[\s#・、。,.!！?？「」『』()（）\[\]【】:：/／&＆'\"-]", "", str(word or ""))
    return w


def build_hashtags(article, cfg_manifest):
    words = list(cfg_manifest.get("base_hashtags") or [])
    if article.get("area"):
        words.append(article["area"])
    if article.get("cat") in art.CAT_HASHTAG:
        words.append(art.CAT_HASHTAG[article["cat"]])
    words.extend(article.get("tags") or [])
    seen, out = set(), []
    for w in words:
        h = _hashtag(w)
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return out[: int(cfg_manifest.get("max_hashtags", 8))]


def _event_period(article):
    s, e = article.get("eventStartDate") or "", article.get("eventEndDate") or ""

    def fmt(d):
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            return f"{dt.month}/{dt.day}"
        except ValueError:
            return ""

    if s and e and s != e:
        return f"{fmt(s)}〜{fmt(e)}"
    if s or e:
        return fmt(s or e)
    return ""


def build_scenes(article, cfg_manifest, images):
    """シーン列。type はrendererが見た目を変えるためのヒント(title / point / info / cta)。"""
    limit = int(cfg_manifest.get("scene_text_max_chars", 40))
    scenes = [{"type": "title", "text": _truncate(article["title"], limit * 2)}]
    paragraphs = [p for p in (article.get("body") or "").split("\n") if p.strip()]
    for p in paragraphs[: int(cfg_manifest.get("max_body_scenes", 3))]:
        scenes.append({"type": "point", "text": _truncate(_first_sentence(p), limit)})
    # 字幕に焼き込む可能性があるため、日本語フォントに無い絵文字は使わない
    info = [article["area"]] if article.get("area") else []
    period = _event_period(article)
    if period:
        info.append(period)
    if info:
        scenes.append({"type": "info", "text": " | ".join(info)})
    scenes.append({"type": "cta", "text": cfg_manifest.get("cta", "")})

    total = float(cfg_manifest["duration_target_sec"])
    base = round(total / len(scenes), 2)
    for i, s in enumerate(scenes):
        s["index"] = i
        s["image_url"] = images[i % len(images)] if images else ""
        s["duration_sec"] = base
    # 端数は最後のシーンで吸収し、合計を duration_target に揃える
    scenes[-1]["duration_sec"] = round(total - base * (len(scenes) - 1), 2)
    return scenes


def build_caption(article, hashtags, cta):
    url = art.article_url(article)
    parts = [article["title"], article.get("dek", "").strip(), f"{cta}\n{url}".strip(),
             " ".join(f"#{h}" for h in hashtags)]
    return "\n\n".join(p for p in parts if p)


def article_to_video_manifest(article, cfg, *, now=None):
    m = cfg["manifest"]
    images = art.source_image_urls(article)
    hashtags = build_hashtags(article, m)
    cta = m.get("cta", "")
    generated_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema_version": SCHEMA_VERSION,
        "article_id": article["id"],
        "article_url": art.article_url(article),
        "slug": article["slug"],
        "area": article.get("area", ""),
        "category": art.CAT_LABEL.get(article.get("cat", ""), article.get("cat", "")),
        "title": article["title"],
        "dek": article.get("dek", ""),
        "hook": _truncate(_first_sentence(article.get("dek") or article["title"]), int(m.get("hook_max_chars", 40))),
        "scenes": build_scenes(article, m, images),
        "source_image_urls": images,
        "cta": cta,
        "caption": build_caption(article, hashtags, cta),
        "hashtags": hashtags,
        "duration_target_sec": m["duration_target_sec"],
        "generated_at": generated_at,
    }


def video_asset_id(manifest, cfg):
    """同じ記事・同じ内容・同じ renderer/出力設定なら同じIDになる(generated_at は含めない)。"""
    content = {k: v for k, v in manifest.items() if k != "generated_at"}
    payload = {"manifest": content, "renderer": cfg["renderer"], "output": cfg["output"]}
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return f"sv-{manifest['article_id']}-{digest[:12]}"
