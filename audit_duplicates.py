#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
湘南Doors 既存記事の重複・情報源監査スクリプト(読み取り専用。articles.jsonは変更しない)

  python audit_duplicates.py          # レポートを表示(問題があれば終了コード1)

検出するもの:
  - 同一対象の記事(一次情報URLが同じ / 対象名が一致 / 同エリアで本文が高類似)
    ※毎年開催イベントの別年度の回は除外
  - 情報源ポリシー違反(他メディアURL・人物の発言の引用)
generate_articles.py と同じ判定関数を使うため、日次生成の判定と監査の判定がずれない。
"""
import json, os, re, sys, types

# generate_articles.py は anthropic を import するが、監査には不要なのでスタブで代替する
sys.modules.setdefault("anthropic", types.ModuleType("anthropic"))
import generate_articles as g  # noqa: E402

def main():
    articles = g.load_json(g.ARTICLES_JSON_PATH)
    # 統合済み(mergedInto)の記事は公開一覧から外れているため監査対象外
    articles = sorted([a for a in articles if not a.get("mergedInto")], key=lambda a: a["id"])
    problems = 0

    print("== 同一対象の疑い ==")
    for i, a in enumerate(articles):
        reason = g.find_same_subject(a, articles[:i])
        if reason:
            problems += 1
            print(f"- id:{a['id']}「{a['title']}」→ {reason}")

    print("\n== 情報源ポリシー違反の疑い ==")
    for a in articles:
        urls = (a.get("sources") or []) + [a.get("link") or ""]
        bad = [u for u in urls if g.is_secondary_media(u)]
        quotes = [m.group(0) for pat in g.QUOTE_ATTRIBUTION_PATTERNS for m in [re.search(pat, a.get("body", ""))] if m]
        if bad or quotes:
            problems += 1
            print(f"- id:{a['id']}「{a['title']}」 他メディアURL:{bad} 引用:{quotes}")

    print(f"\n合計 {problems} 件")
    return 1 if problems else 0

if __name__ == "__main__":
    sys.exit(main())
