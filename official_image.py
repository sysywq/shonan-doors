#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
湘南Doors 公式画像のオーナー目視確認(読取り補助)
------------------------------------------------------------------------
公式ページに掲載された画像/フライヤー内の文字は一次情報として扱う(fact_audit の basis=official_image)。
AIが画像内の文字を十分な確度で読み取れない場合(imageReading=ambiguous)だけ、オーナーに目視確認を求める。

オーナーが画像を見て確認した結果は data/image_confirmations.json に記録し(resume_image_check.py)、
以後の監査で同じ画像の読取り補助として使う。
  - owner_readings_for  … 記事に関係する確認結果(承認済み)を取り出し、監査のプロンプトに渡す
  - apply_owner_reading … 読み取りが曖昧だった claim に、同じ画像の確認結果を当てはめる
                          (記事の値と一致 → confirmed / 違う → contradicted。値が一意に決まるので自動修正の対象)
  - fetch_claim_image   … verify モードで、claim の根拠になった公式画像を取り直す
"""
import base64
import json
import os
import re

import generate_articles as g

CONFIRMATIONS_PATH = os.path.join(g.ROOT, "data", "image_confirmations.json")


def load_confirmations(path=CONFIRMATIONS_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def owner_readings_for(entry, confirmations=None):
    """記事に関係するオーナーの目視確認(承認済み)を返す。記事の link / sources に掲載ページが含まれるもの。"""
    confirmations = load_confirmations() if confirmations is None else confirmations
    urls = {entry.get("link") or ""} | set(entry.get("sources") or [])
    urls.discard("")
    return [
        {k: c.get(k, "") for k in ("imageUrl", "pageUrl", "item", "value")}
        for c in confirmations
        if c.get("decision") == "approve" and (c.get("pageUrl") in urls or c.get("imageUrl") in urls)
    ]


def _norm(s):
    return re.sub(r"[\s　]", "", s or "")


def _same_image(c, r):
    if c.get("imageUrl") and r.get("imageUrl"):
        return c["imageUrl"] == r["imageUrl"]
    return bool(r.get("pageUrl")) and r["pageUrl"] == c.get("primaryUrl")


def apply_owner_reading(c, readings):
    """読み取りが曖昧だった claim(imageReading=ambiguous)に、同じ画像のオーナー確認を当てはめる。
    - オーナーの値が記事の値と一致(包含) → confirmed(basis=official_image, imageReading=clear)
    - 食い違う → contradicted(primaryValue=オーナーの値)。一意に決まるため自動修正の対象になる
    どの確認を使うか一意に決まらなければ何もしない。"""
    if c.get("imageReading") != "ambiguous" or not readings:
        return c
    hits = [r for r in readings if _same_image(c, r)]
    if len(hits) > 1 and c.get("claim"):
        hits = [r for r in hits if r.get("item") and (_norm(r["item"]) in _norm(c["claim"])
                                                      or _norm(c["claim"]) in _norm(r["item"]))]
    if len(hits) != 1 or not hits[0].get("value"):
        return c
    value, av = hits[0]["value"], c.get("articleValue", "")
    tag = f" [オーナーが公式画像を目視で確認: 「{value}」]"
    base = dict(c, basis="official_image", imageReading="clear", primaryValue=value,
                note=(c.get("note", "") + tag).strip())
    if av and (_norm(value) in _norm(av) or _norm(av) in _norm(value)):
        return dict(base, status="confirmed", logicallyCompatible=True)
    return dict(base, status="contradicted", logicallyCompatible=False)


def fetch_claim_image(claim, getter=None):
    """claim の根拠になった公式画像を取り直し、API に渡す画像ブロックを返す。扱えなければ None。
    掲載ページ(primaryUrl)が他メディアではなく、そのページのHTMLに画像が載っている場合に限る
    (第三者の転載・切り抜き画像を根拠にしない)。"""
    import fact_audit as fa  # fact_audit がこのモジュールを import するため遅延 import

    getter = getter or fa._http_get
    page, img = claim.get("primaryUrl") or "", claim.get("imageUrl") or ""
    if not (page.startswith(("http://", "https://")) and img.startswith(("http://", "https://"))):
        return None
    if g.is_secondary_media(page) or g.is_secondary_media(img):
        return None
    try:
        raw, _ctype, charset = getter(page, 2_000_000)
        if img not in fa._image_candidates(raw.decode(charset or "utf-8", "ignore"), page):
            return None
        data, ctype, _ = getter(img, fa.IMAGE_MAX_BYTES)
    except Exception:
        return None
    if ctype not in fa.IMAGE_MEDIA_TYPES or not data or len(data) > fa.IMAGE_MAX_BYTES:
        return None
    return {"type": "image", "source": {"type": "base64", "media_type": ctype,
                                        "data": base64.b64encode(data).decode("ascii")}}
