#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
X投稿済みログ(data/x_post_log.json)の保存先
------------------------------------------------------------------------
main への直接pushをやめるため、投稿直後のログは保護されていない専用のデータbranch
(LOG_BRANCH。x_post_log.json 1ファイルだけを持つ)に保存する。main には push しない。

  pull … LOG_BRANCH のログを data/x_post_log.json に取り込む(article_id で和集合)。
         X投稿の直前(重複投稿防止)と、Daily Articles の当日PRを作る前に呼ぶ。
         当日PRに data/x_post_log.json を含めることで、main の控えもPR経由で最新になる。
  push … data/x_post_log.json を LOG_BRANCH に保存する(作業ツリー・現在のbranchは変更しない)。
         先に LOG_BRANCH の最新を取り込んでから commit するため、ほかの実行の記録を消さない。
         競合で push が拒否されたら取り込み直して再試行する。

LOG_BRANCH は main とは別の branch で、GITHUB_TOKEN(contents: write)で更新できる。
main の保護ルールは変更しない。

使い方:
  python x_post_log_store.py pull
  python x_post_log_store.py push [--message "..."]
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
LOCAL_PATH = os.path.join(ROOT, "data", "x_post_log.json")
LOG_BRANCH = os.environ.get("X_POST_LOG_BRANCH", "x-post-log")
BRANCH_FILE = "x_post_log.json"
PUSH_RETRIES = 3
BOT_ENV = {
    "GIT_AUTHOR_NAME": "shonan-doors-bot", "GIT_AUTHOR_EMAIL": "bot@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "shonan-doors-bot", "GIT_COMMITTER_EMAIL": "bot@users.noreply.github.com",
}


def merge_logs(*logs):
    """複数のログを article_id で和集合にする。同じ記事の記録は先に出てきたものを残し、
    並び順は先のログの順を保ったまま、無い記録を後ろに足す。"""
    seen, out = set(), []
    for log in logs:
        for r in (log or {}).get("posts") or []:
            if isinstance(r, dict) and "article_id" in r and r["article_id"] not in seen:
                seen.add(r["article_id"])
                out.append(r)
    return {"posts": out}


def posted_ids(log):
    return {r.get("article_id") for r in (log or {}).get("posts") or [] if isinstance(r, dict)}


def git(args, runner=subprocess.run, env=None, input_text=None):
    return runner(["git"] + args, cwd=ROOT, capture_output=True, text=True,
                  env=dict(os.environ, **(env or {})), input=input_text)


def fetch_branch_log(runner=subprocess.run):
    """LOG_BRANCH の先頭commitとログを返す。branch が無ければ ("", None)。"""
    r = git(["fetch", "--quiet", "origin", f"+refs/heads/{LOG_BRANCH}:refs/remotes/origin/{LOG_BRANCH}"], runner)
    if r.returncode != 0:
        return "", None
    sha = git(["rev-parse", f"refs/remotes/origin/{LOG_BRANCH}"], runner).stdout.strip()
    show = git(["show", f"{sha}:{BRANCH_FILE}"], runner)
    if show.returncode != 0:
        return sha, None
    try:
        return sha, json.loads(show.stdout)
    except ValueError:
        return sha, None


def load_local(path=LOCAL_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"posts": []}


def save_local(data, path=LOCAL_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def pull(runner=subprocess.run, path=LOCAL_PATH):
    _sha, remote = fetch_branch_log(runner)
    local = load_local(path)
    merged = merge_logs(local, remote)
    if merged != local:
        save_local(merged, path)
    print(f"X投稿済みログ: {LOG_BRANCH} から取り込み → 累計{len(merged['posts'])}件")
    return merged


def push(message, runner=subprocess.run, path=LOCAL_PATH):
    """ログを LOG_BRANCH に保存する。変更が無ければ何もしない。戻り値: 0=成功 / 1=失敗"""
    for attempt in range(1, PUSH_RETRIES + 1):
        parent, remote = fetch_branch_log(runner)
        merged = merge_logs(load_local(path), remote)
        if remote is not None and posted_ids(merged) == posted_ids(remote):
            print("X投稿済みログに新しい記録はありません(保存不要)。")
            return 0
        save_local(merged, path)
        content = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
        blob = git(["hash-object", "-w", "--stdin"], runner, input_text=content).stdout.strip()
        tree = git(["mktree"], runner, input_text=f"100644 blob {blob}\t{BRANCH_FILE}\n").stdout.strip()
        commit_args = ["commit-tree", tree, "-m", message] + (["-p", parent] if parent else [])
        commit = git(commit_args, runner, env=BOT_ENV).stdout.strip()
        if not (blob and tree and commit):
            print("::error::X投稿済みログのcommitを作れませんでした", file=sys.stderr)
            return 1
        r = git(["push", "origin", f"{commit}:refs/heads/{LOG_BRANCH}"], runner)
        if r.returncode == 0:
            print(f"X投稿済みログを {LOG_BRANCH} に保存しました(累計{len(merged['posts'])}件)。")
            return 0
        print(f"push が拒否されました({attempt}/{PUSH_RETRIES})。最新を取り込んで再試行します。")
    print(f"::error::X投稿済みログを {LOG_BRANCH} に保存できませんでした", file=sys.stderr)
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pull")
    p = sub.add_parser("push")
    p.add_argument("--message", default="chore: X投稿済みログを更新")
    args = ap.parse_args(argv)
    if args.cmd == "pull":
        pull()
        return 0
    return push(args.message)


if __name__ == "__main__":
    sys.exit(main())
