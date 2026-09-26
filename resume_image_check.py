#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公式画像の目視確認(Approve / Reject)を受けて、見送った記事の処理を再開する。
------------------------------------------------------------------------
公開前監査で「根拠は公式画像にあるが、AIの読取り確度だけが足りない」と判定された記事は、
report_gate_rejections.py が「[公式画像の確認]」Issue を作る(本文にドラフトと確認項目を埋め込む)。
オーナーが画像を見て Approve / Reject したら、このスクリプトで処理を再開する。

Approve:
  - オーナーの確認結果を data/image_confirmations.json に記録する(以後の監査でも同じ画像の読取り補助になる)
      --value 省略時 … 記事の記載(確認してほしい項目の値)が正しいと確認された、として記録
      --value あり   … オーナーが読んだ正しい値として記録(記事と違えば自動修正の対象になる)
  - 確認結果を読取り補助にしてドラフトを公開前監査にかけ直す(publish_gate.check_draft)
  - 合格なら重複を確認し、IDを発行して data/articles.json に追加する
    (build.py の実行・PR作成は呼び出し元で行う)
Reject:
  - 確認結果(見送り)を記録するだけで、記事は公開しない。別トピックの補充は Daily Articles の top-up に任せる

使い方:
  python resume_image_check.py --issue-body-file body.md --decision approve [--value "10/1〜10/15"]
  python resume_image_check.py --issue 123 --decision reject      # GitHub API で本文を取得
  --dry-run … 記録・articles.json の変更をせず、何をするかだけ出力する

終了コード: 0=処理完了(公開 or 見送り) / 2=入力不正 / 3=再監査で不合格(公開しない)
"""
import argparse
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import generate_articles as g
import official_image as oi
import report_gate_rejections as rgr


def load_issue_body(args, request=rgr.github_request):
    if args.issue_body_file:
        with open(args.issue_body_file, encoding="utf-8") as f:
            return f.read()
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (args.issue and token and repo):
        return None
    issue = request("GET", f"/repos/{repo}/issues/{args.issue}", token)
    return issue.get("body") if isinstance(issue, dict) else None


def confirmation_records(checks, decision, value, issue, now):
    """オーナーの確認結果を image_confirmations.json の形にする。
    --value は確認項目が1件のときだけ使う(どの項目の値か一意に決まらないため)。"""
    out = []
    for c in checks:
        v = value if (value and len(checks) == 1) else c.get("articleValue", "")
        out.append({
            "imageUrl": c.get("imageUrl", ""), "pageUrl": c.get("pageUrl", ""),
            "item": c.get("claim", ""), "value": v, "aiCandidate": c.get("candidate", ""),
            "decision": decision, "issue": issue or "", "confirmedAt": now,
        })
    return out


def resume(payload, decision, value="", issue="", dry_run=False, client=None,
           confirmations_path=oi.CONFIRMATIONS_PATH, articles_path=None, check_draft=None):
    """戻り値: (exit_code, message, published_entry or None)"""
    import publish_gate

    articles_path = articles_path or g.ARTICLES_JSON_PATH
    check_draft = check_draft or publish_gate.check_draft
    checks = payload.get("imageChecks") or []
    draft = payload.get("draft") or {}
    if not checks or not draft.get("title") or not draft.get("body"):
        return 2, "Issue本文に再開用のデータ(確認項目・ドラフト)がありません", None
    if value and len(checks) > 1:
        return 2, "確認項目が複数あるため --value は使えません(項目ごとの値が一意に決まらない)", None

    now = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M")
    records = confirmation_records(checks, decision, value, issue, now)
    existing = oi.load_confirmations(confirmations_path)
    confirmations = existing + records
    if not dry_run:
        g.atomic_write_json(confirmations_path, confirmations)

    title = draft.get("title", "")
    if decision == "reject":
        return 0, f"見送り: 「{title}」は公開しません。別トピックの補充は Daily Articles の top-up で行います。", None

    if dry_run:
        return 0, f"[dry-run] 「{title}」をオーナーの確認結果つきで再監査します(記録・公開はしていません)", None

    gate = check_draft(dict(draft), client=client, confirmations=confirmations)
    if not gate.get("passed"):
        return 3, f"再監査で不合格のため公開しません: 「{title}」— {' / '.join(gate.get('reasons') or [])}", None

    entry = dict(gate["entry"])
    articles = g.load_json(articles_path)
    live = [a for a in articles if not a.get("mergedInto")]
    dup = g.is_duplicate(entry, live, days=90) or g.find_same_subject(entry, live)
    if dup:
        return 3, f"既存記事と重複するため公開しません: 「{title}」({dup})", None
    entry["date"] = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
    g.finalize_news_entry_id(entry, g.reserve_ids(1)[0])
    g.atomic_write_json(articles_path, articles + [entry])
    changes = ", ".join(f"「{f['from']}」→「{f['to'] or '(削除)'}」" for f in gate.get("autofix") or [])
    return 0, (f"公開: id={entry['id']} 「{title}」を data/articles.json に追加しました"
               + (f"(自動修正: {changes})" if changes else "") + "。build.py を実行してください。"), entry


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue", default="", help="「[公式画像の確認]」Issue の番号")
    ap.add_argument("--issue-body-file", default="", help="Issue本文を保存したファイル(--issue の代わり)")
    ap.add_argument("--decision", required=True, choices=["approve", "reject"])
    ap.add_argument("--value", default="", help="オーナーが画像で読んだ正しい値(記事の記載と違う場合)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    body = load_issue_body(args)
    payload = rgr.decode_payload(body) if body else None
    if not payload:
        print("::error::Issue本文から再開用のデータを読み取れませんでした", file=sys.stderr)
        return 2
    code, message, _entry = resume(payload, args.decision, args.value, args.issue, args.dry_run)
    print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
