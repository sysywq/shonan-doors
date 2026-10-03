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

# Google AdSense。Publisher IDはGoogle AdSense管理画面(サイトの所有権確認)で
# 発行された正式な値をそのまま使用する(推測値は使用していない)。
ADSENSE_PUBLISHER_ID = "ca-pub-2458583563727225"
ADS_TXT_PUBLISHER_ID = "pub-2458583563727225"  # ads.txtはca-pub-ではなくpub-表記が仕様
SITE_TITLE = "湘南Doors｜湘南の人・企業・文化・体験をつなぐメディア"

# ビルド基準日の環境変数。未設定なら実行日(date.today())を使う。
# CIではコミット済みsitemap.xmlのlastmodをここに渡し、実行日が変わっても
# 同じ入力から同じ生成物になるか(生成物の再生成漏れが無いか)を検証する。
BUILD_DATE_ENV = "SHONAN_DOORS_BUILD_DATE"


def build_today():
    value = os.environ.get(BUILD_DATE_ENV, "").strip()
    return _date.fromisoformat(value) if value else _date.today()


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
    """記事詳細ページ(および記事Heroと同じ画像を使うカルーセル)のHero画像を解決する。
    優先順位:
      1. 記事固有の実写真(item["heroImage"]) — 将来、記事ごとに個別写真を
         持たせる場合はこのフィールドを設定するだけで自動的に優先される。
      2. カテゴリ別写真(assets/images/category-photos/)。今回アップロードされた
         7枚の実写真で、トップページ上部のカテゴリロゴ/イラスト(assets/images/categories/)
         とは別物。カルーセル/記事Heroの現時点でのfallback先。
    戻り値: (src, alt)"""
    if item.get("heroImage"):
        return item["heroImage"], f"{esc(item['title'])}のイメージ"
    cat_en = CAT_EN[item["cat"]]
    return f"{IMAGES_BASE_URL}/category-photos/{cat_en}.webp", f"{esc(CATS[item['cat']]['label'])}のイメージ"





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

# favicon一式は generate_favicons.py で新ロゴ(白背景版)から生成し、
# リポジトリ直下(/favicon.ico 等)に静的ファイルとして配置している。
# HEAD_COMMON(全ページ共通)から一箇所だけ参照することで、
# ページごとの重複記述を避けている。
GA4_SNIPPET = """<script async src="https://www.googletagmanager.com/gtag/js?id=G-PB4LBKENHT"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());
  gtag('config', 'G-PB4LBKENHT');
</script>"""

HEAD_COMMON = f"""<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/png" sizes="16x16" href="/favicon-16x16.png">
<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png">
<link rel="icon" type="image/png" sizes="48x48" href="/favicon-48x48.png">
<link rel="icon" type="image/png" sizes="96x96" href="/favicon-96x96.png">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
<link rel="manifest" href="/site.webmanifest">
<meta name="theme-color" content="#ffffff">
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={ADSENSE_PUBLISHER_ID}" crossorigin="anonymous"></script>
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
      湘南Doorsは、湘南の企業・お店・人・文化・イベント・観光など、地域のさまざまな魅力を発信するメディアです。湘南をもっと盛り上げ、この街の魅力をより多くの人に届けることを目指して運営しています。地域で暮らす人にも、湘南を訪れる人にも、新しい湘南と出会える「湘南を知る入口」であり続けます。
      <div class="foot-area-links">
        <span class="foot-area-links-label">エリアから探す</span>
        {FOOTER_AREA_LINKS}
      </div>
    </div>
    <div class="foot-note">
      掲載情報は公開情報をもとに編集部が取材・構成したものです。店舗情報・開催情報は変更となる場合がありますので、最新情報は各施設・団体の公式情報をご確認ください。<br><br>
      広告掲載プランをご用意しています。詳しくはinfo@shonandoors.comまでお問い合わせください。<br><br>
      <span class="foot-links">
        <a href="/privacy/" id="footPrivacy">プライバシーポリシー</a>
        <a href="/contact/" id="footContact">お問い合わせ</a>
        <a href="/about/" id="footOperator">運営者情報</a>
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
        today = build_today()
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

    # 編集部が明示的にPICK UP指定した記事は先頭で扱う。現地取材・独自写真など、
    # 通常の公開日ランキングだけでは埋もれる編集記事を確実に露出できるようにする。
    featured = [a for a in candidates if a.get("pickupFeatured")]
    regular = [a for a in candidates if not a.get("pickupFeatured")]
