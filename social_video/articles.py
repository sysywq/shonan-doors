# -*- coding: utf-8 -*-
"""公開済み記事の読み込み(article metadata loader)。

「公開済み」の判定:
  1. data/articles.json(main)に存在する(保留記事・未承認記事はここに入らない)
  2. mergedInto が無い(統合済み記事はURLだけ残る転送ページなので対象外)
  3. date が今日(JST)以前
  4. 終了済みイベント(eventEndDate < 今日)は既定で対象外(config.manifest.skip_ended_events)
  5. 本番URLが HTTP 200 を返す(check_live。workflowでは既定で確認する)

カテゴリー・エリアの対応表は build.py / post_to_x.py と値を揃えている
(このパッケージは build.py を import せず単体で動かす)。
"""
import json
import os
import urllib.parse
from datetime import datetime, timedelta, timezone

from .config import ROOT

ARTICLES_JSON_PATH = os.path.join(ROOT, "data", "articles.json")
SITE_DOMAIN = "https://www.shonandoors.com"
JST = timezone(timedelta(hours=9))

CAT_EN = {"t": "tourism", "b": "business", "g": "gourmet", "p": "people", "c": "culture", "e": "event", "l": "life"}
CAT_LABEL = {"t": "観光", "b": "企業・店舗", "g": "グルメ", "p": "人", "c": "文化", "e": "イベント", "l": "暮らし"}
CAT_HASHTAG = {"t": "湘南観光", "b": "湘南企業店舗", "g": "湘南グルメ", "p": "湘南の人", "c": "湘南文化",
               "e": "湘南イベント", "l": "湘南暮らし"}


class ArticleNotPublished(Exception):
    """対象記事が公開済みではない(または動画化の対象外)。"""


def today_jst():
    return datetime.now(JST).date().isoformat()


def load_articles(path=ARTICLES_JSON_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def article_url(article):
    return f"{SITE_DOMAIN}/articles/{article['slug']}/"


def to_absolute(url):
    if not url:
        return ""
    return urllib.parse.urljoin(SITE_DOMAIN + "/", url)


def source_image_urls(article):
    """動画素材の画像URL(公開URL)。記事固有の写真 → カテゴリ写真 → OGP の順。
    記事に掲載済みの画像だけを使う(新たな第三者画像は持ち込まない)。"""
    urls = []
    if article.get("heroImage"):
        urls.append(to_absolute(article["heroImage"]))
    for extra in article.get("images") or []:
        if isinstance(extra, str):
            urls.append(to_absolute(extra))
    cat_en = CAT_EN.get(article.get("cat", ""))
    if cat_en:
        urls.append(f"{SITE_DOMAIN}/assets/images/category-photos/{cat_en}.webp")
    if article.get("scene"):
        urls.append(f"{SITE_DOMAIN}/assets/ogp/{article['scene']}.png")
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def local_path_for_url(url):
    """サイト内画像の公開URLをリポジトリ内のファイルパスに変換する(無ければ None)。"""
    if not url.startswith(SITE_DOMAIN + "/"):
        return None
    rel = urllib.parse.unquote(url[len(SITE_DOMAIN) + 1:])
    path = os.path.normpath(os.path.join(ROOT, rel))
    if not path.startswith(ROOT + os.sep) or not os.path.isfile(path):
        return None
    return path


def find_article(articles, article_id):
    for a in articles:
        if str(a.get("id")) == str(article_id):
            return a
    return None


def check_published(article, *, today=None, skip_ended_events=True):
    """公開済み・動画化対象かを確認する。対象外なら ArticleNotPublished。"""
    today = today or today_jst()
    if article.get("mergedInto"):
        raise ArticleNotPublished(f"記事{article['id']}は統合済み(mergedInto={article['mergedInto']})です")
    if str(article.get("date", "")) > today:
        raise ArticleNotPublished(f"記事{article['id']}の公開日({article.get('date')})が未来です")
    end = article.get("eventEndDate") or ""
    if skip_ended_events and end and end < today:
        raise ArticleNotPublished(f"記事{article['id']}のイベントは終了しています(eventEndDate={end})")


def check_live(article, http, timeout=15):
    """本番URLが HTTP 200 で取得できるか。"""
    from .http import HttpError, HttpTimeout
    url = article_url(article)
    try:
        resp = http.request("GET", url, timeout=timeout)
    except (HttpError, HttpTimeout) as e:
        raise ArticleNotPublished(f"記事{article['id']}の本番URLを取得できません: {e}") from None
    if resp.status != 200:
        raise ArticleNotPublished(f"記事{article['id']}の本番URLが HTTP {resp.status} です")


def load_published_article(article_id, *, articles=None, today=None, skip_ended_events=True):
    articles = articles if articles is not None else load_articles()
    a = find_article(articles, article_id)
    if a is None:
        raise ArticleNotPublished(f"記事ID {article_id} は data/articles.json にありません(未公開・保留の可能性)")
    check_published(a, today=today, skip_ended_events=skip_ended_events)
    return a


def latest_published_ids(n, *, articles=None, today=None, skip_ended_events=True):
    """公開日の新しい順に、動画化対象の記事IDを最大n件返す(schedule連携用)。"""
    articles = articles if articles is not None else load_articles()
    out = []
    for a in sorted(articles, key=lambda x: (str(x.get("date", "")), int(x.get("id", 0))), reverse=True):
        try:
            check_published(a, today=today, skip_ended_events=skip_ended_events)
        except ArticleNotPublished:
            continue
        out.append(a["id"])
        if len(out) >= n:
            break
    return out
