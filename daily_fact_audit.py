#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily Articles 当日分の Fact Audit(mode=full)と機械判定
------------------------------------------------------------------------
generate_articles.py が公開対象にした記事(実行レポートの accepted_ids)だけを、
fact_audit.py と同じ監査ロジック(fact_audit.main --mode full --ids ...)にかけ、
当日PRを自動マージしてよいかを機械的に判定する。

流れ:
  1. fact_audit.main(["--mode", "full", "--ids", accepted_ids, "--out-dir", ...])
     (レポートの形式は Fact Audit workflow と同じ。Artifact として保存する)
  2. 記事ごとに publish_gate.evaluate で合否を決める
     (verdict=confirmed、かつ contradicted・骨格の未確認・人物の発言・形式異常・要人確認が無いこと)
  3. 不合格のうち、一次情報で修正内容が一意に決まるもの(publish_gate.safe_autofix)は
     data/articles.json の該当記事を局所修正し、修正した claim だけを verify(fact_verify.verify_article)で
     再確認する(修正後の確認は verify を使う運用ルールに従う)。すべて resolved なら合格
  4. 判定結果を DECISION_PATH に書き出す。confirmed 以外が1件でも残れば auto_merge=false
     (当日PRは自動マージしない)。`issues` サブコマンドで保留記事ごとに Issue を作る

公式サイト・公式SNSの画像内の文字も一次情報として扱う既存ルール(fact_audit の basis=official_image)は
そのまま使う。画像の読み取りが曖昧で人の確認が必要な記事は、対象トピック / 確認対象 / AI読取候補 /
確認してほしいこと / 公式ソースURL / 画像直リンクを載せた Approve / Reject の2択の Issue にする。

使い方:
  python daily_fact_audit.py audit                       # 監査と判定(DECISION_PATH に書き出す)
  python daily_fact_audit.py issues --pr-url URL --branch daily/2026-09-26   # 保留記事をIssue化
  python daily_fact_audit.py issues ... --dry-run        # 作る予定のIssueを表示するだけ

終了コード: 0=判定完了(合否は DECISION_PATH と GITHUB_OUTPUT の auto_merge で返す) / 1=処理エラー
"""
import argparse
import base64
import json
import os
import sys

import generate_articles as g
import report_gate_rejections as rgr

RUN_REPORT_PATH = rgr.RUN_REPORT_PATH
DECISION_PATH = os.environ.get(
    "SHONAN_DOORS_AUDIT_DECISION_PATH",
    os.path.join("/tmp", "shonan_doors_fact_audit_decision.json"),
)
AUDIT_OUT_DIR = os.environ.get("SHONAN_DOORS_AUDIT_OUT_DIR", os.path.join("/tmp", "daily_fact_audit"))
AUDIT_SLEEP_SEC = 2  # 記事ごとの監査の間隔(fact_audit.main と同じ既定値)
HOLD_TITLE_PREFIX ="[Daily Fact Audit]"
HOLD_MARKER = "daily-hold-payload:"
SITE = "https://www.shonandoors.com"


# ---------- 判定 ----------

def judge(result):
    """正規化済みの監査結果1件の合否。戻り値: (passed, reasons)"""
    import publish_gate as pg

    if not isinstance(result, dict):
        return False, ["監査結果がない"]
    passed, reasons, _blocking = pg.evaluate(result)
    if passed and result.get("verdict") != "confirmed":
        passed, reasons = False, [f"Fact Audit の判定が {result.get('verdict')}"]
    return passed, reasons


def apply_verify(result, verified_claims):
    """自動修正後に verify した claim の結果を full の結果に反映し、判定をやり直した結果を返す。
    resolved の claim は confirmed(修正済み)に、それ以外は contradicted のまま残す。"""
    import fact_audit as fa

    by_claim = {c.get("claim"): c for c in verified_claims}
    claims = []
    for c in result.get("claims") or []:
        v = by_claim.get(c.get("claim")) if c.get("status") == "contradicted" else None
        if v is not None and v.get("verifyResult") == "resolved":
            c = dict(c, status="confirmed", note=(c.get("note", "") + " [自動修正後に verify で解決]").strip())
        claims.append(c)
    verdict, reasons = fa.decide_verdict(claims, result.get("needsHuman"), result.get("anomalies"))
    return dict(result, claims=claims, verdict=verdict, verdictReasons=reasons, verified=True)


def autofix_and_verify(article, result, client_getter, fetcher=None, verify=None):
    """一次情報で修正内容が一意に決まる誤りを直し、直した claim だけを verify する。
    戻り値: (修正後の記事 or None, 修正内容, 反映後の結果)"""
    import fact_verify as fv
    import publish_gate as pg

    fixed, fixes = pg.safe_autofix(article, result)
    if fixed is None:
        return None, [], result
    targets = [c for c in result.get("claims") or [] if c.get("status") == "contradicted"]
    verify = verify or fv.verify_article
    verified = verify(fixed, targets, client_getter, fetcher or fv.default_fetcher, fv.new_stats())
    return fixed, fixes, apply_verify(result, verified)


def hold_record(article, result, reasons, fixes=()):
    """自動マージしない記事1件分の記録(Issue化に使う)。"""
    import publish_gate as pg

    result = result if isinstance(result, dict) else {}
    esc = pg.escalation(article, result) if result else None
    blocking = pg.evaluate(result)[2] if result else []
    return {
        "id": article.get("id"), "slug": article.get("slug", ""), "title": article.get("title", ""),
        "articleType": article.get("articleType", ""),
        "url": f"{SITE}/articles/{article.get('slug', '')}/",
        "verdict": result.get("verdict"), "reasons": list(reasons),
        "summary": result.get("summary", ""),
        "claims": [pg._claim_summary(c) for c in blocking],
        "primarySources": result.get("primarySources", []),
        "escalation": esc, "autofix": list(fixes),
    }


def decide(accepted_ids, results, articles_by_id, client_getter=None, fetcher=None, verify=None):
    """accepted_ids の監査結果から、記事ごとの合否と自動マージ可否を決める。
    戻り値: (decision dict, 修正した記事の dict{id: article})"""
    confirmed, held, fixed_articles = [], [], {}
    for aid in accepted_ids:
        article = articles_by_id.get(aid)
        result = results.get(aid)
        if article is None:
            held.append({"id": aid, "title": "", "reasons": ["data/articles.json に記事がない"],
                         "escalation": None, "claims": []})
            continue
        if result is None:
            held.append(hold_record(article, None, ["Fact Audit の結果がない(監査できていない)"]))
            continue
        passed, reasons = judge(result)
        fixes = []
        if not passed and client_getter is not None:
            fixed, fixes, verified = autofix_and_verify(article, result, client_getter, fetcher, verify)
            if fixed is not None:
                result = verified
                passed, reasons = judge(result)
                article = fixed
                fixed_articles[aid] = fixed
                if not passed:
                    reasons = ["自動修正後の verify で未解決"] + reasons
        if passed:
            confirmed.append(aid)
        else:
            held.append(hold_record(article, result, reasons, fixes))
    decision = {
        "accepted_ids": list(accepted_ids),
        "confirmed_ids": confirmed,
        "held": held,
        "autofixed_ids": sorted(fixed_articles),
        "all_confirmed": not held and len(confirmed) == len(accepted_ids),
    }
    decision["auto_merge"] = decision["all_confirmed"]
    return decision, fixed_articles


# ---------- Issue ----------

def encode_hold_payload(h, date, branch):
    payload = {"date": date, "branch": branch, "articleId": h.get("id"), "slug": h.get("slug", ""),
               "kind": (h.get("escalation") or {}).get("kind", ""),
               "imageChecks": (h.get("escalation") or {}).get("imageChecks") or []}
    return base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")


def decode_hold_payload(body):
    import re
    m = re.search(re.escape(HOLD_MARKER) + r"\s+([A-Za-z0-9+/=]+)\s*-->", body or "")
    if not m:
        return None
    try:
        data = json.loads(base64.b64decode(m.group(1)).decode("utf-8"))
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def hold_title(h, date):
    kind = (h.get("escalation") or {}).get("kind")
    label = "公式画像の確認" if kind == "image_reading" else "要判断"
    return f"{HOLD_TITLE_PREFIX} {label}: {h.get('title') or '(タイトル不明)'} (id:{h.get('id')}, {date})"


def hold_body(h, date, branch, pr_url):
    """保留記事の Issue 本文。判断は Approve / Reject の2択。"""
    esc = h.get("escalation") or {}
    reject = f"この記事を当日PR({branch})から外し、残りの記事だけを公開します"
    lines = []
    if esc.get("kind") == "image_reading":
        lines.append(f"対象トピック: {h.get('title', '')}(id:{h.get('id')})")
        checks = esc.get("imageChecks") or []
        for n, c in enumerate(checks, 1):
            pre = f"({n}) " if len(checks) > 1 else ""
            lines += [
                f"{pre}確認対象: {c.get('claim', '')}",
                f"{pre}AI読取候補: {c.get('candidate') or '(読み取れず)'}",
                f"{pre}確認してほしいこと: 画像の記載が記事の「{c.get('articleValue', '')}」と一致するか",
                f"{pre}公式ソースURL: {c.get('pageUrl') or '(記録なし)'}",
                f"{pre}画像直リンク: {c.get('imageUrl') or '(記録なし)'}",
            ]
        lines += [
            "[Approve] 記事の記載が正しいと確認済みとして記録し、当日PRをマージできる状態にします"
            "(値が違うときは `正しい値: …` を添えてください。その値に直してから記録します)",
            f"[Reject] 公式画像の記載は根拠にせず、{reject}",
        ]
    else:
        lines += [
            f"対象トピック: {h.get('title', '')}(id:{h.get('id')})",
            f"記事内: {esc.get('article') or '(該当する記載の特定なし)'}",
            f"公式情報: {esc.get('official') or h.get('summary') or '(記録なし)'}",
            f"Approve: {esc.get('approve') or '一次情報で確認できる内容に直して verify で再確認し、当日PRをマージできる状態にします'}",
            f"Reject: {reject}",
        ]
    lines += [
        "",
        "このIssueに `@claude Approve` または `@claude Reject` とコメントしてください。",
        "",
        f"Daily Articles の Fact Audit(full)で confirmed にならなかったため、当日PRは**自動マージしていません**: {pr_url}",
        "",
        "<details><summary>監査の詳細</summary>",
        "",
        f"- 判定: {h.get('verdict')}",
        f"- 理由: {' / '.join(h.get('reasons') or []) or '(記録なし)'}",
        f"- 記事URL(公開予定): {h.get('url', '')}",
    ]
    if h.get("summary"):
        lines.append(f"- 校閲メモ: {h['summary']}")
    for c in h.get("claims") or []:
        lines.append(f"- [{c.get('role')}/{c.get('status')}] {c.get('claim', '')} "
                     f"記事「{c.get('articleValue', '')}」/ 一次情報「{c.get('primaryValue', '')}」 {c.get('primaryUrl', '')}")
    for f in h.get("autofix") or []:
        lines.append(f"- 自動修正(verify で未解決): 「{f.get('from')}」→「{f.get('to') or '(削除)'}」")
    lines += ["", "他メディアは事実確認の根拠にしないでください。", "", "</details>", "",
              f"<!-- {HOLD_MARKER} {encode_hold_payload(h, date, branch)} -->"]
    return "\n".join(lines)


# ---------- 入出力 ----------

def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_output(**kv):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


def run_audit(args, client=None, fetcher=None, verify=None):
    import fact_audit as fa

    report = load_json(args.report)
    if not isinstance(report, dict):
        print(f"::error::実行レポート({args.report})を読み込めません", file=sys.stderr)
        return 1
    accepted = [int(i) for i in report.get("accepted_ids") or []]
    articles = g.load_json(args.articles)
    by_id = {a["id"]: a for a in articles}
    results = {}
    if accepted:
        os.makedirs(args.out_dir, exist_ok=True)
        code = fa.main(["--mode", "full", "--ids", ",".join(map(str, accepted)), "--out-dir", args.out_dir],
                       client=client, articles_override=articles, sleep_sec=AUDIT_SLEEP_SEC, fetcher=fetcher)
        if code:
            print(f"::error::Fact Audit が終了コード {code} で終了しました", file=sys.stderr)
            return 1
        results = fa.load_previous_results([args.out_dir])
    holder = {"client": client}

    def client_getter():
        if holder["client"] is None:
            holder["client"] = fa.make_client()
        return holder["client"]

    decision, fixed = decide(accepted, results, by_id, client_getter, fetcher, verify)
    decision["date"] = report.get("date") or ""
    if fixed:
        g.atomic_write_json(args.articles, [fixed.get(a["id"], a) for a in articles])
        print(f"一次情報で一意に直せる誤りを自動修正し、verify で確認しました: id={sorted(fixed)}")
    with open(args.decision, "w", encoding="utf-8") as f:
        json.dump(decision, f, ensure_ascii=False, indent=1)
    print(f"Fact Audit(full) 判定: confirmed {decision['confirmed_ids']} / 保留 {[h['id'] for h in decision['held']]}"
          f" → 自動マージ{'可' if decision['auto_merge'] else '不可'}")
    write_output(auto_merge=str(decision["auto_merge"]).lower(),
                 held_count=len(decision["held"]),
                 confirmed_ids=",".join(map(str, decision["confirmed_ids"])))
    return 0


def run_issues(args, request=rgr.github_request):
    decision = load_json(args.decision)
    if not isinstance(decision, dict):
        print(f"::error::判定結果({args.decision})がありません", file=sys.stderr)
        return 1
    held = decision.get("held") or []
    if not held:
        print("保留記事はありません。Issue は作りません。")
        return 0
    date = decision.get("date") or ""
    issues = [(hold_title(h, date), hold_body(h, date, args.branch, args.pr_url)) for h in held]
    if args.dry_run:
        for title, body in issues:
            print(f"--- {title}\n{body}\n")
        return 0
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("::error::GITHUB_TOKEN と GITHUB_REPOSITORY が必要です", file=sys.stderr)
        return 1
    existing = rgr.open_issue_titles(repo, token, request)
    for title, body in issues:
        if title in existing:
            print(f"同じタイトルのIssueが既にあるためスキップ: {title}")
            continue
        res = request("POST", f"/repos/{repo}/issues", token, {"title": title, "body": body})
        existing.add(title)
        print(f"Issueを作成しました: {title} {res.get('html_url', '') if isinstance(res, dict) else ''}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit")
    a.add_argument("--report", default=RUN_REPORT_PATH)
    a.add_argument("--articles", default=g.ARTICLES_JSON_PATH)
    a.add_argument("--decision", default=DECISION_PATH)
    a.add_argument("--out-dir", default=AUDIT_OUT_DIR)
    i = sub.add_parser("issues")
    i.add_argument("--decision", default=DECISION_PATH)
    i.add_argument("--pr-url", default="")
    i.add_argument("--branch", default="")
    i.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    return run_audit(args) if args.cmd == "audit" else run_issues(args)


if __name__ == "__main__":
    sys.exit(main())
