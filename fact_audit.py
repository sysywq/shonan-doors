#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
湘南Doors 既存記事の一次情報照合監査(読み取り専用。articles.json は一切変更しない)
------------------------------------------------------------------------
各記事の本文から「事実の記述」(日付・場所・住所・営業時間・価格・席数・数値・
経歴・設備・立地・人物の発言など)を抜き出し、一次情報(店舗・企業・団体・主催者・
自治体の公式サイト/公式SNS/本人のプレスリリース)で確認できるかを1件ずつ判定する。

他メディア(新聞・ニュースサイト・地域まとめメディア・ブログ・口コミサイト等)は
web_search / web_fetch の blocked_domains で機械的に遮断しているため、
他メディアの記述を根拠に「確認済み」と判定されることはない。

使い方:
  python fact_audit.py                 # 全記事
  python fact_audit.py --ids 65,74     # 指定IDのみ
  python fact_audit.py --start 1 --end 40   # ID範囲(分割実行用)

出力:
  audit_reports/fact_audit_<日時>.json  … 記事ごとの判定詳細
  audit_reports/fact_audit_<日時>.md    … 要修正記事の一覧(人が読む用)
"""
import argparse
import json
import os
import sys
import time
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

# generate_articles.py の定数(他メディアのドメイン一覧)を共有する。
# generate_articles.py 自体は anthropic を import するだけなので、そのまま読み込める。
import generate_articles as g

MODEL = os.environ.get("FACT_AUDIT_MODEL", "claude-sonnet-4-6")
MAX_TURNS = 12
OUT_DIR = os.path.join(g.ROOT, "audit_reports")

AUDIT_TOOL = {
    "name": "submit_audit",
    "description": "1記事分の一次情報照合結果を提出する。",
    "input_schema": {
        "type": "object",
        "properties": {
            "primarySources": {
                "type": "array", "items": {"type": "string"},
                "description": "実際に開いて確認した一次情報のURL",
            },
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string", "description": "記事中の事実の記述(要約)"},
                        "status": {
                            "type": "string",
                            "enum": ["confirmed", "not_found_in_primary", "contradicted"],
                            "description": "confirmed=一次情報で確認できた / not_found_in_primary=一次情報に記載なし(他メディア由来の疑い) / contradicted=一次情報と食い違う",
                        },
                        "primaryUrl": {"type": "string", "description": "確認できた一次情報のURL。無ければ空文字"},
                        "note": {"type": "string", "description": "補足(食い違いの内容など)"},
                    },
                    "required": ["claim", "status", "primaryUrl", "note"],
                },
            },
            "hasQuotedComment": {
                "type": "boolean",
                "description": "人物の発言を「」で引用している、または取材コメントを地の文で使っているか",
            },
            "verdict": {
                "type": "string",
                "enum": ["ok", "fix", "rewrite"],
                "description": "ok=問題なし / fix=該当箇所の削除・修正で足りる / rewrite=一次情報で書き直しが必要",
            },
            "summary": {"type": "string", "description": "判定理由を1〜2文で"},
        },
        "required": ["primarySources", "claims", "hasQuotedComment", "verdict", "summary"],
    },
}

SYSTEM_PROMPT = """あなたは地域メディア「湘南Doors」の校閲担当です。
渡された記事の事実の記述を、一次情報だけで照合してください。

【一次情報の定義】
その店舗・企業・団体・主催者・自治体自身が出している公式サイト、公式SNS、
本人が発表したプレスリリースのみ。新聞・ニュースサイト・地域まとめメディア・
ブログ・口コミサイト・Wikipedia等の他メディアは、たとえ内容が正しくても
根拠として扱わないこと(ツール側でも遮断されています)。

【手順】
1. 本文・dek・店舗情報欄から、確認が必要な事実の記述をすべて抜き出す
   (日付、場所、住所、営業時間、定休日、価格、席数、メニュー数、数値、
    沿革・経歴、設備や立地の説明、人物の発言、評判の記述など)。
   一般的な地理・歴史の常識や、編集部の主観的な感想は対象外。
2. 記事のlinkがあればまずそれを開き、足りなければweb_searchで公式の情報源を探して開く。
3. 各記述を confirmed / not_found_in_primary / contradicted に分類する。
   一次情報に書かれていない記述は、他メディア由来の疑いがあるので
   not_found_in_primary とすること(推測で confirmed にしない)。
4. 人物の発言の引用・取材コメントがあれば hasQuotedComment=true。
5. verdict: 全て confirmed かつ引用なし → ok / 一部削除・修正で済む → fix /
   記事の骨格が他メディア由来 → rewrite。
最後に必ず submit_audit ツールで提出すること。"""


def article_prompt(a):
    info = {
        k: a.get(k) for k in (
            "id", "title", "dek", "area", "link", "date", "address", "access",
            "hours", "closedDays", "snsLinks", "eventStartDate", "eventEndDate", "body",
        )
    }
    return "以下の記事を照合してください。\n\n" + json.dumps(info, ensure_ascii=False, indent=1)


def audit_one(client, a, blocked):
    tools = [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 6, "blocked_domains": blocked},
        {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 8, "blocked_domains": blocked},
        AUDIT_TOOL,
    ]
    messages = [{"role": "user", "content": article_prompt(a)}]
    for _ in range(MAX_TURNS):
        resp = client.messages.create(
            model=MODEL, max_tokens=8000, system=SYSTEM_PROMPT,
            tools=tools, messages=messages,
            extra_headers={"anthropic-beta": "web-fetch-2025-09-10"},
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "submit_audit":
                return block.input
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "pause_turn":
            continue
        messages.append({"role": "user", "content": "照合を続け、終わったらsubmit_auditで提出してください。"})
    raise RuntimeError("submit_auditが呼ばれませんでした")


def blocked_domains():
    # blocked_domains は完全なドメイン指定が必要なため、部分一致用の語("tripadvisor")は除外し、
    # 代表的なドメインを補う。
    out = [d for d in g.SECONDARY_MEDIA_DOMAINS if "." in d]
    out += ["tripadvisor.jp", "tripadvisor.com"]
    return sorted(set(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="")
    ap.add_argument("--start", type=int, default=None)
    ap.add_argument("--end", type=int, default=None)
    args = ap.parse_args()

    articles = sorted([a for a in g.load_json(g.ARTICLES_JSON_PATH) if not a.get("mergedInto")], key=lambda a: a["id"])
    if args.ids:
        want = {int(x) for x in args.ids.split(",") if x.strip()}
        articles = [a for a in articles if a["id"] in want]
    if args.start is not None:
        articles = [a for a in articles if a["id"] >= args.start]
    if args.end is not None:
        articles = [a for a in articles if a["id"] <= args.end]

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    blocked = blocked_domains()
    results = []
    for a in articles:
        print(f"[audit] id:{a['id']} {a['title']}", flush=True)
        try:
            r = audit_one(client, a, blocked)
        except Exception as e:  # 1記事の失敗で全体を止めない
            r = {"verdict": "error", "summary": f"監査失敗: {e}", "claims": [], "primarySources": [], "hasQuotedComment": False}
        r.update({"id": a["id"], "slug": a["slug"], "title": a["title"]})
        results.append(r)
        time.sleep(2)

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d-%H%M")
    json_path = os.path.join(OUT_DIR, f"fact_audit_{stamp}.json")
    md_path = os.path.join(OUT_DIR, f"fact_audit_{stamp}.md")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)

    order = {"rewrite": 0, "fix": 1, "error": 2, "ok": 3}
    lines = [f"# 一次情報照合レポート({stamp})", ""]
    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    lines.append("判定: " + " / ".join(f"{k}: {v}件" for k, v in sorted(counts.items(), key=lambda x: order.get(x[0], 9))))
    lines.append("")
    for r in sorted(results, key=lambda r: (order.get(r["verdict"], 9), r["id"])):
        if r["verdict"] == "ok":
            continue
        lines.append(f"## [{r['verdict']}] id:{r['id']} {r['title']}")
        lines.append(f"- 理由: {r.get('summary','')}")
        if r.get("hasQuotedComment"):
            lines.append("- 人物の発言・取材コメントあり")
        for c in r.get("claims", []):
            if c.get("status") != "confirmed":
                lines.append(f"- {c['status']}: {c['claim']}" + (f"({c['note']})" if c.get("note") else ""))
        lines.append("")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines))
    print(f"完了: {json_path}")


if __name__ == "__main__":
    sys.exit(main())
