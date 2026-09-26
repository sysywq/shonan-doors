#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily Articles の当日PRで保留になった記事(Fact Audit で confirmed 以外)を、Approve / Reject で処理する。
------------------------------------------------------------------------
daily_fact_audit.py が「[Daily Fact Audit] …」Issue を作る(本文末尾に再開用のデータを埋め込む)。
オーナーの判断を受けて、**当日PRのbranch(daily/<日付>)上で**このスクリプトを実行する。

Approve(公式画像の確認 kind=image_reading):
  - 確認結果を data/image_confirmations.json に記録する(以後の監査でも同じ画像の読取り補助になる)
  - --value(オーナーが画像で読んだ正しい値)が記事の記載と違えば、記事内の記載をその値に置き換える
Approve(それ以外):
  - 機械的には直さない。Issue の Approve 欄の内容に沿って一次情報で確認できる内容に局所修正し、
    fact_audit.py --mode verify で再確認する(CLAUDE.md の通常ルール)
Reject:
  - 記事を data/articles.json から外す(当日PRで公開しない)。ストック記事ならテーマ台帳を skipped に戻す
  - 公式画像の確認なら、見送りの記録も data/image_confirmations.json に残す

どちらも、このあと `python3 build.py` を実行して当日branchに push する。
保留があった当日PRはオーナーの確認後にマージする(自動マージしない)。

使い方:
  python resolve_daily_hold.py --issue 123 --decision approve [--value "10/1〜10/15"]
  python resolve_daily_hold.py --issue-body-file body.md --decision reject
  --dry-run … ファイルを変更せず、何をするかだけ出力する

終了コード: 0=処理完了 / 2=入力不正 / 3=機械的には処理できない(人・Claude の編集が必要)
"""
import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import daily_fact_audit as dfa
import generate_articles as g
import official_image as oi
import resume_image_check as ric


def replace_value(article, old, new):
    """記事内の記載 old を new に置き換える(publish_gate の自動修正と同じ一致規則)。戻り値: 置き換えた箇所数"""
    import publish_gate as pg

    n = 0
    for f in pg.CORE_FIX_FIELDS:
        text = article.get(f)
        if isinstance(text, str) and old:
            n += pg._count(text, old)
            article[f] = pg._value_re(old).sub(lambda _m: new, text)
    if n:
        pg._sync_event_dates(article, old, new)
    return n


def resolve(payload, decision, value="", issue="", dry_run=False,
            articles_path=None, confirmations_path=oi.CONFIRMATIONS_PATH, stock_path=None):
    """戻り値: (exit_code, message)"""
    articles_path = articles_path or g.ARTICLES_JSON_PATH
    stock_path = stock_path or g.STOCK_TOPICS_PATH
    aid = payload.get("articleId")
    if not isinstance(aid, int):
        return 2, "Issue本文に対象記事のIDがありません"
    articles = g.load_json(articles_path)
    idx = next((i for i, a in enumerate(articles) if a.get("id") == aid), None)
    if idx is None:
        return 2, f"data/articles.json に id={aid} がありません(当日branchで実行しているか確認してください)"
    article = dict(articles[idx])
    checks = payload.get("imageChecks") or []
    now = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M")

    if decision == "approve" and not checks:
        return 3, (f"id={aid} は公式画像の確認ではないため、機械的には直しません。Issue の Approve 欄に沿って"
                   "一次情報で確認できる内容に局所修正し、verify で再確認してください。")
    if value and len(checks) != 1:
        return 2, "確認項目が1件のときだけ --value を使えます"

    records = ric.confirmation_records(checks, decision, value, issue, now) if checks else []
    if decision == "approve":
        old = checks[0].get("articleValue", "") if value else ""
        if value and old and value != old:
            if replace_value(article, old, value) == 0:
                return 3, f"記事内に「{old}」が見つからないため、値を自動で置き換えられません"
        message = f"Approve: id={aid} の公式画像の確認結果を記録しました" + (
            f"(「{old}」→「{value}」に修正)" if value and old and value != old else "")
        new_articles = articles[:idx] + [article] + articles[idx + 1:]
    else:
        message = f"Reject: id={aid}「{article.get('title', '')}」を当日PRから外しました"
        new_articles = articles[:idx] + articles[idx + 1:]
    if dry_run:
        return 0, "[dry-run] " + message
    if records:
        g.atomic_write_json(confirmations_path, oi.load_confirmations(confirmations_path) + records)
    g.atomic_write_json(articles_path, new_articles)
    if decision == "reject" and article.get("articleType") == "stock":
        topics = g.load_json(stock_path)
        for t in topics:
            if t.get("generatedArticleId") == aid:
                t["status"] = "skipped"
                t["skipReason"] = f"Daily Fact Audit で保留 → オーナーが Reject(id={aid})"
        g.atomic_write_json(stock_path, topics)
    return 0, message + "。python3 build.py を実行して当日branchに push してください。"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue", default="", help="「[Daily Fact Audit]」Issue の番号")
    ap.add_argument("--issue-body-file", default="", help="Issue本文を保存したファイル(--issue の代わり)")
    ap.add_argument("--decision", required=True, choices=["approve", "reject"])
    ap.add_argument("--value", default="", help="オーナーが画像で読んだ正しい値(記事の記載と違う場合)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    body = ric.load_issue_body(args)
    payload = dfa.decode_hold_payload(body) if body else None
    if not payload:
        print("::error::Issue本文から再開用のデータを読み取れませんでした", file=sys.stderr)
        return 2
    code, message = resolve(payload, args.decision, args.value, args.issue, args.dry_run)
    print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
