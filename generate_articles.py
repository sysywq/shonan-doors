#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
湘南Doors 記事自動生成スクリプト (Phase 1: JSON構造対応版)
----------------------------------------------------------
毎朝、直近2週間以内の湘南エリアのニュース、または今後のイベント情報を
Claude(Web検索つき)に調べさせ、data/articles.json 形式の記事を作らせて
articles.json に追記する。

このスクリプトはもう index.html を直接編集しない。
ページの再生成は別ステップで build.py が行う(このスクリプトの責務ではない)。

安全設計:
- IDは data/id_counter.json から発行し、常にインクリメントのみ(再利用しない)。
  API呼び出しやバリデーションが失敗しても、発行済みIDが失われるだけで
  (=欠番が増えるだけで)、IDの重複や巻き戻りは絶対に起きない。
- 生成された記事はスキーマ検証を通ったものだけを追記する。
  3件のうち1件だけ不正/重複でも、残り2件は正常に追記される。
- articles.json への書き込みはtmpファイル+os.replaceによる原子的な置換で行う。
  途中でクラッシュしても、書きかけの壊れたJSONが残ることはない。
- 重複記事チェック: 直近90日分の既存記事とタイトル完全一致、または
  同エリア×タグの重なりが大きいものは「重複の疑いあり」として弾く。
- build.py はこのスクリプトの後段の別ステップ(GitHub Actions側)で実行され、
  build.py が失敗した場合はワークフローがそこで停止するため、
  壊れた状態のまま commit / push されることはない。
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

ARTICLES_PER_DAY = int(os.environ.get("NEWS_ARTICLES_PER_DAY", "3"))

ARTICLE_TOOL = {
    "name": "submit_articles",
    "description": "調査・執筆が完了した記事を提出する。",
    "input_schema": {
        "type": "object",
        "properties": {
            "articles": {
                "type": "array",
                "minItems": ARTICLES_PER_DAY,
                "maxItems": ARTICLES_PER_DAY,
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


def build_system_prompt(recent_titles):
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
これから開催が確定している実在のイベント情報を{ARTICLES_PER_DAY}件調べてください。
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

調査・執筆が終わったら、必ず submit_articles ツールを使って{ARTICLES_PER_DAY}件まとめて提出してください。
"""


def call_claude(recent_titles):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    system_prompt = build_system_prompt(recent_titles)

    messages = [{
        "role": "user",
        "content": f"本日分の{ARTICLES_PER_DAY}記事を、直近2週間以内のニュースまたは今後のイベント情報から作成し、submit_articlesツールで提出してください。",
    }]
    tools = [
        {"type": "web_search_20250305", "name": "web_search"},
        ARTICLE_TOOL,
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


# ---------- ID発行(常にインクリメントのみ・再利用しない) ----------

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
    この後の処理(API呼び出し・検証・articles.json書き込み)が何であれ失敗しても、
    ここで予約したIDが将来再利用されることはない(欠番になるだけ)。"""
    counter = load_json(ID_COUNTER_PATH)
    start = counter["next_id"]
    ids = list(range(start, start + count))
    counter["next_id"] = start + count
    atomic_write_json(ID_COUNTER_PATH, counter)
    return ids


# ---------- スキーマ検証 ----------

def validate_item(item):
    """1件分の記事データがarticles.jsonのschemaに適合しているか検証する。
    不正なら理由の文字列を返し、問題なければNoneを返す。"""
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
    完全な意味的重複判定ではなく、あくまで明らかな重複を弾くための保険。"""
    cutoff = (datetime.now(ZoneInfo("Asia/Tokyo")) - timedelta(days=days)).strftime("%Y-%m-%d")
    new_title = normalize(item["title"])
    new_tags = set(item.get("tags", []))

    for existing in existing_articles:
        if existing.get("date", "") < cutoff:
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


def main():
    if not os.path.exists(ARTICLES_JSON_PATH):
        raise SystemExit(f"{ARTICLES_JSON_PATH} が見つかりません。先にPhase 1の移行を完了させてください。")
    if not os.path.exists(ID_COUNTER_PATH):
        raise SystemExit(f"{ID_COUNTER_PATH} が見つかりません。")

    existing_articles = load_json(ARTICLES_JSON_PATH)
    today = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")

    cutoff = (datetime.now(ZoneInfo("Asia/Tokyo")) - timedelta(days=90)).strftime("%Y-%m-%d")
    recent_titles = [a["title"] for a in existing_articles if a.get("date", "") >= cutoff]

    try:
        raw_items = call_claude(recent_titles)
    except Exception as e:
        print(f"エラー: 記事生成に失敗しました。articles.jsonは変更していません。詳細: {e}", file=sys.stderr)
        write_run_report(status="error", stage="call_claude", error=str(e),
                          accepted_ids=[], accepted_slugs=[])
        raise

    if len(raw_items) != ARTICLES_PER_DAY:
        print(f"警告: 期待した{ARTICLES_PER_DAY}件ではなく{len(raw_items)}件が返されました", file=sys.stderr)

    reserved_ids = reserve_ids(max(len(raw_items), ARTICLES_PER_DAY))

    accepted = []
    rejected = []
    working_set = list(existing_articles)

    for i, item in enumerate(raw_items):
        reason = validate_item(item)
        if reason:
            rejected.append((item.get("title", "(タイトル不明)"), f"スキーマ不正: {reason}"))
            continue
        dup_reason = is_duplicate(item, working_set)
        if dup_reason:
            rejected.append((item["title"], f"重複疑い: {dup_reason}"))
            continue

        new_id = reserved_ids[i]
        entry = {
            "id": new_id,
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

    for title, reason in rejected:
        print(f"スキップ: 「{title}」— {reason}", file=sys.stderr)

    if not accepted:
        print("採用できる記事が1件もありませんでした。articles.jsonは変更していません。", file=sys.stderr)
        write_run_report(status="no_articles_accepted", accepted_ids=[], accepted_slugs=[],
                          rejected_count=len(rejected), raw_count=len(raw_items))
        sys.exit(1 if not raw_items else 0)

    new_articles = existing_articles + accepted
    atomic_write_json(ARTICLES_JSON_PATH, new_articles)

    print(f"{len(accepted)}/{len(raw_items)}件の記事を追加しました "
          f"(id:{[a['id'] for a in accepted]}, date:{today})。"
          f"{len(rejected)}件はスキップされました。")
    print("続けて build.py を実行し、静的ページを再生成してください。")

    write_run_report(
        status="ok",
        accepted_ids=[a["id"] for a in accepted],
        accepted_slugs=[a["slug"] for a in accepted],
        rejected_count=len(rejected),
        raw_count=len(raw_items),
        date=today,
    )


if __name__ == "__main__":
    main()
