#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
既存記事へ articleType("news" | "stock") を付与するワンタイム移行スクリプト。

分類基準:
  以下のいずれかに該当する記事は news:
    - eventStartDate が設定されている(実際の開催日が確認されているイベント)
    - タイトルに具体的な日付表記(例: 9月14日)が含まれる
    - タイトルに「開催」「オープン」「グランド」「新店」「誕生」「進出」
      「上陸」「開幕」「執り行わ」「開店」などの、時期に紐づく語が含まれる
  それ以外は stock(時期を問わず読まれ続けるスポット/文化/暮らしガイド)。

実行方法:
    python3 classify_existing_articles.py
"""
import json
import re

ARTICLES_PATH = "data/articles.json"

NEWS_TITLE_MARKERS = [
    "開催", "オープン", "グランド", "新店", "誕生", "進出", "上陸", "開幕", "執り行わ", "開店",
]
DATE_IN_TITLE = re.compile(r"[0-9]{1,2}月[0-9]{1,2}日")


def classify(article):
    title = article["title"]
    if article.get("eventStartDate"):
        return "news"
    if DATE_IN_TITLE.search(title):
        return "news"
    if any(marker in title for marker in NEWS_TITLE_MARKERS):
        return "news"
    return "stock"


def main():
    with open(ARTICLES_PATH, encoding="utf-8") as f:
        articles = json.load(f)

    counts = {"news": 0, "stock": 0}
    for a in articles:
        a["articleType"] = classify(a)
        counts[a["articleType"]] += 1

    with open(ARTICLES_PATH, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)

    print(f"分類完了: news={counts['news']}件, stock={counts['stock']}件, 合計={len(articles)}件")


if __name__ == "__main__":
    main()
