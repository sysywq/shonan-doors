#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公開前監査で不合格になった記事を GitHub Issue にする。
------------------------------------------------------------------------
generate_articles.py が書き出す実行レポート(RUN_REPORT_PATH)の gate_rejected を読み、
不合格記事1件につきIssueを1件作る。本文には不合格の理由と、原因になった claim
(記事側の記述・一次情報側の記述・一次情報URL)を記録する。
実行レポートに shortfall(公開件数が最低ラインに未達)があれば、その理由もIssueを1件作って残す。

- 同じタイトルのIssueが既に open なら作らない(再実行で重複させない)
- GITHUB_TOKEN(または GH_TOKEN)と GITHUB_REPOSITORY が必要
- --dry-run ではIssueを作らず、作る予定の内容だけを出力する
- 標準ライブラリだけで動く(追加パッケージ不要)

使い方:
  python report_gate_rejections.py            # Issueを作成
  python report_gate_rejections.py --dry-run  # 内容の確認のみ
"""
import argparse
import json
import os
import sys
import urllib.request

RUN_REPORT_PATH = os.environ.get(
    "SHONAN_DOORS_RUN_REPORT_PATH",
    os.path.join("/tmp", "shonan_doors_run_report.json"),
)
API = "https://api.github.com"
TITLE_PREFIX = "[公開前監査] 不合格:"
SHORTFALL_TITLE_PREFIX = "[Daily Articles] 公開件数が最低ラインに未達"


def issue_title(r, date):
    return f"{TITLE_PREFIX} {r.get('title', '(タイトル不明)')} ({date})"


def issue_body(r, date):
    lines = [
        "Daily Articles の公開前監査で不合格になったため、この記事は**公開していません**。",
        "",
        f"- 生成日: {date}",
        f"- 種別: {r.get('articleType', '')} / エリア: {r.get('area', '')} / カテゴリ: {r.get('cat', '')}",
        f"- Fact Audit 判定: {r.get('verdict')}",
        f"- 不合格の理由: {' / '.join(r.get('reasons') or []) or '(記録なし)'}",
    ]
    if r.get("summary"):
        lines.append(f"- 校閲メモ: {r['summary']}")
    lines += ["", "### 問題の claim", ""]
    claims = r.get("claims") or []
    if claims:
        lines += ["| role | status | claim | 記事の記述 | 一次情報の記述 | 一次情報URL |",
                  "|---|---|---|---|---|---|"]
        for c in claims:
            cells = [c.get(k, "") or "" for k in ("role", "status", "claim", "articleValue", "primaryValue", "primaryUrl")]
            lines.append("| " + " | ".join(str(x).replace("|", "\\|").replace("\n", " ") for x in cells) + " |")
    else:
        lines.append("(claim の記録なし。監査応答の異常・監査処理の例外などで監査できなかった可能性があります)")
    if r.get("autofix"):
        lines += ["", "### 自動修正を試みた内容(再監査で不合格)", ""]
        for f in r["autofix"]:
            lines.append(f"- 「{f.get('from')}」→「{f.get('to')}」 {f.get('primaryUrl', '')}")
    lines += ["", "### ドラフト", "", f"- タイトル: {r.get('title', '')}", f"- リード: {r.get('dek', '')}"]
    if r.get("link"):
        lines.append(f"- link: {r['link']}")
    for u in r.get("sources") or []:
        lines.append(f"- source: {u}")
    for u in r.get("primarySources") or []:
        lines.append(f"- 監査で確認した一次情報: {u}")
    lines += ["", "一次情報で確認し直してから記事化するか、見送るかを判断してください。"
              "他メディアは事実確認の根拠にしないでください。"]
    return "\n".join(lines)


def shortfall_title(date):
    return f"{SHORTFALL_TITLE_PREFIX} ({date})"


def shortfall_body(s, date, rejected):
    lines = [
        f"{date} の Daily Articles で公開できた記事は **{s.get('total')}件** で、"
        f"最低ライン {s.get('min')}件(目標 {s.get('target')}件)に届きませんでした。",
        "",
        "件数を合わせるために品質基準を下げたり、不合格の記事を公開したりはしていません。",
        "",
        "### 理由",
        "",
    ]
    lines += [f"- {r}" for r in (s.get("reasons") or [])] or ["- (記録なし)"]
    if rejected:
        lines += ["", "### 公開前監査で不合格の記事", ""]
        lines += [f"- {r.get('title', '')}: {' / '.join(r.get('reasons') or [])}" for r in rejected]
    lines += ["", "必要なら一次情報で確認した記事を追加するか、Daily Articles の手動実行(workflow_dispatch)を検討してください。"]
    return "\n".join(lines)


def github_request(method, path, token, payload=None):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "shonan-doors-publish-gate"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8") or "null")


def open_issue_titles(repo, token, request=github_request):
    titles = set()
    for page in range(1, 6):
        items = request("GET", f"/repos/{repo}/issues?state=open&per_page=100&page={page}", token) or []
        titles |= {i.get("title") for i in items if isinstance(i, dict) and "pull_request" not in i}
        if len(items) < 100:
            break
    return titles


def main(argv=None, request=github_request):
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default=RUN_REPORT_PATH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.exists(args.report):
        print(f"実行レポート({args.report})がありません。Issue化する対象はありません。")
        return 0
    with open(args.report, encoding="utf-8") as f:
        report = json.load(f)
    rejected = [r for r in (report.get("gate_rejected") or []) if isinstance(r, dict)]
    shortfall = report.get("shortfall") if isinstance(report.get("shortfall"), dict) else None
    if not rejected and not shortfall:
        print("公開前監査で不合格の記事はありません。")
        return 0
    date = report.get("date") or ""
    # (タイトル, 本文) の一覧。不合格記事1件につき1件、最低件数に未達ならその旨を1件。
    issues = [(issue_title(r, date), issue_body(r, date)) for r in rejected]
    if shortfall:
        issues.append((shortfall_title(date), shortfall_body(shortfall, date, rejected)))

    if args.dry_run:
        for title, body in issues:
            print(f"--- {title}\n{body}\n")
        print(f"[dry-run] {len(issues)}件のIssueを作成する予定です(作成はしていません)。")
        return 0

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("::error::GITHUB_TOKEN と GITHUB_REPOSITORY が必要です", file=sys.stderr)
        return 1

    existing = open_issue_titles(repo, token, request)
    created = 0
    for title, body in issues:
        if title in existing:
            print(f"同じタイトルのIssueが既にあるためスキップ: {title}")
            continue
        res = request("POST", f"/repos/{repo}/issues", token, {"title": title, "body": body})
        existing.add(title)
        created += 1
        print(f"Issueを作成しました: {title} {res.get('html_url', '') if isinstance(res, dict) else ''}")
    print(f"公開前監査の不合格 {len(rejected)}件"
          f"{'・最低件数未達 1件' if shortfall else ''}のうち、{created}件をIssue化しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
