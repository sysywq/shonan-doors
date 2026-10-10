#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily Articles の独立 watchdog(daily-publication-watchdog.yml から30分ごとに実行)
------------------------------------------------------------------------
その日(JST)の「本番で公開済みの記事が3本以上」を最終基準にして、次のどれかを検出したときだけ動く。

  - cron 欠落・遅延 … 05:30 JST を過ぎても当日の Daily Articles run が1件もない → workflow_dispatch で起動
  - 失敗・3本未達   … 実行中の run がなく、main の当日記事が3本未満 → workflow_dispatch で起動
                      (daily_pr.py plan が当日branch/PRを再利用し、不足分だけの補充 round にする)
  - 本番未反映      … main には当日記事が3本以上あるのに、本番URL(HTTP 200)・トップページ一覧に出ない
                      → GitHub Pages の再ビルドを要求し、watchdog を failure にして通知する

暴走・浪費の防止:
  - 実行中・待機中の run がある間は何もしない(実行中の run の updated_at は進まないため、経過時間は
    run_started_at と job の timeout から判断する。正常な run を stale と誤判定して重複起動しない)
  - 日次状態(bot/daily-state)が停止(system_failure / round_limit)の日は起動しない(オーナーの手動実行で再開)
  - bot が起動した当日の run 数に上限(DAILY_MAX_REFILL_ROUNDS + WATCHDOG_EXTRA_DISPATCHES)
  - 直前に終わった run から一定時間は起動しない / 日付をまたぐ時間帯(22時以降)は起動しない
記事の生成・Fact Audit・一次情報・重複排除の基準はこのモジュールでは一切扱わない(起動するかどうかだけ)。
"""
import base64
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import daily_state as ds
import report_gate_rejections as rgr

JST = ZoneInfo("Asia/Tokyo")
SITE_DOMAIN = "https://www.shonandoors.com"
DAILY_WORKFLOW = "daily-articles.yml"
ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")
# daily-articles.yml の timeout-minutes(300)+余裕。これを超えた in_progress だけを stale とみなす
ACTIVE_MAX_MIN = int(os.environ.get("WATCHDOG_ACTIVE_MAX_MIN", "320"))
FIRST_DISPATCH_AT = (5, 30)   # cron(04:13 JST)が遅延・欠落したとみなす時刻
LAST_DISPATCH_HOUR = 22       # これ以降は起動しない(run が日付をまたがないように)
COOLDOWN_MIN = 10             # 直前の run 終了から、補充 run の起動が一覧に出るまでの猶予
DEPLOY_GRACE_MIN = 20         # main 反映から本番反映までの猶予
WATCHDOG_EXTRA_DISPATCHES = int(os.environ.get("WATCHDOG_EXTRA_DISPATCHES", "3"))


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def jst_date(s):
    t = parse_ts(s)
    return t.astimezone(JST).date().isoformat() if t else ""


def is_bot(run):
    actor = ((run.get("triggering_actor") or run.get("actor") or {}).get("login") or "")
    return actor.endswith("[bot]")


def decide(now, runs, state, main_ids, live_ids, main_pushed_at=None):
    """何をするかを決める(純粋関数)。
    now: aware datetime / runs: daily-articles.yml の workflow runs(新しい順)/ state: 当日の日次状態
    main_ids: main の当日記事ID / live_ids: 本番で HTTP 200 かつ一覧に出ている当日記事ID
    戻り値: (action, reason)。action は wait / ok / stopped / dispatch / limit / deploy_lag"""
    today = now.astimezone(JST).date().isoformat()
    need = ds.DAILY_MIN_ARTICLES
    if len(live_ids) >= need:
        return "ok", f"本番で公開を確認した当日記事 {len(live_ids)}/{need}本"

    for r in runs:
        if r.get("status") not in ACTIVE:
            continue
        started = parse_ts(r.get("run_started_at") or r.get("created_at"))
        age = (now - started).total_seconds() / 60 if started else 0
        if r.get("status") != "in_progress" or age < ACTIVE_MAX_MIN:
            return "wait", f"run {r.get('id')} が {r.get('status')}({age:.0f}分経過)。終わるまで待つ"

    if len(main_ids) >= need:
        if main_pushed_at and (now - main_pushed_at).total_seconds() / 60 < DEPLOY_GRACE_MIN:
            return "wait", f"main の当日記事 {len(main_ids)}本。本番反映を待つ"
        return "deploy_lag", (f"main の当日記事 {len(main_ids)}本に対し、本番で確認できたのは {len(live_ids)}本"
                              f"(未反映: {[i for i in main_ids if i not in live_ids]})")

    if (state or {}).get("status") in ds.STOPPED:
        reason = ((state.get("failure") or {}).get("reason") or state["status"])
        return "stopped", f"日次状態が停止中({reason})。オーナーの手動実行で再開する"

    local = now.astimezone(JST)
    todays = [r for r in runs if jst_date(r.get("created_at")) == today]
    if not todays and (local.hour, local.minute) < FIRST_DISPATCH_AT:
        return "wait", "定時 run(cron)の起動を待つ"
    if local.hour >= LAST_DISPATCH_HOUR:
        return "limit", f"{LAST_DISPATCH_HOUR}時以降は日付をまたぐため起動しない(当日記事 {len(main_ids)}/{need}本)"
    done = [parse_ts(r.get("updated_at")) for r in todays if r.get("status") == "completed" and r.get("updated_at")]
    if done and (now - max(done)).total_seconds() / 60 < COOLDOWN_MIN:
        return "wait", "直前の run が終わったばかり(補充 run の起動を待つ)"
    cap = ds.DAILY_MAX_REFILL_ROUNDS + WATCHDOG_EXTRA_DISPATCHES
    bot_runs = [r for r in todays if r.get("event") == "workflow_dispatch" and is_bot(r)]
    if len(bot_runs) >= cap:
        return "limit", f"bot が起動した当日の run が上限 {cap}件に達した(当日記事 {len(main_ids)}/{need}本)"
    why = "当日の run がない(cron 欠落・遅延)" if not todays else f"実行中の run がなく当日記事 {len(main_ids)}/{need}本"
    return "dispatch", why


# ---------- I/O ----------

def url_ok(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ShonanDoorsWatchdog/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status == 200
    except Exception:
        return False


def fetch_text(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ShonanDoorsWatchdog/1.0",
                                                   "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"::warning::{url} を取得できません: {e}")
        return ""


def live_articles(rows, check=url_ok, listing=None):
    """本番で記事URL(/articles/<slug>/)が HTTP 200 で、トップページ一覧にも載っている記事ID。"""
    listing = fetch_text(f"{SITE_DOMAIN}/") if listing is None else listing
    live = []
    for a in rows:
        slug = str(a.get("slug") or "").strip()
        if not slug:
            continue
        path = f"/articles/{slug}/"
        ok, listed = check(SITE_DOMAIN + path), path in listing
        print(f"- id:{a.get('id')} {path} HTTP200={ok} 一覧={listed}")
        if ok and listed:
            live.append(a.get("id"))
    return live


def load_state(call, date):
    try:
        res = call("GET", f"/repos/{{repo}}/contents/{ds.STATE_REL}?ref={ds.BRANCH}")
        data = json.loads(base64.b64decode(res.get("content") or "").decode("utf-8") or "{}")
    except Exception as e:  # データbranch が読めなくても、本番の公開状況で判断する
        print(f"::warning::日次状態を読めません: {e}")
        data = {}
    return ds.normalize_state((data.get("days") or {}).get(date), date)


def main(now=None, request=rgr.github_request, check=url_ok, listing=None):
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY") or os.environ.get("REPO")
    if not token or not repo:
        print("::error::GH_TOKEN と GITHUB_REPOSITORY が必要です")
        return 1

    def call(method, path, payload=None):
        return request(method, path.replace("{repo}", repo), token, payload)

    now = now or datetime.now(timezone.utc)
    today = now.astimezone(JST).date().isoformat()
    runs = (call("GET", f"/repos/{{repo}}/actions/workflows/{DAILY_WORKFLOW}/runs?per_page=30") or {}).get(
        "workflow_runs") or []
    state = load_state(call, today)
    contents = call("GET", "/repos/{repo}/contents/data/articles.json?ref=main") or {}
    if contents.get("content"):
        articles = json.loads(base64.b64decode(contents["content"]).decode("utf-8"))
    else:  # 1MB を超えると content が空になるため blob で取る
        blob = call("GET", f"/repos/{{repo}}/git/blobs/{contents.get('sha')}") or {}
        articles = json.loads(base64.b64decode(blob.get("content") or "").decode("utf-8") or "[]")
    rows = [a for a in articles if a.get("date") == today and not a.get("mergedInto")]
    main_ids = [a.get("id") for a in rows]
    print(f"## Daily Publication Watchdog ({today})\nmain の当日記事: {main_ids}")
    live = live_articles(rows, check=check, listing=listing)
    head = (call("GET", "/repos/{repo}/commits/main") or {}).get("commit") or {}
    pushed = parse_ts(((head.get("committer") or {}).get("date")))
    action, reason = decide(now, runs, state, main_ids, live, pushed)
    print(f"判定: {action} — {reason}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"## Daily Publication Watchdog ({today})\n\n- main の当日記事: {main_ids}\n"
                    f"- 本番で確認(HTTP 200・一覧掲載): {live}\n- 判定: **{action}** — {reason}\n")
    if action == "dispatch":
        call("POST", f"/repos/{{repo}}/actions/workflows/{DAILY_WORKFLOW}/dispatches", {"ref": "main"})
        print("Daily Shonan Doors Articles を起動しました(当日branch/PRは daily_pr.py plan が再利用する)")
        return 0
    if action == "deploy_lag":
        try:
            call("POST", "/repos/{repo}/pages/builds")
            print("GitHub Pages の再ビルドを要求しました")
        except Exception as e:
            print(f"::warning::GitHub Pages の再ビルドを要求できません: {e}")
        print(f"::error::本番未反映: {reason}")
        return 1
    if action == "limit":
        print(f"::error::{reason}")
        return 1
    if action == "stopped":
        print(f"::warning::{reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
