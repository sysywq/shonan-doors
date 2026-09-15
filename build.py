#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
湘南Doors SSGビルドスクリプト (Phase 1)
----------------------------------------
data/articles.json を Single Source of Truth として、

  - /articles/{slug}/index.html   … 記事ごとの独立した静的ページ
  - /index.html                    … トップページ(記事一覧・概要のみ)

を生成する。CSSは /assets/site.css、共通JSは /assets/common.js を
全ページで共有する。

実行方法:
    python3 build.py

このスクリプトは何度実行しても同じ結果になる(冪等)。
articles/ 以下は毎回このスクリプトの出力で上書きされるため、
記事ページを手動で直接編集しないこと(articles.jsonを直すこと)。
"""
import json
import os
import re
import html
import shutil

ROOT = os.path.dirname(os.path.abspath(__file__))
SITE_DOMAIN = "https://www.shonandoors.com"  # CNAMEファイルに準拠(wwwあり)
SITE_TITLE = "湘南Doors｜湘南の人・企業・文化・体験をつなぐメディア"

CATS = {
    "t": {"label": "観光", "bg": "#1D3557"},
    "b": {"label": "企業・店舗", "bg": "#A6431E"},
    "g": {"label": "グルメ", "bg": "#8B5E34"},
    "p": {"label": "人", "bg": "#16233A"},
    "c": {"label": "文化", "bg": "#0F2038"},
    "e": {"label": "イベント", "bg": "#E8542B"},
    "l": {"label": "暮らし", "bg": "#33424F"},
}
AREA_ORDER = ["藤沢", "茅ヶ崎", "鎌倉", "平塚", "大磯", "二宮", "逗子", "葉山"]

FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 60 60'%3E"
           "%3Ccircle cx='30' cy='30' r='30' fill='%23F4F5F1'/%3E%3Ccircle cx='30' cy='30' r='27' "
           "fill='none' stroke='%2316233A' stroke-width='2'/%3E%3Cpath d='M14 34 Q22 24 30 34 T46 34' "
           "fill='none' stroke='%231D3557' stroke-width='3'/%3E%3Cpath d='M14 42 Q22 32 30 42 T46 42' "
           "fill='none' stroke='%23E8542B' stroke-width='2.5' opacity='.85'/%3E%3Ccircle cx='30' cy='16' "
           "r='5.5' fill='%23C99A3E'/%3E%3C/svg%3E")

GA4_SNIPPET = """<script async src="https://www.googletagmanager.com/gtag/js?id=G-PB4LBKENHT"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());
  gtag('config', 'G-PB4LBKENHT');
</script>"""

HEAD_COMMON = f"""<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" type="image/svg+xml" href="{FAVICON}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Shippori+Mincho:wght@400;500;600;800&family=Zen+Kaku+Gothic+New:wght@400;500;700;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/assets/site.css">
{GA4_SNIPPET}"""

HEADER_HTML = """<header>
  <svg class="wave-deco" viewBox="0 0 300 200" fill="none">
    <path d="M0 120 Q40 90 80 120 T160 120 T240 120 T320 120" stroke="#1D3557" stroke-width="2" opacity=".3"/>
    <path d="M0 145 Q40 115 80 145 T160 145 T240 145 T320 145" stroke="#E8542B" stroke-width="2" opacity=".25"/>
    <path d="M0 170 Q40 140 80 170 T160 170 T240 170 T320 170" stroke="#1D3557" stroke-width="1.5" opacity=".18"/>
  </svg>
  <div class="head-inner">
    <div class="brand-block">
      <a href="/" style="display:flex; align-items:center; gap:16px; text-decoration:none; color:inherit;">
      <svg class="brand-mark" viewBox="0 0 60 60">
        <circle cx="30" cy="30" r="29" fill="none" stroke="#16233A" stroke-width="1.2"/>
        <path d="M14 34 Q22 24 30 34 T46 34" fill="none" stroke="#1D3557" stroke-width="2"/>
        <path d="M14 41 Q22 31 30 41 T46 41" fill="none" stroke="#E8542B" stroke-width="1.6" opacity=".85"/>
        <circle cx="30" cy="18" r="4.5" fill="#C99A3E"/>
      </svg>
      <div>
        <div class="brand-name serif">湘南Doors</div>
        <div class="brand-tagline">SHONAN DOORS — 湘南と、人をつなぐ地域メディア</div>
      </div>
      </a>
      <div class="sns-row" id="snsRow"></div>
    </div>
  </div>
  <div class="mission-strip">
    <div class="mission-inner">
      <p class="mission-text">
        湘南には、まだ知られていない人がいる。まだ知られていない企業がある。<br>
        「湘南Doors」は、湘南にある魅力を見つけ、伝え、つなぐことに情熱を注ぐ地域メディアです。<br>
        湘南で暮らす人にも、これから関わりたい人にも、<strong>「知ってよかった」と思える出会い</strong>を届けます。
      </p>
    </div>
  </div>
  <div style="max-width:1160px;margin:0 auto;height:22px;"></div>
</header>"""

FOOTER_HTML = """<footer>
  <div class="foot-inner">
    <div class="foot-mission">
      <span class="serif">「湘南に関わるなら、この場所。」</span>
      湘南Doorsは、湘南の企業・お店・人・文化・イベント・観光を継続的に取材し、記録として積み重ねていく独立した地域メディアです。特定のサービスやEC、求人といった事業には属さず、あくまで「湘南を知る入口」であることを目的に運営しています。
    </div>
    <div class="foot-note">
      掲載情報は公開情報をもとに編集部が取材・構成したものです。店舗情報・開催情報は変更となる場合がありますので、最新情報は各施設・団体の公式情報をご確認ください。<br><br>
      広告掲載プランをご用意しています（松：ページ最上部プレミアム枠 ／ 竹：カテゴリ内スポンサー枠 ／ 梅：記事内タイアップ枠）。詳しくはお問い合わせください。<br><br>
      <span class="foot-links">
        <a href="#" id="footPrivacy">プライバシーポリシー</a>
        <a href="#" id="footContact">お問い合わせ</a>
        <a href="#" id="footOperator">運営者情報</a>
      </span><br><br>
      © 湘南Doors運営事務局
    </div>
  </div>
</footer>

<div class="overlay" id="overlay"><div class="modal" id="modal"></div></div>"""


def load_json(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return json.load(f)


def esc(s):
    return html.escape(s or "", quote=True)


def format_date(d):
    y, m, day = d.split("-")
    return f"{y}.{m}.{day}"


# ---------- Phase 2: SEO用メタデータ生成 ----------

def build_seo_title(item):
    """検索結果に表示される<title>を、地域名・カテゴリ・年度等を意識して組み立てる。
    ページ本文のH1(元の編集タイトルそのまま)とはあえて分離している。
    不自然なキーワード詰め込みは避け、要素を足しすぎない。"""
    year = item["date"][:4]
    area = item["area"]
    if item["cat"] == "e":
        # イベントは年度が重要な検索キーワードになるため明示する
        return f"{item['title']}【{area}】{year}年 | 湘南Doors"
    if item["cat"] in ("b", "g"):
        return f"{item['title']}｜{area}の{CATS[item['cat']]['label']} - 湘南Doors"
    return f"{item['title']}｜{area} - 湘南Doors"


def build_meta_description(item):
    """dek(編集部が書いた要約)をベースにmeta descriptionを組み立てる。
    dekは元々100字前後で書かれており、descriptionとして十分な品質があるため
    新規に自動生成し直すのではなく、これをそのまま活かす方針にしている。"""
    desc = item["dek"].strip()
    if item["area"] not in desc:
        desc = f"【{item['area']}】{desc}"
    if len(desc) > 155:
        desc = desc[:154] + "…"
    return desc


def build_ogp_image_url(item):
    return f"{SITE_DOMAIN}/assets/ogp/{item['scene']}.png"


def build_structured_data(item, canonical_url):
    """記事タイプに応じてJSON-LDを出し分ける。
    - cat='e' かつ eventStartDateが確認できている場合 → Event
    - cat in (b, g) かつ住所が確認できている場合       → LocalBusiness / Restaurant
    - それ以外                                          → Article
    確実な値が無いのに無理にEvent/LocalBusinessにすると、Search Console上で
    構造化データエラーとして検出され逆効果になるため、必要な値が欠けている場合は
    安全側(Article)にフォールバックする。"""
    area = item["area"]
    image_url = build_ogp_image_url(item)

    if item["cat"] == "e" and item.get("eventStartDate"):
        data = {
            "@context": "https://schema.org",
            "@type": "Event",
            "name": item["title"],
            "description": item["dek"],
            "startDate": item["eventStartDate"],
            "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
            "eventStatus": "https://schema.org/EventScheduled",
            "location": {
                "@type": "Place",
                "name": f"{area}(神奈川県)",
                "address": item.get("address") or f"神奈川県{area}",
            },
            "image": [image_url],
            "organizer": {"@type": "Organization", "name": "湘南Doors運営事務局", "url": SITE_DOMAIN},
            "url": canonical_url,
        }
        if item.get("eventEndDate"):
            data["endDate"] = item["eventEndDate"]
        return data

    if item["cat"] in ("b", "g") and item.get("address"):
        data = {
            "@context": "https://schema.org",
            "@type": "Restaurant" if item["cat"] == "g" else "LocalBusiness",
            "name": item["title"],
            "description": item["dek"],
            "address": {
                "@type": "PostalAddress",
                "streetAddress": item["address"],
                "addressRegion": "神奈川県",
                "addressCountry": "JP",
            },
            "image": [image_url],
            "url": canonical_url,
        }
        if item.get("hours"):
            data["openingHours"] = item["hours"]
        if item.get("link"):
            data["sameAs"] = [item["link"]]
        return data

    data = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": item["title"],
        "description": item["dek"],
        "datePublished": item["date"],
        "dateModified": item["date"],
        "image": [image_url],
        "author": {"@type": "Organization", "name": "湘南Doors運営事務局"},
        "publisher": {
            "@type": "Organization",
            "name": "湘南Doors",
            "logo": {"@type": "ImageObject", "url": f"{SITE_DOMAIN}/assets/ogp/{item['scene']}.png"},
        },
        "mainEntityOfPage": {"@type": "WebPage", "@id": canonical_url},
    }
    return data


def render_head_seo(item, canonical_url):
    """記事ページ向けの<title>・meta description・OGP・Twitter Card・JSON-LDをまとめて返す。"""
    seo_title = build_seo_title(item)
    description = build_meta_description(item)
    image_url = build_ogp_image_url(item)
    ld = build_structured_data(item, canonical_url)

    return f"""<title>{esc(seo_title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="canonical" href="{canonical_url}">
<meta property="og:type" content="article">
<meta property="og:site_name" content="湘南Doors">
<meta property="og:title" content="{esc(seo_title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{canonical_url}">
<meta property="og:image" content="{image_url}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:locale" content="ja_JP">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{esc(seo_title)}">
<meta name="twitter:description" content="{esc(description)}">
<meta name="twitter:image" content="{image_url}">
<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>"""


def render_infobox(item):
    rows = []
    if item.get("address"):
        rows.append(f'<div class="modal-info-row"><span class="modal-info-label">住所</span><span>{esc(item["address"])}</span></div>')
    if item.get("access"):
        rows.append(f'<div class="modal-info-row"><span class="modal-info-label">アクセス</span><span>{esc(item["access"])}</span></div>')
    if item.get("hours"):
        rows.append(f'<div class="modal-info-row"><span class="modal-info-label">営業時間</span><span>{esc(item["hours"])}</span></div>')
    if item.get("closedDays"):
        rows.append(f'<div class="modal-info-row"><span class="modal-info-label">定休日</span><span>{esc(item["closedDays"])}</span></div>')

    sns = item.get("snsLinks") or {}
    sns_chips = []
    if sns.get("instagram"):
        sns_chips.append(f'<a class="modal-sns-chip" style="background:linear-gradient(45deg,#FEDA75,#FA7E1E,#D62976,#962FBF,#4F5BD5)" href="{esc(sns["instagram"])}" target="_blank" rel="noopener">Instagram</a>')
    if sns.get("facebook"):
        sns_chips.append(f'<a class="modal-sns-chip" style="background:#1877F2" href="{esc(sns["facebook"])}" target="_blank" rel="noopener">Facebook</a>')
    if sns.get("x"):
        sns_chips.append(f'<a class="modal-sns-chip" style="background:#000" href="{esc(sns["x"])}" target="_blank" rel="noopener">X</a>')
    if sns.get("tiktok"):
        sns_chips.append(f'<a class="modal-sns-chip" style="background:#000" href="{esc(sns["tiktok"])}" target="_blank" rel="noopener">TikTok</a>')
    if sns_chips:
        rows.append(f'<div class="modal-info-row"><span class="modal-info-label">SNS</span><span class="modal-sns-row">{"".join(sns_chips)}</span></div>')

    if not rows:
        return ""
    return f'<div class="modal-infobox">{"".join(rows)}</div>'


def render_article_main(item, scenes):
    tags_html = "".join(f'<span class="tag-pill">#{esc(t)}</span>' for t in item.get("tags", []))
    infobox = render_infobox(item)
    map_html = ""
    if item.get("address"):
        from urllib.parse import quote
        map_html = (f'<iframe class="modal-map" loading="lazy" referrerpolicy="no-referrer-when-downgrade" '
                    f'src="https://www.google.com/maps?q={quote(item["address"])}&output=embed"></iframe>')
    link_html = ""
    if item.get("link"):
        link_html = f'<a class="modal-linkbtn" href="{esc(item["link"])}" target="_blank" rel="noopener">さらに詳しい情報を見る ↗</a>'
    estate_html = ""
    if item.get("estateLink"):
        estate_html = (f'<div class="related-estate"><div class="related-estate-label">{esc(item["area"])}の物件をさがす</div>'
                        f'<a class="related-estate-btn" href="{esc(item["estateLink"])}" target="_blank" rel="noopener">実際の物件を見る ↗</a></div>')

    # 本文は改行(\n\n)で段落分けされたプレーンテキスト。既存の.modal-text(white-space:pre-line)をそのまま利用する。
    body_html = esc(item["body"])

    return f"""<main>
  <div class="article-page-wrap">
    <div class="breadcrumb"><a href="/">湘南Doors トップ</a> ／ {esc(CATS[item["cat"]]["label"])} ／ {esc(item["area"])}</div>
    <div class="modal article-modal-static">
      <div class="modal-art">{scenes[item["scene"]]}</div>
      <div class="modal-body">
        <div class="modal-eyebrow">{esc(CATS[item["cat"]]["label"])}<span style="color:var(--ink-faint); font-weight:400;">／ {esc(item["area"])}</span><span style="color:var(--ink-faint); font-weight:400; margin-left:auto;">{format_date(item["date"])}</span></div>
        <h1 class="modal-title serif">{esc(item["title"])}</h1>
        <p class="modal-dek">{esc(item["dek"])}</p>
        <div class="modal-text">{body_html}</div>
        {infobox}
        {map_html}
        {link_html}
        <div class="modal-tags">{tags_html}</div>
        {estate_html}
      </div>
    </div>
    <a class="back-to-top" href="/">← 湘南Doors トップへ戻る</a>
  </div>
</main>"""


def render_article_page(item, scenes):
    canonical = f"{SITE_DOMAIN}/articles/{item['slug']}/"
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
{render_head_seo(item, canonical)}
{HEAD_COMMON}
</head>
<body>
{HEADER_HTML}
{render_article_main(item, scenes)}
{FOOTER_HTML}
<script src="/assets/common.js" defer></script>
</body>
</html>
"""


def render_card(item, scenes):
    # data-search はトップページ上の絞り込み用(タイトル・概要・タグのみ。本文全文はトップページに置かない)
    search_hay = esc((item["title"] + item["dek"] + "".join(item.get("tags", []))).lower())
    return f"""<a class="card" href="/articles/{item['slug']}/" data-cat="{item['cat']}" data-area="{esc(item['area'])}" data-search="{search_hay}">
        <div class="card-art" style="position:relative;">
          {scenes[item['scene']]}
          <div class="card-eyebrow">{esc(CATS[item['cat']]['label'])}<span class="dot"></span>{esc(item['area'])}</div>
        </div>
        <div class="card-body">
          <div class="card-date">{format_date(item['date'])}</div>
          <div class="card-title serif">{esc(item['title'])}</div>
          <div class="card-dek">{esc(item['dek'])}</div>
          <div class="card-foot">
            <span class="read-more">続きを読む →</span>
            <span class="area-tag">{esc(item['area'])}</span>
          </div>
        </div>
      </a>"""


def render_index(articles, scenes):
    articles_sorted = sorted(articles, key=lambda d: d["date"], reverse=True)
    cards_html = "\n      ".join(render_card(a, scenes) for a in articles_sorted)

    chips_html = '<div class="chip active" data-cat="all">すべて</div>' + "".join(
        f'<div class="chip" data-cat="{k}">{v["label"]}</div>' for k, v in CATS.items()
    )
    used_areas = {a["area"] for a in articles}
    area_opts = "".join(f'<option value="{a}">{a}</option>' for a in AREA_ORDER if a in used_areas)

    # 旧hash URL(#article-{id}) → 新URL への転送用マップ(フォールバック用)
    id_to_slug = {a["id"]: a["slug"] for a in articles}
    redirect_map_json = json.dumps(id_to_slug, ensure_ascii=False)

    insta_tile_ids = [8, 1, 23, 56, 19, 39]
    insta_map = {a["id"]: a for a in articles}
    insta_items = [insta_map[i] for i in insta_tile_ids if i in insta_map]
    insta_html = "".join(
        f'<a class="insta-grid-item" href="/articles/{a["slug"]}/">{scenes[a["scene"]]}</a>' for a in insta_items
    )

    featured_ids = [1, 8, 19, 23, 56]
    pickup_items = [
        {"id": a["id"], "slug": a["slug"], "cat": a["cat"], "area": a["area"],
         "title": a["title"], "dek": a["dek"], "scene": a["scene"]}
        for a in (insta_map.get(i) for i in featured_ids) if a
    ]
    pickup_items_json = json.dumps(pickup_items, ensure_ascii=False)
    cats_label_bg_json = json.dumps({k: {"label": v["label"], "bg": v["bg"]} for k, v in CATS.items()}, ensure_ascii=False)
    scenes_json = json.dumps(scenes, ensure_ascii=False)

    site_description = "湘南(藤沢・茅ヶ崎・鎌倉・平塚・大磯・二宮・逗子・葉山)の企業・お店・人・文化・イベント・観光情報を継続的に取材する地域メディア。"
    website_ld = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": "湘南Doors",
        "url": f"{SITE_DOMAIN}/",
        "description": site_description,
        "publisher": {"@type": "Organization", "name": "湘南Doors運営事務局", "url": f"{SITE_DOMAIN}/"},
    }

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<title>{esc(SITE_TITLE)}</title>
<meta name="description" content="{esc(site_description)}">
<link rel="canonical" href="{SITE_DOMAIN}/">
<meta property="og:type" content="website">
<meta property="og:site_name" content="湘南Doors">
<meta property="og:title" content="{esc(SITE_TITLE)}">
<meta property="og:description" content="{esc(site_description)}">
<meta property="og:url" content="{SITE_DOMAIN}/">
<meta property="og:image" content="{SITE_DOMAIN}/assets/ogp/beach.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:locale" content="ja_JP">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{esc(SITE_TITLE)}">
<meta name="twitter:description" content="{esc(site_description)}">
<meta name="twitter:image" content="{SITE_DOMAIN}/assets/ogp/beach.png">
<script type="application/ld+json">{json.dumps(website_ld, ensure_ascii=False)}</script>
{HEAD_COMMON}
</head>
<body>
{HEADER_HTML}

<!-- top banner slot (松プラン想定・回転バナー) : 実際に広告主がつく際は、ステマ規制対応として小さく「PR」等の表示を追加してください -->
<div class="pickup-section">
  <div class="pickup-viewport" id="pickupViewport">
    <div class="pickup-track" id="pickupTrack"></div>
  </div>
  <div class="pickup-nav">
    <button class="pickup-arrow" id="pickupPrev" aria-label="前へ">‹</button>
    <div class="pickup-dots" id="pickupDots"></div>
    <button class="pickup-arrow" id="pickupNext" aria-label="次へ">›</button>
  </div>
</div>

<div class="filterbar">
  <div class="filter-inner">
    <div class="chip-row" id="catChips">{chips_html}</div>
    <div class="filter-right">
      <select id="areaSelect"><option value="">エリア：すべて</option>{area_opts}</select>
      <input type="text" id="searchBox" placeholder="キーワードで探す">
    </div>
  </div>
</div>

<main>
  <div class="content-layout">
    <div class="content-main">
      <p id="resultCount">全 {len(articles)} 件を掲載中</p>
      <div class="grid" id="grid">
      {cards_html}
      </div>
    </div>
    <aside class="content-sidebar">
      <div class="sidebar-card">
        <div class="sidebar-card-title">Instagramの最新投稿</div>
        <div class="insta-grid" id="instaGrid">{insta_html}</div>
        <a class="insta-follow-btn" href="#" target="_blank" rel="noopener">📷 Instagramでフォロー</a>
      </div>
      <div class="sidebar-card">
        <div class="sidebar-card-title">X（旧Twitter）の最新投稿</div>
        <a class="twitter-timeline" data-height="420" data-theme="light" href="https://twitter.com/shonan_doors?ref_src=twsrc%5Etfw">Tweets by shonan_doors</a>
        <script async src="https://platform.twitter.com/widgets.js" charset="utf-8"></script>
      </div>
    </aside>
  </div>
</main>

{FOOTER_HTML}

<script>
/* 記事一覧の絞り込み(カテゴリ/エリア/キーワード)。DOM内の.cardをクライアント側でフィルタする。
   ※記事本文自体はこのJSに依存せず、常に/articles/{{slug}}/の静的HTMLとして存在する。 */
(function(){{
  const CATS = {json.dumps({k: v["label"] for k, v in CATS.items()}, ensure_ascii=False)};
  const grid = document.getElementById('grid');
  const cards = Array.from(grid.children);
  const catChips = document.getElementById('catChips');
  const areaSelect = document.getElementById('areaSelect');
  const searchBox = document.getElementById('searchBox');
  const resultCount = document.getElementById('resultCount');
  let activeCat = 'all';

  function apply(){{
    const areaVal = areaSelect.value;
    const q = searchBox.value.trim().toLowerCase();
    let shown = 0;
    cards.forEach(card=>{{
      const cat = card.dataset.cat, area = card.dataset.area, hay = card.dataset.search;
      let ok = true;
      if (activeCat !== 'all' && cat !== activeCat) ok = false;
      if (areaVal && area !== areaVal) ok = false;
      if (q && !hay.includes(q)) ok = false;
      card.style.display = ok ? '' : 'none';
      if (ok) shown++;
    }});
    resultCount.textContent = (activeCat==='all' && !areaVal && !q) ? `全 ${{cards.length}} 件を掲載中` : `${{shown}} 件が見つかりました`;
  }}
  catChips.addEventListener('click', e=>{{
    if(!e.target.classList.contains('chip')) return;
    [...catChips.children].forEach(c=>c.classList.remove('active'));
    e.target.classList.add('active');
    activeCat = e.target.dataset.cat;
    apply();
  }});
  areaSelect.addEventListener('change', apply);
  searchBox.addEventListener('input', apply);

  /* 旧hash URL(#article-{{id}})でのアクセス・共有リンクを、新しい /articles/{{slug}}/ へ誘導する
     フォールバック。GitHub Pagesはサーバー側301ができないため、JSによる誘導としている。 */
  const OLD_ID_TO_SLUG = {redirect_map_json};
  const m = location.hash.match(/^#article-(\\d+)$/);
  if (m && OLD_ID_TO_SLUG[m[1]]) {{
    location.replace('/articles/' + OLD_ID_TO_SLUG[m[1]] + '/');
  }}
}})();

/* GA4カスタムイベント送信ヘルパー */
function trackEvent(name, params){{
  if (typeof gtag === 'function') {{ gtag('event', name, params); }}
}}

/* ---------- PICK UP carousel (松プラン想定枠) ----------
   FEATURED_IDS を編集すれば、回転させる記事を自由に入れ替えられます(articleのidで指定)。
   将来ここが実際の広告枠になった際は、別途「広告主データ」を差し込む形に拡張してください。
   Phase 1でのリンク化に伴い、カードは<a href>による該当記事ページへの通常遷移に変更しています。 */
const PICKUP_ITEMS = {pickup_items_json};
const CATS_FOR_PICKUP = {cats_label_bg_json};
const PICKUP_SCENES = {scenes_json};
(function(){{
  const N = PICKUP_ITEMS.length;
  if (!N) return;
  const track = document.getElementById('pickupTrack');
  const dots = document.getElementById('pickupDots');
  let trackIndex = 1, pickupTimer = null, isJumping = false, lastImpressionRealIndex = -1;

  function makeCardEl(item){{
    const a = document.createElement('a');
    a.className = 'pickup-card';
    a.href = '/articles/' + item.slug + '/';
    a.innerHTML = `
      <div class="pickup-card-img">${{PICKUP_SCENES[item.scene]}}</div>
      <div class="pickup-card-body">
        <div class="pickup-taglist">
          <span class="pickup-tag" style="background:${{CATS_FOR_PICKUP[item.cat].bg}}">${{CATS_FOR_PICKUP[item.cat].label}}</span>
          <span class="pickup-area">${{item.area}}</span>
        </div>
        <div class="pickup-card-title serif">${{item.title}}</div>
        <div class="pickup-card-dek">${{item.dek}}</div>
      </div>`;
    return a;
  }}
  const sequence = [PICKUP_ITEMS[N-1], ...PICKUP_ITEMS, PICKUP_ITEMS[0]];
  sequence.forEach(item => track.appendChild(makeCardEl(item)));
  PICKUP_ITEMS.forEach((item,i)=>{{
    const dot = document.createElement('div');
    dot.className = 'pickup-dot' + (i===0 ? ' active' : '');
    dot.addEventListener('click', ()=>{{ goToReal(i); restartPickupTimer(); }});
    dots.appendChild(dot);
  }});

  function updateActiveStates(){{
    const cards = [...track.children];
    cards.forEach((c,i)=> c.classList.toggle('active', i===trackIndex));
    const realIndex = ((trackIndex - 1) + N) % N;
    [...dots.children].forEach((d,i)=> d.classList.toggle('active', i===realIndex));
    if (realIndex !== lastImpressionRealIndex) {{
      lastImpressionRealIndex = realIndex;
      const shownItem = PICKUP_ITEMS[realIndex];
      if (shownItem) trackEvent('pickup_impression', {{ article_id: shownItem.id, article_title: shownItem.title }});
    }}
  }}
  function scrollToTrack(behavior){{
    const viewport = document.getElementById('pickupViewport');
    const card = track.children[trackIndex];
    if(!card || !viewport) return;
    const target = card.offsetLeft - (viewport.clientWidth - card.offsetWidth) / 2;
    viewport.scrollTo({{ left: target, behavior }});
  }}
  function goTo(newTrackIndex, animate){{
    trackIndex = newTrackIndex;
    updateActiveStates();
    scrollToTrack(animate ? 'smooth' : 'auto');
    if(trackIndex === N+1 || trackIndex === 0){{
      isJumping = true;
      setTimeout(()=>{{
        trackIndex = trackIndex === N+1 ? 1 : N;
        updateActiveStates();
        scrollToTrack('auto');
        isJumping = false;
      }}, animate ? 420 : 0);
    }}
  }}
  function advance(dir){{ if(!isJumping) goTo(trackIndex + dir, true); }}
  function goToReal(realIndex){{ isJumping = false; clearInterval(pickupTimer); goTo(realIndex + 1, true); }}
  function restartPickupTimer(){{ clearInterval(pickupTimer); pickupTimer = setInterval(()=> advance(1), 3000); }}

  updateActiveStates();
  requestAnimationFrame(()=> scrollToTrack('auto'));
  window.addEventListener('load', ()=> scrollToTrack('auto'));
  restartPickupTimer();
  document.getElementById('pickupPrev').addEventListener('click', ()=>{{ advance(-1); restartPickupTimer(); }});
  document.getElementById('pickupNext').addEventListener('click', ()=>{{ advance(1); restartPickupTimer(); }});
  const vp = document.getElementById('pickupViewport');
  vp.addEventListener('mouseenter', ()=> clearInterval(pickupTimer));
  vp.addEventListener('mouseleave', ()=> restartPickupTimer());
  window.addEventListener('resize', ()=> scrollToTrack('auto'));
  window.addEventListener('orientationchange', ()=> setTimeout(()=> scrollToTrack('auto'), 200));
}})();
</script>
<script src="/assets/common.js" defer></script>
</body>
</html>
"""


def render_sitemap(articles):
    from datetime import date
    urls = [{"loc": f"{SITE_DOMAIN}/", "lastmod": date.today().isoformat(), "priority": "1.0"}]
    for a in sorted(articles, key=lambda x: x["date"], reverse=True):
        urls.append({
            "loc": f"{SITE_DOMAIN}/articles/{a['slug']}/",
            "lastmod": a["date"],
            "priority": "0.7",
        })
    entries = "\n".join(
        f"  <url>\n    <loc>{u['loc']}</loc>\n    <lastmod>{u['lastmod']}</lastmod>\n    <priority>{u['priority']}</priority>\n  </url>"
        for u in urls
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{entries}
</urlset>
"""


def render_robots_txt():
    return f"""User-agent: *
Allow: /

Sitemap: {SITE_DOMAIN}/sitemap.xml
"""


def main():
    articles = load_json("data/articles.json")
    scenes = load_json("data/scenes.json")

    slugs = [a["slug"] for a in articles]
    if len(slugs) != len(set(slugs)):
        dupes = {s for s in slugs if slugs.count(s) > 1}
        raise SystemExit(f"slugの重複を検出しました。ビルドを中止します: {dupes}")

    ids = [a["id"] for a in articles]
    if len(ids) != len(set(ids)):
        raise SystemExit("idの重複を検出しました。ビルドを中止します。")

    # ---- 原子的ビルド ----
    # 一時ディレクトリに全ページを描画しきってから、最後に本番の articles/ と
    # index.html / sitemap.xml / robots.txt を置き換える。途中で例外が起きても、
    # 本番側は直前の成功時点の状態のまま残り、壊れた/不完全な状態が公開されることはない。
    staging_articles_dir = os.path.join(ROOT, ".build_tmp_articles")
    if os.path.isdir(staging_articles_dir):
        shutil.rmtree(staging_articles_dir)
    os.makedirs(staging_articles_dir, exist_ok=True)

    try:
        for item in articles:
            page_dir = os.path.join(staging_articles_dir, item["slug"])
            os.makedirs(page_dir, exist_ok=True)
            with open(os.path.join(page_dir, "index.html"), "w", encoding="utf-8") as f:
                f.write(render_article_page(item, scenes))

        new_index_html = render_index(articles, scenes)
        new_sitemap_xml = render_sitemap(articles)
        new_robots_txt = render_robots_txt()

        # ここまで例外なく到達できた場合のみ、本番ファイルを置き換える
        final_articles_dir = os.path.join(ROOT, "articles")
        if os.path.isdir(final_articles_dir):
            shutil.rmtree(final_articles_dir)
        shutil.move(staging_articles_dir, final_articles_dir)

        for filename, content in [
            ("index.html", new_index_html),
            ("sitemap.xml", new_sitemap_xml),
            ("robots.txt", new_robots_txt),
        ]:
            tmp_path = os.path.join(ROOT, filename + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, os.path.join(ROOT, filename))
    except Exception:
        if os.path.isdir(staging_articles_dir):
            shutil.rmtree(staging_articles_dir)
        raise

    print(f"ビルド完了: 記事ページ {len(articles)} 件 + トップページ + sitemap.xml + robots.txt")


if __name__ == "__main__":
    main()
