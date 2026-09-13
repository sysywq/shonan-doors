#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
湘南Doors 記事自動生成スクリプト
----------------------------------
毎朝、直近2週間以内の湘南エリアのニュース、または今後のイベント情報を
Claude(Web検索つき)に調べさせ、サイトのDATA配列と同じ形式で3記事分の
原稿を作らせて、HTMLファイルに直接追記するスクリプト。

前提:
- 環境変数 ANTHROPIC_API_KEY が設定されていること
- このスクリプトと同じリポジトリに index.html が存在すること
- GitHub Actions から1日1回、cronで実行される想定
"""

import os
import re
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
import anthropic

HTML_PATH = "index.html"
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

ARTICLE_TOOL = {
    "name": "submit_articles",
    "description": "調査・執筆が完了した記事3件を提出する。",
    "input_schema": {
        "type": "object",
        "properties": {
            "articles": {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "cat": {"type": "string", "description": "t|b|g|p|c|e|l のいずれか1文字"},
                        "area": {"type": "string", "description": "|".join(AREAS) + " のいずれか"},
                        "scene": {"type": "string", "description": "|".join(SCENES) + " のいずれか"},
                        "title": {"type": "string"},
                        "dek": {"type": "string"},
                        "body": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "link": {"type": "string", "description": "一次情報源のURL。見つからなければ空文字"},
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
                        "address", "access", "hours", "closedDays",
                        "instagram", "facebook", "x", "tiktok",
                    ],
                },
            },
        },
        "required": ["articles"],
    },
}

SYSTEM_PROMPT = f"""あなたは地域メディア「湘南Doors」の編集者です。
対象エリアは次の8つに限定してください: {", ".join(AREAS)}
カテゴリは次のいずれかを使ってください: {json.dumps(CATS, ensure_ascii=False)}

Web検索を使って、直近2週間以内に実際にあった湘南エリアのニュース、または
これから開催が確定している実在のイベント情報を3件調べてください。
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

本文(body)は4段落程度・合計1000文字以上とし、段落の区切りは\\n\\nで表現してください。
事実に基づき、湘南Doors編集部としての視点を交えた読み物として書くこと。
他サイトの文章の丸写しは禁止、必ず自分の言葉で書き直すこと。

調査・執筆が終わったら、必ず submit_articles ツールを使って3件まとめて提出してください。
"""

def call_claude():
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    messages = [{
        "role": "user",
        "content": "本日分の3記事を、直近2週間以内のニュースまたは今後のイベント情報から作成し、submit_articlesツールで提出してください。",
    }]
    tools = [
        {"type": "web_search_20250305", "name": "web_search"},
        ARTICLE_TOOL,
    ]

    # submit_articlesが呼ばれるまで、最大10ターンやり取りを続ける
    # （Web検索を挟むと複数ターンに分かれることがあるため）
    for _ in range(10):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == "submit_articles":
                # 構造化ツール呼び出しなので、ここで得られる中身は
                # 既にAnthropic API側でJSONスキーマとして検証済み。
                # 文字列パースの失敗が起こりようがない。
                return block.input["articles"]

        if response.stop_reason != "tool_use":
            raise RuntimeError(
                "submit_articlesが呼ばれないまま終了しました。stop_reason="
                + str(response.stop_reason)
                + " content=" + repr(response.content)
            )

        # web_search等、submit_articles以外のツール呼び出しがあった場合は
        # そのままAPI側で処理済みの内容が response.content に含まれているので、
        # それを会話履歴に積んで次のターンへ継続する
        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": "続けて調査を進め、準備ができ次第submit_articlesツールで提出してください。",
        })

    raise RuntimeError("10ターン以内にsubmit_articlesが呼ばれませんでした。")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print("---- JSON解析に失敗した文字列（デバッグ用）----", file=sys.stderr)
        print(raw, file=sys.stderr)
        print("------------------------------------------------", file=sys.stderr)
        raise

def js_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")

def build_entry(item: dict, new_id: int, date_str: str) -> str:
    tags = ",".join(f"'{js_escape(t)}'" for t in item["tags"])
    link = js_escape(item.get("link") or "")
    address = js_escape(item.get("address") or "")
    access = js_escape(item.get("access") or "")
    hours = js_escape(item.get("hours") or "")
    closed_days = js_escape(item.get("closedDays") or "")
    instagram = js_escape(item.get("instagram") or "")
    facebook = js_escape(item.get("facebook") or "")
    x_link = js_escape(item.get("x") or "")
    tiktok = js_escape(item.get("tiktok") or "")
    return (
        "{id:%d,cat:'%s',area:'%s',scene:'%s',title:'%s',dek:'%s',\n"
        "link:'%s',\ndate:'%s',\n"
        "address:'%s',\naccess:'%s',\nhours:'%s',\nclosedDays:'%s',\n"
        "snsLinks:{instagram:'%s',facebook:'%s',x:'%s',tiktok:'%s'},\n"
        "body:'%s',\n"
        "tags:[%s]}"
    ) % (
        new_id, item["cat"], item["area"], item["scene"],
        js_escape(item["title"]), js_escape(item["dek"]), link, date_str,
        address, access, hours, closed_days,
        instagram, facebook, x_link, tiktok,
        js_escape(item["body"]), tags,
    )

def main():
    with open(HTML_PATH, encoding="utf-8") as f:
        content = f.read()

    existing_ids = [int(m) for m in re.findall(r"id:(\d+),cat:", content)]
    next_id = max(existing_ids) + 1 if existing_ids else 1

    today = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")

    items = call_claude()
    if len(items) != 3:
        print(f"警告: 期待した3件ではなく{len(items)}件が返されました", file=sys.stderr)

    entries = []
    for i, item in enumerate(items):
        entries.append(build_entry(item, next_id + i, today))

    data_end = content.find("\n];")
    if data_end == -1:
        raise RuntimeError("DATA配列の終端が見つかりませんでした")

    before = content[:data_end].rstrip()
    if not before.endswith(","):
        before += ","
    after = content[data_end:]

    new_content = before + "\n" + ",\n\n".join(entries) + after
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"{len(items)}件の記事を追加しました（id:{next_id}〜{next_id+len(items)-1}, date:{today}）")

if __name__ == "__main__":
    main()
