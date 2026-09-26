#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily Articles の保留記事(「[Daily Articles 保留]」Issue)を Approve / Reject で処理する。
------------------------------------------------------------------------
daily_fact_audit.py が Fact Audit で confirmed にならなかった記事を当日PRから外し、
report_daily_holds.py がその記事の Issue を作る(本文末尾に記事データを埋め込む)。
オーナーの `@claude Approve` / `@claude Reject` を受けて、このスクリプトで処理する。

Approve(この記事だけを再監査して公開する。build.py の実行・PR作成は呼び出し元で行う):
  - 公式画像の目視確認の項目があれば、確認結果を data/image_confirmations.json に記録する
    (--value … オーナーが画像で読んだ正しい値。項目が1件のときだけ使える)
  - 記事を Fact Audit(full)にかけ直す。一意に直せる誤りは自動修正 → verify(daily_fact_audit と同じ)
  - 公開してよいのは次のどちらか:
      a) 再監査の結果が confirmed
      b) contradicted(公式情報と矛盾する記述)・監査できなかった形跡・人物の発言がなく、
         残っているのがオーナーに確認を求めた点(骨格の未確認・情報源の食い違い)だけ
    それ以外(矛盾が残る・監査できない)は公開しない(終了コード3。理由を出力する)
  - 公開する場合は、保留時と同じID・slugのまま data/articles.json に追加し(URLを変えない)、
    ストックテーマを generated に戻し、イベントシリーズを登録し直す
Reject:
  - 記事は公開しない。ストックテーマを skipped にし、公式画像の確認項目があれば見送りとして記録する
    (別トピックの補充は Daily Articles の top-up 方針に任せる)

使い方:
  python resolve_daily_hold.py --issue 123 --decision approve [--value "10/1〜10/15"]
  python resolve_daily_hold.py --issue 123 --decision reject
  python resolve_daily_hold.py --payload-file held.json --decision approve   # Artifact の保留記事データを使う
  --dry-run … ファイルを変更せず、何をするかだけ出力する

終了コード: 0=処理完了(公開 or 見送り) / 2=入力不正 / 3=再監査で公開できない
"""
import argparse
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import generate_articles as g
import official_image as oi
import report_daily_holds as rdh
import report_gate_rejections as rgr
import resume_image_check as ric


def load_payload(args, request=rgr.github_request):
    if args.payload_file:
        with open(args.payload_file, encoding="utf-8") as f:
            data = json.load(f)
        items = data if isinstance(data, list) else [data]
        if args.article_id:
            items = [h for h in items if isinstance(h, dict) and h.get("id") == args.article_id]
        return items[0] if len(items) == 1 and isinstance(items[0].get("entry"), dict) else None
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (args.issue and token and repo):
        return None
    issue = request("GET", f"/repos/{repo}/issues/{args.issue}", token)
    if not isinstance(issue, dict) or not str(issue.get("title", "")).startswith(rdh.TITLE_PREFIX):
        return None
    return rdh.decode_payload(issue.get("body"))


def approvable(audited):
    """Approve された保留記事を公開してよいか。戻り値: (ok, 理由)"""
    if audited.get("confirmed"):
        return True, "再監査で confirmed"
    result = audited.get("result") or {}
    if result.get("anomalies"):
        return False, "再監査で監査応答の形式異常・例外(監査できていない)"
    if result.get("hasQuotedComment"):
        return False, "人物の発言(他メディアの取材コメント流用の疑い)を含む"
    contradicted = [c for c in result.get("claims") or [] if c.get("status") == "contradicted"]
    if contradicted:  # 自動修正 → verify で解消できていれば confirmed になっている
        return False, f"公式情報と矛盾する記述が{len(contradicted)}件残っている(自動修正できない)"
    if not result.get("claims"):
        return False, "確認できた claim が0件(監査できていない)"
    return True, "矛盾はなく、残っているのはオーナーが確認した点(骨格の未確認・情報源の食い違い)だけ"


def resolve(payload, decision, value="", issue="", dry_run=False, client=None, fetcher=None, image_fetcher=None,
            articles_path=None, stock_topics_path=None, event_series_path=None,
            confirmations_path=oi.CONFIRMATIONS_PATH):
    """戻り値: (exit_code, message, published_entry or None)"""
    import daily_fact_audit as dfa

    articles_path = articles_path or g.ARTICLES_JSON_PATH
    stock_topics_path = stock_topics_path or g.STOCK_TOPICS_PATH
    event_series_path = event_series_path or g.EVENT_SERIES_PATH
    entry = payload.get("entry") or {}
    checks = payload.get("imageChecks") or []
    if not (isinstance(entry.get("id"), int) and entry.get("slug") and entry.get("title") and entry.get("body")):
        return 2, "Issue本文に保留記事のデータ(ID・slug・本文)がありません", None
    if value and len(checks) != 1:
        return 2, "--value は公式画像の確認項目が1件のときだけ使えます", None
    title, article_id = entry["title"], entry["id"]
    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    stamp = now.strftime("%Y-%m-%d %H:%M")
    articles = g.load_json(articles_path)
    if any(a.get("id") == article_id or a.get("slug") == entry["slug"] for a in articles):
        return 2, f"id={article_id}(/articles/{entry['slug']}/)は既に data/articles.json にあります", None

    confirmations = oi.load_confirmations(confirmations_path)
    if checks:
        confirmations = confirmations + ric.confirmation_records(checks, decision, value, issue, stamp)
    topics = g.load_json(stock_topics_path) if os.path.exists(stock_topics_path) else []

    if decision == "reject":
        if not dry_run:
            if checks:
                g.atomic_write_json(confirmations_path, confirmations)
            for t in topics:
                if t.get("generatedArticleId") == article_id and t.get("status") == "held":
                    t.update(status="skipped",
                             skipReason="Fact Audit で保留 → オーナーが見送り" + (f"(Issue #{issue})" if issue else ""))
                    t.pop("holdReason", None)
            g.atomic_write_json(stock_topics_path, topics)
        return 0, (f"見送り: id={article_id}「{title}」は公開しません。"
                   "別トピックの補充は Daily Articles の top-up 方針に任せます。"), None

    if dry_run:
        return 0, f"[dry-run] id={article_id}「{title}」を再監査します(記録・公開はしていません)", None
    if checks:
        g.atomic_write_json(confirmations_path, confirmations)
    if client is None:
        import fact_audit as fa
        client = fa.make_client()
    audited = dfa.audit_article(client, entry, confirmations=confirmations, fetcher=fetcher,
                                image_fetcher=image_fetcher)
    ok, why = approvable(audited)
    if not ok:
        return 3, f"再監査の結果、公開しません: id={article_id}「{title}」— {why} / {' / '.join(audited['reasons'])}", None

    published = dict(audited["entry"], date=now.strftime("%Y-%m-%d"))
    live = [a for a in articles if not a.get("mergedInto")]
    dup = g.is_duplicate(published, live, days=90) or g.find_same_subject(published, live)
    if dup:
        return 3, f"既存記事と重複するため公開しません: 「{title}」({dup})", None
    g.atomic_write_json(articles_path, articles + [published])
    for t in topics:
        if t.get("generatedArticleId") == article_id and t.get("status") == "held":
            t.update(status="generated", generatedAt=stamp)
            t.pop("holdReason", None)
            t.pop("heldAt", None)
    if topics:
        g.atomic_write_json(stock_topics_path, topics)
    series = g.load_json(event_series_path) if os.path.exists(event_series_path) else []
    series, added = g.register_event_series_if_new(published, series)
    if added:
        g.atomic_write_json(event_series_path, series)
    changes = ", ".join(f"「{f['from']}」→「{f['to'] or '(削除)'}」" for f in audited.get("autofix") or [])
    return 0, (f"公開: id={article_id}「{title}」を /articles/{published['slug']}/ として data/articles.json に追加しました"
               f"({why}" + (f"。自動修正: {changes}" if changes else "") + ")。build.py を実行して PR を作成してください。"), published


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue", default="", help="「[Daily Articles 保留]」Issue の番号")
    ap.add_argument("--payload-file", default="", help="保留記事データのJSON(Artifact daily-held-articles)")
    ap.add_argument("--article-id", type=int, default=0, help="--payload-file に複数件あるときの対象ID")
    ap.add_argument("--decision", required=True, choices=["approve", "reject"])
    ap.add_argument("--value", default="", help="オーナーが公式画像で読んだ正しい値(記事の記載と違う場合)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    payload = load_payload(args)
    if not payload:
        print("::error::保留記事のデータを読み取れませんでした(Issueのタイトル・本文を確認してください)", file=sys.stderr)
        return 2
    code, message, _entry = resolve(payload, args.decision, args.value, args.issue, args.dry_run)
    print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
