#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily Articles の当日branch / PR / 自動マージ / 公開確認
------------------------------------------------------------------------
main へは直接pushしない。当日分は `daily/<JSTの日付>` branch に commit/push し、
main 向けPRを作って、次の条件をすべて満たしたときだけ自動マージする(merge_decision)。

  - 当日commitに記録した Fact Audit の判定が confirmed(Daily-Fact-Audit: confirmed)
  - 当日commitに記録した tests / build / 永続化検証 / 生成物の差分チェックが合格(Daily-Checks: passed)
  - PR が open・main 向け・当日branchから・draft ではない
  - PR の head が bot の当日commitのまま(人やほかの処理が後から積んだcommitがあれば自動マージしない)
  - CI(ci.yml)を当日branchで workflow_dispatch し、同じcommitで success
  - GitHub が mergeable=false と判定していない

GITHUB_TOKEN が作ったPRでは pull_request イベントのworkflowが起動しないため、CI は
workflow_dispatch(GITHUB_TOKEN からでも起動できる)で当日branchに対して明示的に実行し、結果を待つ。
新規PAT・GitHub App は使わない。mainの保護ルール(ruleset)にも触れない。

同じ日付の重複防止・再実行(plan):
  - 当日PRがマージ済み      → published   : 生成しない(公開確認・X投稿・IndexNowだけ行う)
  - 当日PRが open           → resume_open : 生成しない(条件を満たせばマージ段階から再開)
  - 当日PRが未マージでclose → abandoned   : 生成しない(オーナーが見送った日として扱う)
  - branchだけある(PRなし) → resume_branch: 生成しない(PRを作ってマージ段階から再開)
  - どれも無い              → generate    : 最新mainから当日branchを作って生成する

使い方(GitHub Actions から呼ぶ。GITHUB_TOKEN / GITHUB_REPOSITORY が必要):
  python daily_pr.py plan [--date YYYY-MM-DD]
  python daily_pr.py trailers --date D --accepted-ids 1,2 --decision /tmp/decision.json
  python daily_pr.py open-pr --branch daily/D [--decision FILE]
  python daily_pr.py merge-stage --branch daily/D
  python daily_pr.py publish-targets (--merge-sha SHA | --article-ids 1,2)
  python daily_pr.py wait-published --article-ids 1,2
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import report_gate_rejections as rgr

ROOT = os.path.dirname(os.path.abspath(__file__))
BRANCH_PREFIX = "daily/"
BASE_BRANCH = "main"
CI_WORKFLOW = "ci.yml"
CI_TIMEOUT_SEC = 30 * 60
CI_POLL_SEC = 20
PUBLISH_TIMEOUT_SEC = 20 * 60
PUBLISH_POLL_SEC = 20
SITE = "https://www.shonandoors.com"

TRAILER_DATE = "Daily-Run-Date"
TRAILER_IDS = "Daily-Accepted-Ids"
TRAILER_AUDIT = "Daily-Fact-Audit"
TRAILER_CHECKS = "Daily-Checks"


def today_jst():
    return datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")


def branch_name(date):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        raise ValueError(f"日付の形式が不正です: {date!r}")
    return BRANCH_PREFIX + date


# ---------- commit trailer(当日commitに判定結果を残す) ----------

def commit_trailers(date, accepted_ids, decision):
    """当日commitのメッセージ末尾に付ける判定結果。commit/push はすべてのチェック合格後にしか
    行わないので、Daily-Checks は常に passed。Fact Audit の判定は confirmed か held。"""
    audit = "confirmed" if isinstance(decision, dict) and decision.get("auto_merge") is True else "held"
    return "\n".join([
        f"{TRAILER_DATE}: {date}",
        f"{TRAILER_IDS}: {','.join(str(i) for i in accepted_ids)}",
        f"{TRAILER_AUDIT}: {audit}",
        f"{TRAILER_CHECKS}: passed",
    ])


def parse_trailers(message):
    out = {}
    for line in (message or "").splitlines():
        m = re.fullmatch(r"(Daily-[A-Za-z-]+):\s*(.*)", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def parse_ids(text):
    return [int(x) for x in re.split(r"[,\s]+", text or "") if x.strip().isdigit()]


# ---------- 判定(純粋関数。テスト対象) ----------

def decide_mode(branch_exists, prs):
    """同じ日付のbranch/PRの状態から、この実行で何をするかを決める。戻り値: (mode, pr)"""
    prs = [p for p in prs or [] if isinstance(p, dict)]
    merged = [p for p in prs if p.get("merged_at")]
    if merged:
        return "published", merged[0]
    opened = [p for p in prs if p.get("state") == "open"]
    if opened:
        return "resume_open", opened[0]
    if prs:
        return "abandoned", prs[0]
    if branch_exists:
        return "resume_branch", None
    return "generate", None


def merge_decision(trailers, pr, branch, bot_sha, ci_conclusion):
    """自動マージしてよいか。戻り値: (ok, reasons)。reasons は不可の理由(ok なら空)。"""
    reasons = []
    if trailers.get(TRAILER_AUDIT) != "confirmed":
        reasons.append(f"Fact Audit が confirmed ではない({trailers.get(TRAILER_AUDIT) or '記録なし'})")
    if trailers.get(TRAILER_CHECKS) != "passed":
        reasons.append("tests / build / 永続化検証の合格記録がない")
    if not bot_sha:
        reasons.append("当日commitを特定できない")
    if not isinstance(pr, dict):
        reasons.append("当日PRがない")
    else:
        if pr.get("state") != "open" or pr.get("merged_at"):
            reasons.append("PRが open ではない")
        if pr.get("draft"):
            reasons.append("PRが draft")
        if (pr.get("base") or {}).get("ref") != BASE_BRANCH:
            reasons.append(f"PRのマージ先が {BASE_BRANCH} ではない")
        if (pr.get("head") or {}).get("ref") != branch:
            reasons.append(f"PRのbranchが {branch} ではない")
        if bot_sha and (pr.get("head") or {}).get("sha") != bot_sha:
            reasons.append("PRのheadが当日commitではない(後からcommitが追加されている)")
        if pr.get("mergeable") is False:
            reasons.append("GitHub がマージ不可(コンフリクト等)と判定")
    if ci_conclusion != "success":
        reasons.append(f"CI が success ではない({ci_conclusion or '未実行'})")
    return not reasons, reasons


def new_article_ids(before, after):
    """マージ前後の articles.json から、今回 main に入った(=公開確定した)記事IDを返す。"""
    old = {a.get("id") for a in before or [] if isinstance(a, dict)}
    return [a["id"] for a in after or []
            if isinstance(a, dict) and isinstance(a.get("id"), int)
            and a["id"] not in old and not a.get("mergedInto")]


# ---------- GitHub API ----------

class Api:
    def __init__(self, repo=None, token=None, request=rgr.github_request):
        self.repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        self.request = request
        if not self.repo or not self.token:
            raise SystemExit("::error::GITHUB_TOKEN と GITHUB_REPOSITORY が必要です")

    def call(self, method, path, payload=None):
        return self.request(method, f"/repos/{self.repo}{path}", self.token, payload)

    def branch_sha(self, branch):
        """branch の先頭commitのSHA。branch が無ければ空文字(git refs API は / を含む名前もそのまま扱える)。"""
        try:
            ref = self.call("GET", f"/git/ref/heads/{branch}") or {}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return ""
            raise
        return (ref.get("object") or {}).get("sha", "")

    def branch_exists(self, branch):
        return bool(self.branch_sha(branch))

    def prs_for(self, branch):
        owner = self.repo.split("/")[0]
        head = urllib.parse.quote(f"{owner}:{branch}", safe=":/")
        return self.call("GET", f"/pulls?state=all&head={head}&per_page=30") or []

    def pr(self, number):
        return self.call("GET", f"/pulls/{number}")

    def head_commit(self, branch):
        sha = self.branch_sha(branch)
        if not sha:
            return "", ""
        c = self.call("GET", f"/git/commits/{sha}") or {}
        return sha, c.get("message", "")


def write_output(**kv):
    path = os.environ.get("GITHUB_OUTPUT")
    for k, v in kv.items():
        print(f"{k}={v}")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")


def summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


# ---------- サブコマンド ----------

def cmd_plan(args, api=None):
    api = api or Api()
    date = args.date or today_jst()
    branch = branch_name(date)
    mode, pr = decide_mode(api.branch_exists(branch), api.prs_for(branch))
    print(f"{date} の Daily Articles: mode={mode}"
          + (f" (PR #{pr.get('number')})" if pr else ""))
    write_output(date=date, branch=branch, mode=mode,
                 pr_number=(pr or {}).get("number", ""),
                 merge_sha=(pr or {}).get("merge_commit_sha", "") if mode == "published" else "")
    return 0


def cmd_trailers(args):
    decision = load_json(args.decision) if args.decision else None
    print(commit_trailers(args.date, parse_ids(args.accepted_ids), decision))
    return 0


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def pr_body(date, trailers, decision, run_url=""):
    ids = parse_ids(trailers.get(TRAILER_IDS, ""))
    audit = trailers.get(TRAILER_AUDIT, "held")
    lines = [
        f"{date} の Daily Articles(自動生成)です。",
        "",
        f"- 公開対象(accepted_ids): {', '.join(map(str, ids)) or 'なし(台帳の更新のみ)'}",
        f"- Fact Audit(full): {'すべて confirmed' if audit == 'confirmed' else 'confirmed 以外あり → 自動マージしません'}",
        "- tests / build / 永続化検証 / 生成物の差分チェック: 合格",
        "",
        "Fact Audit・tests・build・CI がすべて合格した場合だけ、Daily Articles workflow がこのPRを自動マージします"
        "(CLAUDE.md の Daily Articles 自動運用の例外)。自動マージしない場合は、保留理由のIssueを確認してください。",
    ]
    held = (decision or {}).get("held") or []
    if held:
        lines += ["", "### 保留(confirmed 以外)の記事", ""]
        lines += [f"- id:{h.get('id')} {h.get('title', '')} — {' / '.join(h.get('reasons') or [])}" for h in held]
    if (decision or {}).get("autofixed_ids"):
        lines += ["", f"一次情報で一意に直せる誤りを自動修正し verify で確認した記事: {decision['autofixed_ids']}"]
    if run_url:
        lines += ["", f"実行ログ: {run_url}"]
    return "\n".join(lines)


def ensure_pr(api, branch, title, body):
    """当日branchの open なPRを返す。無ければ作る(同じbranchのPRを重複して作らない)。"""
    for p in api.prs_for(branch):
        if p.get("state") == "open":
            return p, False
    pr = api.call("POST", "/pulls", {"title": title, "head": branch, "base": BASE_BRANCH, "body": body})
    return pr, True


def run_url():
    server, repo, run = (os.environ.get(k, "") for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else ""


def cmd_open_pr(args, api=None):
    api = api or Api()
    _sha, message = api.head_commit(args.branch)
    trailers = parse_trailers(message)
    date = trailers.get(TRAILER_DATE) or args.branch[len(BRANCH_PREFIX):]
    decision = load_json(args.decision) if args.decision else None
    pr, created = ensure_pr(api, args.branch, f"Daily Articles {date}",
                            pr_body(date, trailers, decision, run_url()))
    print(f"{'PRを作成しました' if created else '既存のPRを再利用します'}: #{pr.get('number')} {pr.get('html_url', '')}")
    write_output(pr_number=pr.get("number", ""), pr_url=pr.get("html_url", ""))
    return 0


def dispatch_ci_and_wait(api, branch, sha, timeout=CI_TIMEOUT_SEC, poll=CI_POLL_SEC, sleep=time.sleep,
                         now=time.monotonic):
    """当日branchで CI を workflow_dispatch し、同じcommitのrunの結論を返す(success / failure / timeout 等)。"""
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    api.call("POST", f"/actions/workflows/{CI_WORKFLOW}/dispatches", {"ref": branch})
    deadline = now() + timeout
    while True:
        sleep(poll)
        runs = (api.call("GET", f"/actions/workflows/{CI_WORKFLOW}/runs?branch={branch}"
                                f"&event=workflow_dispatch&head_sha={sha}&per_page=10") or {}).get("workflow_runs") or []
        runs = [r for r in runs if r.get("head_sha") == sha and (r.get("created_at") or "") >= started]
        if runs:
            run = sorted(runs, key=lambda r: r.get("created_at") or "")[-1]
            if run.get("status") == "completed":
                print(f"CI: {run.get('conclusion')} {run.get('html_url', '')}")
                return run.get("conclusion") or "unknown"
        if now() >= deadline:
            return "timeout"


def cmd_merge_stage(args, api=None, ci=dispatch_ci_and_wait):
    """当日PRを用意し、CIを実行して、すべての条件を満たすときだけマージする。"""
    api = api or Api()
    bot_sha, message = api.head_commit(args.branch)
    trailers = parse_trailers(message)
    if not trailers:
        bot_sha = ""  # bot の当日commitではない
    date = trailers.get(TRAILER_DATE) or args.branch[len(BRANCH_PREFIX):]
    pr, _created = ensure_pr(api, args.branch, f"Daily Articles {date}", pr_body(date, trailers, None, run_url()))
    number = pr.get("number")
    if trailers.get(TRAILER_AUDIT) != "confirmed" or not bot_sha:
        # Fact Audit の保留(人の判断待ち)は正常な結果。CIは回さず、マージもしない
        msg = (f"PR #{number} は自動マージしません: Fact Audit で confirmed 以外の記事があるか、"
               "当日commitの判定記録がありません。保留理由のIssueを確認してください。")
        print(msg)
        summary(f"### Daily Articles: 自動マージしない(保留)\n\n{msg}\n")
        write_output(merged="false", pr_number=number, merge_sha="")
        return 0
    conclusion = ci(api, args.branch, bot_sha)
    ok, reasons = merge_decision(trailers, api.pr(number), args.branch, bot_sha, conclusion)
    if not ok:
        msg = f"PR #{number} は自動マージしません: " + " / ".join(reasons)
        print(f"::error::{msg}", file=sys.stderr)
        summary(f"### Daily Articles: 自動マージしない\n\n{msg}\n")
        write_output(merged="false", pr_number=number, merge_sha="")
        return 1
    try:
        res = api.call("PUT", f"/pulls/{number}/merge",
                       {"merge_method": "merge", "sha": bot_sha,
                        "commit_title": f"Daily Articles {date} (#{number})"})
    except urllib.error.HTTPError as e:  # ruleset の条件を満たさない・head が変わった等
        res = {"merged": False, "message": f"HTTP {e.code}"}
    merge_sha = (res or {}).get("sha", "")
    if not (res or {}).get("merged"):
        print(f"::error::PR #{number} のマージに失敗しました: {(res or {}).get('message', '')}", file=sys.stderr)
        write_output(merged="false", pr_number=number, merge_sha="")
        return 1
    print(f"PR #{number} をマージしました: {merge_sha}")
    summary(f"### Daily Articles: PR #{number} を自動マージ\n\nFact Audit / tests / build / CI すべて合格。merge: {merge_sha}\n")
    write_output(merged="true", pr_number=number, merge_sha=merge_sha)
    return 0


def git_show_json(rev, path, runner=subprocess.run):
    r = runner(["git", "show", f"{rev}:{path}"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def cmd_publish_targets(args, runner=subprocess.run):
    """公開確定した記事ID(=マージで main に入った記事)を返す。--article-ids 指定時は main に実在するものだけ。"""
    if args.merge_sha:
        before = git_show_json(f"{args.merge_sha}^1", "data/articles.json", runner)
        after = git_show_json(args.merge_sha, "data/articles.json", runner)
        if before is None or after is None:
            print("::error::マージ前後の data/articles.json を読めません(fetch-depth を確認してください)", file=sys.stderr)
            return 1
        ids = new_article_ids(before, after)
    else:
        with open(os.path.join(ROOT, "data", "articles.json"), encoding="utf-8") as f:
            live = {a["id"] for a in json.load(f) if not a.get("mergedInto")}
        ids = [i for i in parse_ids(args.article_ids) if i in live]
    print(f"公開確定した記事ID: {ids}")
    write_output(article_ids=",".join(map(str, ids)))
    return 0


def http_ok(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ShonanDoorsPublishCheck/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def wait_published(urls_by_id, check=http_ok, timeout=PUBLISH_TIMEOUT_SEC, poll=PUBLISH_POLL_SEC,
                   sleep=time.sleep, now=time.monotonic):
    """記事URLが本番で HTTP 200 になるまで待つ。戻り値: 公開を確認できた記事IDのリスト(入力順)。"""
    pending, done = dict(urls_by_id), set()
    deadline = now() + timeout
    while pending:
        for aid, url in list(pending.items()):
            if check(url):
                print(f"公開確認OK: id={aid} {url}")
                done.add(aid)
                pending.pop(aid)
        if not pending or now() >= deadline:
            break
        sleep(poll)
    for aid, url in pending.items():
        print(f"::warning::公開を確認できませんでした(X投稿・IndexNowの対象外): id={aid} {url}")
    return [aid for aid in urls_by_id if aid in done]


def cmd_wait_published(args):
    ids = parse_ids(args.article_ids)
    with open(os.path.join(ROOT, "data", "articles.json"), encoding="utf-8") as f:
        by_id = {a["id"]: a for a in json.load(f)}
    urls = {i: f"{SITE}/articles/{by_id[i]['slug']}/" for i in ids if i in by_id}
    published = wait_published(urls) if urls else []
    write_output(article_ids=",".join(map(str, published)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--date", default="")
    t = sub.add_parser("trailers")
    t.add_argument("--date", required=True)
    t.add_argument("--accepted-ids", default="")
    t.add_argument("--decision", default="")
    o = sub.add_parser("open-pr")
    o.add_argument("--branch", required=True)
    o.add_argument("--decision", default="")
    m = sub.add_parser("merge-stage")
    m.add_argument("--branch", required=True)
    pt = sub.add_parser("publish-targets")
    pt.add_argument("--merge-sha", default="")
    pt.add_argument("--article-ids", default="")
    w = sub.add_parser("wait-published")
    w.add_argument("--article-ids", default="")
    args = ap.parse_args(argv)
    return {"plan": cmd_plan, "trailers": cmd_trailers, "open-pr": cmd_open_pr,
            "merge-stage": cmd_merge_stage, "publish-targets": cmd_publish_targets,
            "wait-published": cmd_wait_published}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
