#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
湘南Doors 記事自動生成スクリプト
----------------------------------------------------------
毎日2系統の記事を生成し、data/articles.json に追記する。

  A) ニュース/イベント型(news)  … NEWS_ARTICLES_PER_DAY 件/日 (既定 3)
  B) ストックSEO型(stock)       … STOCK_ARTICLES_PER_DAY 件/日 (既定 2)

このスクリプトはもう index.html を直接編集しない。
ページの再生成は別ステップで build.py が行う(このスクリプトの責務ではない)。

安全設計(ニュース系。Phase 1〜4から変更なし):
- IDは data/id_counter.json から発行し、常にインクリメントのみ(再利用しない)。
- 生成された記事はスキーマ検証を通ったものだけを追記する。
- articles.json への書き込みはtmpファイル+os.replaceによる原子的な置換で行う。
- 重複記事チェック(直近90日、タイトル完全一致/タグ重複率)。
- APIレスポンスの型検証(listでない・件数異常・dict以外混在を検出して安全停止)。

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

# ---------- 生成数設定(環境変数で上書き可能) ----------
NEWS_ARTICLES_PER_DAY = int(os.environ.get("NEWS_ARTICLES_PER_DAY", "3"))
STOCK_ARTICLES_PER_DAY = int(os.environ.get("STOCK_ARTICLES_PER_DAY", "2"))

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
    "input_schema": {
        "type": "object",
        "properties": {
            "articles": {
                "type": "array",
                "minItems": NEWS_ARTICLES_PER_DAY,
                "maxItems": NEWS_ARTICLES_PER_DAY,
                "items": {
                    "type": "object",
                    "properties": {
                        "cat": {"type": "string", "enum": list(CATS.keys())},
                        "area": {"type": "string", "enum": AREAS},
                        "scene": {"type": "string", "enum": SCENES},
                        "title": {"type": "string"},
                        "dek": {"type": "string"},
                        "body": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "link": {"type": "string", "description": "一次情報源のURL。見つからなければ空文字"},
                        "eventStartDate": {"type": "string", "description": "catが'e'(イベント)の場合のみ: 開催日をYYYY-MM-DD形式で。複数日開催の場合は初日。不明な場合は空文字"},
                        "eventEndDate": {"type": "string", "description": "catが'e'(イベント)の場合のみ: 複数日開催の場合の最終日をYYYY-MM-DD形式で。単日開催または不明な場合は空文字"},
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
                        "eventStartDate", "eventEndDate",
                        "address", "access", "hours", "closedDays",
                        "instagram", "facebook", "x", "tiktok",
                    ],
                },
            },
        },
        "required": ["articles"],
    },
}


def build_news_system_prompt(recent_titles):
    recent_block = ""
    if recent_titles:
        joined = "\n".join(f"- {t}" for t in recent_titles)
        recent_block = f"""
【重複防止(必須)】
直近90日以内に、湘南Doorsではすでに以下のタイトルの記事を公開しています。
これらと同じ話題・同じ切り口の記事は書かないでください。少しでも似た話題を
扱う場合は、必ず異なる切り口・異なる情報(未紹介の店舗、今回のみの新情報など)
を選んでください。
{joined}
"""
    return f"""あなたは地域メディア「湘南Doors」の編集者です。
対象エリアは次の8つに限定してください: {", ".join(AREAS)}
カテゴリは次のいずれかを使ってください: {json.dumps(CATS, ensure_ascii=False)}
{recent_block}
Web検索を使って、直近2週間以内に実際にあった湘南エリアのニュース、または
これから開催が確定している実在のイベント情報を{NEWS_ARTICLES_PER_DAY}件調べてください。
架空の情報は絶対に作らないこと。

【情報源・データの出典について（必須・例外なし）】
タウンニュースや号外NETのような二次的なまとめメディアの記事は、ネタを見つける
きっかけとして使うのは構いませんが、記事に実際に書く日付・住所・営業時間・
SNSアカウント等の具体的な情報は、必ずその一次情報源（該当する店舗・団体・
自治体の公式サイト、または公式SNSアカウント本体）まで辿って確認してから
書いてください。二次メディアに書かれている内容をそのまま転記することは
禁止します。一次情報源にたどり着けなかった項目は、無理に埋めず空文字("")に
してください。
"link"には、必ずその一次情報源のURLを入れてください（二次メディアのURLを
入れることは禁止します。一次情報源が見つからない場合は空文字にしてください）。

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
なります)。

本文(body)は4段落程度・合計1000文字以上とし、段落の区切りは\\n\\nで表現してください。
事実に基づき、湘南Doors編集部としての視点を交えた読み物として書くこと。
他サイトの文章の丸写しは禁止、必ず自分の言葉で書き直すこと。

調査・執筆が終わったら、必ず submit_articles ツールを使って{NEWS_ARTICLES_PER_DAY}件まとめて提出してください。
"""


def call_claude_news(recent_titles):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    system_prompt = build_news_system_prompt(recent_titles)

    messages = [{
        "role": "user",
        "content": f"本日分の{NEWS_ARTICLES_PER_DAY}記事を、直近2週間以内のニュースまたは今後のイベント情報から作成し、submit_articlesツールで提出してください。",
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


def run_news_generation(existing_articles, today):
    """ニュース/イベント型記事を生成する。戻り値: (accepted_entries, log_lines)
    致命的なエラー(API呼び出し失敗・レスポンス形状異常)はそのまま例外を送出する
    (このスクリプト全体を失敗させ、articles.jsonへの書き込みを行わせないため)。"""
    log_lines = []
    cutoff = (datetime.now(ZoneInfo("Asia/Tokyo")) - timedelta(days=90)).strftime("%Y-%m-%d")
    recent_titles = [a["title"] for a in existing_articles if a.get("date", "") >= cutoff]

    raw_items = call_claude_news(recent_titles)
    raw_items = validate_response_shape(raw_items, label="news")

    if len(raw_items) != NEWS_ARTICLES_PER_DAY:
        log_lines.append(f"警告(news): 期待した{NEWS_ARTICLES_PER_DAY}件ではなく{len(raw_items)}件が返されました")

    reserved_ids = reserve_ids(max(len(raw_items), NEWS_ARTICLES_PER_DAY))

    accepted = []
    working_set = list(existing_articles)

    for i, item in enumerate(raw_items):
        reason = validate_item(item)
        if reason:
            title_for_log = item.get("title", "(タイトル不明)") if isinstance(item, dict) else f"(dict以外: {type(item).__name__})"
            log_lines.append(f"スキップ(news): 「{title_for_log}」— スキーマ不正: {reason}")
            continue
        dup_reason = is_duplicate(item, working_set, days=90)
        if dup_reason:
            log_lines.append(f"スキップ(news): 「{item['title']}」— 重複疑い: {dup_reason}")
            continue

        new_id = reserved_ids[i]
        entry = {
            "id": new_id,
            "articleType": "news",
            "cat": item["cat"],
            "area": item["area"],
            "scene": item["scene"],
            "title": item["title"],
            "dek": item["dek"],
            "link": item.get("link") or "",
            "date": today,
            "eventStartDate": item.get("eventStartDate") or "",
            "eventEndDate": item.get("eventEndDate") or "",
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
            "slug": f"{AREA_EN[item['area']]}-{CAT_EN[item['cat']]}-{new_id:04d}",
        }
        accepted.append(entry)
        working_set.append(entry)

    log_lines.append(
        f"news: {len(accepted)}/{len(raw_items)}件を採用しました (id:{[a['id'] for a in accepted]})"
    )
    return accepted, log_lines


# ============================================================
# ストックSEO型記事の生成(新規)
# ============================================================

STOCK_TOPIC_REFILL_TOOL = {
    "name": "submit_new_stock_topics",
    "description": "新しいストックSEOテーマ候補を提出する。",
    "input_schema": {
        "type": "object",
        "properties": {
            "topics": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_NEW_STOCK_TOPICS_PER_REFILL,
                "items": {
                    "type": "object",
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
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "topicId": {"type": "string", "description": "評価対象の候補テーマのid"},
                        "decision": {"type": "string", "enum": ["write", "skip"]},
                        "skipReason": {"type": "string", "description": "decisionがskipの場合の理由。writeの場合は空文字でよい"},
                        "article": {
                            "type": "object",
                            "description": "decisionがwriteの場合のみ使用する。skipの場合は各項目を空文字/空配列にしてよい",
                            "properties": {
                                "cat": {"type": "string", "enum": list(CATS.keys())},
                                "area": {"type": "string", "enum": AREAS},
                                "scene": {"type": "string", "enum": SCENES},
                                "title": {"type": "string"},
                                "dek": {"type": "string"},
                                "body": {"type": "string"},
                                "tags": {"type": "array", "items": {"type": "string"}},
                                "link": {"type": "string"},
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
        f"{a['title']} (tags: {', '.join(a.get('tags', []))})"
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
(確認できない項目は空文字で構いません)。他サイトの文章の丸写しは禁止です。

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

        if response.stop_reason != "tool_use":
            raise RuntimeError(
                "submit_stock_decisionsが呼ばれないまま終了しました。stop_reason="
                + str(response.stop_reason)
            )
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": "続けてsubmit_stock_decisionsツールで提出してください。"})

    raise RuntimeError(f"{MAX_STOCK_SELECTION_TURNS}ターン以内にsubmit_stock_decisionsが呼ばれませんでした。")


def run_stock_generation(existing_articles, stock_topics, today, log_lines):
    """ストックSEO型記事を生成する。戻り値: (accepted_entries, updated_stock_topics)

    ニュース生成とは異なり、この関数内のあらゆる失敗(API呼び出し失敗・
    レスポンス形状異常など)は呼び出し元で捕捉され、『今日はstockを0件』
    として扱われる(newsの正常な生成・commitを妨げないため)。"""
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

        # ここまでの検証(write判定・スキーマ・重複チェック)をすべて通過し、
        # articles.jsonへ実際に追加することが確定した記事についてのみ、
        # その場でID を1件予約する。上限2件を予約してから絞り込む方式だと、
        # 1件しか採用されない日・0件の日に不要な欠番が積み上がってしまうため、
        # 「確定した分だけ消費する」設計に変更している。
        new_id = reserve_ids(1)[0]
        entry = {
            "id": new_id,
            "articleType": "stock",
            "cat": article["cat"],
            "area": article["area"],
            "scene": article["scene"],
            "title": article["title"],
            "dek": article["dek"],
            "link": article.get("link") or "",
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
            "slug": f"{AREA_EN[article['area']]}-{CAT_EN[article['cat']]}-{new_id:04d}",
        }
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
    if require_event_fields:
        for key in ("eventStartDate", "eventEndDate"):
            val = item.get(key) or ""
            if val and not re.match(r"^\d{4}-\d{2}-\d{2}$", val):
                return f"{key}の形式が不正(YYYY-MM-DD形式である必要): {val}"
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

    log_lines = []

    # ---- A) ニュース/イベント型(失敗したら全体を失敗させる) ----
    # NEWS_ARTICLES_PER_DAY=0 は「ニュース生成を意図的に停止する」設定として
    # 特別扱いする(将来的にニュース生成だけ止めたい場合のため)。それ以外は、
    # 採用件数が期待値と一致しない場合、過去のサイレント障害(Successなのに
    # 実際は記事が公開されていない)を二度と起こさないため、ここで確実に
    # 失敗させる(articles.jsonへは一切書き込まない)。
    if NEWS_ARTICLES_PER_DAY == 0:
        accepted_news = []
        log_lines.append("news: NEWS_ARTICLES_PER_DAY=0 のため、ニュース生成は意図的にスキップしました")
    else:
        try:
            accepted_news, news_log = run_news_generation(existing_articles, today)
            log_lines.extend(news_log)
        except Exception as e:
            print(f"エラー: ニュース記事の生成に失敗しました。articles.jsonは変更していません。詳細: {e}", file=sys.stderr)
            write_run_report(status="error", stage="news_generation", error=str(e),
                              accepted_ids=[], accepted_slugs=[], news_ids=[], stock_ids=[])
            raise

        if len(accepted_news) != NEWS_ARTICLES_PER_DAY:
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
            existing_articles + accepted_news, stock_topics, today, log_lines
        )
    except Exception as e:
        log_lines.append(f"stock: エラーのため本日は0件としました。詳細: {e}")
        print(f"警告: ストック記事の生成でエラーが発生しました(newsの結果には影響しません)。詳細: {e}", file=sys.stderr)

    for line in log_lines:
        is_warn = line.startswith(("スキップ", "警告")) or "スキップ" in line or "エラー" in line
        print(line, file=sys.stderr if is_warn else sys.stdout)

    accepted_all = accepted_news + accepted_stock

    if not accepted_all:
        print("採用できる記事が1件もありませんでした(news/stockともに0件)。articles.jsonは変更していません。", file=sys.stderr)
        write_run_report(
            status="no_articles_accepted",
            accepted_ids=[], accepted_slugs=[], news_ids=[], stock_ids=[],
            news_count=0, stock_count=0, total_count=0,
        )
        # ストック台帳の状態(候補追加・skip反映)だけは保存しておく価値があるため書き込む
        if stock_topics:
            atomic_write_json(STOCK_TOPICS_PATH, stock_topics)
        sys.exit(1)

    new_articles = existing_articles + accepted_all
    atomic_write_json(ARTICLES_JSON_PATH, new_articles)
    atomic_write_json(STOCK_TOPICS_PATH, stock_topics)

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
    )


if __name__ == "__main__":
    main()
