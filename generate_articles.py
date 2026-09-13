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

SYSTEM_PROMPT = f"""あなたは地域メディア「湘南Doors」の編集者です。
対象エリアは次の8つに限定してください: {", ".join(AREAS)}
カテゴリは次のいずれかを使ってください: {json.dumps(CATS, ensure_ascii=False)}

Web検索を使って、直近2週間以内に実際にあった湘南エリアのニュース、または
これから開催が確定している実在のイベント情報を3件調べてください。
架空の情報は絶対に作らないこと。日付や場所は必ず一次情報（公式サイト等）で
裏を取ること。

3件それぞれについて、以下の厳密なJSON形式の配列で出力してください。
出力はJSON以外の文字を一切含めないこと。

[
  {{
    "cat": "t|b|g|p|c|e|l のいずれか1文字",
    "area": "上記8エリアのいずれか",
    "scene": "{'|'.join(SCENES)} のいずれか",
    "title": "記事タイトル（40文字前後）",
    "dek": "1行の要約（40〜60文字）",
    "body": "4段落程度、合計1000文字以上の本文。段落の区切りは\\n\\nで表現。事実に基づき、湘南Doors編集部としての視点を交えた読み物として書くこと。他サイトの文章の丸写しは禁止、必ず自分の言葉で書き直すこと。",
    "tags": ["タグ1","タグ2","タグ3"]
  }},
  ...
]
"""

def call_claude():
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{
            "role": "user",
            "content": "本日分の3記事を、直近2週間以内のニュースまたは今後のイベント情報から作成してください。",
        }],
    )
    # 最後のtextブロックにJSONが入っている想定
    text_blocks = [b.text for b in response.content if b.type == "text"]
    raw = text_blocks[-1].strip()
    # ```json ... ``` で囲まれている場合に備えて除去
    raw = re.sub(r"^```json\s*|\s*```$", "", raw.strip())
    return json.loads(raw)

def js_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")

def build_entry(item: dict, new_id: int, date_str: str) -> str:
    tags = ",".join(f"'{js_escape(t)}'" for t in item["tags"])
    return (
        "{id:%d,cat:'%s',area:'%s',scene:'%s',title:'%s',dek:'%s',\n"
        "link:'',\ndate:'%s',\n"
        "body:'%s',\n"
        "tags:[%s]}"
    ) % (
        new_id, item["cat"], item["area"], item["scene"],
        js_escape(item["title"]), js_escape(item["dek"]), date_str,
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
