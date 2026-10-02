#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公開対象の記事に、利用条件を確認済みの一次情報画像を付与する。

Phase 1 は PR TIMES の本人/企業発信プレスリリースだけを対象にする。
「Webで見つかる画像」一般は転載しない。取得・検証に失敗した記事は変更せず、
build.py のカテゴリ別画像へ安全にフォールバックする。
"""
import html
import json
import os
import re
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
ARTICLES_PATH = os.path.join(ROOT, "data", "articles.json")
RUN_REPORT_PATH = os.environ.get(
    "SHONAN_DOORS_RUN_REPORT_PATH", "/tmp/shonan_doors_run_report.json")
IMAGE_DIR = os.path.join(ROOT, "assets", "images", "articles")
MAX_IMAGE_BYTES = 8 * 1024 * 1024
USER_AGENT = "ShonanDoors/1.0 (+https://www.shonandoors.com/)"

# 自動転載を許可する配信元は、利用条件を確認したものだけ明示的に追加する。
# Phase 1 では PR TIMES のみ。公式サイト一般・SNS・他媒体は対象外。
ALLOWED_SOURCE_HOSTS = {"prtimes.jp"}
IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _host(url):
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def allowed_source(url):
    host = _host(url)
    return any(host == h or host.endswith("." + h) for h in ALLOWED_SOURCE_HOSTS)


def _get(url, max_bytes):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("response too large")
        return data, (resp.headers.get_content_type() or "").lower(), resp.geturl()


def extract_og_image(page_html, base_url):
    """PR本文ページの og:image を返す。"""
    patterns = [
        r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::secure_url)?["\']',
    ]
    for pat in patterns:
        m = re.search(pat, page_html, re.I)
        if m:
            return urllib.parse.urljoin(base_url, html.unescape(m.group(1)).strip())
    return ""


def candidate_source(article):
    """記事が実際に根拠として使ったPR TIMES URLだけを候補にする。"""
    urls = list(article.get("sources") or [])
    if article.get("link"):
        urls.append(article["link"])
    for url in urls:
        if isinstance(url, str) and allowed_source(url):
            return url
    return ""


def enrich_article(article, getter=_get):
    """成功時のみ heroImage/credit を設定。失敗時は元記事をそのまま返す。"""
    if article.get("heroImage"):
        return article, False
    source = candidate_source(article)
    if not source:
        return article, False
    try:
        raw, ctype, final_page = getter(source, 2 * 1024 * 1024)
        if ctype not in ("text/html", "application/xhtml+xml"):
            return article, False
        if not allowed_source(final_page):
            return article, False
        page_html = raw.decode("utf-8", "ignore")
        image_url = extract_og_image(page_html, final_page)
        if not image_url:
            return article, False
        image, image_type, _final_image = getter(image_url, MAX_IMAGE_BYTES)
        ext = IMAGE_TYPES.get(image_type)
        if not ext or not image:
            return article, False
        os.makedirs(IMAGE_DIR, exist_ok=True)
        article_id = article.get("id")
        if article_id is None:
            return article, False
        rel = f"/assets/images/articles/{article_id}{ext}"
        dest = os.path.join(IMAGE_DIR, f"{article_id}{ext}")
        with open(dest, "wb") as f:
            f.write(image)
        updated = dict(article)
        updated.update({
            "heroImage": rel,
            "heroImageSourceUrl": source,
            "heroImageCredit": "画像出典：PR TIMES",
            "heroImageOriginUrl": image_url,
        })
        return updated, True
    except Exception as exc:
        print(f"::warning::記事 {article.get('id')} の公式画像取得をスキップ: {exc}")
        return article, False


def load_target_ids():
    try:
        with open(RUN_REPORT_PATH, encoding="utf-8") as f:
            report = json.load(f)
        return {i for i in report.get("accepted_ids") or []}
    except (OSError, ValueError, TypeError):
        return set()


def main():
    target_ids = load_target_ids()
    if not target_ids:
        print("画像付与対象の記事がありません。")
        return 0
    with open(ARTICLES_PATH, encoding="utf-8") as f:
        articles = json.load(f)
    changed = 0
    out = []
    for article in articles:
        if article.get("id") in target_ids:
            article, did_change = enrich_article(article)
            changed += int(did_change)
        out.append(article)
    if changed:
        tmp = ARTICLES_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, ARTICLES_PATH)
    print(f"記事固有画像を {changed} 件に付与しました。未取得記事はカテゴリ画像へフォールバックします。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
