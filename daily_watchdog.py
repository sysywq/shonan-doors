#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily Publication Watchdog: Daily Articles とは独立に、その日の公開を本番URLで確認して安全に再実行する
------------------------------------------------------------------------
daily-publication-watchdog.yml から30分ごと(JST 05:23〜23:53)に呼ばれる。記事の生成・監査・投稿はしない。
判定に使うのは GitHub の run 一覧・日次状態(bot/daily-state)・main の data/articles.json・本番URLだけ。

検出するもの
  - cron 欠落/遅延 … JST 05:20 を過ぎても当日の Daily Articles run が1本も無い
  - 失敗           … 当日の最後の run が failure/cancelled 等で終わり、実行中の run も無い
  - 3本未達        … 当日の confirmed(main の当日記事)が DAILY_MIN_ARTICLES 未満で、実行中の run も無い
  - 本番未反映     … main には当日記事が3本以上あるのに、本番URL(/articles/{slug}/)の HTTP 200 か
                      トップページへの掲載が3本に届かない
復旧
  - Daily Articles を workflow_dispatch する(daily_pr.py plan が当日branch/PR・日次状態から続きを決めるので、
    同じ日に記事を作り直したり、公開済み記事を重複させたりしない。SNS は投稿済みログで二重投稿しない)
  - 本番未反映だけのときは Daily Articles を起動せず、GitHub Pages の再ビルドだけを要求する
安全装置(無限ループ・API浪費の防止)
  - 実行中/待機中の run があれば何もしない(run の updated_at は実行中に更新されないので使わない。
    run_started_at から job の timeout を超えた run だけを停滞とみなす)
  - 日次状態が停止(system_failure / round_limit)の日は起動しない(オーナーの手動実行だけが再開できる)
  - 当日の PR がマージされずに閉じられている(人が止めた)日は起動しない
  - 1日の Daily Articles run 数と、連続 failure 数に上限を設ける。上限に達したら起動をやめて Issue で通知する
  - 直前の run が終わってすぐ(補充 run の起動待ち)は待つ

Fact Audit・一次情報・鮮度・重複の基準はこのモジュールでは一切扱わない。
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import daily_state as ds

JST = ZoneInfo("Asia/Tokyo")
API = "https://api.github.com"
SITE_DOMAIN = "https://www.shonandoors.com"
DAILY_WORKFLOW = "daily-articles.yml"
ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")
FAILED = ("failure", "cancelled", "timed_out", "startup_failure", "action_required", "stale")
START_AFTER = (5, 20)          # cron は JST 05:07。これより前は当日の run が無くても待つ
JOB_TIMEOUT_MIN = 300          # daily-articles.yml の timeout-minutes
STALE_MIN = JOB_TIMEOUT_MIN + 30
COOLDOWN_MIN = 5               # 直前の run 終了から、補充 run が現れるまでの猶予
DEPLOY_GRACE_MIN = 20          # main 反映から本番に出るまでの猶予
MAX_REDEPLOYS = 2              # 1日に Pages 再ビルドを要求する上限
WATCHDOG_RETRIES = 3           # 補充 round とは別に、失敗・欠落の復旧で起動してよい回数
MAX_CONSECUTIVE_FAILURES = 3   # 当日の run がこの回数連続で failure なら起動をやめる
ISSUE_PREFIX = "[Daily Publication Watchdog] 自動再実行を停止"

OK, WAIT, DISPATCH, REDEPLOY, HALT, STOPPED = "ok", "wait", "dispatch", "redeploy", "halt", "stopped"


def max_runs_per_day():
    """1日の Daily Articles run 数の上限(round 0 + 補充 round + watchdog の復旧分)。"""
    return 1 + ds.DAILY_MAX_REFILL_ROUNDS + WATCHDOG_RETRIES


def parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def jst_date(value):
    ts = parse_ts(value)
    return ts.astimezone(JST).date().isoformat() if ts else ""


def minutes_since(value, now):
    ts = parse_ts(value)
    return (now - ts).total_seconds() / 60 if ts else None


def split_active(runs, now):
    """実行中/待機中の run を (健全, 停滞) に分ける。停滞は run_started_at から job の timeout を超えたものだけ。"""
    healthy, stale = [], []
    for r in runs:
        if r.get("status") not in ACTIVE:
            continue
        age = minutes_since(r.get("run_started_at") or r.get("created_at"), now)
        (stale if r.get("status") == "in_progress" and age is not None and age > STALE_MIN else healthy).append(r)
    return healthy, stale


def consecutive_failures(completed):
    """当日の完了 run(新しい順)のうち、先頭から連続する failure 系の数。"""
    n = 0
    for r in completed:
        if r.get("conclusion") not in FAILED:
            break
        n += 1
    return n


def decide(now, runs, state, today_articles, verified_ids, closed_daily_prs=(), redeploys_today=0,
           last_main_push_min=None):
    """watchdog の行動を決める(純粋関数)。
    now … UTC の datetime / runs … Daily Articles の run(新しい順。当日分以外も含んでよい)
    state … 日次状態 / today_articles … main の当日記事 [(id, slug)] / verified_ids … 本番 200 かつ一覧掲載の記事ID
    戻り値: (action, reason, detail)"""
    local = now.astimezone(JST)
    date = local.date().isoformat()
    verified = [i for i, _ in today_articles if i in set(verified_ids)]
    detail = {"date": date, "confirmed_on_main": len(today_articles), "verified": verified}
    minimum = ds.DAILY_MIN_ARTICLES
    if len(verified) >= minimum:
        return OK, f"本番で公開を確認できた当日記事 {len(verified)}/{minimum}件。復旧は不要", detail

    healthy, stale = split_active(runs, now)
    detail["stale_run_ids"] = [r.get("id") for r in stale]
    if healthy:
        return WAIT, f"Daily Articles run {[r.get('id') for r in healthy]} が実行中/待機中のため待つ", detail
    if (local.hour, local.minute) < START_AFTER:
        return WAIT, "当日の Daily Articles の予定時刻(JST 05:00)前のため待つ", detail

    # main には3本以上あるのに本番に出ていない: Daily Articles を再実行しても直らない。Pages の再ビルドだけ
    if len(today_articles) >= minimum:
        if last_main_push_min is not None and last_main_push_min < DEPLOY_GRACE_MIN:
            return WAIT, f"main 反映から{last_main_push_min:.0f}分。本番への反映を待つ", detail
        if redeploys_today >= MAX_REDEPLOYS:
            return HALT, (f"main には当日記事が{len(today_articles)}件あるが、本番で確認できたのは{len(verified)}件。"
                          f"Pages の再ビルドを{redeploys_today}回要求しても反映されない"), detail
        return REDEPLOY, (f"本番未反映: main の当日記事 {len(today_articles)}件のうち本番で確認できたのは"
                          f"{len(verified)}件。GitHub Pages の再ビルドを要求する"), detail

    if state.get("status") in ds.STOPPED:
        reason = (state.get("failure") or {}).get("reason") or state.get("status")
        return STOPPED, (f"日次状態が停止中({state.get('status')}: {reason})。bot からは再実行しない。"
                         "原因を解消してオーナーが Daily Shonan Doors Articles を手動実行すると再開する"), detail
    if closed_daily_prs:
        return STOPPED, f"当日PR {list(closed_daily_prs)} がマージされずに閉じられている(人が止めた)ため再実行しない", detail

    todays = [r for r in runs if jst_date(r.get("created_at")) == date]
    completed = [r for r in todays if r.get("status") == "completed"]
    detail["runs_today"] = len(todays)
    if len(todays) >= max_runs_per_day():
        return HALT, (f"当日の Daily Articles run が{len(todays)}本(上限{max_runs_per_day()}本)に達しても"
                      f"confirmed {len(today_articles)}/{minimum}件"), detail
    fails = consecutive_failures(completed)
    if fails >= MAX_CONSECUTIVE_FAILURES:
        return HALT, f"当日の Daily Articles run が{fails}回連続で失敗している", detail
    if completed:
        ended = minutes_since(completed[0].get("updated_at"), now)
        if ended is not None and ended < COOLDOWN_MIN:
            return WAIT, f"直前の run {completed[0].get('id')} の終了から{ended:.0f}分。補充 run の起動を待つ", detail

    if not todays:
        why = "cron 欠落/遅延: 当日の Daily Articles run がまだ起動していない"
    elif completed and completed[0].get("conclusion") in FAILED:
        why = f"失敗: 当日の最後の run {completed[0].get('id')} が {completed[0].get('conclusion')}"
    else:
        why = f"3本未達: 当日の confirmed {len(today_articles)}/{minimum}件で、実行中の run が無い"
    if stale:
        why += f"(停滞 run {[r.get('id') for r in stale]} は取り消す)"
    return DISPATCH, why, detail


# ---------- 本番の確認 ----------

def http_ok(url, timeout=20):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ShonanDoorsWatchdog/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def fetch_text(url, timeout=20):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ShonanDoorsWatchdog/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace") if resp.status == 200 else ""
    except Exception:
        return ""


def verify_production(today_articles, check=http_ok, listing=None):
    """当日記事のうち、本番URL /articles/{slug}/ が HTTP 200 で、トップページの一覧にも載っている記事のID。"""
    if not today_articles:
        return [], {}
    if listing is None:
        listing = fetch_text(f"{SITE_DOMAIN}/?watchdog={int(time.time())}")
    out, status = [], {}
    for aid, slug in today_articles:
        live = check(f"{SITE_DOMAIN}/articles/{slug}/")
        listed = f"/articles/{slug}/" in listing
        status[aid] = {"slug": slug, "http200": live, "listed": listed}
        if live and listed:
            out.append(aid)
    return out, status


def today_articles_from(articles, date):
    """main の当日記事(統合済みを除く)を [(id, slug)] で返す。slug の無い記事は URL を確かめられないので除く。"""
    return [(a.get("id"), a.get("slug")) for a in articles or []
            if a.get("date") == date and not a.get("mergedInto") and a.get("slug")]


# ---------- GitHub API ----------

class GitHub:
    def __init__(self, repo, token):
        self.repo, self.token = repo, token

    def call(self, method, path, payload=None, accept="application/vnd.github+json", raw=False):
        req = urllib.request.Request(
            API + path.replace("{repo}", self.repo), method=method,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Accept": accept,
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "shonan-doors-watchdog"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            if raw:
                return body
            return json.loads(body or "null")

    def runs(self, workflow, per_page=50):
        res = self.call("GET", f"/repos/{{repo}}/actions/workflows/{workflow}/runs?per_page={per_page}") or {}
        return res.get("workflow_runs") or []

    def file(self, path, ref):
        try:
            res = self.call("GET", f"/repos/{{repo}}/contents/{path}?ref={ref}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        if res.get("content") and res.get("encoding") == "base64":
            return base64.b64decode(res["content"]).decode("utf-8")
        # 1MB を超えるファイルは raw で取り直す
        return self.call("GET", f"/repos/{{repo}}/contents/{path}?ref={ref}",
                         accept="application/vnd.github.raw", raw=True)


def load_state(gh, date):
    text = gh.file(ds.STATE_REL, ds.BRANCH)
    try:
        data = json.loads(text) if text else {"days": {}}
    except ValueError:
        data = {"days": {}}
    return ds.normalize_state((data.get("days") or {}).get(date), date)


def closed_daily_prs(gh, date):
    pulls = gh.call("GET", "/repos/{repo}/pulls?state=closed&base=main&sort=created&direction=desc&per_page=50") or []
    pattern = re.compile(rf"^daily/{re.escape(date)}(-r\d+)?$")
    return [p.get("number") for p in pulls
            if pattern.match((p.get("head") or {}).get("ref") or "") and not p.get("merged_at")]


def main_pushed_minutes_ago(gh, now):
    commit = gh.call("GET", "/repos/{repo}/commits/main") or {}
    stamp = ((commit.get("commit") or {}).get("committer") or {}).get("date")
    return minutes_since(stamp, now)


def redeploys_since(gh, pushed_min, now):
    """main の最新 push 以降に watchdog が要求した Pages の再ビルド数(push 自体のビルド1回を除く)。"""
    if pushed_min is None:
        return 0
    try:
        builds = gh.call("GET", "/repos/{repo}/pages/builds?per_page=30") or []
    except urllib.error.HTTPError:
        return 0
    since = [b for b in builds if (minutes_since(b.get("created_at"), now) or 0) <= pushed_min + 1]
    return max(0, len(since) - 1)


def open_issue(gh, title, body):
    issues = gh.call("GET", "/repos/{repo}/issues?state=open&per_page=100") or []
    if any(i.get("title") == title for i in issues if "pull_request" not in i):
        print(f"Issue「{title}」は既にあります。")
        return
    gh.call("POST", "/repos/{repo}/issues", {"title": title, "body": body})


def summary(text):
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n\n")


def main(argv=None, now=None):
    dry_run = "--dry-run" in (sys.argv[1:] if argv is None else argv)  # 判定だけ表示し、起動・Issue化はしない
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY") or os.environ.get("REPO")
    if not token or not repo:
        print("::error::GH_TOKEN と GITHUB_REPOSITORY が必要です", file=sys.stderr)
        return 1
    gh = GitHub(repo, token)
    now = now or datetime.now(timezone.utc)
    date = now.astimezone(JST).date().isoformat()

    articles = json.loads(gh.file("data/articles.json", "main") or "[]")
    today = today_articles_from(articles, date)
    verified, status = verify_production(today)
    runs = gh.runs(DAILY_WORKFLOW)
    state = load_state(gh, date)
    closed = closed_daily_prs(gh, date)
    pushed = main_pushed_minutes_ago(gh, now)
    action, reason, detail = decide(now, runs, state, today, verified, closed,
                                    redeploys_today=redeploys_since(gh, pushed, now), last_main_push_min=pushed)
    lines = [f"## Daily Publication Watchdog ({date})", "",
             f"- 判定: **{action}** — {reason}",
             f"- main の当日記事: {len(today)}件 / 本番 200 かつ一覧掲載: {len(verified)}件",
             f"- 日次状態: {state.get('status')}(confirmed_today={state.get('confirmed_today')})"]
    lines += [f"  - id {i}: `/articles/{s['slug']}/` HTTP200={s['http200']} 一覧={s['listed']}" for i, s in status.items()]
    summary("\n".join(lines))

    if dry_run:
        print("[dry-run] 起動・再ビルド要求・Issue化は行いません。")
        return 0
    if action == DISPATCH:
        for run_id in detail.get("stale_run_ids") or []:
            try:
                gh.call("POST", f"/repos/{{repo}}/actions/runs/{run_id}/cancel")
                print(f"停滞 run {run_id} を取り消しました。")
            except urllib.error.HTTPError as e:
                print(f"::warning::停滞 run {run_id} を取り消せませんでした: {e}")
        gh.call("POST", f"/repos/{{repo}}/actions/workflows/{DAILY_WORKFLOW}/dispatches", {"ref": "main"})
        summary(f"Daily Shonan Doors Articles を起動しました(理由: {reason})。")
    elif action == REDEPLOY:
        try:
            gh.call("POST", "/repos/{repo}/pages/builds")
            summary("GitHub Pages の再ビルドを要求しました。")
        except urllib.error.HTTPError as e:
            summary(f"::error::GitHub Pages の再ビルドを要求できませんでした: HTTP {e.code}")
            open_issue(gh, f"{ISSUE_PREFIX} ({date})",
                       f"本番未反映を検出しましたが、Pages の再ビルドを要求できませんでした(HTTP {e.code})。\n\n- {reason}\n\n"
                       "watchdog の workflow に `pages: write` 権限があるか、Pages の設定を確認してください。")
            return 1
    elif action == HALT:
        open_issue(gh, f"{ISSUE_PREFIX} ({date})", "\n".join([
            f"{date} の公開を自動で復旧できないため、watchdog は再実行を止めました(暴走・API浪費の防止)。", "",
            f"- 理由: {reason}",
            f"- main の当日記事: {len(today)}件 / 本番で確認: {len(verified)}件",
            f"- 日次状態: {state.get('status')}", "",
            "Fact Audit・一次情報・鮮度・重複の基準は緩めていません。原因を確認し、必要ならオーナーが Actions で"
            "「Daily Shonan Doors Articles」を手動実行してください。",
        ]))
        # 通知は Issue で行う。30分ごとの watchdog を毎回 failure にはしない
        print(f"::error::{reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
