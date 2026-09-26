#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily Articles の当日branch/PR・CI・自動マージ・公開確認(main へ直接 push しない)
------------------------------------------------------------------------
Daily Articles workflow から、次のサブコマンドを順に呼ぶ。GitHub API は既存の GITHUB_TOKEN だけで使う。

  plan      … 当日branch(daily/YYYY-MM-DD)と当日PRの状態から、この実行で何をするかを決める
                fresh  : 当日branchもPRも無い → 最新mainから当日branchを作って記事を生成する
                resume : 当日branchまたは open の当日PRがある → 記事を生成し直さず、既存の branch/PR を再利用して
                         PR作成・CI・マージ・公開確認の続きだけを行う(同じ日の記事を重複生成しない)
                done   : 当日PRはマージ済み → 公開確認と X投稿・IndexNow だけを行う(投稿済みログで二重投稿しない)
                closed : 当日PRが人の手でマージせずに閉じられている → 何もしない(意図を尊重して再生成しない)
  record    … 実行レポートから、PR本文・コミットに埋め込むメタ情報(公開対象・保留のID)を作る
  open      … main 向けの当日PRを作る(既にあれば再利用する)
  ci        … 当日branchの head で CI workflow(ci.yml)を workflow_dispatch で起動し、完了まで待つ
              (GITHUB_TOKEN で作ったPRでは pull_request の CI が起動しないため)
  merge     … 自動マージの条件をすべて確認し、満たすときだけマージする。満たさなければIssueを作って失敗する
  published … マージ後、main への反映と各記事URLの公開(HTTP 200)を確認し、公開を確認できた記事IDだけを出力する
              (X投稿・IndexNow はこのIDだけを対象にする)
  after-merge … 人がマージしたPR(保留記事の Approve 等)で main に新しく入った記事について、同じく公開を確認する
              (Publish Articles After Merge workflow 用)

自動マージの条件(merge_blockers。1つでも満たさなければマージしない):
  - PR が open・base=main・head=当日branch(同じリポジトリ)・github-actions[bot] 作成・draft でない・競合なし
  - PR の head がこの実行で検証したコミットと同じ
  - 公開対象の記事がすべて Fact Audit(full)で confirmed(confirmed 以外の記事は保留として分離済み)
  - 保留記事が PR の data/articles.json に含まれていない / 公開対象の記事が含まれている
  - PR の変更ファイルが Daily Articles の元データ・生成物だけ(コードや workflow の変更を含まない)
  - PR の head で CI(tests / build / 生成物の差分チェック)が success
tests / build / 永続化検証(verify_daily_run.py)は、コミット前に workflow のステップで実行する(失敗すればここまで来ない)。

使い方(workflow から):
  python daily_pr.py plan | record | open | ci | merge | published
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import report_gate_rejections as rgr

ROOT = os.path.dirname(os.path.abspath(__file__))
RUN_REPORT_PATH = os.environ.get("SHONAN_DOORS_RUN_REPORT_PATH", os.path.join("/tmp", "shonan_doors_run_report.json"))
META_PATH = os.environ.get("SHONAN_DOORS_DAILY_META_PATH", os.path.join("/tmp", "shonan_doors_daily_meta.json"))
COMMIT_MSG_PATH = os.path.join("/tmp", "shonan_doors_daily_commit_msg.txt")
SITE_DOMAIN = "https://www.shonandoors.com"
BASE = "main"
BRANCH_PREFIX = "daily/"
BOT_LOGIN = "github-actions[bot]"
CI_WORKFLOW = "ci.yml"
META_MARKER = "daily-articles-meta:"
MERGE_ISSUE_PREFIX = "[Daily Articles] 自動マージを見送り"
# 当日PRに含まれてよいファイル(Daily Articles の元データと build.py の生成物だけ)
ALLOWED_FILES = ("data/articles.json", "data/id_counter.json", "data/stock_topics.json", "data/event_series.json",
                 "data/x_post_log.json", "index.html", "sitemap.xml", "robots.txt", "404.html",
                 "site.webmanifest", "ads.txt")
ALLOWED_DIRS = ("articles/", "area/", "category/", "page/", "privacy/", "about/", "contact/")
POLL_SEC = int(os.environ.get("DAILY_PR_POLL_SECONDS", "20"))
CI_TIMEOUT_SEC = int(os.environ.get("DAILY_PR_CI_TIMEOUT_SECONDS", "1800"))
PUBLISH_TIMEOUT_SEC = int(os.environ.get("DAILY_PR_PUBLISH_TIMEOUT_SECONDS", "1200"))


# ---------- GitHub API ----------

class GitHub:
    def __init__(self, repo, token, request=rgr.github_request):
        self.repo, self.token, self._request = repo, token, request

    def call(self, method, path, payload=None, missing_ok=False):
        try:
            return self._request(method, path.replace("{repo}", self.repo), self.token, payload)
        except urllib.error.HTTPError as e:
            if missing_ok and e.code == 404:
                return None
            raise

    def pulls_for_branch(self, branch):
        owner = self.repo.split("/")[0]
        return self.call("GET", f"/repos/{{repo}}/pulls?head={owner}:{branch}&base={BASE}&state=all&per_page=100") or []

    def ci_runs(self, sha):
        res = self.call("GET", f"/repos/{{repo}}/actions/workflows/{CI_WORKFLOW}/runs?head_sha={sha}&per_page=50") or {}
        return [r for r in res.get("workflow_runs") or [] if r.get("head_sha") == sha]


def github_from_env(request=rgr.github_request):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        raise SystemExit("::error::GITHUB_TOKEN と GITHUB_REPOSITORY が必要です")
    return GitHub(repo, token, request)


# ---------- メタ情報(PR本文・コミットに埋め込む) ----------

def today():
    return datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")


def branch_for(date):
    return f"{BRANCH_PREFIX}{date}"


def encode_meta(meta):
    return f"{META_MARKER} {json.dumps(meta, ensure_ascii=False, separators=(',', ':'))}"


def decode_meta(text):
    m = re.search(re.escape(META_MARKER) + r"\s*(\{.*?\})\s*(?:-->|$)", text or "", re.M)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def meta_from_report(report, date, branch):
    fa = report.get("fact_audit") if isinstance(report.get("fact_audit"), dict) else {}
    return {
        "date": date, "branch": branch,
        "published_ids": [i for i in report.get("accepted_ids") or []],
        "published_slugs": [s for s in report.get("accepted_slugs") or []],
        "held_ids": [h.get("id") for h in report.get("held") or [] if isinstance(h, dict)],
        "all_published_confirmed": bool(fa.get("all_published_confirmed")) and "verdicts" in fa,
    }


def load_meta():
    try:
        with open(META_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_meta(meta):
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)


def set_output(**kw):
    path = os.environ.get("GITHUB_OUTPUT")
    for k, v in kw.items():
        print(f"{k}={v}")
        if path:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{k}={v}\n")


def summary(text):
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n\n")


# ---------- plan ----------

def decide_mode(pulls, branch_exists):
    """当日PRの一覧と当日branchの有無から、この実行のモードと対象PRを決める。"""
    merged = [p for p in pulls if p.get("merged_at")]
    if merged:
        return "done", merged[0]
    opened = [p for p in pulls if p.get("state") == "open"]
    if opened:
        return "resume", opened[0]
    if pulls:  # マージされずに閉じられている = 人が止めた
        return "closed", pulls[0]
    if branch_exists:
        return "resume", None
    return "fresh", None


def cmd_plan(gh, date):
    branch = branch_for(date)
    pulls = gh.pulls_for_branch(branch)
    head = gh.call("GET", f"/repos/{{repo}}/commits/{branch}", missing_ok=True)
    mode, pr = decide_mode(pulls, head is not None)
    meta = decode_meta(pr.get("body")) if pr else None
    if meta is None and head is not None:
        meta = decode_meta((head.get("commit") or {}).get("message"))
    if mode in ("resume", "done") and meta is None:
        summary(f"::error::当日branch {branch} のメタ情報を読み取れません。branch/PR を確認してください。")
        return 1
    meta = dict(meta or {"date": date, "branch": branch}, pr=pr.get("number") if pr else None)
    save_meta(meta)
    notes = {
        "fresh": "当日branch/PRはありません。最新mainから当日branchを作って記事を生成します。",
        "resume": "当日branchまたはPRが既にあります。記事は生成し直さず、既存のbranch/PRで続きを行います。",
        "done": "当日PRはマージ済みです。公開確認と X投稿・IndexNow だけを行います(投稿済みの記事は投稿しません)。",
        "closed": f"当日PR #{pr.get('number') if pr else ''} はマージされずに閉じられています。記事の生成・マージは行いません。",
    }
    summary(f"## Daily Articles: 当日branch/PR\n\n- branch: `{branch}`\n- mode: **{mode}** — {notes[mode]}")
    set_output(mode=mode, branch=branch, date=date, pr=meta.get("pr") or "")
    return 0


# ---------- record / open ----------

def pr_body(meta):
    pub, held = meta.get("published_ids") or [], meta.get("held_ids") or []
    lines = [
        f"Daily Articles({meta.get('date')})の自動PRです。",
        "",
        f"- 公開対象(Fact Audit full で confirmed): {len(pub)}件 {pub}",
        f"- 保留(confirmed 以外。このPRには含めず、記事ごとの Issue で Approve / Reject を確認): {len(held)}件 {held}",
        "",
        "公開対象の記事だけで build し直した内容です。tests / build / 永続化検証 / CI がすべて成功した場合に限り、"
        "CLAUDE.md の Daily Articles 限定の例外に従って自動マージします。",
        "",
        "🤖 Generated by Daily Articles workflow",
        "",
        f"<!-- {encode_meta(meta)} -->",
    ]
    return "\n".join(lines)


def cmd_record(date, branch):
    with open(RUN_REPORT_PATH, encoding="utf-8") as f:
        report = json.load(f)
    meta = dict(load_meta(), **meta_from_report(report, date, branch))
    save_meta(meta)
    with open(COMMIT_MSG_PATH, "w", encoding="utf-8") as f:
        f.write(f"chore: 本日分の記事を自動追加 ({date})\n\n"
                f"公開対象: {meta['published_ids']} / 保留: {meta['held_ids']}\n\n{encode_meta(meta)}\n")
    set_output(published_ids=",".join(str(i) for i in meta["published_ids"]))
    return 0


def cmd_open(gh, meta):
    branch = meta["branch"]
    opened = [p for p in gh.pulls_for_branch(branch) if p.get("state") == "open"]
    if opened:
        pr = opened[0]
        print(f"既存の当日PR #{pr['number']} を再利用します。")
    else:
        pr = gh.call("POST", "/repos/{repo}/pulls", {
            "title": f"Daily Articles {meta['date']}", "head": branch, "base": BASE, "body": pr_body(meta)})
        print(f"当日PR #{pr['number']} を作成しました: {pr.get('html_url', '')}")
    meta["pr"] = pr["number"]
    save_meta(meta)
    set_output(pr=pr["number"])
    return 0


# ---------- ci ----------

def ci_state(runs, since=None):
    """CI の実行一覧から状態を返す: success / failure / pending / none。
    since を指定すると、それ以降に作られた実行(この呼び出しで起動したもの)と実行中のものだけを見る。"""
    if any(r.get("status") == "completed" and r.get("conclusion") == "success" for r in runs):
        return "success"
    live = [r for r in runs if r.get("status") != "completed"]
    if live:
        return "pending"
    recent = [r for r in runs if since is None or str(r.get("created_at", "")) >= since]
    if recent:
        return "failure"
    return "none"


def cmd_ci(gh, meta, sleep=time.sleep, clock=time.monotonic):
    pr = gh.call("GET", f"/repos/{{repo}}/pulls/{meta['pr']}")
    sha = pr["head"]["sha"]
    state = ci_state(gh.ci_runs(sha))
    since = None
    if state in ("none", "failure"):
        # 時計のずれで起動した実行を見落とさないよう、少し前の時刻を基準にする
        since = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
        gh.call("POST", f"/repos/{{repo}}/actions/workflows/{CI_WORKFLOW}/dispatches", {"ref": meta["branch"]})
        print(f"CI({CI_WORKFLOW})を {meta['branch']}@{sha[:7]} で起動しました。")
    deadline = clock() + CI_TIMEOUT_SEC
    while True:
        state = ci_state(gh.ci_runs(sha), since)
        if state == "success":
            summary(f"CI 成功: {meta['branch']}@{sha[:7]}")
            return 0
        if state == "failure":
            summary(f"::error::CI が失敗しました({meta['branch']}@{sha[:7]})。自動マージは行いません。")
            return 1
        if clock() >= deadline:
            summary(f"::error::CI が {CI_TIMEOUT_SEC}秒以内に完了しませんでした。自動マージは行いません。")
            return 1
        sleep(POLL_SEC)


# ---------- merge ----------

def changed_files(gh, number):
    files = []
    for page in range(1, 31):
        items = gh.call("GET", f"/repos/{{repo}}/pulls/{number}/files?per_page=100&page={page}") or []
        files += [i.get("filename", "") for i in items]
        if len(items) < 100:
            break
    return files


def is_allowed_file(path):
    return path in ALLOWED_FILES or path.startswith(ALLOWED_DIRS)


def merge_blockers(meta, pr, repo, local_sha, article_ids, files, ci):
    """自動マージしてはいけない理由の一覧(空ならマージしてよい)。"""
    b = []
    if pr.get("state") != "open":
        b.append(f"PR が open ではない(state={pr.get('state')})")
    if (pr.get("base") or {}).get("ref") != BASE:
        b.append("PR の base が main ではない")
    head = pr.get("head") or {}
    if head.get("ref") != meta.get("branch") or ((head.get("repo") or {}).get("full_name")) != repo:
        b.append("PR の head が当日branch(同じリポジトリ)ではない")
    if (pr.get("user") or {}).get("login") != BOT_LOGIN:
        b.append(f"PR の作成者が {BOT_LOGIN} ではない")
    if pr.get("draft"):
        b.append("PR が draft")
    if pr.get("mergeable") is False:
        b.append("PR に競合がある(mergeable=false)")
    if head.get("sha") != local_sha:
        b.append("PR の head がこの実行で検証したコミットと異なる")
    if not meta.get("all_published_confirmed"):
        b.append("公開対象に Fact Audit(full)で confirmed になっていない記事がある")
    held_in = sorted(set(meta.get("held_ids") or []) & set(article_ids))
    if held_in:
        b.append(f"保留記事 {held_in} が data/articles.json に含まれている")
    missing = sorted(set(meta.get("published_ids") or []) - set(article_ids))
    if missing:
        b.append(f"公開対象の記事 {missing} が data/articles.json にない")
    extra = [f for f in files if not is_allowed_file(f)]
    if extra:
        b.append(f"Daily Articles の元データ・生成物以外の変更を含む: {extra[:10]}")
    if not files:
        b.append("PR に変更ファイルがない")
    if ci != "success":
        b.append(f"PR の head で CI が success ではない({ci})")
    return b


def report_merge_blocked(gh, meta, reasons):
    title = f"{MERGE_ISSUE_PREFIX} ({meta.get('date')})"
    body = "\n".join([f"Daily Articles({meta.get('date')})の当日PR #{meta.get('pr')} は、次の理由で自動マージしませんでした。",
                      "", *[f"- {r}" for r in reasons], "",
                      "記事は公開されていません(X投稿・IndexNow も行っていません)。PR を確認し、問題なければオーナーの判断でマージしてください。",
                      "マージ後の X投稿・IndexNow は「Publish Articles After Merge」workflow が行います。"])
    try:
        if title not in rgr.open_issue_titles(gh.repo, gh.token, gh._request):
            gh.call("POST", "/repos/{repo}/issues", {"title": title, "body": body})
    except Exception as e:  # Issue化の失敗でマージ見送りの判断は変えない
        print(f"::warning::Issue の作成に失敗しました: {e}")


def local_head():
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout.strip()


def local_article_ids():
    with open(os.path.join(ROOT, "data", "articles.json"), encoding="utf-8") as f:
        return [a.get("id") for a in json.load(f)]


def cmd_merge(gh, meta, sleep=time.sleep):
    pr = gh.call("GET", f"/repos/{{repo}}/pulls/{meta['pr']}")
    for _ in range(10):  # mergeable は計算中だと null になる
        if pr.get("mergeable") is not None:
            break
        sleep(5)
        pr = gh.call("GET", f"/repos/{{repo}}/pulls/{meta['pr']}")
    sha = (pr.get("head") or {}).get("sha", "")
    reasons = merge_blockers(meta, pr, gh.repo, local_head(), local_article_ids(),
                             changed_files(gh, meta["pr"]), ci_state(gh.ci_runs(sha)) if sha else "none")
    if not reasons:
        try:
            res = gh.call("PUT", f"/repos/{{repo}}/pulls/{meta['pr']}/merge", {
                "sha": sha, "merge_method": "merge",
                "commit_title": f"Daily Articles {meta.get('date')} (#{meta['pr']})"})
            summary(f"当日PR #{meta['pr']} を自動マージしました(merge: {(res or {}).get('sha', '')[:7]})。")
            return 0
        except urllib.error.HTTPError as e:
            reasons = [f"マージ API が失敗しました(HTTP {e.code}。main の保護ルールの条件を満たしていない可能性)"]
    summary("::error::自動マージの条件を満たしていません:\n" + "\n".join(f"- {r}" for r in reasons))
    report_merge_blocked(gh, meta, reasons)
    return 1


# ---------- published ----------

def url_ok(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ShonanDoorsDailyPublish/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception:
        return False


def wait_published(ids, slugs, check=url_ok, sleep=time.sleep, clock=time.monotonic, timeout=None):
    """各記事URLが HTTP 200 になるまで待つ。戻り値: 公開を確認できた記事ID(元の順)"""
    pending = dict(zip(ids, slugs))
    live = set()
    deadline = clock() + (PUBLISH_TIMEOUT_SEC if timeout is None else timeout)
    while pending:
        for i, slug in list(pending.items()):
            if check(f"{SITE_DOMAIN}/articles/{slug}/"):
                live.add(i)
                pending.pop(i)
        if not pending or clock() >= deadline:
            break
        sleep(POLL_SEC)
    return [i for i in ids if i in live]


def cmd_published(gh, meta, check=url_ok, sleep=time.sleep):
    pr = gh.call("GET", f"/repos/{{repo}}/pulls/{meta['pr']}") if meta.get("pr") else None
    if not pr or not pr.get("merged_at"):
        summary("::error::当日PRがマージされていないため、公開確認・X投稿・IndexNow は行いません。")
        set_output(article_ids="")
        return 1
    cmp = gh.call("GET", f"/repos/{{repo}}/compare/{pr['merge_commit_sha']}...{BASE}") or {}
    if cmp.get("status") not in ("identical", "ahead"):
        summary("::error::マージコミットが main に反映されていません。")
        set_output(article_ids="")
        return 1
    ids, slugs = meta.get("published_ids") or [], meta.get("published_slugs") or []
    live = wait_published(ids, slugs, check=check, sleep=sleep)
    missing = [i for i in ids if i not in live]
    summary(f"## Daily Articles: 公開確認\n\n公開を確認: {live}"
            + (f"\n\n::warning::{PUBLISH_TIMEOUT_SEC}秒以内に公開を確認できなかった記事(X投稿・IndexNow の対象外): {missing}"
               if missing else ""))
    set_output(article_ids=",".join(str(i) for i in live))
    return 0


def articles_at(ref):
    """指定コミットの data/articles.json(取れなければ None)"""
    r = subprocess.run(["git", "show", f"{ref}:data/articles.json"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def newly_added(before, after):
    """before → after で新しく公開された記事(統合済みを除く)。戻り値: [(id, slug)]"""
    old = {a.get("id") for a in before or []}
    return [(a["id"], a["slug"]) for a in after or [] if a.get("id") not in old and not a.get("mergedInto")]


def cmd_after_merge(before, after, check=url_ok, sleep=time.sleep):
    """人がマージしたPR(保留記事の Approve など)で main に新しく入った記事の公開を確認し、IDを出力する。
    Daily Articles の自動マージ(GITHUB_TOKEN による push)ではこの workflow は起動しない。"""
    old, new = articles_at(before), articles_at(after)
    if new is None:
        summary("::error::マージ後の data/articles.json を読み取れません。")
        set_output(article_ids="")
        return 1
    added = newly_added(old or [], new) if old is not None else []
    if not added:
        summary("新しく公開された記事はありません(X投稿・IndexNow は行いません)。")
        set_output(article_ids="")
        return 0
    ids, slugs = [i for i, _ in added], [s for _, s in added]
    live = wait_published(ids, slugs, check=check, sleep=sleep)
    summary(f"## マージ後の公開確認\n\n新しく公開された記事: {ids} / 公開を確認: {live}")
    set_output(article_ids=",".join(str(i) for i in live))
    return 0


def main(argv=None, gh=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["plan", "record", "open", "ci", "merge", "published", "after-merge"])
    ap.add_argument("--date", default="")
    ap.add_argument("--before", default="", help="after-merge: push 前のコミット")
    ap.add_argument("--after", default="HEAD", help="after-merge: push 後のコミット")
    args = ap.parse_args(argv)
    date = args.date or today()
    if args.command == "record":
        return cmd_record(date, branch_for(date))
    if args.command == "after-merge":
        return cmd_after_merge(args.before, args.after)
    gh = gh or github_from_env()
    if args.command == "plan":
        return cmd_plan(gh, date)
    meta = load_meta()
    if not meta.get("branch"):
        print("::error::メタ情報がありません(先に plan を実行してください)", file=sys.stderr)
        return 1
    if args.command == "open":
        return cmd_open(gh, meta)
    if args.command == "ci":
        return cmd_ci(gh, meta)
    if args.command == "merge":
        return cmd_merge(gh, meta)
    return cmd_published(gh, meta)


if __name__ == "__main__":
    sys.exit(main())
