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
from datetime import date as _date

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

# トップページのカテゴリーナビゲーション用(丸い淡色背景+アイコン)。
# CATSのbg(バッジ用の濃色)とは別に、淡いパステル配色を用意する。
# 既存のcategory slug/URL/hub構造(CAT_EN・/category/{slug}/)には一切影響しない。
CATEGORY_NAV_STYLE = {
    "t": {"bg": "#E3F2F7", "fg": "#1D6E8C",
          "icon": '<svg viewBox="0 0 24 24" fill="none"><path d="M3 16l4.5-6 3 3.5L15 7l6 9" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><circle cx="8" cy="7.5" r="1.6" fill="currentColor"/></svg>'},
    "g": {"bg": "#FDE9DE", "fg": "#B8532B",
          "icon": '<svg viewBox="0 0 24 24" fill="none"><path d="M7 3v7a2 2 0 002 2v9M7 3v9M5 3v9M17 3c-1.5 0-2.5 2-2.5 5s1 5 2.5 5v8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'},
    "b": {"bg": "#F6E4E7", "fg": "#B5495B",
          "icon": '<svg viewBox="0 0 24 24" fill="none"><path d="M4 9l1.2-4h13.6L20 9M4 9h16M4 9v9a1 1 0 001 1h14a1 1 0 001-1V9M9 19v-5h6v5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'},
    "e": {"bg": "#E7F3E9", "fg": "#357A4A",
          "icon": '<svg viewBox="0 0 24 24" fill="none"><path d="M6 3v18M6 4h11l-2.5 3.5L17 11H6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'},
    "c": {"bg": "#EFE7F4", "fg": "#6E4A96",
          "icon": '<svg viewBox="0 0 24 24" fill="none"><path d="M3 8h18M5 8V6l2-2h10l2 2v2M7 8v13M17 8v13M4 21h16" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'},
    "l": {"bg": "#FDF3DC", "fg": "#A67A1E",
          "icon": '<svg viewBox="0 0 24 24" fill="none"><path d="M4 11l8-7 8 7M6 10v10h12V10" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'},
    "p": {"bg": "#E4EAF6", "fg": "#3B5998",
          "icon": '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="8" r="3.4" stroke="currentColor" stroke-width="1.8"/><path d="M5 20c1.2-4 4-6 7-6s5.8 2 7 6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>'},
}


def _scenic_visual(sky, sea, accent, silhouette, gradient_id):
    """Hero/記事詳細用の「写真風」の湘南の風景イラストを生成する。
    実写真は使わず、既存のvisual system(フラットなSVGイラスト)の延長として、
    グラデーションの空・海+ワンポイントのシルエットで湘南らしい雰囲気を表現する。
    著作権のかかる外部画像は一切使用しない。"""
    return f"""<svg viewBox="0 0 600 320" preserveAspectRatio="xMidYMid slice" width="100%" height="100%" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <linearGradient id="sky-{gradient_id}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="{sky[0]}"/>
          <stop offset="100%" stop-color="{sky[1]}"/>
        </linearGradient>
        <linearGradient id="sea-{gradient_id}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="{sea[0]}"/>
          <stop offset="100%" stop-color="{sea[1]}"/>
        </linearGradient>
      </defs>
      <rect width="600" height="190" fill="url(#sky-{gradient_id})"/>
      <circle cx="480" cy="70" r="34" fill="{accent}" opacity=".9"/>
      <rect y="190" width="600" height="130" fill="url(#sea-{gradient_id})"/>
      <path d="M0 195 Q60 182 120 195 T240 195 T360 195 T480 195 T600 195 V320 H0 Z" fill="{sea[1]}" opacity=".55"/>
      {silhouette}
    </svg>"""


# 記事Hero・トップページHero用の「写真風」ビジュアル(カテゴリー単位)。
# 既存の21種の小さなシーンSVG(記事一覧のサムネイル用)とは別に、
# より大きく雰囲気のある構図をカテゴリーごとに1枚ずつ用意する。
CATEGORY_HERO_VISUALS = {
    "t": _scenic_visual(  # 観光: 江の島・海
        sky=("#BFE3F0", "#EAF6F2"), sea=("#3E8FB0", "#1D6E8C"), accent="#F6D186",
        silhouette='<path d="M340 195 Q380 150 420 160 Q450 168 470 195 Z" fill="#4A7A8C" opacity=".8"/>'
                    '<path d="M0 260 Q60 245 120 260 T240 260 T360 260 T480 260 T600 260" stroke="#fff" stroke-width="3" fill="none" opacity=".6"/>',
        gradient_id="t"),
    "g": _scenic_visual(  # グルメ: 暖色の街並み・カフェ
        sky=("#FBE3CE", "#FCEFE1"), sea=("#E2A272", "#C97A44"), accent="#F2C879",
        silhouette='<rect x="60" y="150" width="90" height="45" fill="#8B5E34" opacity=".85"/>'
                    '<rect x="170" y="165" width="70" height="30" fill="#A6743E" opacity=".85"/>'
                    '<rect x="90" y="130" width="20" height="20" fill="#FCEFE1" opacity=".7"/>',
        gradient_id="g"),
    "b": _scenic_visual(  # 企業・店舗: 街並み
        sky=("#F4DCDD", "#F8ECE3"), sea=("#C97A6E", "#A6431E"), accent="#E9B48A",
        silhouette='<rect x="80" y="140" width="60" height="55" fill="#A6431E" opacity=".85"/>'
                    '<rect x="150" y="120" width="45" height="75" fill="#8B5E34" opacity=".8"/>'
                    '<rect x="205" y="150" width="55" height="45" fill="#B5495B" opacity=".75"/>',
        gradient_id="b"),
    "e": _scenic_visual(  # イベント: 賑やかな海辺
        sky=("#D9EEDD", "#EFF7EF"), sea=("#5FA37A", "#357A4A"), accent="#F2D06B",
        silhouette='<path d="M120 195 L120 140 L160 155 L120 170 Z" fill="#357A4A" opacity=".85"/>'
                    '<path d="M420 195 L420 150 L455 163 L420 176 Z" fill="#5FA37A" opacity=".85"/>',
        gradient_id="e"),
    "c": _scenic_visual(  # 文化: 鳥居・寺社
        sky=("#E7DCEF", "#F3EEF7"), sea=("#7C6A9E", "#6E4A96"), accent="#D8B979",
        silhouette='<path d="M270 195 V145 M330 195 V145 M255 150 H345 M260 135 H340" stroke="#6E4A96" stroke-width="7" opacity=".85" stroke-linecap="round"/>',
        gradient_id="c"),
    "l": _scenic_visual(  # 暮らし: 住宅街
        sky=("#FBEFD3", "#FCF6E4"), sea=("#D9B36C", "#A67A1E"), accent="#F0C878",
        silhouette='<path d="M90 160 L130 130 L170 160 V195 H90 Z" fill="#A67A1E" opacity=".85"/>'
                    '<path d="M210 170 L240 148 L270 170 V195 H210 Z" fill="#C99A4C" opacity=".8"/>',
        gradient_id="l"),
    "p": _scenic_visual(  # 人: 夕景の海辺の人影
        sky=("#DCE3F1", "#EFF2F8"), sea=("#6F87B8", "#3B5998"), accent="#F0DDA0",
        silhouette='<circle cx="300" cy="168" r="10" fill="#16233A" opacity=".85"/>'
                    '<path d="M300 178 V210 M290 195 L300 205 L310 195" stroke="#16233A" stroke-width="4" fill="none" stroke-linecap="round" opacity=".85"/>',
        gradient_id="p"),
}

# トップページHero専用(観光カテゴリーのビジュアルを流用し、湘南=海のブランドイメージを統一する)
TOP_HERO_VISUAL = CATEGORY_HERO_VISUALS["t"]

# エリアHub用のfallback風景ビジュアル(8エリア)。実写真/AI生成画像が用意されるまでの
# 暫定表示として、カテゴリーと同じ手法で色調だけエリアごとに変えたものを用意する。
AREA_HERO_VISUALS = {
    "藤沢": _scenic_visual(sky=("#BFE3F0", "#EAF6F2"), sea=("#3E8FB0", "#1D6E8C"), accent="#F6D186",
        silhouette='<path d="M340 195 Q380 150 420 160 Q450 168 470 195 Z" fill="#4A7A8C" opacity=".8"/>', gradient_id="fujisawa"),
    "茅ヶ崎": _scenic_visual(sky=("#D9EEDD", "#EFF7EF"), sea=("#5FA37A", "#357A4A"), accent="#F2D06B",
        silhouette='<path d="M120 195 L120 140 L160 155 L120 170 Z" fill="#357A4A" opacity=".85"/>', gradient_id="chigasaki"),
    "鎌倉": _scenic_visual(sky=("#E7DCEF", "#F3EEF7"), sea=("#7C6A9E", "#6E4A96"), accent="#D8B979",
        silhouette='<path d="M270 195 V145 M330 195 V145 M255 150 H345 M260 135 H340" stroke="#6E4A96" stroke-width="7" opacity=".85" stroke-linecap="round"/>', gradient_id="kamakura"),
    "平塚": _scenic_visual(sky=("#FBE3CE", "#FCEFE1"), sea=("#E2A272", "#C97A44"), accent="#F2C879",
        silhouette='<rect x="60" y="150" width="90" height="45" fill="#8B5E34" opacity=".85"/>', gradient_id="hiratsuka"),
    "大磯": _scenic_visual(sky=("#DCE3F1", "#EFF2F8"), sea=("#6F87B8", "#3B5998"), accent="#F0DDA0",
        silhouette='<circle cx="300" cy="168" r="10" fill="#16233A" opacity=".85"/>', gradient_id="oiso"),
    "二宮": _scenic_visual(sky=("#FBEFD3", "#FCF6E4"), sea=("#D9B36C", "#A67A1E"), accent="#F0C878",
        silhouette='<path d="M90 160 L130 130 L170 160 V195 H90 Z" fill="#A67A1E" opacity=".85"/>', gradient_id="ninomiya"),
    "逗子": _scenic_visual(sky=("#F4DCDD", "#F8ECE3"), sea=("#C97A6E", "#A6431E"), accent="#E9B48A",
        silhouette='<rect x="80" y="140" width="60" height="55" fill="#A6431E" opacity=".85"/>', gradient_id="zushi"),
    "葉山": _scenic_visual(sky=("#BFE3F0", "#EAF6F2"), sea=("#3E8FB0", "#1D6E8C"), accent="#F6D186",
        silhouette='<path d="M0 260 Q60 245 120 260 T240 260 T360 260 T480 260 T600 260" stroke="#fff" stroke-width="3" fill="none" opacity=".6"/>', gradient_id="hayama"),
}

AREA_ORDER = ["藤沢", "茅ヶ崎", "鎌倉", "平塚", "大磯", "二宮", "逗子", "葉山"]
AREA_EN = {
    "藤沢": "fujisawa", "茅ヶ崎": "chigasaki", "鎌倉": "kamakura", "平塚": "hiratsuka",
    "大磯": "oiso", "二宮": "ninomiya", "逗子": "zushi", "葉山": "hayama",
}
AREA_EN_TO_JA = {v: k for k, v in AREA_EN.items()}

# ---------- 画像解決アーキテクチャ ----------
# 優先順位:
#   1. article/pageに指定された実画像(article["heroImage"] / article["thumbnailImage"])
#      → 将来、記事生成パイプラインでAI生成画像を自動保存する差し込み口。
#      現時点ではdata/articles.jsonにこのフィールドを持つ記事は存在しないため、
#      常に2へフォールバックする(=既存データへの変更は不要)。
#   2. カテゴリー別fallback画像(assets/images/categories/{cat_en}.svg)
#      実写真/AI生成画像を用意した場合は、同名で拡張子違いのファイルに差し替えるだけでよい。
#   3. (2と同一ファイルのため、実質的な最終fallbackは2に統合されている)
IMAGES_BASE_URL = "/assets/images"
# トップページHeroの正式画像(placeholderのSVGではなく実写真)。
# resolve_*_hero_image() 系のfallbackアーキテクチャとは独立した、
# TOPページ専用の1枚の固定パス。
TOP_HERO_IMAGE = f"{IMAGES_BASE_URL}/hero/shonan-hero.webp"
TOP_HERO_IMAGE_WIDTH = 1672
TOP_HERO_IMAGE_HEIGHT = 941


def resolve_article_hero_image(item):
    """記事詳細ページのHero画像を解決する。
    戻り値: (src, alt)"""
    if item.get("heroImage"):
        return item["heroImage"], f"{esc(item['title'])}のイメージ"
    cat_en = CAT_EN[item["cat"]]
    return f"{IMAGES_BASE_URL}/categories/{cat_en}.svg", f"{esc(CATS[item['cat']]['label'])}のイメージ"


def resolve_thumbnail_image(item):
    """記事一覧(list-item)のサムネイル画像を解決する。
    戻り値: (src, alt)"""
    if item.get("thumbnailImage"):
        return item["thumbnailImage"], f"{esc(item['title'])}のサムネイル"
    cat_en = CAT_EN[item["cat"]]
    return f"{IMAGES_BASE_URL}/categories/{cat_en}.svg", f"{esc(CATS[item['cat']]['label'])}のサムネイル"


def resolve_area_hero_image(area_ja):
    """エリアHubページのHero画像を解決する。戻り値: (src, alt)"""
    area_en = AREA_EN[area_ja]
    return f"{IMAGES_BASE_URL}/areas/{area_en}.svg", f"{esc(area_ja)}のイメージ"

CAT_EN = {
    "t": "tourism", "b": "business", "g": "gourmet",
    "p": "people", "c": "culture", "e": "event", "l": "life",
}
CAT_EN_TO_JA = {v: k for k, v in CAT_EN.items()}

HUB_PAGE_SIZE = 24  # 一覧系ページ(トップ/地域/カテゴリ)1ページあたりの表示件数

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

CATEGORY_MENU_LINKS = "".join(
    f'<a href="/category/{CAT_EN[k]}/">{v["label"]}</a>' for k, v in CATS.items()
)
AREA_MENU_LINKS = "".join(
    f'<a href="/area/{AREA_EN[a]}/">{a}</a>' for a in AREA_ORDER
)

HEADER_HTML = f"""<header>
  <svg class="wave-deco" viewBox="0 0 300 200" fill="none">
    <path d="M0 120 Q40 90 80 120 T160 120 T240 120 T320 120" stroke="#1D3557" stroke-width="2" opacity=".3"/>
    <path d="M0 145 Q40 115 80 145 T160 145 T240 145 T320 145" stroke="#E8542B" stroke-width="2" opacity=".25"/>
    <path d="M0 170 Q40 140 80 170 T160 170 T240 170 T320 170" stroke="#1D3557" stroke-width="1.5" opacity=".18"/>
  </svg>
  <div class="head-inner">
    <div class="brand-block">
      <a href="/" class="brand-link">
      <img class="brand-mark" src="{IMAGES_BASE_URL}/brand/shonan-doors-logo.webp" width="240" height="240" alt="Shonan Doors">
      <div class="brand-text">
        <div class="brand-name serif">湘南Doors</div>
        <div class="brand-tagline">湘南と、人をつなぐ地域メディア</div>
      </div>
      </a>
      <div class="head-actions">
        <a class="head-icon-btn" href="/" aria-label="トップ・記事をさがす">
          <svg viewBox="0 0 24 24" fill="none"><circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="2"/><path d="M20 20L16.5 16.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
        </a>
        <button class="head-icon-btn" id="navMenuToggle" aria-label="メニュー" aria-expanded="false">
          <svg viewBox="0 0 24 24" fill="none"><path d="M4 7H20M4 12H20M4 17H20" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
        </button>
      </div>
    </div>
  </div>
  <nav class="nav-menu-panel" id="navMenuPanel" hidden>
    <div class="nav-menu-inner">
      <div class="nav-menu-group">
        <div class="nav-menu-label">カテゴリー</div>
        <div class="nav-menu-links">{CATEGORY_MENU_LINKS}</div>
      </div>
      <div class="nav-menu-group">
        <div class="nav-menu-label">エリア</div>
        <div class="nav-menu-links">{AREA_MENU_LINKS}</div>
      </div>
    </div>
  </nav>
</header>"""

FOOTER_AREA_LINKS = "".join(
    f'<a href="/area/{AREA_EN[a]}/">{a}</a>' for a in AREA_ORDER
)

FOOTER_HTML = f"""<footer>
  <svg class="wave-deco foot-wave" viewBox="0 0 300 60" fill="none" preserveAspectRatio="none">
    <path d="M0 30 Q40 5 80 30 T160 30 T240 30 T320 30" stroke="#1D3557" stroke-width="2" opacity=".18"/>
    <path d="M0 42 Q40 17 80 42 T160 42 T240 42 T320 42" stroke="#4A90A4" stroke-width="2" opacity=".22"/>
  </svg>
  <div class="foot-inner">
    <div class="foot-closing">
      <p class="foot-closing-text serif">湘南の「今」が、<br>もっと身近になる。</p>
      <div class="sns-row" id="snsRow"></div>
    </div>
    <div class="foot-mission">
      <span class="serif">「湘南に関わるなら、この場所。」</span>
      湘南Doorsは、湘南の企業・お店・人・文化・イベント・観光を継続的に取材し、記録として積み重ねていく独立した地域メディアです。特定のサービスやEC、求人といった事業には属さず、あくまで「湘南を知る入口」であることを目的に運営しています。
      <div class="foot-area-links">
        <span class="foot-area-links-label">エリアから探す</span>
        {FOOTER_AREA_LINKS}
      </div>
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


# ---------- Phase 4: イベントライフサイクル判定(表示用途のみ) ----------
# 重要: この判定はユーザー向けの終了表示(バナー等)にのみ使用する。
# Event structured data(build_structured_data内)の出力可否・eventStatusの
# 値は、この関数の結果に一切依存させない(開催日超過だけを理由に
# Event構造化データをArticleへフォールバックさせたり、eventStatusを
# 書き換えたりしない、という今回の方針のため)。

def event_lifecycle_status(item, today=None):
    """cat='e'の記事について、eventStartDate/eventEndDateをもとに
    'upcoming' / 'ongoing' / 'ended' / 'unknown' のいずれかを返す。

    - eventEndDateが無い場合は、eventStartDateをeffective_endとして扱う
      (単日イベントの自然なfallback)。
    - eventStartDateが無い場合は判定不能として'unknown'を返す
      (日付を推測しないため)。
    """
    if today is None:
        today = _date.today()
    start = item.get("eventStartDate") or ""
    if not start:
        return "unknown"
    end = item.get("eventEndDate") or start
    try:
        start_d = _date.fromisoformat(start)
        end_d = _date.fromisoformat(end)
    except (ValueError, TypeError):
        return "unknown"
    if today < start_d:
        return "upcoming"
    if start_d <= today <= end_d:
        return "ongoing"
    if today > end_d:
        return "ended"
    return "unknown"


def render_event_ended_banner(item, today=None):
    """cat='e'かつステータスが'ended'の場合のみ、記事上部に表示する
    終了バナーHTMLを返す。それ以外はNoneを返す(何も挿入しない)。"""
    if item.get("cat") != "e":
        return None
    status = event_lifecycle_status(item, today)
    if status != "ended":
        return None
    start = item["eventStartDate"]
    y, m, _ = start.split("-")
    return (
        '<div class="event-ended-banner">'
        '⚠ このイベントは終了しました。'
        f'この記事は{int(y)}年{int(m)}月開催時点の情報です。'
        '</div>'
    )


# ---------- トップページ PICK UP選定 ----------
# 現時点ではGA4/Search Console等の外部人気度データを取得していないため、
# 「終了済みイベントを除外した直近記事」を暫定fallbackとして使う。
# 将来的にGA4等から popularity_scores={article_id: score} を用意できた場合、
# その引数を渡すだけで人気順に切り替えられる構造にしてある
# (呼び出し側のrender_index()やHTML/JS/CSSには一切手を加えずに済む)。

def select_pickup_articles(articles, count=5, popularity_scores=None, today=None):
    """PICK UPカルーセルに表示する記事を選ぶ。

    popularity_scores が渡された場合はそのスコア降順で選ぶ(Phase 6以降、
    GA4等の実データを使う際の差し替え口)。
    Noneの場合(現状)は、以下の暫定fallbackロジックを使う:
      1. event_lifecycle_status()が'ended'の記事を除外
      2. 残りを公開日の新しい順に並べる
      3. 同一エリアに極端に偏らないよう、まず各エリア1件ずつを優先的に
         拾ってから、残り枠を新しい順で埋める(複雑なランキングは行わない)
      4. 上位count件を返す
    """
    candidates = [a for a in articles if event_lifecycle_status(a, today) != "ended"]

    if popularity_scores:
        candidates.sort(key=lambda a: popularity_scores.get(a["id"], 0), reverse=True)
        return candidates[:count]

    candidates.sort(key=lambda a: a["date"], reverse=True)

    picked = []
    picked_ids = set()
    seen_areas = set()
    # 1巡目: エリアが被らない範囲で新しい順に拾う
    for a in candidates:
        if len(picked) >= count:
            break
        if a["area"] in seen_areas:
            continue
        picked.append(a)
        picked_ids.add(a["id"])
        seen_areas.add(a["area"])
    # 2巡目: 件数が足りなければ、エリアの重複を気にせず新しい順で埋める
    if len(picked) < count:
        for a in candidates:
            if len(picked) >= count:
                break
            if a["id"] in picked_ids:
                continue
            picked.append(a)
            picked_ids.add(a["id"])

    return picked


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
        sns_chips.append(render_infobox_sns_icon(sns["instagram"], "Instagram",
            "linear-gradient(45deg,#FEDA75,#FA7E1E,#D62976,#962FBF,#4F5BD5)",
            'M7 2h10a5 5 0 0 1 5 5v10a5 5 0 0 1-5 5H7a5 5 0 0 1-5-5V7a5 5 0 0 1 5-5zm0 2a3 3 0 0 0-3 3v10a3 3 0 0 0 3 3h10a3 3 0 0 0 3-3V7a3 3 0 0 0-3-3H7zm5 3.5A4.5 4.5 0 1 1 7.5 12 4.5 4.5 0 0 1 12 7.5zm0 2A2.5 2.5 0 1 0 14.5 12 2.5 2.5 0 0 0 12 9.5zM17.8 6.2a1.1 1.1 0 1 1-1.1 1.1 1.1 1.1 0 0 1 1.1-1.1z')
        )
    if sns.get("facebook"):
        sns_chips.append(render_infobox_sns_icon(sns["facebook"], "Facebook", "#1877F2",
            'M14 9h3V6h-3c-2.2 0-4 1.8-4 4v2H8v3h2v6h3v-6h3l1-3h-4v-2c0-.6.4-1 1-1z')
        )
    if sns.get("x"):
        sns_chips.append(render_infobox_sns_icon(sns["x"], "X", "#000",
            'M4 4l7 8.5L4.5 20H7l5-5.8L16 20h4l-7.3-8.9L19.5 4H17l-4.6 5.3L8 4H4z')
        )
    if sns.get("tiktok"):
        sns_chips.append(render_infobox_sns_icon(sns["tiktok"], "TikTok", "#000",
            'M14 3h2.2a4.6 4.6 0 0 0 3.8 3.9V9a7 7 0 0 1-3.8-1.1v6.6a5.5 5.5 0 1 1-5.5-5.5c.2 0 .4 0 .6.03v2.2a3.3 3.3 0 1 0 2.3 3.15V3z')
        )
    if sns_chips:
        rows.append(f'<div class="modal-info-row"><span class="modal-info-label">SNS</span><span class="modal-sns-row">{"".join(sns_chips)}</span></div>')

    if not rows:
        return ""
    return f'<div class="modal-infobox">{"".join(rows)}</div>'


def render_infobox_sns_icon(url, label, bg, path):
    """店舗情報ボックス内のSNSボタン。TOP/FooterのSNSアイコン(.sns-icon)と
    同じ「ブランドカラーの円形背景+白いアイコン」のデザインを再利用する。
    アイコンはfill="currentColor"にし、ボタン側のcolor:#fffを確実に継承させる
    (テキストラベルではなく実SVGアイコンにすることで、以前.modal-info-row a{color:...}
    という周辺スタイルに文字色を上書きされていた問題も併せて解消している)。"""
    return (
        f'<a class="modal-sns-chip" style="background:{bg}" href="{esc(url)}" '
        f'target="_blank" rel="noopener" aria-label="{esc(label)}">'
        f'<svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor"><path d="{path}"/></svg>'
        f'</a>'
    )


# ---------- Phase 3: 関連記事・パンくずリスト ----------

def score_related(item, all_articles):
    """記事間の関連度をスコアリングする。SEO目的の機械的な羅列ではなく、
    「同じ地域」「同じカテゴリ」「共通タグ」の重なりを見て、実際に読者が
    次に読みたくなりそうな記事を優先する設計。"""
    scored = []
    item_tags = set(item.get("tags", []))
    for other in all_articles:
        if other["id"] == item["id"]:
            continue
        score = 0
        if other["area"] == item["area"]:
            score += 3
        if other["cat"] == item["cat"]:
            score += 2
        score += len(item_tags & set(other.get("tags", [])))
        if score > 0:
            scored.append((score, other["date"], other))
    # スコア優先、同点は新しい記事を優先
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [x[2] for x in scored]


def render_related_articles(item, all_articles, scenes, count=5):
    related = score_related(item, all_articles)[:count]
    if not related:
        return ""
    cards = "\n      ".join(render_list_item(r, scenes) for r in related)
    return f"""<div class="related-articles">
      <div class="related-articles-title">この記事に関連する話題</div>
      <div class="article-list">
      {cards}
      </div>
    </div>"""


def render_list_item(item, scenes, with_search_attrs=False):
    """コンパクトな横リスト形式の1記事ぶんのHTMLを返す。
    [thumbnail] [category badge + headline + date] [>] という構成で、
    トップページの新着記事一覧・地域/カテゴリハブ・pagination・関連記事の
    すべてで共通のデザインシステムとして使う(1画面に複数記事を表示できる
    密度を優先し、大きなカードUIは使わない)。"""
    search_attrs = ""
    if with_search_attrs:
        search_hay = esc((item["title"] + item["dek"] + "".join(item.get("tags", []))).lower())
        search_attrs = f' data-cat="{item["cat"]}" data-area="{esc(item["area"])}" data-search="{search_hay}"'
    thumb_src, thumb_alt = resolve_thumbnail_image(item)
    return f"""<a class="list-item" href="/articles/{item['slug']}/"{search_attrs}>
        <img class="list-item-thumb" src="{thumb_src}" width="60" height="60" alt="{thumb_alt}" loading="lazy" decoding="async">
        <div class="list-item-body">
          <div class="list-item-meta">
            <span class="list-item-badge" style="background:{CATS[item['cat']]['bg']}">{esc(CATS[item['cat']]['label'])}</span>
            <span class="list-item-area">{esc(item['area'])}</span>
          </div>
          <div class="list-item-title serif">{esc(item['title'])}</div>
          <div class="list-item-date">{format_date(item['date'])}</div>
        </div>
        <span class="list-item-chevron" aria-hidden="true">›</span>
      </a>"""


def render_breadcrumb(items):
    """items: [(label, url_or_None), ...] 最後の要素は現在ページ(リンクなし)を想定。
    見た目のパンくずHTMLと、BreadcrumbList構造化データの両方を返す。

    引数のurlはルート相対パス(例: "/", "/area/fujisawa/")を想定している。
    表示用HTML側の<a href>は従来通りこの相対パスのまま使う(ブラウザ上は
    問題なく解決されるため)が、BreadcrumbList JSON-LDのitemはGoogleの
    構造化データ仕様上、絶対URLである必要があるため、SITE_DOMAINを使って
    絶対URL化してからitemへ格納する。"""
    def to_absolute_url(u):
        if u.startswith("http://") or u.startswith("https://"):
            return u  # すでに絶対URLの場合はそのまま(将来の呼び出し元向けの保険)
        return SITE_DOMAIN + u

    parts = []
    for label, url in items:
        if url:
            parts.append(f'<a href="{url}">{esc(label)}</a>')
        else:
            parts.append(f'<span aria-current="page">{esc(label)}</span>')
    breadcrumb_sep = ' <span class="breadcrumb-sep">›</span> '
    html_out = f'<nav class="breadcrumb" aria-label="breadcrumb">{breadcrumb_sep.join(parts)}</nav>'

    ld = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": i + 1,
                "name": label,
                **({"item": to_absolute_url(url)} if url else {}),
            }
            for i, (label, url) in enumerate(items)
        ],
    }
    ld_html = f'<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>'
    return html_out, ld_html


def render_pagination(base_path, current_page, total_pages):
    """base_path例: '/', '/area/fujisawa/', '/category/event/'
    1ページ目は base_path そのもの、2ページ目以降は base_path + 'page/{n}/' を使う。
    Googlebotが通常の<a href>ですべてのページを辿れるようにする。"""
    if total_pages <= 1:
        return ""

    def page_url(n):
        return base_path if n == 1 else f"{base_path}page/{n}/"

    links = []
    if current_page > 1:
        links.append(f'<a class="pagination-link" href="{page_url(current_page-1)}">‹ 前へ</a>')
    for n in range(1, total_pages + 1):
        cls = "pagination-link active" if n == current_page else "pagination-link"
        if n == current_page:
            links.append(f'<span class="{cls}">{n}</span>')
        else:
            links.append(f'<a class="{cls}" href="{page_url(n)}">{n}</a>')
    if current_page < total_pages:
        links.append(f'<a class="pagination-link" href="{page_url(current_page+1)}">次へ ›</a>')

    return f'<nav class="pagination" aria-label="ページネーション">{"".join(links)}</nav>'

def render_article_main(item, scenes, all_articles):
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

    area_en = AREA_EN[item["area"]]
    canonical_url = f"{SITE_DOMAIN}/articles/{item['slug']}/"
    breadcrumb_html, breadcrumb_ld = render_breadcrumb([
        ("湘南Doors トップ", "/"),
        (item["area"], f"/area/{area_en}/"),
        (item["title"], None),
    ])
    related_html = render_related_articles(item, all_articles, scenes)
    event_ended_banner = render_event_ended_banner(item) or ""
    share_html = render_share_buttons(item, canonical_url)
    hero_img_src, hero_img_alt = resolve_article_hero_image(item)

    return f"""<main class="article-main">
  <div class="article-page-wrap">
    {breadcrumb_html}
    <div class="modal article-modal-static">
      <div class="modal-art">
        <img src="{hero_img_src}" width="1200" height="630" alt="{hero_img_alt}" fetchpriority="high">
        <span class="modal-art-badge" style="background:{CATS[item["cat"]]["bg"]}">{esc(CATS[item["cat"]]["label"])}</span>
      </div>
      <div class="modal-body">
        <div class="modal-eyebrow">
          <span class="modal-eyebrow-date">{format_date(item["date"])}</span>
          <span class="modal-eyebrow-badge" style="background:{CATS[item["cat"]]["bg"]}">{esc(CATS[item["cat"]]["label"])}</span>
          <span class="modal-eyebrow-badge modal-eyebrow-area">{esc(item["area"])}</span>
        </div>
        {event_ended_banner}
        <h1 class="modal-title serif">{esc(item["title"])}</h1>
        <p class="modal-dek">{esc(item["dek"])}</p>
        {share_html}
        <div class="modal-text">{body_html}</div>
        {infobox}
        {map_html}
        {link_html}
        <div class="modal-tags">{tags_html}</div>
        {estate_html}
      </div>
    </div>
    {related_html}
    <a class="back-to-top" href="/">← 湘南Doors トップへ戻る</a>
  </div>
</main>
{breadcrumb_ld}"""


def render_share_buttons(item, canonical_url):
    """X / Facebook / LINE / リンクをコピー、の4つだけを実装する。
    Bookmark機能は現状存在しないため、見た目だけのダミーボタンは作らない。"""
    from urllib.parse import quote
    title_q = quote(item["title"])
    url_q = quote(canonical_url)
    x_url = f"https://twitter.com/intent/tweet?url={url_q}&text={title_q}"
    fb_url = f"https://www.facebook.com/sharer/sharer.php?u={url_q}"
    line_url = f"https://social-plugins.line.me/lineit/share?url={url_q}"
    return f"""<div class="share-row">
      <a class="share-btn share-btn-x" href="{x_url}" target="_blank" rel="noopener" aria-label="Xでシェア">
        <svg viewBox="0 0 24 24" fill="none"><path d="M4 4l7 8.5L4.5 20H7l5-5.8L16 20h4l-7.3-8.9L19.5 4H17l-4.6 5.3L8 4H4z" fill="currentColor"/></svg>
      </a>
      <a class="share-btn share-btn-fb" href="{fb_url}" target="_blank" rel="noopener" aria-label="Facebookでシェア">
        <svg viewBox="0 0 24 24" fill="none"><path d="M14 9h3V6h-3c-2.2 0-4 1.8-4 4v2H8v3h2v6h3v-6h3l1-3h-4v-2c0-.6.4-1 1-1z" fill="currentColor"/></svg>
      </a>
      <a class="share-btn share-btn-line" href="{line_url}" target="_blank" rel="noopener" aria-label="LINEでシェア">
        <svg viewBox="0 0 24 24" fill="none"><path d="M12 3C6.5 3 2 6.6 2 11c0 3.9 3.5 7.1 8.3 7.8.3.1.8.2.9.5.1.3.1.7 0 1l-.1 1c0 .3-.2 1 .9.6 1-.4 5.6-3.3 7.6-5.6 1.4-1.5 2.4-3 2.4-5.3C22 6.6 17.5 3 12 3z" fill="currentColor"/></svg>
      </a>
      <button type="button" class="share-btn share-btn-copy" data-copy-url="{esc(canonical_url)}" aria-label="リンクをコピー">
        <svg viewBox="0 0 24 24" fill="none"><path d="M9 9V6a2 2 0 012-2h7a2 2 0 012 2v7a2 2 0 01-2 2h-3M6 9h7a2 2 0 012 2v7a2 2 0 01-2 2H6a2 2 0 01-2-2v-7a2 2 0 012-2z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </button>
    </div>"""


def render_article_page(item, scenes, all_articles):
    canonical = f"{SITE_DOMAIN}/articles/{item['slug']}/"
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
{render_head_seo(item, canonical)}
{HEAD_COMMON}
</head>
<body>
{HEADER_HTML}
{render_article_main(item, scenes, all_articles)}
{FOOTER_HTML}
<script src="/assets/common.js" defer></script>
</body>
</html>
"""


def render_card(item, scenes):
    """記事一覧(トップ/地域ハブ/カテゴリハブ/pagination)の1件ぶん。
    render_list_item()と同じデザインシステムを使い、絞り込み用のdata属性のみ追加する。"""
    return render_list_item(item, scenes, with_search_attrs=True)


def render_index(articles, scenes):
    articles_sorted = sorted(articles, key=lambda d: d["date"], reverse=True)
    total_pages = max(1, -(-len(articles_sorted) // HUB_PAGE_SIZE))  # 切り上げ除算
    page_articles = articles_sorted[:HUB_PAGE_SIZE]
    cards_html = "\n      ".join(render_card(a, scenes) for a in page_articles)
    pagination_html = render_pagination("/", 1, total_pages)

    category_nav_html = "".join(
        f'<a class="cat-nav-item" href="/category/{CAT_EN[k]}/">'
        f'<span class="cat-nav-icon" style="background:{CATEGORY_NAV_STYLE[k]["bg"]}; color:{CATEGORY_NAV_STYLE[k]["fg"]}">{CATEGORY_NAV_STYLE[k]["icon"]}</span>'
        f'<span class="cat-nav-label">{v["label"]}</span></a>'
        for k, v in CATS.items()
    )
    used_areas = {a["area"] for a in articles}
    area_opts = "".join(f'<option value="{AREA_EN[a]}">{a}</option>' for a in AREA_ORDER if a in used_areas)

    # 旧hash URL(#article-{id}) → 新URL への転送用マップ(フォールバック用)
    id_to_slug = {a["id"]: a["slug"] for a in articles}
    redirect_map_json = json.dumps(id_to_slug, ensure_ascii=False)

    insta_tile_ids = [8, 1, 23, 56, 19, 39]
    insta_map = {a["id"]: a for a in articles}
    insta_items = [insta_map[i] for i in insta_tile_ids if i in insta_map]
    insta_html = "".join(
        f'<a class="insta-grid-item" href="/articles/{a["slug"]}/">{scenes[a["scene"]]}</a>' for a in insta_items
    )

    featured_articles = select_pickup_articles(articles, count=5)
    pickup_items = [
        {"id": a["id"], "slug": a["slug"], "cat": a["cat"], "area": a["area"],
         "title": a["title"], "dek": a["dek"], "scene": a["scene"], "date": a["date"]}
        for a in featured_articles
    ]
    pickup_items_json = json.dumps(pickup_items, ensure_ascii=False)
    cats_label_bg_json = json.dumps({k: {"label": v["label"], "bg": v["bg"]} for k, v in CATS.items()}, ensure_ascii=False)
    cat_en_json = json.dumps(CAT_EN, ensure_ascii=False)
    scenes_json = json.dumps(scenes, ensure_ascii=False)
    category_hero_json = json.dumps(CATEGORY_HERO_VISUALS, ensure_ascii=False)

    hero_html = f"""<section class="hero">
  <img class="hero-visual" src="{TOP_HERO_IMAGE}" width="{TOP_HERO_IMAGE_WIDTH}" height="{TOP_HERO_IMAGE_HEIGHT}" alt="湘南の海岸から望む富士山と江の島" fetchpriority="high" decoding="async">
  <div class="hero-overlay">
    <div class="hero-inner">
      <p class="hero-tagline serif">湘南を、もっと<br>知りたくなる。</p>
      <p class="hero-sub">湘南の今を、<br>人・まち・くらしの視点でお届けします。</p>
    </div>
  </div>
</section>
<nav class="cat-nav" aria-label="カテゴリーから探す">
  <div class="cat-nav-track">{category_nav_html}</div>
</nav>"""

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

{hero_html}

<!-- top banner slot (松プラン想定・回転バナー) : 実際に広告主がつく際は、ステマ規制対応として小さく「PR」等の表示を追加してください -->
<div class="pickup-section">
  <div class="pickup-section-title">ピックアップ記事</div>
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
    <select id="areaSelect" onchange="if(this.value) location.href='/area/'+this.value+'/';">
      <option value="">エリアで探す</option>{area_opts}
    </select>
    <input type="text" id="searchBox" placeholder="このページ内をキーワードで絞り込み">
  </div>
</div>

<main>
  <div class="content-layout">
    <div class="content-main">
      <div class="section-head">
        <h2 class="section-title serif">新着記事</h2>
        <p id="resultCount">全 {len(articles)} 件中 最新{len(page_articles)}件を表示中</p>
      </div>
      <div class="article-list" id="grid">
      {cards_html}
      </div>
      {pagination_html}
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
/* このページに表示中の記事だけを対象にした、キーワードでの簡易絞り込み。
   カテゴリ・エリアはPhase 3で /category/{{cat}}/ ・ /area/{{area}}/ の
   実ページ(ハブページ)に分離したため、ここでは検索ボックスのみを扱う。
   ※記事本文自体はこのJSに依存せず、常に/articles/{{slug}}/の静的HTMLとして存在する。 */
(function(){{
  const grid = document.getElementById('grid');
  const cards = Array.from(grid.children);
  const searchBox = document.getElementById('searchBox');
  const resultCount = document.getElementById('resultCount');
  const totalCount = {len(articles)};
  const pageCount = cards.length;

  searchBox.addEventListener('input', ()=>{{
    const q = searchBox.value.trim().toLowerCase();
    let shown = 0;
    cards.forEach(card=>{{
      const ok = !q || card.dataset.search.includes(q);
      card.style.display = ok ? '' : 'none';
      if (ok) shown++;
    }});
    resultCount.textContent = q
      ? `このページ内で ${{shown}} 件が見つかりました`
      : `全 ${{totalCount}} 件中 最新${{pageCount}}件を表示中`;
  }});

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
const CATEGORY_HERO_VISUALS = {category_hero_json};
const CAT_EN_FOR_PICKUP = {cat_en_json};
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
    const dateFormatted = (item.date || '').split('-').join('.');
    const imgSrc = '/assets/images/categories/' + CAT_EN_FOR_PICKUP[item.cat] + '.svg';
    a.innerHTML = `
      <div class="pickup-card-img">
        <img src="${{imgSrc}}" width="800" height="420" alt="${{CATS_FOR_PICKUP[item.cat].label}}のイメージ" loading="lazy" decoding="async">
        <span class="pickup-tag" style="background:${{CATS_FOR_PICKUP[item.cat].bg}}">${{CATS_FOR_PICKUP[item.cat].label}}</span>
      </div>
      <div class="pickup-card-body">
        <div class="pickup-card-title serif">${{item.title}}</div>
        <div class="pickup-card-dek">${{item.dek}}</div>
        <div class="pickup-card-meta"><span class="pickup-area">${{item.area}}</span><span class="pickup-date">${{dateFormatted}}</span></div>
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


def render_listing_page(*, title, description, canonical_url, breadcrumb_items,
                         heading, subheading, page_articles, scenes, base_path,
                         page_num, total_pages, total_count, hero_image=None, category_icon=None):
    """地域ハブ・カテゴリハブ・トップページ2ページ目以降で共有するシンプルな一覧ページ。
    PICK UPカルーセルやサイドバーは持たず、パンくず+見出し+記事グリッド+ページネーションのみ。
    hero_image: (src, alt) を渡すとエリアHubのように見出し上にHero画像を表示する。
    category_icon: (bg, fg, icon_svg) を渡すとカテゴリーHubのように丸いアイコンを見出し横に表示する。
    (人気順等のソートタブは、実データ(閲覧数等)が無いため今回は追加しない。
     fake functionalityを作らない方針のため。)"""
    cards_html = "\n      ".join(render_card(a, scenes) for a in page_articles)
    pagination_html = render_pagination(base_path, page_num, total_pages)
    breadcrumb_html, breadcrumb_ld = render_breadcrumb(breadcrumb_items)

    hero_html = ""
    if hero_image:
        src, alt = hero_image
        hero_html = f'<img class="hub-hero" src="{src}" width="1200" height="480" alt="{alt}">'

    heading_html = f'<h1 class="hub-title serif">{esc(heading)}</h1>'
    if category_icon:
        bg, fg, icon_svg = category_icon
        heading_html = (
            f'<div class="hub-heading-row">'
            f'<span class="hub-heading-icon" style="background:{bg}; color:{fg}">{icon_svg}</span>'
            f'<h1 class="hub-title serif">{esc(heading)}</h1>'
            f'</div>'
        )

    ld = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": title,
        "description": description,
        "url": canonical_url,
    }

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="canonical" href="{canonical_url}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="湘南Doors">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{canonical_url}">
<meta property="og:image" content="{SITE_DOMAIN}/assets/ogp/beach.png">
<meta name="twitter:card" content="summary_large_image">
<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>
{breadcrumb_ld}
{HEAD_COMMON}
</head>
<body>
{HEADER_HTML}
<main class="hub-main">
  <div class="hub-page-wrap hub-page-wrap-top">
    {breadcrumb_html}
  </div>
  {hero_html}
  <div class="hub-page-wrap hub-page-wrap-bottom">
    {heading_html}
    <p class="hub-subtitle">{esc(subheading)}</p>
    <div class="article-list">
      {cards_html}
    </div>
    {pagination_html}
  </div>
</main>
{FOOTER_HTML}
<script src="/assets/common.js" defer></script>
</body>
</html>
"""


def render_area_hub_pages(area_ja, area_articles, scenes):
    """1エリア分の全ページ({page: html}の辞書)を生成する。"""
    area_en = AREA_EN[area_ja]
    articles_sorted = sorted(area_articles, key=lambda d: d["date"], reverse=True)
    total_pages = max(1, -(-len(articles_sorted) // HUB_PAGE_SIZE))
    pages = {}
    hero_image = resolve_area_hero_image(area_ja)
    for page_num in range(1, total_pages + 1):
        chunk = articles_sorted[(page_num - 1) * HUB_PAGE_SIZE: page_num * HUB_PAGE_SIZE]
        base_path = f"/area/{area_en}/"
        canonical = SITE_DOMAIN + base_path if page_num == 1 else f"{SITE_DOMAIN}{base_path}page/{page_num}/"
        pages[page_num] = render_listing_page(
            title=f"{area_ja}の記事一覧" + (f"({page_num}ページ目)" if page_num > 1 else "") + " | 湘南Doors",
            description=f"{area_ja}に関する企業・お店・人・文化・イベント・観光の記事一覧({len(articles_sorted)}件)。湘南Doorsが継続的に取材しています。",
            canonical_url=canonical,
            breadcrumb_items=[("湘南Doors トップ", "/"), (area_ja, None)],
            heading=f"{area_ja}の記事",
            subheading=f"{area_ja}に関する記事を{len(articles_sorted)}件掲載しています。",
            page_articles=chunk,
            scenes=scenes,
            base_path=base_path,
            page_num=page_num,
            total_pages=total_pages,
            total_count=len(articles_sorted),
            hero_image=hero_image,
        )
    return pages


def render_category_hub_pages(cat_key, cat_articles, scenes):
    cat_en = CAT_EN[cat_key]
    cat_label = CATS[cat_key]["label"]
    articles_sorted = sorted(cat_articles, key=lambda d: d["date"], reverse=True)
    total_pages = max(1, -(-len(articles_sorted) // HUB_PAGE_SIZE))
    pages = {}
    nav_style = CATEGORY_NAV_STYLE[cat_key]
    category_icon = (nav_style["bg"], nav_style["fg"], nav_style["icon"])
    for page_num in range(1, total_pages + 1):
        chunk = articles_sorted[(page_num - 1) * HUB_PAGE_SIZE: page_num * HUB_PAGE_SIZE]
        base_path = f"/category/{cat_en}/"
        canonical = SITE_DOMAIN + base_path if page_num == 1 else f"{SITE_DOMAIN}{base_path}page/{page_num}/"
        pages[page_num] = render_listing_page(
            title=f"{cat_label}の記事一覧" + (f"({page_num}ページ目)" if page_num > 1 else "") + " | 湘南Doors",
            description=f"湘南エリアの「{cat_label}」に関する記事一覧({len(articles_sorted)}件)。湘南Doorsが継続的に取材しています。",
            canonical_url=canonical,
            breadcrumb_items=[("湘南Doors トップ", "/"), (cat_label, None)],
            heading=f"{cat_label}の記事",
            subheading=f"「{cat_label}」に関する記事を{len(articles_sorted)}件掲載しています。",
            page_articles=chunk,
            scenes=scenes,
            base_path=base_path,
            page_num=page_num,
            total_pages=total_pages,
            total_count=len(articles_sorted),
            category_icon=category_icon,
        )
    return pages


def render_top_pagination_pages(articles, scenes):
    """トップページ2ページ目以降(/page/2/, /page/3/, ...)。1ページ目はindex.html自体が担う。"""
    articles_sorted = sorted(articles, key=lambda d: d["date"], reverse=True)
    total_pages = max(1, -(-len(articles_sorted) // HUB_PAGE_SIZE))
    pages = {}
    for page_num in range(2, total_pages + 1):
        chunk = articles_sorted[(page_num - 1) * HUB_PAGE_SIZE: page_num * HUB_PAGE_SIZE]
        base_path = "/"
        canonical = f"{SITE_DOMAIN}/page/{page_num}/"
        pages[page_num] = render_listing_page(
            title=f"記事一覧({page_num}ページ目) | 湘南Doors",
            description=f"湘南Doorsの記事一覧 {page_num}ページ目。",
            canonical_url=canonical,
            breadcrumb_items=[("湘南Doors トップ", "/"), (f"{page_num}ページ目", None)],
            heading="記事一覧",
            subheading=f"{page_num}ページ目",
            page_articles=chunk,
            scenes=scenes,
            base_path=base_path,
            page_num=page_num,
            total_pages=total_pages,
            total_count=len(articles_sorted),
        )
    return pages


def render_404_page():
    """GitHub Pages用のカスタム404ページ。
    - canonical・structured data・sitemapへの掲載は行わない(実在しないURL用のため)
    - noindex,followを付与し、検索エンジンにインデックスさせない
    - 既存のHEADER_HTML/FOOTER_HTML/hub-page-wrapをそのまま再利用し、
      デザインの二重管理を避ける"""
    area_links_html = "".join(
        f'<a class="card" href="/area/{AREA_EN[a]}/" style="padding:14px 18px; display:inline-block; margin:4px;">{esc(a)}</a>'
        for a in AREA_ORDER
    )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<title>お探しのページが見つかりません｜湘南Doors</title>
<meta name="robots" content="noindex,follow">
{HEAD_COMMON}
</head>
<body>
{HEADER_HTML}
<main>
  <div class="hub-page-wrap">
    <h1 class="hub-title serif">お探しのページが見つかりません</h1>
    <p class="hub-subtitle">ページが移動・削除されたか、URLが間違っている可能性があります。</p>
    <p style="margin:20px 0;"><a class="back-to-top" href="/" style="font-size:14px;">← 湘南Doors トップへ戻る</a></p>
    <div style="margin-top:12px;">
      <div class="hub-subtitle" style="margin-bottom:10px;">湘南の主要エリアから探す</div>
      <div>{area_links_html}</div>
    </div>
  </div>
</main>
{FOOTER_HTML}
<script src="/assets/common.js" defer></script>
</body>
</html>
"""


def render_sitemap(articles):
    from datetime import date
    today = date.today().isoformat()
    urls = [{"loc": f"{SITE_DOMAIN}/", "lastmod": today, "priority": "1.0"}]

    # 地域ハブ・カテゴリハブ(各ハブの1ページ目のみ。2ページ目以降はページネーション
    # リンクを辿ればクロールできるため、sitemapの肥大化を避ける目的で含めない)
    used_areas = sorted({a["area"] for a in articles}, key=lambda x: AREA_ORDER.index(x) if x in AREA_ORDER else 99)
    for area_ja in used_areas:
        latest = max((a["date"] for a in articles if a["area"] == area_ja), default=today)
        urls.append({"loc": f"{SITE_DOMAIN}/area/{AREA_EN[area_ja]}/", "lastmod": latest, "priority": "0.6"})

    used_cats = [k for k in CATS if any(a["cat"] == k for a in articles)]
    for cat_key in used_cats:
        latest = max((a["date"] for a in articles if a["cat"] == cat_key), default=today)
        urls.append({"loc": f"{SITE_DOMAIN}/category/{CAT_EN[cat_key]}/", "lastmod": latest, "priority": "0.6"})

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
    # 一時ディレクトリに全ページを描画しきってから、最後に本番の articles/・area/・
    # category/・page/ と index.html / sitemap.xml / robots.txt を置き換える。
    # 途中で例外が起きても、本番側は直前の成功時点の状態のまま残り、
    # 壊れた/不完全な状態が公開されることはない。
    staging_dir = os.path.join(ROOT, ".build_tmp")
    if os.path.isdir(staging_dir):
        shutil.rmtree(staging_dir)
    os.makedirs(staging_dir, exist_ok=True)

    def write(rel_path, content):
        full = os.path.join(staging_dir, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)

    try:
        for item in articles:
            write(f"articles/{item['slug']}/index.html", render_article_page(item, scenes, articles))

        by_area = {}
        for a in articles:
            by_area.setdefault(a["area"], []).append(a)
        for area_ja, area_articles in by_area.items():
            for page_num, html in render_area_hub_pages(area_ja, area_articles, scenes).items():
                area_en = AREA_EN[area_ja]
                rel = f"area/{area_en}/index.html" if page_num == 1 else f"area/{area_en}/page/{page_num}/index.html"
                write(rel, html)

        by_cat = {}
        for a in articles:
            by_cat.setdefault(a["cat"], []).append(a)
        for cat_key, cat_articles in by_cat.items():
            for page_num, html in render_category_hub_pages(cat_key, cat_articles, scenes).items():
                cat_en = CAT_EN[cat_key]
                rel = f"category/{cat_en}/index.html" if page_num == 1 else f"category/{cat_en}/page/{page_num}/index.html"
                write(rel, html)

        for page_num, html in render_top_pagination_pages(articles, scenes).items():
            write(f"page/{page_num}/index.html", html)

        new_index_html = render_index(articles, scenes)
        new_sitemap_xml = render_sitemap(articles)
        new_robots_txt = render_robots_txt()
        new_404_html = render_404_page()
        write("index.html", new_index_html)
        write("sitemap.xml", new_sitemap_xml)
        write("robots.txt", new_robots_txt)
        write("404.html", new_404_html)

        # ここまで例外なく到達できた場合のみ、本番ディレクトリを置き換える。
        # articles/ area/ category/ page/ はビルド生成物のみが置かれるディレクトリ
        # なので、生成物ごと丸ごと入れ替える(articles.jsonから消えた記事の
        # ページ等が残り続けることを防ぐ)。
        for dirname in ("articles", "area", "category", "page"):
            final_dir = os.path.join(ROOT, dirname)
            staged_dir = os.path.join(staging_dir, dirname)
            if os.path.isdir(final_dir):
                shutil.rmtree(final_dir)
            if os.path.isdir(staged_dir):
                shutil.move(staged_dir, final_dir)

        for filename in ("index.html", "sitemap.xml", "robots.txt", "404.html"):
            tmp_path = os.path.join(ROOT, filename + ".tmp")
            shutil.move(os.path.join(staging_dir, filename), tmp_path)
            os.replace(tmp_path, os.path.join(ROOT, filename))
    except Exception:
        if os.path.isdir(staging_dir):
            shutil.rmtree(staging_dir)
        raise
    finally:
        if os.path.isdir(staging_dir):
            shutil.rmtree(staging_dir)

    print(f"ビルド完了: 記事ページ {len(articles)} 件 + 地域ハブ{len(by_area)} + "
          f"カテゴリハブ{len(by_cat)} + トップページ + sitemap.xml + robots.txt + 404.html")


if __name__ == "__main__":
    main()
