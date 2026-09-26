#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
湘南Doors 記事自動生成スクリプト
----------------------------------------------------------
毎日2系統の記事を生成し、data/articles.json に追記する。

  A) ニュース/イベント型(news)  … NEWS_ARTICLES_PER_DAY 件/日 (既定 3)
  B) ストックSEO型(stock)       … STOCK_ARTICLES_PER_DAY 件/日 (既定 2)
  C) 補充(top-up)               … A+Bが DAILY_TARGET_ARTICLES 件(既定 5)に届かない分を
                                   別のnews候補で補う(回数・監査件数に上限あり)

このスクリプトはもう index.html を直接編集しない。
ページの再生成は別ステップで build.py が行う(このスクリプトの責務ではない)。

安全設計(ニュース系。Phase 1〜4から変更なし):
- IDは data/id_counter.json から発行し、常にインクリメントのみ(再利用しない)。
- 生成された記事はスキーマ検証を通ったものだけを追記する。
- articles.json への書き込みはtmpファイル+os.replaceによる原子的な置換で行う。
- 重複記事チェック(直近90日、タイトル完全一致/タグ重複率)。
- 同一対象チェック(全期間): 同じ一次情報URL、同じ対象(店舗・施設・イベント・人物)の
  名称、本文の高類似度のいずれかに該当すれば、別記事として追加しない。
  「1つの対象につき記事は1本」が原則(イベントは開催年度が違う場合のみ別記事可)。
- 情報源ポリシー: 事実は一次情報(公式サイト・公式SNS・本人/主催者の発表・行政)のみ。
  他メディア(新聞・ニュースサイト・地域まとめメディア・ブログ・口コミサイト等)は
  ネタ探しのきっかけにだけ使ってよく、事実確認・表現の参考には使わない。
  sourcesに他メディアのURLが含まれる記事、
  人物の発言を引用している記事は機械的にrejectする。
- APIレスポンスの型検証(listでない・件数異常・dict以外混在を検出して安全停止)。
- 公開前監査(publish_gate.py): ドラフトは生成しただけでは公開しない。上記の検証を
  通った候補を、IDを発行する前にFact Audit(fact_audit.pyと同じ判定ルール)にかけ、
  合格した記事だけをarticles.jsonに書き込む。不合格記事は実行レポートの
  gate_rejectedに理由とclaimを残し、report_gate_rejections.pyがIssue化する。
  X投稿・IndexNowは従来どおりaccepted_ids(=公開が確定した記事)だけを対象にする。

ストック系(今回追加)の安全設計:
- ニュース系が失敗しても影響を受けない/その逆も同様(それぞれ独立したtry節)。
- テーマはdata/stock_topics.jsonという台帳で管理し、都度AIに自由発想させない。
- 台帳の候補が少なくなったら、AIに新しい候補を「補充」させる(無制限には増やさない)。
- 記事化する前に、既存記事・台帳内の他テーマと検索意図が重複していないかを
  AI自身に評価させたうえで、必要なら"skip"として記事化を見送る。
  無理に既定の本数(2件)を埋めようとはしない。
- API呼び出し回数は全ステージで上限付き(下記 MAX_* 定数を参照)。
"""

import os
import re
import sys
import json
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import anthropic

ROOT = os.path.dirname(os.path.abspath(__file__))
ARTICLES_JSON_PATH = os.path.join(ROOT, "data", "articles.json")
ID_COUNTER_PATH = os.path.join(ROOT, "data", "id_counter.json")
STOCK_TOPICS_PATH = os.path.join(ROOT, "data", "stock_topics.json")
EVENT_SERIES_PATH = os.path.join(ROOT, "data", "event_series.json")
# 実行結果レポート。ワークフロー側が「本当に永続化されたか」を検証するための
# 機械可読な記録。リポジトリの外(の一時領域)に書くため、誤ってcommit対象に
# 含まれる心配がない。
RUN_REPORT_PATH = os.environ.get(
    "SHONAN_DOORS_RUN_REPORT_PATH",
    os.path.join(tempfile.gettempdir(), "shonan_doors_run_report.json"),
)

AREAS = ["藤沢", "茅ヶ崎", "鎌倉", "平塚", "大磯", "二宮", "逗子", "葉山"]
CATS = {
    "t": "観光", "b": "企業・店舗", "g": "グルメ",
    "p": "人", "c": "文化", "e": "イベント", "l": "暮らし",
}
SCENES = [
    "shrine", "garden", "beach", "crossing", "monorail", "market", "brewery",
    "surf", "butcher", "mall", "seafood", "farm", "kanji", "stadium", "music",
    "mansion", "festival", "portrait", "marina", "cinema", "street",
]
AREA_EN = {
    "藤沢": "fujisawa", "茅ヶ崎": "chigasaki", "鎌倉": "kamakura", "平塚": "hiratsuka",
    "大磯": "oiso", "二宮": "ninomiya", "逗子": "zushi", "葉山": "hayama",
}
CAT_EN = {
    "t": "tourism", "b": "business", "g": "gourmet",
    "p": "people", "c": "culture", "e": "event", "l": "life",
}

# ---------- 情報源ポリシー(一次情報のみ) ----------
# 他メディアのドメイン。sources/linkにこれらが含まれる記事はrejectする。
# (一次情報=店舗・団体・主催者・行政の公式サイト/公式SNS/本人が出したプレスリリース。
#  PR TIMES等の配信サービス上の「本人が出したプレスリリース」は一次情報として扱う)
SECONDARY_MEDIA_DOMAINS = [
    "goguynet.jp", "jimohack-shonan.jp", "townnews.co.jp", "keizai.biz", "minkei.net",
    "news.yahoo.co.jp", "news.goo.ne.jp", "news.livedoor.com", "excite.co.jp", "msn.com",
    "shonanjin.com", "shonan-chilltime.com", "kanaloco.jp", "nikkei.com", "asahi.com",
    "yomiuri.co.jp", "mainichi.jp", "sankei.com", "tokyo-np.co.jp", "nhk.or.jp",
    "jcast.com", "itmedia.co.jp", "fashion-press.net", "walkerplus.com", "retrip.jp",
    "ameblo.jp", "note.com", "hatenablog.com", "livedoor.blog", "fc2.com",
    "tabelog.com", "retty.me", "hotpepper.jp", "gnavi.co.jp", "tripadvisor",
    "wikipedia.org", "jalan.net", "iko-yo.net", "enjoytokyo.jp",
]
# 人物の発言の引用(=他メディアの取材コメントの流用リスク)を検出するパターン。
QUOTE_ATTRIBUTION_PATTERNS = [
    r"」と(話す|話した|語る|語った|語っている|コメント|述べ|明かす|明かした|笑う|笑顔)",
    r"(店長|代表(?!する|的|作|格)|社長|オーナー|担当者|館長|会長|理事長|実行委員長)[^。\n]{0,12}「",
]

SOURCE_POLICY_BLOCK = """
【情報源ポリシー(最重要・例外なし)】
- 記事に書く事実(日付・場所・住所・営業時間・価格・席数・メニュー数・経歴・数値・
  店舗の設備や立地の説明など)は、すべて一次情報で確認できたものだけを使うこと。
  一次情報 = その店舗・企業・団体・主催者・自治体自身が出している公式サイト、
  公式SNS、本人が発表したプレスリリース。
- 新聞、ニュースサイト、地域まとめメディア(タウンニュース、号外NET、ジモハック、
  みんなの経済新聞/各地の経済新聞、湘南人 等)、Yahoo!ニュース等の転載、ブログ、
  口コミ・グルメサイト、Wikipediaは「他メディア」とみなす。他メディアは
  「ネタ(話題)を見つけるきっかけ」としてのみ使ってよい。事実確認や、構成・
  言い回し・切り口の参考には一切使わないこと。ネタを見つけたら、必ず公式の
  一次情報を探し、そこに書かれている内容だけで記事を組み立てること。
- 他メディアにしか載っていない情報(取材で得た席数・価格・店長のコメント等)は、
  たとえ事実でも書かないこと。
- 人物の発言を「」で引用しないこと(一次情報に本人の言葉が載っていても、要約して
  地の文で書く)。
- 一次情報で確認できない項目は空文字にし、本文でも触れないこと。推測で埋めない。
- sources には、記事の事実確認に実際に使った一次情報のURLをすべて入れること
  (他メディアのURLを入れた記事はシステムにより自動で不採用になる)。
"""

SUBJECT_POLICY_BLOCK = """
【1つの対象につき記事は1本(重複禁止・最重要)】
- 記事が扱う主な対象(店舗・施設・企業・イベント・展覧会・人物)が、既存記事一覧の
  いずれかと同じであれば、切り口やタイトルを変えても新しい記事にしないこと。
  名称の表記ゆれ(英字/カタカナ/略称/正式名称、例:「SHONAN TEA Luv.」と
  「湘南ティーラブ」)は同一の対象として扱うこと。
- 例外は、毎年開催されるイベントの「別の年度の回」だけ。同じ年度の同じイベントを
  2本書くことは禁止(告知・開幕・開催中などの段階違いも同一とみなす)。
- subjectNames には、主な対象の名称を表記ゆれも含めてすべて入れること
  (正式名称、英字表記、カタカナ/ひらがな読み、略称)。一般名詞(「カフェ」
  「彼岸花」など)は入れないこと。
"""

# ---------- 生成数設定(環境変数で上書き可能) ----------
NEWS_ARTICLES_PER_DAY = int(os.environ.get("NEWS_ARTICLES_PER_DAY", "3"))
STOCK_ARTICLES_PER_DAY = int(os.environ.get("STOCK_ARTICLES_PER_DAY", "2"))

# newsの重複/スキーマ不正で採用数がNEWS_ARTICLES_PER_DAYに満たない場合、
# 不足分だけを追加生成して補充する(refill)回数の上限。無限ループ防止。
# 例: 3件中1件reject → 1件だけ追加生成(1回目のrefillで届けば2回目以降は行わない)。
NEWS_REFILL_MAX_ATTEMPTS = int(os.environ.get("NEWS_REFILL_MAX_ATTEMPTS", "3"))

# 1日の公開件数の目標と最低ライン。news+stockが目標に届かない場合(stockが重複で
# 全件見送り、公開前監査で不合格など)は、別候補(news)を補充生成して公開前監査に
# かける(top-up)。品質基準は緩めない。最低ラインに届かなくても不合格記事は公開せず、
# 理由を実行レポート(shortfall)に残してIssue化する。
DAILY_TARGET_ARTICLES = int(os.environ.get("DAILY_TARGET_ARTICLES", "5"))
DAILY_MIN_ARTICLES = int(os.environ.get("DAILY_MIN_ARTICLES", "3"))
# top-upで別候補を生成する回数の上限(初回を含む)。無限ループ防止。
DAILY_TOPUP_MAX_ATTEMPTS = int(os.environ.get("DAILY_TOPUP_MAX_ATTEMPTS", "3"))
# 1回の実行で公開前監査にかけるドラフト数の上限(自動修正後の再監査は数えない)。
# refill/top-upが重なってもAPI費用が際限なく増えないための歯止め。
MAX_GATE_DRAFTS_PER_RUN = int(os.environ.get("MAX_GATE_DRAFTS_PER_RUN", "15"))

# ---------- ストック系: API呼び出し・台帳サイズの上限(暴走防止) ----------
STOCK_TOPIC_REFILL_THRESHOLD = int(os.environ.get("STOCK_TOPIC_REFILL_THRESHOLD", "6"))
MAX_NEW_STOCK_TOPICS_PER_REFILL = int(os.environ.get("MAX_NEW_STOCK_TOPICS_PER_REFILL", "8"))
MAX_STOCK_REFILL_TURNS = 5     # 台帳補充1回あたりの最大ターン数(tool_useループ)
MAX_STOCK_SELECTION_TURNS = 6  # テーマ選定+執筆1回あたりの最大ターン数
MAX_STOCK_CANDIDATES_IN_PROMPT = 20  # プロンプトに渡す候補テーマの上限件数


# ============================================================
# ニュース/イベント型記事の生成 (既存ロジック。変更は最小限)
# ============================================================

NEWS_ARTICLE_TOOL = {
    "name": "submit_articles",
    "description": "調査・執筆が完了した記事を提出する。",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "articles": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "cat": {"type": "string", "enum": list(CATS.keys())},
                        "area": {"type": "string", "enum": AREAS},
                        "scene": {"type": "string", "enum": SCENES},
                        "title": {"type": "string"},
                        "dek": {"type": "string"},
                        "body": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "subjectNames": {"type": "array", "items": {"type": "string"}, "description": "記事の主な対象(店舗・施設・イベント・人物等)の名称。英字/カナ/略称など表記ゆれもすべて"},
                        "sources": {"type": "array", "items": {"type": "string"}, "description": "事実確認に使った一次情報のURL(公式サイト・公式SNS・本人のプレスリリース・自治体)。他メディアは禁止"},
                        "link": {"type": "string", "description": "一次情報源のURL。見つからなければ空文字"},
                        "eventStartDate": {"type": "string", "description": "catが'e'(イベント)の場合のみ: 開催日をYYYY-MM-DD形式で。複数日開催の場合は初日。不明な場合は空文字"},
                        "eventEndDate": {"type": "string", "description": "catが'e'(イベント)の場合のみ: 複数日開催の場合の最終日をYYYY-MM-DD形式で。単日開催または不明な場合は空文字"},
                        "eventSeriesKey": {"type": "string", "description": "catが'e'(イベント)の場合のみ: このイベントの一意なシリーズ識別子(kebab-case英数字、例: tsurugaoka-hachimangu-reitaisai)。既存のイベントシリーズ台帳に一致するイベントがあれば同じキーを使い、無ければ新しいキーを考える。catが'e'以外の場合は空文字"},
                        "address": {"type": "string", "description": "b/gカテゴリのみ。確認できなければ空文字"},
                        "access": {"type": "string", "description": "b/gカテゴリのみ。確認できなければ空文字"},
                        "hours": {"type": "string", "description": "b/gカテゴリのみ。確認できなければ空文字"},
                        "closedDays": {"type": "string", "description": "b/gカテゴリのみ。確認できなければ空文字"},
                        "instagram": {"type": "string", "description": "公式InstagramのURL。無ければ空文字"},
                        "facebook": {"type": "string", "description": "公式FacebookのURL。無ければ空文字"},
                        "x": {"type": "string", "description": "公式XのURL。無ければ空文字"},
                        "tiktok": {"type": "string", "description": "公式TikTokのURL。無ければ空文字"},
                    },
                    "required": [
                        "cat", "area", "scene", "title", "dek", "body", "tags", "link",
                        "subjectNames", "sources",
                        "eventStartDate", "eventEndDate", "eventSeriesKey",
                        "address", "access", "hours", "closedDays",
                        "instagram", "facebook", "x", "tiktok",
                    ],
                },
            },
        },
        "required": ["articles"],
    },
}


def build_news_system_prompt(recent_titles, event_series=None, count=None):
    count = count or NEWS_ARTICLES_PER_DAY
    recent_block = ""
    if recent_titles:
        joined = "\n".join(f"- {t}" for t in recent_titles)
        recent_block = f"""
【既存記事一覧(重複防止・必須)】
湘南Doorsではすでに以下の記事を公開しています(全期間)。ここに含まれる対象
(店舗・施設・イベント・人物)は、切り口を変えても新しい記事にしないでください。
{joined}
"""

    event_series = event_series or []
    if event_series:
        series_lines = "\n".join(
            f"- key=\"{s['seriesKey']}\" 名称: {s.get('canonicalName','')}"
            f"（エリア: {s.get('area','')}）"
            for s in event_series
        )
        series_block = f"""
【イベントシリーズ台帳(eventSeriesKeyについて・必須)】
湘南Doorsでは、毎年繰り返し開催されるイベント(例: 例大祭、花火大会、まつり等)を
"eventSeriesKey"という識別子で管理しています。現在登録されているシリーズ:
{series_lines}

今回書くイベント記事が、上記のいずれかと同一のイベント(同じ祭り・同じ大会等、
開催回数や年度が違うだけの同一イベント)であれば、**必ず同じkeyをそのまま使って
ください**(新しいキーを作らないこと)。上記のどれにも当てはまらない新しいイベント
であれば、内容を表す新しいkebab-case(小文字英数字とハイフンのみ)のkeyを考えて
ください(例: "enoshima-toro"、"koide-gawa-higanbana-matsuri")。
catが'e'(イベント)以外の記事では、eventSeriesKeyは空文字にしてください。
"""
    else:
        series_block = """
【イベントシリーズ台帳(eventSeriesKeyについて・必須)】
まだ登録されているイベントシリーズはありません。catが'e'(イベント)の記事を書く
場合は、そのイベントの内容を表す新しいkebab-case(小文字英数字とハイフンのみ)の
eventSeriesKeyを考えてください(例: "enoshima-toro")。catが'e'以外の記事では
空文字にしてください。
"""

    return f"""あなたは地域メディア「湘南Doors」の編集者です。
対象エリアは次の8つに限定してください: {", ".join(AREAS)}
カテゴリは次のいずれかを使ってください: {json.dumps(CATS, ensure_ascii=False)}
{recent_block}
Web検索を使って、直近2週間以内に実際にあった湘南エリアのニュース、または
これから開催が確定している実在のイベント情報を{count}件調べてください。
架空の情報は絶対に作らないこと。

{SOURCE_POLICY_BLOCK}
{SUBJECT_POLICY_BLOCK}
"link"には、一次情報源のURLを入れること(見つからない場合は空文字)。

【店舗・企業を紹介する記事の場合】
カテゴリが b（企業・店舗）または g（グルメ）の記事では、上記の一次情報源から
住所・アクセス方法・営業時間・定休日・公式SNS（Instagram/Facebook/X/TikTok）も
確認して含めてください（確認できない項目は空文字で構いません）。
それ以外のカテゴリ（観光・人・文化・イベント・暮らし）では、これらは空文字で構いません。

【イベント記事(cat='e')の場合】
必ず一次情報源から実際の開催日を確認し、eventStartDate(開催初日、YYYY-MM-DD形式)を
埋めてください。複数日にわたって開催される場合はeventEndDate(最終日)も埋めてください。
単日開催の場合はeventEndDateは空文字のままで構いません。開催日が確認できない場合のみ、
eventStartDateを空文字にしてください(この場合、検索結果でのイベント情報表示の対象外に
なります)。日付は必ず情報源に明記されているものだけを使い、推測で埋めないこと。
{series_block}
本文(body)は4段落程度・合計1000文字以上とし、段落の区切りは\\n\\nで表現してください。
事実に基づき、湘南Doors編集部としての視点を交えた読み物として書くこと。
一次情報の文章も丸写しは禁止、必ず自分の言葉で書き直すこと。

【submit_articlesの提出形式について(必須・厳守)】
articlesは必ずJSON配列(Python側ではlistとして解釈される構造)として渡してください。
以下を厳守すること:
- articlesはツール入力スキーマ通り、配列(array)として渡す
- 配列をJSON文字列としてエンコードしない(articles全体を1本の文字列にしない)
- 配列全体、または配列を含む値全体を引用符で囲まない
- articlesの値として自然言語の説明文やMarkdownを入れない(記事オブジェクトのみを並べる)
- NEWS_ARTICLE_TOOLのスキーマに完全準拠すること
- {count}記事は、articles配列内の{count}個のオブジェクトとして提出する

正しい例:
"articles": [
  {{ "cat": "e", "area": "藤沢", ... }},
  {{ "cat": "g", "area": "鎌倉", ... }},
  {{ "cat": "t", "area": "逗子", ... }}
]

誤った例(これは絶対にしないこと):
"articles": "[{{...}}, {{...}}, {{...}}]"

調査・執筆が終わったら、必ず submit_articles ツールを使って{count}件まとめて提出してください。
"""


def call_claude_news(recent_titles, event_series=None, count=None):
    count = count or NEWS_ARTICLES_PER_DAY
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    system_prompt = build_news_system_prompt(recent_titles, event_series, count=count)

    messages = [{
        "role": "user",
        "content": f"本日分の{count}記事を、直近2週間以内のニュースまたは今後のイベント情報から作成し、submit_articlesツールで提出してください。",
    }]
    tools = [
        {"type": "web_search_20250305", "name": "web_search"},
        NEWS_ARTICLE_TOOL,
    ]

    for _ in range(10):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == "submit_articles":
                return block.input["articles"]

        if response.stop_reason == "pause_turn":
            # web_search等のサーバー側ツールで長時間実行中のターンが一時停止しただけで、
            # エラーではない。assistantの応答をそのまま積み、新しいuserメッセージは
            # 追加せず、同じtools定義を維持したまま次のループ(=次のAPI呼び出し)へ進む。
            messages.append({"role": "assistant", "content": response.content})
            continue

        if response.stop_reason != "tool_use":
            raise RuntimeError(
                "submit_articlesが呼ばれないまま終了しました。stop_reason="
                + str(response.stop_reason)
                + " content=" + repr(response.content)
            )

        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": "続けて調査を進め、準備ができ次第submit_articlesツールで提出してください。",
        })

    raise RuntimeError("10ターン以内にsubmit_articlesが呼ばれませんでした。")


def build_news_entry(item, today, article_id="today-run-pending-id"):
    """API側から返ってきた1件分の生データ(item)を、articles.jsonのスキーマに
    沿ったエントリへ変換する。article_id未指定の間は「まだIDが確定していない
    (=採用が最終決定していない)」状態を表し、id/slugは仮の値のままにする
    (working_setに積んだ際、is_duplicate等のログに「id:today-run-pending-id」
    と出ることで、既存記事ではなく今回のrun内で採用済みの記事だと分かるようにする)。
    IDが決まった時点でfinalize_news_entry_id()を呼んで確定させる。"""
    return {
        "id": article_id,
        "articleType": "news",
        "cat": item["cat"],
        "area": item["area"],
        "scene": item["scene"],
        "title": item["title"],
        "dek": item["dek"],
        "link": item.get("link") or "",
        "subjectNames": item.get("subjectNames") or [],
        "sources": item.get("sources") or [],
        "date": today,
        "eventStartDate": item.get("eventStartDate") or "",
        "eventEndDate": item.get("eventEndDate") or "",
        "eventSeriesKey": item.get("eventSeriesKey") or "",
        "address": item.get("address") or "",
        "access": item.get("access") or "",
        "hours": item.get("hours") or "",
        "closedDays": item.get("closedDays") or "",
        "snsLinks": {
            "instagram": item.get("instagram") or "",
            "facebook": item.get("facebook") or "",
            "x": item.get("x") or "",
            "tiktok": item.get("tiktok") or "",
        },
        "body": item["body"],
        "tags": item.get("tags", []),
        "slug": "",
    }


def finalize_news_entry_id(entry, article_id):
    """採用が確定したエントリにだけ、実際のarticle ID/slugを書き込む。
    reject/refillのために生成しただけの候補にはこの関数を一切呼ばないため、
    articles.jsonに保存される記事だけが連番のIDを持つことになる。"""
    entry["id"] = article_id
    entry["slug"] = f"{AREA_EN[entry['area']]}-{CAT_EN[entry['cat']]}-{article_id:04d}"
    return entry


def run_news_generation(existing_articles, today, event_series=None, gate_rejections=None,
                        count=None, max_refills=None, strict=True, label="news"):
    """ニュース/イベント型記事を生成する。戻り値: (accepted_entries, log_lines, event_series)
    致命的なエラー(API呼び出し失敗・レスポンス形状異常)はそのまま例外を送出する
    (このスクリプト全体を失敗させ、articles.jsonへの書き込みを行わせないため)。

    重複・スキーマ不正でrejectされた候補が出てcount件(既定NEWS_ARTICLES_PER_DAY)に
    満たない場合、不足分だけを追加生成するrefillを最大max_refills回
    (既定NEWS_REFILL_MAX_ATTEMPTS)まで試みる(件数チェック自体は緩めない。
    閾値未満のまま採用したり、記事を減らして公開したりはしない。最終判定は
    呼び出し元のmain()が行う)。

    article IDは「最終的に採用が確定した記事」にのみ、この関数の最後で
    まとめて発行する(reserve_ids)。reject/refillで捨てられた候補のために
    IDが消費されることはない。

    各候補は採用前に公開前監査(run_publish_gate)にかけ、不合格なら採用せず
    gate_rejections に記録する(不合格分もrefillの対象になる)。refillしても
    件数に届かなかった原因に公開前監査の不合格が含まれる場合だけは、合格した
    記事を公開するため、届かなかった分を欠いたまま採用分を返す。

    strict=False(目標件数への補充 top-up 用)では、届かなくても合格した分をそのまま返す。
    公開前監査の件数上限(MAX_GATE_DRAFTS_PER_RUN)に達したら、それ以上は生成しない。"""
    log_lines = []
    if gate_rejections is None:
        gate_rejections = []
    target = count if count is not None else NEWS_ARTICLES_PER_DAY
    max_refills = NEWS_REFILL_MAX_ATTEMPTS if max_refills is None else max_refills
    gate_rejected_before = len(gate_rejections)
    event_series = list(event_series or [])
    cutoff = (datetime.now(ZoneInfo("Asia/Tokyo")) - timedelta(days=90)).strftime("%Y-%m-%d")
    # 重複防止用の既存記事リストは90日で切らず全期間を渡す(タイトルだけでは
    # 表記ゆれで同一対象を見落とすため、対象名とURLも併記する)。
    recent_titles = [article_digest(a) for a in existing_articles]

    accepted = []             # 採用確定(ただしID未発行)のエントリ
    working_set = list(existing_articles)  # 重複チェック対象(既存記事+今回採用済み分)

    def process_batch(raw_items):
        """1回分のAPIレスポンスを検証し、通った候補をaccepted/working_setに積む。"""
        for item in raw_items:
            if len(accepted) >= target:
                break  # 想定より多く返ってきても、必要数を超えて採用はしない
            reason = validate_item(item)
            if reason:
                title_for_log = item.get("title", "(タイトル不明)") if isinstance(item, dict) else f"(dict以外: {type(item).__name__})"
                log_lines.append(f"スキップ({label}): 「{title_for_log}」— スキーマ不正: {reason}")
                continue
            dup_reason = is_duplicate(item, working_set, days=90)
            if dup_reason:
                log_lines.append(f"スキップ({label}): 「{item['title']}」— 重複疑い: {dup_reason}")
                continue
            series_dup_reason = check_series_year_duplicate(item, working_set)
            if series_dup_reason:
                log_lines.append(f"スキップ({label}): 「{item['title']}」— {series_dup_reason}")
                continue
            subject_dup_reason = find_same_subject(item, working_set)
            if subject_dup_reason:
                log_lines.append(f"スキップ({label}): 「{item['title']}」— 同一対象の既存記事あり: {subject_dup_reason}")
                continue

            entry = run_publish_gate(build_news_entry(item, today), label, gate_rejections, log_lines)
            if entry is None:
                continue
            accepted.append(entry)
            working_set.append(entry)  # 同一run内での重複(今回採用済み分との重複)も以後ここで検出される

    # ---- 初回生成 ----
    log_lines.append(f"{label}: 初回{target}件生成を試みます")
    raw_items = call_claude_news(recent_titles, event_series, count=target)
    raw_items = validate_response_shape(raw_items, label=label)
    if len(raw_items) != target:
        log_lines.append(f"警告({label}): 期待した{target}件ではなく{len(raw_items)}件が返されました")
    process_batch(raw_items)
    log_lines.append(f"{label}: {len(accepted)}/{target}件採用")

    # ---- 不足分だけをrefill(最大max_refills回。公開前監査の件数上限でも止める) ----
    attempt = 0
    while len(accepted) < target and attempt < max_refills:
        if gate_budget_exhausted():
            log_lines.append(f"警告({label}): 公開前監査の件数上限({MAX_GATE_DRAFTS_PER_RUN}件)に達したため、補充生成を打ち切ります")
            break
        attempt += 1
        shortfall = target - len(accepted)
        log_lines.append(f"{label}: {shortfall}件不足 → refill attempt {attempt}/{max_refills}")
        # 「今回のrunで既に採用済みの記事」も重複防止リストに含めることで、
        # refillが直前に採用した記事と同じ話題を提案してくる確率を下げる。
        refill_recent_titles = recent_titles + [article_digest(a) for a in accepted]
        refill_raw_items = call_claude_news(refill_recent_titles, event_series, count=shortfall)
        refill_raw_items = validate_response_shape(refill_raw_items, label=f"{label}_refill")
        log_lines.append(f"{label}: 追加で{len(refill_raw_items)}件生成しました(refill attempt {attempt}/{max_refills})")
        process_batch(refill_raw_items)
        log_lines.append(f"{label}: {len(accepted)}/{target}件採用")

    if len(accepted) >= target:
        log_lines.append(f"{label}: {len(accepted)}/{target}件採用完了")
    elif not strict:
        log_lines.append(f"警告({label}): 上限まで補充しても{target}件に届きませんでした。合格した{len(accepted)}件だけを公開します。")
    elif len(gate_rejections) > gate_rejected_before:
        # 不足の原因に公開前監査の不合格が含まれる。不合格記事を公開しないことが
        # 目的なので、合格した記事だけを公開する(件数合わせのために基準は緩めない)。
        log_lines.append(
            f"警告({label}): 公開前監査の不合格があり、retry上限({max_refills}回)後も"
            f"{target}件に届きませんでした。合格した{len(accepted)}件だけを公開します。"
        )
    else:
        log_lines.append(
            f"{label}: retry上限({max_refills}回)に達しても"
            f"{target}件に届きませんでした(最終 {len(accepted)}件)。"
            "articles.json / id_counterへは一切書き込みません。"
        )
        # ここでは例外を出さず、呼び出し元(main)の既存の件数チェックに判定を委ねる
        # (現在の仕様どおり、部分的な成功でもworkflow全体を失敗させるのはmain側の責務)。
        return [], log_lines, event_series

    # ---- 採用が確定した分だけ、まとめてIDを発行する ----
    # reject/refillで捨てられた候補はここに含まれないため、IDが無駄に消費されたり
    # 欠番が生じたりしない。articles.jsonに保存される記事だけが連番IDを持つ。
    reserved_ids = reserve_ids(len(accepted))
    for entry, new_id in zip(accepted, reserved_ids):
        finalize_news_entry_id(entry, new_id)
        event_series, added = register_event_series_if_new(entry, event_series)
        if added:
            log_lines.append(f"{label}: 新規イベントシリーズ「{entry['eventSeriesKey']}」を台帳に追加しました")

    log_lines.append(
        f"{label}: {len(accepted)}/{target}件を採用しました (id:{[a['id'] for a in accepted]})"
    )
    return accepted, log_lines, event_series


# ============================================================
# ストックSEO型記事の生成(新規)
# ============================================================

STOCK_TOPIC_REFILL_TOOL = {
    "name": "submit_new_stock_topics",
    "description": "新しいストックSEOテーマ候補を提出する。",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "topics": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "query": {"type": "string", "description": "想定される検索クエリ(例: 「茅ヶ崎 デート」)"},
                        "titleIdea": {"type": "string", "description": "記事タイトル案"},
                        "area": {"type": "string", "enum": AREAS},
                        "category": {"type": "string", "enum": list(CATS.keys())},
                        "searchIntent": {"type": "string", "description": "検索者が何を知りたくて検索しているかの説明"},
                    },
                    "required": ["query", "titleIdea", "area", "category", "searchIntent"],
                },
            },
        },
        "required": ["topics"],
    },
}

STOCK_DECISION_TOOL = {
    "name": "submit_stock_decisions",
    "description": "各ストック候補テーマについて、採用(write)するか見送る(skip)かを判定し、"
                    "採用する場合は記事本文もあわせて提出する。",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "topicId": {"type": "string", "description": "評価対象の候補テーマのid"},
                        "decision": {"type": "string", "enum": ["write", "skip"]},
                        "skipReason": {"type": "string", "description": "decisionがskipの場合の理由。writeの場合は空文字でよい"},
                        "article": {
                            "type": "object",
                            "description": "decisionがwriteの場合のみ使用する。skipの場合は各項目を空文字/空配列にしてよい",
                            "additionalProperties": False,
                            "properties": {
                                "cat": {"type": "string", "enum": list(CATS.keys())},
                                "area": {"type": "string", "enum": AREAS},
                                "scene": {"type": "string", "enum": SCENES},
                                "title": {"type": "string"},
                                "dek": {"type": "string"},
                                "body": {"type": "string"},
                                "tags": {"type": "array", "items": {"type": "string"}},
                                "link": {"type": "string"},
                                "subjectNames": {"type": "array", "items": {"type": "string"}},
                                "sources": {"type": "array", "items": {"type": "string"}},
                                "address": {"type": "string"},
                                "access": {"type": "string"},
                                "hours": {"type": "string"},
                                "closedDays": {"type": "string"},
                                "instagram": {"type": "string"},
                                "facebook": {"type": "string"},
                                "x": {"type": "string"},
                                "tiktok": {"type": "string"},
                            },
                            "required": [
                                "cat", "area", "scene", "title", "dek", "body", "tags", "link",
                                "subjectNames", "sources",
                                "address", "access", "hours", "closedDays",
                                "instagram", "facebook", "x", "tiktok",
                            ],
                        },
                    },
                    "required": ["topicId", "decision", "skipReason", "article"],
                },
            },
        },
        "required": ["decisions"],
    },
}


def load_stock_topics():
    if not os.path.exists(STOCK_TOPICS_PATH):
        return []
    return load_json(STOCK_TOPICS_PATH)


def build_cannibalization_context(existing_articles, stock_topics):
    """カニバリ判定用に、既存記事とテーマ台帳の要約テキストを作る。
    ストック記事は日付を問わず読まれ続けるため、newsのような90日カットオフは
    設けず全期間を対象にする。"""
    article_lines = [
        f"- [{a.get('articleType','?')}/{a['area']}/{CATS.get(a['cat'],a['cat'])}] "
        f"{article_digest(a)} (tags: {', '.join(a.get('tags', []))})"
        for a in existing_articles
    ]
    topic_lines = [
        f"- id={t['id']} status={t['status']} query=「{t['query']}」 "
        f"intent={t['searchIntent']} area={t['area']} category={CATS.get(t['category'], t['category'])}"
        for t in stock_topics
    ]
    return "\n".join(article_lines), "\n".join(topic_lines)


def build_stock_refill_prompt(existing_articles, stock_topics):
    article_summary, topic_summary = build_cannibalization_context(existing_articles, stock_topics)
    return f"""あなたは地域メディア「湘南Doors」のSEO担当編集者です。
このメディアは湘南エリア({", ".join(AREAS)})の情報を発信しており、
カテゴリは次の7種類のみを使います: {json.dumps(CATS, ensure_ascii=False)}
(新しいカテゴリを作らないこと。既存のこの7種類の中から選ぶこと)

あなたの仕事は、時間が経っても検索され続ける「ストックSEO記事」のテーマ候補を
新たに{MAX_NEW_STOCK_TOPICS_PER_REFILL}件以内で考え、submit_new_stock_topicsツールで提出することです。
実際に記事を書くのは別の担当者なので、ここではテーマの提案だけを行ってください。

【良いストックテーマの条件】
- 実際に検索されそうな、具体的な検索クエリが想定できること(例:「茅ヶ崎 デート」「藤沢 ランチ」)
- 検索意図が明確であること(検索した人が何を知りたいのか)
- 湘南Doorsとして独自の価値を出せること(単なる一般論で終わらないこと)
- 短期間で価値が失われない(evergreen)こと。特定の日付・開催期間に依存するテーマは禁止
  (そういうテーマはニュース記事の領分です)

【既存記事一覧(重複回避のため必ず確認すること)】
{article_summary if article_summary else "(まだ記事がありません)"}

【現在のテーマ台帳(重複回避のため必ず確認すること)】
{topic_summary if topic_summary else "(まだテーマがありません)"}

上記の既存記事・既存テーマと検索意図が重複しないテーマを考えてください。
「茅ヶ崎 デートスポット」と「茅ヶ崎 カップル おすすめスポット」のように、
表現は違っても検索意図が実質同じものは重複とみなし、避けてください。

【submit_new_stock_topicsの提出形式について(必須・厳守)】
topicsは必ずJSON配列(Python側ではlistとして解釈される構造)として渡してください。
配列をJSON文字列としてエンコードしない、配列全体を引用符で囲まない、
topicsの値として自然言語の説明文を入れないこと。スキーマに完全準拠すること。

正しい例:
"topics": [
  {{ "query": "...", "titleIdea": "...", ... }},
  {{ "query": "...", "titleIdea": "...", ... }}
]

誤った例(これは絶対にしないこと):
"topics": "[{{...}}, {{...}}]"

考え終えたら、必ず submit_new_stock_topics ツールを使って提出してください。
"""


def call_claude_stock_refill(existing_articles, stock_topics):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    system_prompt = build_stock_refill_prompt(existing_articles, stock_topics)

    messages = [{
        "role": "user",
        "content": "新しいストックSEOテーマ候補を考え、submit_new_stock_topicsツールで提出してください。",
    }]
    tools = [STOCK_TOPIC_REFILL_TOOL]

    for _ in range(MAX_STOCK_REFILL_TURNS):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == "submit_new_stock_topics":
                return block.input["topics"]

        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue

        if response.stop_reason != "tool_use":
            raise RuntimeError(
                "submit_new_stock_topicsが呼ばれないまま終了しました。stop_reason="
                + str(response.stop_reason)
            )
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": "続けてsubmit_new_stock_topicsツールで提出してください。"})

    raise RuntimeError(f"{MAX_STOCK_REFILL_TURNS}ターン以内にsubmit_new_stock_topicsが呼ばれませんでした。")


def refill_stock_topics_if_needed(existing_articles, stock_topics, log_lines):
    """候補(status=candidate)が閾値未満なら、AIに新しい候補を1回だけ補充させる。
    失敗しても(この関数の外側で)ストック生成全体を失敗させない設計にする。"""
    candidate_count = sum(1 for t in stock_topics if t["status"] == "candidate")
    if candidate_count >= STOCK_TOPIC_REFILL_THRESHOLD:
        log_lines.append(f"stock: 候補テーマが{candidate_count}件あり閾値({STOCK_TOPIC_REFILL_THRESHOLD})以上のため補充は行いません")
        return stock_topics, 0

    log_lines.append(f"stock: 候補テーマが{candidate_count}件(閾値{STOCK_TOPIC_REFILL_THRESHOLD}未満)のため、新規テーマの補充を試みます")
    new_topics_raw = call_claude_stock_refill(existing_articles, stock_topics)
    new_topics_raw = validate_response_shape(new_topics_raw, label="stock_refill", max_items=MAX_NEW_STOCK_TOPICS_PER_REFILL)

    existing_queries = {normalize(t["query"]) for t in stock_topics}
    next_num = len(stock_topics) + 1
    added = 0
    for item in new_topics_raw:
        if not isinstance(item, dict):
            continue
        required = ["query", "titleIdea", "area", "category", "searchIntent"]
        if any(k not in item for k in required):
            continue
        if item["area"] not in AREAS or item["category"] not in CATS:
            continue
        if normalize(item["query"]) in existing_queries:
            continue  # 完全一致の重複クエリは追加しない(意味的な重複判定はAI側の役目)
        stock_topics.append({
            "id": f"t{next_num:04d}",
            "query": item["query"],
            "titleIdea": item["titleIdea"],
            "area": item["area"],
            "category": item["category"],
            "searchIntent": item["searchIntent"],
            "status": "candidate",
            "generatedArticleId": None,
            "generatedAt": None,
            "skipReason": None,
            "createdAt": datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d"),
        })
        existing_queries.add(normalize(item["query"]))
        next_num += 1
        added += 1

    log_lines.append(f"stock: 新規候補テーマを{added}件、台帳に追加しました")
    return stock_topics, added


def build_stock_selection_prompt(candidates, existing_articles, stock_topics):
    article_summary, topic_summary = build_cannibalization_context(existing_articles, stock_topics)
    candidates_block = "\n".join(
        f"- id={t['id']} query=「{t['query']}」 titleIdea=「{t['titleIdea']}」 "
        f"area={t['area']} category={CATS.get(t['category'], t['category'])} intent={t['searchIntent']}"
        for t in candidates
    )
    return f"""あなたは地域メディア「湘南Doors」の編集者です。
対象エリアは次の8つに限定してください: {", ".join(AREAS)}
カテゴリは次のいずれかを使ってください: {json.dumps(CATS, ensure_ascii=False)}

以下は、時間が経っても検索され続けることを狙った「ストックSEO記事」の候補テーマです。
本日はこの中から最大{STOCK_ARTICLES_PER_DAY}件を選び、記事として書き上げてください。

【候補テーマ】
{candidates_block}

【既存記事一覧(カニバリゼーション確認用)】
{article_summary if article_summary else "(まだ記事がありません)"}

【テーマ台帳全体(カニバリゼーション確認用)】
{topic_summary if topic_summary else "(まだテーマがありません)"}

各候補テーマについて、必ず以下7点を評価してから採否(write/skip)を決定してください。
  ① 想定検索クエリとして現実的か
  ② 検索意図が明確か
  ③ 既存記事・既存テーマと検索意図が重複していないか
     (表現が違っても実質同じ検索意図なら重複とみなすこと。例:
      「茅ヶ崎 デートスポット」と「茅ヶ崎 カップル おすすめスポット」は重複)
  ④ 湘南Doorsとして独自の価値を出せるか(一般論の寄せ集めで終わらないか)
  ⑤ 対象エリアが妥当か
  ⑥ 対象カテゴリが妥当か(新しいカテゴリを作らないこと)
  ⑦ evergreen性(特定の日付・開催期間に依存せず、長期間価値を保てるか)

重複していたり、上記の基準を満たさない候補は無理に採用せず"skip"とし、
skipReasonに理由を書いてください。良い候補が1件しか無ければ1件だけ、
0件であれば0件の採用でも構いません(件数を埋めるために基準を下げないこと)。

採用(write)と判定した候補については、Web検索を使って必要な事実確認を行った上で、
本文(body)4段落程度・合計1000文字以上の記事として書き上げてください。段落の区切りは
\\n\\nで表現すること。特定の日付に依存する内容(「9月X日開催」等)は書かないこと
(evergreenなストック記事のため)。カテゴリがb(企業・店舗)またはg(グルメ)の場合は
住所・アクセス・営業時間・定休日・公式SNSも一次情報源から確認して埋めてください
(確認できない項目は空文字で構いません)。一次情報の文章も丸写しは禁止です。
{SOURCE_POLICY_BLOCK}
{SUBJECT_POLICY_BLOCK}
【submit_stock_decisionsの提出形式について(必須・厳守)】
decisionsは必ずJSON配列(Python側ではlistとして解釈される構造)として渡してください。
配列をJSON文字列としてエンコードしない、配列全体を引用符で囲まない、
decisionsの値として自然言語の説明文を入れないこと(skipReason等の各文字列フィールドの
中身は自然言語で構いませんが、decisions自体は配列のままにすること)。スキーマに完全準拠すること。

正しい例:
"decisions": [
  {{ "topicId": "t0001", "decision": "write", ... }},
  {{ "topicId": "t0002", "decision": "skip", ... }}
]

誤った例(これは絶対にしないこと):
"decisions": "[{{...}}, {{...}}]"

評価・執筆が終わったら、候補テーマ全件について(write/skip問わず)
submit_stock_decisions ツールで提出してください。
"""


def call_claude_stock_selection(candidates, existing_articles, stock_topics):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    system_prompt = build_stock_selection_prompt(candidates, existing_articles, stock_topics)

    messages = [{
        "role": "user",
        "content": f"候補テーマを評価し、最大{STOCK_ARTICLES_PER_DAY}件を選んで執筆したうえで、"
                    "submit_stock_decisionsツールで全候補の判定結果を提出してください。",
    }]
    tools = [
        {"type": "web_search_20250305", "name": "web_search"},
        STOCK_DECISION_TOOL,
    ]

    for _ in range(MAX_STOCK_SELECTION_TURNS):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == "submit_stock_decisions":
                return block.input["decisions"]

        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue

        if response.stop_reason != "tool_use":
            raise RuntimeError(
                "submit_stock_decisionsが呼ばれないまま終了しました。stop_reason="
                + str(response.stop_reason)
            )
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": "続けてsubmit_stock_decisionsツールで提出してください。"})

    raise RuntimeError(f"{MAX_STOCK_SELECTION_TURNS}ターン以内にsubmit_stock_decisionsが呼ばれませんでした。")


def run_stock_generation(existing_articles, stock_topics, today, log_lines, gate_rejections=None):
    """ストックSEO型記事を生成する。戻り値: (accepted_entries, updated_stock_topics)

    ニュース生成とは異なり、この関数内のあらゆる失敗(API呼び出し失敗・
    レスポンス形状異常など)は呼び出し元で捕捉され、『今日はstockを0件』
    として扱われる(newsの正常な生成・commitを妨げないため)。"""
    if gate_rejections is None:
        gate_rejections = []
    stock_topics, _ = refill_stock_topics_if_needed(existing_articles, stock_topics, log_lines)

    candidates = [t for t in stock_topics if t["status"] == "candidate"][:MAX_STOCK_CANDIDATES_IN_PROMPT]
    if not candidates:
        log_lines.append("stock: 評価可能な候補テーマが0件のため、本日のストック記事生成は見送ります")
        return [], stock_topics

    decisions_raw = call_claude_stock_selection(candidates, existing_articles, stock_topics)
    decisions_raw = validate_response_shape(decisions_raw, label="stock_decisions", max_items=len(candidates) + 5)

    candidates_by_id = {t["id"]: t for t in candidates}
    accepted = []
    working_set = list(existing_articles)
    write_count = 0
    now_str = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M")

    for decision in decisions_raw:
        if not isinstance(decision, dict):
            log_lines.append(f"stock: 不正な判定オブジェクトをスキップしました(型: {type(decision).__name__})")
            continue
        topic_id = decision.get("topicId")
        topic = candidates_by_id.get(topic_id)
        if topic is None:
            log_lines.append(f"stock: 未知のtopicId「{topic_id}」の判定をスキップしました")
            continue

        if decision.get("decision") != "write":
            reason = decision.get("skipReason") or "(理由の記載なし)"
            topic["status"] = "skipped"
            topic["skipReason"] = reason
            log_lines.append(f"stock: スキップ「{topic['query']}」— {reason}")
            continue

        if write_count >= STOCK_ARTICLES_PER_DAY:
            log_lines.append(f"stock: 「{topic['query']}」は採用判定でしたが、本日の上限({STOCK_ARTICLES_PER_DAY}件)に達したため見送りました")
            continue

        article = decision.get("article")
        reason = validate_item(article, require_event_fields=False)
        if reason:
            log_lines.append(f"stock: 「{topic['query']}」— 記事のスキーマ不正のため見送り: {reason}")
            continue
        dup_reason = is_duplicate(article, working_set, days=None)
        if dup_reason:
            log_lines.append(f"stock: 「{topic['query']}」— 重複疑いのため見送り: {dup_reason}")
            continue
        subject_dup_reason = find_same_subject(article, working_set)
        if subject_dup_reason:
            topic["status"] = "skipped"
            topic["skipReason"] = f"同一対象の既存記事あり: {subject_dup_reason}"
            log_lines.append(f"stock: 「{topic['query']}」— 同一対象の既存記事ありのため見送り: {subject_dup_reason}")
            continue

        entry = {
            "id": "today-run-pending-id",
            "articleType": "stock",
            "cat": article["cat"],
            "area": article["area"],
            "scene": article["scene"],
            "title": article["title"],
            "dek": article["dek"],
            "link": article.get("link") or "",
            "subjectNames": article.get("subjectNames") or [],
            "sources": article.get("sources") or [],
            "date": today,
            "eventStartDate": "",
            "eventEndDate": "",
            "address": article.get("address") or "",
            "access": article.get("access") or "",
            "hours": article.get("hours") or "",
            "closedDays": article.get("closedDays") or "",
            "snsLinks": {
                "instagram": article.get("instagram") or "",
                "facebook": article.get("facebook") or "",
                "x": article.get("x") or "",
                "tiktok": article.get("tiktok") or "",
            },
            "body": article["body"],
            "tags": article.get("tags", []),
            "slug": "",
        }

        # 公開前監査。不合格の記事は公開せず、同じテーマを毎日選び直さないよう
        # テーマ台帳ではskipped扱いにする(理由はskipReasonに残す)。
        # 監査件数の上限に達していたら、テーマは候補のまま残して翌日以降に回す。
        if gate_budget_exhausted():
            log_lines.append(f"stock: 「{topic['query']}」— 公開前監査の件数上限({MAX_GATE_DRAFTS_PER_RUN}件)に達したため見送り(候補のまま残します)")
            continue
        entry = run_publish_gate(entry, "stock", gate_rejections, log_lines)
        if entry is None:
            topic["status"] = "skipped"
            topic["skipReason"] = "公開前監査で不合格: " + " / ".join(gate_rejections[-1]["reasons"])
            continue

        # ここまでの検証(write判定・スキーマ・重複チェック・公開前監査)をすべて通過し、
        # articles.jsonへ実際に追加することが確定した記事についてのみ、
        # その場でID を1件予約する。上限2件を予約してから絞り込む方式だと、
        # 1件しか採用されない日・0件の日に不要な欠番が積み上がってしまうため、
        # 「確定した分だけ消費する」設計に変更している。
        new_id = reserve_ids(1)[0]
        entry["id"] = new_id
        entry["slug"] = f"{AREA_EN[article['area']]}-{CAT_EN[article['cat']]}-{new_id:04d}"
        accepted.append(entry)
        working_set.append(entry)
        write_count += 1

        topic["status"] = "generated"
        topic["generatedArticleId"] = new_id
        topic["generatedAt"] = now_str
        log_lines.append(f"stock: 採用「{topic['query']}」→ id={new_id}")

    log_lines.append(f"stock: {len(accepted)}/{STOCK_ARTICLES_PER_DAY}件を採用しました (id:{[a['id'] for a in accepted]})")
    return accepted, stock_topics


# ============================================================
# 共通ユーティリティ
# ============================================================

def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def atomic_write_json(path, data):
    """tmpファイルに書いてからos.replaceで置換する。
    途中でプロセスが落ちても、既存ファイルが壊れかけの内容で上書きされることはない。"""
    dir_ = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def reserve_ids(count):
    """id_counter.json から count 件分のIDを予約し、カウンタを即座に前進させる。
    news/stockどちらからも呼ばれるが、同じ台帳を使って原子的に発行するため、
    IDが重複することはない。この後の処理が何であれ失敗しても、予約したIDが
    将来再利用されることはない(欠番になるだけ)。"""
    counter = load_json(ID_COUNTER_PATH)
    start = counter["next_id"]
    ids = list(range(start, start + count))
    counter["next_id"] = start + count
    atomic_write_json(ID_COUNTER_PATH, counter)
    return ids


# ---------- スキーマ検証 ----------

def validate_item(item, require_event_fields=True):
    """1件分の記事データがarticles.jsonのschemaに適合しているか検証する。
    不正なら理由の文字列を返し、問題なければNoneを返す。
    require_event_fields=False の場合、eventStartDate/eventEndDateのチェックを行わない
    (ストック記事はそもそもこれらのキー自体を送ってこないため)。"""
    if not isinstance(item, dict):
        return f"記事オブジェクトがdictではない(型: {type(item).__name__})"
    required = ["cat", "area", "scene", "title", "dek", "body", "tags", "link"]
    for key in required:
        if key not in item:
            return f"必須フィールド'{key}'が欠落"
    if item["cat"] not in CATS:
        return f"不正なcat: {item['cat']}"
    if item["area"] not in AREAS:
        return f"不正なarea: {item['area']}"
    if item["scene"] not in SCENES:
        return f"不正なscene: {item['scene']}"
    if not isinstance(item["title"], str) or len(item["title"].strip()) < 3:
        return "titleが空または短すぎる"
    if not isinstance(item["body"], str) or len(item["body"].strip()) < 200:
        return "bodyが空または短すぎる(200文字未満)"
    if not isinstance(item.get("tags"), list):
        return "tagsが配列でない"
    src_reason = check_source_policy(item)
    if src_reason:
        return src_reason
    if require_event_fields:
        for key in ("eventStartDate", "eventEndDate"):
            val = item.get(key) or ""
            if val and not re.match(r"^\d{4}-\d{2}-\d{2}$", val):
                return f"{key}の形式が不正(YYYY-MM-DD形式である必要): {val}"
    return None


# ---------- 情報源ポリシーのチェック ----------

def _domain(url):
    m = re.match(r"^[a-z]+://([^/?#]+)", (url or "").strip().lower())
    return m.group(1) if m else ""


def is_secondary_media(url):
    d = _domain(url)
    return bool(d) and any(d == m or d.endswith("." + m) or m in d for m in SECONDARY_MEDIA_DOMAINS)


def check_source_policy(item):
    """一次情報のみを使っているかを機械的に検証する。違反なら理由を返す。"""
    sources = item.get("sources")
    if not isinstance(sources, list) or not [u for u in sources if isinstance(u, str) and u.strip()]:
        return "sources(一次情報URL)が空"
    for u in sources + [item.get("link") or ""]:
        if is_secondary_media(u):
            return f"他メディアのURLが情報源に含まれている: {u}"
    body = item.get("body") or ""
    for pat in QUOTE_ATTRIBUTION_PATTERNS:
        m = re.search(pat, body)
        if m:
            return f"人物の発言の引用を含む(他メディアの取材コメント流用防止): 「{m.group(0)}」"
    names = item.get("subjectNames")
    if not isinstance(names, list) or not [n for n in names if isinstance(n, str) and n.strip()]:
        return "subjectNames(記事の対象名)が空"
    return None


# ---------- 同一対象チェック(全期間) ----------
BODY_SIM_THRESHOLD = 0.35        # 同エリア記事との本文類似度(文字bigram Jaccard)
MIN_SUBJECT_NAME_LEN = 3         # これより短い対象名は一般語と衝突しやすいので照合しない


def normalize_name(s):
    s = (s or "").lower()
    return re.sub(r'[\s\u3000・.,、。!！?？"“”「」『』()（）\-–—_/|｜:：]', '', s)


def normalize_url(u):
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = re.sub(r"[?#].*$", "", u)
    return u.rstrip("/")


def article_digest(a):
    """プロンプトに渡す既存記事1行分(タイトル+対象名+一次情報URL)。"""
    names = "/".join(a.get("subjectNames") or [])
    parts = [a.get("title", "")]
    if names:
        parts.append(f"対象: {names}")
    if a.get("link"):
        parts.append(f"URL: {a['link']}")
    return " | ".join(parts)


def _same_event_other_year(item, existing):
    """毎年開催イベントの別年度の回なら、同一対象でも別記事として許容する。"""
    return item.get("cat") == "e" and existing.get("cat") == "e" and year_of(item) != year_of(existing)


def find_same_subject(item, existing_articles):
    """「1つの対象につき記事は1本」を機械的に担保する。期間は絞らない(全期間)。
    次のいずれかに該当する既存記事があれば理由文字列を返す。
      1) 一次情報URL(link)が同じ
      2) 記事の対象名(subjectNamesの表記ゆれ含む)が、既存記事のsubjectNames・
         タイトル・リードのいずれかに含まれる
      3) 同エリアで本文の類似度がBODY_SIM_THRESHOLD以上
    """
    new_link = normalize_url(item.get("link"))
    new_names = {normalize_name(n) for n in (item.get("subjectNames") or [])}
    new_names = {n for n in new_names if len(n) >= MIN_SUBJECT_NAME_LEN}
    for ex in existing_articles:
        if _same_event_other_year(item, ex):
            continue
        ex_id = ex.get("id")
        if new_link and new_link == normalize_url(ex.get("link")):
            return f"一次情報URLが同じ(既存記事id:{ex_id})"
        ex_names = {normalize_name(n) for n in (ex.get("subjectNames") or [])}
        ex_text = normalize_name(ex.get("title", "") + ex.get("dek", ""))
        for n in new_names:
            if n in ex_names or n in ex_text:
                return f"対象名「{n}」が既存記事id:{ex_id}と一致"
        if ex.get("area") == item.get("area"):
            sim = _text_similarity(item.get("body", ""), ex.get("body", ""))
            if sim >= BODY_SIM_THRESHOLD:
                return f"本文の類似度が高い(既存記事id:{ex_id}, 類似度{sim:.2f})"
    return None


# ---------- 重複記事チェック ----------

def normalize(s):
    return re.sub(r"\s+", "", (s or "")).lower()


def is_duplicate(item, existing_articles, days=90):
    """タイトル完全一致、またはタグの重なりが大きい同エリア記事を「重複疑い」として検出する。
    完全な意味的重複判定ではなく、あくまで明らかな重複を弾くための保険。
    days=None の場合は期間を絞らず全期間を対象にする(ストック記事はevergreenなため、
    90日以上前の記事とも重複しうる)。"""
    cutoff = None
    if days is not None:
        cutoff = (datetime.now(ZoneInfo("Asia/Tokyo")) - timedelta(days=days)).strftime("%Y-%m-%d")
    new_title = normalize(item["title"])
    new_tags = set(item.get("tags", []))

    for existing in existing_articles:
        if cutoff is not None and existing.get("date", "") < cutoff:
            continue
        if normalize(existing["title"]) == new_title:
            return f"タイトル完全一致(既存記事id:{existing['id']})"
        if existing["area"] == item["area"] and new_tags and set(existing.get("tags", [])):
            overlap = len(new_tags & set(existing["tags"])) / len(new_tags | set(existing["tags"]))
            if overlap >= 0.7:
                return f"タグの重なりが大きい(既存記事id:{existing['id']}, 重複率{overlap:.0%})"
    return None


# ---------- Phase 4: 同一イベントシリーズ・同一年度の重複判定 ----------
# 「同一eventSeriesKey + 同一年度」だけでは無条件でreject しない。
# タイトル/dek/タグのいずれかの類似度が閾値を超えた場合のみ「検索意図が
# 同一」とみなして重複扱いにする(閾値未満なら、同じイベント・同じ年度でも
# 異なる切り口の記事として許容する)。
SERIES_TITLE_SIM_THRESHOLD = 0.30
SERIES_DEK_SIM_THRESHOLD = 0.30
SERIES_TAG_SIM_THRESHOLD = 0.25


def _char_bigrams(s):
    s = (s or "").replace(" ", "").replace("\u3000", "")
    return set(s[i:i + 2] for i in range(len(s) - 1))


def _text_similarity(a, b):
    """日本語向けの軽量な類似度指標(文字bigramのJaccard係数)。
    形態素解析器を導入せずに、タイトル/dekの表現ゆれをある程度吸収できる。"""
    A, B = _char_bigrams(a), _char_bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def year_of(article):
    """記事の「年度」を、eventStartDateがあればそこから、無ければdateから算出する。"""
    if article.get("eventStartDate"):
        return article["eventStartDate"][:4]
    return (article.get("date") or "")[:4]


def check_series_year_duplicate(item, existing_articles):
    """同一eventSeriesKey・同一年度の既存記事の中に、タイトル/dek/タグの
    類似度が閾値を超えるもの(=検索意図が実質同一と判定できるもの)があれば
    理由文字列を返す。無ければNoneを返す(同シリーズ・同年度でも許容)。"""
    series_key = item.get("eventSeriesKey")
    if not series_key:
        return None
    item_year = year_of(item)
    if not item_year:
        return None

    item_tags = set(item.get("tags", []))
    for existing in existing_articles:
        if existing.get("eventSeriesKey") != series_key:
            continue
        if year_of(existing) != item_year:
            continue

        title_sim = _text_similarity(item.get("title", ""), existing.get("title", ""))
        dek_sim = _text_similarity(item.get("dek", ""), existing.get("dek", ""))
        existing_tags = set(existing.get("tags", []))
        tag_sim = 0.0
        if item_tags and existing_tags:
            tag_sim = len(item_tags & existing_tags) / len(item_tags | existing_tags)

        if (title_sim >= SERIES_TITLE_SIM_THRESHOLD
                or dek_sim >= SERIES_DEK_SIM_THRESHOLD
                or tag_sim >= SERIES_TAG_SIM_THRESHOLD):
            return (
                f"同一イベントシリーズ(eventSeriesKey={series_key})・同一年度({item_year})の"
                f"既存記事id:{existing['id']}と検索意図が同一と判定"
                f"(title類似度{title_sim:.2f}, dek類似度{dek_sim:.2f}, tag類似度{tag_sim:.2f})"
            )
    return None


# ---------- Phase 4: イベントシリーズ台帳の読み書き ----------

def load_event_series():
    if not os.path.exists(EVENT_SERIES_PATH):
        return []
    return load_json(EVENT_SERIES_PATH)


def register_event_series_if_new(item, event_series):
    """記事が新しいeventSeriesKeyを使っていれば、台帳へ追記する。
    既存キーの場合は何もしない(台帳の内容を書き換えない)。"""
    series_key = item.get("eventSeriesKey")
    if not series_key or item.get("cat") != "e":
        return event_series, False
    if any(s["seriesKey"] == series_key for s in event_series):
        return event_series, False
    if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", series_key):
        # kebab-case以外の値は登録しない(将来の自動運用で扱いに困る形式を弾く保険)。
        # ただし記事自体の生成・保存は妨げない。
        return event_series, False
    event_series.append({
        "seriesKey": series_key,
        "canonicalName": item.get("title", ""),
        "aliases": [],
        "area": item.get("area", ""),
        "category": item.get("cat", ""),
        "createdAt": datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d"),
    })
    return event_series, True



# ---------- 公開前監査ゲート ----------

_gate_client = None
_gate_drafts_checked = 0  # この実行で公開前監査にかけたドラフト数(MAX_GATE_DRAFTS_PER_RUN と比べる)


def gate_budget_exhausted():
    """公開前監査の件数上限に達したか。達したら以後のドラフトは生成・監査しない(公開もしない)。"""
    import publish_gate

    return publish_gate.enabled() and _gate_drafts_checked >= MAX_GATE_DRAFTS_PER_RUN


def run_publish_gate(entry, article_type, gate_rejections, log_lines):
    """ドラフトを公開前監査(publish_gate.check_draft)にかける。
    合格なら公開してよいentry(自動修正した場合は修正後)を、不合格ならNoneを返し、
    理由とclaimをgate_rejectionsに記録する。監査自体に失敗した記事も不合格(公開しない)。
    監査件数の上限に達していたら、監査せずにNoneを返す(品質の不合格ではないので記録しない)。"""
    global _gate_client, _gate_drafts_checked
    import publish_gate  # fact_auditがこのモジュールをimportするため、循環しないよう遅延import

    if not publish_gate.enabled():
        return entry
    if gate_budget_exhausted():
        log_lines.append(f"スキップ({article_type}): 「{entry.get('title', '')}」— 公開前監査の件数上限"
                         f"({MAX_GATE_DRAFTS_PER_RUN}件)に達したため監査せず、公開しません")
        return None
    if _gate_client is None:
        _gate_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    _gate_drafts_checked += 1
    gate = publish_gate.check_draft(entry, client=_gate_client)
    title = entry.get("title", "")
    if gate["passed"]:
        if gate.get("autofix"):
            changes = ", ".join(f"「{f['from']}」→「{f['to'] or '(削除)'}」" for f in gate["autofix"])
            log_lines.append(f"{article_type}: 公開前監査で自動修正し、再監査で合格: 「{title}」({changes})")
        else:
            log_lines.append(f"{article_type}: 公開前監査に合格: 「{title}」")
        return gate["entry"]
    gate_rejections.append(publish_gate.rejection_record(entry, gate, article_type))
    log_lines.append(f"スキップ({article_type}): 「{title}」— 公開前監査で不合格: {' / '.join(gate['reasons'])}")
    return None


def write_gate_summary(gate_rejections):
    """不合格記事をActionsのジョブサマリーと警告に出す(Issue化されなくてもログで追えるようにする)。"""
    for r in gate_rejections:
        print(f"::warning::公開前監査で不合格のため公開しません: 「{r['title']}」— {' / '.join(r['reasons'])}")
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path or not gate_rejections:
        return
    lines = [f"## 公開前監査で不合格の記事({len(gate_rejections)}件・公開していません)", ""]
    for r in gate_rejections:
        lines.append(f"### [{r['articleType']}] {r['title']}")
        lines.append(f"- 判定: {r.get('verdict')} / 理由: {' / '.join(r['reasons'])}")
        for c in r.get("claims", []):
            lines.append(f"- {c.get('status')}({c.get('role')}): {c.get('claim')} "
                         f"記事「{c.get('articleValue')}」/ 一次情報「{c.get('primaryValue')}」 {c.get('primaryUrl')}")
        lines.append("")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        print(f"警告: ジョブサマリーの書き込みに失敗しました: {e}", file=sys.stderr)


def shortfall_info(total, stock_count, gate_rejected):
    """公開件数が最低ライン(DAILY_MIN_ARTICLES)に届かなかった場合の記録。届いていればNone。
    件数合わせのために不合格記事を公開することはしないので、理由を残して人が判断できるようにする。"""
    if total >= DAILY_MIN_ARTICLES:
        return None
    reasons = []
    if gate_rejected:
        reasons.append(f"公開前監査で不合格 {len(gate_rejected)}件(品質基準は緩めずに見送り)")
    if stock_count == 0:
        reasons.append("stockは重複判定・見送り・不合格などで0件")
    if gate_budget_exhausted():
        reasons.append(f"公開前監査の件数上限({MAX_GATE_DRAFTS_PER_RUN}件)に到達")
    reasons.append(f"news補充(refill {NEWS_REFILL_MAX_ATTEMPTS}回・top-up {DAILY_TOPUP_MAX_ATTEMPTS}回)の上限まで試行")
    return {"total": total, "min": DAILY_MIN_ARTICLES, "target": DAILY_TARGET_ARTICLES, "reasons": reasons}


def write_shortfall_summary(shortfall, today):
    """最低件数に届かなかったことを警告とジョブサマリーに出す(Issue化は report_gate_rejections.py)。"""
    if not shortfall:
        return
    msg = (f"{today} の公開は{shortfall['total']}件で、最低{shortfall['min']}件に届きませんでした"
           f"(目標{shortfall['target']}件)。不合格記事は公開していません。理由: {' / '.join(shortfall['reasons'])}")
    print(f"::warning::{msg}")
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"## 公開件数が最低ラインに未達\n\n{msg}\n\n")
    except OSError as e:
        print(f"警告: ジョブサマリーの書き込みに失敗しました: {e}", file=sys.stderr)


# ---------- 実行結果レポート ----------

def write_run_report(**fields):
    """ワークフロー側の検証ステップが読む、機械可読な実行結果。
    stdout の日本語メッセージのパースに頼らず、確実に判定できるようにする。"""
    try:
        with open(RUN_REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(fields, f, ensure_ascii=False, indent=2)
    except Exception as e:
        # レポート書き込み自体の失敗で本処理を止めたくはないが、検知はできるようにする
        print(f"警告: 実行レポートの書き込みに失敗しました: {e}", file=sys.stderr)


# ---------- APIレスポンス形状の検証(堅牢化) ----------

MAX_REASONABLE_ARTICLES = 20  # これを超える件数が返ってきたら、明らかに異常なレスポンスとして処理を中断する


def log_response_diagnostics(raw, label):
    """今後GitHub Actions上で原因を追跡できるよう、安全な範囲の診断情報だけを出力する。
    raw response全文やAPIキー等の機微情報は一切出力しない。"""
    root_type = type(raw).__name__
    length = None
    try:
        length = len(raw)
    except Exception:
        pass
    first_item_type = None
    try:
        first = next(iter(raw))
        first_item_type = type(first).__name__
    except Exception:
        pass
    print(
        f"[診断:{label}] root型={root_type} 件数={length} 先頭要素の型={first_item_type}",
        file=sys.stderr,
    )


def validate_response_shape(raw, label="news", max_items=None):
    """call_claude_*()から返ってきた値が『dictのlist』という期待した形か検証する。
    ここで弾かれたものは、件数のカウントにもreserve_idsにも一切渡さない。

    実際に本番で、モデルのtool_use入力がJSON配列ではなく巨大な文字列になって
    しまい、それをlist扱いしたことで文字数由来の異常な件数が発生し、1文字ずつを
    itemとしてループしてクラッシュした事例があったため、件数を信用する前に
    必ずこの検証を通す。"""
    log_response_diagnostics(raw, label)
    limit = max_items or MAX_REASONABLE_ARTICLES

    if not isinstance(raw, list):
        raise RuntimeError(
            f"[{label}] 想定外のレスポンス形式です。listである必要がありますが、"
            f"実際の型は{type(raw).__name__}でした。"
        )
    if len(raw) == 0:
        raise RuntimeError(f"[{label}] レスポンスが空配列でした。")
    if len(raw) > limit:
        raise RuntimeError(
            f"[{label}] 件数が異常です({len(raw)}件、上限{limit}件)。"
            "APIレスポンスが壊れている可能性が高いため処理を中断します。"
        )
    non_dict_indices = [i for i, x in enumerate(raw) if not isinstance(x, dict)]
    if non_dict_indices:
        first_bad = raw[non_dict_indices[0]]
        raise RuntimeError(
            f"[{label}] dict以外の要素が{len(non_dict_indices)}件含まれています"
            f"(最初の不正要素のindex={non_dict_indices[0]}, 型={type(first_bad).__name__})。"
        )
    return raw


# ============================================================
# メイン処理
# ============================================================

def main():
    if not os.path.exists(ARTICLES_JSON_PATH):
        raise SystemExit(f"{ARTICLES_JSON_PATH} が見つかりません。先にPhase 1の移行を完了させてください。")
    if not os.path.exists(ID_COUNTER_PATH):
        raise SystemExit(f"{ID_COUNTER_PATH} が見つかりません。")

    existing_articles = load_json(ARTICLES_JSON_PATH)
    today = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
    event_series = load_event_series()

    log_lines = []
    # 公開前監査で不合格になった記事(公開しない)。実行レポートの gate_rejected に残し、
    # report_gate_rejections.py がこれを読んでIssue化する。
    news_gate_rejections = []
    stock_gate_rejections = []

    # ---- A) ニュース/イベント型(失敗したら全体を失敗させる) ----
    # NEWS_ARTICLES_PER_DAY=0 は「ニュース生成を意図的に停止する」設定として
    # 特別扱いする(将来的にニュース生成だけ止めたい場合のため)。それ以外は、
    # 採用件数が期待値と一致しない場合、過去のサイレント障害(Successなのに
    # 実際は記事が公開されていない)を二度と起こさないため、ここで確実に
    # 失敗させる(articles.jsonへは一切書き込まない)。
    # ただし不足の原因に公開前監査の不合格が含まれる場合は、合格した記事だけを公開する
    # (不合格記事を公開しないことが目的のため。不合格分はIssue化される)。
    if NEWS_ARTICLES_PER_DAY == 0:
        accepted_news = []
        log_lines.append("news: NEWS_ARTICLES_PER_DAY=0 のため、ニュース生成は意図的にスキップしました")
    else:
        try:
            accepted_news, news_log, event_series = run_news_generation(
                existing_articles, today, event_series, gate_rejections=news_gate_rejections)
            log_lines.extend(news_log)
        except Exception as e:
            print(f"エラー: ニュース記事の生成に失敗しました。articles.jsonは変更していません。詳細: {e}", file=sys.stderr)
            write_gate_summary(news_gate_rejections)
            write_run_report(status="error", stage="news_generation", error=str(e),
                              accepted_ids=[], accepted_slugs=[], news_ids=[], stock_ids=[],
                              gate_rejected=news_gate_rejections)
            raise

        if len(accepted_news) != NEWS_ARTICLES_PER_DAY and news_gate_rejections:
            log_lines.append(
                f"警告(news): 公開前監査の不合格{len(news_gate_rejections)}件のため、"
                f"news は{len(accepted_news)}/{NEWS_ARTICLES_PER_DAY}件の公開になります"
            )
        elif len(accepted_news) != NEWS_ARTICLES_PER_DAY:
            error_msg = (
                f"news採用件数が期待値と一致しません(期待:{NEWS_ARTICLES_PER_DAY}件, "
                f"実際:{len(accepted_news)}件)。ニュース記事はgithub Pagesに"
                "とって主要コンテンツのため、部分的な成功であってもワークフロー"
                "全体を失敗させます。articles.jsonへは一切書き込みません。"
            )
            print(f"エラー: {error_msg}", file=sys.stderr)
            for line in log_lines:
                print(line, file=sys.stderr)
            write_run_report(
                status="error", stage="news_count_mismatch", error=error_msg,
                accepted_ids=[], accepted_slugs=[], news_ids=[], stock_ids=[],
                news_count=len(accepted_news), expected_news_count=NEWS_ARTICLES_PER_DAY,
            )
            raise RuntimeError(error_msg)

    # ---- B) ストックSEO型(失敗しても致命的にはしない。0件として続行) ----
    accepted_stock = []
    stock_topics = load_stock_topics()
    try:
        accepted_stock, stock_topics = run_stock_generation(
            existing_articles + accepted_news, stock_topics, today, log_lines,
            gate_rejections=stock_gate_rejections,
        )
    except Exception as e:
        log_lines.append(f"stock: エラーのため本日は0件としました。詳細: {e}")
        print(f"警告: ストック記事の生成でエラーが発生しました(newsの結果には影響しません)。詳細: {e}", file=sys.stderr)

    # ---- C) 目標件数への補充(top-up。失敗しても致命的にはしない) ----
    # news+stockが DAILY_TARGET_ARTICLES に届かない分だけ、別のnews候補を生成して
    # 同じ公開前監査にかける(基準は緩めない)。生成回数・監査件数とも上限付き。
    shortfall = DAILY_TARGET_ARTICLES - len(accepted_news) - len(accepted_stock)
    if shortfall > 0 and NEWS_ARTICLES_PER_DAY > 0 and DAILY_TOPUP_MAX_ATTEMPTS > 0:
        if gate_budget_exhausted():
            log_lines.append(f"警告(topup): 公開前監査の件数上限({MAX_GATE_DRAFTS_PER_RUN}件)に達しているため、補充は行いません")
        else:
            log_lines.append(f"topup: 目標{DAILY_TARGET_ARTICLES}件に{shortfall}件不足 → 別候補を補充生成して公開前監査にかけます"
                             f"(最大{DAILY_TOPUP_MAX_ATTEMPTS}回)")
            try:
                accepted_topup, topup_log, event_series = run_news_generation(
                    existing_articles + accepted_news + accepted_stock, today, event_series,
                    gate_rejections=news_gate_rejections, count=shortfall,
                    max_refills=DAILY_TOPUP_MAX_ATTEMPTS - 1, strict=False, label="topup")
                log_lines.extend(topup_log)
                accepted_news = accepted_news + accepted_topup
            except Exception as e:
                log_lines.append(f"topup: エラーのため補充を打ち切りました(合格済みの記事はそのまま公開します)。詳細: {e}")

    for line in log_lines:
        is_warn = line.startswith(("スキップ", "警告")) or "スキップ" in line or "エラー" in line
        print(line, file=sys.stderr if is_warn else sys.stdout)

    accepted_all = accepted_news + accepted_stock
    gate_rejected = news_gate_rejections + stock_gate_rejections
    write_gate_summary(gate_rejected)
    shortfall = shortfall_info(len(accepted_all), len(accepted_stock), gate_rejected)
    write_shortfall_summary(shortfall, today)

    if not accepted_all:
        print("採用できる記事が1件もありませんでした(news/stockともに0件)。articles.jsonは変更していません。", file=sys.stderr)
        # 全件が公開前監査で不合格だった場合は、不合格記事を公開しないという正常な結果なので
        # 失敗扱いにしない(後続のIssue化・台帳の保存を進める)。それ以外は従来どおり失敗させる。
        all_rejected_by_gate = bool(gate_rejected)
        write_run_report(
            status="no_articles_passed_gate" if all_rejected_by_gate else "no_articles_accepted",
            accepted_ids=[], accepted_slugs=[], news_ids=[], stock_ids=[],
            news_count=0, stock_count=0, total_count=0, date=today,
            gate_rejected=gate_rejected, shortfall=shortfall,
        )
        # ストック台帳・イベントシリーズ台帳の状態(候補追加・skip反映)だけは
        # 保存しておく価値があるため書き込む
        if stock_topics:
            atomic_write_json(STOCK_TOPICS_PATH, stock_topics)
        if event_series:
            atomic_write_json(EVENT_SERIES_PATH, event_series)
        if all_rejected_by_gate:
            return
        sys.exit(1)

    new_articles = existing_articles + accepted_all
    atomic_write_json(ARTICLES_JSON_PATH, new_articles)
    atomic_write_json(STOCK_TOPICS_PATH, stock_topics)
    atomic_write_json(EVENT_SERIES_PATH, event_series)

    news_ids = [a["id"] for a in accepted_news]
    stock_ids = [a["id"] for a in accepted_stock]

    print(
        f"\n本日の生成結果 — news: {len(accepted_news)}件 (id:{news_ids}), "
        f"stock: {len(accepted_stock)}件 (id:{stock_ids}), "
        f"合計: {len(accepted_all)}件 (date:{today})"
    )
    print("続けて build.py を実行し、静的ページを再生成してください。")

    write_run_report(
        status="ok",
        accepted_ids=[a["id"] for a in accepted_all],
        accepted_slugs=[a["slug"] for a in accepted_all],
        news_ids=news_ids,
        news_slugs=[a["slug"] for a in accepted_news],
        stock_ids=stock_ids,
        stock_slugs=[a["slug"] for a in accepted_stock],
        news_count=len(accepted_news),
        stock_count=len(accepted_stock),
        total_count=len(accepted_all),
        date=today,
        gate_rejected=gate_rejected,
        shortfall=shortfall,
    )


if __name__ == "__main__":
    main()
