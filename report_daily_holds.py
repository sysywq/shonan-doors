#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily Articles で Fact Audit により保留になった記事を、1件につき1つの GitHub Issue にする。
------------------------------------------------------------------------
daily_fact_audit.py が書き出す保留記事の記録(/tmp/shonan_doors_daily_holds.json)を読み、
保留記事ごとに Approve / Reject の2択で判断できる Issue を作る。保留記事は当日PRに含めていない
(=公開していない)。他の confirmed 記事は当日PRで通常どおり公開される。

Issue 本文の先頭には判断用の項目を置く:
  対象トピック / 問題箇所 / AI判断 / 確認してほしい内容 / 公式ソースURL(公式画像なら画像の直リンク)
  / [Approve] / [Reject]
本文末尾に、Approve 後の公開に使う記事データ(保留記事の内容と監査結果)を圧縮して埋め込む。
`@claude Approve` / `@claude Reject` を受けて resolve_daily_hold.py がこれを使う。

- 同じタイトルのIssueが既に open なら作らない(再実行で重複させない)
- GITHUB_TOKEN(または GH_TOKEN)と GITHUB_REPOSITORY が必要
- --dry-run ではIssueを作らず、作る予定の内容だけを出力する

使い方:
  python report_daily_holds.py
  python report_daily_holds.py --dry-run
"""
import argparse
import base64
import json
import os
import re
import sys
import zlib

import report_gate_rejections as rgr

HOLDS_PATH = os.environ.get("SHONAN_DOORS_DAILY_HOLDS_PATH", os.path.join("/tmp", "shonan_doors_daily_holds.json"))
TITLE_PREFIX = "[Daily Articles 保留]"
PAYLOAD_MARKER = "daily-hold-payload:"
ISSUE_BODY_LIMIT = 65000  # GitHub の Issue 本文の上限(65536文字)に余裕を持たせた値
MAX_ITEMS = 5

STATUS_LABELS = {
    "contradicted": "公式情報と矛盾",
    "not_found_in_primary": "公式情報で確認できない",
    "source_unavailable": "公式ページを確認できない",
    "unparsed": "監査応答の形式異常",
}


def issue_title(h):
    return f"{TITLE_PREFIX} {h.get('title') or '(タイトル不明)'} (id:{h.get('id')} / {h.get('date', '')})"


def encode_payload(h):
    data = json.dumps(h, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(zlib.compress(data, 9)).decode("ascii")


def decode_payload(body):
    """Issue本文に埋め込んだ保留記事のデータを取り出す。無ければ None。"""
    m = re.search(re.escape(PAYLOAD_MARKER) + r"\s+([A-Za-z0-9+/=]+)\s*-->", body or "")
    if not m:
        return None
    try:
        data = json.loads(zlib.decompress(base64.b64decode(m.group(1))).decode("utf-8"))
    except (ValueError, TypeError, zlib.error):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("entry"), dict) else None


def _cell(v):
    return str(v or "").replace("|", "\\|").replace("\n", " ")


def problem_lines(h):
    claims = h.get("claims") or []
    out = []
    for c in claims[:MAX_ITEMS]:
        core = "【骨格】" if c.get("role") in ("title", "dek", "central") else ""
        label = STATUS_LABELS.get(c.get("status"), c.get("status") or "")
        vals = f" 記事「{c.get('articleValue')}」" if c.get("articleValue") else ""
        if c.get("primaryValue"):
            vals += f" / 公式「{c.get('primaryValue')}」"
        if c.get("imageReading") == "ambiguous":
            vals += "(公式画像の読み取りに曖昧さあり)"
        out.append(f"- {core}{c.get('claim', '')}: {label}{vals}")
    if len(claims) > MAX_ITEMS:
        out.append(f"- ほか{len(claims) - MAX_ITEMS}件(下の詳細を参照)")
    if not out:
        if h.get("anomalies"):
            out.append("- 監査応答の形式異常・監査処理の例外のため、記事の内容を確認できていません")
        elif h.get("hasQuotedComment"):
            out.append("- 人物の発言(他メディアの取材コメント流用の疑い)を含みます")
        else:
            out.append("- (特定の記述の記録なし)")
    return out


def ask_lines(h):
    """確認してほしい内容。公式画像の目視確認なら項目ごとに、それ以外は問題箇所が公式情報どおりかを聞く。"""
    checks = h.get("imageChecks") or []
    if checks:
        return [f"- 公式画像で「{c.get('claim', '')}」が「{c.get('articleValue', '')}」で正しいか"
                f"(AIが読み取れた候補: {c.get('candidate') or '読み取れず'})" for c in checks]
    esc = h.get("escalation") or {}
    lines = []
    if esc.get("reason"):
        lines.append(f"- 判断が必要な理由: {esc['reason']}")
    if h.get("anomalies"):
        lines.append("- 監査をやり直してよいか(Approve で再監査し、confirmed のときだけ公開します)")
    else:
        lines.append("- 上の問題箇所が公式情報(下のURL)のとおりで、この記事を公開してよいか")
    return lines


def source_lines(h):
    pages, images = [], []
    for c in (h.get("claims") or []) + (h.get("imageChecks") or []):
        for k in ("primaryUrl", "pageUrl"):
            if c.get(k) and c[k] not in pages:
                pages.append(c[k])
        if c.get("imageUrl") and c["imageUrl"] not in images:
            images.append(c["imageUrl"])
    entry = h.get("entry") or {}
    for u in [entry.get("link")] + list(entry.get("sources") or []) + list(h.get("primarySources") or []):
        if u and u not in pages:
            pages.append(u)
    out = [f"- 公式画像(直リンク): {u}" for u in images]
    out += [f"- {u}" for u in pages[:8]]
    return out or ["- (記録なし)"]


def issue_body(h, run_url=""):
    entry = h.get("entry") or {}
    image = bool(h.get("imageChecks"))
    reasons = " / ".join(h.get("reasons") or []) or "(記録なし)"
    lines = [
        f"対象トピック: {h.get('title', '')}(id:{h.get('id')} / {h.get('articleType', '')} / "
        f"{h.get('area', '')} / 予定URL: /articles/{h.get('slug', '')}/)",
        "",
        "問題箇所:",
        *problem_lines(h),
        "",
        f"AI判断: Fact Audit(full)の判定は **{h.get('verdict')}**。{reasons}"
        + (f"。校閲メモ: {h['summary']}" if h.get("summary") else ""),
        "",
        "確認してほしい内容:",
        *ask_lines(h),
        "",
        "公式ソースURL:",
        *source_lines(h),
        "",
        "[Approve] この記事だけを再監査し、公開できる状態ならこの記事だけを公開する別PRを作ります"
        + ("(値が違うときは `正しい値: …` を添えてください。その値で直して再監査します)" if image else ""),
        "[Reject] この記事は公開しません(非公開のまま見送り。不足分は Daily Articles の top-up 方針で別トピックを補充します)",
        "",
        "このIssueに `@claude Approve`"
        + ("(公式画像の値が違うときは `@claude Approve 正しい値: …`)" if image else "")
        + " または `@claude Reject` とコメントしてください。",
        "",
        "<details><summary>監査の詳細</summary>",
        "",
        f"Daily Articles({h.get('date', '')})の Fact Audit(full)で confirmed にならなかったため、"
        "この記事は当日PRから外して**公開していません**。同じ日の confirmed 記事は通常どおり公開されています。",
        "",
        f"- 種別: {h.get('articleType', '')} / エリア: {h.get('area', '')} / カテゴリ: {h.get('cat', '')}",
        f"- タイトル: {entry.get('title', '')}",
        f"- リード: {entry.get('dek', '')}",
    ]
    if h.get("stockTopicId"):
        lines.append(f"- ストックテーマ: {h['stockTopicId']}(status=held)")
    if run_url:
        lines.append(f"- 実行ログ: {run_url}")
    claims = h.get("claims") or []
    if claims:
        lines += ["", "| role | status | claim | 記事の記述 | 一次情報の記述 | 一次情報URL |", "|---|---|---|---|---|---|"]
        lines += ["| " + " | ".join(_cell(c.get(k)) for k in
                                     ("role", "status", "claim", "articleValue", "primaryValue", "primaryUrl")) + " |"
                  for c in claims]
    for f in h.get("autofix") or []:
        lines.append(f"- 自動修正を試した内容(verify で未解消): 「{f.get('from')}」→「{f.get('to') or '(削除)'}」")
    lines += ["", "他メディアは事実確認の根拠にしないでください。", "", "</details>", ""]
    body = "\n".join(lines)
    payload = f"<!-- {PAYLOAD_MARKER} {encode_payload(h)} -->"
    if len(body) + len(payload) > ISSUE_BODY_LIMIT:
        # 本文に収まらない場合は、Artifact(daily-held-articles)の保存先を示す(resolve_daily_hold.py --payload-file で使う)
        return body + (f"\n記事データが大きいため本文に埋め込めませんでした。Artifact `daily-held-articles` "
                       f"({run_url or '実行ログ'})の JSON を `--payload-file` で指定してください。")
    return body + "\n" + payload


def main(argv=None, request=rgr.github_request):
    ap = argparse.ArgumentParser()
    ap.add_argument("--holds", default=HOLDS_PATH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    try:
        with open(args.holds, encoding="utf-8") as f:
            holds = [h for h in json.load(f) if isinstance(h, dict)]
    except (OSError, ValueError):
        holds = []
    if not holds:
        print("保留記事はありません。")
        return 0
    server, repo, run_id = (os.environ.get(k, "") for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    run_url = f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else ""
    issues = [(issue_title(h), issue_body(h, run_url)) for h in holds]
    if args.dry_run:
        for title, body in issues:
            print(f"--- {title}\n{body}\n")
        print(f"[dry-run] {len(issues)}件のIssueを作成する予定です(作成はしていません)。")
        return 0

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token or not repo:
        print("::error::GITHUB_TOKEN と GITHUB_REPOSITORY が必要です", file=sys.stderr)
        return 1
    existing = rgr.open_issue_titles(repo, token, request)
    created = 0
    for title, body in issues:
        if title in existing:
            print(f"同じタイトルのIssueが既にあるためスキップ: {title}")
            continue
        res = request("POST", f"/repos/{repo}/issues", token, {"title": title, "body": body})
        existing.add(title)
        created += 1
        print(f"Issueを作成しました: {title} {res.get('html_url', '') if isinstance(res, dict) else ''}")
    print(f"保留記事 {len(holds)}件について、{created}件をIssue化しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
